"""HTTPS front of the RU XHTTP entry.

Part 1 renders the role's nginx.conf and runs the real nginx (NGINX_TEST_BINARY) with the real
Xray: a client speaking VLESS + XHTTP + TLS reaches the target only through nginx, large and
slow transfers are not held back by buffering, and an unknown name learns nothing.
Part 2 runs the Ansible transaction with emulated services: order of the port hand-over,
certificate rotation without an Xray restart, and rollback.
"""

import hashlib
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from test_bridge_apply import apply_env as apply_env_fixture
from test_bridge_apply import executable, run_apply
from test_bridge_config import PASSWORD, free_port, running_xray
from test_bridge_config import xray_binary as xray_binary_fixture
from test_bridge_support import support
from test_bridge_xhttp_entry import XHTTP_PATH, XHTTP_UUID, render

apply_env = apply_env_fixture
xray_binary = xray_binary_fixture
ROLE = Path(__file__).resolve().parents[2] / "ansible/roles/bridge_xray_relay"
DOMAIN = "bridge.test"


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def pki(tmp_path):
    """A private CA and a certificate for DOMAIN, standing in for the public CA."""
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to issue test certificates")
    directory = tmp_path / "pki"
    directory.mkdir()
    curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    openssl("req", "-x509", *curve, "-days", "30", "-subj", "/CN=Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-keyout", "ca.key", "-out", "ca.crt", cwd=directory)
    for name in (DOMAIN, "other.test"):
        openssl("req", *curve, "-subj", f"/CN={name}", "-keyout", f"{name}.key",
                "-out", f"{name}.csr", cwd=directory)
        (directory / f"{name}.ext").write_text(f"subjectAltName=DNS:{name}\n")
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                "-CAcreateserial", "-days", "30", "-extfile", f"{name}.ext",
                "-out", f"{name}.crt", cwd=directory)
    return directory


def nginx_conf(root, pki, https_port, http_port, xhttp_port, **extra):
    env = Environment(undefined=StrictUndefined)
    values = dict(
        bridge_https_user="root", bridge_https_pid=str(root / "nginx.pid"),
        bridge_https_log_dir=str(root / "log"), bridge_https_mime_types=str(root / "mime.types"),
        bridge_https_temp_dir=str(root / "tmp"), bridge_https_port=https_port,
        bridge_https_http_port=http_port, bridge_https_dir=str(root / "tls"),
        bridge_https_domain=DOMAIN, bridge_https_webroot=str(root / "www"),
        bridge_xhttp_path=XHTTP_PATH, bridge_xhttp_port=xhttp_port,
        bridge_https_body_size="2m", bridge_https_timeout=3600, bridge_xhttp_mode="auto",
        bridge_https_site_names=[],
    )
    for name in ("log", "tmp", "tls", "www"):
        (root / name).mkdir(exist_ok=True)
    (root / "mime.types").write_text("types { text/html html; }\n")
    (root / "www/index.html").write_text("<h1>ordinary page</h1>")
    shutil.copy(pki / f"{DOMAIN}.crt", root / "tls/fullchain.pem")
    shutil.copy(pki / f"{DOMAIN}.key", root / "tls/privkey.pem")
    shutil.copy(pki / "other.test.crt", root / "tls/default.crt")
    shutil.copy(pki / "other.test.key", root / "tls/default.key")
    template = (ROLE / "templates/nginx.conf.j2").read_text()
    (root / "nginx.conf").write_text(env.from_string(template).render(**{**values, **extra}))
    return root / "nginx.conf"


@pytest.fixture
def nginx_binary():
    raw = os.environ.get("NGINX_TEST_BINARY")
    if not raw:
        pytest.skip("set NGINX_TEST_BINARY to run the reverse-proxy integration tests")
    assert Path(raw).is_file(), raw
    return Path(raw).resolve()


