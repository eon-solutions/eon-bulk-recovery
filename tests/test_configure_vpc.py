"""VPC connectivity configuration step."""

import pytest

from handlers import configure_vpc


VPC_CONFIGS = [
    {
        "region": "us-east-1",
        "vpc": "vpc-1",
        "subnetsPerAvailabilityZone": [{"availabilityZone": "us-east-1a", "subnetId": "subnet-1"}],
        "securityGroups": {"restoreServer": ["sg-1"], "restoredRdsInstance": ["sg-1"]},
    }
]


def configured_client(fake_eon_client, patch_eon_client):
    return patch_eon_client(
        "handlers.configure_vpc", fake_eon_client(configure_vpc_connectivity={"ok": True})
    )


def test_applies_the_configs(eon_credentials, fake_eon_client, patch_eon_client):
    client = configured_client(fake_eon_client, patch_eon_client)

    result = configure_vpc.handler(
        {"eonRestoreAccountId": "eon-acct", "vpcConfigs": VPC_CONFIGS}, None
    )

    assert result == {
        "status": "SUCCESS",
        "eonRestoreAccountId": "eon-acct",
        "vpcConfigsApplied": 1,
        "privateSubnetEnabledVpcs": ["vpc-1"],
    }
    call = client.calls_to("configure_vpc_connectivity")[0]
    assert call["restore_account_id"] == "eon-acct"
    assert call["vpc_configs"] == [{**VPC_CONFIGS[0], "privateSubnetEnabled": True}]


def test_private_subnet_defaults_on_so_cross_region_reads_use_an_access_point(
    eon_credentials, fake_eon_client, patch_eon_client
):
    # Eon's own default for privateSubnetEnabled is false, and it only routes a
    # cross-region restore through a Multi-Region Access Point when the target
    # region's VPC config says true. The workflow has to say it explicitly.
    client = configured_client(fake_eon_client, patch_eon_client)

    configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": VPC_CONFIGS}, None)

    sent = client.calls_to("configure_vpc_connectivity")[0]["vpc_configs"]
    assert sent[0]["privateSubnetEnabled"] is True


def test_an_explicit_false_is_sent_as_is(eon_credentials, fake_eon_client, patch_eon_client):
    client = configured_client(fake_eon_client, patch_eon_client)
    configs = [{**VPC_CONFIGS[0], "privateSubnetEnabled": False}]

    result = configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": configs}, None)

    sent = client.calls_to("configure_vpc_connectivity")[0]["vpc_configs"]
    assert sent[0]["privateSubnetEnabled"] is False
    assert result["privateSubnetEnabledVpcs"] == []


def test_the_default_is_decided_per_vpc(eon_credentials, fake_eon_client, patch_eon_client):
    client = configured_client(fake_eon_client, patch_eon_client)
    configs = [
        VPC_CONFIGS[0],
        {"region": "us-west-2", "vpc": "vpc-2", "privateSubnetEnabled": False},
        {"region": "eu-west-1", "vpc": "vpc-3", "privateSubnetEnabled": None},
    ]

    result = configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": configs}, None)

    sent = client.calls_to("configure_vpc_connectivity")[0]["vpc_configs"]
    assert [c["privateSubnetEnabled"] for c in sent] == [True, False, True]
    assert result["vpcConfigsApplied"] == 3
    assert result["privateSubnetEnabledVpcs"] == ["vpc-1", "vpc-3"]


def test_the_input_is_not_mutated(eon_credentials, fake_eon_client, patch_eon_client):
    # The state machine hands the original vpcConfigs to later steps.
    configured_client(fake_eon_client, patch_eon_client)
    configs = [dict(VPC_CONFIGS[0])]

    configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": configs}, None)

    assert "privateSubnetEnabled" not in configs[0]


def test_a_non_boolean_flag_fails_before_eon_is_called(
    eon_credentials, fake_eon_client, patch_eon_client
):
    client = configured_client(fake_eon_client, patch_eon_client)
    configs = [{**VPC_CONFIGS[0], "privateSubnetEnabled": "false"}]

    with pytest.raises(ValueError, match="privateSubnetEnabled must be true or false"):
        configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": configs}, None)

    assert client.calls == []


def test_the_log_names_the_path_each_vpc_takes(
    eon_credentials, fake_eon_client, patch_eon_client, capsys
):
    configured_client(fake_eon_client, patch_eon_client)
    configs = [VPC_CONFIGS[0], {"region": "us-west-2", "vpc": "vpc-2", "privateSubnetEnabled": False}]

    configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": configs}, None)

    out = capsys.readouterr().out
    assert (
        "vpc-1 in us-east-1: privateSubnetEnabled=true, "
        "cross-region vault reads via S3 Multi-Region Access Point"
    ) in out
    assert (
        "vpc-2 in us-west-2: privateSubnetEnabled=false, "
        "cross-region vault reads via the vault region's access point"
    ) in out


def test_no_configs_skips_without_calling_eon(eon_credentials, fake_eon_client, patch_eon_client):
    client = patch_eon_client("handlers.configure_vpc", fake_eon_client())

    result = configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": []}, None)

    assert result["status"] == "SKIPPED"
    assert result["message"] == "No VPC configuration provided"
    assert client.calls == []


def test_absent_configs_key_also_skips(eon_credentials, fake_eon_client, patch_eon_client):
    patch_eon_client("handlers.configure_vpc", fake_eon_client())

    assert configure_vpc.handler({"eonRestoreAccountId": "eon-acct"}, None)["status"] == "SKIPPED"
