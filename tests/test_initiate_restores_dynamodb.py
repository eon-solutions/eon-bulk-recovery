"""DynamoDB restores: method selection, WCU scaling, and the two restore paths."""

from unittest.mock import MagicMock

import pytest

from handlers import initiate_restores as ir
from handlers.initiate_restores import (
    MIN_ROLE_VERSION_FOR_IMPORT,
    RESTORE_METHOD_CAPACITY_BASED,
    RESTORE_METHOD_IMPORT_TABLE,
    _initiate_dynamodb_restore,
    _restore_dynamodb_in_place,
    _restore_dynamodb_new_table,
    _restore_dynamodb_table_wcu_immediate,
    _scale_up_dynamodb_table_wcu,
    get_restore_role_version,
    parse_role_version,
    plan_dynamodb_restore_methods,
)

TABLE_ARN = "arn:aws:dynamodb:us-east-1:222222222222:table/orders"

SNAPSHOT = {
    "resourceId": "res-ddb",
    "resourceName": "orders",
    "resourceType": "AWS_DYNAMO_DB",
    "snapshotId": "snap-1",
    "snapshotPointInTime": "2026-09-08T02:00:00Z",
    "region": "us-east-1",
    "originalTags": {"env": "prod"},
}


class TestParseRoleVersion:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("1.8.1", (1, 8, 1)),
            ("1.15.0", (1, 15, 0)),
            ("v1.8.1", (1, 8, 1)),
            ("  1.8.1  ", (1, 8, 1)),
            ("1.8.1+build5", (1, 8, 1)),
        ],
    )
    def test_parses_a_release(self, version, expected):
        assert parse_role_version(version) == expected

    @pytest.mark.parametrize("version", [None, "", "1.8", "1.8.1.2", "one.eight.one", "1.8.x"])
    def test_unparseable_versions_are_none(self, version):
        assert parse_role_version(version) is None

    def test_a_prerelease_sorts_below_its_release(self):
        """Eon compares server-side with semver, where 1.8.1-rc.1 < 1.8.1."""
        assert parse_role_version("1.8.1-rc.1") < parse_role_version("1.8.1")
        assert parse_role_version("1.8.1-rc.1") < MIN_ROLE_VERSION_FOR_IMPORT

    @pytest.mark.parametrize(
        "version,expected",
        [("1.9.0-rc1", (1, 8, 999)), ("2.0.0-beta", (1, 999, 999)), ("0.0.0-alpha", (0, 0, 0))],
    )
    def test_prerelease_borrowing_across_boundaries(self, version, expected):
        assert parse_role_version(version) == expected


class TestGetRestoreRoleVersion:
    def test_reads_the_nested_version_the_api_actually_returns(self, fake_eon_client):
        """A RestoreAccount carries version.installed, not a flat installedVersion."""
        client = fake_eon_client(
            list_restore_accounts={
                "accounts": [
                    {
                        "id": "other",
                        "providerAccountId": "999999999999",
                        "version": {"installed": "1.2.0", "latest": "1.15.0"},
                    },
                    {
                        "id": "eon-acct",
                        "providerAccountId": "222222222222",
                        "status": "CONNECTED",
                        "version": {
                            "installed": "1.9.0",
                            "installationMethod": "UNSPECIFIED",
                            "latest": "1.15.0",
                        },
                    },
                ]
            }
        )

        assert get_restore_role_version(client, "eon-acct", "222222222222") == "1.9.0"

    def test_a_flat_installed_version_is_also_accepted(self, fake_eon_client):
        client = fake_eon_client(
            list_restore_accounts={"accounts": [{"id": "eon-acct", "installedVersion": "1.15.0"}]}
        )

        assert get_restore_role_version(client, "eon-acct", "222222222222") == "1.15.0"

    def test_an_account_with_no_version_at_all_is_none(self, fake_eon_client):
        client = fake_eon_client(
            list_restore_accounts={"accounts": [{"id": "eon-acct", "version": {}}]}
        )

        assert get_restore_role_version(client, "eon-acct", "222222222222") is None

    def test_an_unlisted_account_is_none(self, fake_eon_client, capsys):
        client = fake_eon_client(list_restore_accounts={"accounts": []})

        assert get_restore_role_version(client, "eon-acct", "222222222222") is None
        assert "not found in the account listing" in capsys.readouterr().out

    def test_an_api_failure_is_none(self, fake_eon_client, capsys):
        def boom(**_kwargs):
            raise RuntimeError("API down")

        client = fake_eon_client(list_restore_accounts=boom)

        assert get_restore_role_version(client, "eon-acct", "222222222222") is None
        assert "Could not list restore accounts" in capsys.readouterr().out


