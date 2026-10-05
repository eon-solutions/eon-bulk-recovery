"""Lambda handler for bootstrapping the restore account."""

import os
import re
import sys
import json
from typing import Dict, Any
import boto3
import requests
from botocore.exceptions import ClientError

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.aws_utils import get_cross_account_credentials

# Eon publishes the current restore-account template here; the console hands the
# same URL to customers onboarding by hand.
RESTORE_ACCOUNT_TEMPLATE_URL = (
    "https://eon-public-b2b628cc-1d96-4fda-8dae-c3b1ad3ea03b.s3.amazonaws.com/restore-account.yml"
)

_CF_VERSION_TAG = re.compile(r'Key:\s*"eon:cf_version"\s*\n\s*Value:\s*"([^"]+)"')


def _template_version(template_body: str) -> str:
    """Return the eon:cf_version the template stamps on its role, or 'unknown'."""
    match = _CF_VERSION_TAG.search(template_body)
    return match.group(1) if match else "unknown"


def ensure_rds_service_linked_role(credentials: Dict[str, Any]) -> None:
    """Ensure the AWSServiceRoleForRDS service-linked role exists in the restore account.

    The workflow's first RDS call in the restore account is CreateDBSubnetGroup.
    That call *requires* the AWSServiceRoleForRDS service-linked role but does NOT
    create it — only RDS provisioning calls (CreateDBInstance,
    RestoreDBInstanceFromDBSnapshot, ...) auto-create it. On an account that has
    never used RDS the SLR is absent, so CreateDBSubnetGroup fails with
    "Missing necessary credentials". Create it explicitly here (idempotent:
    ignore the error when it already exists).
    """
    iam_client = boto3.client(
        "iam",
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )
    try:
        iam_client.create_service_linked_role(AWSServiceName="rds.amazonaws.com")
        print("Created AWSServiceRoleForRDS service-linked role")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        # RDS returns InvalidInput when the SLR already exists.
        if error_code in ("InvalidInput", "EntityAlreadyExists"):
            print("AWSServiceRoleForRDS service-linked role already exists")
        else:
            # Non-fatal: if the SLR is genuinely missing, the CreateDBSubnetGroup
            # call below surfaces a clear error.
            print(f"WARNING: Could not create AWSServiceRoleForRDS service-linked role: {e}")


def _extract_role_arn(cfn_client, stack_name: str, restore_account_id: str) -> str:
    """Return the EonRestoreAccountRoleArn from stack outputs, or the conventional ARN."""
    response = cfn_client.describe_stacks(StackName=stack_name)
    outputs = response["Stacks"][0].get("Outputs", [])
    for output in outputs:
        if output["OutputKey"] == "EonRestoreAccountRoleArn":
            return output["OutputValue"]
    # No output (older template or failed create) — fall back to the conventional name.
    return f"arn:aws:iam::{restore_account_id}:role/EonRestoreAccountRole"


def _create_restore_stack(
    cfn_client,
    stack_name: str,
    template_url: str,
    eon_account_id: str,
    restore_account_id: str,
) -> str:
    """Create the Eon restore-account IAM stack, wait for completion, and return the role ARN.

    The template goes to CloudFormation by URL, never inline: CreateStack caps an
    inline TemplateBody at 51,200 bytes and Eon's published restore-account.yml
    is larger than that. A template read from S3 by URL may be up to 1 MB.
    """
    cfn_client.create_stack(
        StackName=stack_name,
        TemplateURL=template_url,
        Parameters=[
            {"ParameterKey": "EonAccountId", "ParameterValue": eon_account_id}
        ],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Tags=[
            {"Key": "ManagedBy", "Value": "EonBulkRecovery"},
            {"Key": "RestoreAccountId", "Value": restore_account_id}
        ]
    )

    waiter = cfn_client.get_waiter("stack_create_complete")
    waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})

    return _extract_role_arn(cfn_client, stack_name, restore_account_id)


