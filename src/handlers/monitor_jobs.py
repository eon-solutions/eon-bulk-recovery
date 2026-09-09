"""Lambda handler for monitoring restore jobs and sending SNS notifications."""

import os
import sys
import json
from typing import Dict, Any, List
from datetime import datetime, timezone
import boto3

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials, get_cross_account_credentials, create_boto3_client


# Eon restore job statuses (JobStatus enum — see JobStatus.yaml in eon-service).
# Terminal statuses will not change on further polling; running statuses will.
# JOB_REJECTED (e.g. a DynamoDB table already exists with mismatched settings) and
# JOB_SKIPPED are terminal — without listing them here they fall through to the
# "unknown" branch and get treated as still-running, so the workflow polls them
# until the max-iteration timeout instead of finishing.
_SUCCESS_STATUSES = {"JOB_COMPLETED"}
_PARTIAL_STATUSES = {"JOB_PARTIAL"}
_FAILED_STATUSES = {"JOB_FAILED", "JOB_CANCELED", "JOB_REJECTED"}
_REJECTED_STATUSES = {"JOB_REJECTED"}
_SKIPPED_STATUSES = {"JOB_SKIPPED"}
_RUNNING_STATUSES = {"JOB_PENDING", "JOB_RUNNING"}

# Every status that means the job is finished. Used to trigger DynamoDB WCU
# restoration for in-place restores — a rejected/skipped in-place job must also
# have its temporarily-elevated WCU rolled back, not left stuck.
_TERMINAL_STATUSES = _SUCCESS_STATUSES | _PARTIAL_STATUSES | _FAILED_STATUSES | _SKIPPED_STATUSES

# How each DynamoDB restore method reads in the completion notification.
_DYNAMODB_METHOD_LABELS = {
    "RESTORE_METHOD_IMPORT_TABLE": "ImportTable (import from S3)",
    "RESTORE_METHOD_CAPACITY_BASED": "capacity-based",
}