def candidate(resource_id="res-1", name="orders", region="us-east-1", snapshot="snap-1"):
    return {
        "resourceId": resource_id,
        "resourceName": name,
        "restoredName": name,
        "snapshotId": snapshot,
        "region": region,
        "sizeBytes": 1024,
        "inPlace": False,
    }


def planner_client(fake_eon_client, version="1.15.0", availability=None):
    return fake_eon_client(
        list_restore_accounts={"accounts": [{"id": "eon-acct", "version": {"installed": version}}]},
        check_dynamodb_import_availability=availability or {"available": True, "reasons": []},
    )


class TestPlanDynamoDBRestoreMethods:
    def test_no_candidates_means_no_plan(self, fake_eon_client):
        client = planner_client(fake_eon_client)

        assert plan_dynamodb_restore_methods(client, [], "auto", "eon-acct", "222222222222") == {}
        assert client.calls == []

    def test_capacity_mode_never_probes(self, fake_eon_client):
        client = planner_client(fake_eon_client)

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "capacity", "eon-acct", "222222222222"
        )

        assert plan == {"res-1": {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": []}}
        assert client.calls == []

    def test_import_mode_forces_every_table_without_probing(self, fake_eon_client):
        """Forcing means the API rejects what it cannot take, loudly."""
        client = planner_client(fake_eon_client, version="1.0.0")

        plan = plan_dynamodb_restore_methods(
            client, [candidate(), candidate("res-2", "users")], "import", "eon-acct", "222222222222"
        )

        assert all(entry["method"] == RESTORE_METHOD_IMPORT_TABLE for entry in plan.values())
        assert client.calls == []

    def test_auto_uses_import_when_the_snapshot_allows_it(self, fake_eon_client):
        client = planner_client(fake_eon_client)

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "auto", "eon-acct", "222222222222"
        )

        assert plan["res-1"]["method"] == RESTORE_METHOD_IMPORT_TABLE
        assert plan["res-1"]["reasons"] == []

    def test_auto_probes_in_the_restore_region(self, fake_eon_client):
        client = planner_client(fake_eon_client)

        plan_dynamodb_restore_methods(
            client, [candidate(region="eu-west-1")], "auto", "eon-acct", "222222222222"
        )

        probe = client.calls_to("check_dynamodb_import_availability")[0]
        assert probe["region"] == "eu-west-1"
        assert probe["snapshot_id"] == "snap-1"
        # The destination schema rejects a request without it.
        assert probe["restored_name"] == "orders"

    @pytest.mark.parametrize(
        "code,fragment",
        [
            ("RESTORE_METHOD_HAS_LSI", "local secondary indexes"),
            ("RESTORE_METHOD_SIZE_LIMIT", "larger than the AWS import limit"),
            ("RESTORE_METHOD_NOT_SPECIFIED", "no restore method specified"),
        ],
    )
    def test_auto_falls_back_with_a_readable_reason(self, fake_eon_client, code, fragment):
        client = planner_client(
            fake_eon_client, availability={"available": False, "reasons": [code]}
        )

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "auto", "eon-acct", "222222222222"
        )

        assert plan["res-1"]["method"] == RESTORE_METHOD_CAPACITY_BASED
        assert fragment in plan["res-1"]["reasons"][0]

    def test_an_unrecognised_reason_code_is_passed_through(self, fake_eon_client):
        client = planner_client(
            fake_eon_client, availability={"available": False, "reasons": ["SOMETHING_NEW"]}
        )

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "auto", "eon-acct", "222222222222"
        )

        assert plan["res-1"]["reasons"] == ["SOMETHING_NEW"]

    def test_an_unavailable_answer_with_no_reasons_still_falls_back(self, fake_eon_client, capsys):
        client = planner_client(fake_eon_client, availability={"available": False, "reasons": []})

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "auto", "eon-acct", "222222222222"
        )

        assert plan["res-1"]["method"] == RESTORE_METHOD_CAPACITY_BASED
        assert "import not available" in capsys.readouterr().out

    def test_a_probe_failure_falls_back_rather_than_failing_the_run(self, fake_eon_client):
        def boom(**_kwargs):
            raise RuntimeError("403 not enabled")

        client = fake_eon_client(
            list_restore_accounts={"accounts": [{"id": "eon-acct", "version": {"installed": "1.15.0"}}]},
            check_dynamodb_import_availability=boom,
        )

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "auto", "eon-acct", "222222222222"
        )

        assert plan["res-1"]["method"] == RESTORE_METHOD_CAPACITY_BASED
        assert "403 not enabled" in plan["res-1"]["reasons"][0]

    @pytest.mark.parametrize("version", ["1.8.0", "1.7.9", "1.8.1-rc.1", None, "garbage"])
    def test_an_old_role_short_circuits_every_table(self, fake_eon_client, version):
        client = planner_client(fake_eon_client, version=version)

        plan = plan_dynamodb_restore_methods(
            client, [candidate(), candidate("res-2", "users")], "auto", "eon-acct", "222222222222"
        )

        assert all(entry["method"] == RESTORE_METHOD_CAPACITY_BASED for entry in plan.values())
        assert "below 1.8.1" in plan["res-1"]["reasons"][0]
        assert client.calls_to("check_dynamodb_import_availability") == []

    @pytest.mark.parametrize("version", ["1.8.1", "1.9.0", "1.15.0", "2.0.0"])
    def test_the_minimum_role_version_is_inclusive(self, fake_eon_client, version):
        client = planner_client(fake_eon_client, version=version)

        plan = plan_dynamodb_restore_methods(
            client, [candidate()], "auto", "eon-acct", "222222222222"
        )

        assert plan["res-1"]["method"] == RESTORE_METHOD_IMPORT_TABLE

    def test_a_mixed_plan_is_summarised(self, fake_eon_client, capsys):
        def availability(snapshot_id, region, restored_name):
            if snapshot_id == "snap-2":
                return {"available": False, "reasons": ["RESTORE_METHOD_SIZE_LIMIT"]}
            return {"available": True, "reasons": []}

        client = fake_eon_client(
            list_restore_accounts={"accounts": [{"id": "eon-acct", "version": {"installed": "1.15.0"}}]},
            check_dynamodb_import_availability=availability,
        )

        plan = plan_dynamodb_restore_methods(
            client,
            [candidate(), candidate("res-2", "big", snapshot="snap-2")],
            "auto",
            "eon-acct",
            "222222222222",
        )

        assert plan["res-1"]["method"] == RESTORE_METHOD_IMPORT_TABLE
        assert plan["res-2"]["method"] == RESTORE_METHOD_CAPACITY_BASED
        assert "1 via ImportTable, 1 capacity-based" in capsys.readouterr().out


