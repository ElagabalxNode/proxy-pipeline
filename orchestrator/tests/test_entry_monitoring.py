"""Monitoring of the bridge's HTTPS entry: probe checks on RU, verdicts, alerts, controller view."""

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from pipelinectl import alerts, chainlog, deep_health, health, watchdog
from test_bridge_apply import executable
from test_bridge_https import DOMAIN, TlsSite
from test_bridge_https import pki as pki_fixture

pki = pki_fixture
PROBE = Path(__file__).resolve().parents[2] / "ansible/roles/bridge_xray_relay/templates/chain-probe.sh.j2"

TOOL = r'''
import os, sys
from pathlib import Path
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with (Path(__file__).parent / "calls.log").open("a") as log:
    log.write(" ".join([tool, *args]) + "\n")
scenario = os.environ.get("PROBE_SCENARIO", "")
if tool == "openssl" and args[0] == "s_client":
    print("CONNECTION ESTABLISHED")
elif tool == "openssl" and args[0] == "x509":
    sys.exit(1 if scenario == "cert_expiring" else 0)
elif tool == "curl":
    if "--resolve" in args:
        if scenario == "entry_down":
            print("curl: (60) SSL certificate problem: certificate has expired", file=sys.stderr)
            sys.exit(60)
        print("200", end="")
    else:
        print("204 0.150", end="")
'''


def render_probe(tmp_path, domain):
    env = Environment(undefined=StrictUndefined)
    text = env.from_string(PROBE.read_text()).render(
        chain_probe_log=str(tmp_path / "probe.jsonl"), chain_probe_interval="60s",
        chain_probe_url="https://probe.example/204", chain_probe_socks_port=1080,
        chain_probe_direct_port=1081, chain_probe_timeout=5, chain_probe_cert_days=14,
        exit_state={"exit_ip": "192.0.2.1", "chain_port": 8443, "reality_sni": "direct.example",
                    "chain_reality_sni": "chain.example"},
        bridge_reality_dest="dest.example:443", bridge_reality_sni="dest.example",
        bridge_dest_resolver="", bridge_https_domain=domain, bridge_https_port=443,
        bridge_https_dir="/etc/nginx/proxy-pipeline",
    )
    script = tmp_path / "chain-probe.sh"
    script.write_text(text)
    return script


