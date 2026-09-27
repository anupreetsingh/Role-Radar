"""Read-only checks for local settings and a deployed SAM stack. Never send alerts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from role_radar import aws
from role_radar.backends import AwsClients, ConfigSource, NotifierSource, open_backend
from role_radar.config import RuntimeSettings
from role_radar.notifications import ConsoleNotifier, UnconfiguredNotifier
from role_radar.storage import from_iso, utcnow


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def diagnose(runtime: RuntimeSettings, path: Path | None, clients: AwsClients, stack: str | None = None) -> list[Check]:
    checks: list[Check] = []

    def check(name: str, action: Callable[[], str]) -> Any:
        try:
            detail = action()
            checks.append(Check(name, True, detail))
        except Exception as exc:
            # AWS/transport errors can include credentials or request URLs.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
            checks.append(Check(name, False, str(exc) if isinstance(exc, DiagnosticError) else code))

    deployed: dict[str, str] = {}
    cf = None
    if stack:
        def find_stack() -> str:
            nonlocal cf
            if not clients.session.get_credentials():
                raise DiagnosticError("No AWS credentials found. Configure an AWS profile, then rerun with --profile and --region.")
            cf = aws.client(clients.session, "cloudformation")
            info = cf.describe_stacks(StackName=stack)["Stacks"][0]
            deployed.update({item["OutputKey"]: item["OutputValue"] for item in info.get("Outputs", [])})
            if info["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
                raise DiagnosticError(f"Stack status: {info['StackStatus']}")
            return f"{stack}: {info['StackStatus']}"

        check("AWS stack", find_stack)
        required = {"TableName", "ConfigUrl", "SecretsPath", "FunctionName"}
        if required <= deployed.keys():
            remote = replace(runtime, storage="dynamodb", table=deployed["TableName"],
                             config_url=deployed["ConfigUrl"], secrets="ssm:" + deployed["SecretsPath"])
            same = all(getattr(runtime, key) == getattr(remote, key) for key in ("storage", "table", "config_url", "secrets"))
            checks.append(Check("Laptop / Lambda settings", same,
                                "Same backends" if same else "Local runtime differs from the stack; copy its RuntimeSection output into your config."))
            runtime = remote
            clients = AwsClients(runtime)

            def function() -> str:
                lam = aws.client(clients.session, "lambda")
                info = lam.get_function_configuration(FunctionName=deployed["FunctionName"])
                if info.get("State") != "Active" or info.get("LastUpdateStatus") == "Failed":
                    raise DiagnosticError("Lambda is not active or its latest update failed; inspect the deployment.")
                if info.get("Handler") != "lambda_handler.handler":
                    raise DiagnosticError("Lambda handler must be lambda_handler.handler.")
                env = info.get("Environment", {}).get("Variables", {})
                expected = {"ROLE_RADAR_STORAGE": "dynamodb", "ROLE_RADAR_TABLE": runtime.table,
                            "ROLE_RADAR_CONFIG_URL": runtime.config_url, "ROLE_RADAR_SECRETS": runtime.secrets}
                if any(env.get(key) != value for key, value in expected.items()):
                    raise DiagnosticError("Lambda environment does not match the stack outputs; redeploy the template.")
                concurrency = lam.get_function_concurrency(FunctionName=deployed["FunctionName"])
                if concurrency.get("ReservedConcurrentExecutions") == 0:
                    raise DiagnosticError("Lambda reserved concurrency is zero; all invocations are blocked.")
                return f"{deployed['FunctionName']}: active"

            def schedule() -> str:
                resources = [r for page in cf.get_paginator("list_stack_resources").paginate(StackName=stack)
                             for r in page["StackResourceSummaries"] if r["ResourceType"] == "AWS::Scheduler::Schedule"]
                if not resources:
                    raise DiagnosticError("No EventBridge schedule in the stack; redeploy deploy/aws/template.yaml.")
                scheduler = aws.client(clients.session, "scheduler")
                for resource in resources:
                    identifier = resource["PhysicalResourceId"].split(":schedule/")[-1].split("/")
                    args = {"Name": identifier[-1]}
                    if len(identifier) > 1:
                        args["GroupName"] = identifier[-2]
                    info = scheduler.get_schedule(**args)
                    if info["State"] != "ENABLED":
                        raise DiagnosticError("EventBridge schedule is disabled.")
                    if info["Target"]["Arn"].split(":function:")[-1].split(":")[0] != deployed["FunctionName"]:
                        raise DiagnosticError("EventBridge targets a different Lambda function.")
                return "Enabled and targets the monitor"

            check("Lambda", function)
            check("EventBridge schedule", schedule)
        elif deployed:
            checks.append(Check("Stack outputs", False, "Missing Role Radar outputs; check the stack name."))

    checks.append(Check("Automatic monitoring", runtime.storage == "dynamodb" and bool(runtime.config_url),
                        "Shared AWS state and config selected" if runtime.storage == "dynamodb" and runtime.config_url
                        else "Local mode only. Deploy the SAM stack and set runtime.storage/table/config_url/secrets from its outputs."))

    def config() -> str:
        source = ConfigSource(runtime, path, clients)
        loaded = source.load()
        count = sum(c.enabled for c in loaded.companies)
        if not count:
            raise DiagnosticError("No companies enabled.")
        if runtime.config_url and path and path.read_text(encoding="utf-8") != source.read_text():
            raise DiagnosticError("Local config differs from S3. Run role-radar config push after connecting the local runtime.")
        return f"{count} enabled company/companies; config readable"

    def state() -> str:
        backend = open_backend(runtime, "doctor", clients)
        backend.store.load_schedule()
        runs = backend.store.last_runs()
        if runtime.storage == "dynamodb":
            lease = backend.lease.read()
            if lease and lease.expires_at > utcnow().timestamp():
                return f"State readable; active runner: {lease.holder}"
            latest = max((run.get("finished_at", "") for run in runs.values()), default="")
            if not latest or (utcnow() - from_iso(latest)).total_seconds() > 900:
                raise DiagnosticError("No recent pass or active lease. Check EventBridge and Lambda logs.")
        failed = [r for r in runs.values() if r.get("undelivered")]
        return "State readable" + ("; a last pass had undelivered alerts (see status)" if failed else "")

    check("Companies config", config)
    check("State / recent activity", state)

    def channels() -> str:
        configured = NotifierSource(runtime, clients)()
        if not configured or all(isinstance(n, ConsoleNotifier) for n in configured):
            raise DiagnosticError("No delivery channels. Set DISCORD_WEBHOOK_URL and/or SMTP_HOST + EMAIL_TO in the selected secrets source.")
        broken = [n.problem for n in configured if isinstance(n, UnconfiguredNotifier)]
        if broken:
            raise DiagnosticError("; ".join(broken))
        return ", ".join(n.name for n in configured) + " configured; delivery not tested"

    check("Notifications", channels)
    if not stack:
        checks.append(Check("AWS deployment inspection", False,
                            "Use doctor --stack role-radar --profile PROFILE --region REGION to inspect Lambda and EventBridge."))
    return checks


class DiagnosticError(Exception):
    """An actionable error containing no secret values."""
