"""Rendered routing contracts and optional local tests with the real Xray binary.

XRAY_TEST_BINARY=/path/to/xray pytest -q tests/test_bridge_config.py
The binary's directory must contain geoip.dat. No RU/WORLD servers are contacted.
"""

import base64
import json
import os
import socket
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from http.client import HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from jinja2 import Environment, StrictUndefined

TEMPLATE = Path(__file__).resolve().parents[2] / (
    "ansible/roles/bridge_xray_relay/templates/xray.config.json.j2"
)
PASSWORD = base64.b64encode(b"\x01" * 16).decode()


def render_bridge(mode):
    env = Environment(undefined=StrictUndefined)
    env.filters["to_json"] = json.dumps
    return json.loads(env.from_string(TEMPLATE.read_text()).render(
        bridge_chain_mode=mode,
        bridge_dns_servers=["77.88.8.8"],
        bridge_client_uuid="e11fd630-493a-4181-b514-8e462ce601e5",
        bridge_reality_dest="example.com:443",
        bridge_reality_sni="example.com",
        bridge_private_key=base64.urlsafe_b64encode(b"\x01" * 32).decode().rstrip("="),
        bridge_short_id="aabbccdd",
        bridge_ss_method="2022-blake3-aes-128-gcm",
        bridge_ss_password=PASSWORD,
        ss_method="2022-blake3-aes-128-gcm",
        ss_password=PASSWORD,
        ss_port=8388,
        exit_ip="127.0.0.1",
        chain_port=8443,
        chain_uuid="6aac169a-291c-49d8-85fc-cf3c4e22e7f2",
        exit_reality_sni="example.org",
        exit_public_key=base64.urlsafe_b64encode(b"\x02" * 32).decode().rstrip("="),
        exit_short_id="11223344",
    ))


@pytest.mark.parametrize("mode", ["ss", "xhttp"])
def test_only_diagnostic_loopback_can_reach_ru_freedom(mode):
    cfg = render_bridge(mode)
    by_tag = {o["tag"]: o for o in cfg["outbounds"]}
    assert cfg["outbounds"][0]["protocol"] == "blackhole"
    assert not cfg["routing"].get("balancers")
    direct_rules = [r for r in cfg["routing"]["rules"] if r["outboundTag"] == "direct"]
    assert len(direct_rules) == 1
    assert direct_rules[0]["inboundTag"] == ["probe-direct-in"]
    assert by_tag["direct"]["protocol"] == "freedom"
    inbounds = {i["tag"]: i for i in cfg["inbounds"]}
    assert inbounds["probe-direct-in"]["listen"] == "127.0.0.1"
    assert inbounds["probe-in"]["listen"] == "127.0.0.1"
    routed = [r for r in cfg["routing"]["rules"] if r["outboundTag"] == "chain-to-exit"]
    assert len(routed) == 1
    assert set(routed[0]["inboundTag"]) == {"vless-reality-in", "ss2022-in", "probe-in"}
    private = next(r for r in cfg["routing"]["rules"] if "geoip:private" in r.get("ip", []))
    assert private["outboundTag"] == "block"
    assert set(private["inboundTag"]) == set(routed[0]["inboundTag"])
    assert cfg["routing"]["rules"].index(private) < cfg["routing"]["rules"].index(routed[0])
    assert cfg["routing"]["domainStrategy"] == "AsIs"
    chain = by_tag["chain-to-exit"]
    if mode == "xhttp":
        assert chain["streamSettings"]["security"] == "reality"
        assert chain["settings"]["vnext"][0]["users"][0]["flow"] == ""
        assert chain["settings"]["vnext"][0]["users"][0]["id"] != (
            inbounds["vless-reality-in"]["settings"]["clients"][0]["id"]
        )
    else:
        assert chain["protocol"] == "shadowsocks"


@pytest.fixture
def xray_binary():
    raw = os.environ.get("XRAY_TEST_BINARY")
    if not raw:
        pytest.skip("set XRAY_TEST_BINARY to run local Xray integration tests")
    binary = Path(raw).resolve()
    assert binary.is_file(), f"Xray binary not found: {binary}"
    return binary


def xray_env(binary):
    return {**os.environ, "XRAY_LOCATION_ASSET": str(binary.parent)}


