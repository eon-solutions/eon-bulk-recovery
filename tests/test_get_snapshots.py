"""Snapshot selection, skip reporting, and the no-snapshots alert."""

from unittest.mock import MagicMock, patch

import pytest

from handlers import get_snapshots
from handlers.get_snapshots import (
    MAX_REPORTED_SKIPS,
    parse_snapshot_date,
    send_no_snapshots_notification,
)

from conftest import SNS_TOPIC_ARN


class TestParseSnapshotDate:
    @pytest.mark.parametrize("value", [None, "", "   ", "latest", "LATEST", " Latest "])
    def test_latest_means_no_date_filter(self, value):
        assert parse_snapshot_date(value) == (None, None)

    def test_a_pinned_date_becomes_an_inclusive_range(self):
        assert parse_snapshot_date("2026-08-29") == ("2026-08-29", "2026-08-30")

    def test_month_and_year_rollover(self):
        assert parse_snapshot_date("2026-12-31") == ("2026-12-31", "2027-01-01")

    @pytest.mark.parametrize("value", ["29-08-2026", "2026/08/29", "yesterday", "2026-13-01"])
    def test_a_malformed_date_fails_at_the_input(self, value):
        with pytest.raises(ValueError, match="Invalid snapshotDate"):
            parse_snapshot_date(value)

    def test_the_error_names_the_accepted_forms(self):
        with pytest.raises(ValueError) as excinfo:
            parse_snapshot_date("nope")
        assert "YYYY-MM-DD" in str(excinfo.value)
        assert "latest" in str(excinfo.value)


def skipped(count: int):
    return [
        {
            "resourceName": f"res-{i}",
            "resourceType": "AWS_EC2",
            "region": "us-east-1",
            "reason": "no snapshots exist for this resource",
        }
        for i in range(count)
    ]


class TestNoSnapshotsNotification:
    def test_publishes_to_sns(self):
        sns = MagicMock()
        with patch.object(get_snapshots.boto3, "client", return_value=sns):
            send_no_snapshots_notification("333333333333", "2026-08-29", 3, skipped(2))

        args = sns.publish.call_args.kwargs
        assert args["TopicArn"] == SNS_TOPIC_ARN
        assert args["Subject"] == "Eon Bulk Recovery - NO SNAPSHOTS FOUND"
        assert "none of them have a snapshot on 2026-08-29" in args["Message"]
        assert "Source Account: 333333333333" in args["Message"]
        assert "- res-0 (AWS_EC2) in us-east-1" in args["Message"]

    def test_latest_reads_as_at_all(self):
        sns = MagicMock()
        with patch.object(get_snapshots.boto3, "client", return_value=sns):
            send_no_snapshots_notification("333333333333", "latest", 1, skipped(1))

        assert "have a snapshot at all" in sns.publish.call_args.kwargs["Message"]

    def test_a_missing_source_account_reads_as_unknown(self):
        sns = MagicMock()
        with patch.object(get_snapshots.boto3, "client", return_value=sns):
            send_no_snapshots_notification(None, None, 1, skipped(1))

        message = sns.publish.call_args.kwargs["Message"]
        assert "Source Account: Unknown" in message
        assert "Requested Snapshot Date: latest" in message

    def test_the_listing_is_capped_and_says_so(self):
        sns = MagicMock()
        with patch.object(get_snapshots.boto3, "client", return_value=sns):
            send_no_snapshots_notification("333333333333", None, 150, skipped(150))

        message = sns.publish.call_args.kwargs["Message"]
        assert f"and {150 - MAX_REPORTED_SKIPS} more" in message
        assert "res-100 " not in message

    def test_without_a_topic_it_does_nothing(self, monkeypatch, capsys):
        monkeypatch.delenv("SNS_TOPIC_ARN")
        with patch.object(get_snapshots.boto3, "client") as boto_client:
            send_no_snapshots_notification("333333333333", None, 1, skipped(1))

        boto_client.assert_not_called()
        assert "No SNS topic ARN configured" in capsys.readouterr().out

    def test_a_publish_failure_is_swallowed(self, capsys):
        sns = MagicMock()
        sns.publish.side_effect = RuntimeError("sns down")
        with patch.object(get_snapshots.boto3, "client", return_value=sns):
            send_no_snapshots_notification("333333333333", None, 1, skipped(1))

        assert "Failed to send 'no snapshots found' notification" in capsys.readouterr().out


