"""The restore-initiation handler: input validation, planning, and the job loop."""

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from handlers import initiate_restores as ir
from handlers.initiate_restores import (
    DYNAMODB_RESTORE_METHOD_CHOICES,
    RESTORE_METHOD_CAPACITY_BASED,
    RESTORE_METHOD_IMPORT_TABLE,
    handler,
)

VPC_CONFIGS = [
    {
        "region": "us-east-1",
        "vpc": "vpc-1",
        "subnetsPerAvailabilityZone": [{"availabilityZone": "us-east-1a", "subnetId": "subnet-1a"}],
        "securityGroups": {"restoreServer": ["sg-1"], "restoredRdsInstance": ["sg-2"]},
    }
]

BASE_EVENT = {
    "resourceSnapshots": [],
    "eonRestoreAccountId": "eon-acct",
    "restoreAccountId": "222222222222",
    "restoreRegion": "us-east-1",
    "kmsKeyArnsByRegion": {"us-east-1": "arn:aws:kms:us-east-1:222222222222:key/abc"},
    "rdsSubnetGroupsByRegion": {"us-east-1": "eon-restore-us-east-1"},
    "vpcConfigs": VPC_CONFIGS,
    "recoveryStackNames": [],
    "recoveryStacksOnly": False,
    "excludeEC2TagKeys": [],
    "dynamodbRegionalWcuLimit": 40000,
    "dynamodbTableWcuMax": 40000,
    "dynamodbRestoreMethod": "auto",
    "crossAccountRoleArn": None,
    "resourceNamePrefix": None,
    "s3InPlaceTagKey": None,
}


def ddb_snapshot(resource_id="res-ddb", name="orders", size=10 * 1024 ** 3, region="us-east-1"):
    return {
        "resourceId": resource_id,
        "resourceName": name,
        "resourceType": "AWS_DYNAMO_DB",
        "snapshotId": f"snap-{resource_id}",
        "snapshotPointInTime": "2026-09-08T02:00:00Z",
        "region": region,
        "tableSizeBytes": size,
        "originalTags": {},
    }


@pytest.fixture
def wiring(monkeypatch, eon_credentials, fake_eon_client):
    """
    Stub everything the handler reaches outside itself, and hand back the fake
    Eon client so tests can assert on what was submitted.
    """
    monkeypatch.setattr(
        ir,
        "get_cross_account_credentials",
        lambda **kwargs: {"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"},
    )
    monkeypatch.setattr(ir, "create_boto3_client", lambda *args, **kwargs: MagicMock())

    def _wire(**responses):
        client = fake_eon_client(**responses)
        monkeypatch.setattr(ir, "EonClient", lambda **kwargs: client)
        return client

    return _wire


def importable(available=True, reasons=None):
    return {
        "list_restore_accounts": {"accounts": [{"id": "eon-acct", "version": {"installed": "1.15.0"}}]},
        "check_dynamodb_import_availability": {
            "available": available,
            "reasons": reasons or [],
        },
    }


class TestRestoreMethodInput:
    @pytest.mark.parametrize("value", list(DYNAMODB_RESTORE_METHOD_CHOICES))
    def test_every_documented_value_is_accepted(self, wiring, value):
        wiring(**importable())

        result = handler(dict(BASE_EVENT, dynamodbRestoreMethod=value), None)

        assert result["totalJobs"] == 0

    @pytest.mark.parametrize("value", ["AUTO", " Import ", "CAPACITY"])
    def test_case_and_whitespace_are_normalised(self, wiring, value):
        wiring(**importable())

        assert handler(dict(BASE_EVENT, dynamodbRestoreMethod=value), None)["totalJobs"] == 0

    @pytest.mark.parametrize("value", [None, ""])
    def test_an_absent_value_defaults_to_auto(self, wiring, value, capsys):
        wiring(**importable())

        handler(dict(BASE_EVENT, dynamodbRestoreMethod=value), None)

        assert "DynamoDB restore method: auto" in capsys.readouterr().out

    def test_the_key_being_missing_entirely_also_defaults_to_auto(self, wiring, capsys):
        wiring(**importable())
        event = {k: v for k, v in BASE_EVENT.items() if k != "dynamodbRestoreMethod"}

        handler(event, None)

        assert "DynamoDB restore method: auto" in capsys.readouterr().out

    def test_an_unknown_value_fails_the_step(self, wiring):
        wiring(**importable())

        with pytest.raises(ValueError, match="Invalid dynamodbRestoreMethod 'turbo'"):
            handler(dict(BASE_EVENT, dynamodbRestoreMethod="turbo"), None)