def test_nginx_accepts_the_config_and_rejects_a_foreign_key(nginx_binary, pki, tmp_path):
    conf = nginx_conf(tmp_path, pki, free_port(), free_port(), free_port())
    ok = subprocess.run([str(nginx_binary), "-t", "-c", str(conf)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    site = nginx_conf(tmp_path, pki, free_port(), free_port(), free_port(),
                      bridge_https_site_names=["example.test", "www.example.test"])
    assert subprocess.run([str(nginx_binary), "-t", "-c", str(site)], capture_output=True).returncode == 0
    shutil.copy(pki / "other.test.key", tmp_path / "tls/privkey.pem")
    bad = subprocess.run([str(nginx_binary), "-t", "-c", str(conf)], capture_output=True, text=True)
    assert bad.returncode != 0 and "key values mismatch" in bad.stderr


def test_config_keeps_the_tunnel_unbuffered_and_only_on_the_secret_path(pki, tmp_path):
    def block(text):
        return text[text.index(f"location ^~ {XHTTP_PATH}/"):text.index("location / {")]

    text = nginx_conf(tmp_path, pki, 443, 80, 10443).read_text()
    for directive in ("grpc_pass grpc://127.0.0.1:10443;", "client_max_body_size 0;",
                      "grpc_read_timeout 3600s;", "client_body_timeout 3600s;"):
        assert directive in block(text), directive
    assert "proxy_pass http" not in text and text.count("grpc_pass grpc://") == 1
    packet = nginx_conf(tmp_path, pki, 443, 80, 10443, bridge_xhttp_mode="packet-up").read_text()
    for directive in ("proxy_pass http://127.0.0.1:10443;", "proxy_http_version 1.1;",
                      "proxy_buffering off;", "proxy_request_buffering off;", "proxy_cache off;",
                      "client_max_body_size 2m;", "proxy_read_timeout 3600s;"):
        assert directive in block(packet), directive
    assert "grpc_pass" not in packet and packet.count("proxy_pass http://") == 1
    assert "access_log off;" in text
    assert text.count("return 444;") == 2 and "return 301 https://$host$request_uri;" in text
    assert "[::]" not in text  # no AAAA record is published for the bridge
    # The plain site of the bare domain: the page only, never a way into the tunnel.
    sited = nginx_conf(tmp_path, pki, 443, 80, 10443,
                       bridge_https_site_names=["example.test", "www.example.test"]).read_text()
    plain = sited[sited.index("server_name example.test www.example.test;"):]
    plain = plain[:plain.index("\n    }\n") + 1]
    assert "pass" not in plain and XHTTP_PATH not in plain and "try_files" in plain
    assert sited.count("grpc_pass grpc://") == 1
    assert f"server_name {DOMAIN} example.test www.example.test;" in sited  # http -> https redirect
    assert "example.test" not in text
    assert "listen 80" not in nginx_conf(tmp_path, pki, 443, 0, 10443).read_text()


def socks(port, host, target_port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00"
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + struct.pack("!H", target_port))
    reply = sock.recv(10)
    if len(reply) < 2 or reply[1] != 0:
        sock.close()
        return None
    sock.settimeout(6)  # idle limit while reading; longer than the slow response's pause
    return sock


def read_all(sock):
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except OSError:
        pass
    return data


PACKET_UP_DAMAGE = (
    "Xray 25.10.15 damages some large packet-up uploads (a duplicated block near the first "
    "8 KiB), with or without nginx; stream-up is intact. Kept as a tripwire for a newer fixture."
)


@pytest.mark.parametrize("mode, client_mode", [
    ("stream-up", "stream-up"),
    pytest.param("packet-up", "packet-up", marks=pytest.mark.xfail(strict=False, reason=PACKET_UP_DAMAGE)),
    # The default entry accepts whatever the client application chooses, through one relay.
    ("auto", "stream-up"),
    ("auto", "stream-one"),
    pytest.param("auto", "packet-up", marks=pytest.mark.xfail(strict=False, reason=PACKET_UP_DAMAGE)),
    pytest.param("auto", "auto", marks=pytest.mark.xfail(strict=False, reason=PACKET_UP_DAMAGE)),
])
def test_client_reaches_world_only_through_nginx_without_buffering(
    xray_binary, nginx_binary, pki, tmp_path, mode, client_mode,
):
    big = os.urandom(6 * 1024 * 1024)
    uploads = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"first")
                self.wfile.flush()
                time.sleep(3)
                self.wfile.write(b"second")
                return
            body = big if self.path == "/big" else b"world-test-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            uploads.append(hashlib.sha256(body).hexdigest())
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    used = {target.server_address[1]}

    def port():
        while (number := free_port()) in used:
            pass
        used.add(number)
        return number

    world_port, https_port, http_port, client_port = port(), port(), port(), port()
    world_cfg = {
        "log": {"loglevel": "warning"}, "dns": {"hosts": {"target.test": "127.0.0.1"}},
        "inbounds": [{"listen": "127.0.0.1", "port": world_port, "protocol": "shadowsocks",
                      "settings": {"method": "2022-blake3-aes-128-gcm", "password": PASSWORD}}],
        "outbounds": [{"protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}}],
    }
    ru = render("ss", bridge_xhttp_mode=mode)
    ru["log"] = {"loglevel": "warning"}
    ports = {}
    for inbound in ru["inbounds"]:
        inbound.update(listen="127.0.0.1", port=port())
        ports[inbound["tag"]] = inbound["port"]
    next(o for o in ru["outbounds"] if o["tag"] == "chain-to-exit")["settings"]["servers"][0]["port"] = world_port
    client = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": client_port, "protocol": "socks",
                      "settings": {"auth": "noauth"}}],
        "outbounds": [{"protocol": "vless", "settings": {"vnext": [{
            "address": "127.0.0.1", "port": https_port,
            "users": [{"id": XHTTP_UUID, "encryption": "none", "flow": ""}]}]},
            "streamSettings": {
                "network": "xhttp", "security": "tls",
                "tlsSettings": {"serverName": DOMAIN, "alpn": ["h2"], "certificates": [
                    {"usage": "verify", "certificateFile": str(pki / "ca.crt")}]},
                "xhttpSettings": {"mode": client_mode, "path": XHTTP_PATH}}}],
    }
    conf = nginx_conf(tmp_path, pki, https_port, http_port, ports["vless-xhttp-in"],
                      bridge_xhttp_mode=mode)
    nginx = subprocess.Popen([str(nginx_binary), "-c", str(conf), "-g", "daemon off;"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    target_port = target.server_address[1]
    try:
        with running_xray(xray_binary, world_cfg, tmp_path / "world.json", world_port), \
                running_xray(xray_binary, ru, tmp_path / "ru.json", ports["vless-xhttp-in"]), \
                running_xray(xray_binary, client, tmp_path / "client.json", client_port):
            deadline = time.monotonic() + 5
            while True:
                try:
                    socket.create_connection(("127.0.0.1", https_port), timeout=0.2).close()
                    break
                except OSError:
                    assert nginx.poll() is None and time.monotonic() < deadline, "nginx did not start"
                    time.sleep(0.05)

            # What a visitor and what an address scanner see.
            assert support.check_https({"name": DOMAIN, "port": https_port,
                                        "ca_file": str(pki / "ca.crt")}) == {"https": "ok"}
            with socket.create_connection(("127.0.0.1", http_port), timeout=5) as plain:
                plain.sendall(f"GET /x HTTP/1.0\r\nHost: {DOMAIN}\r\n\r\n".encode())
                redirect = read_all(plain)
            assert redirect.startswith(b"HTTP/1.1 301") and f"https://{DOMAIN}/x".encode() in redirect

            sock = socks(client_port, "target.test", target_port)
            sock.sendall(b"GET / HTTP/1.0\r\nHost: target.test\r\n\r\n")
            assert b"world-test-response" in read_all(sock)

            # A download much larger than any proxy buffer arrives intact.
            sock = socks(client_port, "target.test", target_port)
            sock.sendall(b"GET /big HTTP/1.0\r\nHost: target.test\r\n\r\n")
            assert read_all(sock).split(b"\r\n\r\n", 1)[1] == big

            # An upload far larger than a packet-up request or any proxy buffer, bit for bit.
            sock = socks(client_port, "target.test", target_port)
            sock.sendall(f"POST /up HTTP/1.0\r\nHost: target.test\r\nContent-Length: {len(big)}\r\n\r\n".encode())
            sock.sendall(big)
            assert read_all(sock).endswith(b"ok")
            assert uploads == [hashlib.sha256(big).hexdigest()]

            # The first bytes of a slow response arrive while the response is still open:
            # nginx passes data on as it comes instead of collecting the whole reply.
            sock = socks(client_port, "target.test", target_port)
            started = time.monotonic()
            sock.sendall(b"GET /slow HTTP/1.0\r\nHost: target.test\r\n\r\n")
            received = b""
            while b"first" not in received:
                received += sock.recv(4096)
            assert time.monotonic() - started < 2 and b"second" not in received
            assert b"second" in received + read_all(sock)
    finally:
        nginx.terminate()
        nginx.wait(timeout=5)
        target.shutdown()
        target.server_close()


# ── Ansible transaction with emulated services ────────────────────────────────────

SYSTEMCTL = r'''
import json, os, sys
from pathlib import Path
root = Path(__file__).parent
state_path = root / "units.json"
state = json.loads(state_path.read_text())
action, unit = sys.argv[1], sys.argv[2]
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps([action, unit]) + "\n")
service = state.setdefault(unit, {"active": "inactive", "enabled": "disabled"})
if action in ("is-active", "is-enabled"):
    value = service["active" if action == "is-active" else "enabled"]
    print(value)
    sys.exit(0 if value in ("active", "enabled") else 3)
if action in ("start", "restart", "reload-or-restart"):
    service["active"] = "active"
elif action == "stop":
    service["active"] = "inactive"
elif action in ("enable", "disable"):
    service["enabled"] = action + "d"
else:
    sys.exit(2)
state_path.write_text(json.dumps(state))
'''

NGINX = r'''
import os, sys
assert sys.argv[1] == "-t" and sys.argv[2] == "-c"
text = open(sys.argv[3]).read()
assert "proxy-pipeline managed" in text
sys.exit(1 if os.environ.get("BRIDGE_TEST_SCENARIO") == "nginx_invalid" else 0)
'''


class TlsSite:
    """Stands in for the running nginx: the bridge name gets its certificate, others another."""

    def __init__(self, pki):
        self.known = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.known.load_cert_chain(pki / f"{DOMAIN}.crt", pki / f"{DOMAIN}.key")
        other = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        other.load_cert_chain(pki / "other.test.crt", pki / "other.test.key")

        def pick(sock, name, _context):
            if name != DOMAIN:
                sock.context = other
        self.known.sni_callback = pick
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            try:
                with self.known.wrap_socket(conn, server_side=True) as tls:
                    if tls.recv(4096):
                        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except (OSError, ssl.SSLError):
                pass

    def close(self):
        # shutdown wakes the thread blocked in accept(); close alone would let it take one more.
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()


@pytest.fixture
def https_env(apply_env, pki):
    root, _, reality_port = apply_env
    executable(root / "systemctl", SYSTEMCTL)
    executable(root / "nginx", NGINX)
    (root / "units.json").write_text(json.dumps({
        "xray.service": {"active": "active", "enabled": "enabled"}}))
    site = TlsSite(pki)
    xhttp = socket.socket()
    xhttp.bind(("127.0.0.1", 0))
    xhttp.listen(8)
    play = yaml.safe_load((root / "play.yml").read_text())
    play[0]["vars"].update(
        bridge_xhttp_enabled=True, bridge_xhttp_uuid=XHTTP_UUID, bridge_xhttp_path=XHTTP_PATH,
        bridge_xhttp_port=xhttp.getsockname()[1],
        bridge_https_domain=DOMAIN, bridge_https_cert=str(pki / f"{DOMAIN}.crt"),
        bridge_https_key=str(pki / f"{DOMAIN}.key"), bridge_https_port=site.port,
        bridge_https_http_port=0, bridge_https_install_package=False,
        bridge_https_binary=str(root / "nginx"), bridge_https_conf=str(root / "nginx/nginx.conf"),
        bridge_https_mime_types="/etc/nginx/mime.types", bridge_https_dir=str(root / "nginx/tls"),
        bridge_https_webroot=str(root / "www"), bridge_https_temp_dir=str(root / "nginx/tmp"),
        bridge_https_log_dir=str(root / "nginx/log"), bridge_https_pid=str(root / "nginx.pid"),
        bridge_https_user="root", bridge_https_ca_file=str(pki / "ca.crt"),
        bridge_support_path=str(ROLE / "files/bridge_support.py"),
    )
    (root / "nginx").with_name("nginx-dir").mkdir()
    for key in ("bridge_https_conf", "bridge_https_dir", "bridge_https_temp_dir", "bridge_https_log_dir"):
        play[0]["vars"][key] = play[0]["vars"][key].replace("/nginx/", "/nginx-dir/")

    def write(**changes):
        play[0]["vars"].update(changes)
        (root / "play.yml").write_text(yaml.safe_dump(play))
    write()
    try:
        yield root, write, site, reality_port
    finally:
        site.close()
        xhttp.close()


def unit_commands(root, unit):
    lines = (root / "commands.jsonl").read_text().splitlines()
    return [json.loads(line)[0] for line in lines if json.loads(line)[1] == unit]


def changes(root, unit):
    return [c for c in unit_commands(root, unit) if c not in ("is-active", "is-enabled")]


def test_https_entry_takes_the_port_after_xray_and_repeat_changes_nothing(https_env, pki):
    root, write, site, reality_port = https_env
    first = run_apply(root)
    assert first.returncode == 0, first.stdout + first.stderr
    conf = (root / "nginx-dir/nginx.conf").read_text()
    assert f"server_name {DOMAIN};" in conf and f"listen {site.port} ssl http2;" in conf
    key = root / "nginx-dir/tls/privkey.pem"
    assert key.read_bytes() == (pki / f"{DOMAIN}.key").read_bytes()
    assert key.stat().st_mode & 0o777 == 0o600 and (root / "nginx-dir/tls/default.crt").is_file()
    assert "Сайт обновляется" in (root / "www/index.html").read_text()
    runtime = json.loads((root / "bridge_runtime.json").read_text())
    assert runtime["bridge_domain"] == DOMAIN and runtime["bridge_https_port"] == site.port
    assert runtime["bridge_reality_port"] == reality_port != site.port
    # Xray moves first, nginx starts afterwards.
    order = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    assert order.index(["restart", "xray.service"]) < order.index(["reload-or-restart", "nginx.service"])
    assert changes(root, "nginx.service") == ["reload-or-restart", "enable"]
    assert (pki / f"{DOMAIN}.key").read_text().split("\n")[1] not in first.stdout + first.stderr

    second = run_apply(root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert changes(root, "nginx.service") == ["reload-or-restart", "enable", "enable"]
    assert unit_commands(root, "xray.service").count("restart") == 1


def test_renewed_certificate_reloads_nginx_without_touching_xray(https_env, pki, tmp_path):
    root, write, site, _ = https_env
    assert run_apply(root).returncode == 0
    renewed = tmp_path / "renewed.crt"
    renewed.write_bytes((pki / f"{DOMAIN}.crt").read_bytes() + (pki / "ca.crt").read_bytes())
    write(bridge_https_cert=str(renewed))
    result = run_apply(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "nginx-dir/tls/fullchain.pem").read_bytes() == renewed.read_bytes()
    assert changes(root, "nginx.service").count("reload-or-restart") == 2
    assert unit_commands(root, "xray.service").count("restart") == 1


def test_invalid_nginx_config_fails_before_xray_gives_up_the_port(https_env):
    root, write, site, _ = https_env
    saved = (root / "config.json").read_bytes()
    result = run_apply(root, "nginx_invalid")
    assert result.returncode != 0
    assert (root / "config.json").read_bytes() == saved
    assert changes(root, "xray.service") == [] and changes(root, "nginx.service") == []
    assert not (root / "nginx-dir/nginx.conf").exists()


def test_failed_https_check_returns_the_port_to_the_previous_xray_config(https_env):
    root, write, site, _ = https_env
    saved = {name: (root / name).read_bytes() for name in
             ("config.json", "bridge_runtime.json", "bridge_identity.json")}
    site.close()  # nginx "starts" but nothing valid answers on the HTTPS port
    write(bridge_start_timeout=2)
    result = run_apply(root)
    assert result.returncode != 0 and "Bridge apply failed" in result.stdout
    assert {name: (root / name).read_bytes() for name in saved} == saved
    order = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    # nginx is stopped before Xray is restarted with the config that owns the public port.
    stop = order.index(["stop", "nginx.service"])
    assert stop < max(i for i, c in enumerate(order) if c == ["restart", "xray.service"])
    units = json.loads((root / "units.json").read_text())
    assert units["nginx.service"]["active"] == "inactive" and units["xray.service"]["active"] == "active"


def test_disabling_the_entry_stops_nginx_before_xray_returns(https_env):
    root, write, site, reality_port = https_env
    assert run_apply(root).returncode == 0
    (root / "commands.jsonl").write_text("")
    write(bridge_https_domain="", bridge_xhttp_enabled=False)
    result = run_apply(root)
    assert result.returncode == 0, result.stdout + result.stderr
    order = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    assert order.index(["stop", "nginx.service"]) < order.index(["restart", "xray.service"])
    assert ["disable", "nginx.service"] in order
    config = json.loads((root / "config.json").read_text())
    assert not any(i["tag"] == "vless-xhttp-in" for i in config["inbounds"])
    assert "bridge_domain" not in json.loads((root / "bridge_runtime.json").read_text())


@pytest.mark.parametrize("change", [
    {"bridge_https_domain": "Bridge.Test"}, {"bridge_https_domain": "localhost"},
    {"bridge_https_cert": ""}, {"bridge_xhttp_enabled": False}, {"same_port": True},
])
def test_inconsistent_https_settings_stop_before_any_change(https_env, change):
    root, write, site, reality_port = https_env
    if change.pop("same_port", False):
        change = {"bridge_https_port": reality_port}
    write(**change)
    saved = (root / "config.json").read_bytes()
    result = run_apply(root)
    assert result.returncode != 0 and "HTTPS entry needs" in result.stdout
    assert (root / "config.json").read_bytes() == saved
    assert changes(root, "xray.service") == [] and not (root / "nginx-dir/nginx.conf").exists()


# ── pipelinectl: bridge_entry is checked before inventory or Ansible ───────────────

from typer.testing import CliRunner  # noqa: E402

from pipelinectl import cli  # noqa: E402
from test_configure import configure_env as configure_env_fixture  # noqa: E402

configure_env = configure_env_fixture


def entry(pki, **changes):
    return {"domain": DOMAIN, "certificate": str(pki / f"{DOMAIN}.crt"),
            "private_key": str(pki / f"{DOMAIN}.key"), **changes}


def test_bridge_entry_reaches_the_bridge_plays_and_moves_the_legacy_inbound(configure_env, pki):
    cfg, _, _, _, playbook = configure_env
    cfg["bridge_entry"] = entry(pki)
    for kwargs in ({}, {"bridge_only": True}):
        cli._do_configure(**kwargs)
        extra = playbook.call_args.args[3]
        assert extra["bridge_xhttp_enabled"] is True and extra["bridge_xhttp_mode"] == "auto"
        assert extra["bridge_https_domain"] == DOMAIN and extra["bridge_reality_port"] == 8443
        assert extra["bridge_https_key"] == str(pki / f"{DOMAIN}.key")
    cfg["bridge_entry"] = entry(pki, legacy_reality_port=9443, xhttp_mode="packet-up")
    cli._do_configure(bridge_only=True)
    extra = playbook.call_args.args[3]
    assert (extra["bridge_reality_port"], extra["bridge_xhttp_mode"]) == (9443, "packet-up")
    cli._do_configure(exit_firewall_only=True)
    assert "bridge_https_domain" not in playbook.call_args.args[3]
    cfg["bridge_entry"] = {"domain": ""}
    cli._do_configure(bridge_only=True)
    extra = playbook.call_args.args[3]
    assert "bridge_xhttp_enabled" not in extra and extra["bridge_reality_port"] == 443


@pytest.mark.parametrize("change, message", [
    ({"domain": "Bridge.Test"}, "lower-case host name"),
    ({"domain": "elsewhere.test"}, "is not valid for elsewhere.test"),
    ({"private_key": "OTHER"}, "is not the private key"),
    ({"certificate": "/nonexistent/fullchain.pem"}, "existing absolute file"),
    ({"private_key": "relative.key"}, "existing absolute file"),
    ({"legacy_reality_port": 443}, "nginx owns ports 80/443"),
    ({"xhttp_mode": "stream-one"}, "auto, stream-up or packet-up"),
    ({"site_dir": "/nonexistent/site"}, "existing directory"),
])
def test_unusable_bridge_entry_stops_before_inventory(configure_env, pki, change, message):
    cfg, _, _, inventory, playbook = configure_env
    if change.get("private_key") == "OTHER":
        change = {"private_key": str(pki / "other.test.key")}
    cfg["bridge_entry"] = entry(pki, **change)
    for args in (["configure"], ["configure", "--bridge-only"], ["configure", "--preflight"]):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code != 0 and message in result.output, result.output
    inventory.assert_not_called()
    playbook.assert_not_called()


def test_wildcard_certificate_and_expiry_window(configure_env, pki):
    cfg, _, _, inventory, playbook = configure_env
    curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    for name, days, san in (("wild", "30", "DNS:*.example.ru,DNS:example.ru"), ("soon", "3", "DNS:lk.example.ru")):
        openssl("req", "-x509", *curve, "-days", days, "-subj", "/CN=x", "-addext",
                f"subjectAltName={san}", "-keyout", f"{name}.key", "-out", f"{name}.crt", cwd=pki)
    cfg["bridge_entry"] = {"domain": "lk.example.ru", "certificate": str(pki / "wild.crt"),
                           "private_key": str(pki / "wild.key")}
    assert CliRunner().invoke(cli.app, ["configure", "--preflight"]).exit_code == 0
    cfg["bridge_entry"].update(certificate=str(pki / "soon.crt"), private_key=str(pki / "soon.key"))
    result = CliRunner().invoke(cli.app, ["configure", "--preflight"])
    assert result.exit_code != 0 and "expires within 7 days" in result.output
    cfg["bridge_entry"].update(domain="deep.lk.example.ru", certificate=str(pki / "wild.crt"),
                               private_key=str(pki / "wild.key"))
    assert "is not valid for deep.lk.example.ru" in CliRunner().invoke(
        cli.app, ["configure", "--preflight"]).output


def test_site_names_must_be_covered_by_the_certificate(configure_env, pki):
    cfg, _, _, inventory, playbook = configure_env
    curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    openssl("req", "-x509", *curve, "-days", "30", "-subj", "/CN=x", "-addext",
            "subjectAltName=DNS:*.example.ru,DNS:example.ru", "-keyout", "zone.key", "-out", "zone.crt", cwd=pki)
    base = {"domain": "lk.example.ru", "certificate": str(pki / "zone.crt"), "private_key": str(pki / "zone.key")}
    cfg["bridge_entry"] = {**base, "site_names": ["example.ru", "www.example.ru"]}
    cli._do_configure(bridge_only=True)
    assert playbook.call_args.args[3]["bridge_https_site_names"] == ["example.ru", "www.example.ru"]
    cfg["bridge_entry"] = base
    cli._do_configure(bridge_only=True)
    assert playbook.call_args.args[3]["bridge_https_site_names"] == []
    playbook.reset_mock()
    for names, message in ((["other.ru"], "is not valid for other.ru"), (["lk.example.ru"], "another lower-case"),
                           (["Example.ru"], "another lower-case"), ("example.ru", "must be a list")):
        cfg["bridge_entry"] = {**base, "site_names": names}
        result = CliRunner().invoke(cli.app, ["configure", "--bridge-only"])
        assert result.exit_code != 0 and message in result.output, result.output
    playbook.assert_not_called()
