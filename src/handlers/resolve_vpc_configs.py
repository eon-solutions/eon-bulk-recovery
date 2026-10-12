"""Lambda handler that turns VPC config names into resource IDs.

vpcConfigs is consumed as IDs by every later step: bootstrap passes subnet IDs
to CreateDBSubnetGroup, Configure VPC sends the block to Eon, and Initiate
Restores places instances and RDS into the listed subnets and security groups.
Callers can still pass IDs. A value that is not already an ID is treated as the
name stamped on the resource when the account was created and looked up in the
restore account.

A name is the resource's Name tag. A security group also matches its group
name (GroupName), which is what the EC2 console shows as the group's name.
The handler returns the vpcConfigs list itself so the state machine can write
it back over $.vpcConfigs.
"""

import os
import re
import sys
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.aws_utils import create_boto3_client, get_cross_account_credentials

_DESCRIBE_ACTIONS = "ec2:DescribeVpcs, ec2:DescribeSubnets, and ec2:DescribeSecurityGroups"

# AWS resource IDs are 8 hex characters (the original form) or 17 (the current
# form). Anything else — including a name that happens to start with "vpc-" —
# is a name to look up.
_VPC_ID = re.compile(r"^vpc-([0-9a-f]{8}|[0-9a-f]{17})$")
_SUBNET_ID = re.compile(r"^subnet-([0-9a-f]{8}|[0-9a-f]{17})$")
_SECURITY_GROUP_ID = re.compile(r"^sg-([0-9a-f]{8}|[0-9a-f]{17})$")

_ID_PATTERNS = {
    "vpc": _VPC_ID,
    "subnet": _SUBNET_ID,
    "sg": _SECURITY_GROUP_ID,
}


def _is_resource_id(value: Any, kind: str) -> bool:
    return isinstance(value, str) and _ID_PATTERNS[kind].match(value.strip()) is not None


def _require_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} is missing. Pass a resource ID or a name.")
    return value.strip()


def _name_tag(resource: Dict[str, Any]) -> Optional[str]:
    for tag in resource.get("Tags") or []:
        if tag.get("Key") == "Name":
            return tag.get("Value")
    return None


def _collect(
    client: Any, method_name: str, result_key: str, region: str, **kwargs: Any
) -> List[Dict[str, Any]]:
    """Follow NextToken so a name on a later page is still found.

    A missing describe permission becomes an error that names the role update,
    because that is the usual reason a name lookup fails on a least-privilege role.
    """
    method = getattr(client, method_name)
    items: List[Dict[str, Any]] = []
    token = None
    while True:
        request = dict(kwargs)
        if token:
            request["NextToken"] = token
        try:
            response = method(**request)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
                raise ValueError(
                    f"Not allowed to {method_name} in {region}. The restore account role "
                    f"needs {_DESCRIBE_ACTIONS} when vpcConfigs uses names. Redeploy "
                    f"cross-account-role.yaml or the StackSet, then re-run. AWS error: {error}"
                ) from error
            raise
        items.extend(response.get(result_key) or [])
        token = response.get("NextToken")
        if not token:
            return items


def _require_client(ec2: Any, region: str) -> Any:
    if ec2 is None:
        raise RuntimeError(f"No EC2 client for name lookup in {region}")
    return ec2


def _resolve_vpc(ec2: Any, region: str, value: Any) -> str:
    name = _require_name(value, f"vpc in {region}")
    if _is_resource_id(name, "vpc"):
        return name

    vpcs = _collect(
        _require_client(ec2, region),
        "describe_vpcs",
        "Vpcs",
        region,
        Filters=[{"Name": "tag:Name", "Values": [name]}],
    )
    if len(vpcs) == 1:
        vpc_id = vpcs[0]["VpcId"]
        print(f"Resolved VPC name '{name}' in {region} to {vpc_id}")
        return vpc_id
    if not vpcs:
        raise ValueError(
            f"No VPC with Name tag '{name}' in {region}. "
            f"vpc accepts a VPC ID (vpc-...) or the VPC's Name tag."
        )
    ids = ", ".join(found["VpcId"] for found in vpcs)
    raise ValueError(
        f"Multiple VPCs in {region} have Name tag '{name}': {ids}. Pass the VPC ID in vpc instead."
    )


def _resolve_subnet(ec2: Any, region: str, vpc_id: str, subnet: Dict[str, Any]) -> str:
    name = _require_name(subnet.get("subnetId"), f"subnetId in {region}")
    if _is_resource_id(name, "subnet"):
        return name

    availability_zone = subnet.get("availabilityZone")
    if not isinstance(availability_zone, str) or not availability_zone.strip():
        raise ValueError(
            f"Subnet '{name}' in {region} needs availabilityZone so the name can be matched "
            f"to one subnet. subnetId accepts a subnet ID (subnet-...) or the subnet's Name tag."
        )
    availability_zone = availability_zone.strip()

    subnets = _collect(
        _require_client(ec2, region),
        "describe_subnets",
        "Subnets",
        region,
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "tag:Name", "Values": [name]},
        ],
    )
    in_zone = [item for item in subnets if item.get("AvailabilityZone") == availability_zone]
    if len(in_zone) == 1:
        subnet_id = in_zone[0]["SubnetId"]
        print(f"Resolved subnet name '{name}' in {vpc_id} ({availability_zone}) to {subnet_id}")
        return subnet_id
    if len(in_zone) > 1:
        ids = ", ".join(item["SubnetId"] for item in in_zone)
        raise ValueError(
            f"Multiple subnets named '{name}' in {vpc_id} ({availability_zone}): {ids}. "
            f"Pass the subnet ID in subnetId instead."
        )
    if subnets:
        found = ", ".join(
            f"{item['SubnetId']} in {item.get('AvailabilityZone', 'an unknown AZ')}"
            for item in subnets
        )
        raise ValueError(
            f"No subnet named '{name}' in {availability_zone} (VPC {vpc_id}, {region}). "
            f"Found that name in other availability zones: {found}."
        )
    raise ValueError(
        f"No subnet with Name tag '{name}' in VPC {vpc_id} ({region}). "
        f"subnetId accepts a subnet ID (subnet-...) or the subnet's Name tag."
    )


