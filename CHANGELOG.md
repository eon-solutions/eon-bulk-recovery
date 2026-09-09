# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-09-09

### Added

- **`dynamodbRestoreMethod` execution input.** Selects how new DynamoDB tables are rebuilt:
  `"auto"` (default), `"import"`, or `"capacity"`. Optional — the state machine applies the
  default when the key is absent, so an execution input written before this release still runs.

### Changed

- **DynamoDB tables now restore via AWS `ImportTable` by default.** Eon stages the snapshot as
  DynamoDB JSON in S3 and AWS creates, loads and indexes the table in one pass, instead of
  writing every item back through the DynamoDB write API. Under `"auto"` each snapshot is
  checked before submission and falls back to the capacity-based path when the table has local
  secondary indexes, exceeds the AWS import size limit for the region (15 TiB in
  `us-east-1`/`us-west-1`/`us-west-2`, 1 TiB elsewhere), or the restore account's Eon role is
  older than 1.8.1. The reason is logged and recorded on the job. In-place restores into
  recovery-stack tables are unaffected: `ImportTable` only creates new tables.
- **WCU allocation covers only the tables that write through the DynamoDB API.** ImportTable
  restores consume no write capacity, so including them in the regional budget starved the
  tables that fall back to the capacity-based path. In-place tables are now also counted against
  the region they are restored into rather than the region a new table would have used.
- **Warm throughput is skipped for ImportTable restores.** The table does not exist until the
  import completes, so there are no partitions to pre-allocate.
- **An all-rejected run now sends subject `Eon Bulk Recovery - REJECTED`** instead of `- FAILURE`,
  because nothing ran and the fix-and-re-run remedy is different. If you filter notification mail
  on the exact subject, add the new value.
- **Optional execution-input keys can now carry a default.** The state machine starts with an
  `Apply Input Defaults` / `Normalize Input` pair that layers the supplied input over a defaults
  object, so a key the ASL references by JSONPath no longer has to be present. Only
  `dynamodbRestoreMethod` is defaulted today; every other key is still required.
- **A DynamoDB table reporting 0 bytes gets no WCU allocation.** There is nothing to write back,
  so the restore request omits `writeCapacityUnits` and Eon applies its own minimum. Previously
  the sized tables consumed 100% of the regional budget and every 0-byte table landed on 1 WCU
  anyway; now the budget goes entirely to the tables that have data.

### Fixed

- **The restore role version was never read, so ImportTable never fired.** `get_restore_role_version`
  looked for a flat `installedVersion`; a RestoreAccount carries it at `version.installed`. The
  read returned `None` for every account, which the viability check treats as "below 1.8.1", so
  `auto` silently fell back to capacity-based everywhere.
- **Rejected jobs are now reported as rejected, with their cause.** `JOB_REJECTED` (Eon refusing
  to start a job, typically a permissions precondition) was folded into the failed count and the
  Eon `errorCode` was dropped entirely, so the notification said "restore jobs failed" and never
  named the thing to fix. The job status now carries `errorCode`, the notification prints it, an
  all-rejected run gets a `REJECTED` subject telling you to fix and re-run, and mixed runs say how
  many of the failures were rejections.
- **A `resourceTypes` list containing only blanks no longer widens the run.** `["", "  "]`
  normalised to `[]`, which dropped the server-side type filter and pulled in every resource
  type in the account, including ones the workflow cannot restore. It now means the same as
  omitting the field.
- **The completion notification's duration no longer reads `Unknown`.** `send_completion_notification`
  used `datetime.utcnow()`, which is deprecated and raises under `-W error`; the surrounding
  `except Exception` swallowed it and fell through to `Unknown`. Now uses timezone-aware
  `datetime.now(timezone.utc)` and treats a naive `startTime` as UTC.

### Added (development)

- **Test suite.** `pytest` under `tests/`, 510 tests at 99% branch coverage, gated at 98%. No
  test touches AWS or the Eon API. `pip install -r requirements-dev.txt && pytest`.
- **CI.** `.github/workflows/test.yml` runs the suite plus `sam validate --lint` on push and PR.

## [1.1.0] - 2026-08-31

### Added

