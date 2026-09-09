"""Connecting (or reconnecting) the restore account to Eon."""

from unittest.mock import MagicMock

import pytest
from requests.exceptions import HTTPError

from handlers import connect_account
from handlers.connect_account import _reconnect_and_wait


def http_error(status: int = 409) -> HTTPError:
    response = MagicMock()
    response.status_code = status
    return HTTPError(response=response)


EVENT = {
    "roleArn": "arn:aws:iam::222222222222:role/EonRestore",
    "restoreAccountId": "222222222222",
    "restoreAccountName": "my-restore-account",
}


class TestReconnectAndWait:
    def test_returns_immediately_when_the_reconnect_connects(self, fake_eon_client):
        client = fake_eon_client(
            reconnect_restore_account={"restoreAccount": {"status": "CONNECTED"}}
        )

        assert _reconnect_and_wait(client, "eon-acct", "222222222222") == "CONNECTED"
        assert client.calls_to("list_restore_accounts") == []

    def test_polls_until_connected(self, fake_eon_client):
        client = fake_eon_client(
            reconnect_restore_account={"restoreAccount": {"status": "PENDING"}},
            list_restore_accounts=[
                {"accounts": [{"status": "PENDING"}]},
                {"accounts": [{"status": "CONNECTED"}]},
            ],
        )

        assert _reconnect_and_wait(client, "eon-acct", "222222222222") == "CONNECTED"
        assert len(client.calls_to("list_restore_accounts")) == 2

    def test_gives_up_after_max_attempts(self, fake_eon_client):
        client = fake_eon_client(
            reconnect_restore_account={"restoreAccount": {"status": "PENDING"}},
            list_restore_accounts={"accounts": [{"status": "INSUFFICIENT_PERMISSIONS"}]},
        )

        status = _reconnect_and_wait(client, "eon-acct", "222222222222", max_attempts=3)

        assert status == "INSUFFICIENT_PERMISSIONS"
        assert len(client.calls_to("list_restore_accounts")) == 3

    def test_stops_when_the_account_disappears(self, fake_eon_client):
        client = fake_eon_client(
            reconnect_restore_account={"restoreAccount": {"status": "PENDING"}},
            list_restore_accounts={"accounts": []},
        )

        assert _reconnect_and_wait(client, "eon-acct", "222222222222") == "PENDING"
        assert len(client.calls_to("list_restore_accounts")) == 1

    def test_missing_status_reads_as_unknown(self, fake_eon_client):
        client = fake_eon_client(
            reconnect_restore_account={},
            list_restore_accounts={"accounts": [{}]},
        )

        assert _reconnect_and_wait(client, "eon-acct", "222222222222", max_attempts=1) == "UNKNOWN"


class TestHandler:
    def test_connects_a_new_account(self, eon_credentials, fake_eon_client, patch_eon_client):
        client = patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(
                connect_restore_account={"restoreAccount": {"id": "eon-acct", "status": "CONNECTED"}}
            ),
        )

        result = connect_account.handler(dict(EVENT), None)

        assert result == {
            "eonRestoreAccountId": "eon-acct",
            "restoreAccountName": "my-restore-account",
            "roleArn": EVENT["roleArn"],
            "restoreAccountId": "222222222222",
            "status": "CONNECTED",
        }
        assert client.calls_to("connect_restore_account")[0]["name"] == "my-restore-account"

    def test_generates_a_name_when_none_is_given(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        client = patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(connect_restore_account={"restoreAccount": {"id": "eon-acct"}}),
        )
        event = dict(EVENT, restoreAccountName=None)

        result = connect_account.handler(event, None)

        assert result["restoreAccountName"] == "bulk-recovery-222222222222"
        assert client.calls_to("connect_restore_account")[0]["name"] == "bulk-recovery-222222222222"

    def test_reuses_an_already_connected_account(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        client = patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(
                connect_restore_account=_raise(http_error()),
                list_restore_accounts={"accounts": [{"id": "existing", "status": "CONNECTED"}]},
            ),
        )

        result = connect_account.handler(dict(EVENT), None)

        assert result["eonRestoreAccountId"] == "existing"
        assert result["status"] == "CONNECTED"
        assert client.calls_to("reconnect_restore_account") == []

    @pytest.mark.parametrize("status", ["DISCONNECTED", "INSUFFICIENT_PERMISSIONS"])
    def test_reconnects_a_recoverable_account(
        self, eon_credentials, fake_eon_client, patch_eon_client, status
    ):
        client = patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(
                connect_restore_account=_raise(http_error()),
                list_restore_accounts=[
                    {"accounts": [{"id": "existing", "status": status}]},
                ],
                reconnect_restore_account={"restoreAccount": {"status": "CONNECTED"}},
            ),
        )

        result = connect_account.handler(dict(EVENT), None)

        assert result["status"] == "CONNECTED"
        assert len(client.calls_to("reconnect_restore_account")) == 1

    def test_a_reconnect_that_does_not_connect_raises_so_the_step_retries(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(
                connect_restore_account=_raise(http_error()),
                list_restore_accounts=[
                    {"accounts": [{"id": "existing", "status": "DISCONNECTED"}]},
                    {"accounts": [{"id": "existing", "status": "DISCONNECTED"}]},
                    {"accounts": [{"id": "existing", "status": "DISCONNECTED"}]},
                    {"accounts": [{"id": "existing", "status": "DISCONNECTED"}]},
                    {"accounts": [{"id": "existing", "status": "DISCONNECTED"}]},
                    {"accounts": [{"id": "existing", "status": "DISCONNECTED"}]},
                ],
                reconnect_restore_account={"restoreAccount": {"status": "DISCONNECTED"}},
            ),
        )

        with pytest.raises(ValueError, match="did not reach CONNECTED after reconnect"):
            connect_account.handler(dict(EVENT), None)

    def test_no_existing_account_re_raises_the_connect_error(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(
                connect_restore_account=_raise(http_error()),
                list_restore_accounts={"accounts": []},
            ),
        )

        with pytest.raises(HTTPError):
            connect_account.handler(dict(EVENT), None)

    def test_an_unexpected_existing_status_is_reported_but_not_fatal(
        self, eon_credentials, fake_eon_client, patch_eon_client, capsys
    ):
        patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(
                connect_restore_account=_raise(http_error()),
                list_restore_accounts={"accounts": [{"id": "existing", "status": "PENDING"}]},
            ),
        )

        result = connect_account.handler(dict(EVENT), None)

        assert result["status"] == "PENDING"
        assert "unexpected status: PENDING" in capsys.readouterr().out

    def test_a_connect_returning_no_id_is_an_error(
        self, eon_credentials, fake_eon_client, patch_eon_client
    ):
        patch_eon_client(
            "handlers.connect_account",
            fake_eon_client(connect_restore_account={"restoreAccount": {}}),
        )

        with pytest.raises(ValueError, match="Failed to retrieve Eon restore account ID"):
            connect_account.handler(dict(EVENT), None)


def _raise(exc):
    def _side_effect(**_kwargs):
        raise exc

    return _side_effect
