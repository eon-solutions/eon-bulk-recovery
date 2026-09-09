"""EC2, RDS and S3 restores, plus recovery-stack discovery and bucket creation."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from handlers import initiate_restores as ir
from handlers.initiate_restores import (
    _global_table_replica_regions,
    _initiate_ec2_restore,
    _initiate_rds_restore,
    _initiate_s3_restore,
    _restore_s3_in_place,
    _restore_s3_new_bucket,
    create_s3_bucket,
    discover_dynamodb_tables_from_stacks,
    discover_s3_buckets_from_stacks,
)

CREDS = {"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}


def client_error(code: str, message: str = "", operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message or code}}, operation)


@pytest.fixture
def aws(monkeypatch):
    """Addressable per-(service, region) clients behind create_boto3_client."""

    class Clients(dict):
        def __missing__(self, key):
            client = MagicMock(name=key)
            self[key] = client
            return client

    clients = Clients()

    def factory(service, region, credentials=None):
        client = clients[f"{service}:{region}"]
        client.credentials = credentials
        return client

    monkeypatch.setattr(ir, "create_boto3_client", factory)
    return clients


def stack_resources(client, resources):
    paginator = MagicMock()
    paginator.paginate.return_value = [{"StackResourceSummaries": resources}]
    client.get_paginator.return_value = paginator
    return paginator


EC2_SNAPSHOT = {
    "resourceId": "res-ec2",
    "resourceName": "web-1",
    "snapshotId": "snap-1",
    "snapshotPointInTime": "2026-09-08T02:00:00Z",
    "providerResourceId": "i-0f600a1b15b035105",
    "instanceType": "m5.large",
    "volumes": [
        {"providerVolumeId": "vol-1", "volumeSettings": {"sizeGb": 8}, "tags": {"role": "root"}}
    ],
    "originalTags": {"env": "prod", "aws:autoscaling:groupName": "asg-1"},
}


class TestEc2Restore:
    def test_builds_the_destination_from_the_snapshot_and_vpc_config(
        self, aws, restore_context, fake_eon_client
    ):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        client = fake_eon_client(restore_ec2_instance="job-1")
        ctx = restore_context(eon_client=client)

        job_id, details = _initiate_ec2_restore(ctx, EC2_SNAPSHOT, "us-east-1")

        assert job_id == "job-1"
        assert details == {
            "restoredRegion": "us-east-1",
            "instanceType": "m5.large",
            "volumeCount": 1,
            "restoredName": "web-1",
        }
        destination = client.calls_to("restore_ec2_instance")[0]["destination_config"]["awsEc2"]
        assert destination["subnetId"] == "subnet-1a"
        assert destination["securityGroupIds"] == ["sg-restore"]
        assert destination["instanceType"] == "m5.large"
        assert destination["tags"]["Name"] == "web-1"
        assert destination["tags"]["RestoreSource"] == "i-0f600a1b15b035105"

    def test_volume_parameters_carry_the_kms_key_and_restore_tags(
        self, aws, restore_context, fake_eon_client
    ):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        client = fake_eon_client(restore_ec2_instance="job-1")

        _initiate_ec2_restore(restore_context(eon_client=client), EC2_SNAPSHOT, "us-east-1")

        volume = client.calls_to("restore_ec2_instance")[0]["destination_config"]["awsEc2"][
            "volumeRestoreParameters"
        ][0]
        assert volume["providerVolumeId"] == "vol-1"
        assert volume["volumeEncryptionKeyId"].startswith("arn:aws:kms:")
        assert volume["volumeSettings"] == {"sizeGb": 8}
        assert volume["tags"]["role"] == "root"
        assert volume["tags"]["eon:snapshot_id"] == "snap-1"

    def test_excluded_tag_keys_are_dropped_from_instance_and_volumes(
        self, aws, restore_context, fake_eon_client, capsys
    ):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        client = fake_eon_client(restore_ec2_instance="job-1")
        snapshot = dict(EC2_SNAPSHOT)
        snapshot["volumes"] = [
            {"providerVolumeId": "vol-1", "tags": {"aws:autoscaling:groupName": "asg-1"}}
        ]
        ctx = restore_context(eon_client=client, exclude_ec2_tag_keys=["aws:autoscaling:groupName"])

        _initiate_ec2_restore(ctx, snapshot, "us-east-1")

        destination = client.calls_to("restore_ec2_instance")[0]["destination_config"]["awsEc2"]
        assert "aws:autoscaling:groupName" not in destination["tags"]
        assert "aws:autoscaling:groupName" not in destination["volumeRestoreParameters"][0]["tags"]
        assert "Excluding EC2 tags for web-1" in capsys.readouterr().out

    def test_an_unavailable_instance_type_check_falls_back_to_any_subnet(
        self, aws, restore_context, fake_eon_client, capsys
    ):
        aws["ec2:us-east-1"].describe_instance_type_offerings.side_effect = client_error(
            "UnauthorizedOperation"
        )
        client = fake_eon_client(restore_ec2_instance="job-1")

        _initiate_ec2_restore(restore_context(eon_client=client), EC2_SNAPSHOT, "us-east-1")

        assert "Could not check instance type availability" in capsys.readouterr().out

    def test_no_subnets_in_the_region_is_an_error(self, aws, restore_context, fake_eon_client):
        ctx = restore_context(
            eon_client=fake_eon_client(),
            vpc_configs_by_region={"us-east-1": {"subnetsPerAvailabilityZone": []}},
        )

        with pytest.raises(ValueError, match="No subnets available in region us-east-1"):
            _initiate_ec2_restore(ctx, EC2_SNAPSHOT, "us-east-1")

    def test_a_snapshot_with_no_volumes_is_an_error(self, aws, restore_context, fake_eon_client):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        snapshot = dict(EC2_SNAPSHOT, volumes=[])

        with pytest.raises(ValueError, match="No volumes found for EC2 instance web-1"):
            _initiate_ec2_restore(restore_context(eon_client=fake_eon_client()), snapshot, "us-east-1")

    def test_a_missing_instance_type_falls_back_to_t3_medium(
        self, aws, restore_context, fake_eon_client
    ):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        client = fake_eon_client(restore_ec2_instance="job-1")
        snapshot = {k: v for k, v in EC2_SNAPSHOT.items() if k != "instanceType"}

        _, details = _initiate_ec2_restore(restore_context(eon_client=client), snapshot, "us-east-1")

        assert details["instanceType"] == "t3.medium"

    def test_the_name_prefix_is_applied(self, aws, restore_context, fake_eon_client):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        client = fake_eon_client(restore_ec2_instance="job-1")
        ctx = restore_context(eon_client=client, resource_name_prefix="dr-")

        _, details = _initiate_ec2_restore(ctx, EC2_SNAPSHOT, "us-east-1")

        assert details["restoredName"] == "dr-web-1"

    def test_the_source_instance_profile_is_not_reattached(
        self, aws, restore_context, fake_eon_client
    ):
        aws["ec2:us-east-1"].describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [{"Location": "us-east-1a"}]
        }
        client = fake_eon_client(restore_ec2_instance="job-1")
        snapshot = dict(EC2_SNAPSHOT, instanceProfileName="app-profile")

        _initiate_ec2_restore(restore_context(eon_client=client), snapshot, "us-east-1")

        destination = client.calls_to("restore_ec2_instance")[0]["destination_config"]["awsEc2"]
        assert "instanceProfileName" not in destination


RDS_SNAPSHOT = {
    "resourceId": "res-rds",
    "resourceName": "Orders_DB",
    "snapshotId": "snap-1",
    "snapshotPointInTime": "2026-09-08T02:00:00Z",
    "providerResourceId": "orders-db",
    "dbInstanceClass": "db.r6g.large",
    "engine": "POSTGRES",
    "originalTags": {"env": "prod"},
}


def orderable(aws, azs, region="us-east-1"):
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"OrderableDBInstanceOptions": [{"AvailabilityZones": [{"Name": az} for az in azs]}]}
    ]
    aws[f"rds:{region}"].get_paginator.return_value = paginator


class TestRdsRestore:
    def test_builds_the_destination_and_sanitises_the_identifier(
        self, aws, restore_context, fake_eon_client
    ):
        orderable(aws, ["us-east-1a"])
        client = fake_eon_client(restore_rds_instance="job-1")

        job_id, details = _initiate_rds_restore(
            restore_context(eon_client=client), RDS_SNAPSHOT, "us-east-1"
        )

        assert job_id == "job-1"
        assert details["restoredName"] == "orders-db"
        assert details["subnetGroup"] == "eon-restore-222222222222-us-east-1"
        destination = client.calls_to("restore_rds_instance")[0]["destination_config"]["awsRds"]
        assert destination["dbInstanceClass"] == "db.r6g.large"
        assert destination["securityGroups"] == ["sg-rds"]
        assert destination["subnetGroup"] == "eon-restore-222222222222-us-east-1"

    def test_an_absent_instance_class_is_omitted_so_eon_inherits_it(
        self, aws, restore_context, fake_eon_client, capsys
    ):
        """Aurora members carry no class in the Eon API; the field must be left out."""
        client = fake_eon_client(restore_rds_instance="job-1")
        snapshot = dict(RDS_SNAPSHOT, dbInstanceClass=None)

        _, details = _initiate_rds_restore(
            restore_context(eon_client=client), snapshot, "us-east-1"
        )

        destination = client.calls_to("restore_rds_instance")[0]["destination_config"]["awsRds"]
        assert "dbInstanceClass" not in destination
        assert details["dbInstanceClass"] == "(inherit from source)"
        assert "omitting dbInstanceClass" in capsys.readouterr().out

    def test_an_unavailable_instance_class_stops_the_restore(
        self, aws, restore_context, fake_eon_client
    ):
        orderable(aws, ["us-east-1f"])

        with pytest.raises(ValueError, match="not available in any configured AZ"):
            _initiate_rds_restore(
                restore_context(eon_client=fake_eon_client()), RDS_SNAPSHOT, "us-east-1"
            )

    def test_region_resolution_uses_the_subnet_groups(
        self, aws, restore_context, fake_eon_client
    ):
        orderable(aws, ["eu-west-1a"], region="eu-west-1")
        client = fake_eon_client(restore_rds_instance="job-1")
        ctx = restore_context(
            eon_client=client,
            rds_subnet_groups_by_region={"eu-west-1": "eon-restore-eu"},
            kms_key_arns_by_region={"eu-west-1": "arn:kms:eu"},
            vpc_configs_by_region={
                "eu-west-1": {
                    "subnetsPerAvailabilityZone": [{"availabilityZone": "eu-west-1a", "subnetId": "s"}],
                    "securityGroups": {"restoredRdsInstance": ["sg-eu"]},
                }
            },
        )

        _, details = _initiate_rds_restore(ctx, RDS_SNAPSHOT, "us-east-1")

        assert details["restoredRegion"] == "eu-west-1"

    def test_no_subnet_group_anywhere_is_an_error(self, aws, restore_context, fake_eon_client):
        ctx = restore_context(eon_client=fake_eon_client(), rds_subnet_groups_by_region={})

        with pytest.raises(ValueError, match="No RDS subnet groups available"):
            _initiate_rds_restore(ctx, RDS_SNAPSHOT, "us-east-1")

    def test_a_missing_kms_key_is_an_error(self, aws, restore_context, fake_eon_client):
        orderable(aws, ["us-east-1a"])
        ctx = restore_context(eon_client=fake_eon_client(), kms_key_arns_by_region={})

        with pytest.raises(ValueError, match="No KMS key available"):
            _initiate_rds_restore(ctx, RDS_SNAPSHOT, "us-east-1")


S3_SNAPSHOT = {
    "resourceId": "res-s3",
    "resourceName": "my-bucket",
    "snapshotId": "snap-1",
    "snapshotPointInTime": "2026-09-08T02:00:00Z",
    "providerResourceId": "my-bucket",
    "originalTags": {"env": "prod"},
}


class TestS3Restore:
    def test_creates_a_uniquely_named_bucket_and_restores_into_it(
        self, aws, restore_context, fake_eon_client
    ):
        client = fake_eon_client(restore_s3_bucket="job-1")

        job_id, details = _restore_s3_new_bucket(
            restore_context(eon_client=client), S3_SNAPSHOT, "us-east-1", "arn:kms:key"
        )

        assert job_id == "job-1"
        assert details["originalBucketName"] == "my-bucket"
        assert details["restoredBucketName"].startswith("my-bucket-")
        aws["s3:us-east-1"].create_bucket.assert_called_once_with(
            Bucket=details["restoredBucketName"]
        )

    def test_the_bucket_name_is_deterministic_for_the_same_snapshot(
        self, aws, restore_context, fake_eon_client
    ):
        ctx = restore_context(eon_client=fake_eon_client(restore_s3_bucket="job-1"))

        first = _restore_s3_new_bucket(ctx, S3_SNAPSHOT, "us-east-1", "arn:kms:key")[1]
        second = _restore_s3_new_bucket(ctx, S3_SNAPSHOT, "us-east-1", "arn:kms:key")[1]

        assert first["restoredBucketName"] == second["restoredBucketName"]

    def test_a_different_region_yields_a_different_bucket_name(
        self, aws, restore_context, fake_eon_client
    ):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_s3_bucket="job-1"),
            kms_key_arns_by_region={"us-east-1": "arn:1", "eu-west-1": "arn:2"},
        )

        east = _restore_s3_new_bucket(ctx, S3_SNAPSHOT, "us-east-1", "arn:1")[1]
        west = _restore_s3_new_bucket(ctx, S3_SNAPSHOT, "eu-west-1", "arn:2")[1]

        assert east["restoredBucketName"] != west["restoredBucketName"]

    def test_the_name_prefix_is_applied(self, aws, restore_context, fake_eon_client):
        ctx = restore_context(
            eon_client=fake_eon_client(restore_s3_bucket="job-1"), resource_name_prefix="dr-"
        )

        _, details = _restore_s3_new_bucket(ctx, S3_SNAPSHOT, "us-east-1", "arn:kms:key")

        assert details["restoredBucketName"].startswith("dr-my-bucket-")

    def test_an_in_place_match_restores_into_the_stack_bucket(
        self, aws, restore_context, fake_eon_client
    ):
        client = fake_eon_client(restore_s3_bucket="job-2")
        stack_bucket = {
            "bucketName": "orders-data-prod",
            "region": "us-east-1",
            "stackName": "OrdersStack",
        }

        job_id, details = _restore_s3_in_place(
            restore_context(eon_client=client), S3_SNAPSHOT, stack_bucket, "orders-data"
        )

        assert job_id == "job-2"
        assert details["restoreType"] == "IN_PLACE"
        assert details["restoredBucketName"] == "orders-data-prod"
        assert details["eonFunctionalId"] == "orders-data"
        aws["s3:us-east-1"].create_bucket.assert_not_called()

    def test_in_place_without_a_kms_key_for_that_region_is_an_error(
        self, aws, restore_context, fake_eon_client
    ):
        ctx = restore_context(eon_client=fake_eon_client(), kms_key_arns_by_region={})
        stack_bucket = {"bucketName": "b", "region": "us-east-1", "stackName": "S"}

        with pytest.raises(ValueError, match="required for in-place S3 restore"):
            _restore_s3_in_place(ctx, S3_SNAPSHOT, stack_bucket, "fid")

    def test_dispatch_prefers_a_tag_matched_bucket(self, aws, restore_context, fake_eon_client):
        client = fake_eon_client(restore_s3_bucket="job-2")
        snapshot = dict(S3_SNAPSHOT, originalTags={"eon_functional_id": "orders-data"})
        ctx = restore_context(
            eon_client=client,
            recovery_stack_s3_buckets={
                "orders-data": {"bucketName": "b", "region": "us-east-1", "stackName": "S"}
            },
        )

        _, details = _initiate_s3_restore(ctx, snapshot, "us-east-1")

        assert details["restoreType"] == "IN_PLACE"

    def test_dispatch_honours_a_custom_tag_key(self, aws, restore_context, fake_eon_client):
        client = fake_eon_client(restore_s3_bucket="job-2")
        snapshot = dict(S3_SNAPSHOT, originalTags={"app_id": "orders-data"})
        ctx = restore_context(
            eon_client=client,
            s3_in_place_tag_key="app_id",
            recovery_stack_s3_buckets={
                "orders-data": {"bucketName": "b", "region": "us-east-1", "stackName": "S"}
            },
        )

        _, details = _initiate_s3_restore(ctx, snapshot, "us-east-1")

        assert details["restoreType"] == "IN_PLACE"

    def test_dispatch_creates_a_bucket_when_nothing_matches(
        self, aws, restore_context, fake_eon_client
    ):
        client = fake_eon_client(restore_s3_bucket="job-1")

        _, details = _initiate_s3_restore(
            restore_context(eon_client=client), S3_SNAPSHOT, "us-east-1"
        )

        assert "restoreType" not in details
        assert details["restoredBucketName"].startswith("my-bucket-")

    def test_stacks_only_mode_skips_an_unmatched_bucket(
        self, aws, restore_context, fake_eon_client, capsys
    ):
        ctx = restore_context(eon_client=fake_eon_client(), recovery_stacks_only=True)

        assert _initiate_s3_restore(ctx, S3_SNAPSHOT, "us-east-1") is None
        assert "recoveryStacksOnly mode, no matching stack bucket" in capsys.readouterr().out


class TestCreateS3Bucket:
    def test_us_east_1_takes_no_location_constraint(self, aws):
        create_s3_bucket("b", "us-east-1", "arn:key", "222222222222", "snap-1", "t", None, CREDS)

        aws["s3:us-east-1"].create_bucket.assert_called_once_with(Bucket="b")

    def test_other_regions_need_a_location_constraint(self, aws):
        create_s3_bucket("b", "eu-west-1", "arn:key", "222222222222", "snap-1", "t", None, CREDS)

        aws["s3:eu-west-1"].create_bucket.assert_called_once_with(
            Bucket="b", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
        )

    def test_default_encryption_uses_the_restore_key(self, aws):
        create_s3_bucket("b", "us-east-1", "arn:key", "222222222222", "snap-1", "t", None, CREDS)

        config = aws["s3:us-east-1"].put_bucket_encryption.call_args.kwargs[
            "ServerSideEncryptionConfiguration"
        ]["Rules"][0]
        assert config["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"] == "arn:key"
        assert config["BucketKeyEnabled"] is True

    def test_aws_reserved_tag_prefixes_are_filtered_out(self, aws):
        tags = {
            "env": "prod",
            "aws:cloudformation:stack-name": "S",
            "elasticbeanstalk:environment-name": "E",
        }

        create_s3_bucket("b", "us-east-1", "arn:key", "222222222222", "snap-1", "t", tags, CREDS)

        tag_set = aws["s3:us-east-1"].put_bucket_tagging.call_args.kwargs["Tagging"]["TagSet"]
        keys = {tag["Key"] for tag in tag_set}
        assert "env" in keys
        assert not any(key.lower().startswith(("aws:", "elasticbeanstalk:")) for key in keys)
        assert "eon:snapshot_id" in keys

    def test_an_existing_owned_bucket_is_reused(self, aws, capsys):
        aws["s3:us-east-1"].create_bucket.side_effect = client_error("BucketAlreadyOwnedByYou")

        create_s3_bucket("b", "us-east-1", "arn:key", "222222222222", "snap-1", "t", None, CREDS)

        assert "already exists" in capsys.readouterr().out

    def test_any_other_create_error_propagates(self, aws):
        aws["s3:us-east-1"].create_bucket.side_effect = client_error("BucketAlreadyExists")

        with pytest.raises(ClientError):
            create_s3_bucket("b", "us-east-1", "arn:key", "222222222222", "snap-1", "t", None, CREDS)


class TestGlobalTableReplicaRegions:
    def test_lists_the_stack_region_first_then_replicas(self, aws):
        aws["dynamodb:us-east-1"].describe_table.return_value = {
            "Table": {"Replicas": [{"RegionName": "eu-west-1"}, {"RegionName": "us-east-1"}]}
        }

        assert _global_table_replica_regions("orders", "us-east-1", CREDS) == [
            "us-east-1",
            "eu-west-1",
        ]

    def test_a_describe_failure_leaves_just_the_stack_region(self, aws, capsys):
        aws["dynamodb:us-east-1"].describe_table.side_effect = RuntimeError("denied")

        assert _global_table_replica_regions("orders", "us-east-1", CREDS) == ["us-east-1"]
        assert "Could not read replica regions" in capsys.readouterr().out

    def test_a_replica_without_a_region_name_is_ignored(self, aws):
        aws["dynamodb:us-east-1"].describe_table.return_value = {"Table": {"Replicas": [{}]}}

        assert _global_table_replica_regions("orders", "us-east-1", CREDS) == ["us-east-1"]


class TestDiscoverDynamoDBTablesFromStacks:
    def test_no_stacks_means_no_lookup(self, aws):
        assert discover_dynamodb_tables_from_stacks([], CREDS) == {}

    def test_finds_a_regular_table(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::DynamoDB::Table",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders",
                    "LogicalResourceId": "OrdersTable",
                }
            ],
        )

        tables = discover_dynamodb_tables_from_stacks(["OrdersStack"], CREDS, ["us-east-1"])

        assert tables["orders"]["region"] == "us-east-1"
        assert tables["orders"]["regions"] == ["us-east-1"]
        assert tables["orders"]["stackName"] == "OrdersStack"
        assert tables["orders"]["cfnResourceType"] == "AWS::DynamoDB::Table"

    def test_a_global_table_matches_all_its_replica_regions(self, aws):
        """CDK's TableV2 emits GlobalTable; matching only Table silently misses it."""
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::DynamoDB::GlobalTable",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders",
                    "LogicalResourceId": "OrdersTable",
                }
            ],
        )
        aws["dynamodb:us-east-1"].describe_table.return_value = {
            "Table": {"Replicas": [{"RegionName": "eu-west-1"}]}
        }

        tables = discover_dynamodb_tables_from_stacks(["OrdersStack"], CREDS, ["us-east-1"])

        assert tables["orders"]["regions"] == ["us-east-1", "eu-west-1"]
        assert tables["orders"]["region"] == "us-east-1"

    @pytest.mark.parametrize("status", ["CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"])
    def test_every_completed_status_counts(self, aws, status):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::DynamoDB::Table",
                    "ResourceStatus": status,
                    "PhysicalResourceId": "orders",
                }
            ],
        )

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS, ["us-east-1"])

    def test_in_progress_and_failed_resources_are_ignored(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::DynamoDB::Table",
                    "ResourceStatus": "CREATE_FAILED",
                    "PhysicalResourceId": "orders",
                },
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "a-bucket",
                },
            ],
        )

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS, ["us-east-1"]) == {}

    def test_a_resource_without_a_physical_id_is_skipped(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [{"ResourceType": "AWS::DynamoDB::Table", "ResourceStatus": "CREATE_COMPLETE"}],
        )

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS, ["us-east-1"]) == {}

    def test_a_missing_stack_is_reported_and_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].get_paginator.side_effect = client_error(
            "ValidationError", "Stack with id S does not exist"
        )

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "not found in region us-east-1" in capsys.readouterr().out

    def test_another_aws_error_is_reported_and_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].get_paginator.side_effect = client_error("AccessDenied")

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "Error checking CloudFormation stack" in capsys.readouterr().out

    def test_an_unexpected_error_is_reported_and_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].get_paginator.side_effect = RuntimeError("boom")

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "Unexpected error checking CloudFormation stack" in capsys.readouterr().out

    def test_the_default_region_is_us_east_1(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::DynamoDB::Table",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders",
                }
            ],
        )

        assert discover_dynamodb_tables_from_stacks(["S"], CREDS)["orders"]["region"] == "us-east-1"


