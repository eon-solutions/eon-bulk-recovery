"""Resource listing: type scoping, ID routing, pagination, field extraction."""

import pytest

from handlers import list_resources
from handlers.list_resources import (
    SUPPORTED_RESOURCE_TYPES,
    build_resource_id_queries,
    resolve_resource_types,
)

UUID = "1ee34dc5-0a7c-4e56-a820-917371e05c8d"
OTHER_UUID = "2f97ca76-6a78-55d8-94d3-66c2f2cfff23"


class TestResolveResourceTypes:
    @pytest.mark.parametrize("requested", [None, [], ["", "  "]])
    def test_empty_means_everything_supported(self, requested):
        assert resolve_resource_types(requested) == SUPPORTED_RESOURCE_TYPES

    def test_returns_a_copy_not_the_module_list(self):
        got = resolve_resource_types(None)
        got.append("AWS_EFS")
        assert "AWS_EFS" not in SUPPORTED_RESOURCE_TYPES

    def test_normalises_case_and_whitespace(self):
        assert resolve_resource_types([" aws_ec2 ", "Aws_S3"]) == ["AWS_EC2", "AWS_S3"]

    def test_preserves_canonical_order_and_drops_duplicates(self):
        assert resolve_resource_types(["AWS_S3", "AWS_EC2", "AWS_S3"]) == ["AWS_EC2", "AWS_S3"]

    def test_an_unsupported_type_fails_at_the_input(self):
        with pytest.raises(ValueError, match="Unsupported resourceTypes: AWS_EFS"):
            resolve_resource_types(["AWS_EC2", "AWS_EFS"])

    def test_reports_every_unsupported_type_once_sorted(self):
        with pytest.raises(ValueError, match="AWS_EFS, AWS_FSX"):
            resolve_resource_types(["AWS_FSX", "AWS_EFS", "AWS_FSX"])


class TestBuildResourceIdQueries:
    @pytest.mark.parametrize("ids", [None, [], ["", "   "]])
    def test_no_ids_means_no_queries(self, ids):
        assert build_resource_id_queries(ids) == []

    def test_non_uuid_ids_only_go_to_the_provider_filter(self):
        assert build_resource_id_queries(["i-0f600a1b15b035105", "my-bucket"]) == [
            {"provider_resource_ids": ["i-0f600a1b15b035105", "my-bucket"]}
        ]

    def test_uuid_ids_are_queried_both_ways(self):
        """A DynamoDB providerResourceId is itself a UUID, so shape cannot route it."""
        assert build_resource_id_queries([UUID]) == [
            {"provider_resource_ids": [UUID]},
            {"resource_ids": [UUID]},
        ]

    def test_mixed_input_splits_correctly(self):
        assert build_resource_id_queries([UUID, "my-bucket"]) == [
            {"provider_resource_ids": [UUID, "my-bucket"]},
            {"resource_ids": [UUID]},
        ]

    def test_uuid_matching_is_case_insensitive(self):
        assert build_resource_id_queries([UUID.upper()])[1] == {"resource_ids": [UUID.upper()]}

    def test_blank_entries_are_dropped(self):
        assert build_resource_id_queries(["  ", "my-bucket", ""]) == [
            {"provider_resource_ids": ["my-bucket"]}
        ]

    def test_ids_are_stripped(self):
        assert build_resource_id_queries(["  my-bucket  "]) == [
            {"provider_resource_ids": ["my-bucket"]}
        ]