def run_probe(tmp_path, domain, scenario=""):
    script = render_probe(tmp_path, domain)
    for tool in ("openssl", "curl"):
        executable(tmp_path / tool, TOOL)
    (tmp_path / "probe.jsonl").unlink(missing_ok=True)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    subprocess.run(["bash", str(script)], check=True, timeout=60,
                   env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PROBE_SCENARIO": scenario})
    return {r["check"]: r for r in map(json.loads, (tmp_path / "probe.jsonl").read_text().splitlines())}


def test_probe_checks_the_https_entry_only_when_it_exists(tmp_path):
    assert set(run_probe(tmp_path, "")) == {"tls_exit", "chain_http", "direct_http", "dest_tls"}
    round_ = run_probe(tmp_path, "lk.example.ru")
    assert set(round_) == {"tls_exit", "chain_http", "direct_http", "dest_tls", "entry_https", "entry_cert"}
    assert round_["entry_https"]["ok"] is True and round_["entry_https"]["code"] == "200"
    assert round_["entry_cert"]["ok"] is True
    calls = (tmp_path / "calls.log").read_text()
    # The name and certificate are checked for real, but the request never leaves the host.
    assert "--resolve lk.example.ru:443:127.0.0.1" in calls and "https://lk.example.ru:443/" in calls
    assert "-checkend 1209600" in calls and "/etc/nginx/proxy-pipeline/fullchain.pem" in calls
    # The hop check uses the chain's own SNI, not the direct inbound's.
    assert "-servername chain.example" in calls and "direct.example" not in calls


def test_probe_reports_a_broken_entry_and_an_expiring_certificate(tmp_path):
    down = run_probe(tmp_path, "lk.example.ru", "entry_down")
    assert down["entry_https"]["ok"] is False and "curl rc=60" in down["entry_https"]["err"]
    assert down["chain_http"]["ok"] is True and down["entry_cert"]["ok"] is True
    expiring = run_probe(tmp_path, "lk.example.ru", "cert_expiring")
    assert expiring["entry_cert"]["ok"] is False and "14 days" in expiring["entry_cert"]["err"]
    assert expiring["entry_https"]["ok"] is True


def test_entry_verdicts_never_point_at_the_exit():
    entry = chainlog.verdict({"entry_https"})
    assert "HTTPS entry" in entry and "clients cannot connect" in entry
    assert "expires soon" in chainlog.verdict({"entry_cert"})
    assert "HTTPS entry" in chainlog.verdict({"entry_https", "entry_cert", "dest_tls"})
    for failed in ({"entry_https"}, {"entry_cert"}, {"entry_https", "dest_tls"}):
        assert not watchdog.rotation_would_help(chainlog.verdict_with_dest(failed)), failed
    # With a real Exit failure the Exit verdict stays, and the entry is an added note.
    both = chainlog.verdict_with_dest({"chain_http", "tls_exit", "entry_https", "entry_cert"})
    assert watchdog.rotation_would_help(both)
    assert "HTTPS entry down" in both and "certificate expires soon" in both
    assert chainlog.verdict_with_dest({"chain_http", "tls_exit", "dest_tls"}).endswith(
        " + Reality dest down (client handshakes stall)")


def records(pattern, start):
    out = []
    for minute, failed in enumerate(pattern):
        ts = start + timedelta(minutes=minute)
        for check in chainlog.CHECKS:
            out.append({"_ts": ts, "check": check, "ok": check not in failed, "ms": 100})
    return out


def test_entry_outage_alerts_as_down_but_an_expiring_certificate_does_not():
    start = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    now = start + timedelta(minutes=3)
    down = alerts.evaluate(records([set(), {"entry_https"}, {"entry_https"}, {"entry_https"}], start), now)
    assert down.status == "down" and "HTTPS entry" in down.detail
    assert not watchdog.rotation_would_help(down.detail)
    assert alerts.evaluate(records([{"entry_cert"}] * 4, start), now).status == "ok"
    assert alerts.evaluate(records([{"entry_https"}, set(), {"entry_https"}, set()], start), now).status == "ok"


def test_summary_hides_entry_rows_for_a_bridge_without_the_entry(tmp_path):
    start = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    legacy = [r for r in records([set()], start) if not r["check"].startswith("entry_")]
    assert set(chainlog.summarize(legacy)) == {"tls_exit", "chain_http", "direct_http", "dest_tls"}
    assert "entry_https" in chainlog.summarize(records([{"entry_https"}], start))
    # Records with the new check names are kept when the log is loaded.
    (tmp_path / "probe.jsonl").write_text(
        '{"ts":"2026-10-13T12:00:00Z","check":"entry_https","ok":false,"ms":5,"code":"","err":"x"}\n')
    assert [r["check"] for r in chainlog.load_records(tmp_path)] == ["entry_https"]


def test_controller_sees_dns_and_certificate_of_the_entry_separately(pki):
    site = TlsSite(pki)
    ca = str(pki / "ca.crt")

    def resolver(*addresses):
        return lambda name, port, type=None: [(socket.AF_INET, type, 6, "", (a, port)) for a in addresses]
    try:
        good = deep_health._entry_https_probe(DOMAIN, "127.0.0.1", site.port, ca, resolver("127.0.0.1"))
        assert [(c.name, c.ok) for c in good] == [("entry_dns", True), ("entry_https", True)]
        assert "200" in good[1].detail and "TLS" in good[1].detail

        elsewhere = deep_health._entry_https_probe(DOMAIN, "127.0.0.1", site.port, ca, resolver("192.0.2.9"))
        assert not elsewhere[0].ok and "expected only 127.0.0.1" in elsewhere[0].detail and elsewhere[1].ok
        split = deep_health._entry_https_probe(DOMAIN, "127.0.0.1", site.port, ca, resolver("127.0.0.1", "192.0.2.9"))
        assert not split[0].ok

        def unresolvable(name, port, type=None):
            raise socket.gaierror("Name or service not known")
        missing = deep_health._entry_https_probe(DOMAIN, "127.0.0.1", site.port, ca, unresolvable)
        assert not missing[0].ok and "does not resolve" in missing[0].detail and missing[1].ok

        untrusted = deep_health._entry_https_probe(DOMAIN, "127.0.0.1", site.port, None, resolver("127.0.0.1"))
        assert untrusted[0].ok and not untrusted[1].ok and "certificate:" in untrusted[1].detail
        assert untrusted[1].severity == "error"  # a bad certificate is bad from anywhere
        wrong_name = deep_health._entry_https_probe("other.invalid", "127.0.0.1", site.port, ca, resolver("127.0.0.1"))
        assert not wrong_name[1].ok
    finally:
        site.close()
    closed = deep_health._entry_https_probe(DOMAIN, "127.0.0.1", site.port, ca, resolver("127.0.0.1"))
    # Unreachable from the controller alone is a warning: clients use a different path.
    assert not closed[1].ok and closed[1].severity == "warn"
    assert "not reachable from the controller" in closed[1].detail


def test_reference_checks_of_the_bridge_do_not_fail_the_report(tmp_path, monkeypatch):
    rounds = [{"ts": "t", "check": c, "ok": c in ok, "ms": 1, "code": "", "err": ""}
              for ok in ({"tls_exit", "chain_http", "entry_https", "entry_cert"},)
              for c in ("tls_exit", "chain_http", "direct_http", "dest_tls", "entry_https", "entry_cert")]
    monkeypatch.setattr(deep_health, "_bridge_probe_round", lambda *a, **k: (True, "", rounds))
    monkeypatch.setattr(deep_health, "_exit_public_ip_via_ssh", lambda *a: (True, "192.0.2.1"))
    monkeypatch.setattr(deep_health, "_tls_reality_probe", lambda *a: (True, "ok"))
    monkeypatch.setattr(deep_health, "_entry_https_probe", lambda *a, **k: [])

    class NoTunnel:
        def __enter__(self):
            raise OSError("not under test")

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(deep_health, "_panel_tunnel", lambda *a, **k: NoTunnel())

    def severities(data):
        rpt = deep_health.run_deep_health(data, tmp_path / "k", "pw", "root", bridge_ip="198.51.100.2",
                                          bridge_user="root", bridge_key=tmp_path / "k")
        return {c.name: c.severity for c in rpt.results if c.name.startswith("bridge_") and not c.ok}

    base = {"exit_ip": "192.0.2.1"}
    # Without the HTTPS entry the Reality dest is the client entry: its failure stays an error.
    assert severities(base) == {"bridge_direct_http": "warn", "bridge_dest_tls": "error"}
    assert severities({**base, "bridge_domain": "lk.example.ru"}) == {
        "bridge_direct_http": "warn", "bridge_dest_tls": "warn"}


def test_health_checks_the_listeners_the_bridge_really_has(tmp_path, monkeypatch):
    (tmp_path / "runtime.json").write_text(json.dumps({"exit_ip": "192.0.2.1"}))
    seen = []
    monkeypatch.setattr(health, "_tcp_open", lambda host, port, timeout=5.0: seen.append((host, port)) or True)
    monkeypatch.setattr(health, "_ssh_ok", lambda *a, **k: (False, "not tested here"))

    def bridge_ports(bridge):
        seen.clear()
        (tmp_path / "bridge_runtime.json").write_text(json.dumps({"bridge_ip": "198.51.100.2", **bridge}))
        rpt = health.run_health(tmp_path, tmp_path / "key", tmp_path / "key")
        return [c.name for c in rpt.results if c.name.startswith("tcp_bridge_")]

    assert bridge_ports({}) == ["tcp_bridge_443"]
    assert bridge_ports({"bridge_reality_port": 8443, "bridge_domain": "lk.example.ru",
                         "bridge_https_port": 443}) == ["tcp_bridge_8443", "tcp_bridge_443"]
    assert ("198.51.100.2", 8443) in seen and ("198.51.100.2", 443) in seen
