"""Shared fixtures.

Every test runs with the Lambda environment variables set and with AWS
credentials pointed at nothing real, so a test that reaches boto3 without a stub
fails loudly instead of touching an account.
"""

import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


EON_DOMAIN = "test-account"
EON_PROJECT_ID = "1ee34dc5-0a7c-4e56-a820-917371e05c8d"
EON_ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:111111111111:eon-bulk-recovery-notifications"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111111111111:secret:eon-credentials-AbCdEf"

API_BASE = f"https://{EON_DOMAIN}.console.eon.io/api"
API_V1 = f"{API_BASE}/v1"


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    """The environment the Lambdas run under."""
    monkeypatch.setenv("EON_ACCOUNT_DOMAIN", EON_DOMAIN)
    monkeypatch.setenv("EON_PROJECT_ID", EON_PROJECT_ID)
    monkeypatch.setenv("EON_ACCOUNT_ID", EON_ACCOUNT_ID)
    monkeypatch.setenv("EON_CREDENTIALS_SECRET_ARN", SECRET_ARN)
    monkeypatch.setenv("SNS_TOPIC_ARN", SNS_TOPIC_ARN)
    monkeypatch.delenv("MANAGEMENT_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("MAX_MONITORING_ITERATIONS", raising=False)

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Restore initiation and reconnect polling both sleep; tests should not."""
    for module in ("handlers.initiate_restores", "handlers.connect_account", "handlers.bootstrap"):
        try:
            mod = __import__(module, fromlist=["time"])
        except ImportError:  # pragma: no cover - import guard
            continue
        if hasattr(mod, "time"):
            monkeypatch.setattr(mod.time, "sleep", lambda *_: None, raising=False)


@pytest.fixture
def eon_credentials(monkeypatch):
    """Stub get_eon_credentials everywhere it is imported."""
    creds = {"clientId": "client-id", "clientSecret": "client-secret"}
    for module in (
        "lib.aws_utils",
        "handlers.configure_vpc",
        "handlers.connect_account",
        "handlers.get_snapshots",
        "handlers.initiate_restores",
        "handlers.list_resources",
        "handlers.monitor_jobs",
    ):
        try:
            mod = __import__(module, fromlist=["get_eon_credentials"])
        except ImportError:  # pragma: no cover - import guard
            continue
        if hasattr(mod, "get_eon_credentials"):
            monkeypatch.setattr(mod, "get_eon_credentials", lambda: dict(creds))
    return creds


class FakeEonClient:
    """
    Records the calls a handler makes and replays canned responses.

    Anything not explicitly configured raises, so a handler that starts calling a
    new endpoint shows up as a test failure rather than a silent no-op.
    """

    def __init__(self, **responses: Any):
        self.calls: List[Dict[str, Any]] = []
        self._responses = responses

    def calls_to(self, method: str) -> List[Dict[str, Any]]:
        return [call for call in self.calls if call["method"] == method]

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        responses = self._responses
        calls = self.calls

        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append({"method": attr, "args": args, **kwargs})
            if attr not in responses:
                raise AssertionError(
                    f"FakeEonClient got an unexpected call: {attr}(*{args}, **{kwargs})"
                )
            value = responses[attr]
            if callable(value):
                return value(*args, **kwargs)
            if isinstance(value, list):
                return value.pop(0)
            return value

        return _call


@pytest.fixture
def fake_eon_client():
    return FakeEonClient


@pytest.fixture
def patch_eon_client(monkeypatch):
    """Replace the EonClient class in a handler module with a fixed instance."""

    def _patch(module_name: str, client: Any):
        mod = __import__(module_name, fromlist=["EonClient"])
        monkeypatch.setattr(mod, "EonClient", lambda **kwargs: client)
        return client

    return _patch


@pytest.fixture
def sts_credentials() -> Dict[str, str]:
    return {
        "AccessKeyId": "ASIAEXAMPLE",
        "SecretAccessKey": "secret",
        "SessionToken": "token",
    }


@pytest.fixture
def boto_clients(monkeypatch):
    """
    Hand out one MagicMock per (service, region) and route create_boto3_client
    and boto3.client to them.
    """
    created: Dict[str, MagicMock] = {}

    def _get(service: str, region: Optional[str] = None) -> MagicMock:
        key = f"{service}:{region}" if region else service
        return created.setdefault(key, MagicMock(name=key))

    def _create_boto3_client(service, region, credentials=None):
        client = _get(service, region)
        client.eon_credentials = credentials
        return client

    for module in (
        "lib.aws_utils",
        "handlers.bootstrap",
        "handlers.initiate_restores",
        "handlers.monitor_jobs",
    ):
        mod = __import__(module, fromlist=["create_boto3_client"])
        if hasattr(mod, "create_boto3_client"):
            monkeypatch.setattr(mod, "create_boto3_client", _create_boto3_client)
        if hasattr(mod, "boto3"):
            monkeypatch.setattr(mod.boto3, "client", lambda service, **kw: _get(service, kw.get("region_name")))

    _get.clients = created
    return _get


@pytest.fixture
def restore_context():
    """Build a _RestoreContext with sane defaults, overridable per test."""
    from handlers.initiate_restores import _RestoreContext

    def _build(**overrides: Any):
        defaults: Dict[str, Any] = {
            "eon_client": None,
            "eon_restore_account_id": "eon-restore-acct",
            "restore_account_id": "222222222222",
            "restore_region": "us-east-1",
            "kms_key_arns_by_region": {"us-east-1": "arn:aws:kms:us-east-1:222222222222:key/abc"},
            "rds_subnet_groups_by_region": {"us-east-1": "eon-restore-222222222222-us-east-1"},
            "vpc_configs_by_region": {
                "us-east-1": {
                    "region": "us-east-1",
                    "vpc": "vpc-1",
                    "subnetsPerAvailabilityZone": [
                        {"availabilityZone": "us-east-1a", "subnetId": "subnet-1a"},
                        {"availabilityZone": "us-east-1b", "subnetId": "subnet-1b"},
                    ],
                    "securityGroups": {
                        "restoreServer": ["sg-restore"],
                        "restoredRdsInstance": ["sg-rds"],
                    },
                }
            },
            "restore_account_credentials": {
                "AccessKeyId": "ASIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            },
            "resource_name_prefix": None,
            "exclude_ec2_tag_keys": [],
            "recovery_stack_tables": {},
            "recovery_stack_s3_buckets": {},
            "recovery_stacks_only": False,
            "dynamodb_wcu_allocation": {},
            "dynamodb_restore_methods": {},
            "dynamodb_warm_throughput": True,
            "s3_in_place_tag_key": "eon_functional_id",
        }
        defaults.update(overrides)
        return _RestoreContext(**defaults)

    return _build