def resource(**overrides):
    base = {
        "id": UUID,
        "resourceName": "web-1",
        "resourceType": "AWS_EC2",
        "providerResourceId": "i-0f600a1b15b035105",
        "resourceProperties": {"region": "us-east-1", "instanceType": "m5.large"},
        "latestSnapshotTime": "2026-09-08T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestHandler:
    def test_lists_and_flattens_resources(self, eon_credentials, fake_eon_client, patch_eon_client):
        patch_eon_client(
            "handlers.list_resources",
            fake_eon_client(list_resources={"resources": [resource()]}),
        )

        result = list_resources.handler({"sourceAccountId": "333333333333"}, None)

        assert result["totalCount"] == 1
        assert result["sourceAccountId"] == "333333333333"
        assert result["resourceTypes"] == SUPPORTED_RESOURCE_TYPES
        assert result["requestedResourceIdsNotFound"] == []
        assert result["resources"][0] == {
            "id": UUID,
            "resourceName": "web-1",
            "resourceType": "AWS_EC2",
            "providerResourceId": "i-0f600a1b15b035105",
            "region": "us-east-1",
            "vpc": None,
            "subnets": [],
            "latestSnapshotTime": "2026-09-08T00:00:00Z",
            "instanceType": "m5.large",
        }

    def test_region_falls_back_to_the_top_level_field(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        res = resource(resourceProperties={}, region="eu-west-1")
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources={"resources": [res]}))

        result = list_resources.handler({"sourceAccountId": "333333333333"}, None)

        assert result["resources"][0]["region"] == "eu-west-1"

    def test_rds_carries_instance_class_and_engine(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        res = resource(
            resourceType="AWS_RDS",
            resourceProperties={
                "region": "us-east-1",
                "awsRds": {"instanceClass": "db.r6g.large", "engine": "AURORA_POSTGRESQL"},
            },
        )
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources={"resources": [res]}))

        got = list_resources.handler({"sourceAccountId": "333333333333"}, None)["resources"][0]

        assert got["dbInstanceClass"] == "db.r6g.large"
        assert got["engine"] == "AURORA_POSTGRESQL"

    def test_rds_without_aws_rds_properties(self, eon_credentials, fake_eon_client, patch_eon_client):
        res = resource(resourceType="AWS_RDS", resourceProperties={"region": "us-east-1"})
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources={"resources": [res]}))

        got = list_resources.handler({"sourceAccountId": "333333333333"}, None)["resources"][0]

        assert got["dbInstanceClass"] is None
        assert got["engine"] is None

    def test_dynamodb_carries_the_table_size(self, eon_credentials, fake_eon_client, patch_eon_client):
        res = resource(resourceType="AWS_DYNAMO_DB", sourceStorage={"sizeBytes": 10_737_418_240})
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources={"resources": [res]}))

        got = list_resources.handler({"sourceAccountId": "333333333333"}, None)["resources"][0]

        assert got["tableSizeBytes"] == 10_737_418_240

    def test_dynamodb_without_source_storage_defaults_to_zero(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        res = resource(resourceType="AWS_DYNAMO_DB")
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources={"resources": [res]}))

        got = list_resources.handler({"sourceAccountId": "333333333333"}, None)["resources"][0]

        assert got["tableSizeBytes"] == 0

    def test_pages_until_the_token_runs_out(self, eon_credentials, fake_eon_client, patch_eon_client):
        pages = [
            {"resources": [resource(id="a")], "nextToken": "page-2"},
            {"resources": [resource(id="b")], "nextToken": "page-3"},
            {"resources": [resource(id="c")]},
        ]
        client = patch_eon_client(
            "handlers.list_resources", fake_eon_client(list_resources=list(pages))
        )

        result = list_resources.handler({"sourceAccountId": "333333333333"}, None)

        assert [r["id"] for r in result["resources"]] == ["a", "b", "c"]
        tokens = [call["page_token"] for call in client.calls_to("list_resources")]
        assert tokens == [None, "page-2", "page-3"]

    def test_type_scope_is_passed_to_the_api(self, eon_credentials, fake_eon_client, patch_eon_client):
        client = patch_eon_client(
            "handlers.list_resources", fake_eon_client(list_resources={"resources": []})
        )

        list_resources.handler(
            {"sourceAccountId": "333333333333", "resourceTypes": ["AWS_S3"]}, None
        )

        assert client.calls_to("list_resources")[0]["resource_types"] == ["AWS_S3"]

    def test_id_scope_unions_the_two_queries(self, eon_credentials, fake_eon_client, patch_eon_client):
        by_provider = {"resources": [resource(id="a", providerResourceId="my-bucket")]}
        by_id = {"resources": [resource(id=UUID, providerResourceId="ddb-table")]}
        client = patch_eon_client(
            "handlers.list_resources", fake_eon_client(list_resources=[by_provider, by_id])
        )

        result = list_resources.handler(
            {"sourceAccountId": "333333333333", "resourceIds": [UUID, "my-bucket"]}, None
        )

        assert [r["id"] for r in result["resources"]] == ["a", UUID]
        calls = client.calls_to("list_resources")
        assert calls[0]["provider_resource_ids"] == [UUID, "my-bucket"]
        assert calls[1]["resource_ids"] == [UUID]

    def test_a_resource_matched_by_both_queries_appears_once(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        same = resource(id=UUID, providerResourceId=UUID)
        patch_eon_client(
            "handlers.list_resources",
            fake_eon_client(list_resources=[{"resources": [same]}, {"resources": [same]}]),
        )

        result = list_resources.handler(
            {"sourceAccountId": "333333333333", "resourceIds": [UUID]}, None
        )

        assert result["totalCount"] == 1

    def test_reports_requested_ids_that_matched_nothing(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        found = {"resources": [resource(id=UUID, providerResourceId="my-bucket")]}
        patch_eon_client(
            "handlers.list_resources", fake_eon_client(list_resources=[found, {"resources": []}])
        )

        result = list_resources.handler(
            {"sourceAccountId": "333333333333", "resourceIds": [UUID, "my-bucket", "typo-name"]},
            None,
        )

        assert result["requestedResourceIdsNotFound"] == ["typo-name"]

    def test_an_id_matched_by_provider_id_counts_as_found(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        found = {"resources": [resource(id=OTHER_UUID, providerResourceId="my-bucket")]}
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources=[found]))

        result = list_resources.handler(
            {"sourceAccountId": "333333333333", "resourceIds": ["my-bucket"]}, None
        )

        assert result["requestedResourceIdsNotFound"] == []

    def test_an_unsupported_type_fails_the_step(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client("handlers.list_resources", fake_eon_client())

        with pytest.raises(ValueError, match="Unsupported resourceTypes"):
            list_resources.handler(
                {"sourceAccountId": "333333333333", "resourceTypes": ["AWS_EFS"]}, None
            )

    def test_a_type_with_no_extra_fields_passes_straight_through(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        res = resource(resourceType="AWS_S3", resourceName="my-bucket")
        patch_eon_client("handlers.list_resources", fake_eon_client(list_resources={"resources": [res]}))

        got = list_resources.handler({"sourceAccountId": "333333333333"}, None)["resources"][0]

        assert got["resourceType"] == "AWS_S3"
        assert "instanceType" not in got
        assert "tableSizeBytes" not in got
