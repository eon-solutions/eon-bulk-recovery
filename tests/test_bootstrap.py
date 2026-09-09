"""Restore-account bootstrap: IAM stack, RDS subnet groups, KMS keys."""

import json
from unittest.mock import MagicMock

import pytest
import requests
from botocore.exceptions import ClientError

from handlers import bootstrap

RESTORE_ACCOUNT = "222222222222"
ROLE_ARN = f"arn:aws:iam::{RESTORE_ACCOUNT}:role/EonRestoreAccountRole"
TEMPLATE_URL = "https://eon-public-b2b628cc-1d96-4fda-8dae-c3b1ad3ea03b.s3.amazonaws.com/restore-account.yml"

VPC_CONFIGS = [
    {
        "region": "us-east-1",
        "vpc": "vpc-1",
        "subnetsPerAvailabilityZone": [
            {"availabilityZone": "us-east-1a", "subnetId": "subnet-1a"},
            {"availabilityZone": "us-east-1b", "subnetId": "subnet-1b"},
        ],
    }
]


def client_error(code: str, operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.fixture
def aws(monkeypatch, sts_credentials):
    """
    Route every boto3.client call in bootstrap to an addressable MagicMock, and
    stub the cross-account credential fetch and the template download.
    """
    class Clients(dict):
        """Clients are addressable before the handler creates them."""

        def __missing__(self, key):
            client = MagicMock(name=key)
            self[key] = client
            return client

    clients = Clients()

    def factory(service, **kwargs):
        key = f"{service}:{kwargs.get('region_name')}" if kwargs.get("region_name") else service
        client = clients[key]
        client.call_kwargs = kwargs
        return client

    monkeypatch.setattr(bootstrap.boto3, "client", factory)
    monkeypatch.setattr(
        bootstrap, "get_cross_account_credentials", lambda **kwargs: dict(sts_credentials)
    )

    template = MagicMock()
    template.text = "AWSTemplateFormatVersion: '2010-09-09'"
    template.raise_for_status.return_value = None
    monkeypatch.setattr(bootstrap.requests, "get", lambda url: template)

    clients["_template_response"] = template
    return clients


def kms_not_found(clients, region="us-east-1"):
    clients[f"kms:{region}"].describe_key.side_effect = client_error("NotFoundException", "DescribeKey")
    clients[f"kms:{region}"].create_key.return_value = {
        "KeyMetadata": {"Arn": f"arn:aws:kms:{region}:{RESTORE_ACCOUNT}:key/new", "KeyId": "new"}
    }


class TestEnsureRdsServiceLinkedRole:
    def test_creates_the_role(self, aws, sts_credentials, capsys):
        bootstrap.ensure_rds_service_linked_role(sts_credentials)

        aws["iam"].create_service_linked_role.assert_called_once_with(
            AWSServiceName="rds.amazonaws.com"
        )
        assert "Created AWSServiceRoleForRDS" in capsys.readouterr().out

    @pytest.mark.parametrize("code", ["InvalidInput", "EntityAlreadyExists"])
    def test_an_existing_role_is_fine(self, aws, sts_credentials, capsys, code):
        aws["iam"].create_service_linked_role.side_effect = client_error(code)

        bootstrap.ensure_rds_service_linked_role(sts_credentials)

        assert "already exists" in capsys.readouterr().out

    def test_any_other_failure_is_non_fatal(self, aws, sts_credentials, capsys):
        aws["iam"].create_service_linked_role.side_effect = client_error("AccessDenied")

        bootstrap.ensure_rds_service_linked_role(sts_credentials)

        assert "WARNING: Could not create AWSServiceRoleForRDS" in capsys.readouterr().out


class TestExtractRoleArn:
    def test_prefers_the_stack_output(self):
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "EonRestoreAccountRoleArn", "OutputValue": "arn:from-output"}]}]
        }

        assert bootstrap._extract_role_arn(cfn, "stack", RESTORE_ACCOUNT) == "arn:from-output"

    def test_falls_back_to_the_conventional_name(self):
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [{"OutputKey": "Other", "OutputValue": "x"}]}]}

        assert bootstrap._extract_role_arn(cfn, "stack", RESTORE_ACCOUNT) == ROLE_ARN

    def test_falls_back_when_there_are_no_outputs(self):
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{}]}

        assert bootstrap._extract_role_arn(cfn, "stack", RESTORE_ACCOUNT) == ROLE_ARN


