from __future__ import annotations

import json

from .config import settings


def enqueue(run_id: str, attempt: int = 0) -> None:
    """Create a deterministic Cloud Task; duplicate calls are safe."""
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import tasks_v2
    if not all((settings.gcp_project_id, settings.dispatch_url, settings.dispatch_service_account, settings.internal_audience)):
        raise RuntimeError("Cloud Tasks dispatch is not configured")
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.gcp_project_id, settings.gcp_tasks_location, settings.gcp_tasks_queue)
    task_name = f"{parent}/tasks/run-{run_id}-{attempt}"
    task = {"name": task_name, "http_request": {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": settings.dispatch_url,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"run_id": run_id}).encode(),
        "oidc_token": {"service_account_email": settings.dispatch_service_account,
                       "audience": settings.internal_audience},
    }}
    try:
        client.create_task(request={"parent": parent, "task": task})
    except AlreadyExists:
        pass


def launch_job(run_id: str, user_id: str) -> None:
    from google.cloud import run_v2
    if not (settings.gcp_project_id and settings.gcp_run_job):
        raise RuntimeError("Cloud Run Job dispatch is not configured")
    client = run_v2.JobsClient()
    name = client.job_path(settings.gcp_project_id, settings.gcp_region, settings.gcp_run_job)
    client.run_job(request={"name": name, "overrides": {"container_overrides": [
        {"env": [{"name": "RUN_ID", "value": run_id}, {"name": "USER_ID", "value": user_id}]}
    ], "timeout": {"seconds": 21600}}})


def launch_reel_job(run_id: str, user_id: str, reel_id: str) -> None:
    from google.cloud import run_v2
    if not (settings.gcp_project_id and settings.gcp_run_job):
        raise RuntimeError("Cloud Run Job dispatch is not configured")
    runner = run_v2.JobsClient()
    runner.run_job(request={"name": runner.job_path(settings.gcp_project_id, settings.gcp_region, settings.gcp_run_job),
        "overrides": {"container_overrides": [{"env": [
            {"name": "RUN_ID", "value": run_id}, {"name": "USER_ID", "value": user_id},
            {"name": "REEL_ID", "value": reel_id}]}], "timeout": {"seconds": 6000}}})
