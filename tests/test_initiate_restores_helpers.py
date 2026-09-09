"""Pure helpers in the restore-initiation step: naming, regions, WCU maths."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from handlers.initiate_restores import (
    calculate_dynamodb_wcu_allocation_by_region,
    eon_engine_to_aws,
    get_restored_name,
    require_kms_key,
    resolve_target_region,
    sanitize_rds_identifier,
    sanitize_s3_bucket_name,
    stack_table_match,
    validate_rds_instance_class_available,
    _select_ec2_subnet,
)

GiB = 1024 ** 3


class TestEonEngineToAws:
    @pytest.mark.parametrize(
        "eon,aws",
        [
            ("POSTGRES", "postgres"),
            ("AURORA_MYSQL", "aurora-mysql"),
            ("AURORA_POSTGRESQL", "aurora-postgresql"),
            ("MYSQL", "mysql"),
            ("SQLSERVER_SE", "sqlserver-se"),
        ],
    )
    def test_maps_the_enum_to_the_api_name(self, eon, aws):
        assert eon_engine_to_aws(eon) == aws

    @pytest.mark.parametrize("value", [None, "", "UNSPECIFIED", "unspecified"])
    def test_absent_or_unspecified_is_none(self, value):
        assert eon_engine_to_aws(value) is None


class TestResolveTargetRegion:
    def test_uses_the_target_region_when_configured(self):
        assert resolve_target_region("eu-west-1", {"eu-west-1": {}, "us-east-1": {}}, "res") == "eu-west-1"

    def test_falls_back_to_the_first_configured_region(self, capsys):
        assert resolve_target_region("ap-south-1", {"us-east-1": {}}, "res") == "us-east-1"
        assert "falling back to: us-east-1" in capsys.readouterr().out

    def test_no_target_region_falls_back(self):
        assert resolve_target_region(None, {"us-east-1": {}}, "res") == "us-east-1"

    def test_nothing_configured_is_an_error(self):
        with pytest.raises(ValueError, match="No VPC configurations available for restoring res"):
            resolve_target_region("us-east-1", {}, "res")

    def test_the_labels_are_customisable_for_rds(self):
        with pytest.raises(ValueError, match="No RDS subnet groups available"):
            resolve_target_region("us-east-1", {}, "db", "RDS subnet group", "RDS subnet groups")


class TestRequireKmsKey:
    def test_returns_the_key(self):
        assert require_kms_key({"us-east-1": "arn:key"}, "us-east-1") == "arn:key"

    def test_missing_key_is_an_error(self):
        with pytest.raises(ValueError, match="No KMS key available for region eu-west-1"):
            require_kms_key({"us-east-1": "arn:key"}, "eu-west-1")


class TestGetRestoredName:
    def test_no_prefix_keeps_the_original(self):
        assert get_restored_name("orders") == "orders"

    def test_prefix_is_prepended(self):
        assert get_restored_name("orders", "dr-") == "dr-orders"

    def test_an_empty_prefix_is_ignored(self):
        assert get_restored_name("orders", "") == "orders"


class TestSanitizeS3BucketName:
    def test_short_names_pass_through_lowercased(self):
        assert sanitize_s3_bucket_name("My-Bucket", "a1b2c3d4") == "my-bucket-a1b2c3d4"

    def test_the_hash_suffix_always_survives_truncation(self):
        name = sanitize_s3_bucket_name("x" * 100, "a1b2c3d4")

        assert len(name) <= 63
        assert name.endswith("-a1b2c3d4")

    def test_truncation_lands_exactly_on_the_limit(self):
        assert len(sanitize_s3_bucket_name("y" * 63, "a1b2c3d4")) == 63

    def test_consecutive_hyphens_are_collapsed(self):
        assert sanitize_s3_bucket_name("my--weird---bucket", "a1b2c3d4") == "my-weird-bucket-a1b2c3d4"

    def test_leading_and_trailing_hyphens_are_stripped(self):
        assert sanitize_s3_bucket_name("-edge-", "a1b2c3d4") == "edge-a1b2c3d4"

    def test_a_trailing_hyphen_at_the_truncation_point_is_removed(self):
        base = "a" * 53 + "-" + "b" * 20
        name = sanitize_s3_bucket_name(base, "a1b2c3d4")

        assert "--" not in name
        assert name.endswith("-a1b2c3d4")


class TestSanitizeRdsIdentifier:
    def test_lowercases_and_replaces_invalid_characters(self):
        assert sanitize_rds_identifier("My_DB.Instance") == "my-db-instance"

    def test_collapses_and_strips_hyphens(self):
        assert sanitize_rds_identifier("--my--db--") == "my-db"

    def test_prefixes_a_letter_when_it_starts_with_a_digit(self):
        assert sanitize_rds_identifier("123db") == "r123db"

    def test_an_all_invalid_name_becomes_a_hash(self):
        got = sanitize_rds_identifier("___")

        assert got.startswith("r")
        assert len(got) == 9

    def test_long_names_are_truncated_with_a_collision_resistant_suffix(self):
        first = sanitize_rds_identifier("a" * 70 + "-one")
        second = sanitize_rds_identifier("a" * 70 + "-two")

        assert len(first) <= 63 and len(second) <= 63
        assert first != second

    def test_the_result_never_ends_in_a_hyphen(self):
        assert not sanitize_rds_identifier("db-name-" + "x" * 60).endswith("-")

    def test_a_valid_identifier_is_left_alone(self):
        assert sanitize_rds_identifier("orders-db") == "orders-db"


class TestSelectEc2Subnet:
    def test_prefers_a_subnet_in_a_supported_az(self):
        subnets = [
            {"availabilityZone": "us-east-1a", "subnetId": "subnet-a"},
            {"availabilityZone": "us-east-1b", "subnetId": "subnet-b"},
        ]

        assert _select_ec2_subnet(subnets, {"us-east-1b"}, "m5.large") == "subnet-b"

    def test_raises_when_no_configured_az_supports_the_type(self):
        subnets = [{"availabilityZone": "us-east-1a", "subnetId": "subnet-a"}]

        with pytest.raises(ValueError, match="not available in any configured availability zones"):
            _select_ec2_subnet(subnets, {"us-east-1f"}, "p5.48xlarge")

    def test_falls_back_to_any_subnet_when_availability_is_unknown(self):
        subnets = [{"availabilityZone": "us-east-1a", "subnetId": "subnet-a"}]

        assert _select_ec2_subnet(subnets, None, "m5.large") == "subnet-a"

    def test_ignores_entries_without_a_subnet_id(self):
        subnets = [
            {"availabilityZone": "us-east-1a"},
            {"availabilityZone": "us-east-1b", "subnetId": "subnet-b"},
        ]

        assert _select_ec2_subnet(subnets, {"us-east-1a", "us-east-1b"}, "m5.large") == "subnet-b"


def paginated_options(azs):
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"OrderableDBInstanceOptions": [{"AvailabilityZones": [{"Name": az} for az in azs]}]}
    ]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    return client


class TestValidateRdsInstanceClassAvailable:
    def test_passes_when_a_configured_az_offers_the_class(self, capsys):
        client = paginated_options(["us-east-1a", "us-east-1c"])

        validate_rds_instance_class_available(
            client, "POSTGRES", "db.r6g.large", ["us-east-1a", "us-east-1b"], "db"
        )

        assert "available in configured AZs: ['us-east-1a']" in capsys.readouterr().out
        client.get_paginator.return_value.paginate.assert_called_once_with(
            Engine="postgres", DBInstanceClass="db.r6g.large"
        )

    def test_raises_when_the_class_is_not_orderable_at_all(self):
        client = paginated_options([])

        with pytest.raises(ValueError, match="is not orderable for engine postgres"):
            validate_rds_instance_class_available(client, "POSTGRES", "db.r6g.large", ["us-east-1a"], "db")

    def test_raises_when_no_configured_az_offers_it(self):
        client = paginated_options(["us-east-1f"])

        with pytest.raises(ValueError, match="not available in any configured AZ"):
            validate_rds_instance_class_available(client, "POSTGRES", "db.r6g.large", ["us-east-1a"], "db")

    def test_no_class_soft_skips(self, capsys):
        client = MagicMock()

        validate_rds_instance_class_available(client, "POSTGRES", "", ["us-east-1a"], "db")

        client.get_paginator.assert_not_called()
        assert "No source DB instance class" in capsys.readouterr().out

    def test_no_engine_soft_skips(self, capsys):
        client = MagicMock()

        validate_rds_instance_class_available(client, None, "db.r6g.large", ["us-east-1a"], "db")

        client.get_paginator.assert_not_called()
        assert "No engine available" in capsys.readouterr().out

    def test_an_aws_error_soft_skips_rather_than_blocking_the_restore(self, capsys):
        client = MagicMock()
        client.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "DescribeOrderableDBInstanceOptions"
        )

        validate_rds_instance_class_available(client, "POSTGRES", "db.r6g.large", ["us-east-1a"], "db")

        assert "Could not check RDS instance class availability" in capsys.readouterr().out


class TestStackTableMatch:
    TABLES = {
        "orders": {"tableName": "orders", "region": "us-east-1", "regions": ["us-east-1", "eu-west-1"]},
        "legacy": {"tableName": "legacy", "region": "us-east-1"},
    }

    def test_matches_on_name_and_stack_region(self):
        assert stack_table_match(self.TABLES, "orders", "us-east-1")["tableName"] == "orders"

    def test_a_global_table_matches_any_replica_region(self):
        assert stack_table_match(self.TABLES, "orders", "eu-west-1")["tableName"] == "orders"

    def test_no_match_in_another_region(self):
        assert stack_table_match(self.TABLES, "orders", "ap-south-1") is None

    def test_an_entry_without_a_regions_list_falls_back_to_its_region(self):
        assert stack_table_match(self.TABLES, "legacy", "us-east-1")["tableName"] == "legacy"
        assert stack_table_match(self.TABLES, "legacy", "eu-west-1") is None

    def test_an_unknown_table_name_never_matches(self):
        assert stack_table_match(self.TABLES, "nope", "us-east-1") is None


def table(resource_id, name, size_gb):
    return {"resourceId": resource_id, "resourceName": name, "sizeBytes": int(size_gb * GiB)}


class TestWcuAllocation:
    def test_allocates_proportionally_to_size(self):
        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": [table("a", "orders", 30), table("b", "users", 10)]},
            regional_wcu_capacity=40000,
        )

        # 95% of 40,000 = 38,000, split 75/25.
        assert allocation == {"a": 28500, "b": 9500}

    def test_a_table_reporting_no_data_takes_no_share_of_the_budget(self):
        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": [table("a", "orders", 30), table("b", "cache", 0)]},
            regional_wcu_capacity=40000,
        )

        # Nothing to write back, so the whole budget goes to the table that has data.
        assert allocation["a"] == 38000
        assert allocation["b"] == 0

    def test_the_per_table_cap_stops_one_table_taking_everything(self):
        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": [table("a", "orders", 30), table("b", "users", 10)]},
            regional_wcu_capacity=80000,
            table_wcu_max=38000,
        )

        assert allocation["a"] == 38000
        assert allocation["b"] == 19000  # capped budget, 25% share

    def test_regions_are_budgeted_independently(self):
        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {
                "us-east-1": [table("a", "orders", 10)],
                "eu-west-1": [table("b", "orders-eu", 10)],
            },
            regional_wcu_capacity=40000,
        )

        assert allocation == {"a": 38000, "b": 38000}

    def test_every_sized_table_gets_at_least_one_wcu(self):
        tables = [table("big", "big", 1000)] + [
            {"resourceId": f"t{i}", "resourceName": f"t{i}", "sizeBytes": 1} for i in range(5)
        ]

        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": tables}, regional_wcu_capacity=40000
        )

        assert all(value >= 1 for value in allocation.values())

    def test_many_empty_tables_never_dilute_the_budget(self):
        tables = [table("a", "orders", 30)] + [
            {"resourceId": f"z{i}", "resourceName": f"z{i}", "sizeBytes": 0} for i in range(800)
        ]

        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": tables}, regional_wcu_capacity=40000
        )

        assert allocation["a"] == 38000
        assert all(allocation[f"z{i}"] == 0 for i in range(800))

    def test_empty_tables_alone_allocate_nothing(self):
        tables = [
            {"resourceId": f"z{i}", "resourceName": f"z{i}", "sizeBytes": 0} for i in range(3)
        ]

        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": tables}, regional_wcu_capacity=40000
        )

        assert allocation == {"z0": 0, "z1": 0, "z2": 0}

    def test_a_non_zero_default_is_still_bounded_by_what_is_left(self):
        """The knob exists, but sized tables are allocated first and take the budget."""
        with_headroom = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": [{"resourceId": "b", "resourceName": "cache", "sizeBytes": 0}]},
            regional_wcu_capacity=40000,
            default_wcu_for_zero_size=50,
        )
        assert with_headroom["b"] == 50

        no_headroom = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": [table("a", "orders", 30), {"resourceId": "b", "resourceName": "cache", "sizeBytes": 0}]},
            regional_wcu_capacity=40000,
            default_wcu_for_zero_size=50,
        )
        assert no_headroom["b"] == 0

    def test_an_empty_region_list_is_skipped(self):
        assert calculate_dynamodb_wcu_allocation_by_region({"us-east-1": []}) == {}

    def test_no_regions_gives_no_allocation(self):
        assert calculate_dynamodb_wcu_allocation_by_region({}) == {}

    def test_utilisation_leaves_headroom(self):
        allocation = calculate_dynamodb_wcu_allocation_by_region(
            {"us-east-1": [table("a", "orders", 10)]},
            regional_wcu_capacity=40000,
            utilization_percentage=0.5,
        )

        assert allocation["a"] == 20000
