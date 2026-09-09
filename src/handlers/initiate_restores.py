"""Lambda handler for initiating restore jobs for all snapshots."""

import os
import sys
import hashlib
import re
import time
import random
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
import boto3
from botocore.exceptions import ClientError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials, get_cross_account_credentials, create_boto3_client


# CloudFormation resource types that represent a DynamoDB table in a recovery
# stack. GlobalTable is what CDK's TableV2 construct emits; it restores exactly
# like a regular table.
DYNAMODB_STACK_RESOURCE_TYPES = ("AWS::DynamoDB::Table", "AWS::DynamoDB::GlobalTable")


# DynamoDB restore methods, as the Eon API names them.
RESTORE_METHOD_IMPORT_TABLE = "RESTORE_METHOD_IMPORT_TABLE"
RESTORE_METHOD_CAPACITY_BASED = "RESTORE_METHOD_CAPACITY_BASED"

# Accepted values for the dynamodbRestoreMethod execution input.
DYNAMODB_RESTORE_METHOD_AUTO = "auto"
DYNAMODB_RESTORE_METHOD_IMPORT = "import"
DYNAMODB_RESTORE_METHOD_CAPACITY = "capacity"
DYNAMODB_RESTORE_METHOD_CHOICES = (
    DYNAMODB_RESTORE_METHOD_AUTO,
    DYNAMODB_RESTORE_METHOD_IMPORT,
    DYNAMODB_RESTORE_METHOD_CAPACITY,
)

# ImportTable needs restore-account role 1.8.1 or newer (it grants the
# dynamodb:ImportTable and S3 access-point permissions the import path uses).
MIN_ROLE_VERSION_FOR_IMPORT = (1, 8, 1)

# Reason codes the availability endpoint returns, in readable form.
_IMPORT_UNAVAILABLE_REASONS = {
    "RESTORE_METHOD_HAS_LSI": "table has local secondary indexes, which ImportTable cannot create",
    "RESTORE_METHOD_SIZE_LIMIT": "table is larger than the AWS import limit for this region "
                                 "(15 TiB in us-east-1/us-west-1/us-west-2, 1 TiB elsewhere)",
    "RESTORE_METHOD_NOT_SPECIFIED": "no restore method specified",
}


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def eon_engine_to_aws(eon_engine: Optional[str]) -> Optional[str]:
    """
    Map Eon's AwsRdsEngine enum value to the AWS RDS API engine name
    (e.g. ``POSTGRES`` → ``postgres``, ``AURORA_MYSQL`` → ``aurora-mysql``).
    Returns None when the input is empty or UNSPECIFIED.
    """
    if not eon_engine or eon_engine.upper() == "UNSPECIFIED":
        return None
    return eon_engine.lower().replace("_", "-")


def validate_rds_instance_class_available(
    rds_client,
    eon_engine: Optional[str],
    db_instance_class: str,
    configured_azs: List[str],
    resource_name: str,
) -> None:
    """
    Mirror of the EC2 instance-type availability check for RDS.

    Calls ``describe_orderable_db_instance_options`` for the source engine and
    class, then raises if no configured AZ offers the class. The intent is to
    fail loudly before submitting a restore that AWS would later reject, since
    the script's top priority is to restore using the source's instance class.

    Soft-skips with a warning when engine or class is missing, or when the AWS
    call itself errors (e.g. permission denied). A soft-skip means the existing
    server-side behavior takes over instead of pre-failing on our side.
    """
    if not db_instance_class:
        print(f"WARNING: No source DB instance class available for {resource_name} — skipping availability check")
        return

    aws_engine = eon_engine_to_aws(eon_engine)
    if not aws_engine:
        print(f"WARNING: No engine available for {resource_name} — skipping instance class availability check for {db_instance_class}")
        return

    print(f"Checking availability of RDS instance class {db_instance_class} for engine {aws_engine}")
    try:
        offering_azs: set = set()
        paginator = rds_client.get_paginator("describe_orderable_db_instance_options")
        for page in paginator.paginate(Engine=aws_engine, DBInstanceClass=db_instance_class):
            for option in page.get("OrderableDBInstanceOptions", []):
                for az in option.get("AvailabilityZones", []):
                    name = az.get("Name")
                    if name:
                        offering_azs.add(name)
    except ClientError as e:
        print(f"WARNING: Could not check RDS instance class availability: {e}")
        return

    if not offering_azs:
        raise ValueError(
            f"RDS instance class {db_instance_class} is not orderable for engine {aws_engine} "
            f"in the target region (restoring {resource_name}). "
            f"Re-run with a different snapshotDate or update the source resource's instance class."
        )

    overlap = offering_azs.intersection(configured_azs)
    if not overlap:
        raise ValueError(
            f"RDS instance class {db_instance_class} (engine {aws_engine}) is not available "
            f"in any configured AZ for {resource_name}. "
            f"Class available in: {sorted(offering_azs)}, "
            f"Configured AZs: {sorted(configured_azs)}"
        )
    print(f"RDS instance class {db_instance_class} is available in configured AZs: {sorted(overlap)}")


def resolve_target_region(
    target_region: Optional[str],
    region_configs: Dict[str, Any],
    resource_name: str,
    config_label: str = "VPC config",
    error_label: str = "VPC configurations",
) -> str:
    """Pick the best region from *region_configs*, warning on fallback."""
    if target_region and target_region in region_configs:
        return target_region
    if region_configs:
        fallback = list(region_configs.keys())[0]
        print(f"WARNING: No {config_label} for target region {target_region}, falling back to: {fallback}")
        return fallback
    raise ValueError(f"No {error_label} available for restoring {resource_name}")


def require_kms_key(kms_key_arns_by_region: Dict[str, str], region: str) -> str:
    """Return the KMS key ARN for *region* or raise."""
    kms_key_arn = kms_key_arns_by_region.get(region)
    if not kms_key_arn:
        raise ValueError(f"No KMS key available for region {region}")
    return kms_key_arn


def get_restored_name(original_name: str, prefix: Optional[str] = None) -> str:
    """Apply the optional resource-name prefix."""
    if prefix:
        return f"{prefix}{original_name}"
    return original_name


def sanitize_s3_bucket_name(base_name: str, hash_suffix: str) -> str:
    """
    Build a globally-unique S3 bucket name from a base name and hash suffix.

    S3 bucket name constraints:
    - 3-63 lowercase alphanumeric characters, hyphens, or dots
    - Must begin and end with a letter or number
    - No consecutive periods (we also avoid consecutive hyphens for clarity)

    The hash_suffix is always preserved so the name stays globally unique.
    When the combined name exceeds 63 chars, the base is truncated to make
    room for ``-<hash_suffix>``.
    """
    max_len = 63
    suffix_with_sep = f"-{hash_suffix}"  # e.g. "-a1b2c3d4" (9 chars)

    combined = f"{base_name}{suffix_with_sep}".lower()

    if len(combined) <= max_len:
        name = combined
    else:
        # Truncate the base to leave room for the suffix
        allowed_base = max_len - len(suffix_with_sep)
        truncated = base_name[:allowed_base].rstrip("-")
        name = f"{truncated}{suffix_with_sep}".lower()

    # Collapse consecutive hyphens and strip leading/trailing hyphens
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    return name


def sanitize_rds_identifier(name: str) -> str:
    """
    Sanitize a name to comply with RDS DB instance/cluster identifier constraints:
    - 1-63 lowercase alphanumeric characters or hyphens
    - Must begin with a letter
    - Can't end with a hyphen
    - Can't contain two consecutive hyphens

    When truncation is needed, appends a short hash of the original name
    to prevent collisions between names that share the same prefix.
    """
    original = name
    # Lowercase and replace invalid characters with hyphens
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    # Collapse consecutive hyphens
    name = re.sub(r"-{2,}", "-", name)
    # Strip leading/trailing hyphens
    name = name.strip("-")
    # Ensure it starts with a letter
    if name and not name[0].isalpha():
        name = "r" + name
    # If empty after sanitization, generate from hash
    if not name:
        name = "r" + hashlib.sha256(original.encode()).hexdigest()[:8]
    # Truncate to 63 chars, using a hash suffix to avoid collisions
    max_len = 63
    if len(name) > max_len:
        suffix = hashlib.sha256(original.encode()).hexdigest()[:6]
        # Leave room for hyphen + 6-char hash suffix
        truncated = name[:max_len - 7].rstrip("-")
        name = f"{truncated}-{suffix}"
    # Final safety: strip any trailing hyphen (shouldn't happen but just in case)
    name = name.rstrip("-")
    return name


# ---------------------------------------------------------------------------
# DynamoDB restore method selection
# ---------------------------------------------------------------------------

