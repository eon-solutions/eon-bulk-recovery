"""Lambda handler for listing backed up resources from the source account."""

import os
import re
import sys
from typing import Dict, Any, List, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.eon_client import EonClient
from lib.aws_utils import get_eon_credentials

# Resource types this application knows how to restore. Anything else in the
# source account is ignored.
SUPPORTED_RESOURCE_TYPES = ["AWS_EC2", "AWS_RDS", "AWS_S3", "AWS_DYNAMO_DB"]

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def resolve_resource_types(requested: Optional[List[str]]) -> List[str]:
    """
    Validate the caller's resourceTypes scope against what we can restore.

    An empty or absent list means "everything supported". An unsupported type is
    a hard error rather than a silent drop: a run scoped to a type that will
    never produce a restore should fail at the input, not finish with zero jobs.
    """
    if not requested:
        return list(SUPPORTED_RESOURCE_TYPES)

    normalized = [str(t).strip().upper() for t in requested if str(t).strip()]
    if not normalized:
        # A list of nothing but blanks reads as "no scope given", not "no types" —
        # returning [] here would drop the server-side filter and widen the run to
        # every resource type in the account, including ones we cannot restore.
        return list(SUPPORTED_RESOURCE_TYPES)

    unsupported = [t for t in normalized if t not in SUPPORTED_RESOURCE_TYPES]
    if unsupported:
        raise ValueError(
            f"Unsupported resourceTypes: {', '.join(sorted(set(unsupported)))}. "
            f"Supported types are: {', '.join(SUPPORTED_RESOURCE_TYPES)}"
        )

    # Preserve the canonical order and drop duplicates
    return [t for t in SUPPORTED_RESOURCE_TYPES if t in normalized]


def build_resource_id_queries(resource_ids: Optional[List[str]]) -> List[Dict[str, List[str]]]:
    """
    Turn a caller-supplied resource ID list into the API queries that will find them.

    Callers copy IDs from wherever is convenient — the Eon console shows UUIDs, the
    AWS console shows things like `i-0f600a1b15b035105` or a bucket name — so the
    input accepts either. Shape alone cannot tell them apart: DynamoDB's
    `providerResourceId` is itself a UUID. What differs is what each filter accepts.

    - `providerResourceId` is matched as a plain string, so every supplied ID can
      go through it safely.
    - `id` is parsed as a UUID server-side and returns HTTP 500 on anything else,
      so only UUID-shaped values may be sent to it.

    Hence: query every ID as a provider ID, and additionally query the UUID-shaped
    ones as Eon IDs. Results are unioned by the caller.
    """
    if not resource_ids:
        return []

    all_ids = []
    uuid_ids = []
    for raw in resource_ids:
        value = str(raw).strip()
        if not value:
            continue
        all_ids.append(value)
        if _UUID_PATTERN.match(value):
            uuid_ids.append(value)

    if not all_ids:
        return []

    queries = [{"provider_resource_ids": all_ids}]
    if uuid_ids:
        queries.append({"resource_ids": uuid_ids})
    return queries