def ddb_table_description(billing="PROVISIONED", wcu=5, rcu=5, status="ACTIVE", gsis=None):
    table = {
        "TableArn": TABLE_ARN,
        "TableStatus": status,
        "BillingModeSummary": {"BillingMode": billing},
        "ProvisionedThroughput": {"WriteCapacityUnits": wcu, "ReadCapacityUnits": rcu},
    }
    if gsis:
        table["GlobalSecondaryIndexes"] = gsis
    return {"Table": table}


@pytest.fixture
def ddb(monkeypatch):
    """A single DynamoDB client for the scale-up helpers."""
    client = MagicMock()
    client.list_tags_of_resource.return_value = {"Tags": []}
    monkeypatch.setattr(ir, "create_boto3_client", lambda *args, **kwargs: client)
    return client


CREDS = {"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}


class TestScaleUpWcu:
    def test_no_credentials_means_no_scale_up(self, capsys):
        assert _scale_up_dynamodb_table_wcu("orders", "us-east-1", 1000, None) == {
            "wcuScaledUp": False
        }
        assert "No cross-account credentials" in capsys.readouterr().out

    def test_scales_a_provisioned_table_and_tags_the_original(self, ddb):
        ddb.describe_table.return_value = ddb_table_description(wcu=5, rcu=10)

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result == {
            "wcuScaledUp": True,
            "originalBillingMode": "PROVISIONED",
            "originalWcu": 5,
            "originalRcu": 10,
            "originalGsiThroughput": {},
        }
        update = ddb.update_table.call_args.kwargs
        assert update["ProvisionedThroughput"] == {
            "ReadCapacityUnits": 10,
            "WriteCapacityUnits": 28500,
        }
        tags = {t["Key"]: t["Value"] for t in ddb.tag_resource.call_args.kwargs["Tags"]}
        assert tags["eon:original_billing_mode"] == "PROVISIONED"
        assert tags["eon:original_wcu"] == "5"

    def test_switches_an_on_demand_table_to_provisioned(self, ddb):
        ddb.describe_table.return_value = ddb_table_description(billing="PAY_PER_REQUEST", wcu=0, rcu=0)

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result["originalBillingMode"] == "PAY_PER_REQUEST"
        update = ddb.update_table.call_args.kwargs
        assert update["BillingMode"] == "PROVISIONED"
        assert update["ProvisionedThroughput"]["WriteCapacityUnits"] == 28500
        assert update["ProvisionedThroughput"]["ReadCapacityUnits"] == 5

    def test_gsis_are_scaled_with_the_base_table(self, ddb):
        gsis = [
            {"IndexName": "by-customer", "ProvisionedThroughput": {"ReadCapacityUnits": 3, "WriteCapacityUnits": 4}}
        ]
        ddb.describe_table.return_value = ddb_table_description(gsis=gsis)

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result["originalGsiThroughput"] == {"by-customer": {"rcu": 3, "wcu": 4}}
        updates = ddb.update_table.call_args.kwargs["GlobalSecondaryIndexUpdates"]
        assert updates[0]["Update"]["IndexName"] == "by-customer"
        assert updates[0]["Update"]["ProvisionedThroughput"]["WriteCapacityUnits"] == 28500
        # RCU floors at 5 so the index is not left unreadable.
        assert updates[0]["Update"]["ProvisionedThroughput"]["ReadCapacityUnits"] == 5

    def test_on_demand_gsis_are_included_in_the_billing_mode_switch(self, ddb):
        gsis = [{"IndexName": "by-customer", "ProvisionedThroughput": {}}]
        ddb.describe_table.return_value = ddb_table_description(
            billing="PAY_PER_REQUEST", gsis=gsis
        )

        _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        updates = ddb.update_table.call_args.kwargs["GlobalSecondaryIndexUpdates"]
        assert updates[0]["Update"]["IndexName"] == "by-customer"

    def test_a_table_that_is_not_active_is_left_alone(self, ddb, capsys):
        ddb.describe_table.return_value = ddb_table_description(status="UPDATING")

        assert _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS) == {
            "wcuScaledUp": False
        }
        ddb.update_table.assert_not_called()
        assert "is in UPDATING state" in capsys.readouterr().out

    def test_a_table_already_above_the_target_is_left_alone(self, ddb, capsys):
        ddb.describe_table.return_value = ddb_table_description(wcu=40000)

        assert _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS) == {
            "wcuScaledUp": False
        }
        ddb.update_table.assert_not_called()
        assert "skipping scale-up" in capsys.readouterr().out

    def test_existing_tags_win_on_a_retry(self, ddb, capsys):
        """A second scale-up must not record the already-elevated WCU as 'original'."""
        ddb.describe_table.return_value = ddb_table_description(wcu=20000, rcu=5)
        ddb.list_tags_of_resource.return_value = {
            "Tags": [
                {"Key": "eon:original_billing_mode", "Value": "PAY_PER_REQUEST"},
                {"Key": "eon:original_wcu", "Value": "5"},
                {"Key": "eon:original_rcu", "Value": "5"},
                {"Key": "eon:original_gsi:by-customer", "Value": "3/4"},
            ]
        }

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result["originalBillingMode"] == "PAY_PER_REQUEST"
        assert result["originalWcu"] == 5
        assert result["originalGsiThroughput"] == {"by-customer": {"rcu": 3, "wcu": 4}}
        ddb.tag_resource.assert_not_called()
        assert "likely a retry" in capsys.readouterr().out

    def test_tag_pages_are_all_read(self, ddb):
        ddb.describe_table.return_value = ddb_table_description()
        ddb.list_tags_of_resource.side_effect = [
            {"Tags": [{"Key": "unrelated", "Value": "x"}], "NextToken": "more"},
            {"Tags": [{"Key": "eon:original_billing_mode", "Value": "PROVISIONED"},
                      {"Key": "eon:original_wcu", "Value": "7"}]},
        ]

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result["originalWcu"] == 7

    def test_a_malformed_gsi_tag_is_ignored(self, ddb):
        ddb.describe_table.return_value = ddb_table_description()
        ddb.list_tags_of_resource.return_value = {
            "Tags": [
                {"Key": "eon:original_billing_mode", "Value": "PROVISIONED"},
                {"Key": "eon:original_gsi:broken", "Value": "not-a-pair"},
            ]
        }

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result["originalGsiThroughput"] == {}

    def test_a_tag_read_failure_is_not_fatal(self, ddb, capsys):
        ddb.describe_table.return_value = ddb_table_description()
        ddb.list_tags_of_resource.side_effect = RuntimeError("denied")

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result["wcuScaledUp"] is True
        assert "Could not read tags" in capsys.readouterr().out

    def test_an_over_long_gsi_tag_key_is_skipped(self, ddb, capsys):
        gsis = [
            {"IndexName": "g" * 200, "ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}}
        ]
        ddb.describe_table.return_value = ddb_table_description(gsis=gsis)

        _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        keys = [t["Key"] for t in ddb.tag_resource.call_args.kwargs["Tags"]]
        assert all(len(key) <= 128 for key in keys)
        assert "exceeds 128-char limit" in capsys.readouterr().out

    def test_a_failure_reports_but_does_not_raise(self, ddb, capsys):
        ddb.describe_table.side_effect = RuntimeError("table gone")

        result = _scale_up_dynamodb_table_wcu("orders", "us-east-1", 28500, CREDS)

        assert result == {"wcuScaledUp": False, "error": "table gone"}
        assert "Failed to scale up WCU" in capsys.readouterr().out


class TestImmediateWcuRollback:
    ORIGINAL = {
        "wcuScaledUp": True,
        "originalBillingMode": "PROVISIONED",
        "originalWcu": 5,
        "originalRcu": 10,
        "originalGsiThroughput": {"by-customer": {"rcu": 3, "wcu": 4}},
    }

    def test_restores_throughput_and_clears_the_tags(self, ddb):
        ddb.describe_table.return_value = ddb_table_description()

        _restore_dynamodb_table_wcu_immediate("orders", "us-east-1", self.ORIGINAL, CREDS)

        update = ddb.update_table.call_args.kwargs
        assert update["ProvisionedThroughput"] == {"ReadCapacityUnits": 10, "WriteCapacityUnits": 5}
        assert update["GlobalSecondaryIndexUpdates"][0]["Update"]["ProvisionedThroughput"] == {
            "ReadCapacityUnits": 3,
            "WriteCapacityUnits": 4,
        }
        tag_keys = ddb.untag_resource.call_args.kwargs["TagKeys"]
        assert "eon:original_billing_mode" in tag_keys
        assert "eon:original_gsi:by-customer" in tag_keys

    def test_restores_on_demand_billing(self, ddb):
        ddb.describe_table.return_value = ddb_table_description()
        settings = dict(self.ORIGINAL, originalBillingMode="PAY_PER_REQUEST")

        _restore_dynamodb_table_wcu_immediate("orders", "us-east-1", settings, CREDS)

        update = ddb.update_table.call_args.kwargs
        assert update["BillingMode"] == "PAY_PER_REQUEST"
        assert "ProvisionedThroughput" not in update

    def test_does_nothing_when_there_was_no_scale_up(self, ddb):
        _restore_dynamodb_table_wcu_immediate("orders", "us-east-1", {"wcuScaledUp": False}, CREDS)

        ddb.update_table.assert_not_called()

    def test_does_nothing_without_credentials(self, ddb):
        _restore_dynamodb_table_wcu_immediate("orders", "us-east-1", self.ORIGINAL, None)

        ddb.update_table.assert_not_called()

    def test_a_rollback_failure_warns_about_manual_cleanup(self, ddb, capsys):
        ddb.update_table.side_effect = RuntimeError("throttled")

        _restore_dynamodb_table_wcu_immediate("orders", "us-east-1", self.ORIGINAL, CREDS)

        printed = capsys.readouterr().out
        assert "Failed to rollback WCU" in printed
        assert "manual intervention may be needed" in printed


class TestRestoreNewTable:
    def test_import_omits_write_capacity_and_warm_throughput(
        self, restore_context, fake_eon_client
    ):
        client = fake_eon_client(restore_dynamodb_table="job-1")
        ctx = restore_context(
            eon_client=client,
            dynamodb_wcu_allocation={"res-ddb": 28500},
            dynamodb_restore_methods={
                "res-ddb": {"method": RESTORE_METHOD_IMPORT_TABLE, "reasons": []}
            },
        )

        job_id, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert job_id == "job-1"
        assert details["restoreMethod"] == RESTORE_METHOD_IMPORT_TABLE
        assert "writeCapacityUnits" not in details
        assert "warmThroughputTarget" not in details
        call = client.calls_to("restore_dynamodb_table")[0]
        assert call["restore_method"] == RESTORE_METHOD_IMPORT_TABLE
        assert "writeCapacityUnits" not in call["destination_config"]["awsDynamodb"]

    def test_capacity_based_sends_the_allocation_and_warms_the_table(
        self, restore_context, fake_eon_client
    ):
        client = fake_eon_client(restore_dynamodb_table="job-1")
        ctx = restore_context(
            eon_client=client,
            dynamodb_wcu_allocation={"res-ddb": 28500},
            dynamodb_restore_methods={
                "res-ddb": {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": []}
            },
        )

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert details["writeCapacityUnits"] == 28500
        assert details["warmThroughputTarget"] == 28500
        assert details["warmThroughputApplied"] is False
        call = client.calls_to("restore_dynamodb_table")[0]
        assert call["destination_config"]["awsDynamodb"]["writeCapacityUnits"] == 28500
        assert call["restore_method"] == RESTORE_METHOD_CAPACITY_BASED

    def test_warm_throughput_can_be_turned_off(self, restore_context, fake_eon_client):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_table="job-1"),
            dynamodb_wcu_allocation={"res-ddb": 100},
            dynamodb_restore_methods={
                "res-ddb": {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": []}
            },
            dynamodb_warm_throughput=False,
        )

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert "warmThroughputTarget" not in details

    def test_an_unplanned_table_defaults_to_capacity_based(self, restore_context, fake_eon_client):
        client = fake_eon_client(restore_dynamodb_table="job-1")
        ctx = restore_context(eon_client=client, dynamodb_restore_methods={})

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert details["restoreMethod"] == RESTORE_METHOD_CAPACITY_BASED

    def test_a_zero_allocation_omits_write_capacity_entirely(
        self, restore_context, fake_eon_client
    ):
        """A table with no data to write gets Eon's own minimum, not a share."""
        client = fake_eon_client(restore_dynamodb_table="job-1")
        ctx = restore_context(
            eon_client=client,
            dynamodb_wcu_allocation={"res-ddb": 0},
            dynamodb_restore_methods={
                "res-ddb": {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": []}
            },
        )

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert "writeCapacityUnits" not in details
        assert "warmThroughputTarget" not in details
        destination = client.calls_to("restore_dynamodb_table")[0]["destination_config"]
        assert "writeCapacityUnits" not in destination["awsDynamodb"]

    def test_fallback_reasons_are_recorded_on_the_job(self, restore_context, fake_eon_client):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_table="job-1"),
            dynamodb_restore_methods={
                "res-ddb": {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": ["has LSIs"]}
            },
        )

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert details["restoreMethodReasons"] == ["has LSIs"]

    def test_no_reasons_key_when_the_method_was_available(self, restore_context, fake_eon_client):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_table="job-1"),
            dynamodb_restore_methods={
                "res-ddb": {"method": RESTORE_METHOD_IMPORT_TABLE, "reasons": []}
            },
        )

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert "restoreMethodReasons" not in details

    def test_tags_merge_the_originals_with_the_restore_markers(
        self, restore_context, fake_eon_client
    ):
        client = fake_eon_client(restore_dynamodb_table="job-1")
        ctx = restore_context(eon_client=client)

        _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        tags = client.calls_to("restore_dynamodb_table")[0]["destination_config"]["awsDynamodb"]["tags"]
        assert tags["env"] == "prod"
        assert tags["ManagedBy"] == "EonBulkRecovery"
        assert tags["eon:snapshot_id"] == "snap-1"
        assert tags["eon:snapshot_time"] == "2026-09-08T02:00:00Z"

    def test_the_name_prefix_is_applied(self, restore_context, fake_eon_client):
        client = fake_eon_client(restore_dynamodb_table="job-1")
        ctx = restore_context(eon_client=client, resource_name_prefix="dr-")

        _, details = _restore_dynamodb_new_table(ctx, SNAPSHOT, "us-east-1", "arn:key")

        assert details["restoredName"] == "dr-orders"