def ec2_resource(**overrides):
    base = {
        "id": "res-1",
        "resourceName": "web-1",
        "resourceType": "AWS_EC2",
        "providerResourceId": "i-0f600a1b15b035105",
        "region": "us-east-1",
    }
    base.update(overrides)
    return base


def snapshot_response(properties=None, tags=None, **overrides):
    snapshot = {
        "id": "snap-1",
        "pointInTime": "2026-09-08T02:00:00Z",
        "resource": {"properties": properties or {}, "tags": tags or {}},
    }
    snapshot.update(overrides)
    return {"snapshots": [snapshot]}


EC2_PROPERTIES = {
    "awsEc2": {
        "instanceType": "m5.large",
        "instanceProfileName": "app-profile",
        "volumes": [{"volumeId": "vol-1"}],
    }
}


class TestHandler:
    def test_selects_the_newest_snapshot_and_carries_metadata(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        client = patch_eon_client(
            "handlers.get_snapshots",
            fake_eon_client(
                list_snapshots=snapshot_response(EC2_PROPERTIES, {"env": "prod"})
            ),
        )

        result = get_snapshots.handler({"resources": [ec2_resource()]}, None)

        assert result["totalSnapshots"] == 1
        assert result["resourcesWithoutSnapshots"] == []
        assert result["resourcesWithoutSnapshotsCount"] == 0
        snapshot = result["resourceSnapshots"][0]
        assert snapshot["snapshotId"] == "snap-1"
        assert snapshot["snapshotPointInTime"] == "2026-09-08T02:00:00Z"
        assert snapshot["originalTags"] == {"env": "prod"}
        assert snapshot["instanceType"] == "m5.large"
        assert snapshot["instanceProfileName"] == "app-profile"
        assert snapshot["volumes"] == [{"volumeId": "vol-1"}]
        assert client.calls_to("list_snapshots")[0]["page_size"] == 10

    def test_a_pinned_date_is_passed_to_the_api(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        client = patch_eon_client(
            "handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response(EC2_PROPERTIES))
        )

        get_snapshots.handler(
            {"resources": [ec2_resource()], "snapshotDate": "2026-08-29"}, None
        )

        call = client.calls_to("list_snapshots")[0]
        assert call["start_date"] == "2026-08-29"
        assert call["end_date"] == "2026-08-30"

    def test_latest_sends_no_date_filter(self, eon_credentials, fake_eon_client, patch_eon_client):
        client = patch_eon_client(
            "handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response(EC2_PROPERTIES))
        )

        get_snapshots.handler({"resources": [ec2_resource()], "snapshotDate": "latest"}, None)

        call = client.calls_to("list_snapshots")[0]
        assert call["start_date"] is None and call["end_date"] is None

    def test_no_snapshots_on_a_pinned_date_reports_the_latest_available(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots={"snapshots": []}))
        resource = ec2_resource(latestSnapshotTime="2026-09-07T00:00:00Z")

        with patch.object(get_snapshots, "send_no_snapshots_notification") as notify:
            result = get_snapshots.handler(
                {"resources": [resource], "snapshotDate": "2026-08-29", "sourceAccountId": "333333333333"},
                None,
            )

        assert result["resourcesWithoutSnapshots"][0]["reason"] == (
            "no snapshot taken on 2026-08-29, latest available is 2026-09-07T00:00:00Z"
        )
        notify.assert_called_once()

    def test_no_snapshots_on_a_pinned_date_without_a_known_latest(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots={"snapshots": []}))

        with patch.object(get_snapshots, "send_no_snapshots_notification"):
            result = get_snapshots.handler(
                {"resources": [ec2_resource()], "snapshotDate": "2026-08-29"}, None
            )

        assert result["resourcesWithoutSnapshots"][0]["reason"] == "no snapshot taken on 2026-08-29"

    def test_a_resource_with_no_snapshots_at_all(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots={"snapshots": []}))

        with patch.object(get_snapshots, "send_no_snapshots_notification"):
            result = get_snapshots.handler({"resources": [ec2_resource()]}, None)

        assert result["resourcesWithoutSnapshots"][0]["reason"] == (
            "no snapshots exist for this resource"
        )

    def test_ec2_snapshot_without_properties_is_skipped(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client(
            "handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response({}))
        )

        with patch.object(get_snapshots, "send_no_snapshots_notification"):
            result = get_snapshots.handler({"resources": [ec2_resource()]}, None)

        assert result["totalSnapshots"] == 0
        assert result["resourcesWithoutSnapshots"][0]["reason"] == "snapshot has no awsEc2 properties"

    def test_ec2_snapshot_without_volumes_is_skipped(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        properties = {"awsEc2": {"instanceType": "m5.large", "volumes": []}}
        patch_eon_client(
            "handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response(properties))
        )

        with patch.object(get_snapshots, "send_no_snapshots_notification"):
            result = get_snapshots.handler({"resources": [ec2_resource()]}, None)

        assert result["resourcesWithoutSnapshots"][0]["reason"] == "snapshot contains no volumes"

    def test_a_lookup_failure_records_the_resource_and_continues(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        def responder(**kwargs):
            if kwargs["resource_id"] == "res-1":
                raise RuntimeError("API exploded")
            return snapshot_response(EC2_PROPERTIES)

        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots=responder))

        result = get_snapshots.handler(
            {"resources": [ec2_resource(), ec2_resource(id="res-2", resourceName="web-2")]}, None
        )

        assert result["totalSnapshots"] == 1
        assert result["resourcesWithoutSnapshots"][0]["reason"] == (
            "snapshot lookup failed: API exploded"
        )

    def test_rds_carries_class_and_engine_from_the_resource(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response()))
        resource = ec2_resource(
            resourceType="AWS_RDS", dbInstanceClass="db.r6g.large", engine="POSTGRES"
        )

        snapshot = get_snapshots.handler({"resources": [resource]}, None)["resourceSnapshots"][0]

        assert snapshot["dbInstanceClass"] == "db.r6g.large"
        assert snapshot["engine"] == "POSTGRES"

    def test_dynamodb_carries_the_table_size(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response()))
        resource = ec2_resource(resourceType="AWS_DYNAMO_DB", tableSizeBytes=10_737_418_240)

        snapshot = get_snapshots.handler({"resources": [resource]}, None)["resourceSnapshots"][0]

        assert snapshot["tableSizeBytes"] == 10_737_418_240

    def test_dynamodb_with_no_size_warns_but_is_still_restored(
        self, eon_credentials, fake_eon_client, patch_eon_client, capsys
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response()))
        resource = ec2_resource(resourceType="AWS_DYNAMO_DB")

        result = get_snapshots.handler({"resources": [resource]}, None)

        assert result["totalSnapshots"] == 1
        assert result["resourceSnapshots"][0]["tableSizeBytes"] == 0
        assert "No size information available" in capsys.readouterr().out

    def test_the_reported_skip_list_is_capped(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots={"snapshots": []}))
        resources = [ec2_resource(id=f"res-{i}", resourceName=f"web-{i}") for i in range(150)]

        with patch.object(get_snapshots, "send_no_snapshots_notification"):
            result = get_snapshots.handler({"resources": resources}, None)

        assert len(result["resourcesWithoutSnapshots"]) == MAX_REPORTED_SKIPS
        assert result["resourcesWithoutSnapshotsCount"] == 150

    def test_the_alert_fires_only_when_nothing_can_be_restored(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        def responder(**kwargs):
            if kwargs["resource_id"] == "res-1":
                return {"snapshots": []}
            return snapshot_response(EC2_PROPERTIES)

        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots=responder))

        with patch.object(get_snapshots, "send_no_snapshots_notification") as notify:
            get_snapshots.handler(
                {"resources": [ec2_resource(), ec2_resource(id="res-2")]}, None
            )

        notify.assert_not_called()

    def test_an_empty_resource_list_sends_no_alert(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client())

        with patch.object(get_snapshots, "send_no_snapshots_notification") as notify:
            result = get_snapshots.handler({"resources": []}, None)

        notify.assert_not_called()
        assert result["totalSnapshots"] == 0

    def test_a_malformed_snapshot_date_fails_the_step(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client())

        with pytest.raises(ValueError, match="Invalid snapshotDate"):
            get_snapshots.handler({"resources": [], "snapshotDate": "nope"}, None)

    def test_a_type_with_no_extra_properties_passes_straight_through(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.get_snapshots", fake_eon_client(list_snapshots=snapshot_response()))
        resource = ec2_resource(resourceType="AWS_S3", resourceName="my-bucket")

        snapshot = get_snapshots.handler({"resources": [resource]}, None)["resourceSnapshots"][0]

        assert snapshot["resourceType"] == "AWS_S3"
        assert "instanceType" not in snapshot
        assert "tableSizeBytes" not in snapshot