class TestDiscoverS3BucketsFromStacks:
    def test_no_stacks_means_no_lookup(self, aws):
        assert discover_s3_buckets_from_stacks([], CREDS) == {}

    def test_indexes_buckets_by_the_matching_tag(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders-data-prod",
                    "LogicalResourceId": "OrdersBucket",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.return_value = {
            "TagSet": [{"Key": "eon_functional_id", "Value": "orders-data"}]
        }

        buckets = discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"])

        assert buckets["orders-data"]["bucketName"] == "orders-data-prod"
        assert buckets["orders-data"]["eonFunctionalId"] == "orders-data"

    def test_a_custom_tag_key_is_honoured(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders-data-prod",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.return_value = {
            "TagSet": [{"Key": "app_id", "Value": "orders"}]
        }

        buckets = discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"], "app_id")

        assert "orders" in buckets

    def test_an_untagged_bucket_is_skipped(self, aws, capsys):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders-data-prod",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.return_value = {"TagSet": [{"Key": "env", "Value": "p"}]}

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "has no 'eon_functional_id' tag" in capsys.readouterr().out

    def test_a_bucket_with_no_tag_set_is_skipped(self, aws, capsys):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders-data-prod",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.side_effect = client_error("NoSuchTagSet")

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "has no tags" in capsys.readouterr().out

    def test_a_tag_read_error_is_reported_and_skipped(self, aws, capsys):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders-data-prod",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.side_effect = client_error("AccessDenied")

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "Error fetching tags" in capsys.readouterr().out

    def test_an_unexpected_tag_error_is_reported_and_skipped(self, aws, capsys):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "orders-data-prod",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.side_effect = RuntimeError("boom")

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "Unexpected error fetching tags" in capsys.readouterr().out

    def test_a_duplicate_tag_value_warns_and_takes_the_last(self, aws, capsys):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "bucket-a",
                },
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "bucket-b",
                },
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.return_value = {
            "TagSet": [{"Key": "eon_functional_id", "Value": "shared"}]
        }

        buckets = discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"])

        assert buckets["shared"]["bucketName"] == "bucket-b"
        assert "Duplicate 'eon_functional_id' value" in capsys.readouterr().out

    def test_a_missing_stack_is_reported_and_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].get_paginator.side_effect = client_error(
            "ValidationError", "Stack with id S does not exist"
        )

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "not found in region us-east-1" in capsys.readouterr().out

    def test_another_aws_error_is_reported_and_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].get_paginator.side_effect = client_error("AccessDenied")

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "Error checking CloudFormation stack" in capsys.readouterr().out

    def test_an_unexpected_stack_error_is_reported_and_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].get_paginator.side_effect = RuntimeError("boom")

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}
        assert "Unexpected error checking CloudFormation stack" in capsys.readouterr().out

    def test_a_resource_without_a_physical_id_is_skipped(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [{"ResourceType": "AWS::S3::Bucket", "ResourceStatus": "CREATE_COMPLETE"}],
        )

        assert discover_s3_buckets_from_stacks(["S"], CREDS, ["us-east-1"]) == {}

    def test_the_default_region_is_us_east_1(self, aws):
        stack_resources(
            aws["cloudformation:us-east-1"],
            [
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "PhysicalResourceId": "b",
                }
            ],
        )
        aws["s3:us-east-1"].get_bucket_tagging.return_value = {
            "TagSet": [{"Key": "eon_functional_id", "Value": "fid"}]
        }

        assert discover_s3_buckets_from_stacks(["S"], CREDS)["fid"]["region"] == "us-east-1"