class TestRestoreInPlace:
    STACK_MATCH = {"tableName": "orders", "region": "us-east-1", "stackName": "OrdersStack"}

    def test_scales_up_then_restores(self, restore_context, fake_eon_client, monkeypatch):
        client = fake_eon_client(restore_dynamodb_to_existing_table="job-2")
        ctx = restore_context(eon_client=client, dynamodb_wcu_allocation={"res-ddb": 28500})
        monkeypatch.setattr(
            ir,
            "_scale_up_dynamodb_table_wcu",
            lambda **kwargs: {"wcuScaledUp": True, "originalWcu": 5},
        )

        job_id, details = _restore_dynamodb_in_place(ctx, SNAPSHOT, self.STACK_MATCH)

        assert job_id == "job-2"
        assert details["restoreType"] == "IN_PLACE"
        # The existing-table API takes no method, so it is always capacity-based.
        assert details["restoreMethod"] == RESTORE_METHOD_CAPACITY_BASED
        assert details["writeCapacityUnits"] == 28500
        assert details["recoveryStackName"] == "OrdersStack"
        assert details["originalTableSettings"] == {"wcuScaledUp": True, "originalWcu": 5}

    def test_warm_throughput_is_deferred_to_the_monitor(
        self, restore_context, fake_eon_client, monkeypatch
    ):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_to_existing_table="job-2"),
            dynamodb_wcu_allocation={"res-ddb": 28500},
        )
        monkeypatch.setattr(ir, "_scale_up_dynamodb_table_wcu", lambda **kwargs: {"wcuScaledUp": True})

        _, details = _restore_dynamodb_in_place(ctx, SNAPSHOT, self.STACK_MATCH)

        assert details["warmThroughputTarget"] == 28500
        assert details["warmThroughputApplied"] is False

    def test_a_failed_api_call_rolls_the_wcu_back(
        self, restore_context, fake_eon_client, monkeypatch
    ):
        def boom(**_kwargs):
            raise RuntimeError("API rejected")

        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_to_existing_table=boom),
            dynamodb_wcu_allocation={"res-ddb": 28500},
        )
        monkeypatch.setattr(
            ir, "_scale_up_dynamodb_table_wcu", lambda **kwargs: {"wcuScaledUp": True}
        )
        rolled_back = {}
        monkeypatch.setattr(
            ir,
            "_restore_dynamodb_table_wcu_immediate",
            lambda **kwargs: rolled_back.update(kwargs),
        )

        with pytest.raises(RuntimeError, match="API rejected"):
            _restore_dynamodb_in_place(ctx, SNAPSHOT, self.STACK_MATCH)

        assert rolled_back["table_name"] == "orders"
        assert rolled_back["original_settings"] == {"wcuScaledUp": True}

    def test_a_table_with_no_data_is_not_scaled_up(self, ddb, restore_context, fake_eon_client):
        ddb.describe_table.return_value = ddb_table_description(billing="PAY_PER_REQUEST")
        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_to_existing_table="job-2"),
            dynamodb_wcu_allocation={"res-ddb": 0},
        )

        _, details = _restore_dynamodb_in_place(ctx, SNAPSHOT, self.STACK_MATCH)

        ddb.update_table.assert_not_called()
        assert details["originalTableSettings"] == {"wcuScaledUp": False}
        assert "warmThroughputTarget" not in details

    def test_a_missing_kms_key_for_the_stack_region_is_an_error(
        self, restore_context, fake_eon_client
    ):
        ctx = restore_context(eon_client=fake_eon_client(), kms_key_arns_by_region={})

        with pytest.raises(ValueError, match="No KMS key available for region us-east-1"):
            _restore_dynamodb_in_place(ctx, SNAPSHOT, self.STACK_MATCH)


