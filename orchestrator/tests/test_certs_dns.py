"""Bridge DNS record at Timeweb and the controller-side wildcard certificate (pinned lego)."""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pipelinectl import certs, cli, timeweb_dns
from test_bridge_apply import executable

ZONE, HOST, ADDRESS = "example.ru", "lk.example.ru", "198.51.100.2"


class FakeZone:
    """Timeweb's shape: a host's records are listed under the host; an unknown host is a 404."""

    def __init__(self, *rows):
        self.rows = [{"id": n, "type": t, "ttl": 600, "fqdn": f"{name}.{ZONE}" if name else ZONE,
                      "data": {"value": value}} for n, (t, name, value) in enumerate(rows, 1)]
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        version, name = path.split("/")[1], path.split("/")[3]
        # As measured on the real API: v1 rejects a record without a subdomain name.
        if method == "POST" and version == "v1" and not body.get("subdomain"):
            raise timeweb_dns.ApiError(400, "bad_subdomain_name")
        if method == "GET":
            found = [r for r in self.rows if r["fqdn"] == name]
            if not found and name != ZONE:
                raise timeweb_dns.ApiError(404, "Domain not found")
            return {"dns_records": json.loads(json.dumps(found))}
        if method == "DELETE":
            # As measured on the real API: a host's record is deleted under the host, not the zone.
            wanted = path.rsplit("/", 1)[1]
            if not any(str(r["id"]) == wanted and r["fqdn"] == name for r in self.rows):
                raise timeweb_dns.ApiError(404, "Domain or DNS record not found")
            self.rows = [r for r in self.rows if str(r["id"]) != wanted]
            return {}
        assert name == ZONE, "records are created through the zone"
        if method == "POST":
            owner = f"{body['subdomain']}.{ZONE}" if body.get("subdomain") else ZONE
            self.rows.append({"id": 100 + len(self.rows), "type": body["type"], "ttl": 600,
                              "fqdn": owner, "data": {"value": body["value"]}})
            return {"dns_record": self.rows[-1]}
        raise AssertionError(f"unexpected {method}")

    def writes(self):
        return [(m, b) for m, _, b in self.calls if m != "GET"]


def test_a_record_is_created_once_and_mail_records_are_left_alone():
    zone = FakeZone(("MX", None, "mx1.timeweb.ru"), ("TXT", None, "v=spf1 ~all"))
    assert timeweb_dns.ensure_a(zone, ZONE, HOST, ADDRESS)["changed"] is True
    assert zone.writes() == [("POST", {"type": "A", "value": ADDRESS, "subdomain": "lk"})]
    # The second run must find the record it created, although the zone's own list omits it.
    assert timeweb_dns.ensure_a(zone, ZONE, HOST, ADDRESS) == {
        "changed": False, "host": HOST, "address": ADDRESS}
    assert len(zone.writes()) == 1
    assert [r["type"] for r in timeweb_dns.records(zone, ZONE)] == ["MX", "TXT"]
    assert timeweb_dns.records(zone, HOST) == [
        {"id": 102, "type": "A", "ttl": 600, "name": HOST, "value": ADDRESS}]
    assert timeweb_dns.records(zone, f"absent.{ZONE}") == []


def test_moving_the_bridge_needs_an_explicit_replace():
    zone = FakeZone(("A", "lk", "192.0.2.9"))
    with pytest.raises(ValueError, match="already points to 192.0.2.9"):
        timeweb_dns.ensure_a(zone, ZONE, HOST, ADDRESS)
    assert zone.writes() == []
    result = timeweb_dns.ensure_a(zone, ZONE, HOST, ADDRESS, replace=True)
    assert result["replaced"] == ["192.0.2.9"]
    assert [m for m, _ in zone.writes()] == ["DELETE", "POST"]
    assert [r["value"] for r in timeweb_dns.records(zone, HOST)] == [ADDRESS]


@pytest.mark.parametrize("rows, host, address, message", [
    ([("AAAA", "lk", "2001:db8::1")], HOST, ADDRESS, "has a AAAA record"),
    ([("CNAME", "lk", "elsewhere.example")], HOST, ADDRESS, "has a CNAME record"),
    ([], "lk.other.ru", ADDRESS, "not a host inside"),
    ([("AAAA", None, "2001:db8::1")], ZONE, ADDRESS, "has a AAAA record"),
    ([], HOST, "2001:db8::2", "IPv4 address is required"),
])
def test_records_that_would_split_clients_are_refused(rows, host, address, message):
    zone = FakeZone(*rows)
    with pytest.raises(ValueError, match=message):
        timeweb_dns.ensure_a(zone, ZONE, host, address)
    assert zone.writes() == []