class TestDynamoDBPlanning:
    def test_importable_tables_are_left_out_of_the_wcu_budget(self, wiring):
        """The whole point: an import restore must not consume the capacity budget."""

        def availability(snapshot_id, region, restored_name):
            return {
                "available": snapshot_id == "snap-res-a",
                "reasons": [] if snapshot_id == "snap-res-a" else ["RESTORE_METHOD_SIZE_LIMIT"],
            }

        client = wiring(
            list_restore_accounts={"accounts": [{"id": "eon-acct", "version": {"installed": "1.15.0"}}]},
            check_dynamodb_import_availability=availability,
            restore_dynamodb_table="job-1",
        )
        event = dict(
            BASE_EVENT,
            resourceSnapshots=[
                ddb_snapshot("res-a", "orders", 30 * 1024 ** 3),
                ddb_snapshot("res-b", "legacy", 10 * 1024 ** 3),
            ],
        )

        result = handler(event, None)

        by_name = {job["resourceName"]: job for job in result["restoreJobs"]}
        assert by_name["orders"]["restoreMethod"] == RESTORE_METHOD_IMPORT_TABLE
        assert "writeCapacityUnits" not in by_name["orders"]
        # The fallback table gets the entire budget rather than a 25% share.
        assert by_name["legacy"]["writeCapacityUnits"] == 38000

    def test_capacity_mode_skips_the_probe_entirely(self, wiring):
        client = wiring(restore_dynamodb_table="job-1")
        event = dict(
            BASE_EVENT,
            dynamodbRestoreMethod="capacity",
            resourceSnapshots=[ddb_snapshot()],
        )

        result = handler(event, None)

        assert client.calls_to("check_dynamodb_import_availability") == []
        assert client.calls_to("list_restore_accounts") == []
        assert result["restoreJobs"][0]["restoreMethod"] == RESTORE_METHOD_CAPACITY_BASED

    def test_import_mode_forces_the_method(self, wiring):
        client = wiring(restore_dynamodb_table="job-1")
        event = dict(
            BASE_EVENT, dynamodbRestoreMethod="import", resourceSnapshots=[ddb_snapshot()]
        )

        result = handler(event, None)

        assert result["restoreJobs"][0]["restoreMethod"] == RESTORE_METHOD_IMPORT_TABLE
        assert client.calls_to("restore_dynamodb_table")[0]["restore_method"] == (
            RESTORE_METHOD_IMPORT_TABLE
        )

    def test_in_place_tables_are_never_planned_for_import(self, wiring, monkeypatch):
        client = wiring(**importable(), restore_dynamodb_to_existing_table="job-2")
        monkeypatch.setattr(
            ir,
            "discover_dynamodb_tables_from_stacks",
            lambda **kwargs: {
                "orders": {
                    "tableName": "orders",
                    "region": "us-east-1",
                    "regions": ["us-east-1"],
                    "stackName": "OrdersStack",
                }
            },
        )
        monkeypatch.setattr(ir, "discover_s3_buckets_from_stacks", lambda **kwargs: {})
        monkeypatch.setattr(ir, "_scale_up_dynamodb_table_wcu", lambda **kwargs: {"wcuScaledUp": True})
        event = dict(
            BASE_EVENT, recoveryStackNames=["OrdersStack"], resourceSnapshots=[ddb_snapshot()]
        )

        result = handler(event, None)

        assert client.calls_to("check_dynamodb_import_availability") == []
        job = result["restoreJobs"][0]
        assert job["restoreType"] == "IN_PLACE"
        assert job["restoreMethod"] == RESTORE_METHOD_CAPACITY_BASED

    def test_an_in_place_table_draws_from_its_own_stack_region_budget(self, wiring, monkeypatch):
        wiring(**importable(), restore_dynamodb_to_existing_table="job-2")
        monkeypatch.setattr(
            ir,
            "discover_dynamodb_tables_from_stacks",
            lambda **kwargs: {
                "orders": {
                    "tableName": "orders",
                    "region": "eu-west-1",
                    "regions": ["us-east-1"],
                    "stackName": "OrdersStack",
                }
            },
        )
        monkeypatch.setattr(ir, "discover_s3_buckets_from_stacks", lambda **kwargs: {})
        monkeypatch.setattr(ir, "_scale_up_dynamodb_table_wcu", lambda **kwargs: {"wcuScaledUp": True})
        event = dict(
            BASE_EVENT,
            recoveryStackNames=["OrdersStack"],
            kmsKeyArnsByRegion=dict(BASE_EVENT["kmsKeyArnsByRegion"], **{"eu-west-1": "arn:eu"}),
            resourceSnapshots=[ddb_snapshot()],
        )

        result = handler(event, None)

        assert result["restoreJobs"][0]["restoredRegion"] == "eu-west-1"
        assert result["restoreJobs"][0]["writeCapacityUnits"] == 38000

    def test_stacks_only_mode_drops_unmatched_tables_before_planning(self, wiring, monkeypatch):
        client = wiring(**importable())
        monkeypatch.setattr(ir, "discover_dynamodb_tables_from_stacks", lambda **kwargs: {"other": {}})
        monkeypatch.setattr(ir, "discover_s3_buckets_from_stacks", lambda **kwargs: {})
        event = dict(
            BASE_EVENT,
            recoveryStackNames=["OrdersStack"],
            recoveryStacksOnly=True,
            resourceSnapshots=[ddb_snapshot()],
        )

        result = handler(event, None)

        assert result["totalJobs"] == 0
        assert client.calls_to("check_dynamodb_import_availability") == []

    def test_a_probe_failure_falls_back_and_still_restores(self, wiring):
        def boom(**_kwargs):
            raise RuntimeError("403")

        wiring(
            list_restore_accounts={"accounts": [{"id": "eon-acct", "version": {"installed": "1.15.0"}}]},
            check_dynamodb_import_availability=boom,
            restore_dynamodb_table="job-1",
        )
        event = dict(BASE_EVENT, resourceSnapshots=[ddb_snapshot()])

        result = handler(event, None)

        job = result["restoreJobs"][0]
        assert job["restoreMethod"] == RESTORE_METHOD_CAPACITY_BASED
        assert "403" in job["restoreMethodReasons"][0]