def _apply_deferred_warm_throughput(
    job: Dict[str, Any],
    restore_account_credentials: Dict[str, str] = None,
) -> bool:
    """
    Apply warm throughput to a DynamoDB table during the monitor loop.

    Called for both new table restores (where the table doesn't exist yet at initiate
    time) and in-place restores (where the WCU scale-up may still be in progress).
    Once the table is ACTIVE, sets warm throughput to pre-allocate partitions and
    reduce BatchWriteItem throttling during the data restore phase.

    Returns True if warm throughput was applied (or not needed), False on failure.
    """
    warm_target = job.get("warmThroughputTarget")
    if not warm_target or job.get("warmThroughputApplied"):
        return True

    table_name = job.get("restoredName")
    region = job.get("restoredRegion")

    if not table_name or not region or not restore_account_credentials:
        return False

    try:
        dynamodb_client = create_boto3_client("dynamodb", region, restore_account_credentials)

        # Check if table exists and is ACTIVE
        try:
            desc = dynamodb_client.describe_table(TableName=table_name)
            table = desc["Table"]
            status = table.get("TableStatus")
        except Exception as e:
            if "ResourceNotFoundException" in str(type(e).__name__) or "ResourceNotFoundException" in str(e):
                print(f"Table {table_name} does not exist yet — will retry warm throughput next iteration")
                return False
            raise

        if status != "ACTIVE":
            print(f"Table {table_name} is {status} — will retry warm throughput next iteration")
            return False

        # Also check GSI statuses — can't update table while GSIs are still updating
        gsis_raw = table.get("GlobalSecondaryIndexes", [])
        updating_gsis = [g["IndexName"] for g in gsis_raw if g.get("IndexStatus") != "ACTIVE"]
        if updating_gsis:
            print(f"Table {table_name} is ACTIVE but GSIs still updating: {', '.join(updating_gsis)} — will retry next iteration")
            return False

        # Check current warm throughput — never decrease (DynamoDB doesn't allow it)
        current_warm = table.get("WarmThroughput", {})
        current_write = current_warm.get("WriteUnitsPerSecond", 0)
        current_read = current_warm.get("ReadUnitsPerSecond", 0)

        effective_write = max(current_write, warm_target)
        effective_read = max(current_read, 1)

        # Check if any GSI needs updating before deciding to skip
        needs_update = (effective_write > current_write) or (effective_read > current_read)

        gsi_updates = []
        for gsi in gsis_raw:
            gsi_warm = gsi.get("WarmThroughput", {})
            gsi_cur_write = gsi_warm.get("WriteUnitsPerSecond", 0)
            gsi_cur_read = gsi_warm.get("ReadUnitsPerSecond", 0)
            gsi_eff_write = max(gsi_cur_write, warm_target)
            gsi_eff_read = max(gsi_cur_read, 1)
            if gsi_eff_write > gsi_cur_write or gsi_eff_read > gsi_cur_read:
                needs_update = True
            gsi_updates.append({
                "Update": {
                    "IndexName": gsi["IndexName"],
                    "WarmThroughput": {
                        "ReadUnitsPerSecond": gsi_eff_read,
                        "WriteUnitsPerSecond": gsi_eff_write,
                    },
                }
            })

        if not needs_update:
            print(f"Table {table_name} warm throughput already at {current_write:,} WCU — skipping")
            return True

        # Build update request
        update_kwargs = {
            "TableName": table_name,
            "WarmThroughput": {
                "ReadUnitsPerSecond": effective_read,
                "WriteUnitsPerSecond": effective_write,
            },
        }

        # Also warm GSIs
        if gsi_updates:
            update_kwargs["GlobalSecondaryIndexUpdates"] = gsi_updates

        print(f"Setting warm throughput on {table_name}: {warm_target:,} write units/sec")
        dynamodb_client.update_table(**update_kwargs)
        print(f"Warm throughput requested for {table_name}")
        return True

    except Exception as e:
        print(f"WARNING: Failed to set warm throughput on {table_name}: {e}")
        return False