def _fetch_all_pages(eon_client: EonClient, **filters: Any) -> List[Dict[str, Any]]:
    """Page through the resources API, accumulating every matching resource."""
    resources = []
    page_token = None

    while True:
        response = eon_client.list_resources(
            page_token=page_token,
            page_size=100,
            **filters
        )

        resources.extend(response.get("resources", []))

        page_token = response.get("nextToken")
        if not page_token:
            break

        print(f"Retrieved {len(resources)} resources so far, fetching next page...")

    return resources


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    List all protected resources from the source account.

    Input event:
        sourceAccountId: AWS account ID to list resources from
        resourceTypes: Optional list of Eon resource types to restrict the run to
            (default: every supported type)
        resourceIds: Optional list of specific resources to restrict the run to.
            Accepts Eon resource IDs (UUIDs), cloud provider resource IDs
            (e.g. `i-0f600a1b15b035105`, a bucket or table name), or a mix.

    Returns:
        resources: List of protected resources with their details
        totalCount: Total number of protected resources found
        requestedResourceIdsNotFound: Requested IDs that matched nothing
    """
    source_account_id = event["sourceAccountId"]
    resource_types = resolve_resource_types(event.get("resourceTypes"))
    requested_resource_ids = [str(r).strip() for r in (event.get("resourceIds") or []) if str(r).strip()]
    id_queries = build_resource_id_queries(requested_resource_ids)

    # Get Eon credentials
    credentials = get_eon_credentials()

    # Initialize Eon client
    eon_client = EonClient(
        account_domain=os.environ["EON_ACCOUNT_DOMAIN"],
        client_id=credentials["clientId"],
        client_secret=credentials["clientSecret"],
        project_id=os.environ["EON_PROJECT_ID"]
    )

    print(f"Listing protected resources from source account: {source_account_id}")
    print(f"Resource types in scope: {', '.join(resource_types)}")
    if requested_resource_ids:
        print(f"Restricted to {len(requested_resource_ids)} resource ID(s), "
              f"resolved with {len(id_queries)} quer(ies)")

    base_filters = {
        "source_account_id": source_account_id,
        "resource_types": resource_types,
    }

    # The API ANDs its filters, so an ID scope needs one query per filter, unioned here.
    if id_queries:
        all_resources = []
        seen_ids = set()
        for id_filter in id_queries:
            for resource in _fetch_all_pages(eon_client, **base_filters, **id_filter):
                if resource.get("id") not in seen_ids:
                    seen_ids.add(resource.get("id"))
                    all_resources.append(resource)
    else:
        all_resources = _fetch_all_pages(eon_client, **base_filters)

    print(f"Total protected resources found: {len(all_resources)}")

    # Report requested IDs that matched nothing, so a typo doesn't look like a
    # resource that simply has no backups.
    requested_ids_not_found = []
    if requested_resource_ids:
        matched = set()
        for resource in all_resources:
            matched.add(resource.get("id"))
            matched.add(resource.get("providerResourceId"))
        requested_ids_not_found = [rid for rid in requested_resource_ids if rid not in matched]
        if requested_ids_not_found:
            print(f"WARNING: {len(requested_ids_not_found)} requested resource ID(s) matched no "
                  f"backed-up resource of the types in scope: {', '.join(requested_ids_not_found)}")

    # Extract relevant information for each resource
    resource_list = []
    for resource in all_resources:
        # Extract resource properties first to get region from correct location
        resource_properties = resource.get("resourceProperties", {})

        resource_data = {
            "id": resource.get("id"),
            "resourceName": resource.get("resourceName"),
            "resourceType": resource.get("resourceType"),
            "providerResourceId": resource.get("providerResourceId"),
            "region": resource_properties.get("region") or resource.get("region"),
            "vpc": resource.get("vpc"),
            "subnets": resource.get("subnets", []),
            "latestSnapshotTime": resource.get("latestSnapshotTime")
        }

        # Extract instance type/class from resourceProperties for mirroring source configuration
        if resource.get("resourceType") == "AWS_EC2":
            resource_data["instanceType"] = resource_properties.get("instanceType")
        elif resource.get("resourceType") == "AWS_RDS":
            aws_rds = resource_properties.get("awsRds") or {}
            resource_data["dbInstanceClass"] = aws_rds.get("instanceClass")
            resource_data["engine"] = aws_rds.get("engine")
        elif resource.get("resourceType") == "AWS_DYNAMO_DB":
            source_storage = resource.get("sourceStorage", {})
            resource_data["tableSizeBytes"] = source_storage.get("sizeBytes", 0)

        # Debug: Log region extraction
        print(f"Resource {resource_data['resourceName']} ({resource_data['resourceType']}) - region: {resource_data['region']}")

        resource_list.append(resource_data)

    return {
        "resources": resource_list,
        "totalCount": len(resource_list),
        "sourceAccountId": source_account_id,
        "resourceTypes": resource_types,
        "requestedResourceIdsNotFound": requested_ids_not_found
    }
