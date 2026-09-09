"""VPC connectivity configuration step."""

from handlers import configure_vpc


VPC_CONFIGS = [
    {
        "region": "us-east-1",
        "vpc": "vpc-1",
        "subnetsPerAvailabilityZone": [{"availabilityZone": "us-east-1a", "subnetId": "subnet-1"}],
        "securityGroups": {"restoreServer": ["sg-1"], "restoredRdsInstance": ["sg-1"]},
    }
]


def test_applies_the_configs(eon_credentials, fake_eon_client, patch_eon_client):
    client = patch_eon_client(
        "handlers.configure_vpc", fake_eon_client(configure_vpc_connectivity={"ok": True})
    )

    result = configure_vpc.handler(
        {"eonRestoreAccountId": "eon-acct", "vpcConfigs": VPC_CONFIGS}, None
    )

    assert result == {
        "status": "SUCCESS",
        "eonRestoreAccountId": "eon-acct",
        "vpcConfigsApplied": 1,
    }
    call = client.calls_to("configure_vpc_connectivity")[0]
    assert call["restore_account_id"] == "eon-acct"
    assert call["vpc_configs"] == VPC_CONFIGS


def test_no_configs_skips_without_calling_eon(eon_credentials, fake_eon_client, patch_eon_client):
    client = patch_eon_client("handlers.configure_vpc", fake_eon_client())

    result = configure_vpc.handler({"eonRestoreAccountId": "eon-acct", "vpcConfigs": []}, None)

    assert result["status"] == "SKIPPED"
    assert result["message"] == "No VPC configuration provided"
    assert client.calls == []


def test_absent_configs_key_also_skips(eon_credentials, fake_eon_client, patch_eon_client):
    patch_eon_client("handlers.configure_vpc", fake_eon_client())

    assert configure_vpc.handler({"eonRestoreAccountId": "eon-acct"}, None)["status"] == "SKIPPED"
