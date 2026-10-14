"""VDSina address search helper. The token is read from the repo .env and never printed.

Run with the repo venv (needs PyYAML): .venv/bin/python scripts/vdsina_ip.py ...

  overview                       servers, addresses, balance (read-only)
  check <ip> [--ru N] [--world N]  can Russian / foreign networks finish TLS to <ip>:443 (read-only)
  order <serverID>               order ONE additional IPv4 (costs money)
  delete <serverID> <ipID>       delete an additional IPv4
"""

from __future__ import annotations

import collections
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
API = "https://userapi.vdsina.ru/v1"
GLOBALPING = "https://api.globalping.io/v1/measurements"
CONFIG = ROOT / "config" / "pipeline.yaml"


def entry_host() -> str:
    """bridge_entry.domain from config/pipeline.yaml: the name the HTTPS check asks for."""
    import yaml

    domain = ((yaml.safe_load(CONFIG.read_text()) or {}).get("bridge_entry") or {}).get("domain")
    if not domain:
        sys.exit("bridge_entry.domain is not set in config/pipeline.yaml")
    return str(domain)


def token() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("VDSINA_API_TOKEN="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value:
                return value
    sys.exit("VDSINA_API_TOKEN is not set in .env")


def vdsina(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json",
                 "User-Agent": "proxy-pipeline"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        sys.exit(f"VDSina {method} {path}: HTTP {e.code} {detail}")


def overview() -> None:
    balance = vdsina("GET", "/account.balance").get("data")
    print("balance:", json.dumps(balance, ensure_ascii=False))
    for server in vdsina("GET", "/server").get("data") or []:
        keep = {k: server.get(k) for k in ("id", "name", "status", "created", "end")}
        for key in ("datacenter", "server-plan", "template"):
            if isinstance(server.get(key), dict):
                keep[key] = server[key].get("name") or server[key].get("country")
        if isinstance(server.get("ip"), (list, dict)):
            keep["ip"] = server["ip"]
        print("server:", json.dumps(keep, ensure_ascii=False))
    for address in vdsina("GET", "/ip").get("data") or []:
        keep = {k: address.get(k) for k in ("id", "ip", "type", "netmask", "gateway", "host")}
        for key in ("datacenter", "service", "server"):
            if key in address:
                keep[key] = address[key]
        print("address:", json.dumps(keep, ensure_ascii=False))
    print("additional-address services:", json.dumps(vdsina("GET", "/server-ip").get("data"), ensure_ascii=False)[:1500])


def _globalping(body: dict | None = None, url: str = GLOBALPING) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json", "User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def reach(ip: str, locations: list[dict], limit: int) -> tuple[int, int, list[str]]:
    """(reached, total, rows). Reached = the TLS handshake got an answer from the server."""
    started = _globalping({
        "type": "http", "target": ip, "limit": limit, "locations": locations,
        "measurementOptions": {"protocol": "HTTPS", "port": 443,
                               "request": {"method": "HEAD", "path": "/", "host": entry_host()}},
    })
    data: dict = {}
    for _ in range(25):
        time.sleep(2)
        data = _globalping(url=f"{GLOBALPING}/{started['id']}")
        if data["status"] != "in-progress":
            break
    rows, reached = [], 0
    for item in data.get("results", []):
        probe, result = item["probe"], item["result"]
        raw = (result.get("rawOutput") or result.get("status") or "").replace("\n", " ")
        code = result.get("statusCode")
        good = bool(code) or ("timed out" not in raw.lower() and result.get("status") == "finished")
        reached += good
        rows.append(f'   {"OK  " if good else "FAIL"} {probe["country"]} {probe.get("city", "")[:18]:<18} '
                    f'AS{probe["asn"]:<7} {probe["network"][:30]:<30} {code or raw[:55]}')
    return reached, len(rows), sorted(rows)


def check(ip: str, ru: int, world: int) -> None:
    tally = collections.OrderedDict()
    for label, locations, limit in (
        ("Russia", [{"country": "RU"}], ru),
        ("other countries", [{"country": c} for c in ("DE", "NL", "FI", "KZ", "US", "TR", "PL", "FR")], world),
    ):
        if limit <= 0:
            continue
        ok, total, rows = reach(ip, locations, limit)
        tally[label] = (ok, total)
        print(f"{ip} from {label}: {ok} of {total} networks reached the server")
        print("\n".join(rows))
    print("SUMMARY", ip, " ".join(f"{k}={ok}/{total}" for k, (ok, total) in tally.items()))


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["overview"]:
        overview()
    elif args[:1] == ["check"] and len(args) >= 2:
        opts = dict(zip(args[2::2], args[3::2]))
        check(args[1], int(opts.get("--ru", 30)), int(opts.get("--world", 10)))
    elif args[:1] == ["order"] and len(args) == 2:
        print(json.dumps(vdsina("POST", f"/server.ip/{int(args[1])}", {"type": "4", "count": 1}), ensure_ascii=False))
    elif args[:1] == ["delete"] and len(args) == 3:
        print(json.dumps(vdsina("PUT", f"/server.ip/{int(args[1])}", {"type": "4", "delete": [int(args[2])]}), ensure_ascii=False))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
