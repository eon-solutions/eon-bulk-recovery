"""Connectivity defaults applied to vpcConfigs before they reach Eon."""

import pytest

from lib.vpc_configs import apply_connectivity_defaults, private_subnet_enabled


class TestPrivateSubnetEnabled:
    def test_defaults_on_when_absent(self):
        assert private_subnet_enabled({"region": "us-east-1", "vpc": "vpc-1"}) is True

    def test_null_means_the_default(self):
        # The README tells operators to write null for optional fields they do not set.
        assert private_subnet_enabled({"privateSubnetEnabled": None}) is True

    @pytest.mark.parametrize("value", [True, False])
    def test_an_explicit_boolean_is_kept(self, value):
        assert private_subnet_enabled({"privateSubnetEnabled": value}) is value

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, "yes"])
    def test_anything_else_is_an_input_error(self, value):
        # bool("false") is True; a typo here must not quietly choose a path.
        with pytest.raises(ValueError, match="privateSubnetEnabled must be true or false"):
            private_subnet_enabled(
                {"privateSubnetEnabled": value, "vpc": "vpc-1", "region": "us-east-1"}
            )

    def test_the_error_names_the_vpc(self):
        with pytest.raises(ValueError, match="vpc-1 in us-east-1"):
            private_subnet_enabled(
                {"privateSubnetEnabled": "no", "vpc": "vpc-1", "region": "us-east-1"}
            )


class TestApplyConnectivityDefaults:
    def test_makes_the_default_explicit_on_every_config(self):
        configs = [
            {"region": "us-east-1", "vpc": "vpc-1"},
            {"region": "us-west-2", "vpc": "vpc-2", "privateSubnetEnabled": False},
        ]

        effective = apply_connectivity_defaults(configs)

        assert [c["privateSubnetEnabled"] for c in effective] == [True, False]

    def test_keeps_everything_else(self):
        config = {
            "region": "us-east-1",
            "vpc": "vpc-1",
            "subnetsPerAvailabilityZone": [{"availabilityZone": "us-east-1a", "subnetId": "subnet-1"}],
            "securityGroups": {"restoreServer": ["sg-1"], "restoredRdsInstance": ["sg-2"]},
        }

        (effective,) = apply_connectivity_defaults([config])

        assert effective == {**config, "privateSubnetEnabled": True}

    def test_does_not_mutate_the_input(self):
        # Later steps receive the original vpcConfigs from the state machine, not these copies.
        configs = [{"region": "us-east-1", "vpc": "vpc-1"}]

        apply_connectivity_defaults(configs)

        assert configs == [{"region": "us-east-1", "vpc": "vpc-1"}]

    def test_an_empty_list_stays_empty(self):
        assert apply_connectivity_defaults([]) == []
