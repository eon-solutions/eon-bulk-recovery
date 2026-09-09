"""Job monitoring: status categorisation, DynamoDB cleanup, and the report."""

from unittest.mock import MagicMock

import pytest

from handlers import monitor_jobs as mj
from handlers.monitor_jobs import (
    _apply_deferred_warm_throughput,
    _restore_dynamodb_table_wcu,
    handler,
    send_completion_notification,
)

CREDS = {"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}
TABLE_ARN = "arn:aws:dynamodb:us-east-1:222222222222:table/orders"


class ResourceNotFoundException(Exception):
    pass


@pytest.fixture
def ddb(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(mj, "create_boto3_client", lambda *args, **kwargs: client)
    return client


@pytest.fixture
def sns(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(mj.boto3, "client", lambda *args, **kwargs: client)
    return client


def table_description(status="ACTIVE", warm=None, gsis=None):
    table = {"TableArn": TABLE_ARN, "TableStatus": status}
    if warm is not None:
        table["WarmThroughput"] = warm
    if gsis is not None:
        table["GlobalSecondaryIndexes"] = gsis
    return {"Table": table}


class TestWarmThroughput:
    def test_nothing_to_do_without_a_target(self, ddb):
        assert _apply_deferred_warm_throughput({}, CREDS) is True
        ddb.describe_table.assert_not_called()

    def test_nothing_to_do_once_applied(self, ddb):
        job = {"warmThroughputTarget": 1000, "warmThroughputApplied": True}

        assert _apply_deferred_warm_throughput(job, CREDS) is True
        ddb.describe_table.assert_not_called()

    @pytest.mark.parametrize(
        "job",
        [
            {"warmThroughputTarget": 1000, "restoredRegion": "us-east-1"},
            {"warmThroughputTarget": 1000, "restoredName": "orders"},
        ],
    )
    def test_missing_table_details_is_a_failure(self, ddb, job):
        assert _apply_deferred_warm_throughput(job, CREDS) is False

    def test_missing_credentials_is_a_failure(self, ddb):
        job = {"warmThroughputTarget": 1000, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, None) is False

    def test_sets_warm_throughput_on_an_active_table(self, ddb):
        ddb.describe_table.return_value = table_description()
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is True

        update = ddb.update_table.call_args.kwargs
        assert update["WarmThroughput"] == {"ReadUnitsPerSecond": 1, "WriteUnitsPerSecond": 28500}

    def test_a_table_that_does_not_exist_yet_retries_next_iteration(self, ddb, capsys):
        ddb.describe_table.side_effect = ResourceNotFoundException("no such table")
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is False
        assert "does not exist yet" in capsys.readouterr().out

    def test_a_table_still_creating_retries_next_iteration(self, ddb, capsys):
        ddb.describe_table.return_value = table_description(status="CREATING")
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is False
        assert "is CREATING" in capsys.readouterr().out

    def test_updating_gsis_block_the_update(self, ddb, capsys):
        ddb.describe_table.return_value = table_description(
            gsis=[{"IndexName": "by-customer", "IndexStatus": "UPDATING"}]
        )
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is False
        assert "GSIs still updating: by-customer" in capsys.readouterr().out

    def test_warm_throughput_is_never_lowered(self, ddb, capsys):
        """DynamoDB rejects a decrease, so an already-higher value is left alone."""
        ddb.describe_table.return_value = table_description(
            warm={"WriteUnitsPerSecond": 40000, "ReadUnitsPerSecond": 10}
        )
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is True
        ddb.update_table.assert_not_called()
        assert "already at 40,000 WCU" in capsys.readouterr().out

    def test_gsis_are_warmed_alongside_the_base_table(self, ddb):
        ddb.describe_table.return_value = table_description(
            gsis=[{"IndexName": "by-customer", "IndexStatus": "ACTIVE"}]
        )
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        _apply_deferred_warm_throughput(job, CREDS)

        gsi_update = ddb.update_table.call_args.kwargs["GlobalSecondaryIndexUpdates"][0]["Update"]
        assert gsi_update["IndexName"] == "by-customer"
        assert gsi_update["WarmThroughput"]["WriteUnitsPerSecond"] == 28500

    def test_a_gsi_needing_a_bump_forces_the_update_even_if_the_table_is_warm(self, ddb):
        ddb.describe_table.return_value = table_description(
            warm={"WriteUnitsPerSecond": 40000, "ReadUnitsPerSecond": 10},
            gsis=[
                {
                    "IndexName": "by-customer",
                    "IndexStatus": "ACTIVE",
                    "WarmThroughput": {"WriteUnitsPerSecond": 10, "ReadUnitsPerSecond": 1},
                }
            ],
        )
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is True
        ddb.update_table.assert_called_once()

    def test_a_failure_is_reported_and_retried(self, ddb, capsys):
        ddb.describe_table.return_value = table_description()
        ddb.update_table.side_effect = RuntimeError("throttled")
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is False
        assert "Failed to set warm throughput" in capsys.readouterr().out

    def test_a_non_not_found_describe_error_is_reported(self, ddb, capsys):
        ddb.describe_table.side_effect = RuntimeError("AccessDenied")
        job = {"warmThroughputTarget": 28500, "restoredName": "orders", "restoredRegion": "us-east-1"}

        assert _apply_deferred_warm_throughput(job, CREDS) is False
        assert "Failed to set warm throughput" in capsys.readouterr().out


class TestRestoreWcu:
    SCALED = {
        "restoredName": "orders",
        "restoredRegion": "us-east-1",
        "originalTableSettings": {
            "wcuScaledUp": True,
            "originalBillingMode": "PROVISIONED",
            "originalWcu": 5,
            "originalRcu": 10,
            "originalGsiThroughput": {"by-customer": {"rcu": 3, "wcu": 4}},
        },
    }

    def test_nothing_to_do_when_nothing_was_scaled(self, ddb):
        assert _restore_dynamodb_table_wcu({"originalTableSettings": {"wcuScaledUp": False}}) is True
        ddb.describe_table.assert_not_called()

    def test_restores_provisioned_throughput_and_clears_tags(self, ddb):
        ddb.describe_table.return_value = table_description()

        assert _restore_dynamodb_table_wcu(self.SCALED, CREDS) is True

        update = ddb.update_table.call_args.kwargs
        assert update["ProvisionedThroughput"] == {"ReadCapacityUnits": 10, "WriteCapacityUnits": 5}
        assert update["GlobalSecondaryIndexUpdates"][0]["Update"]["ProvisionedThroughput"] == {
            "ReadCapacityUnits": 3,
            "WriteCapacityUnits": 4,
        }
        assert "eon:original_gsi:by-customer" in ddb.untag_resource.call_args.kwargs["TagKeys"]

    def test_restores_on_demand_billing(self, ddb, capsys):
        ddb.describe_table.return_value = table_description()
        job = dict(self.SCALED)
        job["originalTableSettings"] = dict(
            self.SCALED["originalTableSettings"], originalBillingMode="PAY_PER_REQUEST"
        )

        assert _restore_dynamodb_table_wcu(job, CREDS) is True

        assert ddb.update_table.call_args.kwargs["BillingMode"] == "PAY_PER_REQUEST"
        assert "PAY_PER_REQUEST billing mode" in capsys.readouterr().out

    def test_a_deleted_table_needs_no_restoration(self, ddb, capsys):
        ddb.describe_table.side_effect = ResourceNotFoundException("gone")

        assert _restore_dynamodb_table_wcu(self.SCALED, CREDS) is True
        assert "no longer exists" in capsys.readouterr().out

    def test_missing_credentials_is_a_failure(self, ddb, capsys):
        assert _restore_dynamodb_table_wcu(self.SCALED, None) is False
        assert "No cross-account credentials" in capsys.readouterr().out

    def test_missing_table_details_is_a_failure(self, ddb, capsys):
        job = {"originalTableSettings": {"wcuScaledUp": True}}

        assert _restore_dynamodb_table_wcu(job, CREDS) is False
        assert "Missing table name or region" in capsys.readouterr().out

    def test_a_tag_cleanup_failure_does_not_fail_the_restoration(self, ddb, capsys):
        ddb.describe_table.return_value = table_description()
        ddb.untag_resource.side_effect = RuntimeError("denied")

        assert _restore_dynamodb_table_wcu(self.SCALED, CREDS) is True
        assert "Could not remove eon:original_* tags" in capsys.readouterr().out

    def test_an_update_failure_asks_for_manual_intervention(self, ddb, capsys):
        ddb.describe_table.return_value = table_description()
        ddb.update_table.side_effect = RuntimeError("throttled")

        assert _restore_dynamodb_table_wcu(self.SCALED, CREDS) is False
        assert "manual intervention required" in capsys.readouterr().out


def job(job_id="job-1", **overrides):
    base = {
        "jobId": job_id,
        "resourceId": "res-1",
        "resourceName": "orders",
        "resourceType": "AWS_DYNAMO_DB",
        "snapshotPointInTime": "2026-09-08T02:00:00Z",
        "restoredName": "orders",
        "restoredRegion": "us-east-1",
    }
    base.update(overrides)
    return base


def status_response(status="JOB_COMPLETED", **details):
    execution = {"status": status, "startTime": "2026-09-08T02:00:00Z"}
    execution.update(details)
    return {"job": {"jobExecutionDetails": execution}}


@pytest.fixture
def monitored(monkeypatch, eon_credentials, fake_eon_client, sns):
    """Wire the monitor handler to a fake Eon client and no real AWS."""
    monkeypatch.setattr(mj, "create_boto3_client", lambda *args, **kwargs: MagicMock())

    def _wire(**responses):
        client = fake_eon_client(**responses)
        monkeypatch.setattr(mj, "EonClient", lambda **kwargs: client)
        return client

    return _wire


class TestHandlerStatuses:
    @pytest.mark.parametrize(
        "status,bucket",
        [
            ("JOB_COMPLETED", "completedJobs"),
            ("JOB_FAILED", "failedJobs"),
            ("JOB_CANCELED", "failedJobs"),
            ("JOB_REJECTED", "failedJobs"),
            ("JOB_SKIPPED", "skippedJobs"),
            ("JOB_RUNNING", "runningJobs"),
            ("JOB_PENDING", "runningJobs"),
        ],
    )
    def test_each_status_lands_in_the_right_bucket(self, monitored, status, bucket):
        monitored(get_restore_job=status_response(status))

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result[bucket] == 1

    def test_a_partial_job_counts_as_both_partial_and_complete(self, monitored):
        monitored(get_restore_job=status_response("JOB_PARTIAL"))

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["partialJobs"] == 1
        assert result["completedJobs"] == 1
        assert result["allComplete"] is True

    def test_an_unknown_status_keeps_polling(self, monitored, capsys):
        monitored(get_restore_job=status_response("JOB_TELEPORTING"))

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["runningJobs"] == 1
        assert result["allComplete"] is False
        assert "Unrecognized job status" in capsys.readouterr().out

    def test_a_status_lookup_error_keeps_polling(self, monitored):
        def boom(**_kwargs):
            raise RuntimeError("API down")

        monitored(get_restore_job=boom)

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["runningJobs"] == 1
        assert result["jobStatuses"][0]["currentStatus"] == "ERROR_CHECKING_STATUS"

    def test_a_job_that_never_initiated_counts_as_failed(self, monitored):
        monitored()

        result = handler({"restoreJobs": [job(job_id=None)], "iteration": 0}, None)

        assert result["failedJobs"] == 1
        assert result["jobStatuses"][0]["currentStatus"] == "FAILED_TO_INITIATE"

    def test_the_iteration_counter_advances(self, monitored):
        monitored(get_restore_job=status_response("JOB_RUNNING"))

        assert handler({"restoreJobs": [job()], "iteration": 4}, None)["iteration"] == 5

    def test_the_ceiling_sets_timed_out(self, monitored, monkeypatch):
        monkeypatch.setenv("MAX_MONITORING_ITERATIONS", "5")
        monitored(get_restore_job=status_response("JOB_RUNNING"))

        result = handler({"restoreJobs": [job()], "iteration": 5}, None)

        assert result["timedOut"] is True

    def test_below_the_ceiling_is_not_timed_out(self, monitored, monkeypatch):
        monkeypatch.setenv("MAX_MONITORING_ITERATIONS", "5")
        monitored(get_restore_job=status_response("JOB_RUNNING"))

        assert handler({"restoreJobs": [job()], "iteration": 4}, None)["timedOut"] is False


class TestHandlerDynamoDBCleanup:
    def test_warm_throughput_is_applied_while_the_job_runs(self, monitored, monkeypatch):
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        applied = []
        monkeypatch.setattr(
            mj, "_apply_deferred_warm_throughput", lambda j, c: applied.append(j) or True
        )
        jobs = [job(warmThroughputTarget=28500, warmThroughputApplied=False)]

        result = handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert len(applied) == 1
        assert result["restoreJobs"][0]["warmThroughputApplied"] is True

    def test_warm_throughput_is_not_applied_once_the_job_is_terminal(
        self, monitored, monkeypatch
    ):
        monitored(get_restore_job=status_response("JOB_COMPLETED"))
        applied = []
        monkeypatch.setattr(
            mj, "_apply_deferred_warm_throughput", lambda j, c: applied.append(j) or True
        )
        jobs = [job(warmThroughputTarget=28500, warmThroughputApplied=False)]

        handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert applied == []

    def test_a_failed_warm_throughput_attempt_is_retried_next_iteration(
        self, monitored, monkeypatch
    ):
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        monkeypatch.setattr(mj, "_apply_deferred_warm_throughput", lambda j, c: False)
        jobs = [job(warmThroughputTarget=28500, warmThroughputApplied=False)]

        result = handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert result["restoreJobs"][0]["warmThroughputApplied"] is False

    def test_wcu_is_restored_when_an_in_place_job_reaches_a_terminal_state(
        self, monitored, monkeypatch
    ):
        monitored(get_restore_job=status_response("JOB_COMPLETED"))
        restored = []
        monkeypatch.setattr(
            mj, "_restore_dynamodb_table_wcu", lambda j, c: restored.append(j) or True
        )
        jobs = [job(originalTableSettings={"wcuScaledUp": True})]

        result = handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert len(restored) == 1
        assert result["restoreJobs"][0]["wcuRestored"] is True
        assert result["wcuRestorationFailures"] == []

    def test_a_failed_wcu_restoration_is_reported(self, monitored, monkeypatch):
        monitored(get_restore_job=status_response("JOB_FAILED"))
        monkeypatch.setattr(mj, "_restore_dynamodb_table_wcu", lambda j, c: False)
        jobs = [job(originalTableSettings={"wcuScaledUp": True})]

        result = handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert len(result["wcuRestorationFailures"]) == 1

    def test_wcu_is_rolled_back_for_a_job_that_never_initiated(self, monitored, monkeypatch):
        monitored()
        restored = []
        monkeypatch.setattr(
            mj, "_restore_dynamodb_table_wcu", lambda j, c: restored.append(j) or True
        )
        jobs = [job(job_id=None, originalTableSettings={"wcuScaledUp": True})]

        handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert len(restored) == 1

    def test_a_timeout_rolls_back_still_elevated_tables(self, monitored, monkeypatch, capsys):
        monkeypatch.setenv("MAX_MONITORING_ITERATIONS", "1")
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        restored = []
        monkeypatch.setattr(
            mj, "_restore_dynamodb_table_wcu", lambda j, c: restored.append(j) or True
        )
        jobs = [job(originalTableSettings={"wcuScaledUp": True})]

        handler({"restoreJobs": jobs, "iteration": 1}, None)

        assert len(restored) == 1
        assert "Timeout: restoring WCU" in capsys.readouterr().out

    def test_cross_account_credentials_are_fetched_when_a_role_is_given(
        self, monitored, monkeypatch
    ):
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        seen = {}
        monkeypatch.setattr(
            mj, "get_cross_account_credentials", lambda **kwargs: seen.update(kwargs) or CREDS
        )

        handler(
            {
                "restoreJobs": [job()],
                "iteration": 0,
                "restoreAccountId": "222222222222",
                "crossAccountRoleArn": "arn:custom",
            },
            None,
        )

        assert seen["cross_account_role_arn"] == "arn:custom"

    def test_a_credential_failure_is_non_fatal(self, monitored, monkeypatch, capsys):
        monitored(get_restore_job=status_response("JOB_RUNNING"))

        def boom(**_kwargs):
            raise RuntimeError("no access")

        monkeypatch.setattr(mj, "get_cross_account_credentials", boom)

        result = handler(
            {
                "restoreJobs": [job()],
                "iteration": 0,
                "restoreAccountId": "222222222222",
                "crossAccountRoleArn": "arn:custom",
            },
            None,
        )

        assert result["runningJobs"] == 1
        assert "Could not obtain cross-account credentials" in capsys.readouterr().out


def summary(**overrides):
    base = {
        "restoreJobs": [job()],
        "jobStatuses": [
            {
                "jobId": "job-1",
                "resourceId": "res-1",
                "resourceName": "orders",
                "resourceType": "AWS_DYNAMO_DB",
                "currentStatus": "JOB_COMPLETED",
            }
        ],
        "totalJobs": 1,
        "completedJobs": 1,
        "failedJobs": 0,
        "partialJobs": 0,
        "skippedJobs": 0,
        "runningJobs": 0,
        "iteration": 1,
        "sourceAccountId": "333333333333",
        "restoreAccountId": "222222222222",
        "restoreRegion": "us-east-1",
        "vpcConfigs": [],
        "resourcesWithoutSnapshots": [],
        "resourcesWithoutSnapshotsCount": 0,
        "wcuRestorationFailures": [],
        "startTime": "2026-09-08T00:00:00Z",
    }
    base.update(overrides)
    return base


def published(sns):
    return sns.publish.call_args.kwargs


class TestCompletionNotification:
    def test_a_clean_run_is_reported_as_success(self, sns):
        send_completion_notification(summary(), timeout=False)

        assert published(sns)["Subject"] == "Eon Bulk Recovery - SUCCESS"
        assert "All 1 restore jobs completed successfully" in published(sns)["Message"]

    def test_a_timeout_is_reported_as_such(self, sns):
        send_completion_notification(summary(runningJobs=1), timeout=True)

        assert published(sns)["Subject"] == "Eon Bulk Recovery - TIMEOUT"

    def test_all_jobs_failing_is_reported_as_failure(self, sns):
        send_completion_notification(
            summary(completedJobs=0, failedJobs=1), timeout=False
        )

        assert published(sns)["Subject"] == "Eon Bulk Recovery - FAILURE"

    def test_a_mixed_run_is_reported_as_partial(self, sns):
        send_completion_notification(
            summary(totalJobs=2, completedJobs=1, failedJobs=1), timeout=False
        )

        assert published(sns)["Subject"] == "Eon Bulk Recovery - PARTIAL SUCCESS"

    def test_resources_without_snapshots_downgrade_a_clean_run(self, sns):
        send_completion_notification(
            summary(
                resourcesWithoutSnapshots=[
                    {"resourceName": "cache", "resourceType": "AWS_S3", "region": "us-east-1", "reason": "none"}
                ],
                resourcesWithoutSnapshotsCount=1,
            ),
            timeout=False,
        )

        assert published(sns)["Subject"] == "Eon Bulk Recovery - PARTIAL SUCCESS"
        assert "had no snapshot to restore from" in published(sns)["Message"]

    def test_the_capped_skip_list_says_how_many_are_missing(self, sns):
        send_completion_notification(
            summary(
                resourcesWithoutSnapshots=[
                    {"resourceName": f"r{i}", "resourceType": "AWS_S3", "region": "us-east-1", "reason": "none"}
                    for i in range(2)
                ],
                resourcesWithoutSnapshotsCount=10,
            ),
            timeout=False,
        )

        assert "and 8 more" in published(sns)["Message"]

    def test_the_dynamodb_restore_method_is_reported(self, sns):
        jobs = [job(restoreMethod="RESTORE_METHOD_IMPORT_TABLE")]

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        assert "Restore Method: ImportTable (import from S3)" in published(sns)["Message"]

    def test_a_capacity_based_job_reports_its_wcu(self, sns):
        jobs = [job(restoreMethod="RESTORE_METHOD_CAPACITY_BASED", writeCapacityUnits=28500)]

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        message = published(sns)["Message"]
        assert "Restore Method: capacity-based" in message
        assert "Write Capacity Units: 28,500" in message

    def test_an_unknown_method_value_is_printed_as_is(self, sns):
        jobs = [job(restoreMethod="RESTORE_METHOD_SOMETHING_NEW")]

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        assert "Restore Method: RESTORE_METHOD_SOMETHING_NEW" in published(sns)["Message"]

    def test_a_job_link_is_included(self, sns):
        send_completion_notification(summary(), timeout=False)

        assert "console.eon.io/jobs/restore" in published(sns)["Message"]

    def test_ec2_and_rds_and_s3_details_are_reported(self, sns):
        jobs = [
            job("job-ec2", resourceId="res-ec2", resourceType="AWS_EC2", instanceType="m5.large", volumeCount=2),
            job("job-rds", resourceId="res-rds", resourceType="AWS_RDS", dbInstanceClass="db.r6g.large", restoredName="orders-db"),
            job("job-s3", resourceId="res-s3", resourceType="AWS_S3", restoredBucketName="new-bucket", originalBucketName="old-bucket"),
        ]
        statuses = [
            {"jobId": j["jobId"], "resourceId": j["resourceId"], "resourceName": j["resourceName"],
             "resourceType": j["resourceType"], "currentStatus": "JOB_COMPLETED"}
            for j in jobs
        ]

        send_completion_notification(
            summary(restoreJobs=jobs, jobStatuses=statuses, totalJobs=3, completedJobs=3),
            timeout=False,
        )

        message = published(sns)["Message"]
        assert "Instance Type: m5.large" in message
        assert "Volumes: 2" in message
        assert "Instance Class: db.r6g.large" in message
        assert "Restored Bucket: new-bucket" in message
        assert "Original Bucket: old-bucket" in message

    def test_a_cross_region_restore_shows_both_regions(self, sns):
        jobs = [job(sourceRegion="us-east-1", restoredRegion="eu-west-1")]

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        assert "Source Region: us-east-1 → Restored Region: eu-west-1" in published(sns)["Message"]

    def test_vpc_configurations_are_summarised(self, sns):
        configs = [
            {"vpc": "vpc-1", "region": "us-east-1", "subnetsPerAvailabilityZone": [{}, {}]}
        ]

        send_completion_notification(summary(vpcConfigs=configs), timeout=False)

        assert "vpc-1 in us-east-1 (2 subnets)" in published(sns)["Message"]

    def test_failed_wcu_restoration_adds_an_action_required_section(self, sns):
        failure = job(
            writeCapacityUnits=28500,
            originalTableSettings={
                "wcuScaledUp": True,
                "originalBillingMode": "PROVISIONED",
                "originalWcu": 5,
                "originalRcu": 10,
                "originalGsiThroughput": {"by-customer": {"rcu": 3, "wcu": 4}},
            },
        )

        send_completion_notification(summary(wcuRestorationFailures=[failure]), timeout=False)

        assert published(sns)["Subject"].endswith("ACTION REQUIRED")
        message = published(sns)["Message"]
        assert "Current (elevated) WCU: 28,500" in message
        assert "Restore to: PROVISIONED - 5 WCU, 10 RCU" in message
        assert "by-customer: 3 RCU, 4 WCU" in message

    def test_an_on_demand_table_says_to_switch_billing_mode_back(self, sns):
        failure = job(
            originalTableSettings={
                "wcuScaledUp": True,
                "originalBillingMode": "PAY_PER_REQUEST",
                "originalWcu": 0,
                "originalRcu": 0,
            }
        )

        send_completion_notification(summary(wcuRestorationFailures=[failure]), timeout=False)

        assert "Restore to: PAY_PER_REQUEST" in published(sns)["Message"]

    def test_a_malformed_start_time_leaves_the_duration_unknown(self, sns, capsys):
        send_completion_notification(summary(startTime="not-a-time"), timeout=False)

        assert "Total Duration: Unknown" in published(sns)["Message"]

    def test_a_missing_start_time_leaves_the_duration_unknown(self, sns):
        send_completion_notification(summary(startTime=None), timeout=False)

        assert "Total Duration: Unknown" in published(sns)["Message"]

    def test_snapshot_dates_are_collected_from_the_jobs(self, sns):
        jobs = [
            job("job-1", snapshotPointInTime="2026-09-08T02:00:00Z"),
            job("job-2", resourceId="res-2", snapshotPointInTime="2026-09-07T02:00:00Z"),
        ]

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        assert "Snapshot Date(s): 2026-09-07, 2026-09-08" in published(sns)["Message"]

    def test_no_snapshot_dates_reads_as_not_specified(self, sns):
        send_completion_notification(
            summary(restoreJobs=[job(snapshotPointInTime="Unknown")]), timeout=False
        )

        assert "Snapshot Date(s): Not specified" in published(sns)["Message"]

    def test_a_status_row_without_a_resource_id_is_matched_by_job_id(self, sns):
        statuses = [
            {
                "jobId": "job-1",
                "resourceName": "orders",
                "resourceType": "AWS_DYNAMO_DB",
                "currentStatus": "JOB_COMPLETED",
            }
        ]
        jobs = [job(restoreMethod="RESTORE_METHOD_IMPORT_TABLE")]

        send_completion_notification(summary(restoreJobs=jobs, jobStatuses=statuses), timeout=False)

        assert "Restore Method: ImportTable (import from S3)" in published(sns)["Message"]

    def test_without_a_topic_nothing_is_published(self, sns, monkeypatch, capsys):
        monkeypatch.delenv("SNS_TOPIC_ARN")

        send_completion_notification(summary(), timeout=False)

        sns.publish.assert_not_called()
        assert "No SNS topic ARN configured" in capsys.readouterr().out

    def test_a_publish_failure_is_swallowed(self, sns, capsys):
        sns.publish.side_effect = RuntimeError("sns down")

        send_completion_notification(summary(), timeout=False)

        assert "Failed to send SNS notification" in capsys.readouterr().out

    def test_the_handler_notifies_once_everything_is_done(self, monitored, monkeypatch):
        monitored(get_restore_job=status_response("JOB_COMPLETED"))
        sent = []
        monkeypatch.setattr(
            mj, "send_completion_notification", lambda summary, timeout: sent.append(timeout)
        )

        handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert sent == [False]

    def test_the_handler_does_not_notify_while_jobs_run(self, monitored, monkeypatch):
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        sent = []
        monkeypatch.setattr(
            mj, "send_completion_notification", lambda summary, timeout: sent.append(timeout)
        )

        handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert sent == []

    def test_the_handler_notifies_once_on_timeout(self, monitored, monkeypatch):
        monkeypatch.setenv("MAX_MONITORING_ITERATIONS", "1")
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        sent = []
        monkeypatch.setattr(
            mj, "send_completion_notification", lambda summary, timeout: sent.append(timeout)
        )

        handler({"restoreJobs": [job()], "iteration": 1}, None)

        assert sent == [True]


class TestRemainingBranches:
    def test_a_non_not_found_describe_error_during_wcu_restore_is_reported(self, ddb, capsys):
        ddb.describe_table.side_effect = RuntimeError("AccessDenied")
        job_record = {
            "restoredName": "orders",
            "restoredRegion": "us-east-1",
            "originalTableSettings": {"wcuScaledUp": True, "originalBillingMode": "PROVISIONED",
                                      "originalWcu": 5, "originalRcu": 10},
        }

        assert _restore_dynamodb_table_wcu(job_record, CREDS) is False
        assert "Failed to restore WCU" in capsys.readouterr().out

    def test_a_failed_rollback_for_an_uninitiated_job_is_reported(self, monitored, monkeypatch):
        monitored()
        monkeypatch.setattr(mj, "_restore_dynamodb_table_wcu", lambda j, c: False)
        jobs = [job(job_id=None, originalTableSettings={"wcuScaledUp": True})]

        result = handler({"restoreJobs": jobs, "iteration": 0}, None)

        assert len(result["wcuRestorationFailures"]) == 1

    def test_a_failed_rollback_on_timeout_is_reported(self, monitored, monkeypatch):
        monkeypatch.setenv("MAX_MONITORING_ITERATIONS", "1")
        monitored(get_restore_job=status_response("JOB_RUNNING"))
        monkeypatch.setattr(mj, "_restore_dynamodb_table_wcu", lambda j, c: False)
        jobs = [job(originalTableSettings={"wcuScaledUp": True})]

        result = handler({"restoreJobs": jobs, "iteration": 1}, None)

        assert len(result["wcuRestorationFailures"]) == 1

    def test_a_valid_start_time_produces_a_duration(self, sns):
        send_completion_notification(summary(startTime="2026-09-08T00:00:00Z"), timeout=False)

        message = published(sns)["Message"]
        assert "Total Duration: " in message
        assert "Unknown" not in message.split("Total Duration: ")[1].split("\n")[0]

    def test_a_same_region_restore_prints_one_region(self, sns):
        jobs = [job(sourceRegion="us-east-1", restoredRegion="us-east-1")]

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        assert "   Region: us-east-1" in published(sns)["Message"]

    def test_a_restored_region_without_a_source_region_still_prints(self, sns):
        jobs = [job(restoredRegion="eu-west-1")]
        jobs[0].pop("sourceRegion", None)

        send_completion_notification(summary(restoreJobs=jobs), timeout=False)

        assert "Restored Region: eu-west-1" in published(sns)["Message"]

    def test_a_status_message_and_duration_are_included(self, sns):
        statuses = [
            {
                "jobId": "job-1",
                "resourceId": "res-1",
                "resourceName": "orders",
                "resourceType": "AWS_DYNAMO_DB",
                "currentStatus": "JOB_FAILED",
                "statusMessage": "table already exists",
                "durationSeconds": 3600,
            }
        ]

        send_completion_notification(
            summary(jobStatuses=statuses, completedJobs=0, failedJobs=1), timeout=False
        )

        message = published(sns)["Message"]
        assert "Message: table already exists" in message
        assert "Duration: 60 minutes" in message

    def test_a_job_status_with_no_matching_restore_job_still_renders(self, sns):
        statuses = [
            {
                "jobId": "job-unknown",
                "resourceName": "ghost",
                "resourceType": "AWS_S3",
                "currentStatus": "JOB_FAILED",
            }
        ]

        send_completion_notification(
            summary(jobStatuses=statuses, completedJobs=0, failedJobs=1), timeout=False
        )

        assert "ghost (AWS_S3)" in published(sns)["Message"]

    def test_an_unknown_status_gets_the_fallback_emoji(self, sns):
        statuses = [
            {
                "jobId": "job-1",
                "resourceId": "res-1",
                "resourceName": "orders",
                "resourceType": "AWS_DYNAMO_DB",
                "currentStatus": "JOB_TELEPORTING",
            }
        ]

        send_completion_notification(summary(jobStatuses=statuses), timeout=False)

        assert "❓ orders" in published(sns)["Message"]

    def test_a_single_vpc_config_object_is_accepted(self, sns):
        config = {"vpc": "vpc-1", "region": "us-east-1", "subnetsPerAvailabilityZone": [{}]}

        send_completion_notification(summary(vpcConfigs=config), timeout=False)

        assert "vpc-1 in us-east-1 (1 subnets)" in published(sns)["Message"]

    def test_no_vpc_configs_reads_as_none(self, sns):
        send_completion_notification(summary(vpcConfigs=[]), timeout=False)

        assert "VPC Configurations:\nNone" in published(sns)["Message"]

    def test_an_unknown_original_wcu_is_printed_without_formatting(self, sns):
        failure = job(
            originalTableSettings={"wcuScaledUp": True, "originalBillingMode": "PROVISIONED"}
        )

        send_completion_notification(summary(wcuRestorationFailures=[failure]), timeout=False)

        message = published(sns)["Message"]
        assert "Current (elevated) WCU: Unknown" in message
        assert "Restore to: PROVISIONED - Unknown WCU, Unknown RCU" in message

    def test_a_job_without_an_eon_domain_gets_no_link(self, sns, monkeypatch):
        monkeypatch.setenv("EON_ACCOUNT_DOMAIN", "")

        send_completion_notification(summary(), timeout=False)

        assert "Job Link:" not in published(sns)["Message"]

    def test_a_naive_start_time_is_read_as_utc(self, sns):
        send_completion_notification(summary(startTime="2026-09-08T00:00:00"), timeout=False)

        duration = published(sns)["Message"].split("Total Duration: ")[1].split("\n")[0]
        assert duration.endswith("m")
        assert "Unknown" not in duration


class TestRejectedJobs:
    """
    A rejected job is one Eon refused to start — usually a permissions
    precondition. Observed live: JOB_REJECTED with errorCode
    INSUFFICIENT_ENCRYPTION_PERMISSIONS when the vault CMK policy omits Eon.
    """

    REJECTED = status_response(
        "JOB_REJECTED",
        errorCode="INSUFFICIENT_ENCRYPTION_PERMISSIONS",
        statusMessage=(
            "Can't access backup data in the vault because Eon isn't allowed to use the "
            "vault's customer-managed KMS key."
        ),
        endTime="2026-09-09T19:53:04Z",
        durationSeconds=121,
    )

    def test_the_error_code_is_carried_onto_the_job_status(self, monitored):
        monitored(get_restore_job=self.REJECTED)

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["jobStatuses"][0]["errorCode"] == "INSUFFICIENT_ENCRYPTION_PERMISSIONS"

    def test_a_job_with_no_error_code_gets_an_empty_string(self, monitored):
        monitored(get_restore_job=status_response("JOB_COMPLETED"))

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["jobStatuses"][0]["errorCode"] == ""

    def test_rejections_are_counted_separately_but_still_count_as_failures(self, monitored):
        monitored(get_restore_job=self.REJECTED)

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["failedJobs"] == 1
        assert result["rejectedJobs"] == 1
        assert result["allComplete"] is True

    def test_an_ordinary_failure_is_not_counted_as_a_rejection(self, monitored):
        monitored(get_restore_job=status_response("JOB_FAILED"))

        result = handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert result["failedJobs"] == 1
        assert result["rejectedJobs"] == 0

    def test_the_log_line_calls_out_rejections(self, monitored, capsys):
        monitored(get_restore_job=self.REJECTED)

        handler({"restoreJobs": [job()], "iteration": 0}, None)

        assert "1 failed (of which 1 rejected)" in capsys.readouterr().out

    def test_an_all_rejected_run_gets_its_own_subject(self, sns):
        send_completion_notification(
            summary(completedJobs=0, failedJobs=1, rejectedJobs=1), timeout=False
        )

        assert published(sns)["Subject"] == "Eon Bulk Recovery - REJECTED"
        message = published(sns)["Message"]
        assert "rejected before they started" in message
        assert "Nothing was restored" in message

    def test_a_failure_mixing_rejections_says_so(self, sns):
        send_completion_notification(
            summary(totalJobs=2, completedJobs=0, failedJobs=2, rejectedJobs=1), timeout=False
        )

        assert published(sns)["Subject"] == "Eon Bulk Recovery - FAILURE"
        assert "1 of them rejected before starting" in published(sns)["Message"]

    def test_a_partial_run_mentions_rejections_in_the_breakdown(self, sns):
        send_completion_notification(
            summary(totalJobs=3, completedJobs=1, failedJobs=2, rejectedJobs=1), timeout=False
        )

        assert published(sns)["Subject"] == "Eon Bulk Recovery - PARTIAL SUCCESS"
        assert "2 failed, 1 of them rejected before starting" in published(sns)["Message"]

    def test_the_job_summary_block_reports_the_rejected_count(self, sns):
        send_completion_notification(
            summary(completedJobs=0, failedJobs=1, rejectedJobs=1), timeout=False
        )

        assert "of which rejected before starting: 1" in published(sns)["Message"]

    def test_the_error_code_reaches_the_reader(self, sns):
        statuses = [
            {
                "jobId": "job-1",
                "resourceId": "res-1",
                "resourceName": "orders",
                "resourceType": "AWS_DYNAMO_DB",
                "currentStatus": "JOB_REJECTED",
                "errorCode": "INSUFFICIENT_ENCRYPTION_PERMISSIONS",
                "statusMessage": "Can't access backup data in the vault.",
            }
        ]

        send_completion_notification(
            summary(jobStatuses=statuses, completedJobs=0, failedJobs=1, rejectedJobs=1),
            timeout=False,
        )

        message = published(sns)["Message"]
        assert "⛔ orders (AWS_DYNAMO_DB)" in message
        assert "Status: JOB_REJECTED" in message
        assert "Error Code: INSUFFICIENT_ENCRYPTION_PERMISSIONS" in message
        assert "Message: Can't access backup data in the vault." in message

    def test_a_missing_rejected_count_is_treated_as_zero(self, sns):
        payload = summary(completedJobs=0, failedJobs=1)
        payload.pop("rejectedJobs", None)

        send_completion_notification(payload, timeout=False)

        assert published(sns)["Subject"] == "Eon Bulk Recovery - FAILURE"
        assert "rejected before starting" not in published(sns)["Message"].split("Job Summary")[0]