class TestCreateRestoreStack:
    def test_creates_waits_and_returns_the_arn(self):
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}

        arn = bootstrap._create_restore_stack(cfn, "stack", "body", "eon-account", RESTORE_ACCOUNT)

        create = cfn.create_stack.call_args.kwargs
        assert create["StackName"] == "stack"
        assert create["TemplateBody"] == "body"
        assert create["Parameters"] == [{"ParameterKey": "EonAccountId", "ParameterValue": "eon-account"}]
        assert create["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
        assert {"Key": "ManagedBy", "Value": "EonBulkRecovery"} in create["Tags"]
        cfn.get_waiter.assert_called_once_with("stack_create_complete")
        cfn.get_waiter.return_value.wait.assert_called_once()
        assert arn == ROLE_ARN


class TestRestoreRoleExists:
    def test_present(self):
        iam = MagicMock()

        assert bootstrap._restore_role_exists(iam, ROLE_ARN) is True
        iam.get_role.assert_called_once_with(RoleName="EonRestoreAccountRole")

    def test_strips_a_role_path(self):
        iam = MagicMock()

        bootstrap._restore_role_exists(iam, f"arn:aws:iam::{RESTORE_ACCOUNT}:role/eon/EonRestoreAccountRole")

        assert iam.get_role.call_args.kwargs["RoleName"] == "EonRestoreAccountRole"

    @pytest.mark.parametrize("code", ["NoSuchEntity", "NoSuchEntityException"])
    def test_absent(self, code):
        iam = MagicMock()
        iam.get_role.side_effect = client_error(code, "GetRole")

        assert bootstrap._restore_role_exists(iam, ROLE_ARN) is False

    def test_an_ambiguous_error_never_triggers_a_rebuild(self, capsys):
        iam = MagicMock()
        iam.get_role.side_effect = client_error("AccessDenied", "GetRole")

        assert bootstrap._restore_role_exists(iam, ROLE_ARN) is True
        assert "assuming it exists" in capsys.readouterr().out


class TestHandlerStack:
    def test_creates_the_stack_and_a_kms_key(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        result = bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        assert result["roleArn"] == ROLE_ARN
        assert result["restoreAccountId"] == RESTORE_ACCOUNT
        assert result["restoreRegion"] == "us-east-1"
        assert result["rdsSubnetGroupsByRegion"] == {}
        assert result["kmsKeyArnsByRegion"] == {
            "us-east-1": f"arn:aws:kms:us-east-1:{RESTORE_ACCOUNT}:key/new"
        }

    def test_reuses_an_existing_stack_when_the_role_is_present(self, aws, capsys):
        cfn = aws["cloudformation:us-east-1"]
        cfn.create_stack.side_effect = client_error("AlreadyExistsException", "CreateStack")
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        result = bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        assert result["roleArn"] == ROLE_ARN
        assert "reusing existing stack" in capsys.readouterr().out

    def test_an_existing_stack_with_a_missing_role_is_a_clear_failure(self, aws):
        cfn = aws["cloudformation:us-east-1"]
        cfn.create_stack.side_effect = client_error("AlreadyExistsException", "CreateStack")
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        aws["iam"].get_role.side_effect = client_error("NoSuchEntity", "GetRole")

        with pytest.raises(ValueError) as excinfo:
            bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        message = str(excinfo.value)
        assert "EonRestoreAccountRole" in message
        assert f"Delete stack 'eon-restore-account-{RESTORE_ACCOUNT}'" in message

    def test_any_other_create_error_propagates(self, aws):
        aws["cloudformation:us-east-1"].create_stack.side_effect = client_error(
            "InsufficientCapabilities", "CreateStack"
        )

        with pytest.raises(ClientError):
            bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

    def test_a_template_download_failure_stops_the_step(self, aws):
        aws["_template_response"].raise_for_status.side_effect = requests.HTTPError("403")

        with pytest.raises(requests.HTTPError):
            bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

    def test_the_management_account_id_is_passed_to_credential_resolution(
        self, aws, monkeypatch, sts_credentials
    ):
        monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", " 444444444444 ")
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return dict(sts_credentials)

        monkeypatch.setattr(bootstrap, "get_cross_account_credentials", capture)
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT, "crossAccountRoleArn": "arn:custom"}, None)

        assert seen["management_account_id"] == "444444444444"
        assert seen["cross_account_role_arn"] == "arn:custom"

    def test_a_blank_management_account_id_is_treated_as_absent(
        self, aws, monkeypatch, sts_credentials
    ):
        monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", "   ")
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return dict(sts_credentials)

        monkeypatch.setattr(bootstrap, "get_cross_account_credentials", capture)
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        assert seen["management_account_id"] is None


