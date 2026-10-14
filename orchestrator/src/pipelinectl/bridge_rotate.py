"""Replace the RU bridge on a provider with an API (VDSina): order, check, move, clean up.

A candidate server is checked BEFORE anything points at it, because a fresh address can be
filtered on the path (2026-10-13: an address whose TLS was dropped from 30 of 40 Russian
networks and to every foreign site). Gates, in order:
  reputation  the address carries no previous tenant's services or bad tags (InternetDB)
  outbound    TLS from the candidate to foreign sites, TCP to the Exit
  inbound     a temporary HTTPS server on the candidate, reached from Russian networks
A rejected candidate is deleted at once (it is billed) and another is ordered, up to
`attempts`. The move itself (configure, DNS, links, checks) is `rotate-chain.sh bridge
--bridge-ip`, the same path as a manual move. The old server is deleted only on request.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipelinectl import iprep, status, vdsina

OUTBOUND_HOSTS = ("www.google.com", "github.com", "www.cloudflare.com")
OUTBOUND_MIN_SHARE = 0.75
GATE_DIR = "/root/.pp-gate"

OUTBOUND_SCRIPT = r'''
import json, socket, ssl, sys
exit_ip, ports, hosts = sys.argv[1], [int(p) for p in sys.argv[2].split(",")], sys.argv[3].split(",")
ctx = ssl.create_default_context()
tls = total = tcp = 0
for host in hosts:
    for _ in range(4):
        total += 1
        try:
            with socket.create_connection((host, 443), timeout=6) as s, ctx.wrap_socket(s, server_hostname=host):
                tls += 1
        except Exception:
            pass
for _ in range(4):
    for port in ports:
        try:
            socket.create_connection((exit_ip, port), timeout=5).close()
            tcp += 1
            break
        except Exception:
            pass
print(json.dumps({"tls": tls, "tls_total": total, "exit_tcp": tcp, "exit_total": 4}))
'''

GATE_SERVER = r'''
import http.server, ssl, sys
class Page(http.server.BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    do_GET = do_HEAD = _ok
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(("0.0.0.0", 443), Page)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
server.socket = ctx.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''


@dataclass
class Candidate:
    server_id: int
    ip: str = ""
    passed: bool = False
    notes: list[str] = field(default_factory=list)


def _ssh(ip: str, user: str, key: Path) -> list[str]:
    return ["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", f"{user}@{ip}"]


def wait_ssh(ip: str, user: str, key: Path, runner: Any = subprocess.run,
             timeout: float = 600, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runner([*_ssh(ip, user, key), "true"], capture_output=True, timeout=30).returncode == 0:
            return True
        sleep(10)
    return False


def reputation_gate(ip: str, fetch: Callable[[str], tuple[Any, str]] = iprep.fetch) -> tuple[bool, str]:
    data, err = fetch(ip)
    if err:
        return True, f"reputation not checked ({err})"
    clean, reason = iprep.assess(data, allowed_ports=frozenset({22, 80, 443}))
    return clean, f"reputation: {reason}"


def outbound_gate(ip: str, user: str, key: Path, exit_ip: str, chain_port: int,
                  runner: Any = subprocess.run) -> tuple[bool, str]:
    # Any open Exit port proves the path is not blackholed; the chain port alone would fail
    # whenever the Exit firewall admits only the current bridge there.
    ports = ",".join(str(p) for p in dict.fromkeys((chain_port, 443, 22)))
    proc = runner([*_ssh(ip, user, key), "python3", "-", exit_ip, ports, ",".join(OUTBOUND_HOSTS)],
                  input=OUTBOUND_SCRIPT, capture_output=True, text=True, timeout=180)
    try:
        r = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"outbound check did not run: {(proc.stderr or '').strip()[:200]}"
    ok = r["tls"] >= OUTBOUND_MIN_SHARE * r["tls_total"] and r["exit_tcp"] >= 3
    return ok, (f"outbound: TLS to foreign sites {r['tls']}/{r['tls_total']},"
                f" TCP to the Exit {r['exit_tcp']}/{r['exit_total']}")


def inbound_gate(ip: str, user: str, key: Path, cert: Path, privkey: Path, host: str,
                 runner: Any = subprocess.run,
                 reach: Callable[[str, str], tuple[int, int]] = status.outside_reach) -> tuple[bool, str]:
    """Serve HTTPS with the real certificate for a few minutes and ask Russian networks."""
    ssh = _ssh(ip, user, key)
    files = {"cert.pem": cert.read_text(), "key.pem": privkey.read_text(), "srv.py": GATE_SERVER}
    setup = f"umask 077; mkdir -p {GATE_DIR} && python3 -c 'import json,sys; [open(\"{GATE_DIR}/\"+k,\"w\").write(v) for k,v in json.load(sys.stdin).items()]'"
    start = (f"nohup timeout 300 python3 {GATE_DIR}/srv.py {GATE_DIR}/cert.pem {GATE_DIR}/key.pem"
             f" </dev/null >/dev/null 2>&1 & sleep 2; python3 -c \"import socket; socket.create_connection(('127.0.0.1', 443), 3)\"")
    cleanup = f"pkill -f '{GATE_DIR}/srv.py'; rm -rf {GATE_DIR}"
    try:
        if runner([*ssh, setup], input=json.dumps(files), capture_output=True, text=True, timeout=60).returncode:
            return False, "inbound check: could not copy the test server"
        if runner([*ssh, start], capture_output=True, text=True, timeout=60).returncode:
            return False, "inbound check: test HTTPS server did not start (port 443 busy?)"
        try:
            reached, asked = reach(ip, host)
        except (OSError, ValueError, KeyError) as e:
            return True, f"inbound: not measured ({e}) — outbound and reputation passed"
        ok = asked > 0 and reached / asked >= status.OUTSIDE_MIN_SHARE
        return ok, f"inbound: {reached} of {asked} Russian networks completed HTTPS"
    finally:
        runner([*ssh, cleanup], capture_output=True, text=True, timeout=60)


def check_candidate(ip: str, user: str, key: Path, *, exit_ip: str, chain_port: int, cert: Path,
                    privkey: Path, host: str, runner: Any = subprocess.run,
                    reach: Callable[[str, str], tuple[int, int]] = status.outside_reach,
                    fetch: Callable[[str], tuple[Any, str]] = iprep.fetch) -> tuple[bool, list[str]]:
    notes = []
    for gate in (
        lambda: reputation_gate(ip, fetch),
        lambda: outbound_gate(ip, user, key, exit_ip, chain_port, runner),
        lambda: inbound_gate(ip, user, key, cert, privkey, host, runner, reach),
    ):
        ok, note = gate()
        notes.append(("pass  " if ok else "FAIL  ") + note)
        if not ok:
            return False, notes
    return True, notes


def find_good_server(
    request: vdsina.Request,
    spec: dict[str, int],
    ssh_key_id: int,
    check: Callable[[str], tuple[bool, list[str]]],
    ready: Callable[[str], bool],
    attempts: int,
    echo: Callable[[str], None],
    name: str,
    wait: Callable[[int], str] | None = None,
) -> Candidate | None:
    """Order servers until one passes every gate; each rejected one is deleted at once."""
    wait = wait or (lambda sid: vdsina.wait_ready(request, sid))
    for attempt in range(1, attempts + 1):
        cand = Candidate(vdsina.create_server(request, spec, ssh_key_id, f"{name}-{attempt}"))
        echo(f"[bridge] attempt {attempt}/{attempts}: server {cand.server_id} ordered, waiting for it …")
        try:
            cand.ip = wait(cand.server_id)
            if not ready(cand.ip):
                cand.notes = [f"FAIL  no SSH on {cand.ip}"]
            else:
                cand.passed, cand.notes = check(cand.ip)
        except (TimeoutError, RuntimeError, OSError, vdsina.ApiError) as e:
            cand.notes = [f"FAIL  {e}"]
        for note in cand.notes:
            echo(f"[bridge]   {cand.ip or '?'}: {note}")
        if cand.passed:
            return cand
        echo(f"[bridge] deleting rejected server {cand.server_id} ({cand.ip or 'no address'})")
        vdsina.delete_server(request, cand.server_id)
    return None
