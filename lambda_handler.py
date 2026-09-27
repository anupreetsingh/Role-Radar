"""AWS Lambda entry point, run every 5 minutes by EventBridge Scheduler.

If the laptop holds the lease this returns in a fraction of a second.
Otherwise it checks whatever is due for up to ~10 minutes (or until the
laptop asks to take over), then releases the lease.

Settings come from environment variables set by deploy/aws/template.yaml:
ROLE_RADAR_STORAGE=dynamodb, ROLE_RADAR_TABLE, ROLE_RADAR_CONFIG_URL,
ROLE_RADAR_SECRETS=ssm:/role-radar/, ROLE_RADAR_WORK_SECONDS.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from role_radar.backends import AwsClients, ConfigSource, NotifierSource, open_backend
from role_radar.config import RuntimeSettings
from role_radar.http_client import RobotsCache
from role_radar.monitor import EXIT_FAILED, EXIT_NOTHING
from role_radar.runner import LAMBDA_WORK_SECONDS, Runner, Skipped

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
# httpx logs request URLs (webhook URLs are secrets); botocore at DEBUG logs decrypted SSM values.
for _noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("lambda")

# Kept across warm invocations: AWS clients, the config (re-read only when it
# changes), the alert channels (secrets refreshed periodically), parsed robots.txt files.
_runner: Runner | None = None


def _get_runner() -> Runner:
    global _runner
    if _runner is None:
        runtime = RuntimeSettings().with_env()
        clients = AwsClients(runtime)
        _runner = Runner(
            ConfigSource(runtime, None, clients),
            open_backend(runtime, "lambda", clients),
            NotifierSource(runtime, clients),
            robots=RobotsCache(),
            name="lambda",
        )
    return _runner


def handler(event: Any, context: Any) -> dict[str, Any]:
    runner = _get_runner()
    remaining = context.get_remaining_time_in_millis() / 1000
    work_seconds = float(os.environ.get("ROLE_RADAR_WORK_SECONDS", LAMBDA_WORK_SECONDS))
    holder = f"lambda:{getattr(context, 'aws_request_id', 'local')[:8]}"
    result = asyncio.run(runner.lambda_pass(remaining, work_seconds, holder))
    if isinstance(result, Skipped):  # the laptop has the lease, or no config was pushed yet
        log.info("Nothing to do: %s", result.reason)
        return {"skipped": True, "reason": result.reason}

    summary = result.summary()
    # Raise (so the CloudWatch error alarm fires) only for problems worth an email:
    # alerts that couldn't be delivered, or every company failing (e.g. no network).
    # Single broken sites show up in `role-radar status` instead.
    if result.exit_code in (EXIT_FAILED, EXIT_NOTHING):
        raise RuntimeError(f"Role Radar pass failed: {summary}")
    return summary
