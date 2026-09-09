"""The Eon REST client: URLs, payload shape, auth, and error reporting."""

import json
import time

import pytest
import requests
import responses

from lib.eon_client import EonClient

from conftest import API_BASE, API_V1, EON_DOMAIN, EON_PROJECT_ID

RESOURCE_ID = "2f97ca76-6a78-55d8-94d3-66c2f2cfff23"
SNAPSHOT_ID = "ac3014c2-9ab3-5d7f-ab4c-73412d6b9ef5"
RESTORE_ACCOUNT_ID = "1ee34dc5-0a7c-4e56-a820-917371e05c8d"


@pytest.fixture
def client():
    return EonClient(
        account_domain=EON_DOMAIN,
        client_id="client-id",
        client_secret="client-secret",
        project_id=EON_PROJECT_ID,
    )


@pytest.fixture
def token(client):
    """Register the token endpoint and pre-authenticate."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.post(f"{API_V1}/token", json={"accessToken": "tok", "expirationSeconds": 43200})
        yield mock


def last_body(mock) -> dict:
    return json.loads(mock.calls[-1].request.body)


class TestBaseUrls:
    def test_v1_base_and_api_root(self, client):
        assert client.base_url == f"https://{EON_DOMAIN}.console.eon.io/api/v1"
        assert client.api_root == f"https://{EON_DOMAIN}.console.eon.io/api"


class TestAuthentication:
    @responses.activate
    def test_first_call_fetches_a_token_and_sends_it(self, client):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok-1", "expirationSeconds": 43200})
        responses.get(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1", json={"job": {}})

        client.get_restore_job("j1")

        assert responses.calls[0].request.url == f"{API_V1}/token"
        assert json.loads(responses.calls[0].request.body) == {
            "clientId": "client-id",
            "clientSecret": "client-secret",
        }
        assert responses.calls[1].request.headers["Authorization"] == "Bearer tok-1"

    @responses.activate
    def test_token_is_reused_while_valid(self, client):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok", "expirationSeconds": 43200})
        responses.get(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1", json={"job": {}})

        client.get_restore_job("j1")
        client.get_restore_job("j1")

        assert len([c for c in responses.calls if c.request.url.endswith("/token")]) == 1

    @responses.activate
    def test_token_is_refreshed_once_it_expires(self, client, monkeypatch):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok", "expirationSeconds": 43200})
        responses.get(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1", json={"job": {}})

        client.get_restore_job("j1")
        monkeypatch.setattr(time, "time", lambda: client.token_expiry + 1)
        client.get_restore_job("j1")

        assert len([c for c in responses.calls if c.request.url.endswith("/token")]) == 2

    @responses.activate
    def test_expiry_leaves_a_refresh_margin(self, client):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok", "expirationSeconds": 43200})
        responses.get(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1", json={"job": {}})

        before = time.time()
        client.get_restore_job("j1")

        # 12 hours minus the 30 minute margin.
        assert client.token_expiry == pytest.approx(before + 43200 - 1800, abs=5)

    @responses.activate
    def test_missing_expiry_falls_back_to_twelve_hours(self, client):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok"})
        responses.get(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1", json={"job": {}})

        before = time.time()
        client.get_restore_job("j1")

        assert client.token_expiry == pytest.approx(before + 43200 - 1800, abs=5)

    @responses.activate
    def test_a_failed_token_request_raises(self, client):
        responses.post(f"{API_V1}/token", json={"error": "bad credentials"}, status=401)

        with pytest.raises(requests.HTTPError):
            client.get_restore_job("j1")


class TestErrorReporting:
    @responses.activate
    def test_masks_the_bearer_token_in_the_error_output(self, client, capsys):
        responses.post(f"{API_V1}/token", json={"accessToken": "super-secret-token"})
        responses.get(
            f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1",
            json={"error": "nope"},
            status=403,
        )

        with pytest.raises(requests.HTTPError):
            client.get_restore_job("j1")

        printed = capsys.readouterr().out
        assert "super-secret-token" not in printed
        assert "Bearer ***MASKED***" in printed
        assert "Status Code: 403" in printed

    @responses.activate
    def test_reports_a_non_json_body_as_text(self, client, capsys):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok"})
        responses.get(
            f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/j1",
            body="<html>gateway timeout</html>",
            status=504,
        )

        with pytest.raises(requests.HTTPError):
            client.get_restore_job("j1")

        assert "Response Text: <html>gateway timeout</html>" in capsys.readouterr().out

    @responses.activate
    def test_includes_the_request_payload_when_there_is_one(self, client, capsys):
        responses.post(f"{API_V1}/token", json={"accessToken": "tok"})
        responses.post(
            f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts",
            json={"error": "duplicate"},
            status=409,
        )

        with pytest.raises(requests.HTTPError):
            client.connect_restore_account("acct", "arn:aws:iam::222222222222:role/EonRestore")

        printed = capsys.readouterr().out
        assert "Request Payload" in printed
        assert "arn:aws:iam::222222222222:role/EonRestore" in printed

    @responses.activate
    def test_authenticate_masks_the_secret_in_its_error_output(self, client, capsys):
        responses.post(f"{API_V1}/token", json={"error": "nope"}, status=401)

        with pytest.raises(requests.HTTPError):
            client.get_restore_job("j1")

        printed = capsys.readouterr().out
        assert "client-secret" not in printed
        assert "***MASKED***" in printed


class TestAccountEndpoints:
    def test_connect_restore_account(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts", json={"id": "eon-acct"})

        assert client.connect_restore_account("my-account", "arn:role") == {"id": "eon-acct"}
        assert last_body(token) == {
            "name": "my-account",
            "restoreAccountAttributes": {"cloudProvider": "AWS", "aws": {"roleArn": "arn:role"}},
        }

    def test_configure_vpc_connectivity_puts_the_config(self, client, token):
        token.put(
            f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts/eon-acct/connectivity-config",
            json={"ok": True},
        )
        vpc_configs = [{"region": "us-east-1", "vpc": "vpc-1"}]

        assert client.configure_vpc_connectivity("eon-acct", vpc_configs) == {"ok": True}
        assert last_body(token) == {"aws": {"vpcConfigs": vpc_configs}}
        assert token.calls[-1].request.method == "PUT"

    def test_list_restore_accounts_without_filters(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts/list", json={"accounts": []})

        client.list_restore_accounts()

        assert last_body(token) == {}
        assert "pageSize=100" in token.calls[-1].request.url

    def test_list_restore_accounts_with_filters(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts/list", json={"accounts": []})

        client.list_restore_accounts(
            provider_account_id="222222222222", account_status=["CONNECTED"], page_size=25
        )

        assert last_body(token) == {
            "filters": {
                "providerAccountId": {"in": ["222222222222"]},
                "accountStatus": {"in": ["CONNECTED"]},
            }
        }
        assert "pageSize=25" in token.calls[-1].request.url

    def test_list_restore_accounts_with_only_a_status_filter(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts/list", json={"accounts": []})

        client.list_restore_accounts(account_status=["CONNECTED"])

        assert last_body(token) == {"filters": {"accountStatus": {"in": ["CONNECTED"]}}}

    def test_list_restore_accounts_with_only_a_provider_filter(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts/list", json={"accounts": []})

        client.list_restore_accounts(provider_account_id="222222222222")

        assert last_body(token) == {"filters": {"providerAccountId": {"in": ["222222222222"]}}}

    def test_reconnect_restore_account(self, client, token):
        token.post(
            f"{API_V1}/projects/{EON_PROJECT_ID}/restore-accounts/eon-acct/reconnect",
            json={"status": "CONNECTED"},
        )

        assert client.reconnect_restore_account("eon-acct") == {"status": "CONNECTED"}


class TestListResources:
    def test_excludes_statuses_that_cannot_have_snapshots(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/resources", json={"resources": []})

        client.list_resources(source_account_id="333333333333")

        body = last_body(token)
        assert body["filters"]["accountId"] == {"in": ["333333333333"]}
        assert set(body["filters"]["backupStatus"]["notIn"]) == {
            "NOT_BACKED_UP",
            "GENERIC_BACKUPS",
            "EXCLUDED_FROM_BACKUP",
            "UNSUPPORTED",
            "TERMINATED",
            "DISCONNECTED",
            "INITIAL_CLASSIFICATION",
        }
        assert body["sorts"] == [{"field": "resourceName", "order": "ASC"}]
        assert "id" not in body["filters"]
        assert "providerResourceId" not in body["filters"]

    def test_scoping_filters_are_applied_server_side(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/resources", json={"resources": []})

        client.list_resources(
            source_account_id="333333333333",
            resource_types=["AWS_EC2"],
            resource_ids=[RESOURCE_ID],
            provider_resource_ids=["i-0f600a1b15b035105"],
        )

        filters = last_body(token)["filters"]
        assert filters["resourceType"] == {"in": ["AWS_EC2"]}
        assert filters["id"] == {"in": [RESOURCE_ID]}
        assert filters["providerResourceId"] == {"in": ["i-0f600a1b15b035105"]}

    def test_page_token_is_a_query_parameter(self, client, token):
        token.post(f"{API_V1}/projects/{EON_PROJECT_ID}/resources", json={"resources": []})

        client.list_resources(source_account_id="333333333333", page_token="next-page", page_size=50)

        url = token.calls[-1].request.url
        assert "pageToken=next-page" in url
        assert "pageSize=50" in url


class TestListSnapshots:
    def test_sorts_newest_first_and_omits_an_empty_date_filter(self, client, token):
        token.post(
            f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots",
            json={"snapshots": []},
        )

        client.list_snapshots(RESOURCE_ID)

        body = last_body(token)
        assert body == {"sorts": [{"field": "pointInTime", "order": "DESC"}]}

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"start_date": "2026-09-01"}, {"startDate": "2026-09-01"}),
            ({"end_date": "2026-09-02"}, {"endDate": "2026-09-02"}),
            (
                {"start_date": "2026-09-01", "end_date": "2026-09-02"},
                {"startDate": "2026-09-01", "endDate": "2026-09-02"},
            ),
        ],
    )
    def test_date_filters(self, client, token, kwargs, expected):
        token.post(
            f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots",
            json={"snapshots": []},
        )

        client.list_snapshots(RESOURCE_ID, **kwargs)

        assert last_body(token)["filters"]["pointInTime"] == expected


RESTORE_CASES = [
    ("restore_ec2_instance", "restore-ec2-instance"),
    ("restore_rds_instance", "restore-rds-instance"),
    ("restore_s3_bucket", "restore-bucket"),
    ("restore_dynamodb_table", "restore-dynamo-db-table"),
]


class TestRestoreEndpoints:
    @pytest.mark.parametrize("method_name,path", RESTORE_CASES)
    def test_posts_to_the_right_path_and_returns_the_job_id(self, client, token, method_name, path):
        url = f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots/{SNAPSHOT_ID}/{path}"
        token.post(url, json={"jobId": "job-1"})

        job_id = getattr(client, method_name)(
            resource_id=RESOURCE_ID,
            snapshot_id=SNAPSHOT_ID,
            restore_account_id=RESTORE_ACCOUNT_ID,
            destination_config={"awsEc2": {"restoreRegion": "us-east-1"}},
        )

        assert job_id == "job-1"
        assert last_body(token) == {
            "restoreAccountId": RESTORE_ACCOUNT_ID,
            "destination": {"awsEc2": {"restoreRegion": "us-east-1"}},
        }

    @pytest.mark.parametrize("method_name,path", RESTORE_CASES)
    def test_missing_job_id_comes_back_as_none(self, client, token, method_name, path):
        url = f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots/{SNAPSHOT_ID}/{path}"
        token.post(url, json={})

        assert getattr(client, method_name)(
            resource_id=RESOURCE_ID,
            snapshot_id=SNAPSHOT_ID,
            restore_account_id=RESTORE_ACCOUNT_ID,
            destination_config={},
        ) is None


class TestRestoreDynamoDBTable:
    def test_omits_restore_method_when_not_asked_for(self, client, token):
        url = f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots/{SNAPSHOT_ID}/restore-dynamo-db-table"
        token.post(url, json={"jobId": "job-1"})

        client.restore_dynamodb_table(
            resource_id=RESOURCE_ID,
            snapshot_id=SNAPSHOT_ID,
            restore_account_id=RESTORE_ACCOUNT_ID,
            destination_config={"awsDynamodb": {}},
        )

        assert "restoreMethod" not in last_body(token)

    @pytest.mark.parametrize(
        "method", ["RESTORE_METHOD_IMPORT_TABLE", "RESTORE_METHOD_CAPACITY_BASED", "RESTORE_METHOD_AUTO"]
    )
    def test_sends_the_requested_restore_method(self, client, token, method):
        url = f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots/{SNAPSHOT_ID}/restore-dynamo-db-table"
        token.post(url, json={"jobId": "job-1"})

        client.restore_dynamodb_table(
            resource_id=RESOURCE_ID,
            snapshot_id=SNAPSHOT_ID,
            restore_account_id=RESTORE_ACCOUNT_ID,
            destination_config={"awsDynamodb": {}},
            restore_method=method,
        )

        assert last_body(token)["restoreMethod"] == method


class TestRestoreDynamoDBToExistingTable:
    def test_builds_the_destination_from_name_and_region(self, client, token):
        url = f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots/{SNAPSHOT_ID}/restore-dynamodb-to-existing"
        token.post(url, json={"jobId": "job-2"})

        job_id = client.restore_dynamodb_to_existing_table(
            resource_id=RESOURCE_ID,
            snapshot_id=SNAPSHOT_ID,
            restore_account_id=RESTORE_ACCOUNT_ID,
            table_name="orders",
            region="us-east-1",
        )

        assert job_id == "job-2"
        assert last_body(token) == {
            "restoreAccountId": RESTORE_ACCOUNT_ID,
            "destination": {"awsDynamodb": {"restoredName": "orders", "restoreRegion": "us-east-1"}},
        }

    def test_includes_the_encryption_key_when_given(self, client, token):
        url = f"{API_V1}/projects/{EON_PROJECT_ID}/resources/{RESOURCE_ID}/snapshots/{SNAPSHOT_ID}/restore-dynamodb-to-existing"
        token.post(url, json={"jobId": "job-2"})

        client.restore_dynamodb_to_existing_table(
            resource_id=RESOURCE_ID,
            snapshot_id=SNAPSHOT_ID,
            restore_account_id=RESTORE_ACCOUNT_ID,
            table_name="orders",
            region="us-east-1",
            encryption_key_id="arn:aws:kms:us-east-1:222222222222:key/abc",
        )

        assert last_body(token)["destination"]["awsDynamodb"]["encryptionKeyId"] == (
            "arn:aws:kms:us-east-1:222222222222:key/abc"
        )


class TestCheckDynamoDBImportAvailability:
    """
    The availability endpoint is not under /v1 (see openapi-index.yaml), which is
    the easiest thing to get wrong here.
    """

    AVAILABILITY_URL = f"{API_BASE}/projects/{EON_PROJECT_ID}/snapshots/{SNAPSHOT_ID}/dynamodb-restore-method-availability"

    def test_posts_outside_the_v1_prefix(self, client, token):
        token.post(self.AVAILABILITY_URL, json={"available": True})

        client.check_dynamodb_import_availability(SNAPSHOT_ID, "us-east-1", "orders")

        assert token.calls[-1].request.url == self.AVAILABILITY_URL
        assert "/api/v1/" not in token.calls[-1].request.url

    def test_asks_about_import_table_in_the_target_region(self, client, token):
        token.post(self.AVAILABILITY_URL, json={"available": True})

        client.check_dynamodb_import_availability(SNAPSHOT_ID, "eu-west-1", "orders")

        assert last_body(token) == {
            "restoreMethod": "RESTORE_METHOD_IMPORT_TABLE",
            "destination": {
                "awsDynamodb": {"restoreRegion": "eu-west-1", "restoredName": "orders"}
            },
        }

    def test_the_destination_always_carries_both_required_fields(self, client, token):
        """
        The gateway validates the destination against AwsDynamoDBDestination, whose
        required set is [restoredName, restoreRegion]. Omitting restoredName gets a
        400, which the planner reads as "import unavailable" and quietly downgrades
        every table to the capacity-based path.
        """
        token.post(self.AVAILABILITY_URL, json={"available": True})

        client.check_dynamodb_import_availability(SNAPSHOT_ID, "us-east-1", "orders")

        destination = last_body(token)["destination"]["awsDynamodb"]
        assert set(destination) >= {"restoreRegion", "restoredName"}
        assert all(destination[field] for field in ("restoreRegion", "restoredName"))

    def test_available_response(self, client, token):
        token.post(self.AVAILABILITY_URL, json={"available": True})

        assert client.check_dynamodb_import_availability(SNAPSHOT_ID, "us-east-1", "orders") == {
            "available": True,
            "reasons": [],
        }

    def test_unavailable_response_carries_the_reasons(self, client, token):
        token.post(
            self.AVAILABILITY_URL,
            json={"available": False, "reasons": ["RESTORE_METHOD_HAS_LSI"]},
        )

        assert client.check_dynamodb_import_availability(SNAPSHOT_ID, "us-east-1", "orders") == {
            "available": False,
            "reasons": ["RESTORE_METHOD_HAS_LSI"],
        }

    def test_a_null_reasons_field_normalises_to_a_list(self, client, token):
        token.post(self.AVAILABILITY_URL, json={"available": False, "reasons": None})

        assert client.check_dynamodb_import_availability(SNAPSHOT_ID, "us-east-1", "orders")["reasons"] == []

    def test_a_forbidden_response_raises(self, client, token):
        token.post(self.AVAILABILITY_URL, json={"error": "not enabled"}, status=403)

        with pytest.raises(requests.HTTPError):
            client.check_dynamodb_import_availability(SNAPSHOT_ID, "us-east-1", "orders")


class TestGetRestoreJob:
    def test_gets_the_job(self, client, token):
        token.get(
            f"{API_V1}/projects/{EON_PROJECT_ID}/restore-jobs/job-1",
            json={"job": {"jobExecutionDetails": {"status": "JOB_COMPLETED"}}},
        )

        job = client.get_restore_job("job-1")

        assert job["job"]["jobExecutionDetails"]["status"] == "JOB_COMPLETED"
        assert token.calls[-1].request.method == "GET"