class TestHandlerRdsSubnetGroups:
    def test_creates_one_per_region(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        result = bootstrap.handler(
            {"restoreAccountId": RESTORE_ACCOUNT, "vpcConfigs": VPC_CONFIGS}, None
        )

        assert result["rdsSubnetGroupsByRegion"] == {
            "us-east-1": f"eon-restore-{RESTORE_ACCOUNT}-us-east-1"
        }
        create = aws["rds:us-east-1"].create_db_subnet_group.call_args.kwargs
        assert create["SubnetIds"] == ["subnet-1a", "subnet-1b"]
        aws["iam"].create_service_linked_role.assert_called_once()

    def test_an_existing_subnet_group_is_reused(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        aws["rds:us-east-1"].create_db_subnet_group.side_effect = client_error(
            "DBSubnetGroupAlreadyExists", "CreateDBSubnetGroup"
        )
        kms_not_found(aws)

        result = bootstrap.handler(
            {"restoreAccountId": RESTORE_ACCOUNT, "vpcConfigs": VPC_CONFIGS}, None
        )

        assert result["rdsSubnetGroupsByRegion"]["us-east-1"].endswith("us-east-1")

    def test_any_other_subnet_group_error_stops_the_step(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        aws["rds:us-east-1"].create_db_subnet_group.side_effect = client_error(
            "InvalidSubnet", "CreateDBSubnetGroup"
        )

        with pytest.raises(ClientError):
            bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT, "vpcConfigs": VPC_CONFIGS}, None)

    def test_a_region_without_subnets_is_skipped(self, aws, capsys):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)
        configs = [{"region": "us-east-1", "vpc": "vpc-1", "subnetsPerAvailabilityZone": []}]

        result = bootstrap.handler(
            {"restoreAccountId": RESTORE_ACCOUNT, "vpcConfigs": configs}, None
        )

        assert result["rdsSubnetGroupsByRegion"] == {}
        assert "No subnets found for region us-east-1" in capsys.readouterr().out

    def test_no_vpc_configs_means_no_service_linked_role_call(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        aws["iam"].create_service_linked_role.assert_not_called()


class TestHandlerKmsKeys:
    def test_reuses_an_enabled_key(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        aws["kms:us-east-1"].describe_key.return_value = {
            "KeyMetadata": {"Arn": "arn:existing", "KeyId": "existing", "KeyState": "Enabled"}
        }

        result = bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        assert result["kmsKeyArnsByRegion"] == {"us-east-1": "arn:existing"}
        aws["kms:us-east-1"].create_key.assert_not_called()

    def test_replaces_a_key_that_is_not_enabled(self, aws, capsys):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms = aws["kms:us-east-1"]
        kms.describe_key.return_value = {
            "KeyMetadata": {"Arn": "arn:old", "KeyId": "old", "KeyState": "PendingDeletion"}
        }
        kms.create_key.return_value = {"KeyMetadata": {"Arn": "arn:new", "KeyId": "new"}}

        result = bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        assert result["kmsKeyArnsByRegion"] == {"us-east-1": "arn:new"}
        kms.update_alias.assert_called_once_with(
            AliasName=f"alias/eon-restore-{RESTORE_ACCOUNT}-us-east-1", TargetKeyId="new"
        )
        kms.create_alias.assert_not_called()

    def test_creates_a_key_and_alias_when_none_exists(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        kms = aws["kms:us-east-1"]
        kms.create_alias.assert_called_once()
        kms.update_alias.assert_not_called()

    def test_the_key_policy_keeps_the_account_in_control(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws)

        bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

        create = aws["kms:us-east-1"].create_key.call_args.kwargs
        policy = json.loads(create["Policy"])
        root = policy["Statement"][0]
        assert root["Principal"]["AWS"] == f"arn:aws:iam::{RESTORE_ACCOUNT}:root"
        assert root["Action"] == "kms:*"
        # A least-privilege cross-account role has no kms:PutKeyPolicy, so the
        # lockout check has to be bypassed or CreateKey fails outright.
        assert create["BypassPolicyLockoutSafetyCheck"] is True
        assert create["MultiRegion"] is False

    def test_the_service_statement_is_scoped_to_the_region(self, aws):
        aws["cloudformation:eu-west-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws, "eu-west-1")

        bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT, "restoreRegion": "eu-west-1"}, None)

        policy = json.loads(aws["kms:eu-west-1"].create_key.call_args.kwargs["Policy"])
        via_service = policy["Statement"][1]["Condition"]["StringEquals"]["kms:ViaService"]
        assert via_service == [
            "dynamodb.eu-west-1.amazonaws.com",
            "rds.eu-west-1.amazonaws.com",
            "ec2.eu-west-1.amazonaws.com",
            "s3.eu-west-1.amazonaws.com",
        ]

    def test_one_key_per_vpc_config_region(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms_not_found(aws, "us-east-1")
        kms_not_found(aws, "us-west-2")
        configs = VPC_CONFIGS + [
            {
                "region": "us-west-2",
                "vpc": "vpc-2",
                "subnetsPerAvailabilityZone": [{"subnetId": "subnet-2a"}],
            }
        ]

        result = bootstrap.handler(
            {"restoreAccountId": RESTORE_ACCOUNT, "vpcConfigs": configs}, None
        )

        assert set(result["kmsKeyArnsByRegion"]) == {"us-east-1", "us-west-2"}

    def test_an_unexpected_describe_error_propagates(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        aws["kms:us-east-1"].describe_key.side_effect = client_error("AccessDenied", "DescribeKey")

        with pytest.raises(ClientError):
            bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)

    def test_a_create_key_failure_propagates(self, aws):
        aws["cloudformation:us-east-1"].describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        kms = aws["kms:us-east-1"]
        kms.describe_key.side_effect = client_error("NotFoundException", "DescribeKey")
        kms.create_key.side_effect = client_error("LimitExceeded", "CreateKey")

        with pytest.raises(ClientError):
            bootstrap.handler({"restoreAccountId": RESTORE_ACCOUNT}, None)