def parse_role_version(version: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """
    Parse an Eon restore-role version ("1.8.1", "1.9.0-rc.1") into a comparable
    tuple. A prerelease sorts below its release, matching how Eon compares the
    version server-side, so "1.8.1-rc.1" does not satisfy a 1.8.1 minimum.
    Returns None when the version is missing or unparseable.
    """
    if not version:
        return None

    core = version.strip().lstrip("v").split("+", 1)[0]
    core, _, prerelease = core.partition("-")

    parts = core.split(".")
    if len(parts) != 3:
        return None

    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        return None

    if prerelease:
        # Sort below the release it precedes.
        if patch > 0:
            patch -= 1
        elif minor > 0:
            minor, patch = minor - 1, 999
        elif major > 0:
            major, minor, patch = major - 1, 999, 999
        else:
            return (0, 0, 0)

    return (major, minor, patch)


def get_restore_role_version(
    eon_client: EonClient,
    eon_restore_account_id: str,
    restore_account_id: str,
) -> Optional[str]:
    """
    Read the restore role version Eon has installed on the restore account.

    The version lives at ``version.installed`` on a RestoreAccount. (A flat
    ``installedVersion`` appears on the separate Account schema, so accept that
    too rather than silently reading None and disabling ImportTable.)
    """
    try:
        response = eon_client.list_restore_accounts(provider_account_id=restore_account_id)
    except Exception as e:
        print(f"WARNING: Could not list restore accounts to read the role version: {e}")
        return None

    for account in response.get("accounts", []):
        if account.get("id") == eon_restore_account_id:
            version = (account.get("version") or {}).get("installed")
            return version or account.get("installedVersion")

    print(f"WARNING: Restore account {eon_restore_account_id} not found in the account listing")
    return None


def plan_dynamodb_restore_methods(
    eon_client: EonClient,
    dynamodb_candidates: List[Dict[str, Any]],
    requested_method: str,
    eon_restore_account_id: str,
    restore_account_id: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Decide, per table, whether to restore via the AWS ImportTable (import-from-S3)
    path or the capacity-based write path.

    ImportTable rebuilds the table from DynamoDB JSON staged in S3, so it does not
    consume the restore account's write capacity and is not bounded by the regional
    WCU quota. It cannot create local secondary indexes, is capped at the AWS import
    size limit for the region, needs restore-role 1.8.1 or newer, and only applies
    to new-table restores — the existing-table (in-place) API has no method option.

    Args:
        eon_client: Authenticated Eon client
        dynamodb_candidates: New-table DynamoDB restores, each with resourceId,
            resourceName, restoredName and region (the region it is restored into)
        requested_method: One of DYNAMODB_RESTORE_METHOD_CHOICES
        eon_restore_account_id: Eon-assigned restore account ID
        restore_account_id: AWS account ID of the restore account

    Returns:
        Dictionary mapping resource_id to {"method": ..., "reasons": [...]}
    """
    plan: Dict[str, Dict[str, Any]] = {}

    if not dynamodb_candidates:
        return plan

    if requested_method == DYNAMODB_RESTORE_METHOD_CAPACITY:
        print(f"\nDynamoDB restore method: capacity-based for all {len(dynamodb_candidates)} new tables (requested)")
        return {
            table["resourceId"]: {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": []}
            for table in dynamodb_candidates
        }

    if requested_method == DYNAMODB_RESTORE_METHOD_IMPORT:
        # Forced: submit every table as an import and let the API reject the ones
        # it cannot take, rather than silently restoring them a different way.
        print(f"\nDynamoDB restore method: ImportTable forced for all {len(dynamodb_candidates)} new tables (requested)")
        return {
            table["resourceId"]: {"method": RESTORE_METHOD_IMPORT_TABLE, "reasons": []}
            for table in dynamodb_candidates
        }

    # Auto: prefer ImportTable, fall back per table to capacity-based.
    print(f"\nDynamoDB restore method (auto) for {len(dynamodb_candidates)} new tables:")

    role_version = get_restore_role_version(eon_client, eon_restore_account_id, restore_account_id)
    parsed_version = parse_role_version(role_version)

    if parsed_version is None or parsed_version < MIN_ROLE_VERSION_FOR_IMPORT:
        minimum = ".".join(str(part) for part in MIN_ROLE_VERSION_FOR_IMPORT)
        reason = (f"restore account role version {role_version or 'unknown'} is below "
                  f"{minimum}, the minimum for ImportTable")
        print(f"  All tables -> capacity-based: {reason}")
        return {
            table["resourceId"]: {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": [reason]}
            for table in dynamodb_candidates
        }

    print(f"  Restore account role version: {role_version}")

    for table in dynamodb_candidates:
        resource_id = table["resourceId"]
        resource_name = table["resourceName"]
        region = table["region"]

        try:
            availability = eon_client.check_dynamodb_import_availability(
                snapshot_id=table["snapshotId"],
                region=region,
                restored_name=table["restoredName"],
            )
        except Exception as e:
            reason = f"import availability check failed: {e}"
            print(f"  {resource_name} -> capacity-based: {reason}")
            plan[resource_id] = {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": [reason]}
            continue

        if availability["available"]:
            print(f"  {resource_name} -> ImportTable")
            plan[resource_id] = {"method": RESTORE_METHOD_IMPORT_TABLE, "reasons": []}
            continue

        reasons = [_IMPORT_UNAVAILABLE_REASONS.get(code, code) for code in availability["reasons"]]
        print(f"  {resource_name} -> capacity-based: {'; '.join(reasons) or 'import not available'}")
        plan[resource_id] = {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": reasons}

    import_count = sum(1 for entry in plan.values() if entry["method"] == RESTORE_METHOD_IMPORT_TABLE)
    print(f"  {import_count} via ImportTable, {len(plan) - import_count} capacity-based")

    return plan


# ---------------------------------------------------------------------------
# DynamoDB WCU allocation
# ---------------------------------------------------------------------------

def calculate_dynamodb_wcu_allocation_by_region(
    dynamodb_tables_by_region: Dict[str, List[Dict[str, Any]]],
    regional_wcu_capacity: int = 40000,
    utilization_percentage: float = 0.95,
    default_wcu_for_zero_size: int = 0,
    table_wcu_max: int = 40000,
) -> Dict[str, int]:
    """
    Calculate WCU allocation for DynamoDB tables per-region based on their sizes.

    Args:
        dynamodb_tables_by_region: Dictionary mapping region to list of tables
        regional_wcu_capacity: WCU capacity per region (default 40000)
        utilization_percentage: Percentage of capacity to use (default 95%)
        default_wcu_for_zero_size: WCU for tables reporting 0 bytes (default 0 —
            there is nothing to write back, so they take no share of the budget
            and Eon's own minimum capacity applies)
        table_wcu_max: Maximum WCU any single table can receive (default 40000).
            Prevents a single large table from consuming all provisioned capacity
            when the regional limit has been raised above the default.

    Returns:
        Dictionary mapping resource_id to allocated WCU
    """
    wcu_allocation = {}

    print(f"\nDynamoDB WCU Allocation (per-region):")
    print(f"  Regional capacity: {regional_wcu_capacity:,} WCU per region")
    print(f"  Per-table max: {table_wcu_max:,} WCU")
    print(f"  Utilization: {utilization_percentage*100}%")
    print(f"  Regions with DynamoDB tables: {len(dynamodb_tables_by_region)}")

    for region, all_tables in dynamodb_tables_by_region.items():
        if not all_tables:
            continue

        # Separate tables by size
        dynamodb_tables = []
        zero_size_tables = []

        for table in all_tables:
            if table["sizeBytes"] == 0:
                zero_size_tables.append(table)
            else:
                dynamodb_tables.append(table)

        # Calculate available WCUs for this region (95% of regional capacity)
        available_wcu = int(regional_wcu_capacity * utilization_percentage)

        print(f"\n  Region {region}:")
        print(f"    Available WCU: {available_wcu:,}")
        print(f"    Tables with size data: {len(dynamodb_tables)}")
        print(f"    Tables with zero size: {len(zero_size_tables)}")

        # Allocate sized tables first so they get proportional shares of capacity
        allocated_total = 0

        if dynamodb_tables:
            total_size = sum(table["sizeBytes"] for table in dynamodb_tables)

            print(f"    Total data size (sized tables): {total_size / (1024**3):.2f} GB")

            for table in dynamodb_tables:
                proportion = table["sizeBytes"] / total_size
                proportional_wcu = int(available_wcu * proportion)
                allocated_wcu = min(max(proportional_wcu, 1), table_wcu_max)

                wcu_allocation[table["resourceId"]] = allocated_wcu
                allocated_total += allocated_wcu

                table_size_gb = table["sizeBytes"] / (1024**3)
                capped = " (capped)" if proportional_wcu > table_wcu_max else ""
                print(f"    {table['resourceName']}: {table_size_gb:.2f} GB ({proportion*100:.1f}%) -> {allocated_wcu:,} WCU{capped}")

        # A table reporting zero bytes has nothing to write back, so it gets no
        # share of the budget — every WCU it took would come out of a table that
        # does have data. Eon applies its own minimum capacity to these.
        remaining_wcu = available_wcu - allocated_total

        for table in zero_size_tables:
            allocated_wcu = min(default_wcu_for_zero_size, max(remaining_wcu, 0))
            wcu_allocation[table["resourceId"]] = allocated_wcu
            remaining_wcu -= allocated_wcu
            allocated_total += allocated_wcu
            print(f"    {table['resourceName']}: 0 GB (no data to write) -> "
                  f"{allocated_wcu:,} WCU (Eon default applies)")

        print(f"    Total allocated in {region}: {allocated_total:,} WCU ({allocated_total/regional_wcu_capacity*100:.1f}% of regional capacity)")

    return wcu_allocation


# ---------------------------------------------------------------------------
# DynamoDB WCU scaling for in-place restores
# ---------------------------------------------------------------------------

def _get_original_settings_from_tags(
    dynamodb_client,
    table_arn: str,
) -> Optional[Dict[str, Any]]:
    """Check for eon:original_* tags on the table (set during a previous scale-up).

    Returns the original settings dict if tags exist, or None if no tags found.
    """
    try:
        tags = {}
        kwargs = {"ResourceArn": table_arn}
        while True:
            response = dynamodb_client.list_tags_of_resource(**kwargs)
            tags.update({t["Key"]: t["Value"] for t in response.get("Tags", [])})
            if "NextToken" not in response:
                break
            kwargs["NextToken"] = response["NextToken"]

        if "eon:original_billing_mode" in tags:
            original_gsi_throughput = {}
            for key, value in tags.items():
                if key.startswith("eon:original_gsi:"):
                    gsi_name = key[len("eon:original_gsi:"):]
                    try:
                        rcu_str, wcu_str = value.split("/")
                        original_gsi_throughput[gsi_name] = {"rcu": int(rcu_str), "wcu": int(wcu_str)}
                    except (ValueError, IndexError):
                        pass

            return {
                "originalBillingMode": tags["eon:original_billing_mode"],
                "originalWcu": int(tags.get("eon:original_wcu", "0")),
                "originalRcu": int(tags.get("eon:original_rcu", "0")),
                "originalGsiThroughput": original_gsi_throughput,
            }
    except Exception as e:
        print(f"WARNING: Could not read tags from table {table_arn}: {e}")

    return None


def _tag_original_settings(
    dynamodb_client,
    table_arn: str,
    billing_mode: str,
    wcu: int,
    rcu: int,
    gsi_throughput: Dict[str, Dict[str, int]],
) -> None:
    """Write eon:original_* tags to the table for idempotency."""
    tags = [
        {"Key": "eon:original_billing_mode", "Value": billing_mode},
        {"Key": "eon:original_wcu", "Value": str(wcu)},
        {"Key": "eon:original_rcu", "Value": str(rcu)},
    ]
    for gsi_name, gsi_info in gsi_throughput.items():
        tag_key = f"eon:original_gsi:{gsi_name}"
        if len(tag_key) > 128:
            print(f"WARNING: Skipping tag for GSI {gsi_name} — tag key exceeds 128-char limit")
            continue
        tags.append({"Key": tag_key, "Value": f"{gsi_info['rcu']}/{gsi_info['wcu']}"})

    dynamodb_client.tag_resource(ResourceArn=table_arn, Tags=tags)


def _scale_up_dynamodb_table_wcu(
    table_name: str,
    region: str,
    allocated_wcu: int,
    credentials: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """
    Scale up a DynamoDB table's WCU before an in-place restore.

    Captures original settings, tags the table for idempotency, and updates
    throughput. Returns the original settings dict for later restoration.

    Warm throughput is handled separately by the monitor loop after the restore
    is initiated — DynamoDB pre-allocates partitions asynchronously, so just
    getting the request in during the restore is sufficient.
    """
    if not credentials:
        print(f"WARNING: No cross-account credentials — cannot scale WCU for {table_name}")
        return {"wcuScaledUp": False}

    if allocated_wcu <= 0:
        # Nothing to write back, so the table keeps whatever capacity it has.
        print(f"Table {table_name} reports no data to restore — leaving its throughput alone")
        return {"wcuScaledUp": False}

    try:
        dynamodb_client = create_boto3_client("dynamodb", region, credentials)

        # Describe table to get current settings
        desc = dynamodb_client.describe_table(TableName=table_name)
        table = desc["Table"]
        table_arn = table["TableArn"]
        table_status = table.get("TableStatus")

        if table_status != "ACTIVE":
            print(f"WARNING: Table {table_name} is in {table_status} state — skipping WCU scale-up")
            return {"wcuScaledUp": False}

        # Determine current billing mode and throughput
        billing_mode = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
        current_wcu = table.get("ProvisionedThroughput", {}).get("WriteCapacityUnits", 0)
        current_rcu = table.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0)

        # Capture GSI throughput
        gsi_throughput = {}
        for gsi in table.get("GlobalSecondaryIndexes", []):
            gsi_name = gsi["IndexName"]
            gsi_pt = gsi.get("ProvisionedThroughput", {})
            gsi_throughput[gsi_name] = {
                "rcu": gsi_pt.get("ReadCapacityUnits", 0),
                "wcu": gsi_pt.get("WriteCapacityUnits", 0),
            }

        # Check for existing eon:original_* tags (idempotency — we may be retrying)
        tag_settings = _get_original_settings_from_tags(dynamodb_client, table_arn)
        if tag_settings:
            print(f"Found existing eon:original_* tags on {table_name} — using tag values as original settings (likely a retry)")
            original_billing_mode = tag_settings["originalBillingMode"]
            original_wcu = tag_settings["originalWcu"]
            original_rcu = tag_settings["originalRcu"]
            original_gsi_throughput = tag_settings["originalGsiThroughput"]
        else:
            original_billing_mode = billing_mode
            original_wcu = current_wcu
            original_rcu = current_rcu
            original_gsi_throughput = gsi_throughput

        # Short-circuit: already at or above target WCU in PROVISIONED mode
        if billing_mode == "PROVISIONED" and current_wcu >= allocated_wcu:
            print(f"Table {table_name} already has {current_wcu:,} WCU >= allocated {allocated_wcu:,} WCU — skipping scale-up")
            return {"wcuScaledUp": False}

        # Tag original settings before modifying (idempotency for retries)
        if not tag_settings:
            _tag_original_settings(
                dynamodb_client, table_arn,
                original_billing_mode, original_wcu, original_rcu, original_gsi_throughput,
            )

        # Build update_table arguments
        update_kwargs = {"TableName": table_name}

        if billing_mode == "PAY_PER_REQUEST":
            # Switch from on-demand to provisioned
            update_kwargs["BillingMode"] = "PROVISIONED"
            update_kwargs["ProvisionedThroughput"] = {
                "ReadCapacityUnits": 5,  # Minimal RCU — only writes during restore
                "WriteCapacityUnits": allocated_wcu,
            }
            # GSIs must also be set when switching billing mode
            if gsi_throughput:
                update_kwargs["GlobalSecondaryIndexUpdates"] = [
                    {
                        "Update": {
                            "IndexName": gsi_name,
                            "ProvisionedThroughput": {
                                "ReadCapacityUnits": 5,
                                "WriteCapacityUnits": allocated_wcu,
                            },
                        }
                    }
                    for gsi_name in gsi_throughput
                ]
            print(f"Switching {table_name} from PAY_PER_REQUEST to PROVISIONED with {allocated_wcu:,} WCU")
        else:
            # Already provisioned — just update throughput
            update_kwargs["ProvisionedThroughput"] = {
                "ReadCapacityUnits": max(current_rcu, 5),
                "WriteCapacityUnits": allocated_wcu,
            }
            # Scale up GSI WCU too (writes to base table trigger GSI updates)
            if gsi_throughput:
                update_kwargs["GlobalSecondaryIndexUpdates"] = [
                    {
                        "Update": {
                            "IndexName": gsi_name,
                            "ProvisionedThroughput": {
                                "ReadCapacityUnits": max(gsi_info["rcu"], 5),
                                "WriteCapacityUnits": allocated_wcu,
                            },
                        }
                    }
                    for gsi_name, gsi_info in gsi_throughput.items()
                ]
            print(f"Updating {table_name} WCU from {current_wcu:,} to {allocated_wcu:,}")

        dynamodb_client.update_table(**update_kwargs)

        return {
            "wcuScaledUp": True,
            "originalBillingMode": original_billing_mode,
            "originalWcu": original_wcu,
            "originalRcu": original_rcu,
            "originalGsiThroughput": original_gsi_throughput,
        }

    except Exception as e:
        print(f"ERROR: Failed to scale up WCU for table {table_name}: {e}")
        return {"wcuScaledUp": False, "error": str(e)}


def _restore_dynamodb_table_wcu_immediate(
    table_name: str,
    region: str,
    original_settings: Dict[str, Any],
    credentials: Optional[Dict[str, str]],
) -> None:
    """
    Immediately restore a DynamoDB table's WCU to original settings.

    Used for inline rollback when the Eon API call fails after scale-up.
    """
    if not original_settings.get("wcuScaledUp") or not credentials:
        return

    try:
        dynamodb_client = create_boto3_client("dynamodb", region, credentials)
        original_billing_mode = original_settings["originalBillingMode"]
        original_wcu = original_settings["originalWcu"]
        original_rcu = original_settings["originalRcu"]
        original_gsi_throughput = original_settings.get("originalGsiThroughput", {})

        update_kwargs = {"TableName": table_name}

        if original_billing_mode == "PAY_PER_REQUEST":
            update_kwargs["BillingMode"] = "PAY_PER_REQUEST"
        else:
            update_kwargs["ProvisionedThroughput"] = {
                "ReadCapacityUnits": original_rcu,
                "WriteCapacityUnits": original_wcu,
            }
            if original_gsi_throughput:
                update_kwargs["GlobalSecondaryIndexUpdates"] = [
                    {
                        "Update": {
                            "IndexName": gsi_name,
                            "ProvisionedThroughput": {
                                "ReadCapacityUnits": gsi_info["rcu"],
                                "WriteCapacityUnits": gsi_info["wcu"],
                            },
                        }
                    }
                    for gsi_name, gsi_info in original_gsi_throughput.items()
                ]

        dynamodb_client.update_table(**update_kwargs)

        # Clean up idempotency tags
        table_arn = dynamodb_client.describe_table(TableName=table_name)["Table"]["TableArn"]
        tag_keys = ["eon:original_billing_mode", "eon:original_wcu", "eon:original_rcu", "eon:original_gsi_throughput"]
        tag_keys += [f"eon:original_gsi:{gsi_name}" for gsi_name in original_gsi_throughput]
        dynamodb_client.untag_resource(ResourceArn=table_arn, TagKeys=tag_keys)

        print(f"Rolled back WCU for {table_name} to original settings")
    except Exception as e:
        print(f"WARNING: Failed to rollback WCU for {table_name}: {e}")
        print(f"Table may still have elevated WCU — manual intervention may be needed")


# ---------------------------------------------------------------------------
# CloudFormation stack discovery
# ---------------------------------------------------------------------------

def _global_table_replica_regions(
    table_name: str,
    region: str,
    restore_account_credentials: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Return every region a global table has a replica in, starting with `region`.

    Best-effort: if DescribeTable is unavailable the table still matches sources
    in the stack's own region, which is the pre-existing behaviour.
    """
    regions = [region]
    try:
        ddb_client = create_boto3_client("dynamodb", region, restore_account_credentials)
        table = ddb_client.describe_table(TableName=table_name).get("Table", {})
        for replica in table.get("Replicas", []):
            replica_region = replica.get("RegionName")
            if replica_region and replica_region not in regions:
                regions.append(replica_region)
    except Exception as e:
        print(f"WARNING: Could not read replica regions for global table '{table_name}' in {region}: {str(e)}. "
              f"It will only match source resources in {region}.")

    return regions


def stack_table_match(
    recovery_stack_tables: Dict[str, Dict[str, Any]],
    resource_name: str,
    source_region: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    Find the pre-created recovery-stack table for a source resource, if any.

    Matches on table name plus region, where "region" is the stack's region for a
    regular table and any replica region for a global table.
    """
    entry = recovery_stack_tables.get(resource_name)
    if not entry:
        return None

    match_regions = entry.get("regions") or [entry.get("region")]
    return entry if source_region in match_regions else None


def discover_dynamodb_tables_from_stacks(
    stack_names: List[str],
    restore_account_credentials: Optional[Dict[str, str]] = None,
    regions: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """
    Discover DynamoDB tables from CloudFormation stack resources.

    Queries CloudFormation stacks to find all DynamoDB table resources created by
    the stack. Works with any CloudFormation stack (CDK, SAM, plain CFN, etc.).

    Both AWS::DynamoDB::Table and AWS::DynamoDB::GlobalTable are matched. A global
    table is still a DynamoDB table as far as restoring goes — Eon inventories one
    representative resource for the replica set — but CloudFormation reports it
    under a different resource type, which CDK's TableV2 construct emits by
    default. Matching only AWS::DynamoDB::Table silently misses those, so the
    table gets restored as a new table (or skipped in recoveryStacksOnly mode)
    instead of restored in place.

    Args:
        stack_names: List of CloudFormation stack names to scan
        restore_account_credentials: Cross-account credentials for restore account
        regions: List of regions to check for stacks (defaults to us-east-1)

    Returns:
        Dictionary mapping table names to their configuration:
        {
            "MyTable": {"tableName": "MyTable", "region": "us-east-1", "stackName": "MyStack"},
            ...
        }
    """
    if not stack_names:
        return {}

    if not regions:
        regions = ["us-east-1"]

    discovered_tables = {}

    for region in regions:
        cfn_client = create_boto3_client("cloudformation", region, restore_account_credentials)

        for stack_name in stack_names:
            try:
                # List all resources in the stack
                paginator = cfn_client.get_paginator("list_stack_resources")
                tables_found_in_stack = 0

                for page in paginator.paginate(StackName=stack_name):
                    for resource in page.get("StackResourceSummaries", []):
                        resource_type = resource.get("ResourceType", "")
                        resource_status = resource.get("ResourceStatus", "")

                        # Look for DynamoDB tables that are successfully created
                        if resource_type in DYNAMODB_STACK_RESOURCE_TYPES and resource_status in [
                            "CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"
                        ]:
                            # PhysicalResourceId is the table name for both
                            # AWS::DynamoDB::Table and AWS::DynamoDB::GlobalTable.
                            table_name = resource.get("PhysicalResourceId")
                            logical_id = resource.get("LogicalResourceId", "")

                            if table_name:
                                # A global table's stack lives in one region but its
                                # replicas span several, and Eon inventories the
                                # replica set as a single resource whose region may be
                                # any of them. Match on the whole replica set; still
                                # restore into the stack's own region, since writes
                                # replicate to the others.
                                table_regions = [region]
                                if resource_type == "AWS::DynamoDB::GlobalTable":
                                    table_regions = _global_table_replica_regions(
                                        table_name=table_name,
                                        region=region,
                                        restore_account_credentials=restore_account_credentials,
                                    )

                                discovered_tables[table_name] = {
                                    "tableName": table_name,
                                    "region": region,
                                    "regions": table_regions,
                                    "stackName": stack_name,
                                    "logicalId": logical_id,
                                    "cfnResourceType": resource_type
                                }
                                tables_found_in_stack += 1
                                print(f"Discovered DynamoDB table from stack: {table_name} in {region} "
                                      f"({resource_type}, stack: {stack_name}, logical ID: {logical_id}, "
                                      f"matches source regions: {', '.join(table_regions)})")

                if tables_found_in_stack > 0:
                    print(f"Found {tables_found_in_stack} DynamoDB table(s) in stack '{stack_name}' ({region})")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "ValidationError" and "does not exist" in str(e):
                    print(f"CloudFormation stack '{stack_name}' not found in region {region}")
                else:
                    print(f"Error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")
            except Exception as e:
                print(f"Unexpected error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")

    print(f"\nDiscovered {len(discovered_tables)} DynamoDB table(s) from stack(s) for in-place restore")
    return discovered_tables


def discover_s3_buckets_from_stacks(
    stack_names: List[str],
    restore_account_credentials: Optional[Dict[str, str]] = None,
    regions: Optional[List[str]] = None,
    s3_in_place_tag_key: str = "eon_functional_id",
) -> Dict[str, Dict[str, str]]:
    """
    Discover S3 buckets from CloudFormation stack resources and index them by a matching tag.

    Queries CloudFormation stacks to find all AWS::S3::Bucket resources,
    then fetches the matching tag from each bucket to build a lookup dict.

    Args:
        stack_names: List of CloudFormation stack names to scan
        restore_account_credentials: Cross-account credentials for restore account
        regions: List of regions to check for stacks (defaults to us-east-1)
        s3_in_place_tag_key: Tag key used to match source and target S3 buckets
            for in-place restore (default: "eon_functional_id"). The tag value
            can be any string (bucket name, hash, UUID, etc.).

    Returns:
        Dictionary mapping tag value to bucket configuration:
        {
            "my-functional-id": {
                "bucketName": "actual-bucket-name",
                "region": "us-east-1",
                "stackName": "MyStack",
                "logicalId": "MyBucketResource",
                "eonFunctionalId": "my-functional-id"
            },
            ...
        }
    """
    if not stack_names:
        return {}

    if not regions:
        regions = ["us-east-1"]

    # Phase 1: Discover S3 bucket names from CloudFormation stacks
    discovered_buckets = []

    for region in regions:
        cfn_client = create_boto3_client("cloudformation", region, restore_account_credentials)

        for stack_name in stack_names:
            try:
                paginator = cfn_client.get_paginator("list_stack_resources")
                buckets_found_in_stack = 0

                for page in paginator.paginate(StackName=stack_name):
                    for resource in page.get("StackResourceSummaries", []):
                        resource_type = resource.get("ResourceType", "")
                        resource_status = resource.get("ResourceStatus", "")

                        if resource_type == "AWS::S3::Bucket" and resource_status in [
                            "CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"
                        ]:
                            bucket_name = resource.get("PhysicalResourceId")
                            logical_id = resource.get("LogicalResourceId", "")

                            if bucket_name:
                                discovered_buckets.append({
                                    "bucketName": bucket_name,
                                    "region": region,
                                    "stackName": stack_name,
                                    "logicalId": logical_id
                                })
                                buckets_found_in_stack += 1
                                print(f"Discovered S3 bucket from stack: {bucket_name} in {region} (stack: {stack_name}, logical ID: {logical_id})")

                if buckets_found_in_stack > 0:
                    print(f"Found {buckets_found_in_stack} S3 bucket(s) in stack '{stack_name}' ({region})")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "ValidationError" and "does not exist" in str(e):
                    print(f"CloudFormation stack '{stack_name}' not found in region {region}")
                else:
                    print(f"Error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")
            except Exception as e:
                print(f"Unexpected error checking CloudFormation stack '{stack_name}' in {region}: {str(e)}")

    # Phase 2: Fetch matching tags from discovered buckets
    s3_buckets_by_functional_id = {}

    for bucket_info in discovered_buckets:
        s3_client = create_boto3_client("s3", bucket_info["region"], restore_account_credentials)

        try:
            tagging_response = s3_client.get_bucket_tagging(Bucket=bucket_info["bucketName"])
            tag_set = tagging_response.get("TagSet", [])
            functional_id = None
            for tag in tag_set:
                if tag["Key"] == s3_in_place_tag_key:
                    functional_id = tag["Value"]
                    break

            if functional_id:
                if functional_id in s3_buckets_by_functional_id:
                    print(f"WARNING: Duplicate '{s3_in_place_tag_key}' value '{functional_id}' found on bucket '{bucket_info['bucketName']}', overwriting previous match")
                bucket_info["eonFunctionalId"] = functional_id
                s3_buckets_by_functional_id[functional_id] = bucket_info
                print(f"Discovered S3 bucket '{bucket_info['bucketName']}' with {s3_in_place_tag_key}='{functional_id}' (stack: {bucket_info['stackName']})")
            else:
                print(f"S3 bucket '{bucket_info['bucketName']}' has no '{s3_in_place_tag_key}' tag, skipping for in-place matching")

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchTagSet":
                print(f"S3 bucket '{bucket_info['bucketName']}' has no tags, skipping for in-place matching")
            else:
                print(f"Error fetching tags for S3 bucket '{bucket_info['bucketName']}': {str(e)}")
        except Exception as e:
            print(f"Unexpected error fetching tags for S3 bucket '{bucket_info['bucketName']}': {str(e)}")

    print(f"\nDiscovered {len(s3_buckets_by_functional_id)} S3 bucket(s) with '{s3_in_place_tag_key}' from stack(s) for in-place restore")
    return s3_buckets_by_functional_id


# ---------------------------------------------------------------------------
# S3 bucket creation
# ---------------------------------------------------------------------------

def create_s3_bucket(bucket_name: str, region: str, kms_key_id: str, restore_account_id: str, snapshot_id: str, snapshot_point_in_time: str, original_tags: Dict[str, str] = None, restore_account_credentials: Dict[str, str] = None) -> None:
    """Create an S3 bucket in the restore account."""
    if original_tags is None:
        original_tags = {}

    s3_client = create_boto3_client("s3", region, restore_account_credentials)

    try:
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )

        # Enable default encryption
        s3_client.put_bucket_encryption(
            Bucket=bucket_name,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": kms_key_id
                        },
                        "BucketKeyEnabled": True
                    }
                ]
            }
        )

        # Filter out AWS system tags (aws:, elasticbeanstalk:) as they cannot be manually set
        filtered_original_tags = {
            k: v for k, v in original_tags.items()
            if not k.lower().startswith(("aws:", "elasticbeanstalk:"))
        }

        # Merge filtered original tags with restore tags (restore tags take precedence)
        s3_tags = {
            **filtered_original_tags,
            "ManagedBy": "EonBulkRecovery",
            "Purpose": "RestoreDestination",
            "eon:restore": "true",
            "eon:snapshot_id": snapshot_id,
            "eon:snapshot_time": snapshot_point_in_time
        }

        # Convert tags dict to AWS TagSet format
        tag_set = [{"Key": k, "Value": v} for k, v in s3_tags.items()]

        # Add tags
        s3_client.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={"TagSet": tag_set}
        )

        print(f"Created S3 bucket: {bucket_name}")

    except ClientError as e:
        if e.response["Error"]["Code"] == "BucketAlreadyOwnedByYou":
            print(f"S3 bucket {bucket_name} already exists")
        else:
            raise


# ---------------------------------------------------------------------------
# Restore context (shared state for all restore operations)
# ---------------------------------------------------------------------------

@dataclass
class _RestoreContext:
    """Bundles the shared configuration used by every per-resource restore function."""
    eon_client: EonClient
    eon_restore_account_id: str
    restore_account_id: str
    restore_region: str
    kms_key_arns_by_region: Dict[str, str]
    rds_subnet_groups_by_region: Dict[str, str]
    vpc_configs_by_region: Dict[str, Any]
    restore_account_credentials: Optional[Dict[str, str]]
    resource_name_prefix: Optional[str]
    exclude_ec2_tag_keys: List[str]
    recovery_stack_tables: Dict[str, Dict[str, str]]
    recovery_stack_s3_buckets: Dict[str, Dict[str, str]]
    recovery_stacks_only: bool
    dynamodb_wcu_allocation: Dict[str, int]
    dynamodb_restore_methods: Dict[str, Dict[str, Any]]
    dynamodb_warm_throughput: bool
    s3_in_place_tag_key: str


# ---------------------------------------------------------------------------
# Per-resource-type restore functions
#
# Each returns Optional[Tuple[str, dict]]:
#   - None  → skip this resource (e.g. recoveryStacksOnly with no match)
#   - (job_id, details_dict) → restore was initiated
#   - raises on error
# ---------------------------------------------------------------------------

def _initiate_ec2_restore(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    target_region: Optional[str],
) -> Optional[Tuple[str, dict]]:
    resource_id = resource_snapshot["resourceId"]
    resource_name = resource_snapshot["resourceName"]
    snapshot_id = resource_snapshot["snapshotId"]
    snapshot_point_in_time = resource_snapshot.get("snapshotPointInTime", "Unknown")

    # Get instance configuration from snapshot
    instance_type = resource_snapshot.get("instanceType", "t3.medium")
    volumes = resource_snapshot.get("volumes", [])

    # Resolve region
    actual_region = resolve_target_region(target_region, ctx.vpc_configs_by_region, resource_name)
    vpc_config = ctx.vpc_configs_by_region[actual_region]
    if actual_region == target_region:
        print(f"Using VPC config for target region: {actual_region}")

    # Extract subnets and security groups from the region-specific VPC config
    subnets_per_az = vpc_config.get("subnetsPerAvailabilityZone", [])
    security_groups = vpc_config.get("securityGroups", {})
    security_group_ids = security_groups.get("restoreServer", [])

    if not subnets_per_az:
        raise ValueError(f"No subnets available in region {actual_region} for EC2 restore of {resource_name}")

    # Check which AZs support this instance type
    ec2_client = create_boto3_client("ec2", actual_region, ctx.restore_account_credentials)

    print(f"Checking availability of instance type {instance_type} in region {actual_region}")
    try:
        offerings_response = ec2_client.describe_instance_type_offerings(
            LocationType='availability-zone',
            Filters=[
                {'Name': 'instance-type', 'Values': [instance_type]}
            ]
        )
        available_azs = {offering['Location'] for offering in offerings_response.get('InstanceTypeOfferings', [])}
        print(f"Instance type {instance_type} is available in AZs: {sorted(available_azs)}")
    except ClientError as e:
        print(f"WARNING: Could not check instance type availability: {str(e)}")
        available_azs = None

    # Select a subnet in an AZ that supports the instance type
    subnet_id = _select_ec2_subnet(subnets_per_az, available_azs, instance_type)

    # Volumes must be present - no volumes means no data to restore
    if not volumes:
        print(f"ERROR: No volume configuration found for {resource_name}, cannot restore")
        raise ValueError(f"No volumes found for EC2 instance {resource_name}")

    kms_key_arn = require_kms_key(ctx.kms_key_arns_by_region, actual_region)

    # Build volume restore parameters from snapshot volume data
    volume_restore_params = []
    for vol in volumes:
        # Get original volume tags and filter out excluded tag keys
        original_tags = vol.get("tags", {})
        filtered_volume_tags = {
            k: v for k, v in original_tags.items()
            if k not in ctx.exclude_ec2_tag_keys
        }

        volume_tags = {
            **filtered_volume_tags,
            "eon:restore": "true",
            "eon:snapshot_id": snapshot_id,
            "eon:snapshot_time": snapshot_point_in_time
        }

        vol_param = {
            "providerVolumeId": vol.get("providerVolumeId", "unknown"),
            "volumeEncryptionKeyId": kms_key_arn,
            "volumeSettings": vol.get("volumeSettings", {}),
            "tags": volume_tags
        }
        volume_restore_params.append(vol_param)

    print(f"EC2 restore config - region: {actual_region}, instance_type: {instance_type}, subnet: {subnet_id}, "
          f"security_groups: {len(security_group_ids)}, volumes: {len(volume_restore_params)}")
    print(f"Volume encryption - using KMS key: {kms_key_arn}")

    # Build instance tags (filter excluded keys, merge with restore tags)
    original_tags = resource_snapshot.get("originalTags", {})
    filtered_original_tags = {
        k: v for k, v in original_tags.items()
        if k not in ctx.exclude_ec2_tag_keys
    }

    if ctx.exclude_ec2_tag_keys and original_tags:
        excluded_tags = [k for k in original_tags.keys() if k in ctx.exclude_ec2_tag_keys]
        if excluded_tags:
            print(f"Excluding EC2 tags for {resource_name}: {excluded_tags}")

    restored_instance_name = get_restored_name(resource_name, ctx.resource_name_prefix)
    ec2_tags = {
        **filtered_original_tags,
        "Name": restored_instance_name,
        "RestoreSource": resource_snapshot.get("providerResourceId", ""),
        "ManagedBy": "EonBulkRecovery",
        "eon:restore": "true",
        "eon:snapshot_id": snapshot_id,
        "eon:snapshot_time": snapshot_point_in_time
    }

    destination_config = {
        "awsEc2": {
            "region": actual_region,
            "instanceType": instance_type,
            "subnetId": subnet_id,
            "securityGroupIds": security_group_ids,
            "tags": ec2_tags,
            "volumeRestoreParameters": volume_restore_params
        }
    }

    # The source instance's IAM instance profile is deliberately not reattached to
    # the restored instance. Attach one after the restore if the workload needs it.

    job_id = ctx.eon_client.restore_ec2_instance(
        resource_id=resource_id,
        snapshot_id=snapshot_id,
        restore_account_id=ctx.eon_restore_account_id,
        destination_config=destination_config
    )

    restored_resource_details = {
        "restoredRegion": actual_region,
        "instanceType": instance_type,
        "volumeCount": len(volume_restore_params),
        "restoredName": restored_instance_name
    }
    return job_id, restored_resource_details


def _select_ec2_subnet(
    subnets_per_az: List[Dict[str, Any]],
    available_azs: Optional[set],
    instance_type: str,
) -> str:
    """Pick a subnet compatible with *instance_type*, or fall back to random."""
    if available_azs:
        compatible_subnets = [
            subnet for subnet in subnets_per_az
            if subnet.get("availabilityZone") in available_azs and subnet.get("subnetId")
        ]
        if compatible_subnets:
            selected = random.choice(compatible_subnets)
            print(f"Selected subnet {selected['subnetId']} in AZ {selected['availabilityZone']} (supports {instance_type})")
            return selected["subnetId"]

        configured_azs = {subnet.get("availabilityZone") for subnet in subnets_per_az if subnet.get("availabilityZone")}
        raise ValueError(
            f"Instance type {instance_type} is not available in any configured availability zones. "
            f"Instance type available in: {sorted(available_azs)}, "
            f"Configured subnets in: {sorted(configured_azs)}"
        )

    # Couldn't check availability, use random selection as fallback
    available_subnets = [subnet.get("subnetId") for subnet in subnets_per_az if subnet.get("subnetId")]
    subnet_id = random.choice(available_subnets)
    print(f"Selected subnet {subnet_id} (could not verify instance type availability)")
    return subnet_id


def _initiate_rds_restore(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    target_region: Optional[str],
) -> Optional[Tuple[str, dict]]:
    resource_id = resource_snapshot["resourceId"]
    resource_name = resource_snapshot["resourceName"]
    snapshot_id = resource_snapshot["snapshotId"]
    snapshot_point_in_time = resource_snapshot.get("snapshotPointInTime", "Unknown")

    # Eon stores `instanceClass` only at the RDS instance level. For Aurora
    # clusters the class lives on cluster member properties (server-side only,
    # not exposed via API), so `awsRds.instanceClass` comes back empty. In that
    # case we omit `dbInstanceClass` from the restore request entirely — Eon's
    # restore service inherits the source class server-side from the snapshot.
    db_instance_class = resource_snapshot.get("dbInstanceClass") or None
    source_engine = resource_snapshot.get("engine")

    # Resolve region via RDS subnet groups
    actual_region = resolve_target_region(
        target_region, ctx.rds_subnet_groups_by_region, resource_name,
        config_label="RDS subnet group",
        error_label="RDS subnet groups",
    )
    rds_subnet_group_name = ctx.rds_subnet_groups_by_region[actual_region]
    if actual_region == target_region:
        print(f"Using RDS subnet group for target region: {actual_region}")

    # Get security groups from the region-specific VPC config
    vpc_config = ctx.vpc_configs_by_region.get(actual_region, {})
    security_groups_config = vpc_config.get("securityGroups", {})
    rds_security_groups = security_groups_config.get("restoredRdsInstance", [])

    if db_instance_class:
        configured_azs = [
            s.get("availabilityZone")
            for s in vpc_config.get("subnetsPerAvailabilityZone", [])
            if s.get("availabilityZone")
        ]
        rds_client = create_boto3_client("rds", actual_region, ctx.restore_account_credentials)
        validate_rds_instance_class_available(
            rds_client,
            eon_engine=source_engine,
            db_instance_class=db_instance_class,
            configured_azs=configured_azs,
            resource_name=resource_name,
        )
    else:
        print(f"No source DB instance class for {resource_name}; omitting dbInstanceClass so Eon inherits from the snapshot")

    kms_key_arn = require_kms_key(ctx.kms_key_arns_by_region, actual_region)

    # Merge original tags with restore tags (restore tags take precedence)
    original_tags = resource_snapshot.get("originalTags", {})
    restored_db_name = sanitize_rds_identifier(get_restored_name(resource_name, ctx.resource_name_prefix))
    rds_tags = {
        **original_tags,
        "Name": restored_db_name,
        "RestoreSource": resource_snapshot.get("providerResourceId", ""),
        "ManagedBy": "EonBulkRecovery",
        "eon:restore": "true",
        "eon:snapshot_id": snapshot_id,
        "eon:snapshot_time": snapshot_point_in_time
    }

    aws_rds_destination = {
        "restoreRegion": actual_region,
        "encryptionKeyId": kms_key_arn,
        "restoredName": restored_db_name,
        "securityGroups": rds_security_groups,
        "subnetGroup": rds_subnet_group_name,
        "tags": rds_tags
    }
    if db_instance_class:
        aws_rds_destination["dbInstanceClass"] = db_instance_class
    destination_config = {"awsRds": aws_rds_destination}

    print(f"RDS restore config - region: {actual_region}, subnet_group: {rds_subnet_group_name}, "
          f"security_groups: {len(rds_security_groups)}, instance_class: {db_instance_class or '(inherit from source)'}")

    job_id = ctx.eon_client.restore_rds_instance(
        resource_id=resource_id,
        snapshot_id=snapshot_id,
        restore_account_id=ctx.eon_restore_account_id,
        destination_config=destination_config
    )

    restored_resource_details = {
        "restoredRegion": actual_region,
        "dbInstanceClass": db_instance_class or "(inherit from source)",
        "restoredName": restored_db_name,
        "subnetGroup": rds_subnet_group_name
    }
    return job_id, restored_resource_details


def _initiate_s3_restore(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    target_region: Optional[str],
) -> Optional[Tuple[str, dict]]:
    resource_name = resource_snapshot["resourceName"]
    original_tags = resource_snapshot.get("originalTags", {})

    # Resolve region and KMS key up-front (matches original validation order)
    actual_region = resolve_target_region(target_region, ctx.vpc_configs_by_region, resource_name)
    if actual_region == target_region:
        print(f"Restoring S3 to target region: {actual_region}")

    kms_key_arn = require_kms_key(ctx.kms_key_arns_by_region, actual_region)

    # Check for in-place restore via tag matching (configurable key)
    source_functional_id = original_tags.get(ctx.s3_in_place_tag_key)
    stack_bucket_match = None

    if source_functional_id and ctx.recovery_stack_s3_buckets:
        if source_functional_id in ctx.recovery_stack_s3_buckets:
            stack_bucket_match = ctx.recovery_stack_s3_buckets[source_functional_id]
            print(f"Found matching S3 bucket '{stack_bucket_match['bucketName']}' with {ctx.s3_in_place_tag_key}='{source_functional_id}' (stack: {stack_bucket_match['stackName']})")

    if stack_bucket_match:
        return _restore_s3_in_place(ctx, resource_snapshot, stack_bucket_match, source_functional_id)

    # No in-place match
    if ctx.recovery_stacks_only:
        print(f"SKIPPING {resource_name} (S3) - recoveryStacksOnly mode, no matching stack bucket")
        return None

    return _restore_s3_new_bucket(ctx, resource_snapshot, actual_region, kms_key_arn)


def _restore_s3_in_place(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    stack_bucket_match: Dict[str, str],
    source_functional_id: str,
) -> Tuple[str, dict]:
    """Restore S3 data into an existing bucket from a recovery stack."""
    resource_id = resource_snapshot["resourceId"]
    resource_name = resource_snapshot["resourceName"]
    snapshot_id = resource_snapshot["snapshotId"]

    restore_bucket_name = stack_bucket_match["bucketName"]
    restore_bucket_region = stack_bucket_match["region"]

    # Get KMS key for the bucket's region (used for restore worker EC2 EBS encryption)
    stack_kms_key_arn = ctx.kms_key_arns_by_region.get(restore_bucket_region)
    if not stack_kms_key_arn:
        raise ValueError(f"No KMS key available for region {restore_bucket_region} (required for in-place S3 restore)")

    print(f"S3 IN-PLACE restore config - region: {restore_bucket_region}, bucket: {restore_bucket_name} (CloudFormation stack: {stack_bucket_match['stackName']})")

    destination_config = {
        "s3Bucket": {
            "region": restore_bucket_region,
            "bucketName": restore_bucket_name,
            "encryptionKeyId": stack_kms_key_arn,
            "prefix": ""
        }
    }

    job_id = ctx.eon_client.restore_s3_bucket(
        resource_id=resource_id,
        snapshot_id=snapshot_id,
        restore_account_id=ctx.eon_restore_account_id,
        destination_config=destination_config
    )

    restored_resource_details = {
        "restoredRegion": restore_bucket_region,
        "restoredBucketName": restore_bucket_name,
        "originalBucketName": resource_snapshot.get("providerResourceId", resource_name),
        "restoreType": "IN_PLACE",
        "recoveryStackName": stack_bucket_match["stackName"],
        "eonFunctionalId": source_functional_id
    }
    return job_id, restored_resource_details


def _restore_s3_new_bucket(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    actual_region: str,
    kms_key_arn: str,
) -> Tuple[str, dict]:
    """Create a new S3 bucket and restore data into it."""
    resource_id = resource_snapshot["resourceId"]
    resource_name = resource_snapshot["resourceName"]
    snapshot_id = resource_snapshot["snapshotId"]
    snapshot_point_in_time = resource_snapshot.get("snapshotPointInTime", "Unknown")
    original_tags = resource_snapshot.get("originalTags", {})

    # Create a bucket name (S3 bucket names must be globally unique)
    original_bucket_name = resource_snapshot.get("providerResourceId", resource_name)
    hash_input = f"{original_bucket_name}-{snapshot_id}-{actual_region}-{ctx.restore_account_id}"
    hash_suffix = hashlib.md5(hash_input.encode()).hexdigest()[:8]

    if ctx.resource_name_prefix:
        restored_bucket_name = sanitize_s3_bucket_name(f"{ctx.resource_name_prefix}{original_bucket_name}", hash_suffix)
    else:
        restored_bucket_name = sanitize_s3_bucket_name(original_bucket_name, hash_suffix)

    print(f"S3 restore config - region: {actual_region}, bucket: {restored_bucket_name}")

    create_s3_bucket(
        bucket_name=restored_bucket_name,
        region=actual_region,
        kms_key_id=kms_key_arn,
        restore_account_id=ctx.restore_account_id,
        snapshot_id=snapshot_id,
        snapshot_point_in_time=snapshot_point_in_time,
        original_tags=original_tags,
        restore_account_credentials=ctx.restore_account_credentials
    )

    destination_config = {
        "s3Bucket": {
            "region": actual_region,
            "bucketName": restored_bucket_name,
            "encryptionKeyId": kms_key_arn,
            "prefix": ""
        }
    }

    job_id = ctx.eon_client.restore_s3_bucket(
        resource_id=resource_id,
        snapshot_id=snapshot_id,
        restore_account_id=ctx.eon_restore_account_id,
        destination_config=destination_config
    )

    restored_resource_details = {
        "restoredRegion": actual_region,
        "restoredBucketName": restored_bucket_name,
        "originalBucketName": original_bucket_name
    }
    return job_id, restored_resource_details


def _initiate_dynamodb_restore(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    target_region: Optional[str],
) -> Optional[Tuple[str, dict]]:
    resource_name = resource_snapshot["resourceName"]
    source_region = resource_snapshot.get("region")

    # Resolve region and KMS key up-front (matches original validation order)
    actual_region = resolve_target_region(target_region, ctx.vpc_configs_by_region, resource_name)
    if actual_region == target_region:
        print(f"Restoring DynamoDB to target region: {actual_region}")

    kms_key_arn = require_kms_key(ctx.kms_key_arns_by_region, actual_region)

    # Check if a pre-created table exists for in-place restore
    matched_stack_table = None
    if ctx.recovery_stack_tables:
        matched_stack_table = stack_table_match(ctx.recovery_stack_tables, resource_name, source_region)
        if matched_stack_table:
            print(f"Found matching pre-created table '{resource_name}' for source region {source_region} "
                  f"(restoring into {matched_stack_table['region']}, stack: {matched_stack_table['stackName']})")

    if matched_stack_table:
        return _restore_dynamodb_in_place(ctx, resource_snapshot, matched_stack_table)

    # No in-place match
    if ctx.recovery_stacks_only:
        print(f"SKIPPING {resource_name} (DynamoDB) - recoveryStacksOnly mode, no matching stack table")
        return None

    return _restore_dynamodb_new_table(ctx, resource_snapshot, actual_region, kms_key_arn)


def _restore_dynamodb_in_place(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    stack_table_match: Dict[str, str],
) -> Tuple[str, dict]:
    """Restore DynamoDB data into an existing pre-created table."""
    resource_id = resource_snapshot["resourceId"]
    snapshot_id = resource_snapshot["snapshotId"]

    restore_target_region = stack_table_match["region"]
    table_name = stack_table_match["tableName"]

    # Get KMS key for the stack table's region (may be different from target_region)
    stack_kms_key_arn = ctx.kms_key_arns_by_region.get(restore_target_region)
    if not stack_kms_key_arn:
        raise ValueError(f"No KMS key available for region {restore_target_region} (required for in-place restore)")

    # Scale up table WCU before restore to maximize write throughput
    allocated_wcu = ctx.dynamodb_wcu_allocation.get(resource_id, 0)
    original_settings = _scale_up_dynamodb_table_wcu(
        table_name=table_name,
        region=restore_target_region,
        allocated_wcu=allocated_wcu,
        credentials=ctx.restore_account_credentials,
    )

    print(f"DynamoDB IN-PLACE restore config - region: {restore_target_region}, table: {table_name}, WCU: {allocated_wcu:,} (CloudFormation stack: {stack_table_match['stackName']})")

    restored_resource_details = {
        "restoredRegion": restore_target_region,
        "restoredName": table_name,
        "restoreType": "IN_PLACE",
        # The existing-table restore API takes no restore method — data is always
        # written through the capacity-based path.
        "restoreMethod": RESTORE_METHOD_CAPACITY_BASED,
        "recoveryStackName": stack_table_match["stackName"],
        "writeCapacityUnits": allocated_wcu,
        "originalTableSettings": original_settings,
    }
    # Warm throughput is applied by the monitor loop once the table is ACTIVE.
    # DynamoDB pre-allocates partitions asynchronously after the API call.
    # A table with no data to write back has no partitions worth pre-warming.
    if ctx.dynamodb_warm_throughput and allocated_wcu > 0:
        restored_resource_details["warmThroughputTarget"] = allocated_wcu
        restored_resource_details["warmThroughputApplied"] = False

    try:
        job_id = ctx.eon_client.restore_dynamodb_to_existing_table(
            resource_id=resource_id,
            snapshot_id=snapshot_id,
            restore_account_id=ctx.eon_restore_account_id,
            table_name=table_name,
            region=restore_target_region,
            encryption_key_id=stack_kms_key_arn  # EBS encryption for restore worker
        )
    except Exception:
        # Eon API failed — rollback WCU immediately to avoid lingering elevated throughput
        print(f"Eon API call failed for {table_name} — rolling back WCU")
        _restore_dynamodb_table_wcu_immediate(
            table_name=table_name,
            region=restore_target_region,
            original_settings=original_settings,
            credentials=ctx.restore_account_credentials,
        )
        raise

    return job_id, restored_resource_details


def _restore_dynamodb_new_table(
    ctx: _RestoreContext,
    resource_snapshot: Dict[str, Any],
    actual_region: str,
    kms_key_arn: str,
) -> Tuple[str, dict]:
    """Restore DynamoDB data into a newly created table."""
    resource_id = resource_snapshot["resourceId"]
    resource_name = resource_snapshot["resourceName"]
    snapshot_id = resource_snapshot["snapshotId"]
    snapshot_point_in_time = resource_snapshot.get("snapshotPointInTime", "Unknown")

    planned = ctx.dynamodb_restore_methods.get(
        resource_id, {"method": RESTORE_METHOD_CAPACITY_BASED, "reasons": []}
    )
    restore_method = planned["method"]
    use_import = restore_method == RESTORE_METHOD_IMPORT_TABLE

    # Merge original tags with restore tags (restore tags take precedence)
    original_tags = resource_snapshot.get("originalTags", {})
    dynamodb_tags = {
        **original_tags,
        "ManagedBy": "EonBulkRecovery",
        "eon:restore": "true",
        "eon:snapshot_id": snapshot_id,
        "eon:snapshot_time": snapshot_point_in_time
    }

    restored_table_name = get_restored_name(resource_name, ctx.resource_name_prefix)

    destination_config = {
        "awsDynamodb": {
            "restoreRegion": actual_region,
            "encryptionKeyId": kms_key_arn,
            "restoredName": restored_table_name,
            "tags": dynamodb_tags
        }
    }

    restored_resource_details = {
        "restoredRegion": actual_region,
        "restoredName": restored_table_name,
        "restoreType": "NEW_TABLE",
        "restoreMethod": restore_method,
    }
    if planned["reasons"]:
        restored_resource_details["restoreMethodReasons"] = planned["reasons"]

    if use_import:
        # ImportTable creates the table from staged S3 data with the source
        # table's own billing mode and throughput. Write capacity and warm
        # throughput play no part, and the table does not exist until the
        # import finishes.
        print(f"DynamoDB restore config - region: {actual_region}, table: {restored_table_name}, "
              f"method: ImportTable")
    else:
        allocated_wcu = ctx.dynamodb_wcu_allocation.get(resource_id, 0)
        if allocated_wcu > 0:
            destination_config["awsDynamodb"]["writeCapacityUnits"] = allocated_wcu
            restored_resource_details["writeCapacityUnits"] = allocated_wcu
            print(f"DynamoDB restore config - region: {actual_region}, table: {restored_table_name}, "
                  f"method: capacity-based, WCU: {allocated_wcu:,}")
            if ctx.dynamodb_warm_throughput:
                restored_resource_details["warmThroughputTarget"] = allocated_wcu
                restored_resource_details["warmThroughputApplied"] = False
        else:
            # The table reported no data, so there is nothing to size capacity
            # for. Omitting writeCapacityUnits lets Eon apply its own minimum,
            # and there are no partitions worth pre-warming.
            print(f"DynamoDB restore config - region: {actual_region}, table: {restored_table_name}, "
                  f"method: capacity-based, WCU: (Eon default — table reports no data)")

    job_id = ctx.eon_client.restore_dynamodb_table(
        resource_id=resource_id,
        snapshot_id=snapshot_id,
        restore_account_id=ctx.eon_restore_account_id,
        destination_config=destination_config,
        restore_method=restore_method
    )

    return job_id, restored_resource_details


# ---------------------------------------------------------------------------
# Dispatch table: resource type -> restore function
# ---------------------------------------------------------------------------

_RESTORE_DISPATCH = {
    "AWS_EC2": _initiate_ec2_restore,
    "AWS_RDS": _initiate_rds_restore,
    "AWS_S3": _initiate_s3_restore,
    "AWS_DYNAMO_DB": _initiate_dynamodb_restore,
}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Initiate restore jobs for all resource snapshots.

    Input event:
        resourceSnapshots: List of resources with snapshot IDs
        eonRestoreAccountId: Eon-assigned restore account ID
        restoreAccountId: AWS account ID of restore account
        restoreRegion: Primary region for restores
        kmsKeyArnsByRegion: Dictionary mapping region to KMS key ARN for encryption
        rdsSubnetGroupsByRegion: Dictionary mapping region to RDS subnet group name
        dynamodbRegionalWcuLimit: Regional WCU limit per region for DynamoDB (default 40000)
        dynamodbTableWcuMax: Max WCU any single table can receive (default 40000)
        dynamodbRestoreMethod: How new DynamoDB tables are rebuilt — "auto" (default,
            ImportTable where the snapshot and restore account allow it, capacity-based
            otherwise), "import" (force ImportTable), or "capacity" (force the write path)
        vpcConfigs: VPC configurations for the restore
        crossAccountRoleArn: ARN of cross-account role (optional)
        excludeEC2TagKeys: List of tag keys to exclude from EC2 instance tags (optional)
        recoveryStackNames: List of CloudFormation stack names to check for pre-created DynamoDB tables (optional)
        recoveryStacksOnly: If true, only restore resources matching a stack table/bucket (optional, default false)
        s3InPlaceTagKey: Tag key for matching S3 buckets for in-place restore (default: "eon_functional_id")

    Returns:
        restoreJobs: List of initiated restore jobs with job IDs
        totalJobs: Total number of jobs initiated
    """
    # ---- Parse event ----
    resource_snapshots = event["resourceSnapshots"]
    eon_restore_account_id = event["eonRestoreAccountId"]
    restore_account_id = event["restoreAccountId"]
    restore_region = event.get("restoreRegion", "us-east-1")
    kms_key_arns_by_region = event.get("kmsKeyArnsByRegion", {})
    rds_subnet_groups_by_region = event.get("rdsSubnetGroupsByRegion", {})
    dynamodb_regional_wcu_limit = event.get("dynamodbRegionalWcuLimit") or 40000
    dynamodb_table_wcu_max = event.get("dynamodbTableWcuMax") or 40000
    dynamodb_restore_method = (event.get("dynamodbRestoreMethod") or DYNAMODB_RESTORE_METHOD_AUTO).strip().lower()
    if dynamodb_restore_method not in DYNAMODB_RESTORE_METHOD_CHOICES:
        raise ValueError(
            f"Invalid dynamodbRestoreMethod '{dynamodb_restore_method}' — "
            f"expected one of {', '.join(DYNAMODB_RESTORE_METHOD_CHOICES)}"
        )
    vpc_configs = event.get("vpcConfigs", [])
    cross_account_role_arn = event.get("crossAccountRoleArn")
    management_account_id = os.environ.get("MANAGEMENT_ACCOUNT_ID", "").strip() or None
    exclude_ec2_tag_keys = event.get("excludeEC2TagKeys", [])
    enable_cdk_recovery_stacks = event.get("recoveryStackNames", [])
    recovery_stacks_only = event.get("recoveryStacksOnly", False)
    resource_name_prefix = event.get("resourceNamePrefix")  # None means use original name
    s3_in_place_tag_key = event.get("s3InPlaceTagKey") or "eon_functional_id"
    dynamodb_warm_throughput = event.get("dynamodbWarmThroughput", True)

    # ---- Log configuration ----
    print(f"KMS keys available in regions: {list(kms_key_arns_by_region.keys())}")
    print(f"RDS subnet groups available in regions: {list(rds_subnet_groups_by_region.keys())}")
    print(f"DynamoDB regional WCU limit: {dynamodb_regional_wcu_limit:,}")
    print(f"DynamoDB per-table WCU max: {dynamodb_table_wcu_max:,}")
    print(f"DynamoDB restore method: {dynamodb_restore_method}")
    print(f"Resource name prefix: {resource_name_prefix if resource_name_prefix else '(none - using original names)'}")
    print(f"DynamoDB warm throughput: {'enabled' if dynamodb_warm_throughput else 'disabled'}")
    if s3_in_place_tag_key != "eon_functional_id":
        print(f"S3 in-place restore tag key: {s3_in_place_tag_key}")
    if exclude_ec2_tag_keys:
        print(f"EC2 tag keys to exclude: {exclude_ec2_tag_keys}")
    if enable_cdk_recovery_stacks:
        print(f"Recovery stacks to check: {enable_cdk_recovery_stacks}")
    if recovery_stacks_only:
        print(f"Recovery stacks ONLY mode: will skip resources without a matching stack table/bucket")

    # ---- Cross-account credentials ----
    restore_account_credentials = None
    try:
        restore_account_credentials = get_cross_account_credentials(
            restore_account_id=restore_account_id,
            cross_account_role_arn=cross_account_role_arn,
            management_account_id=management_account_id
        )
        print(f"Successfully obtained cross-account credentials for restore account {restore_account_id}")
    except Exception as e:
        print(f"WARNING: Could not obtain cross-account credentials: {str(e)}")
        print("Will attempt operations with Lambda execution role")

    # ---- Discover recovery-stack resources ----
    vpc_regions = [config.get("region") for config in vpc_configs if config.get("region")] or ["us-east-1"]

    recovery_stack_tables = {}
    recovery_stack_s3_buckets = {}
    if enable_cdk_recovery_stacks:
        recovery_stack_tables = discover_dynamodb_tables_from_stacks(
            stack_names=enable_cdk_recovery_stacks,
            restore_account_credentials=restore_account_credentials,
            regions=vpc_regions
        )
        recovery_stack_s3_buckets = discover_s3_buckets_from_stacks(
            stack_names=enable_cdk_recovery_stacks,
            restore_account_credentials=restore_account_credentials,
            regions=vpc_regions,
            s3_in_place_tag_key=s3_in_place_tag_key,
        )

    # ---- Build per-region lookups ----
    vpc_configs_by_region = {}
    for config in vpc_configs:
        region = config.get("region", restore_region)
        vpc_configs_by_region[region] = config

    # ---- Initialize Eon client ----
    credentials = get_eon_credentials()
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # ---- Classify DynamoDB tables in scope ----
    # In recoveryStacksOnly mode, only tables that match a recovery stack are
    # restored at all; the rest are dropped here so they neither get a restore
    # method nor dilute the WCU budget.
    dynamodb_in_scope: List[Dict[str, Any]] = []
    skipped_non_stack = 0
    for snapshot in resource_snapshots:
        if snapshot.get("resourceType") != "AWS_DYNAMO_DB":
            continue

        source_region = snapshot.get("region")
        resource_name = snapshot["resourceName"]

        matched_stack_table = None
        if recovery_stack_tables:
            matched_stack_table = stack_table_match(recovery_stack_tables, resource_name, source_region)
        if recovery_stacks_only and recovery_stack_tables and not matched_stack_table:
            skipped_non_stack += 1
            continue

        target_region = restore_region if restore_region else source_region

        # Determine actual region (same logic as restore section)
        actual_region = target_region if target_region in vpc_configs_by_region else list(vpc_configs_by_region.keys())[0] if vpc_configs_by_region else None

        if not actual_region:
            continue

        dynamodb_in_scope.append({
            "resourceId": snapshot["resourceId"],
            "resourceName": resource_name,
            "restoredName": get_restored_name(resource_name, resource_name_prefix),
            "snapshotId": snapshot["snapshotId"],
            # In-place restores go into the stack table's region, which can differ
            # from the region a new table would be created in.
            "region": matched_stack_table["region"] if matched_stack_table else actual_region,
            "sizeBytes": snapshot.get("tableSizeBytes", 0),
            "inPlace": bool(matched_stack_table),
        })

    if skipped_non_stack:
        print(f"DynamoDB: skipped {skipped_non_stack} tables not matching recovery stacks (recoveryStacksOnly mode)")

    # ---- Choose a restore method per new table ----
    dynamodb_restore_methods = plan_dynamodb_restore_methods(
        eon_client=eon_client,
        dynamodb_candidates=[table for table in dynamodb_in_scope if not table["inPlace"]],
        requested_method=dynamodb_restore_method,
        eon_restore_account_id=eon_restore_account_id,
        restore_account_id=restore_account_id,
    )

    # ---- Pre-compute DynamoDB WCU allocation ----
    # ImportTable restores create the table from staged S3 data and never touch
    # the account's write capacity, so they are left out of the budget — including
    # them would starve the tables that do write through the DynamoDB API.
    dynamodb_tables_by_region: Dict[str, List[Dict[str, Any]]] = {}
    for table in dynamodb_in_scope:
        planned = dynamodb_restore_methods.get(table["resourceId"], {})
        if planned.get("method") == RESTORE_METHOD_IMPORT_TABLE:
            continue

        dynamodb_tables_by_region.setdefault(table["region"], []).append({
            "resourceId": table["resourceId"],
            "resourceName": table["resourceName"],
            "sizeBytes": table["sizeBytes"],
        })

    dynamodb_wcu_allocation = calculate_dynamodb_wcu_allocation_by_region(
        dynamodb_tables_by_region=dynamodb_tables_by_region,
        regional_wcu_capacity=dynamodb_regional_wcu_limit,
        utilization_percentage=0.95,
        default_wcu_for_zero_size=0,
        table_wcu_max=dynamodb_table_wcu_max,
    )

    # ---- Build shared context ----
    ctx = _RestoreContext(
        eon_client=eon_client,
        eon_restore_account_id=eon_restore_account_id,
        restore_account_id=restore_account_id,
        restore_region=restore_region,
        kms_key_arns_by_region=kms_key_arns_by_region,
        rds_subnet_groups_by_region=rds_subnet_groups_by_region,
        vpc_configs_by_region=vpc_configs_by_region,
        restore_account_credentials=restore_account_credentials,
        resource_name_prefix=resource_name_prefix,
        exclude_ec2_tag_keys=exclude_ec2_tag_keys,
        recovery_stack_tables=recovery_stack_tables,
        recovery_stack_s3_buckets=recovery_stack_s3_buckets,
        recovery_stacks_only=recovery_stacks_only,
        dynamodb_wcu_allocation=dynamodb_wcu_allocation,
        dynamodb_restore_methods=dynamodb_restore_methods,
        dynamodb_warm_throughput=dynamodb_warm_throughput,
        s3_in_place_tag_key=s3_in_place_tag_key,
    )

    # ---- Initiate restores ----
    print(f"\nInitiating restore jobs for {len(resource_snapshots)} snapshots")
    print(f"VPC configurations available in regions: {list(vpc_configs_by_region.keys())}")

    restore_jobs = []

    for resource_snapshot in resource_snapshots:
        resource_id = resource_snapshot["resourceId"]
        resource_name = resource_snapshot["resourceName"]
        resource_type = resource_snapshot["resourceType"]
        snapshot_id = resource_snapshot["snapshotId"]
        snapshot_point_in_time = resource_snapshot.get("snapshotPointInTime", "Unknown")
        source_region = resource_snapshot.get("region")

        target_region = restore_region if restore_region else source_region

        print(f"Initiating restore for {resource_type}: {resource_name} (source region: {source_region}, target region: {target_region})")

        # In recoveryStacksOnly mode, skip resource types that don't come from stacks
        if recovery_stacks_only and resource_type in ("AWS_EC2", "AWS_RDS"):
            print(f"SKIPPING {resource_name} ({resource_type}) - recoveryStacksOnly mode, no stack matching for this type")
            continue

        restore_fn = _RESTORE_DISPATCH.get(resource_type)

        try:
            result = None
            if restore_fn:
                result = restore_fn(ctx, resource_snapshot, target_region)

            if result is None and restore_fn:
                # Resource was intentionally skipped (e.g. recoveryStacksOnly)
                continue

            job_id = None
            restored_resource_details = {}
            if result:
                job_id, restored_resource_details = result

            if job_id:
                restore_jobs.append({
                    "jobId": job_id,
                    "resourceId": resource_id,
                    "resourceName": resource_name,
                    "resourceType": resource_type,
                    "snapshotId": snapshot_id,
                    "snapshotPointInTime": snapshot_point_in_time,
                    "sourceRegion": source_region,
                    "status": "INITIATED",
                    **restored_resource_details
                })
                print(f"Successfully initiated restore job {job_id} for {resource_name}")

                # Pause 5 seconds between job initiations to avoid overloading the API
                print("Pausing 5 seconds before next job...")
                time.sleep(5)
            else:
                print(f"WARNING: No job ID returned for {resource_name}")

        except Exception as e:
            print(f"ERROR: Failed to initiate restore for {resource_name}: {str(e)}")
            restore_jobs.append({
                "jobId": None,
                "resourceId": resource_id,
                "resourceName": resource_name,
                "resourceType": resource_type,
                "snapshotId": snapshot_id,
                "snapshotPointInTime": snapshot_point_in_time,
                "sourceRegion": source_region,
                "status": "FAILED_TO_INITIATE",
                "error": str(e)
            })
            continue

    print(f"Successfully initiated {len([j for j in restore_jobs if j['jobId']])} restore jobs")

    return {
        "restoreJobs": restore_jobs,
        "totalJobs": len(restore_jobs),
        "successfulJobs": len([j for j in restore_jobs if j["jobId"]]),
        "failedJobs": len([j for j in restore_jobs if not j["jobId"]])
    }
