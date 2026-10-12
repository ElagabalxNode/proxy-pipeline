"""Cherry Servers API helpers: token discovery and live per-region stock.

Cherry only tells you a region is out of stock when the server is created — after `-replace`
has already destroyed the old Exit (2026-09-08). So stock is checked up front, here, and the
same data is what `pipelinectl stock` shows an operator choosing a region.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipelinectl import paths

API = "https://api.cherryservers.com/v1"
Fetch = Callable[[str, str], Any]  # (path, token) -> parsed JSON

_KV_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_kv_file(path: Path) -> dict[str, str]:
    """Parse KEY=value / key = "value" files (.env, terraform.tfvars). Trailing comments dropped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if raw[:1] in ("'", '"'):
            end = raw.find(raw[0], 1)
            val = raw[1:end] if end > 0 else raw[1:]
        else:
            val = raw.split("#", 1)[0].strip()
        out[key] = val
    return out


def tfvars(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    return read_kv_file(paths.terraform_dir(cfg) / "terraform.tfvars")


def api_token(cfg: dict[str, Any] | None = None) -> str:
    """CHERRY_API_TOKEN from the environment, then .env, then terraform.tfvars (cherry_api_token)."""
    return (
        os.environ.get("CHERRY_API_TOKEN", "").strip()
        or read_kv_file(paths.repo_root() / ".env").get("CHERRY_API_TOKEN", "").strip()
        or tfvars(cfg).get("cherry_api_token", "").strip()
    )


def _http_get(path: str, token: str) -> Any:
    req = urllib.request.Request(
        API + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _slug(obj: dict[str, Any]) -> str:
    return str(obj.get("slug") or obj.get("name") or "")


def is_cloud_vps(plan_slug: str) -> bool:
    """Cherry cloud-VPS plans: Gen-1 `B1-*` (LT/NL/US/SG only) and Gen-2 `B2-*` (all regions)."""
    return re.match(r"^B[12]-", plan_slug) is not None


def plans_stock(
    token: str,
    fetch: Fetch = _http_get,
    plan_filter: Callable[[str], bool] | None = None,
) -> dict[str, dict[str, int]]:
    """{plan_slug: {region_slug: stock_qty}} across every team the token can see.

    A region missing from a plan's map is not sold there at all (e.g. B1-* in DE-Frankfurt).
    """
    out: dict[str, dict[str, int]] = {}
    for team in fetch("/teams", token):
        for plan in fetch(f"/teams/{team['id']}/plans", token):
            slug = _slug(plan)
            if plan_filter and not plan_filter(slug):
                continue
            regions = out.setdefault(slug, {})
            for ar in plan.get("available_regions") or []:
                try:
                    qty = int(ar.get("stock_qty") or 0)
                except (TypeError, ValueError):
                    qty = 0
                r = _slug(ar)
                regions[r] = max(qty, regions.get(r, 0))
    return out


def region_stock(data: dict[str, dict[str, int]], plan: str, region: str) -> int | None:
    """Stock for plan@region; 0 when the plan is not sold in that region; None if plan unknown."""
    if plan not in data:
        return None
    return data[plan].get(region, 0)
