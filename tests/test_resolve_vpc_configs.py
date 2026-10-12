"""Resolving VPC, subnet, and security group names to IDs."""

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from handlers import resolve_vpc_configs


ROOT = pathlib.Path(__file__).resolve().parents[1]

VPC = "vpc-0123456789abcdef0"
LEGACY_VPC = "vpc-01234567"
SUBNET_A = "subnet-0123456789abcdef0"
SUBNET_B = "subnet-11111111111111111"
SUBNET_C = "subnet-22222222222222222"
SUBNET_D = "subnet-33333333333333333"
SG_APP = "sg-0123456789abcdef0"
SG_DATA = "sg-11111111111111111"
ACCOUNT = "222222222222"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/EonBulkRecoveryCrossAccountRole"


def _vpc(vpc_id=VPC, name="primary"):
    return {"VpcId": vpc_id, "Tags": [{"Key": "Name", "Value": name}]}


def _subnet(subnet_id, az, name):
    return {
        "SubnetId": subnet_id,
        "AvailabilityZone": az,
        "Tags": [{"Key": "Name", "Value": name}],
    }


def _page(key, items, token=None):
    response = {key: items}
    if token:
        response["NextToken"] = token
    return response


def named_config():
    """The account-vending shape: names in the same fields that otherwise hold IDs."""
    return {
        "region": "us-east-1",
        "vpc": "primary",
        "subnetsPerAvailabilityZone": [
            {"availabilityZone": "us-east-1a", "subnetId": "data-1"},
            {"availabilityZone": "us-east-1b", "subnetId": "data-2"},
            {"availabilityZone": "us-east-1c", "subnetId": "data-3"},
            {"availabilityZone": "us-east-1d", "subnetId": "data-4"},
        ],
        "securityGroups": {
            "restoreServer": ["primary-app-sg"],
            "restoredRdsInstance": ["primary-data-sg"],
        },
        "note": "kept",
    }


def id_config(**overrides):
    config = {
        "region": "us-east-1",
        "vpc": VPC,
        "subnetsPerAvailabilityZone": [
            {"availabilityZone": "us-east-1a", "subnetId": SUBNET_A},
        ],
        "securityGroups": {
            "restoreServer": [SG_APP],
            "restoredRdsInstance": [SG_DATA],
        },
    }
    config.update(overrides)
    return config


def account_ec2():
    """EC2 client whose describes answer the account-default names."""
    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = _page("Vpcs", [_vpc()])
    ec2.describe_subnets.side_effect = lambda **kwargs: _page(
        "Subnets",
        [
            _subnet(subnet_id, az, name)
            for name, (az, subnet_id) in {
                "data-1": ("us-east-1a", SUBNET_A),
                "data-2": ("us-east-1b", SUBNET_B),
                "data-3": ("us-east-1c", SUBNET_C),
                "data-4": ("us-east-1d", SUBNET_D),
            }.items()
            if name == _filter(kwargs, "tag:Name")
        ],
    )
    ec2.describe_security_groups.return_value = _page(
        "SecurityGroups",
        [
            {"GroupId": SG_APP, "GroupName": "primary-app-sg"},
            {
                "GroupId": SG_DATA,
                "GroupName": "data-sg",
                "Tags": [
                    {"Key": "Environment", "Value": "prod"},
                    {"Key": "Name", "Value": "primary-data-sg"},
                ],
            },
        ],
    )
    return ec2


def _filter(kwargs, name):
    for item in kwargs["Filters"]:
        if item["Name"] == name:
            return item["Values"][0]
    raise AssertionError(f"no filter named {name}")


@pytest.fixture
def aws(monkeypatch, sts_credentials):
    """Stub the cross-account credential fetch and hand out one EC2 client per region."""
    clients = {}
    created = []
    credential_calls = []

    def factory(service, region, credentials=None):
        client = clients.setdefault(region, MagicMock(name=f"ec2:{region}"))
        created.append((service, region, credentials))
        return client

    def credentials(**kwargs):
        credential_calls.append(kwargs)
        return dict(sts_credentials)

    monkeypatch.setattr(resolve_vpc_configs, "create_boto3_client", factory)
    monkeypatch.setattr(resolve_vpc_configs, "get_cross_account_credentials", credentials)
    return clients, created, credential_calls


