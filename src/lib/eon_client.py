"""Eon API client for authentication and API interactions."""

import os
import json
import time
from typing import Dict, Any, Optional, List
import requests
from requests import Response


class EonClient:
    """Client for interacting with the Eon REST API."""

    def __init__(self, account_domain: str, client_id: str, client_secret: str, project_id: str):
        """
        Initialize Eon API client.

        Args:
            account_domain: Eon account domain (e.g., 'mycompany')
            client_id: Eon API client ID
            client_secret: Eon API client secret
            project_id: Eon project ID
        """
        self.api_root = f"https://{account_domain}.console.eon.io/api"
        self.base_url = f"{self.api_root}/v1"
        self.client_id = client_id
        self.client_secret = client_secret
        self.project_id = project_id
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[float] = None

    def _authenticate(self) -> None:
        """Authenticate and retrieve access token."""
        url = f"{self.base_url}/token"
        payload = {
            "clientId": self.client_id,
            "clientSecret": "***MASKED***"  # Mask secret in logs
        }

        actual_payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret
        }

        response = requests.post(url, json=actual_payload)
        self._handle_response(response, "POST", url, payload)

        data = response.json()
        self.access_token = data["accessToken"]
        # Set expiry to 11.5 hours to refresh before actual expiration
        self.token_expiry = time.time() + data.get("expirationSeconds", 43200) - 1800

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid access token."""
        if not self.access_token or not self.token_expiry or time.time() >= self.token_expiry:
            self._authenticate()

    def _get_headers(self) -> Dict[str, str]:
        """Get headers with authentication token."""
        self._ensure_authenticated()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    def _handle_response(self, response: Response, method: str, url: str, payload: Any = None) -> None:
        """
        Handle API response and raise detailed error if request fails.

        Args:
            response: Response object from requests
            method: HTTP method (GET, POST, PUT, etc.)
            url: Request URL
            payload: Request payload/body (will be masked for sensitive data)
        """
        if response.ok:
            return

        # Mask sensitive headers
        headers = dict(response.request.headers)
        if "Authorization" in headers:
            headers["Authorization"] = "Bearer ***MASKED***"

        # Build detailed error message
        error_details = {
            "method": method,
            "url": url,
            "status_code": response.status_code,
            "request_headers": headers,
        }

        if payload is not None:
            error_details["request_payload"] = payload

        try:
            error_details["response_body"] = response.json()
        except Exception:
            error_details["response_text"] = response.text

        error_msg = (
            f"Eon API request failed:\n"
            f"Method: {method}\n"
            f"URL: {url}\n"
            f"Status Code: {response.status_code}\n"
            f"Request Headers: {json.dumps(headers, indent=2)}\n"
        )

        if payload is not None:
            error_msg += f"Request Payload: {json.dumps(payload, indent=2)}\n"

        if "response_body" in error_details:
            error_msg += f"Response Body: {json.dumps(error_details['response_body'], indent=2)}\n"
        else:
            error_msg += f"Response Text: {error_details['response_text']}\n"

        print(error_msg)
        response.raise_for_status()

    def connect_restore_account(
        self,
        name: str,
        role_arn: str
    ) -> Dict[str, Any]:
        """
        Connect a restore account to Eon.

        Args:
            name: Display name for the restore account
            role_arn: ARN of the IAM role for Eon to assume

        Returns:
            Response containing restore account details
        """
        url = f"{self.base_url}/projects/{self.project_id}/restore-accounts"
        payload = {
            "name": name,
            "restoreAccountAttributes": {
                "cloudProvider": "AWS",
                "aws": {
                    "roleArn": role_arn
                }
            }
        }

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json()

    def configure_vpc_connectivity(
        self,
        restore_account_id: str,
        vpc_configs: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Configure VPC connectivity for a restore account.

        Args:
            restore_account_id: Eon-assigned restore account ID
            vpc_configs: List of VPC configurations

        Returns:
            Response containing updated configuration
        """
        url = f"{self.base_url}/projects/{self.project_id}/restore-accounts/{restore_account_id}/connectivity-config"
        payload = {
            "aws": {
                "vpcConfigs": vpc_configs
            }
        }

        response = requests.put(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "PUT", url, payload)
        return response.json()

    def list_resources(
        self,
        source_account_id: str,
        page_token: Optional[str] = None,
        page_size: int = 100,
        resource_types: Optional[List[str]] = None,
        resource_ids: Optional[List[str]] = None,
        provider_resource_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        List protected resources from a source account.

        Args:
            source_account_id: AWS account ID to filter resources
            page_token: Pagination token for next page
            page_size: Number of resources per page
            resource_types: Restrict to these Eon resource types (e.g. ["AWS_EC2"])
            resource_ids: Restrict to these Eon-assigned resource IDs
            provider_resource_ids: Restrict to these cloud-provider resource IDs
                (e.g. ["i-0f600a1b15b035105"])

        Returns:
            Response containing resources list and pagination info

        Note:
            Filters are combined with AND by the API. Pass either resource_ids or
            provider_resource_ids in a given call, never both, or the two lists
            will intersect instead of union.
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources"
        params = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token

        # Exclude statuses that categorically have no Eon snapshots. This keeps
        # the response small on accounts with thousands of resources (without
        # which Step Functions' 256 KB I/O limit can be exceeded). We *don't*
        # restrict to PROTECTED-only because resources in transitional/imperfect
        # states (e.g. VIOLATIONS_DETECTED, ACTION_REQUIRED, LIMIT_EXCEEDED,
        # DLSG, ALL_VIOLATIONS_MUTED) can still have valid Eon snapshots.
        payload = {
            "filters": {
                "accountId": {
                    "in": [source_account_id]
                },
                "backupStatus": {
                    "notIn": [
                        "NOT_BACKED_UP",
                        "GENERIC_BACKUPS",
                        "EXCLUDED_FROM_BACKUP",
                        "UNSUPPORTED",
                        "TERMINATED",
                        "DISCONNECTED",
                        "INITIAL_CLASSIFICATION",
                    ]
                },
            },
            "sorts": [
                {
                    "field": "resourceName",
                    "order": "ASC"
                }
            ]
        }

        # Scoping filters. Applying these server-side keeps the response (and the
        # Step Functions state payload) small on accounts with thousands of resources.
        if resource_types:
            payload["filters"]["resourceType"] = {"in": list(resource_types)}
        if resource_ids:
            payload["filters"]["id"] = {"in": list(resource_ids)}
        if provider_resource_ids:
            payload["filters"]["providerResourceId"] = {"in": list(provider_resource_ids)}

        response = requests.post(url, json=payload, params=params, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json()

    def list_snapshots(
        self,
        resource_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page_size: int = 100
    ) -> Dict[str, Any]:
        """
        List snapshots for a resource.

        Args:
            resource_id: Eon-assigned resource ID
            start_date: Filter snapshots from this date (ISO 8601 YYYY-MM-DD)
            end_date: Filter snapshots to this date (ISO 8601 YYYY-MM-DD)
            page_size: Number of snapshots per page

        Returns:
            Response containing snapshots list
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources/{resource_id}/snapshots"
        params = {"pageSize": page_size}

        payload = {
            "sorts": [
                {
                    "field": "pointInTime",
                    "order": "DESC"
                }
            ]
        }

        if start_date or end_date:
            payload["filters"] = {"pointInTime": {}}
            if start_date:
                payload["filters"]["pointInTime"]["startDate"] = start_date
            if end_date:
                payload["filters"]["pointInTime"]["endDate"] = end_date

        response = requests.post(url, json=payload, params=params, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json()

    def restore_ec2_instance(
        self,
        resource_id: str,
        snapshot_id: str,
        restore_account_id: str,
        destination_config: Dict[str, Any]
    ) -> str:
        """
        Restore an EC2 instance from a snapshot.

        Args:
            resource_id: Eon-assigned resource ID
            snapshot_id: Snapshot ID to restore from
            restore_account_id: Eon-assigned restore account ID
            destination_config: EC2 restore configuration

        Returns:
            Job ID for the restore operation
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources/{resource_id}/snapshots/{snapshot_id}/restore-ec2-instance"
        payload = {
            "restoreAccountId": restore_account_id,
            "destination": destination_config
        }

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json().get("jobId")

    def restore_rds_instance(
        self,
        resource_id: str,
        snapshot_id: str,
        restore_account_id: str,
        destination_config: Dict[str, Any]
    ) -> str:
        """
        Restore an RDS instance from a snapshot.

        Args:
            resource_id: Eon-assigned resource ID
            snapshot_id: Snapshot ID to restore from
            restore_account_id: Eon-assigned restore account ID
            destination_config: RDS restore configuration

        Returns:
            Job ID for the restore operation
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources/{resource_id}/snapshots/{snapshot_id}/restore-rds-instance"
        payload = {
            "restoreAccountId": restore_account_id,
            "destination": destination_config
        }

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json().get("jobId")

    def restore_s3_bucket(
        self,
        resource_id: str,
        snapshot_id: str,
        restore_account_id: str,
        destination_config: Dict[str, Any]
    ) -> str:
        """
        Restore an S3 bucket from a snapshot.

        Args:
            resource_id: Eon-assigned resource ID
            snapshot_id: Snapshot ID to restore from
            restore_account_id: Eon-assigned restore account ID
            destination_config: S3 restore configuration

        Returns:
            Job ID for the restore operation
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources/{resource_id}/snapshots/{snapshot_id}/restore-bucket"
        payload = {
            "restoreAccountId": restore_account_id,
            "destination": destination_config
        }

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json().get("jobId")

    def restore_dynamodb_table(
        self,
        resource_id: str,
        snapshot_id: str,
        restore_account_id: str,
        destination_config: Dict[str, Any],
        restore_method: Optional[str] = None
    ) -> str:
        """
        Restore a DynamoDB table from a snapshot.

        Args:
            resource_id: Eon-assigned resource ID
            snapshot_id: Snapshot ID to restore from
            restore_account_id: Eon-assigned restore account ID
            destination_config: DynamoDB restore configuration
            restore_method: RESTORE_METHOD_IMPORT_TABLE to rebuild the table with
                the AWS ImportTable (import-from-S3) API, RESTORE_METHOD_CAPACITY_BASED
                to write items back through the DynamoDB write path, or
                RESTORE_METHOD_AUTO to let Eon pick. Omitted means capacity-based.

        Returns:
            Job ID for the restore operation
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources/{resource_id}/snapshots/{snapshot_id}/restore-dynamo-db-table"
        payload = {
            "restoreAccountId": restore_account_id,
            "destination": destination_config
        }
        if restore_method:
            payload["restoreMethod"] = restore_method

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json().get("jobId")

    def check_dynamodb_import_availability(
        self,
        snapshot_id: str,
        region: str,
        restored_name: str
    ) -> Dict[str, Any]:
        """
        Check whether a DynamoDB snapshot can be restored with the ImportTable
        (import-from-S3) method.

        The check is snapshot-level: it reports Local Secondary Indexes, which
        ImportTable cannot create, and table sizes above the AWS import ceiling
        (15 TiB in us-east-1/us-west-1/us-west-2, 1 TiB elsewhere). It does not
        check the restore account's role version — that is validated when the
        restore is submitted.

        Args:
            snapshot_id: Snapshot ID to check
            region: Region the table would be restored into (sets the size ceiling)
            restored_name: Name the table would be restored under. Not part of the
                decision, but the destination schema requires it and the request
                is rejected with a 400 without it.

        Returns:
            Dict with "available" (bool) and "reasons" (list of reason codes)
        """
        # This endpoint is not under /v1 (see openapi-index.yaml).
        url = f"{self.api_root}/projects/{self.project_id}/snapshots/{snapshot_id}/dynamodb-restore-method-availability"
        payload = {
            "restoreMethod": "RESTORE_METHOD_IMPORT_TABLE",
            "destination": {
                "awsDynamodb": {
                    "restoreRegion": region,
                    "restoredName": restored_name
                }
            }
        }

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        data = response.json()
        return {
            "available": bool(data.get("available")),
            "reasons": data.get("reasons") or []
        }

    def restore_dynamodb_to_existing_table(
        self,
        resource_id: str,
        snapshot_id: str,
        restore_account_id: str,
        table_name: str,
        region: str,
        encryption_key_id: Optional[str] = None
    ) -> str:
        """
        Restore a DynamoDB snapshot to an existing table (in-place restore).

        The target table must:
        - Already exist in the restore account
        - Be empty (no items)
        - Have a matching key schema (partition key and sort key names must match exactly)

        Args:
            resource_id: Eon-assigned resource ID
            snapshot_id: Snapshot ID to restore from
            restore_account_id: Eon-assigned restore account ID
            table_name: Name of the existing target table
            region: AWS region of the target table
            encryption_key_id: KMS key ARN for the restore worker EC2 instance (optional)

        Returns:
            Job ID for the restore operation
        """
        url = f"{self.base_url}/projects/{self.project_id}/resources/{resource_id}/snapshots/{snapshot_id}/restore-dynamodb-to-existing"

        destination = {
            "awsDynamodb": {
                "restoredName": table_name,
                "restoreRegion": region
            }
        }

        if encryption_key_id:
            destination["awsDynamodb"]["encryptionKeyId"] = encryption_key_id

        payload = {
            "restoreAccountId": restore_account_id,
            "destination": destination
        }

        response = requests.post(url, json=payload, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json().get("jobId")

    def list_restore_accounts(
        self,
        provider_account_id: Optional[str] = None,
        account_status: Optional[List[str]] = None,
        page_size: int = 100
    ) -> Dict[str, Any]:
        """
        List restore accounts in the project.

        Args:
            provider_account_id: Filter by cloud provider account ID (e.g., AWS account ID)
            account_status: Filter by account status (e.g., ["CONNECTED", "DISCONNECTED"])
            page_size: Number of accounts per page

        Returns:
            Response containing accounts list
        """
        url = f"{self.base_url}/projects/{self.project_id}/restore-accounts/list"
        params = {"pageSize": page_size}

        payload = {}

        if provider_account_id or account_status:
            payload["filters"] = {}

            if provider_account_id:
                payload["filters"]["providerAccountId"] = {
                    "in": [provider_account_id]
                }

            if account_status:
                payload["filters"]["accountStatus"] = {
                    "in": account_status
                }

        response = requests.post(url, json=payload, params=params, headers=self._get_headers())
        self._handle_response(response, "POST", url, payload)
        return response.json()

    def reconnect_restore_account(self, account_id: str) -> Dict[str, Any]:
        """
        Reconnect a disconnected restore account.

        Args:
            account_id: Eon-assigned restore account ID

        Returns:
            Response containing reconnected restore account details
        """
        url = f"{self.base_url}/projects/{self.project_id}/restore-accounts/{account_id}/reconnect"

        response = requests.post(url, headers=self._get_headers())
        self._handle_response(response, "POST", url)
        return response.json()

    def get_restore_job(self, job_id: str) -> Dict[str, Any]:
        """
        Get the status of a restore job.

        Args:
            job_id: Job ID to query

        Returns:
            Job details including status
        """
        url = f"{self.base_url}/projects/{self.project_id}/restore-jobs/{job_id}"

        response = requests.get(url, headers=self._get_headers())
        self._handle_response(response, "GET", url)
        return response.json()
