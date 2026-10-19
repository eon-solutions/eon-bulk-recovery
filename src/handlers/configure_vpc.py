"""Lambda handler for configuring VPC connectivity for the restore account."""

import os
import sys
from typing import Dict, Any, List

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials
from lib.vpc_configs import PRIVATE_SUBNET_KEY, apply_connectivity_defaults


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Configure VPC connectivity for a restore account in Eon.

    Input event:
        eonRestoreAccountId: Eon-assigned restore account ID
        vpcConfigs: List of VPC configurations
            Each config contains:
                region: AWS region
                vpc: VPC ID
                subnetsPerAvailabilityZone: List of {availabilityZone, subnetId}
                securityGroups: {restoreServer: [sg-id], restoredRdsInstance: [sg-id]}
                privateSubnetEnabled: optional; defaults to true, which makes Eon read a
                    vault in another region through an S3 Multi-Region Access Point

    Returns:
        status: Success or failure status
        eonRestoreAccountId: The restore account ID
        vpcConfigsApplied: How many VPCs were configured
        privateSubnetEnabledVpcs: The VPC IDs configured with privateSubnetEnabled=true
    """
    eon_restore_account_id = event["eonRestoreAccountId"]
    vpc_configs = event.get("vpcConfigs", [])

    if not vpc_configs:
        print("No VPC configs provided, skipping VPC configuration")
        return {
            "status": "SKIPPED",
            "eonRestoreAccountId": eon_restore_account_id,
            "message": "No VPC configuration provided"
        }

    # Make the connectivity defaults explicit before anything reaches Eon, so a bad
    # value fails here and the log shows exactly what each VPC was configured with.
    effective_configs = apply_connectivity_defaults(vpc_configs)

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # Configure VPC connectivity
    print(f"Configuring VPC connectivity for restore account: {eon_restore_account_id}")
    for config in effective_configs:
        path = (
            "S3 Multi-Region Access Point"
            if config[PRIVATE_SUBNET_KEY]
            else "the vault region's access point"
        )
        print(
            f"  {config.get('vpc')} in {config.get('region')}: "
            f"{PRIVATE_SUBNET_KEY}={str(config[PRIVATE_SUBNET_KEY]).lower()}, "
            f"cross-region vault reads via {path}"
        )
    print(f"VPC configs: {effective_configs}")

    response = eon_client.configure_vpc_connectivity(
        restore_account_id=eon_restore_account_id,
        vpc_configs=effective_configs
    )

    print(f"Successfully configured VPC connectivity")

    return {
        "status": "SUCCESS",
        "eonRestoreAccountId": eon_restore_account_id,
        "vpcConfigsApplied": len(effective_configs),
        "privateSubnetEnabledVpcs": [
            config.get("vpc") for config in effective_configs if config[PRIVATE_SUBNET_KEY]
        ],
    }
