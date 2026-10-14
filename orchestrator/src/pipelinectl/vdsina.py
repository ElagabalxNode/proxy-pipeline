"""VDSina public API (https://userapi.vdsina.ru/v1): just what a bridge rotation needs.

Find the bridge's server by its address, copy its datacenter/plan/template, make sure the
controller's SSH key is registered, create a server, wait for its address, delete a server.
Creating a server costs money (the plan is billed per day); callers confirm first.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pipelinectl import cherry, paths

API = "https://userapi.vdsina.ru/v1"
KEY_NAME = "proxy-pipeline"
Request = Callable[[str, str, "dict[str, Any] | None"], Any]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"VDSina API {status}: {message}")
        self.status = status


def token() -> str:
    """VDSINA_API_TOKEN from the environment, then .env."""
    return (
        os.environ.get("VDSINA_API_TOKEN", "").strip()
        or cherry.read_kv_file(paths.repo_root() / ".env").get("VDSINA_API_TOKEN", "").strip()
    )


def api(auth: str) -> Request:
    def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {auth}", "Content-Type": "application/json",
                     "User-Agent": "proxy-pipeline"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https URL
                return json.load(resp).get("data")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode(errors="replace"))
                message = payload.get("description") or payload.get("status_msg") or str(payload)
            except ValueError:
                message = e.reason
            raise ApiError(e.code, str(message)) from e

    return request


def find_server(request: Request, ip: str) -> dict[str, Any] | None:
    """The server that owns `ip`, as its main address or as an additional one."""
    servers = request("GET", "/server", None) or []
    for server in servers:
        if str((server.get("ip") or {}).get("ip") or "") == ip:
            return server
    for service in request("GET", "/server-ip", None) or []:
        if any(str(a.get("ip")) == ip for a in service.get("ip") or []):
            owner = (service.get("server") or {}).get("id")
            return next((s for s in servers if s.get("id") == owner), None)
    return None


def spec_of(request: Request, server_id: int) -> dict[str, int]:
    """Datacenter, plan and OS template of an existing server: what its replacement gets."""
    data = request("GET", f"/server/{server_id}", None) or {}
    return {
        "datacenter": int(data["datacenter"]["id"]),
        "server-plan": int(data["server-plan"]["id"]),
        "template": int(data["template"]["id"]),
    }


def plan_cost(request: Request, plan_id: int) -> str:
    """'20 RUB/day'-style price of a plan, or '' when the plan list does not show it."""
    currency = (request("GET", "/account.balance", None) or {}).get("currency", "")
    for group in request("GET", "/server-group", None) or []:
        for plan in request("GET", f"/server-plan/{group['id']}", None) or []:
            if plan.get("id") == plan_id:
                return f"{plan.get('cost')} {currency}/{plan.get('period', 'day')}".strip()
    return ""


def ensure_ssh_key(request: Request, public_key: str) -> int:
    """ID of the controller's key in the account; registered once (free)."""
    wanted = " ".join(public_key.split()[:2])
    for item in request("GET", "/ssh-key", None) or []:
        data = (request("GET", f"/ssh-key/{item['id']}", None) or {}).get("data", "")
        if " ".join(str(data).split()[:2]) == wanted:
            return int(item["id"])
    created = request("POST", "/ssh-key", {"name": KEY_NAME, "data": public_key.strip()}) or {}
    return int(created["id"])


def create_server(request: Request, spec: dict[str, int], ssh_key: int, name: str) -> int:
    body = {**spec, "name": name, "ssh-key": ssh_key, "schedule": "0"}  # no paid backup schedule
    created = request("POST", "/server", body) or {}
    return int(created["id"])


def wait_ready(
    request: Request, server_id: int, timeout: float = 900, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Public IPv4 of a new server once it is active; raises TimeoutError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = request("GET", f"/server/{server_id}", None) or {}
        ip = str((data.get("ip") or {}).get("ip") or "")
        if data.get("status") == "active" and ip and not data.get("status_text"):
            return ip
        if data.get("status") in ("block", "notpaid", "deleted"):
            raise RuntimeError(f"server {server_id} is {data.get('status')}")
        sleep(15)
    raise TimeoutError(f"server {server_id} not active after {int(timeout)}s")


def delete_server(request: Request, server_id: int) -> None:
    request("DELETE", f"/server/{server_id}", None)