class TestNamesResolveToIds:
    def test_account_default_names_become_ids(self):
        original = named_config()
        resolved = resolve_vpc_configs.resolve_vpc_config(account_ec2(), original, "us-east-1")

        assert resolved["vpc"] == VPC
        assert resolved["subnetsPerAvailabilityZone"] == [
            {"availabilityZone": "us-east-1a", "subnetId": SUBNET_A},
            {"availabilityZone": "us-east-1b", "subnetId": SUBNET_B},
            {"availabilityZone": "us-east-1c", "subnetId": SUBNET_C},
            {"availabilityZone": "us-east-1d", "subnetId": SUBNET_D},
        ]
        assert resolved["securityGroups"] == {
            "restoreServer": [SG_APP],
            "restoredRdsInstance": [SG_DATA],
        }
        assert resolved["note"] == "kept"
        assert original["vpc"] == "primary"
        assert original["subnetsPerAvailabilityZone"][0]["subnetId"] == "data-1"

    def test_ids_pass_through_without_calling_ec2(self):
        ec2 = MagicMock()
        config = id_config(vpc=f"  {LEGACY_VPC}  ", extra=True)
        config["subnetsPerAvailabilityZone"][0]["subnetId"] = f"  {SUBNET_A}  "
        config["securityGroups"]["restoreServer"] = [f"  {SG_APP}  "]

        resolved = resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

        assert resolved["vpc"] == LEGACY_VPC
        assert resolved["subnetsPerAvailabilityZone"][0]["subnetId"] == SUBNET_A
        assert resolved["securityGroups"]["restoreServer"] == [SG_APP]
        assert resolved["extra"] is True
        ec2.describe_vpcs.assert_not_called()
        ec2.describe_subnets.assert_not_called()
        ec2.describe_security_groups.assert_not_called()

    def test_a_name_that_only_looks_like_an_id_is_looked_up(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = _page("Vpcs", [_vpc(name="vpc-primary")])

        resolved = resolve_vpc_configs.resolve_vpc_config(
            ec2, {"vpc": "vpc-primary"}, "us-east-1"
        )

        assert resolved["vpc"] == VPC
        assert _filter(ec2.describe_vpcs.call_args.kwargs, "tag:Name") == "vpc-primary"

    def test_subnet_lookup_is_scoped_to_the_vpc_and_zone(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = _page("Vpcs", [_vpc()])
        ec2.describe_subnets.return_value = _page(
            "Subnets",
            [
                _subnet(SUBNET_B, "us-east-1b", "data"),
                _subnet(SUBNET_A, "us-east-1a", "data"),
            ],
        )
        config = {
            "vpc": "primary",
            "subnetsPerAvailabilityZone": [
                {"availabilityZone": "  us-east-1a  ", "subnetId": " data "},
            ],
        }

        resolved = resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

        assert resolved["subnetsPerAvailabilityZone"][0]["subnetId"] == SUBNET_A
        assert resolved["subnetsPerAvailabilityZone"][0]["availabilityZone"] == "  us-east-1a  "
        filters = ec2.describe_subnets.call_args.kwargs["Filters"]
        assert {"Name": "vpc-id", "Values": [VPC]} in filters
        assert {"Name": "tag:Name", "Values": ["data"]} in filters

    def test_security_group_listed_twice_across_pages_is_one_match(self):
        group = {"GroupId": SG_APP, "GroupName": "primary-app-sg"}
        ec2 = MagicMock()
        ec2.describe_security_groups.side_effect = [
            _page("SecurityGroups", [group], token="page-2"),
            _page("SecurityGroups", [group]),
        ]

        assert resolve_vpc_configs._resolve_security_group(ec2, "us-east-1", VPC, "primary-app-sg") == SG_APP
        assert "NextToken" not in ec2.describe_security_groups.call_args_list[0].kwargs
        assert ec2.describe_security_groups.call_args_list[1].kwargs["NextToken"] == "page-2"

    def test_a_vpc_on_a_later_page_is_found(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.side_effect = [
            _page("Vpcs", [], token="page-2"),
            _page("Vpcs", [_vpc()]),
        ]

        assert resolve_vpc_configs._resolve_vpc(ec2, "us-east-1", "primary") == VPC

    def test_configs_without_subnets_or_security_groups_only_resolve_the_vpc(self):
        ec2 = account_ec2()

        resolved = resolve_vpc_configs.resolve_vpc_config(ec2, {"vpc": "primary"}, "us-east-1")

        assert resolved == {"vpc": VPC}
        ec2.describe_subnets.assert_not_called()
        ec2.describe_security_groups.assert_not_called()

    def test_an_empty_subnet_list_stays_empty(self):
        resolved = resolve_vpc_configs.resolve_vpc_config(
            None, {"vpc": VPC, "subnetsPerAvailabilityZone": None, "securityGroups": {}}, "us-east-1"
        )

        assert resolved["subnetsPerAvailabilityZone"] == []
        assert resolved["securityGroups"] == {}

    def test_a_mix_of_ids_and_names_resolves_only_the_names(self):
        ec2 = account_ec2()
        config = id_config()
        config["securityGroups"] = {"restoreServer": [SG_APP, "primary-app-sg"]}

        resolved = resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

        assert resolved["securityGroups"]["restoreServer"] == [SG_APP, SG_APP]
        ec2.describe_vpcs.assert_not_called()
        assert ec2.describe_security_groups.call_args.kwargs["Filters"] == [
            {"Name": "vpc-id", "Values": [VPC]}
        ]


class TestResolutionFailures:
    def test_missing_or_blank_values_are_rejected(self):
        with pytest.raises(ValueError, match="vpc in us-east-1 is missing"):
            resolve_vpc_configs.resolve_vpc_config(None, {"vpc": None}, "us-east-1")
        with pytest.raises(ValueError, match="vpc in us-east-1 is missing"):
            resolve_vpc_configs.resolve_vpc_config(None, {"vpc": "  "}, "us-east-1")
        with pytest.raises(ValueError, match="subnetId in us-east-1 is missing"):
            resolve_vpc_configs.resolve_vpc_config(
                None,
                {"vpc": VPC, "subnetsPerAvailabilityZone": [{"availabilityZone": "us-east-1a"}]},
                "us-east-1",
            )
        with pytest.raises(ValueError, match="security group in us-east-1 is missing"):
            resolve_vpc_configs.resolve_vpc_config(
                None,
                {"vpc": VPC, "securityGroups": {"restoreServer": [""]}},
                "us-east-1",
            )

    def test_a_subnet_name_without_an_availability_zone_is_rejected(self):
        ec2 = MagicMock()
        config = {"vpc": VPC, "subnetsPerAvailabilityZone": [{"subnetId": "data-1"}]}

        with pytest.raises(ValueError, match="needs availabilityZone"):
            resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

        config["subnetsPerAvailabilityZone"][0]["availabilityZone"] = "   "
        with pytest.raises(ValueError, match="needs availabilityZone"):
            resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")
        ec2.describe_subnets.assert_not_called()

    def test_no_matching_vpc(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {}

        with pytest.raises(ValueError, match="No VPC with Name tag 'primary' in us-east-1"):
            resolve_vpc_configs.resolve_vpc_config(ec2, {"vpc": "primary"}, "us-east-1")

    def test_ambiguous_vpc(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = _page("Vpcs", [_vpc(VPC), _vpc(LEGACY_VPC)])

        with pytest.raises(ValueError, match=f"{VPC}, {LEGACY_VPC}"):
            resolve_vpc_configs.resolve_vpc_config(ec2, {"vpc": "primary"}, "us-east-1")

    def test_no_matching_subnet(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = {"Subnets": []}
        config = id_config()
        config["subnetsPerAvailabilityZone"] = [
            {"availabilityZone": "us-east-1a", "subnetId": "data-1"}
        ]

        with pytest.raises(ValueError, match="No subnet with Name tag 'data-1'"):
            resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

    def test_subnet_name_found_only_in_another_zone(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = _page(
            "Subnets",
            [
                _subnet(SUBNET_B, "us-east-1b", "data-1"),
                {"SubnetId": SUBNET_C, "Tags": [{"Key": "Name", "Value": "data-1"}]},
            ],
        )
        config = id_config()
        config["subnetsPerAvailabilityZone"] = [
            {"availabilityZone": "us-east-1a", "subnetId": "data-1"}
        ]

        with pytest.raises(ValueError, match=f"{SUBNET_B} in us-east-1b, {SUBNET_C} in an unknown AZ"):
            resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

    def test_ambiguous_subnet_in_the_requested_zone(self):
        ec2 = MagicMock()
        ec2.describe_subnets.return_value = _page(
            "Subnets",
            [_subnet(SUBNET_A, "us-east-1a", "data-1"), _subnet(SUBNET_B, "us-east-1a", "data-1")],
        )
        config = id_config()
        config["subnetsPerAvailabilityZone"] = [
            {"availabilityZone": "us-east-1a", "subnetId": "data-1"}
        ]

        with pytest.raises(ValueError, match=f"{SUBNET_A}, {SUBNET_B}"):
            resolve_vpc_configs.resolve_vpc_config(ec2, config, "us-east-1")

    def test_no_matching_security_group(self):
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = _page(
            "SecurityGroups",
            [{"GroupId": SG_APP, "GroupName": "default", "Tags": [{"Key": "Environment", "Value": "prod"}]}],
        )

        with pytest.raises(ValueError, match="No security group named 'primary-app-sg'"):
            resolve_vpc_configs._resolve_security_group(ec2, "us-east-1", VPC, "primary-app-sg")

    def test_ambiguous_security_group(self):
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = _page(
            "SecurityGroups",
            [
                {"GroupId": SG_APP, "GroupName": "primary-app-sg"},
                {"GroupId": SG_DATA, "GroupName": "other", "Tags": [{"Key": "Name", "Value": "primary-app-sg"}]},
            ],
        )

        with pytest.raises(ValueError, match=f"{SG_APP}.*{SG_DATA}"):
            resolve_vpc_configs._resolve_security_group(ec2, "us-east-1", VPC, "primary-app-sg")

    def test_a_name_lookup_without_a_client_fails_clearly(self):
        with pytest.raises(RuntimeError, match="No EC2 client for name lookup in us-east-1"):
            resolve_vpc_configs.resolve_vpc_config(None, {"vpc": "primary"}, "us-east-1")

    def test_a_missing_describe_permission_names_the_role_update(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}}, "DescribeVpcs"
        )

        with pytest.raises(ValueError, match="ec2:DescribeVpcs") as raised:
            resolve_vpc_configs.resolve_vpc_config(ec2, {"vpc": "primary"}, "us-east-1")
        assert "cross-account-role.yaml" in str(raised.value)

    def test_other_ec2_errors_propagate(self):
        ec2 = MagicMock()
        ec2.describe_vpcs.side_effect = ClientError(
            {"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}}, "DescribeVpcs"
        )

        with pytest.raises(ClientError):
            resolve_vpc_configs.resolve_vpc_config(ec2, {"vpc": "primary"}, "us-east-1")


class TestHandler:
    def test_no_configs_skips_the_lookup(self, aws):
        clients, created, credential_calls = aws

        assert resolve_vpc_configs.handler({}, None) == []
        assert resolve_vpc_configs.handler({"vpcConfigs": None}, None) == []
        assert created == []
        assert credential_calls == []
        assert clients == {}

    def test_id_configs_do_not_assume_a_role(self, aws):
        _, created, credential_calls = aws
        event = {
            "restoreAccountId": ACCOUNT,
            "crossAccountRoleArn": ROLE,
            "vpcConfigs": [id_config()],
        }

        assert resolve_vpc_configs.handler(event, None) == [id_config()]
        assert created == []
        assert credential_calls == []

    def test_names_are_resolved_with_the_restore_account_credentials(self, aws, monkeypatch, sts_credentials):
        clients, created, credential_calls = aws
        clients["us-east-1"] = account_ec2()
        monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", "  444444444444  ")
        event = {
            "restoreAccountId": ACCOUNT,
            "restoreRegion": "eu-west-1",
            "crossAccountRoleArn": ROLE,
            "vpcConfigs": [named_config(), id_config(region="eu-west-1")],
        }

        resolved = resolve_vpc_configs.handler(event, None)

        assert resolved[0]["vpc"] == VPC
        assert resolved[0]["subnetsPerAvailabilityZone"][3]["subnetId"] == SUBNET_D
        assert resolved[1] == id_config(region="eu-west-1")
        assert credential_calls == [{
            "restore_account_id": ACCOUNT,
            "cross_account_role_arn": ROLE,
            "management_account_id": "444444444444",
        }]
        assert created == [("ec2", "us-east-1", sts_credentials)]

    def test_a_blank_management_account_id_is_omitted(self, aws, monkeypatch):
        clients, _, credential_calls = aws
        clients["us-west-2"] = account_ec2()
        monkeypatch.setenv("MANAGEMENT_ACCOUNT_ID", "   ")

        resolve_vpc_configs.handler(
            {
                "restoreAccountId": ACCOUNT,
                "vpcConfigs": [dict(named_config(), region="us-west-2")],
            },
            None,
        )

        assert credential_calls[0]["cross_account_role_arn"] is None
        assert credential_calls[0]["management_account_id"] is None

    def test_one_client_per_region_and_the_config_region_wins(self, aws):
        clients, created, _ = aws
        for region in ("us-east-1", "us-west-2"):
            clients[region] = account_ec2()
        first = named_config()
        second = named_config()
        second["region"] = "  us-west-2  "

        resolved = resolve_vpc_configs.handler(
            {
                "restoreAccountId": ACCOUNT,
                "restoreRegion": "eu-west-1",
                "vpcConfigs": [first, second, named_config()],
            },
            None,
        )

        assert [config["region"].strip() for config in resolved] == [
            "us-east-1",
            "us-west-2",
            "us-east-1",
        ]
        assert [call[1] for call in created] == ["us-east-1", "us-west-2"]

    def test_a_config_without_a_region_uses_the_restore_region(self, aws):
        clients, created, _ = aws
        clients["eu-west-1"] = account_ec2()
        config = named_config()
        del config["region"]

        resolved = resolve_vpc_configs.handler(
            {"restoreAccountId": ACCOUNT, "restoreRegion": " eu-west-1 ", "vpcConfigs": [config]},
            None,
        )

        assert resolved[0]["vpc"] == VPC
        assert "region" not in resolved[0]
        assert created[0][1] == "eu-west-1"

    def test_without_any_region_the_lookup_defaults_to_us_east_1(self, aws):
        clients, created, _ = aws
        clients["us-east-1"] = account_ec2()
        config = named_config()
        config["region"] = "   "

        resolve_vpc_configs.handler(
            {"restoreAccountId": ACCOUNT, "restoreRegion": None, "vpcConfigs": [config]},
            None,
        )

        assert created[0][1] == "us-east-1"

    def test_a_blank_restore_region_also_defaults_to_us_east_1(self, aws):
        clients, created, _ = aws
        clients["us-east-1"] = account_ec2()
        config = named_config()
        del config["region"]

        resolve_vpc_configs.handler(
            {"restoreAccountId": ACCOUNT, "restoreRegion": "  ", "vpcConfigs": [config]},
            None,
        )

        assert created[0][1] == "us-east-1"


class TestNeedsResolution:
    def test_ids_do_not_need_a_lookup(self):
        assert resolve_vpc_configs.config_needs_resolution(id_config()) is False
        assert resolve_vpc_configs.config_needs_resolution({"vpc": f" {VPC} "}) is False

    def test_any_name_needs_a_lookup(self):
        assert resolve_vpc_configs.config_needs_resolution({"vpc": "primary"}) is True
        subnet_name = id_config()
        subnet_name["subnetsPerAvailabilityZone"][0]["subnetId"] = "data-1"
        assert resolve_vpc_configs.config_needs_resolution(subnet_name) is True
        group_name = id_config()
        group_name["securityGroups"]["restoreServer"] = [SG_APP, "primary-app-sg"]
        assert resolve_vpc_configs.config_needs_resolution(group_name) is True

    def test_nine_hex_characters_is_not_an_id(self):
        assert resolve_vpc_configs._is_resource_id("vpc-" + "a" * 9, "vpc") is False
        assert resolve_vpc_configs._is_resource_id("vpc-" + "a" * 8, "vpc") is True
        assert resolve_vpc_configs._is_resource_id("subnet-" + "b" * 17, "subnet") is True
        assert resolve_vpc_configs._is_resource_id(None, "sg") is False


class TestWorkflowContract:
    def test_the_state_machine_resolves_names_before_bootstrap(self):
        asl = json.loads((ROOT / "statemachine.asl.json").read_text())

        assert asl["States"]["Normalize Input"]["Next"] == "Resolve VPC Configs"
        resolve = asl["States"]["Resolve VPC Configs"]
        assert resolve["Parameters"]["step"] == "resolve_vpc_configs"
        assert resolve["ResultPath"] == "$.vpcConfigs"
        assert resolve["Next"] == "Bootstrap Restore Account"
        assert asl["States"]["Bootstrap Restore Account"]["Parameters"]["vpcConfigs.$"] == "$.vpcConfigs"
        assert asl["States"]["Resolve VPC Configs Failed"]["Error"] == "ResolveVpcConfigsFailed"

        template = (ROOT / "template.yaml").read_text()
        assert "ResolveVpcConfigsFunctionArn:" in template

    def test_both_role_templates_can_describe_network_resources(self):
        for name in ("cross-account-role.yaml", "cross-account-role-stackset.yaml"):
            text = (ROOT / name).read_text()
            assert "Sid: VpcNameResolution" in text
            for action in (
                "ec2:DescribeVpcs",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
            ):
                assert action in text
