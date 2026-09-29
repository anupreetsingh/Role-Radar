from unittest.mock import MagicMock

import pytest

from role_radar import aws
from role_radar.backends import AwsClients
from role_radar.config import RuntimeSettings
from role_radar.diagnostics import diagnose
from role_radar.dynamo import DynamoStateStore
from role_radar.storage import to_iso, utcnow


@pytest.fixture
def deployment(table, monkeypatch):
    import boto3

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="role-radar-config")
    s3.put_object(Bucket="role-radar-config", Key="companies.yaml",
                  Body=b"defaults: {filters: {include_keywords: [engineer]}}\n"
                       b"companies: [{name: Acme, url: 'https://jobs.lever.co/acme'}]")
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/role-radar/DISCORD_WEBHOOK_URL", Value="https://discord.invalid/private-token", Type="SecureString")
    runtime = RuntimeSettings(storage="dynamodb", table=table[1], config_url="s3://role-radar-config/companies.yaml",
                              secrets="ssm:/role-radar/", region="us-east-1")
    DynamoStateStore(*table).record_run("lambda", {"finished_at": to_iso(utcnow()), "checked": 1})
    cloudformation, lam, scheduler = MagicMock(), MagicMock(), MagicMock()
    cloudformation.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": [
        {"OutputKey": k, "OutputValue": v} for k, v in {
            "TableName": table[1], "ConfigUrl": runtime.config_url, "SecretsPath": "/role-radar/", "FunctionName": "role-radar-monitor"
        }.items()]}]}
    cloudformation.get_paginator.return_value.paginate.return_value = [{"StackResourceSummaries": [
        {"ResourceType": "AWS::Scheduler::Schedule", "PhysicalResourceId": "monitor-schedule"}]}]
    lam.get_function_configuration.return_value = {
        "State": "Active", "LastUpdateStatus": "Successful", "Handler": "lambda_handler.handler",
        "Environment": {"Variables": {"ROLE_RADAR_STORAGE": "dynamodb", "ROLE_RADAR_TABLE": runtime.table,
            "ROLE_RADAR_CONFIG_URL": runtime.config_url, "ROLE_RADAR_SECRETS": runtime.secrets}}}
    lam.get_function_concurrency.return_value = {"ReservedConcurrentExecutions": 1}
    scheduler.get_schedule.return_value = {"State": "ENABLED", "Target": {"Arn": "arn:aws:lambda:us-east-1:123456789012:function:role-radar-monitor"}}
    original = aws.client
    replacements = {"cloudformation": cloudformation, "lambda": lam, "scheduler": scheduler}
    monkeypatch.setattr(aws, "client", lambda session, service: replacements[service] if service in replacements else original(session, service))
    return runtime, lam, scheduler


def test_healthy_deployment_checks_are_read_only(deployment):
    runtime, lam, scheduler = deployment
    checks = diagnose(runtime, None, AwsClients(runtime), "role-radar")
    assert all(check.ok for check in checks), checks
    assert all("private-token" not in check.detail for check in checks)
    assert {call[0] for call in lam.mock_calls} == {"get_function_configuration", "get_function_concurrency"}
    assert {call[0] for call in scheduler.mock_calls} == {"get_schedule"}


def test_disabled_scheduler_is_reported(deployment):
    runtime, _, scheduler = deployment
    scheduler.get_schedule.return_value["State"] = "DISABLED"
    checks = diagnose(runtime, None, AwsClients(runtime), "role-radar")
    assert any(not c.ok and "disabled" in c.detail for c in checks)


def test_zero_lambda_concurrency_is_reported(deployment):
    runtime, lam, _ = deployment
    lam.get_function_concurrency.return_value = {"ReservedConcurrentExecutions": 0}
    checks = diagnose(runtime, None, AwsClients(runtime), "role-radar")
    assert any(not c.ok and "all invocations are blocked" in c.detail for c in checks)


def test_stale_lambda_activity_is_reported(deployment):
    runtime, _, _ = deployment
    clients = AwsClients(runtime)
    DynamoStateStore(clients.dynamodb, runtime.table).record_run("lambda", {"finished_at": "2000-01-01T00:00:00Z"})
    checks = diagnose(runtime, None, clients, "role-radar")
    assert any(not c.ok and "No recent pass" in c.detail for c in checks)
