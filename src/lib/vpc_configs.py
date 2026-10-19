"""The per-VPC connectivity configuration the workflow sends to Eon.

Each ``vpcConfigs`` entry in the execution input goes to Eon's restore-account
connectivity configuration as written, with one default applied here:
``privateSubnetEnabled`` is on unless the input turns it off.

When it is on and a restore's target region differs from the vault's region,
Eon reads the vault through an S3 Multi-Region Access Point instead of the
vault region's regional access point. A restore server in a private subnet can
then reach the vault over a ``com.amazonaws.s3-global.accesspoint`` interface
endpoint instead of its NAT gateway, and a subnet with no internet route can
restore cross-region at all. Same-region restores are unaffected either way.
"""

from typing import Any, Dict, List

PRIVATE_SUBNET_KEY = "privateSubnetEnabled"


def private_subnet_enabled(vpc_config: Dict[str, Any]) -> bool:
    """The effective ``privateSubnetEnabled`` for one VPC config.

    Absent or ``null`` means the default, which is on. Only an explicit boolean
    ``false`` turns it off. Anything else is an input error: this flag decides
    whether cross-region restore traffic bills as NAT gateway processing, so a
    typo has to fail the run rather than quietly pick a path.
    """
    value = vpc_config.get(PRIVATE_SUBNET_KEY)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"{PRIVATE_SUBNET_KEY} must be true or false, got {value!r} "
        f"for VPC {vpc_config.get('vpc')} in {vpc_config.get('region')}"
    )


def apply_connectivity_defaults(vpc_configs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of ``vpc_configs`` with ``privateSubnetEnabled`` made explicit.

    The input is left untouched: later steps receive the original ``vpcConfigs``
    from the state machine, not these copies.
    """
    return [{**config, PRIVATE_SUBNET_KEY: private_subnet_enabled(config)} for config in vpc_configs]
