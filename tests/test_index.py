"""The step router."""

import pytest

import index


ALL_STEPS = [
    "resolve_vpc_configs",
    "bootstrap",
    "connect_account",
    "configure_vpc",
    "list_resources",
    "get_snapshots",
    "initiate_restores",
    "monitor_jobs",
]


@pytest.mark.parametrize("step", ALL_STEPS)
def test_routes_each_step_to_its_handler(step, monkeypatch):
    module = getattr(index, step)
    monkeypatch.setattr(module, "handler", lambda event, context: {"ran": step})

    assert index.handler({"step": step}, None) == {"ran": step}


def test_passes_the_event_and_context_through(monkeypatch):
    seen = {}

    def record(event, context):
        seen["event"] = event
        seen["context"] = context
        return {}

    monkeypatch.setattr(index.bootstrap, "handler", record)
    event = {"step": "bootstrap", "restoreAccountId": "222222222222"}
    context = object()

    index.handler(event, context)

    assert seen["event"] is event
    assert seen["context"] is context


def test_missing_step_is_an_error():
    with pytest.raises(ValueError, match="Missing 'step' parameter"):
        index.handler({}, None)


def test_unknown_step_is_an_error():
    with pytest.raises(ValueError, match="Unknown step: teleport"):
        index.handler({"step": "teleport"}, None)