def test_token_prefers_the_dedicated_variable(tmp_path, monkeypatch):
    monkeypatch.setattr(timeweb_dns.paths, "repo_root", lambda: tmp_path)
    for name in ("TIMEWEBCLOUD_AUTH_TOKEN", "TWC_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert timeweb_dns.token() == ""
    (tmp_path / ".env").write_text('export TWC_TOKEN="servers-and-dns"\n')
    assert timeweb_dns.token() == "servers-and-dns"
    monkeypatch.setenv("TIMEWEBCLOUD_AUTH_TOKEN", "dns-only")
    assert timeweb_dns.token() == "dns-only"


LEGO = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
store = Path(args[args.index("--path") + 1])
record = {"argv": args, "token": os.environ.get("TIMEWEBCLOUD_AUTH_TOKEN")}
(store / "calls.jsonl").open("a").write(json.dumps(record) + "\n")
scenario = os.environ.get("LEGO_TEST_SCENARIO", "")
if scenario == "fail":
    print("acme: error: 403 :: urn:ietf:params:acme:error:unauthorized using token " + record["token"], file=sys.stderr)
    sys.exit(1)
out = store / "certificates"
out.mkdir(exist_ok=True)
if not (out / "bridge.crt").exists() or "--renew-force" in args or scenario == "due":
    serial = len(list(store.glob("issued-*")))
    (store / f"issued-{serial}").write_text("")
    (out / "bridge.crt").write_text(f"CERT {serial} for {args[args.index('--domains') + 1]}\n")
    (out / "bridge.key").write_text(f"KEY {serial}\n")
    (out / "bridge.issuer.crt").write_text("ISSUER\n")
'''


@pytest.fixture
def acme(tmp_path, monkeypatch):
    """A pinned lego archive whose binary is the emulation above."""
    executable(tmp_path / "lego", LEGO)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "lego", arcname="lego")
        info = tarfile.TarInfo("README.md")
        tar.addfile(info, io.BytesIO(b""))
    cfg = {"acme": {"lego_release": {"version": "5.5.2",
                                     "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}},
           "bridge_entry": {"domain": HOST}}
    monkeypatch.setattr(certs.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(certs.timeweb_dns, "token", lambda cfg=None: "secret-token-value")
    monkeypatch.delenv("LEGO_TEST_SCENARIO", raising=False)
    fetched = []

    def fetch(url, dest):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(archive, dest)

    return cfg, tmp_path / "state", fetch, fetched


def calls(state, kind="production"):
    return [json.loads(line) for line in (state / "acme" / kind / "calls.jsonl").read_text().splitlines()]


def test_issue_publishes_private_files_and_renewal_is_a_noop_until_due(acme, monkeypatch):
    cfg, state, fetch, fetched = acme
    first = certs.issue(cfg, state, ZONE, fetch=fetch)
    cert, key = certs.published(state, ZONE)
    assert first == {"changed": True, "certificate": str(cert), "private_key": str(key),
                     "staging": False, "names": [f"*.{ZONE}", ZONE]}
    assert cert.read_text() == f"CERT 0 for *.{ZONE}\n" and key.read_text() == "KEY 0\n"
    assert key.stat().st_mode & 0o777 == 0o600 and cert.parent.stat().st_mode & 0o777 == 0o700
    assert fetched == ["https://github.com/go-acme/lego/releases/download/v5.5.2/lego_v5.5.2_linux_amd64.tar.gz"]
    call = calls(state)[0]
    argv = call["argv"]
    assert argv[argv.index("--server") + 1] == "letsencrypt" and argv[argv.index("--dns") + 1] == "timewebcloud"
    assert "--accept-tos" in argv and "--no-random-sleep" in argv and "--email" not in argv
    # Wildcard and bare zone only: the bridge host name itself stays out of the public CT logs.
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--domains"] == [f"*.{ZONE}", ZONE]
    assert HOST not in " ".join(argv)
    # The token travels in the environment, never on the command line.
    assert call["token"] == "secret-token-value" and "secret-token-value" not in " ".join(argv)

    again = certs.issue(cfg, state, ZONE, fetch=fetch)
    assert again["changed"] is False and len(fetched) == 1
    monkeypatch.setenv("LEGO_TEST_SCENARIO", "due")
    renewed = certs.issue(cfg, state, ZONE, fetch=fetch)
    assert renewed["changed"] is True and cert.read_text().startswith("CERT 1")
    monkeypatch.delenv("LEGO_TEST_SCENARIO")
    cfg["acme"]["email"] = "ops@example.ru"
    assert certs.issue(cfg, state, ZONE, force=True, fetch=fetch)["changed"] is True
    assert calls(state)[-1]["argv"][-3:] == ["--email", "ops@example.ru", "--renew-force"]
    # Only the authoritative servers are awaited; resolvers are explicit, not the host's own.
    assert "--dns.propagation.disable-rns" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--dns.resolvers"] == ["1.1.1.1:53", "8.8.8.8:53"]


def test_staging_never_touches_the_published_certificate(acme):
    cfg, state, fetch, _ = acme
    certs.issue(cfg, state, ZONE, fetch=fetch)
    real = certs.published(state, ZONE)[0].read_bytes()
    rehearsal = certs.issue(cfg, state, ZONE, staging=True, fetch=fetch)
    assert rehearsal["staging"] and rehearsal["certificate"].endswith(f"{ZONE}-staging/fullchain.pem")
    argv = calls(state, "staging")[0]["argv"]
    assert argv[argv.index("--server") + 1] == "letsencrypt-staging"
    assert certs.published(state, ZONE)[0].read_bytes() == real


def test_failed_issue_keeps_the_old_certificate_and_hides_the_token(acme, monkeypatch):
    cfg, state, fetch, _ = acme
    certs.issue(cfg, state, ZONE, fetch=fetch)
    saved = certs.published(state, ZONE)[0].read_bytes()
    monkeypatch.setenv("LEGO_TEST_SCENARIO", "fail")
    with pytest.raises(OSError, match="lego failed") as error:
        certs.issue(cfg, state, ZONE, fetch=fetch)
    assert "secret-token-value" not in str(error.value) and "<token>" in str(error.value)
    assert certs.published(state, ZONE)[0].read_bytes() == saved


@pytest.mark.parametrize("damage, message", [
    ("pin", "acme.lego_release"), ("checksum", "SHA256 differs"), ("token", "no DNS API token"),
    ("zone", "not a DNS zone"), ("provider", "is not supported"),
])
def test_unusable_inputs_stop_before_any_acme_request(acme, monkeypatch, damage, message):
    cfg, state, fetch, _ = acme
    zone = ZONE
    if damage == "pin":
        cfg["acme"]["lego_release"]["version"] = "latest"
    elif damage == "checksum":
        cfg["acme"]["lego_release"]["archive_sha256"] = "0" * 64
    elif damage == "token":
        monkeypatch.setattr(certs.timeweb_dns, "token", lambda cfg=None: "")
    elif damage == "zone":
        zone = "Example"
    else:
        cfg["acme"]["dns_provider"] = "cloudflare"
    with pytest.raises(ValueError, match=message):
        certs.issue(cfg, state, zone, fetch=fetch)
    assert not (state / "acme/production/calls.jsonl").exists()
    assert not (state / "certs").exists()


def test_zone_is_derived_from_the_bridge_host_or_given_explicitly():
    assert certs.zone_of({"bridge_entry": {"domain": "lk.example.ru"}}) == "example.ru"
    assert certs.zone_of({"bridge_entry": {"domain": "a.b.example.ru", "zone": "example.ru"}}) == "example.ru"
    with pytest.raises(ValueError, match="bridge_entry.zone"):
        certs.zone_of({})


def test_issued_certificate_is_what_configure_delivers_by_default(acme, tmp_path, monkeypatch):
    """bridge_entry with only a domain uses state/certs/<zone>/; a real certificate is checked."""
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed")
    state = tmp_path / "state"
    cert, key = certs.published(state, ZONE)
    cert.parent.mkdir(parents=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "60", "-subj", "/CN=x", "-addext", f"subjectAltName=DNS:*.{ZONE}",
         "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: state)
    found = cli._bridge_entry({"bridge_entry": {"domain": HOST}}, None)
    assert (found["bridge_https_cert"], found["bridge_https_key"]) == (str(cert), str(key))
    info = certs.describe(cert)
    assert info["names"] == [f"*.{ZONE}"] and info["valid_at_least_days"] == 30
    with pytest.raises(ValueError, match="existing absolute file"):
        cli._bridge_entry({"bridge_entry": {"domain": "lk.unissued.ru"}}, None)


def test_dns_and_cert_commands(acme, monkeypatch):
    cfg, state, fetch, _ = acme
    zone = FakeZone(("MX", None, "mx1.timeweb.ru"))
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: state)
    monkeypatch.setattr(cli.timeweb_dns, "token", lambda cfg=None: "secret-token-value")
    monkeypatch.setattr(cli.timeweb_dns, "api", lambda auth: zone)
    monkeypatch.setattr(certs.world_release, "download", fetch)
    runner = CliRunner()
    missing = runner.invoke(cli.app, ["dns", "ensure"])
    assert missing.exit_code == 1 and "pass --ip" in missing.output
    made = runner.invoke(cli.app, ["dns", "ensure", "--ip", ADDRESS])
    assert made.exit_code == 0 and f"{HOST} -> {ADDRESS}: created" in made.output
    shown = runner.invoke(cli.app, ["dns", "show"])
    assert shown.exit_code == 0 and f"A     {HOST}" in shown.output and "MX" in shown.output
    assert "secret-token-value" not in shown.output
    status = runner.invoke(cli.app, ["cert", "status"])
    assert status.exit_code == 0 and f"production: none for {ZONE}" in status.output
    issued = runner.invoke(cli.app, ["cert", "issue", "--staging"])
    assert issued.exit_code == 0, issued.output
    assert "staging *.example.ru, example.ru: new certificate published" in issued.output
    assert Path(state / f"certs/{ZONE}-staging/privkey.pem").is_file()



def test_challenge_records_listed_with_the_zone_are_named_by_their_subdomain():
    """Timeweb lists the ACME TXT records in the zone's own list, tagged with a subdomain."""
    def request(method, path, body=None):
        return {"dns_records": [
            {"id": 1, "type": "TXT", "ttl": 600, "fqdn": ZONE,
             "data": {"subdomain": "_acme-challenge", "value": "token"}},
            {"id": 2, "type": "TXT", "ttl": 600, "fqdn": ZONE, "data": {"subdomain": None, "value": "v=spf1"}},
        ]}
    assert [(r["name"], r["value"]) for r in timeweb_dns.records(request, ZONE)] == [
        (f"_acme-challenge.{ZONE}", "token"), (ZONE, "v=spf1")]


def test_issue_waits_for_the_slow_name_server_and_survives_a_hung_client(acme, monkeypatch):
    cfg, state, fetch, _ = acme
    seen = {}

    def runner(cmd, env=None, timeout=None, **kwargs):
        seen.update(timeout=timeout, propagation=env["TIMEWEBCLOUD_PROPAGATION_TIMEOUT"],
                    interval=env["TIMEWEBCLOUD_POLLING_INTERVAL"])
        raise subprocess.TimeoutExpired(cmd, timeout)
    with pytest.raises(OSError, match="did not finish within 3900s; nothing was published"):
        certs.issue(cfg, state, ZONE, runner=runner, fetch=fetch)
    assert seen == {"timeout": 3900, "propagation": "1800", "interval": "15"}
    assert not (state / "certs").exists()
    # An operator's explicit setting wins over the default.
    monkeypatch.setenv("TIMEWEBCLOUD_PROPAGATION_TIMEOUT", "120")
    with pytest.raises(OSError, match="within 540s"):
        certs.issue(cfg, state, ZONE, runner=runner, fetch=fetch)


def test_bare_domain_and_www_get_their_own_records_without_touching_mail():
    zone = FakeZone(("MX", None, "mx1.timeweb.ru"), ("TXT", None, "v=spf1 ~all"), ("A", "lk", ADDRESS))
    assert timeweb_dns.ensure_a(zone, ZONE, ZONE, ADDRESS)["changed"] is True
    assert timeweb_dns.ensure_a(zone, ZONE, f"www.{ZONE}", ADDRESS)["changed"] is True
    # The bare domain is created without a subdomain field; www like any other host.
    assert zone.writes() == [("POST", {"type": "A", "value": ADDRESS}),
                             ("POST", {"type": "A", "value": ADDRESS, "subdomain": "www"})]
    for name in (ZONE, f"www.{ZONE}", HOST):
        assert timeweb_dns.ensure_a(zone, ZONE, name, ADDRESS)["changed"] is False
    assert len(zone.writes()) == 2
    assert sorted(r["type"] for r in timeweb_dns.records(zone, ZONE)) == ["A", "MX", "TXT"]


def test_dns_ensure_command_publishes_the_site_names_too(acme, monkeypatch):
    cfg, state, _, _ = acme
    cfg["bridge_entry"]["site_names"] = [ZONE, f"www.{ZONE}"]
    zone = FakeZone()
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: state)
    monkeypatch.setattr(cli.timeweb_dns, "token", lambda cfg=None: "secret-token-value")
    monkeypatch.setattr(cli.timeweb_dns, "api", lambda auth: zone)
    result = CliRunner().invoke(cli.app, ["dns", "ensure", "--ip", ADDRESS])
    assert result.exit_code == 0, result.output
    for name in (HOST, ZONE, f"www.{ZONE}"):
        assert f"{name} -> {ADDRESS}: created" in result.output
    assert [b.get("subdomain") for _, b in zone.writes()] == ["lk", None, "www"]