class TestJobLoop:
    def test_a_failing_resource_does_not_stop_the_others(self, wiring):
        def restore(**kwargs):
            if kwargs["resource_id"] == "res-a":
                raise RuntimeError("API rejected")
            return "job-2"

        wiring(**importable(available=False), restore_dynamodb_table=restore)
        event = dict(
            BASE_EVENT,
            resourceSnapshots=[ddb_snapshot("res-a", "orders"), ddb_snapshot("res-b", "users")],
        )

        result = handler(event, None)

        assert result["totalJobs"] == 2
        assert result["successfulJobs"] == 1
        assert result["failedJobs"] == 1
        failed = [job for job in result["restoreJobs"] if job["jobId"] is None][0]
        assert failed["status"] == "FAILED_TO_INITIATE"
        assert "API rejected" in failed["error"]

    def test_a_missing_job_id_is_not_recorded_as_a_job(self, wiring, capsys):
        wiring(**importable(available=False), restore_dynamodb_table=None)
        event = dict(BASE_EVENT, resourceSnapshots=[ddb_snapshot()])

        result = handler(event, None)

        assert result["totalJobs"] == 0
        assert "No job ID returned for orders" in capsys.readouterr().out

    def test_an_unsupported_resource_type_is_ignored(self, wiring):
        wiring()
        snapshot = dict(ddb_snapshot(), resourceType="AWS_EFS")
        event = dict(BASE_EVENT, resourceSnapshots=[snapshot])

        assert handler(event, None)["totalJobs"] == 0

    def test_stacks_only_mode_skips_ec2_and_rds_outright(self, wiring, monkeypatch, capsys):
        wiring()
        monkeypatch.setattr(ir, "discover_dynamodb_tables_from_stacks", lambda **kwargs: {})
        monkeypatch.setattr(ir, "discover_s3_buckets_from_stacks", lambda **kwargs: {})
        snapshots = [
            dict(ddb_snapshot("res-ec2", "web"), resourceType="AWS_EC2"),
            dict(ddb_snapshot("res-rds", "db"), resourceType="AWS_RDS"),
        ]
        event = dict(
            BASE_EVENT,
            recoveryStackNames=["S"],
            recoveryStacksOnly=True,
            resourceSnapshots=snapshots,
        )

        result = handler(event, None)

        assert result["totalJobs"] == 0
        assert "no stack matching for this type" in capsys.readouterr().out

    def test_job_records_carry_the_snapshot_provenance(self, wiring):
        wiring(**importable(), restore_dynamodb_table="job-1")
        event = dict(BASE_EVENT, resourceSnapshots=[ddb_snapshot()])

        job = handler(event, None)["restoreJobs"][0]

        assert job["snapshotId"] == "snap-res-ddb"
        assert job["snapshotPointInTime"] == "2026-09-08T02:00:00Z"
        assert job["sourceRegion"] == "us-east-1"
        assert job["status"] == "INITIATED"


