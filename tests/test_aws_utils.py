"""Credential and cross-account plumbing."""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from lib import aws_utils

from conftest import SECRET_ARN


def client_error(code: str, operation: str = "AssumeRole") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class TestCreateBoto3Client:
    def test_without_credentials_uses_the_ambient_role(self):
        with patch.object(aws_utils.boto3, "client") as boto_client:
            aws_utils.create_boto3_client("s3", "eu-west-1")

        boto_client.assert_called_once_with("s3", region_name="eu-west-1")

    def test_with_credentials_passes_the_session(self, sts_credentials):
        with patch.object(aws_utils.boto3, "client") as boto_client:
            aws_utils.create_boto3_client("dynamodb", "us-east-1", sts_credentials)

        boto_client.assert_called_once_with(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="ASIAEXAMPLE",
            aws_secret_access_key="secret",
            aws_session_token="token",
        )


class TestGetEonCredentials:
    def test_reads_and_parses_the_secret(self):
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"clientId": "id", "clientSecret": "shh"})
        }

        with patch.object(aws_utils.boto3, "client", return_value=secrets):
            assert aws_utils.get_eon_credentials() == {"clientId": "id", "clientSecret": "shh"}

        secrets.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)

    def test_missing_env_var_is_an_error(self, monkeypatch):
        monkeypatch.delenv("EON_CREDENTIALS_SECRET_ARN")
        with pytest.raises(KeyError):
            aws_utils.get_eon_credentials()


class TestAssumeRole:
    def test_returns_a_session_on_the_assumed_credentials(self, sts_credentials):
        sts = MagicMock()
        sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", return_value=sts), \
                patch.object(aws_utils.boto3, "Session") as session:
            aws_utils.assume_role("arn:aws:iam::222222222222:role/Target", "SessionName")

        sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::222222222222:role/Target", RoleSessionName="SessionName"
        )
        session.assert_called_once_with(
            aws_access_key_id="ASIAEXAMPLE",
            aws_secret_access_key="secret",
            aws_session_token="token",
        )

    def test_default_session_name(self, sts_credentials):
        sts = MagicMock()
        sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", return_value=sts), \
                patch.object(aws_utils.boto3, "Session"):
            aws_utils.assume_role("arn:aws:iam::222222222222:role/Target")

        assert sts.assume_role.call_args.kwargs["RoleSessionName"] == "EonBulkRecovery"


def test_get_account_id():
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111111111111"}

    with patch.object(aws_utils.boto3, "client", return_value=sts):
        assert aws_utils.get_account_id() == "111111111111"


class TestGetCrossAccountCredentials:
    def test_explicit_role_arn_wins(self, sts_credentials):
        sts = MagicMock()
        sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", return_value=sts):
            got = aws_utils.get_cross_account_credentials(
                "222222222222", cross_account_role_arn="arn:aws:iam::222222222222:role/Custom"
            )

        assert got == sts_credentials
        assert sts.assume_role.call_args.kwargs["RoleArn"] == "arn:aws:iam::222222222222:role/Custom"

    def test_explicit_role_failure_is_reported_with_the_arn(self):
        sts = MagicMock()
        sts.assume_role.side_effect = client_error("AccessDenied")

        with patch.object(aws_utils.boto3, "client", return_value=sts):
            with pytest.raises(ValueError, match="Failed to assume provided role arn:aws:iam::222222222222:role/Custom"):
                aws_utils.get_cross_account_credentials(
                    "222222222222", cross_account_role_arn="arn:aws:iam::222222222222:role/Custom"
                )

    def test_role_chaining_prefers_control_tower(self, sts_credentials):
        mgmt_sts = MagicMock()
        mgmt_sts.assume_role.return_value = {"Credentials": {"AccessKeyId": "chained"}}
        root_sts = MagicMock()
        root_sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", side_effect=[root_sts, mgmt_sts]):
            got = aws_utils.get_cross_account_credentials(
                "222222222222", management_account_id="444444444444"
            )

        assert got == {"AccessKeyId": "chained"}
        assert root_sts.assume_role.call_args.kwargs["RoleArn"] == (
            "arn:aws:iam::444444444444:role/EonBulkRecoveryChainRole"
        )
        assert mgmt_sts.assume_role.call_args.kwargs["RoleArn"] == (
            "arn:aws:iam::222222222222:role/AWSControlTowerExecution"
        )

    def test_role_chaining_falls_back_to_the_organizations_role(self, sts_credentials):
        mgmt_sts = MagicMock()
        mgmt_sts.assume_role.side_effect = [
            client_error("AccessDenied"),
            {"Credentials": {"AccessKeyId": "org"}},
        ]
        root_sts = MagicMock()
        root_sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", side_effect=[root_sts, mgmt_sts]):
            got = aws_utils.get_cross_account_credentials(
                "222222222222", management_account_id="444444444444"
            )

        assert got == {"AccessKeyId": "org"}
        assert mgmt_sts.assume_role.call_args_list[1].kwargs["RoleArn"] == (
            "arn:aws:iam::222222222222:role/OrganizationAccountAccessRole"
        )

    def test_role_chaining_reports_both_failures(self, sts_credentials):
        mgmt_sts = MagicMock()
        mgmt_sts.assume_role.side_effect = [
            client_error("ControlTowerMissing"),
            client_error("OrgRoleMissing"),
        ]
        root_sts = MagicMock()
        root_sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", side_effect=[root_sts, mgmt_sts]):
            with pytest.raises(ValueError) as excinfo:
                aws_utils.get_cross_account_credentials(
                    "222222222222", management_account_id="444444444444"
                )

        assert "ControlTowerMissing" in str(excinfo.value)
        assert "OrgRoleMissing" in str(excinfo.value)

    def test_chain_role_failure_names_the_management_account(self):
        sts = MagicMock()
        sts.assume_role.side_effect = client_error("AccessDenied")

        with patch.object(aws_utils.boto3, "client", return_value=sts):
            with pytest.raises(ValueError, match="EonBulkRecoveryChainRole in management account 444444444444"):
                aws_utils.get_cross_account_credentials(
                    "222222222222", management_account_id="444444444444"
                )

    def test_direct_organizations_access_is_the_last_resort(self, sts_credentials):
        sts = MagicMock()
        sts.assume_role.return_value = {"Credentials": sts_credentials}

        with patch.object(aws_utils.boto3, "client", return_value=sts):
            got = aws_utils.get_cross_account_credentials("222222222222")

        assert got == sts_credentials
        assert sts.assume_role.call_args.kwargs["RoleArn"] == (
            "arn:aws:iam::222222222222:role/OrganizationAccountAccessRole"
        )

    @pytest.mark.parametrize("code", ["AccessDenied", "NoSuchEntity"])
    def test_direct_access_denial_explains_the_three_options(self, code):
        sts = MagicMock()
        sts.assume_role.side_effect = client_error(code)

        with patch.object(aws_utils.boto3, "client", return_value=sts):
            with pytest.raises(ValueError) as excinfo:
                aws_utils.get_cross_account_credentials("222222222222")

        message = str(excinfo.value)
        assert "Organization Management Account" in message
        assert "ManagementAccountId" in message
        assert "crossAccountRoleArn" in message

    def test_an_unexpected_client_error_propagates(self):
        sts = MagicMock()
        sts.assume_role.side_effect = client_error("Throttling")

        with patch.object(aws_utils.boto3, "client", return_value=sts):
            with pytest.raises(ClientError):
                aws_utils.get_cross_account_credentials("222222222222")
