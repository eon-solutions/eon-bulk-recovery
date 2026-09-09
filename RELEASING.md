# Releasing

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and are recorded in
[`CHANGELOG.md`](CHANGELOG.md), which is the source of truth. A release is a CHANGELOG heading
plus a matching git tag.

## What the version is a promise about

This is a deployed application, not a library, so "the public API" is the surface an operator
touches:

1. **The Step Functions execution input.** Every key in `example-execution-input.json`.
2. **The CloudFormation parameters** in `template.yaml`.
3. **The restore behaviour an operator relies on** — which resources get restored, where, and
   under what names.
4. **The completion notification** — its subjects and the fields it reports.

Internal refactors, test changes, and log wording are not part of that promise.

## Choosing the bump

| Bump | When | Examples |
|------|------|----------|
| **MAJOR** | An existing execution input stops working, or a restore's observable outcome changes for someone who changed nothing | A new **required** input key; renaming or removing a key; changing a default such that resources land in a different account, region, or name; removing a template parameter |
| **MINOR** | New capability, and every input that worked before still works | A new **optional** input key; a new resource type; a new notification section; a new default that does not change where or under what name anything lands |
| **PATCH** | A fix with no interface change | A wrong field name, a swallowed exception, a miscount in the report |

Two rules that decide most of the hard cases here:

- **A new execution-input key is MAJOR unless it is genuinely optional.** The state machine
  references input keys by JSONPath, so a key it names must be present or Step Functions fails at
  runtime with a states-path error. Adding one to the ASL breaks every saved input. To ship a new
  knob as MINOR, give it a default the workflow applies when the key is absent.
- **Changing a default that alters the restore mechanism is MINOR; changing one that alters the
  result is MAJOR.** Restoring the same data by a different internal path is a MINOR change.
  Restoring it to a different name, region, or account is MAJOR.

When a release mixes bump levels, the highest one wins.

## Cutting a release

1. **Pass the development gate first.** A release is never the first time a change is deployed and
   validated.
2. **Settle the version** against the table above, reading every line accumulated under
   `## [Unreleased]`.
3. **Close the section.** Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`. Keep the
   `Added` / `Changed` / `Fixed` grouping and drop any empty group.
4. **Call out breaking changes explicitly.** A MAJOR release opens with a `### Breaking` group
   that says what an operator must change, not just what moved:

   ```markdown
   ### Breaking

   - **`dynamodbRestoreMethod` is now a required execution input.** Add
     `"dynamodbRestoreMethod": "auto"` to any saved execution input; without it the workflow
     fails at the `Initiate Restores` state.
   ```

5. **Commit** the CHANGELOG on its own: `Release X.Y.Z`.
6. **Tag and push:**

   ```bash
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin main --follow-tags
   ```

7. **Open a GitHub release** on the tag, body copied from the CHANGELOG section.

## Between releases

Every merged change adds its line under `## [Unreleased]` in the same commit. Do not defer this to
release day — the point of the CHANGELOG is that whoever cuts the release does not have to
reconstruct intent from the git log.