@pytest.mark.parametrize("mode", ["ss", "xhttp"])
def test_real_xray_accepts_rendered_config(xray_binary, tmp_path, mode):
    cfg = render_bridge(mode)
    cfg["log"] = {"loglevel": "warning"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    result = subprocess.run(
        [str(xray_binary), "run", "-test", "-config", str(path)],
        env=xray_env(xray_binary), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_xray(binary, cfg, path, port):
    path.write_text(json.dumps(cfg))
    with path.with_suffix(".log").open("w+") as log:
        proc = subprocess.Popen(
            [str(binary), "run", "-config", str(path)],
            env=xray_env(binary), stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("Xray did not open its test listener")
            yield proc
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def http_via_socks(port, target, target_port, *, domain=True, host="localhost"):
    def read_exact(sock, n):
        result = b""
        while len(result) < n:
            chunk = sock.recv(n - len(result))
            if not chunk:
                raise OSError("SOCKS connection closed")
            result += chunk
        return result

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"\x05\x01\x00")
            assert read_exact(sock, 2) == b"\x05\x00"
            addr = (b"\x03" + bytes([len(target)]) + target.encode()) if domain else (
                b"\x01" + socket.inet_aton(target)
            )
            sock.sendall(b"\x05\x01\x00" + addr + struct.pack("!H", target_port))
            reply = read_exact(sock, 4)
            if reply[1] != 0:
                return b""
            size = {1: 4, 4: 16}.get(reply[3])
            if size is None:
                size = read_exact(sock, 1)[0]
            read_exact(sock, size + 2)
            sock.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            response = HTTPResponse(sock)
            response.begin()
            return response.read() if response.status == 200 else b""
    except (OSError, HTTPException):
        return b""


def test_exit_failure_and_private_destination_never_fall_back_to_ru(xray_binary, tmp_path):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"world-test-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    allocated = {server.server_address[1]}

    def next_port():
        while (port := free_port()) in allocated:
            pass
        allocated.add(port)
        return port

    world_port = next_port()
    world = {
        "log": {"loglevel": "info"},
        "dns": {"hosts": {"target.test": "127.0.0.1"}},
        "inbounds": [{
            "listen": "127.0.0.1", "port": world_port, "protocol": "shadowsocks",
            "settings": {"method": "2022-blake3-aes-128-gcm", "password": PASSWORD},
        }],
        "outbounds": [{"protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}}],
    }
    ru = render_bridge("ss")
    ru["log"] = {"loglevel": "info"}
    ru["dns"] = {"hosts": {"target.test": "127.0.0.1"}}
    next(o for o in ru["outbounds"] if o["tag"] == "direct")["settings"] = {
        "domainStrategy": "UseIPv4"
    }
    ports = {}
    for inbound in ru["inbounds"]:
        inbound.update(listen="127.0.0.1", port=next_port())
        ports[inbound["tag"]] = inbound["port"]
    unknown_port = next_port()
    ru["inbounds"].append({
        "tag": "unexpected-in", "listen": "127.0.0.1", "port": unknown_port,
        "protocol": "socks", "settings": {"auth": "noauth"},
    })
    next(o for o in ru["outbounds"] if o["tag"] == "chain-to-exit")["settings"]["servers"][0][
        "port"
    ] = world_port
    chain_port, direct_port = ports["probe-in"], ports["probe-direct-in"]
    target_port = server.server_address[1]
    try:
        with running_xray(xray_binary, world, tmp_path / "world.json", world_port) as world_proc:
            with running_xray(xray_binary, ru, tmp_path / "ru.json", chain_port):
                assert b"world-test-response" in http_via_socks(chain_port, "target.test", target_port)
                assert len(hits) == 1
                # Previously geoip:private -> direct bypassed WORLD for this request.
                assert not http_via_socks(chain_port, "127.0.0.1", target_port, domain=False)
                assert not http_via_socks(unknown_port, "127.0.0.1", target_port, domain=False)
                assert len(hits) == 1
                world_proc.terminate()
                world_proc.wait(timeout=5)
                assert not http_via_socks(chain_port, "target.test", target_port)
                assert len(hits) == 1
                # Prove the destination is still reachable from RU, only via diagnostic ingress.
                assert b"world-test-response" in http_via_socks(direct_port, "target.test", target_port)
                assert len(hits) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