def _security_group_matches(group: Dict[str, Any], name: str) -> bool:
    return group.get("GroupName") == name or _name_tag(group) == name


def _resolve_security_group(ec2: Any, region: str, vpc_id: str, value: Any) -> str:
    name = _require_name(value, f"security group in {region}")
    if _is_resource_id(name, "sg"):
        return name

    groups = _collect(
        _require_client(ec2, region),
        "describe_security_groups",
        "SecurityGroups",
        region,
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
    )
    matches = []
    seen = set()
    for group in groups:
        group_id = group.get("GroupId")
        if group_id in seen or not _security_group_matches(group, name):
            continue
        seen.add(group_id)
        matches.append(group)

    if len(matches) == 1:
        group_id = matches[0]["GroupId"]
        print(f"Resolved security group name '{name}' in {vpc_id} ({region}) to {group_id}")
        return group_id
    if not matches:
        raise ValueError(
            f"No security group named '{name}' in VPC {vpc_id} ({region}). "
            f"A name matches the group's GroupName or its Name tag, "
            f"or pass a security group ID (sg-...)."
        )
    details = ", ".join(
        f"{group['GroupId']} (group name {group.get('GroupName')})" for group in matches
    )
    raise ValueError(
        f"Multiple security groups in VPC {vpc_id} ({region}) match '{name}': {details}. "
        f"Pass the security group ID instead."
    )


def config_needs_resolution(config: Dict[str, Any]) -> bool:
    """True when any vpc, subnet, or security group value is a name rather than an ID."""
    if not _is_resource_id(config.get("vpc"), "vpc"):
        return True
    for subnet in config.get("subnetsPerAvailabilityZone") or []:
        if not _is_resource_id(subnet.get("subnetId"), "subnet"):
            return True
    for groups in (config.get("securityGroups") or {}).values():
        for group in groups:
            if not _is_resource_id(group, "sg"):
                return True
    return False


def _region_for(config: Dict[str, Any], restore_region: Optional[str]) -> str:
    region = config.get("region")
    if isinstance(region, str) and region.strip():
        return region.strip()
    if isinstance(restore_region, str) and restore_region.strip():
        return restore_region.strip()
    return "us-east-1"


def resolve_vpc_config(ec2: Any, config: Dict[str, Any], region: str) -> Dict[str, Any]:
    """Return a copy of one vpcConfigs entry with names replaced by IDs.

    ec2 is unused when every value is already an ID, and may be None in that case.
    """
    vpc_id = _resolve_vpc(ec2, region, config.get("vpc"))
    resolved = {**config, "vpc": vpc_id}

    if "subnetsPerAvailabilityZone" in config:
        resolved["subnetsPerAvailabilityZone"] = [
            {**subnet, "subnetId": _resolve_subnet(ec2, region, vpc_id, subnet)}
            for subnet in config.get("subnetsPerAvailabilityZone") or []
        ]

    security_groups = config.get("securityGroups")
    if security_groups:
        resolved["securityGroups"] = {
            purpose: [_resolve_security_group(ec2, region, vpc_id, group) for group in groups]
            for purpose, groups in security_groups.items()
        }
    return resolved


def handler(event: Dict[str, Any], context: Any) -> List[Dict[str, Any]]:
    """
    Resolve names in vpcConfigs to IDs in the restore account.

    Input event:
        restoreAccountId: AWS account ID of the restore account
        restoreRegion: Region used when a config omits its own (optional)
        vpcConfigs: List of VPC configurations. vpc, subnetId, and security
            group entries may be resource IDs or names.
        crossAccountRoleArn: Role to assume in the restore account (optional)

    Returns:
        The same list, with every name replaced by its resource ID. The state
        machine writes this over $.vpcConfigs. Configs that already use IDs are
        returned without calling EC2.
    """
    vpc_configs = event.get("vpcConfigs") or []
    if not vpc_configs:
        print("No VPC configs provided; nothing to resolve")
        return []

    restore_region = event.get("restoreRegion")
    regions = [_region_for(config, restore_region) for config in vpc_configs]

    if not any(config_needs_resolution(config) for config in vpc_configs):
        print("VPC configs already use resource IDs; nothing to resolve")
        return [
            resolve_vpc_config(None, config, region)
            for config, region in zip(vpc_configs, regions)
        ]

    print(f"Resolving VPC config names in restore account {event['restoreAccountId']}")
    management_account_id = os.environ.get("MANAGEMENT_ACCOUNT_ID", "").strip()
    credentials = get_cross_account_credentials(
        restore_account_id=event["restoreAccountId"],
        cross_account_role_arn=event.get("crossAccountRoleArn"),
        management_account_id=management_account_id or None,
    )

    clients: Dict[str, Any] = {}

    def ec2_for(region: str) -> Any:
        if region not in clients:
            clients[region] = create_boto3_client("ec2", region, credentials)
        return clients[region]

    resolved = []
    for config, region in zip(vpc_configs, regions):
        ec2 = ec2_for(region) if config_needs_resolution(config) else None
        resolved.append(resolve_vpc_config(ec2, config, region))
    return resolved