def _restore_role_exists(iam_client, role_arn: str) -> bool:
    """Check whether the IAM role named in *role_arn* still exists in the restore account.

    The bulk recovery workflow trusts the CloudFormation stack outputs for the
    restore role ARN, but the stack existing does NOT guarantee the role does: it
    can be deleted out-of-band, or a prior create can have rolled back and left the
    stack without a usable role. If Eon is then handed an ARN it cannot assume, the
    connect step fails with INSUFFICIENT_PERMISSIONS.

    Returns True if the role is present. Returns False ONLY when IAM explicitly
    reports the role does not exist — on any other error (e.g. AccessDenied,
    throttling) it returns True, so an ambiguous signal never triggers a
    destructive stack recreation.
    """
    role_name = role_arn.split(":role/")[-1].split("/")[-1]
    try:
        iam_client.get_role(RoleName=role_name)
        return True
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchEntity", "NoSuchEntityException"):
            print(f"Restore role {role_name} does not exist in the restore account")
            return False
        print(f"WARNING: Could not verify restore role {role_name} ({error_code}); assuming it exists")
        return True


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Bootstrap the restore account with necessary IAM permissions, RDS subnet groups, and KMS keys.

    Input event:
        restoreAccountId: AWS account ID of the restore account
        restoreRegion: Primary region for the restore (default: us-east-1)
        vpcConfigs: List of VPC configurations (optional)
            Each config contains:
                region: AWS region
                vpc: VPC ID
                subnetsPerAvailabilityZone: List of {availabilityZone, subnetId}
        crossAccountRoleArn: ARN of role to assume in restore account (optional)

    Returns:
        roleArn: ARN of the created Eon restore role
        kmsKeyArnsByRegion: Dictionary mapping region to KMS key ARN
        rdsSubnetGroupsByRegion: Dictionary mapping region to RDS subnet group name
    """
    restore_account_id = event["restoreAccountId"]
    restore_region = event.get("restoreRegion", "us-east-1")
    vpc_configs = event.get("vpcConfigs", [])
    cross_account_role_arn = event.get("crossAccountRoleArn")
    eon_account_id = os.environ["EON_ACCOUNT_ID"]
    management_account_id = os.environ.get("MANAGEMENT_ACCOUNT_ID", "").strip()

    # Get credentials for restore account with role chaining support
    credentials = get_cross_account_credentials(
        restore_account_id=restore_account_id,
        cross_account_role_arn=cross_account_role_arn,
        management_account_id=management_account_id if management_account_id else None
    )

    # Create AWS clients using the cross-account credentials
    cfn_client = boto3.client(
        "cloudformation",
        region_name=restore_region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"]
    )

    # 1. Deploy IAM CloudFormation stack
    stack_name = f"eon-restore-account-{restore_account_id}"

    # CloudFormation reads the template straight from Eon's bucket (see
    # _create_restore_stack). This download is a pre-flight: an unreachable or
    # withdrawn template fails the step here with an HTTP error rather than
    # inside CloudFormation, and the log names the role version being installed.
    template_url = RESTORE_ACCOUNT_TEMPLATE_URL
    print(f"Checking the published restore account template at: {template_url}")

    template_response = requests.get(template_url, timeout=30)
    template_response.raise_for_status()
    template_version = _template_version(template_response.text)

    print(f"Deploying IAM CloudFormation stack: {stack_name} (restore-account template {template_version})")

    try:
        role_arn = _create_restore_stack(
            cfn_client, stack_name, template_url, eon_account_id, restore_account_id
        )

    except ClientError as e:
        if "AlreadyExistsException" not in str(e):
            raise

        # The stack already exists. Trust it ONLY if the IAM role it manages is
        # still present. The role can be deleted out-of-band, or a prior create can
        # have rolled back, leaving a stack whose role ARN Eon cannot assume — which
        # surfaces later as an INSUFFICIENT_PERMISSIONS connect failure. When the
        # role is missing, delete and recreate the stack to rebuild it.
        print(f"Stack {stack_name} already exists, verifying the restore role still exists")
        role_arn = _extract_role_arn(cfn_client, stack_name, restore_account_id)

        iam_client = boto3.client(
            "iam",
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

        if _restore_role_exists(iam_client, role_arn):
            print(f"Restore role {role_arn} exists — reusing existing stack")
        else:
            # CloudFormation has no "repair" operation: an update with the same
            # template is a no-op (it doesn't notice out-of-band deletions), drift
            # detection only reports, and rollback-state stacks can't be updated at
            # all. Rebuilding the role would mean deleting and recreating the stack,
            # which this role is intentionally not permitted to do. Fail with a
            # clear, actionable message instead of handing Eon an unassumable ARN.
            role_name = role_arn.split(":role/")[-1].split("/")[-1]
            raise ValueError(
                f"CloudFormation stack '{stack_name}' exists but its restore role "
                f"'{role_name}' is missing from account {restore_account_id} (deleted "
                f"out-of-band, or a prior create rolled back). Eon cannot assume the role, "
                f"so the restore would fail at connect. Delete stack '{stack_name}' in "
                f"account {restore_account_id} and re-run this workflow to rebuild the role."
            )

    # 2. Create RDS subnet groups for all regions in VPC configs
    rds_subnet_groups_by_region = {}

    if vpc_configs:
        # CreateDBSubnetGroup requires the AWSServiceRoleForRDS service-linked
        # role to exist; create it first on accounts that have never used RDS.
        ensure_rds_service_linked_role(credentials)

        print(f"Creating RDS subnet groups for {len(vpc_configs)} regions")

        for config in vpc_configs:
            region = config.get("region", restore_region)
            vpc_id = config.get("vpc")
            subnets_per_az = config.get("subnetsPerAvailabilityZone", [])
            subnet_ids = [subnet["subnetId"] for subnet in subnets_per_az if "subnetId" in subnet]

            if not subnet_ids:
                print(f"WARNING: No subnets found for region {region}, skipping RDS subnet group creation")
                continue

            # Create RDS client for this region
            rds_client = boto3.client(
                "rds",
                region_name=region,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"]
            )

            subnet_group_name = f"eon-restore-{restore_account_id}-{region}"
            print(f"Creating RDS subnet group in {region}: {subnet_group_name}")

            try:
                rds_client.create_db_subnet_group(
                    DBSubnetGroupName=subnet_group_name,
                    DBSubnetGroupDescription=f"Eon bulk recovery subnet group for account {restore_account_id} in {region}",
                    SubnetIds=subnet_ids,
                    Tags=[
                        {"Key": "ManagedBy", "Value": "EonBulkRecovery"},
                        {"Key": "RestoreAccountId", "Value": restore_account_id},
                        {"Key": "Region", "Value": region}
                    ]
                )
                print(f"Successfully created RDS subnet group in {region}")
            except ClientError as e:
                if "DBSubnetGroupAlreadyExists" in str(e):
                    print(f"RDS subnet group {subnet_group_name} already exists in {region}")
                else:
                    print(f"ERROR: Failed to create RDS subnet group in {region}: {str(e)}")
                    raise

            rds_subnet_groups_by_region[region] = subnet_group_name

    # 3. Create KMS keys for encryption in each region
    kms_key_arns_by_region = {}

    # Collect all unique regions from VPC configs
    regions_to_setup = set()
    if vpc_configs:
        for config in vpc_configs:
            region = config.get("region", restore_region)
            regions_to_setup.add(region)
    else:
        # If no VPC configs, create key in restore_region
        regions_to_setup.add(restore_region)

    print(f"Creating KMS keys for encryption in {len(regions_to_setup)} region(s)")

    for region in regions_to_setup:
        print(f"Checking for existing KMS key in {region}")

        # Create KMS client for this region
        region_kms_client = boto3.client(
            "kms",
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"]
        )

        alias_name = f"alias/eon-restore-{restore_account_id}-{region}"

        # First, check if a key with this alias already exists
        existing_key_id = None
        key_needs_replacement = False

        try:
            alias_response = region_kms_client.describe_key(KeyId=alias_name)
            key_metadata = alias_response["KeyMetadata"]
            kms_key_arn = key_metadata["Arn"]
            existing_key_id = key_metadata["KeyId"]
            key_state = key_metadata["KeyState"]

            print(f"Found existing KMS key in {region}: {kms_key_arn}, state: {key_state}")

            # Check if key is in usable state
            # Key states: Enabled, Disabled, PendingDeletion, PendingImport, PendingReplicaDeletion, Unavailable
            # Only "Enabled" keys can be used for encryption/decryption
            if key_state == "Enabled":
                print(f"Using existing enabled KMS key in {region}: {kms_key_arn}")
                kms_key_arns_by_region[region] = kms_key_arn
                continue  # Key exists and is enabled, move to next region
            else:
                print(f"WARNING: Existing KMS key is in state '{key_state}', will create new key and update alias")
                key_needs_replacement = True

        except ClientError as e:
            if "NotFoundException" in str(e):
                print(f"No existing KMS key found, creating new key in {region}")
            else:
                raise

        # Key doesn't exist, create a new one.
        # Two principal types, both always resolvable so CreateKey never fails on
        # principal validation:
        #   1. Account root with kms:* — keeps the account in full control AND
        #      enables IAM-based access, so Eon's restore roles use the key through
        #      their own (via-service scoped) kms:* permissions in restore-account.yml.
        #   2. AWS service principals (via-service scoped) for service-mediated use.
        # We deliberately do NOT name the EonRestore* roles here: KMS validates that
        # every principal ARN resolves at create time, and a not-yet-created or
        # path-prefixed role would make CreateKey fail with "invalid principals".
        # Root-enables-IAM already covers those roles.
        key_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "Enable IAM User Permissions",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{restore_account_id}:root"},
                    "Action": "kms:*",
                    "Resource": "*"
                },
                {
                    "Sid": "Allow AWS Services",
                    "Effect": "Allow",
                    "Principal": {"Service": [
                        "dynamodb.amazonaws.com",
                        "rds.amazonaws.com",
                        "ec2.amazonaws.com",
                        "s3.amazonaws.com"
                    ]},
                    "Action": [
                        "kms:Decrypt",
                        "kms:Encrypt",
                        "kms:ReEncrypt*",
                        "kms:GenerateDataKey*",
                        "kms:CreateGrant",
                        "kms:DescribeKey"
                    ],
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "kms:ViaService": [
                                f"dynamodb.{region}.amazonaws.com",
                                f"rds.{region}.amazonaws.com",
                                f"ec2.{region}.amazonaws.com",
                                f"s3.{region}.amazonaws.com"
                            ]
                        }
                    }
                }
            ]
        }

        try:
            key_response = region_kms_client.create_key(
                Description=f"Eon bulk recovery encryption key for account {restore_account_id} in {region}",
                KeyUsage="ENCRYPT_DECRYPT",
                Origin="AWS_KMS",
                MultiRegion=False,
                # The key policy grants the account root kms:* (the "Enable IAM User
                # Permissions" statement), so the account always retains control.
                # Bypass the lockout safety check because the scoped cross-account
                # role intentionally lacks kms:PutKeyPolicy. Without this, CreateKey
                # fails with "The new key policy will not allow you to update the key
                # policy in the future" under a least-privilege role (admin roles such
                # as OrganizationAccountAccessRole have kms:* and masked this).
                BypassPolicyLockoutSafetyCheck=True,
                Policy=json.dumps(key_policy),
                Tags=[
                    {"TagKey": "ManagedBy", "TagValue": "EonBulkRecovery"},
                    {"TagKey": "RestoreAccountId", "TagValue": restore_account_id},
                    {"TagKey": "Region", "TagValue": region}
                ]
            )
            kms_key_arn = key_response["KeyMetadata"]["Arn"]
            kms_key_id = key_response["KeyMetadata"]["KeyId"]

            # Create or update alias for the new key
            if key_needs_replacement:
                # Update existing alias to point to new key
                print(f"Updating alias {alias_name} to point to new key")
                region_kms_client.update_alias(
                    AliasName=alias_name,
                    TargetKeyId=kms_key_id
                )
                print(f"Successfully replaced KMS key in {region}: {kms_key_arn}")
            else:
                # Create new alias
                region_kms_client.create_alias(
                    AliasName=alias_name,
                    TargetKeyId=kms_key_id
                )
                print(f"Successfully created KMS key in {region}: {kms_key_arn}")

            kms_key_arns_by_region[region] = kms_key_arn

        except ClientError as e:
            print(f"ERROR: Failed to create KMS key in {region}: {str(e)}")
            raise

    result = {
        "roleArn": role_arn,
        "kmsKeyArnsByRegion": kms_key_arns_by_region,
        "restoreAccountId": restore_account_id,
        "restoreRegion": restore_region,
        "rdsSubnetGroupsByRegion": rds_subnet_groups_by_region
    }

    print(f"Bootstrap complete. Created KMS keys in {len(kms_key_arns_by_region)} regions, RDS subnet groups in {len(rds_subnet_groups_by_region)} regions")

    return result
