"""End-to-end smoke test for the KubeAgent REST + SSE API.

Starts the API on a free local port, ingests an Alertmanager webhook, triggers
the simulated OOM incident, streams the agent's live events, approves the
pending remediation and lists the recent incidents.

Usage::

    cd backend
    python test_api.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent
STARTUP_TIMEOUT_SECONDS = 30.0
STREAM_TIMEOUT_SECONDS = 60.0

ALERTMANAGER_PAYLOAD: dict[str, Any] = {
    "version": "4",
    "groupKey": '{}:{alertname="KubePodNotReady"}',
    "status": "firing",
    "receiver": "kubeagent",
    "groupLabels": {"alertname": "KubePodNotReady"},
    "commonLabels": {"alertname": "KubePodNotReady", "namespace": "default"},
    "commonAnnotations": {"summary": "Pods are not ready in default"},
    "externalURL": "http://alertmanager.monitoring.svc:9093",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "KubePodNotReady",
                "namespace": "default",
                "pod": "payment-api-7f9d8c6b5-4xk2p",
                "severity": "critical",
            },
            "annotations": {
                "summary": "Pod payment-api is not ready",
                "description": "payment-api has been unready for more than 5 minutes.",
            },
            "startsAt": "2026-11-14T09:41:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus.monitoring.svc:9090/graph",
            "fingerprint": "a1b2c3d4e5f60718",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "KubeNodeDiskPressure", "severity": "warning"},
            "annotations": {"summary": "disk pressure cleared"},
            "fingerprint": "deadbeefcafe0001",
        },
    ],
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(client: httpx.Client, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
    deadline = time.time() + timeout
    last_error = "no response"
    while time.time() < deadline:
        try:
            response = client.get("/healthz")
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"API did not become healthy within {timeout:.0f}s: {last_error}")


def _print_event(event: dict[str, Any]) -> None:
    kind = str(event.get("type", "?"))
    message = str(event.get("message") or "")
    tool = (event.get("data") or {}).get("tool")
    suffix = f" (tool={tool})" if tool else ""
    print(f"  [{kind}] {message}{suffix}")


def _stream_until_diagnosis(client: httpx.Client, incident_id: str) -> bool:
    """Print SSE events until the diagnosis arrives (or the stream ends)."""
    with client.stream(
        "GET", f"/api/v1/incidents/{incident_id}/stream", timeout=STREAM_TIMEOUT_SECONDS
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload:
                continue
            event = json.loads(payload)
            _print_event(event)
            if event.get("type") == "diagnosis.completed":
                return True
    return False


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": str(BASE_DIR),
        "SIMULATION_MODE": "true",
        "LOG_LEVEL": "WARNING",
        "ACCESS_LOG": "false",
    }

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BASE_DIR),
        env=env,
    )

    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            _wait_for_health(client)
            print(f"API healthy on {base_url}")

            print("\n1. Ingesting an Alertmanager webhook ...")
            response = client.post("/api/v1/incidents/webhook", json=ALERTMANAGER_PAYLOAD)
            response.raise_for_status()
            ingested = response.json()
            print(
                f"   created={ingested['created_count']} skipped={ingested['skipped']} "
                f"({ingested['message']})"
            )

            print("\n2. Triggering the OOMKilled chaos scenario (auto-analysis) ...")
            response = client.post(
                "/api/v1/incidents/simulate",
                json={"scenario": "OOMKilled", "auto_analyze": True},
            )
            response.raise_for_status()
            body = response.json()
            incident = body["incident"]
            print(
                f"   incident={incident['id']} status={incident['status']} "
                f"analysis_started={body['analysis_started']}"
            )

            print("\n3. Streaming live agent events ...")
            saw_diagnosis = _stream_until_diagnosis(client, incident["id"])
            if not saw_diagnosis:
                print("   (stream closed before a diagnosis arrived)")

            detail = client.get(f"/api/v1/incidents/{incident['id']}").json()
            diagnosis = detail.get("diagnosis") or {}
            print(
                f"\n4. Incident status={detail['status']} "
                f"severity={detail['severity']} "
                f"confidence={diagnosis.get('confidence')}"
            )
            if diagnosis:
                print(f"   root cause: {diagnosis.get('root_cause')}")
                for command in diagnosis.get("commands", [])[:3]:
                    print(f"   suggested: {command}")

            print("\n5. Human-in-the-loop approval ...")
            response = client.post(
                f"/api/v1/incidents/{incident['id']}/approve",
                json={
                    "approved_by": "oncall@kubeagent.dev",
                    "notes": "Reviewed: memory limit bump is safe to apply.",
                },
            )
            if response.status_code == 409:
                print(f"   409: {response.json()['detail']}")
            else:
                response.raise_for_status()
                approved = response.json()
                approval = approved.get("approval") or {}
                print(
                    f"   status={approved['status']} "
                    f"approved_by={approval.get('approved_by')} "
                    f"actions={len(approval.get('approved_actions') or [])}"
                )

            print("\n6. Recent incidents ...")
            for item in client.get("/api/v1/incidents").json():
                print(f"   {item['id']}  {item['status']:<18} {item['title'][:60]}")

            print("\nSSE + REST smoke test passed.")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