class TestDispatch:
    def test_a_stack_match_routes_to_the_in_place_path(
        self, restore_context, fake_eon_client, monkeypatch
    ):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_dynamodb_to_existing_table="job-2"),
            recovery_stack_tables={
                "orders": {"tableName": "orders", "region": "us-east-1", "stackName": "S"}
            },
        )
        monkeypatch.setattr(ir, "_scale_up_dynamodb_table_wcu", lambda **kwargs: {"wcuScaledUp": False})

        _, details = _initiate_dynamodb_restore(ctx, SNAPSHOT, "us-east-1")

        assert details["restoreType"] == "IN_PLACE"

    def test_no_stack_match_creates_a_new_table(self, restore_context, fake_eon_client):
        ctx = restore_context(eon_client=fake_eon_client(restore_dynamodb_table="job-1"))

        _, details = _initiate_dynamodb_restore(ctx, SNAPSHOT, "us-east-1")

        assert details["restoreType"] == "NEW_TABLE"

    def test_stacks_only_mode_skips_an_unmatched_table(
        self, restore_context, fake_eon_client, capsys
    ):
        ctx = restore_context(eon_client=fake_eon_client(), recovery_stacks_only=True)

        assert _initiate_dynamodb_restore(ctx, SNAPSHOT, "us-east-1") is None
        assert "recoveryStacksOnly mode, no matching stack table" in capsys.readouterr().out

    def test_a_missing_kms_key_fails_before_anything_is_submitted(
        self, restore_context, fake_eon_client
    ):
        ctx = restore_context(eon_client=fake_eon_client(), kms_key_arns_by_region={})

        with pytest.raises(ValueError, match="No KMS key available"):
            _initiate_dynamodb_restore(ctx, SNAPSHOT, "us-east-1")