def _restore_dynamodb_table_wcu(
    job: Dict[str, Any],
    restore_account_credentials: Dict[str, str] = None,
) -> bool:
    """
    Restore a DynamoDB table's WCU to its original settings after an in-place restore.

    Returns True on success (or if nothing to do), False on failure.
    """
    original_settings = job.get("originalTableSettings", {})
    if not original_settings.get("wcuScaledUp"):
        return True

    table_name = job.get("restoredName")
    region = job.get("restoredRegion")

    if not table_name or not region:
        print(f"WARNING: Missing table name or region for WCU restoration — skipping")
        return False

    if not restore_account_credentials:
        print(f"WARNING: No cross-account credentials — cannot restore WCU for {table_name}")
        return False

    try:
        dynamodb_client = create_boto3_client("dynamodb", region, restore_account_credentials)

        # Check if table still exists
        try:
            desc = dynamodb_client.describe_table(TableName=table_name)
            table_arn = desc["Table"]["TableArn"]
        except Exception as e:
            if "ResourceNotFoundException" in str(type(e).__name__) or "ResourceNotFoundException" in str(e):
                print(f"Table {table_name} no longer exists — skipping WCU restoration")
                return True
            raise

        original_billing_mode = original_settings["originalBillingMode"]
        original_wcu = original_settings["originalWcu"]
        original_rcu = original_settings["originalRcu"]
        original_gsi_throughput = original_settings.get("originalGsiThroughput", {})

        update_kwargs = {"TableName": table_name}

        if original_billing_mode == "PAY_PER_REQUEST":
            # Switch back to on-demand — GSIs switch automatically
            update_kwargs["BillingMode"] = "PAY_PER_REQUEST"
            print(f"Restoring {table_name} to PAY_PER_REQUEST billing mode")
        else:
            # Restore original provisioned throughput
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
            print(f"Restoring {table_name} to original throughput: {original_wcu:,} WCU, {original_rcu:,} RCU")

        dynamodb_client.update_table(**update_kwargs)

        # Clean up idempotency tags
        tag_keys = ["eon:original_billing_mode", "eon:original_wcu", "eon:original_rcu", "eon:original_gsi_throughput"]
        tag_keys += [f"eon:original_gsi:{gsi_name}" for gsi_name in original_gsi_throughput]
        try:
            dynamodb_client.untag_resource(ResourceArn=table_arn, TagKeys=tag_keys)
        except Exception as e:
            print(f"WARNING: Could not remove eon:original_* tags from {table_name}: {e}")

        print(f"Successfully restored WCU for {table_name}")
        return True

    except Exception as e:
        print(f"ERROR: Failed to restore WCU for {table_name} in {region}: {e}")
        print(f"Table may still have elevated WCU — manual intervention required")
        return False


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Monitor restore job status and send SNS notification when all jobs complete.

    Input event:
        restoreJobs: List of restore jobs with job IDs
        iteration: Current iteration count (for tracking polling attempts)
        sourceAccountId: Source AWS account ID
        restoreAccountId: Restore AWS account ID
        restoreRegion: Restore region
        vpcConfigs: VPC configurations used
        crossAccountRoleArn: ARN of cross-account role for DynamoDB WCU restoration (optional)
        startTime: ISO timestamp when monitoring started

    Returns:
        allComplete: Boolean indicating if all jobs are complete
        jobStatuses: Current status of all jobs
        completedJobs: Count of completed jobs
        failedJobs: Count of failed jobs
        runningJobs: Count of still-running jobs
        iteration: Incremented iteration count
    """
    restore_jobs = event["restoreJobs"]
    iteration = event.get("iteration", 0)
    source_account_id = event.get("sourceAccountId")
    restore_account_id = event.get("restoreAccountId")
    restore_region = event.get("restoreRegion")
    vpc_configs = event.get("vpcConfigs", [])
    cross_account_role_arn = event.get("crossAccountRoleArn")
    resources_without_snapshots = event.get("resourcesWithoutSnapshots", [])
    resources_without_snapshots_count = event.get("resourcesWithoutSnapshotsCount", 0)
    start_time = event.get("startTime")
    max_iterations = int(os.environ.get("MAX_MONITORING_ITERATIONS", "720"))  # 720 * 5min = 60 hours

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    # Get cross-account credentials for DynamoDB WCU restoration (fresh each invocation)
    restore_account_credentials = None
    if cross_account_role_arn and restore_account_id:
        management_account_id = os.environ.get("MANAGEMENT_ACCOUNT_ID", "").strip() or None
        try:
            restore_account_credentials = get_cross_account_credentials(
                restore_account_id=restore_account_id,
                cross_account_role_arn=cross_account_role_arn,
                management_account_id=management_account_id
            )
        except Exception as e:
            print(f"WARNING: Could not obtain cross-account credentials for WCU restoration: {e}")

    print(f"Monitoring {len(restore_jobs)} restore jobs (iteration {iteration})")

    # Track job statuses
    completed_count = 0
    failed_count = 0
    rejected_count = 0
    running_count = 0
    partial_count = 0
    skipped_count = 0
    wcu_restoration_failures = list(event.get("wcuRestorationFailures", []))

    job_statuses = []

    for job in restore_jobs:
        job_id = job.get("jobId")

        # Skip jobs that failed to initiate
        if not job_id:
            failed_count += 1
            # Restore WCU if scale-up happened but Eon API failed and inline rollback also failed
            if (job.get("originalTableSettings", {}).get("wcuScaledUp")
                    and not job.get("wcuRestored")):
                success = _restore_dynamodb_table_wcu(job, restore_account_credentials)
                job["wcuRestored"] = True
                if not success:
                    wcu_restoration_failures.append(job)
            job_statuses.append({
                "jobId": None,
                "resourceId": job.get("resourceId"),
                "resourceName": job.get("resourceName"),
                "resourceType": job.get("resourceType"),
                "currentStatus": "FAILED_TO_INITIATE"
            })
            continue

        try:
            # Get job status from Eon API
            response = eon_client.get_restore_job(job_id)
            job_details = response.get("job", {})
            job_execution = job_details.get("jobExecutionDetails", {})

            status = job_execution.get("status", "UNKNOWN")
            status_message = job_execution.get("statusMessage", "")

            job_status_record = {
                "jobId": job_id,
                "resourceId": job.get("resourceId"),
                "resourceName": job.get("resourceName"),
                "resourceType": job.get("resourceType"),
                "currentStatus": status,
                "statusMessage": status_message,
                # Eon's machine-readable cause (e.g. INSUFFICIENT_ENCRYPTION_PERMISSIONS).
                # It is the part that says what to actually go and fix, so it has to
                # reach the notification rather than being dropped here.
                "errorCode": job_execution.get("errorCode", ""),
                "startTime": job_execution.get("startTime"),
                "endTime": job_execution.get("endTime"),
                "durationSeconds": job_execution.get("durationSeconds")
            }

            # Categorize by status
            if status in _SUCCESS_STATUSES:
                completed_count += 1
            elif status in _FAILED_STATUSES:
                failed_count += 1
                # Rejected means Eon refused to start the job at all — a
                # precondition problem (usually permissions), not a restore that
                # ran and failed. Counted within failures for pass/fail, but
                # reported separately because the remediation is different.
                if status in _REJECTED_STATUSES:
                    rejected_count += 1
            elif status in _PARTIAL_STATUSES:
                partial_count += 1
                # Treat partial as a type of completion for decision purposes
                completed_count += 1
            elif status in _SKIPPED_STATUSES:
                skipped_count += 1
            elif status in _RUNNING_STATUSES:
                running_count += 1
            else:
                # Genuinely unknown status (e.g. JOB_UNSPECIFIED or a value newer
                # than this code knows about). Treat as still running so we keep
                # polling; the max-iteration timeout backstops an indefinite hang.
                print(f"Unrecognized job status '{status}' for job {job_id} — treating as running")
                running_count += 1

            # Apply warm throughput to DynamoDB tables (deferred from initiate step)
            if (job.get("resourceType") == "AWS_DYNAMO_DB"
                    and job.get("warmThroughputTarget")
                    and not job.get("warmThroughputApplied")
                    and status in ("JOB_PENDING", "JOB_RUNNING")):
                success = _apply_deferred_warm_throughput(job, restore_account_credentials)
                if success:
                    job["warmThroughputApplied"] = True

            # Restore DynamoDB WCU for terminal in-place restore jobs
            if (status in _TERMINAL_STATUSES
                    and job.get("originalTableSettings", {}).get("wcuScaledUp")
                    and not job.get("wcuRestored")):
                success = _restore_dynamodb_table_wcu(job, restore_account_credentials)
                job["wcuRestored"] = True
                if not success:
                    wcu_restoration_failures.append(job)

            job_statuses.append(job_status_record)

            print(f"Job {job_id} ({job.get('resourceName')}): {status}")

        except Exception as e:
            print(f"ERROR: Failed to get status for job {job_id}: {str(e)}")
            # Treat as still running, will check again next iteration
            running_count += 1
            job_statuses.append({
                "jobId": job_id,
                "resourceId": job.get("resourceId"),
                "resourceName": job.get("resourceName"),
                "resourceType": job.get("resourceType"),
                "currentStatus": "ERROR_CHECKING_STATUS",
                "statusMessage": str(e)
            })

    all_complete = (running_count == 0)
    timed_out = iteration >= max_iterations

    rejected_note = f" (of which {rejected_count} rejected)" if rejected_count else ""
    print(f"Job summary: {completed_count} completed, {failed_count} failed{rejected_note}, "
          f"{partial_count} partial, {skipped_count} skipped, {running_count} still running")

    result = {
        "restoreJobs": restore_jobs,  # Pass through for next iteration
        "allComplete": all_complete,
        # Set once monitoring hits the max-iteration ceiling. The state machine's
        # "Check If Complete" Choice routes to a terminal state on this flag, so the
        # monitor loop stops (and the timeout notification below fires exactly once)
        # instead of re-polling and re-notifying every 5 minutes indefinitely.
        "timedOut": timed_out,
        "jobStatuses": job_statuses,
        "completedJobs": completed_count,
        "failedJobs": failed_count,
        "rejectedJobs": rejected_count,
        "partialJobs": partial_count,
        "skippedJobs": skipped_count,
        "runningJobs": running_count,
        "totalJobs": len(restore_jobs),
        "iteration": iteration + 1,
        # Pass through context for next iteration and notification
        "sourceAccountId": source_account_id,
        "restoreAccountId": restore_account_id,
        "restoreRegion": restore_region,
        "vpcConfigs": vpc_configs,
        "crossAccountRoleArn": cross_account_role_arn,
        "wcuRestorationFailures": wcu_restoration_failures,
        # Carried through every iteration so the completion notification can report
        # resources that were in scope but had no snapshot to restore from.
        "resourcesWithoutSnapshots": resources_without_snapshots,
        "resourcesWithoutSnapshotsCount": resources_without_snapshots_count,
        "startTime": start_time
    }

    # On timeout, restore WCU for any in-place jobs still running (don't leave elevated WCU)
    if timed_out:
        for job in restore_jobs:
            if (job.get("originalTableSettings", {}).get("wcuScaledUp")
                    and not job.get("wcuRestored")):
                print(f"Timeout: restoring WCU for {job.get('resourceName')}")
                success = _restore_dynamodb_table_wcu(job, restore_account_credentials)
                job["wcuRestored"] = True
                if not success:
                    wcu_restoration_failures.append(job)

    # If all jobs are complete or max iterations reached, send SNS notification.
    # On timeout this fires exactly once because the state machine terminates the
    # monitor loop on the timedOut flag rather than looping back into Monitor Jobs.
    if all_complete or timed_out:
        send_completion_notification(result, timed_out)

    return result


def _rejected_note(rejected_jobs: int) -> str:
    """Call out rejections inside a failure count — they need different remediation."""
    return f", {rejected_jobs} of them rejected before starting" if rejected_jobs else ""


def send_completion_notification(job_summary: Dict[str, Any], timeout: bool) -> None:
    """
    Send SNS notification about bulk recovery completion.

    Args:
        job_summary: Summary of all job statuses
        timeout: Whether the monitoring timed out
    """
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")

    if not sns_topic_arn:
        print("No SNS topic ARN configured, skipping notification")
        return

    sns_client = boto3.client("sns")

    # Get Eon console domain for job links
    eon_account_domain = os.environ.get("EON_ACCOUNT_DOMAIN", "")

    total_jobs = job_summary["totalJobs"]
    completed_jobs = job_summary["completedJobs"]
    failed_jobs = job_summary["failedJobs"]
    partial_jobs = job_summary["partialJobs"]
    skipped_jobs = job_summary.get("skippedJobs", 0)
    rejected_jobs = job_summary.get("rejectedJobs", 0)
    running_jobs = job_summary["runningJobs"]
    no_snapshot_resources = job_summary.get("resourcesWithoutSnapshots", [])
    no_snapshot_count = job_summary.get("resourcesWithoutSnapshotsCount", len(no_snapshot_resources))

    # Extract context information
    source_account_id = job_summary.get("sourceAccountId", "Unknown")
    restore_account_id = job_summary.get("restoreAccountId", "Unknown")
    restore_region = job_summary.get("restoreRegion", "Unknown")
    vpc_configs = job_summary.get("vpcConfigs", [])
    start_time_str = job_summary.get("startTime")

    # Calculate duration
    duration_str = "Unknown"
    if start_time_str:
        try:
            start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            duration = datetime.now(timezone.utc) - start_time
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            duration_str = f"{hours}h {minutes}m"
        except Exception as e:
            print(f"Error calculating duration: {e}")
            duration_str = "Unknown"

    if timeout:
        subject = "Eon Bulk Recovery - TIMEOUT"
        status_summary = f"⚠️ Monitoring timed out after {job_summary['iteration']} iterations"
    elif failed_jobs == 0 and partial_jobs == 0 and skipped_jobs == 0 and no_snapshot_count == 0:
        subject = "Eon Bulk Recovery - SUCCESS"
        status_summary = f"✅ All {total_jobs} restore jobs completed successfully"
    elif failed_jobs == 0 and partial_jobs == 0 and skipped_jobs == 0:
        subject = "Eon Bulk Recovery - PARTIAL SUCCESS"
        status_summary = (f"⚠️ All {total_jobs} restore jobs completed successfully, but "
                          f"{no_snapshot_count} resource(s) had no snapshot to restore from")
    elif completed_jobs > 0 or skipped_jobs > 0:
        subject = "Eon Bulk Recovery - PARTIAL SUCCESS"
        status_summary = (f"⚠️ {completed_jobs}/{total_jobs} jobs completed "
                          f"({failed_jobs} failed{_rejected_note(rejected_jobs)}, "
                          f"{partial_jobs} partial, {skipped_jobs} skipped)")
    elif rejected_jobs == total_jobs:
        # Eon refused to start any of them. Nothing ran, so this is a
        # precondition problem to fix and re-run, not a restore that broke.
        subject = "Eon Bulk Recovery - REJECTED"
        status_summary = (f"⛔ All {total_jobs} restore jobs were rejected before they started. "
                          f"Nothing was restored. See the reasons below, fix them, and re-run.")
    else:
        subject = "Eon Bulk Recovery - FAILURE"
        status_summary = f"❌ All {total_jobs} restore jobs failed{_rejected_note(rejected_jobs)}"

    # Format VPC configuration summary (multi-region support)
    vpc_summary_lines = []
    if vpc_configs:
        configs_list = vpc_configs if isinstance(vpc_configs, list) else [vpc_configs]
        for vpc_config in configs_list:
            vpc_id = vpc_config.get("vpc", "Unknown")
            region = vpc_config.get("region", restore_region)
            subnet_count = len(vpc_config.get("subnetsPerAvailabilityZone", []))
            vpc_summary_lines.append(f"  - {vpc_id} in {region} ({subnet_count} subnets)")

    vpc_summary = "\n".join(vpc_summary_lines) if vpc_summary_lines else "None"

    # Collect snapshot date information from restore jobs
    snapshot_dates = set()
    for job in job_summary["restoreJobs"]:
        snapshot_time = job.get("snapshotPointInTime")
        if snapshot_time and snapshot_time != "Unknown":
            # Extract just the date part (YYYY-MM-DD)
            snapshot_date = snapshot_time.split("T")[0] if "T" in snapshot_time else snapshot_time
            snapshot_dates.add(snapshot_date)

    snapshot_date_summary = ", ".join(sorted(snapshot_dates)) if snapshot_dates else "Not specified"

    # Build detailed message
    message_lines = [
        "Eon Bulk Recovery Status Report",
        "=" * 50,
        "",
        status_summary,
        "",
        "Recovery Details:",
        "-" * 50,
        f"Source Account: {source_account_id}",
        f"Restore Account: {restore_account_id}",
        f"Default Restore Region: {restore_region} (resources restored to original regions)",
        f"Snapshot Date(s): {snapshot_date_summary}",
        f"VPC Configurations:",
        vpc_summary,
        f"Total Duration: {duration_str}",
        "",
        "Job Summary:",
        "-" * 50,
        f"Total Jobs: {total_jobs}",
        f"Completed: {completed_jobs}",
        f"Failed: {failed_jobs}",
        f"  of which rejected before starting: {rejected_jobs}",
        f"Partial: {partial_jobs}",
        f"Skipped: {skipped_jobs}",
        f"Still Running: {running_jobs}",
        f"No Snapshot Available: {no_snapshot_count}",
        "",
        "Job Details:",
        "-" * 50
    ]

    # Add individual job statuses
    # Need to merge job_status with original restore job to get snapshot and region info
    restore_jobs_map = {job.get("resourceId"): job for job in job_summary["restoreJobs"]}

    for job_status in job_summary["jobStatuses"]:
        resource_name = job_status.get("resourceName", "Unknown")
        resource_type = job_status.get("resourceType", "Unknown")
        status = job_status.get("currentStatus", "Unknown")
        job_id = job_status.get("jobId", "N/A")

        status_emoji = {
            "JOB_COMPLETED": "✅",
            "JOB_FAILED": "❌",
            "JOB_PARTIAL": "⚠️",
            "JOB_RUNNING": "🔄",
            "JOB_PENDING": "⏳",
            "JOB_CANCELED": "🚫",
            "JOB_REJECTED": "⛔",
            "JOB_SKIPPED": "⏭️",
            "FAILED_TO_INITIATE": "❌"
        }.get(status, "❓")

        message_lines.append(f"{status_emoji} {resource_name} ({resource_type})")
        message_lines.append(f"   Status: {status}")
        message_lines.append(f"   Job ID: {job_id}")

        # Add Eon console link if we have a valid job ID and domain
        if job_id and job_id != "N/A" and eon_account_domain:
            eon_job_url = f"https://{eon_account_domain}.console.eon.io/jobs/restore?pageIndex=0&pageSize=25&id={job_id}"
            message_lines.append(f"   Job Link: {eon_job_url}")

        # Get original restore job info for this resource
        resource_id = job_status.get("resourceId")
        if not resource_id:
            # Try to find by matching job ID
            for restore_job in job_summary["restoreJobs"]:
                if restore_job.get("jobId") == job_id:
                    resource_id = restore_job.get("resourceId")
                    break

        restore_job = restore_jobs_map.get(resource_id) if resource_id else None

        # Add snapshot and region information
        if restore_job:
            snapshot_time = restore_job.get("snapshotPointInTime")
            if snapshot_time and snapshot_time != "Unknown":
                message_lines.append(f"   Snapshot Date: {snapshot_time}")

            source_region = restore_job.get("sourceRegion")
            restored_region = restore_job.get("restoredRegion")

            if source_region and restored_region:
                if source_region == restored_region:
                    message_lines.append(f"   Region: {restored_region}")
                else:
                    message_lines.append(f"   Source Region: {source_region} → Restored Region: {restored_region}")
            elif restored_region:
                message_lines.append(f"   Restored Region: {restored_region}")

            # Add resource-specific details
            if resource_type == "AWS_EC2":
                instance_type = restore_job.get("instanceType")
                volume_count = restore_job.get("volumeCount")
                if instance_type:
                    message_lines.append(f"   Instance Type: {instance_type}")
                if volume_count:
                    message_lines.append(f"   Volumes: {volume_count}")

            elif resource_type == "AWS_RDS":
                db_instance_class = restore_job.get("dbInstanceClass")
                restored_name = restore_job.get("restoredName")
                if db_instance_class:
                    message_lines.append(f"   Instance Class: {db_instance_class}")
                if restored_name:
                    message_lines.append(f"   Restored Name: {restored_name}")

            elif resource_type == "AWS_S3":
                restored_bucket_name = restore_job.get("restoredBucketName")
                original_bucket_name = restore_job.get("originalBucketName")
                if restored_bucket_name:
                    message_lines.append(f"   Restored Bucket: {restored_bucket_name}")
                if original_bucket_name and original_bucket_name != resource_name:
                    message_lines.append(f"   Original Bucket: {original_bucket_name}")

            elif resource_type == "AWS_DYNAMO_DB":
                restored_name = restore_job.get("restoredName")
                restore_method = restore_job.get("restoreMethod")
                wcu = restore_job.get("writeCapacityUnits")
                if restored_name:
                    message_lines.append(f"   Restored Name: {restored_name}")
                if restore_method:
                    message_lines.append(
                        f"   Restore Method: {_DYNAMODB_METHOD_LABELS.get(restore_method, restore_method)}"
                    )
                if wcu:
                    message_lines.append(f"   Write Capacity Units: {wcu:,}")

        if job_status.get("errorCode"):
            message_lines.append(f"   Error Code: {job_status['errorCode']}")

        if job_status.get("statusMessage"):
            message_lines.append(f"   Message: {job_status['statusMessage']}")

        if job_status.get("durationSeconds"):
            duration_min = job_status["durationSeconds"] // 60
            message_lines.append(f"   Duration: {duration_min} minutes")

        message_lines.append("")

    # List resources that were in scope but produced no restore job, so a
    # partially-empty recovery doesn't read as a complete one.
    if no_snapshot_resources:
        message_lines.append("")
        message_lines.append("Resources Without a Snapshot (not restored):")
        message_lines.append("-" * 50)
        for entry in no_snapshot_resources:
            message_lines.append(
                f"⏭️ {entry.get('resourceName')} ({entry.get('resourceType')}) "
                f"in {entry.get('region') or 'unknown region'}"
            )
            message_lines.append(f"   Reason: {entry.get('reason')}")
        if no_snapshot_count > len(no_snapshot_resources):
            message_lines.append(
                f"... and {no_snapshot_count - len(no_snapshot_resources)} more (see CloudWatch logs)"
            )
        message_lines.append("")

    # Add ACTION REQUIRED section if any WCU restorations failed
    wcu_failures = job_summary.get("wcuRestorationFailures", [])
    if wcu_failures:
        subject += " - ACTION REQUIRED"
        message_lines.append("")
        message_lines.append("⚠️ ACTION REQUIRED: DynamoDB Throughput")
        message_lines.append("=" * 50)
        message_lines.append("")
        message_lines.append("The following tables still have elevated write throughput from the restore.")
        message_lines.append("Please manually restore their settings to avoid unexpected costs.")
        message_lines.append("")

        for failure_job in wcu_failures:
            table_name = failure_job.get("restoredName", "Unknown")
            region = failure_job.get("restoredRegion", "Unknown")
            settings = failure_job.get("originalTableSettings", {})
            original_mode = settings.get("originalBillingMode", "Unknown")
            original_wcu = settings.get("originalWcu", "Unknown")
            original_rcu = settings.get("originalRcu", "Unknown")
            allocated_wcu = failure_job.get("writeCapacityUnits", "Unknown")

            message_lines.append(f"Table: {table_name} ({region})")
            message_lines.append(f"  Current (elevated) WCU: {allocated_wcu:,}" if isinstance(allocated_wcu, int) else f"  Current (elevated) WCU: {allocated_wcu}")
            if original_mode == "PAY_PER_REQUEST":
                message_lines.append(f"  Restore to: PAY_PER_REQUEST (on-demand) billing mode")
            else:
                message_lines.append(f"  Restore to: PROVISIONED - {original_wcu:,} WCU, {original_rcu:,} RCU" if isinstance(original_wcu, int) else f"  Restore to: PROVISIONED - {original_wcu} WCU, {original_rcu} RCU")

            original_gsi = settings.get("originalGsiThroughput", {})
            if original_gsi:
                message_lines.append(f"  GSIs to restore:")
                for gsi_name, gsi_info in original_gsi.items():
                    message_lines.append(f"    {gsi_name}: {gsi_info['rcu']:,} RCU, {gsi_info['wcu']:,} WCU")
            message_lines.append("")

    message = "\n".join(message_lines)

    # Send SNS notification
    try:
        sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        print(f"Sent SNS notification to {sns_topic_arn}")
    except Exception as e:
        print(f"ERROR: Failed to send SNS notification: {str(e)}")
