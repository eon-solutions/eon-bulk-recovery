"""Main Lambda handler that routes to different workflow steps."""

import json
from typing import Dict, Any

from handlers import bootstrap
from handlers import connect_account
from handlers import configure_vpc
from handlers import list_resources
from handlers import get_snapshots
from handlers import initiate_restores
from handlers import monitor_jobs
from handlers import resolve_vpc_configs


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Route requests to appropriate handler based on the step name.

    Event structure:
    {
        "step": "resolve_vpc_configs|bootstrap|connect_account|configure_vpc|list_resources|get_snapshots|initiate_restores|monitor_jobs",
        ... (step-specific parameters)
    }
    """
    step = event.get("step")

    if not step:
        raise ValueError("Missing 'step' parameter in event")

    print(f"Executing step: {step}")

    # Route to appropriate handler
    if step == "resolve_vpc_configs":
        return resolve_vpc_configs.handler(event, context)
    elif step == "bootstrap":
        return bootstrap.handler(event, context)
    elif step == "connect_account":
        return connect_account.handler(event, context)
    elif step == "configure_vpc":
        return configure_vpc.handler(event, context)
    elif step == "list_resources":
        return list_resources.handler(event, context)
    elif step == "get_snapshots":
        return get_snapshots.handler(event, context)
    elif step == "initiate_restores":
        return initiate_restores.handler(event, context)
    elif step == "monitor_jobs":
        return monitor_jobs.handler(event, context)
    else:
        raise ValueError(f"Unknown step: {step}")