class TestStackDiscoveryWiring:
    def test_stack_discovery_runs_only_when_stacks_are_named(self, wiring, monkeypatch):
        wiring(**importable())
        calls = []
        monkeypatch.setattr(
            ir, "discover_dynamodb_tables_from_stacks", lambda **kwargs: calls.append(kwargs) or {}
        )
        monkeypatch.setattr(ir, "discover_s3_buckets_from_stacks", lambda **kwargs: {})

        handler(dict(BASE_EVENT), None)
        assert calls == []

        handler(dict(BASE_EVENT, recoveryStackNames=["S"]), None)
        assert len(calls) == 1
        assert calls[0]["stack_names"] == ["S"]
        assert calls[0]["regions"] == ["us-east-1"]

    def test_the_s3_tag_key_defaults_when_null(self, wiring, monkeypatch):
        wiring(**importable())
        seen = {}
        monkeypatch.setattr(ir, "discover_dynamodb_tables_from_stacks", lambda **kwargs: {})
        monkeypatch.setattr(
            ir, "discover_s3_buckets_from_stacks", lambda **kwargs: seen.update(kwargs) or {}
        )

        handler(dict(BASE_EVENT, recoveryStackNames=["S"], s3InPlaceTagKey=None), None)

        assert seen["s3_in_place_tag_key"] == "eon_functional_id"

    def test_a_custom_s3_tag_key_is_passed_through(self, wiring, monkeypatch, capsys):
        wiring(**importable())
        seen = {}
        monkeypatch.setattr(ir, "discover_dynamodb_tables_from_stacks", lambda **kwargs: {})
        monkeypatch.setattr(
            ir, "discover_s3_buckets_from_stacks", lambda **kwargs: seen.update(kwargs) or {}
        )

        handler(dict(BASE_EVENT, recoveryStackNames=["S"], s3InPlaceTagKey="app_id"), None)

        assert seen["s3_in_place_tag_key"] == "app_id"
        assert "app_id" in capsys.readouterr().out


class TestCredentialAndLoggingBranches:
    def test_a_credential_failure_falls_back_to_the_lambda_role(
        self, wiring, monkeypatch, capsys
    ):
        wiring(**importable())

        def boom(**_kwargs):
            raise RuntimeError("no org access")

        monkeypatch.setattr(ir, "get_cross_account_credentials", boom)

        result = handler(dict(BASE_EVENT), None)

        printed = capsys.readouterr().out
        assert result["totalJobs"] == 0
        assert "Could not obtain cross-account credentials" in printed
        assert "Will attempt operations with Lambda execution role" in printed

    def test_optional_scoping_is_echoed_into_the_log(self, wiring, monkeypatch, capsys):
        wiring(**importable())
        monkeypatch.setattr(ir, "discover_dynamodb_tables_from_stacks", lambda **kwargs: {})
        monkeypatch.setattr(ir, "discover_s3_buckets_from_stacks", lambda **kwargs: {})
        event = dict(
            BASE_EVENT,
            excludeEC2TagKeys=["aws:autoscaling:groupName"],
            recoveryStackNames=["OrdersStack"],
            recoveryStacksOnly=True,
        )

        handler(event, None)

        printed = capsys.readouterr().out
        assert "EC2 tag keys to exclude" in printed
        assert "Recovery stacks to check: ['OrdersStack']" in printed
        assert "Recovery stacks ONLY mode" in printed

    def test_a_table_in_a_region_with_no_vpc_config_is_left_unplanned(self, wiring):
        """With no VPC configs there is nowhere to restore to, so nothing is planned."""
        client = wiring(**importable())
        event = dict(BASE_EVENT, vpcConfigs=[], resourceSnapshots=[ddb_snapshot()])

        result = handler(event, None)

        assert result["totalJobs"] == 1
        assert result["failedJobs"] == 1
        assert client.calls_to("check_dynamodb_import_availability") == []


class TestStateMachineInputContract:
    """
    The state machine references input keys by JSONPath, so a key it names must
    resolve or the execution fails at runtime. `dynamodbRestoreMethod` is
    defaulted by the ASL so an input written before the key existed still runs.
    """

    ASL = pathlib.Path(__file__).resolve().parents[1] / "statemachine.asl.json"
    EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "example-execution-input.json"

    def test_the_asl_defaults_the_restore_method_before_anything_reads_it(self):
        asl = json.loads(self.ASL.read_text())

        assert asl["StartAt"] == "Apply Input Defaults"
        defaults = asl["States"]["Apply Input Defaults"]
        assert defaults["Result"]["dynamodbRestoreMethod"] == "auto"
        # Caller-supplied values must win over the defaults.
        merge = asl["States"]["Normalize Input"]["Parameters"]["merged.$"]
        assert merge == "States.JsonMerge($.inputDefaults, $, false)"

    def test_every_other_referenced_key_is_in_the_example_input(self):
        asl = json.loads(self.ASL.read_text())
        example = json.loads(self.EXAMPLE.read_text())
        defaulted = set(asl["States"]["Apply Input Defaults"]["Result"])

        referenced = set()
        for state in asl["States"].values():
            for value in (state.get("Parameters") or {}).values():
                if isinstance(value, str) and value.startswith("$.") and "." not in value[2:]:
                    referenced.add(value[2:])

        missing = referenced - set(example) - defaulted - {"inputDefaults"}
        assert not missing, f"state machine reads keys absent from the example input: {sorted(missing)}"