- **`resourceTypes` execution input.** Restricts a run to a subset of `AWS_EC2`, `AWS_RDS`,
  `AWS_S3`, `AWS_DYNAMO_DB`. Omitted or `[]` means all of them. An unrecognised type fails the
  run at the `List Resources` step instead of restoring nothing.
- **`resourceIds` execution input.** Restricts a run to specific resources. Accepts Eon resource
  IDs (UUIDs), cloud provider resource IDs (`i-…`, bucket or table names), or a mix. IDs that
  match nothing are logged as a warning so a typo is distinguishable from a resource with no
  backups.
- **`snapshotDate: "latest"`.** Explicit sentinel for "each resource's most recent snapshot",
  equivalent to `null`. A malformed date now fails at the input rather than partway through
  snapshot selection.
- **Reporting for resources with nothing to restore.** `Get Snapshots` records every resource it
  skips and why, including the resource's latest available snapshot time when a pinned date
  misses. The reasons cover a missing snapshot, a snapshot with no EC2 properties or no volumes,
  and a failed lookup.
- **`No Snapshots Found` terminal state.** When resources are in scope but none of them have a
  snapshot for the requested date, the workflow sends a `NO SNAPSHOTS FOUND` notification listing
  the affected resources and stops, instead of initiating zero jobs and reporting success.
- **`AWS::DynamoDB::GlobalTable` in recovery-stack discovery.** CDK's `TableV2` construct emits
  this resource type, and matching only `AWS::DynamoDB::Table` silently missed those tables, so
  they were restored as new tables or skipped under `recoveryStacksOnly`. Replica regions are read
  from `DescribeTable`, so a global table matches a source resource in any of its regions and is
  restored into the stack's own region.
- MPL-2.0 license.

### Changed

- **Monitoring ceiling raised from 30 to 60 hours** (`MAX_MONITORING_ITERATIONS` 360 → 720).
- **Monitoring timeout now terminates the workflow.** On reaching the ceiling with jobs still
  running, the monitor sends one `TIMEOUT` notification and the state machine ends in
  `Monitoring Timed Out`. It previously kept polling and re-sending the timeout notification every
  five minutes.
- **Completion notification carries skipped resources.** Adds a `No Snapshot Available` count to
  the job summary and a `Resources Without a Snapshot (not restored)` section with a reason per
  resource. A run where every job succeeded but some resources were skipped now reports
  `PARTIAL SUCCESS` rather than `SUCCESS`.
- **Resource type filtering moved server-side.** `resourceType` is applied by the Eon API rather
  than after pagination, which keeps the Step Functions payload under its 256 KB limit on large
  accounts.

### Fixed

- **Resource ID filters are routed by what the API accepts, not by the shape of the ID.** DynamoDB
  resources have a UUID as their `providerResourceId`, so a UUID cannot be assumed to be an Eon
  resource ID. The `providerResourceId` filter matches any string; the `id` filter is parsed as a
  UUID server-side and rejects anything else. Every supplied ID is now queried as a provider ID,
  UUID-shaped ones are additionally queried as Eon IDs, and the results are unioned.

## [1.0.0] - 2026-07-10

First tagged baseline. Covers the workflow as it stood before the changes above: bootstrap of the
restore account (IAM, per-region KMS keys, RDS subnet groups), account connection and VPC
configuration in Eon, resource and snapshot enumeration, restore initiation for EC2, RDS, S3 and
DynamoDB across regions, and monitoring through to a completion notification.

Also includes the hardening released on this date:

- **Restore role existence is verified before use.** If the IAM stack exists but its restore role
  is missing (deleted out of band, or a prior create rolled back), the workflow stops with an
  actionable error rather than handing Eon a role ARN it cannot assume.
- **Connect retries against newly installed roles.** A restore account already registered as
  `DISCONNECTED` or `INSUFFICIENT_PERMISSIONS` is reconnected and polled for `CONNECTED`, giving
  roles installed moments earlier by bootstrap time to propagate.
- `JOB_REJECTED` and `JOB_SKIPPED` are handled as terminal job statuses.

[1.1.0]: https://github.com/eon-solutions/eon-bulk-recovery/releases/tag/v1.1.0
[1.0.0]: https://github.com/eon-solutions/eon-bulk-recovery/releases/tag/v1.0.0
