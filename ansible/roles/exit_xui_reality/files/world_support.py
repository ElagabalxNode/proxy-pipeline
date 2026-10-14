"""Controller-side managed-inbound transaction, via an SSH tunnel to WORLD.

The shared REALITY key validator lives in bridge_xray_relay/files (PYTHONPATH set by
Ansible). No panel cookies/passwords are persisted. The recovery journal is mode 0600.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import json
import os
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.cookiejar import CookieJar
from pathlib import Path
from uuid import UUID, uuid4

from bridge_support import recover_keys, validate_identity

REMARKS = ("VLESS-Reality-Vision", "SS2022-Fallback", "Chain-Bridge-Entry")
FIELDS = ("remark", "enable", "listen", "port", "protocol", "settings", "streamSettings", "sniffing")
JSON_FIELDS = ("settings", "streamSettings", "sniffing")
# Chain REALITY parameters and the direct ones they default to: a WORLD configured before the
# split serves both inbounds with one key/shortId/SNI, and stays that way until asked to migrate.
CHAIN_FIELDS = {"chain_private_key": "private_key", "chain_short_id": "short_id",
                "chain_reality_sni": "reality_sni", "chain_reality_dest": "reality_dest"}


class WorldError(Exception):
    """Only fixed, secret-free diagnostics may be put in this exception."""


def object_json(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise WorldError("expected a JSON object; refusing to change WORLD")
    return value


def managed(items):
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise WorldError("invalid inbound list")
    result = {}
    for item in items:
        remark = item.get("remark")
        if remark not in REMARKS:
            continue
        if remark in result:
            raise WorldError("duplicate managed remark; resolve ownership before configuring")
        if type(item.get("id")) is not int or item["id"] <= 0 or item.get("nodeId"):
            raise WorldError("unsupported managed inbound ID/node")
        result[remark] = item
    return result


def normalized(item):
    data = {field: copy.deepcopy(item[field]) for field in FIELDS}
    for field in JSON_FIELDS:
        data[field] = object_json(data[field])
    for settings in (data["streamSettings"],):
        if settings.get("network") == "raw":
            settings["network"] = "tcp"
        if "rawSettings" in settings:
            settings["tcpSettings"] = settings.pop("rawSettings")
    return data


def contains(actual, expected):
    """Allow API-added defaults, but never a missing/changed requested field or client."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains(a, e) for a, e in zip(actual, expected)
        )
    return actual == expected


def matches(actual, expected):
    return actual is not None and contains(normalized(actual), normalized(expected))


def canonical_core(config):
    """Compare generated configs while permitting TCP/RAW spelling and inbound order."""
    def normalize(value):
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if not isinstance(value, dict):
            return value
        out = {k: normalize(v) for k, v in value.items()}
        if out.get("network") == "raw":
            out["network"] = "tcp"
        if "rawSettings" in out:
            out["tcpSettings"] = out.pop("rawSettings")
        return out
    result = normalize(object_json(config))
    result["inbounds"] = sorted(result.get("inbounds", []), key=lambda row: row.get("tag", ""))
    return result


def chain_view(data):
    """The chain inbound's REALITY identity in the shape validate_identity/keys expect."""
    view = {k: v for k, v in data.items() if k != "public_key"}
    view.update(uuid=data["chain_uuid"], **{
        direct: data.get(chain, data[direct]) for chain, direct in CHAIN_FIELDS.items()})
    if "chain_public_key" in data:
        view["public_key"] = data["chain_public_key"]
    return view


def complete(identity, keys):
    """Spell out the chain identity, deriving its public key when it has its own private key."""
    out = dict(identity)
    for chain, direct in CHAIN_FIELDS.items():
        out.setdefault(chain, out[direct])
    if out["chain_private_key"] == out["private_key"]:
        out["chain_public_key"] = out["public_key"]
    else:
        out["chain_public_key"] = keys(chain_view(out))["public_key"]
    return out


def validate_runtime(data):
    if not isinstance(data, dict):
        raise WorldError("invalid WORLD runtime; restore it before configuring")
    try:
        validate_identity({
            **data, "sni": data["reality_sni"], "dest": data["reality_dest"],
        })
        present = [field for field in CHAIN_FIELDS if field in data]
        if present and len(present) != len(CHAIN_FIELDS):
            raise ValueError("partial chain identity")
        view = chain_view(data)
        validate_identity({**view, "sni": view["reality_sni"], "dest": view["reality_dest"]})
        UUID(data["chain_uuid"])
        if data["uuid"] == data["chain_uuid"]:
            raise ValueError("same identity on both hops")
        if not isinstance(data["exit_ip"], str) or not data["exit_ip"]:
            raise ValueError("missing host")
        ports = [data[k] for k in ("reality_port", "ss_port", "chain_port")]
        if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 3:
            raise ValueError("invalid ports")
        path = data.get("chain_path", "/chain-relay")
        if not isinstance(path, str) or not path.startswith("/") or any(c.isspace() for c in path):
            raise ValueError("invalid path")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise WorldError("invalid WORLD identity/runtime; refusing key rotation") from exc
    return data


def live_identity(items, exit_ip, keys):
    found = managed(items)
    if not found:
        return None
    if set(found) != set(REMARKS):
        raise WorldError("partial managed inbound set; restore it before configuring")
    try:
        direct, ss, chain = [found[name] for name in REMARKS]
        d, s, c = [normalized(i) for i in (direct, ss, chain)]
        for i in (d, s, c):
            if i["enable"] is not True or i["listen"] not in ("", "0.0.0.0", "::"):
                raise ValueError("unsupported listener")
        for i, network, flow in ((d, "tcp", "xtls-rprx-vision"), (c, "xhttp", "")):
            st = i["streamSettings"]
            clients = i["settings"]["clients"]
            rs = st["realitySettings"]
            if (i["protocol"] != "vless" or st["network"] != network
                    or st["security"] != "reality" or len(clients) != 1
                    or clients[0].get("flow", "") != flow
                    or clients[0].get("enable", True) is not True
                    or len(rs["shortIds"]) != 1 or len(rs["serverNames"]) != 1):
                raise ValueError("unsupported VLESS layout")
        dr = d["streamSettings"]["realitySettings"]
        cr = c["streamSettings"]["realitySettings"]
        if s["protocol"] != "shadowsocks" or s["settings"].get("clients", []):
            raise ValueError("unsupported SS layout")
        data = {
            "exit_ip": exit_ip, "uuid": d["settings"]["clients"][0]["id"],
            "private_key": dr["privateKey"], "short_id": dr["shortIds"][0],
            "reality_sni": dr["serverNames"][0], "reality_dest": dr["dest"],
            "reality_port": d["port"], "ss_port": s["port"], "chain_port": c["port"],
            "ss_method": s["settings"]["method"], "ss_password": s["settings"]["password"],
            "chain_uuid": c["settings"]["clients"][0]["id"],
            "chain_path": c["streamSettings"]["xhttpSettings"]["path"],
            "chain_private_key": cr["privateKey"], "chain_short_id": cr["shortIds"][0],
            "chain_reality_sni": cr["serverNames"][0], "chain_reality_dest": cr["dest"],
        }
        validate_runtime(data)
        pair = keys(data)
        return complete({**data, **pair}, keys)
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise WorldError("unsupported/corrupt live WORLD identity; refusing to replace it") from exc


def set_reality(row, view):
    """Return the inbound row with this REALITY identity; untouched when it already has it."""
    stream = object_json(copy.deepcopy(row["streamSettings"]))
    rs = stream["realitySettings"]
    wanted = {"privateKey": view["private_key"], "shortIds": [view["short_id"]],
              "serverNames": [view["reality_sni"]], "dest": view["reality_dest"]}
    if all(rs.get(key) == value for key, value in wanted.items()):
        return row
    rs.update(wanted)
    # The panel keeps the public key beside the private one for its own link display.
    if isinstance(rs.get("settings"), dict) and "publicKey" in rs["settings"]:
        rs["settings"]["publicKey"] = view["public_key"]
    return {**row, "streamSettings": json.dumps(stream) if isinstance(row["streamSettings"], str) else stream}


def new_payloads(identity):
    chain_identity = chain_view(identity)
    result = []
    sniff = {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"],
             "metadataOnly": False, "routeOnly": False}
    for remark, port in zip(REMARKS, (identity["reality_port"], identity["ss_port"], identity["chain_port"])):
        chain = remark == REMARKS[2]
        ss = remark == REMARKS[1]
        row = {"remark": remark, "enable": True, "listen": "", "port": port,
               "up": 0, "down": 0, "total": 0, "expiryTime": 0,
               "protocol": "shadowsocks" if ss else "vless", "tag": f"inbound-{port}"}
        if ss:
            settings = {"method": identity["ss_method"], "password": identity["ss_password"],
                        "network": "tcp,udp", "clients": []}
            stream = {"network": "tcp", "security": "none",
                      "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}}}
        else:
            settings = {"clients": [{"id": identity["chain_uuid"] if chain else identity["uuid"],
                "flow": "" if chain else "xtls-rprx-vision", "email": "bridge-relay" if chain else "main",
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
                "subId": "bridge" if chain else "main"}], "decryption": "none", "fallbacks": []}
            reality = chain_identity if chain else identity
            stream = {"network": "xhttp" if chain else "tcp", "security": "reality",
                      "realitySettings": {"show": False, "xver": 0, "dest": reality["reality_dest"],
                      "serverNames": [reality["reality_sni"]], "privateKey": reality["private_key"],
                      "shortIds": [reality["short_id"]]}}
            if chain:
                stream["xhttpSettings"] = {"mode": "auto", "path": identity.get("chain_path", "/chain-relay")}
            else:
                stream["tcpSettings"] = {"acceptProxyProtocol": False, "header": {"type": "none"}}
        row.update(settings=json.dumps(settings), streamSettings=json.dumps(stream), sniffing=json.dumps(sniff))
        result.append(row)
    return result


def full_candidate(config, before, desired):
    config = copy.deepcopy(object_json(config))
    inbounds = config.get("inbounds")
    if not isinstance(inbounds, list) or not isinstance(config.get("outbounds"), list):
        raise WorldError("panel did not return a complete Xray config")
    for row in desired:
        old = before.get(row["remark"])
        if old:
            hits = [ib for ib in inbounds if ib.get("port") == old["port"]
                    and ib.get("protocol") == old["protocol"]]
            if len(hits) != 1:
                raise WorldError("panel/config drift: managed inbound is not uniquely represented")
            ib = hits[0]
            # Existing identity and all other Xray settings stay exactly as generated by the panel.
            if old["protocol"] == "vless":
                client_ids = [(c.get("id"), c.get("flow", "")) for c in ib.get("settings", {}).get("clients", [])]
                if client_ids != [(c["id"], c.get("flow", "")) for c in object_json(old["settings"])["clients"]]:
                    raise WorldError("panel/config client identity drift")
            expected = normalized(old)
            if old["protocol"] == "vless":
                rs = ib.get("streamSettings", {}).get("realitySettings", {})
                if any(rs.get(k) != expected["streamSettings"]["realitySettings"][k]
                       for k in ("privateKey", "shortIds", "serverNames", "dest")):
                    raise WorldError("panel/config REALITY identity drift")
                if old["remark"] == REMARKS[2] and ib.get("streamSettings", {}).get("xhttpSettings", {}).get("path") != expected["streamSettings"]["xhttpSettings"]["path"]:
                    raise WorldError("panel/config XHTTP path drift")
            elif any(ib.get("settings", {}).get(k) != expected["settings"][k]
                     for k in ("method", "password")):
                raise WorldError("panel/config SS identity drift")
            ib["port"] = row["port"]
            if old["protocol"] == "vless":
                # A requested chain identity change must be part of the validated candidate.
                wanted = normalized(row)["streamSettings"]["realitySettings"]
                rs.update({k: wanted[k] for k in ("privateKey", "shortIds", "serverNames", "dest")})
        else:
            if any(ib.get("port") == row["port"] for ib in inbounds):
                raise WorldError("desired WORLD port conflicts with an existing config inbound")
            ib = {key: row[key] for key in ("port", "protocol", "listen", "tag")}
            ib["listen"] = ib["listen"] or "0.0.0.0"
            for key in JSON_FIELDS:
                ib[key] = object_json(row[key])
            inbounds.append(ib)
    return config


def atomic_write(path, content, mode=0o600):
    fd, temp = tempfile.mkstemp(prefix=".world-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def snapshot(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorldError("state must be a regular file")
    if not path.exists():
        return None
    return {"content": base64.b64encode(path.read_bytes()).decode(),
            "mode": stat.S_IMODE(path.stat().st_mode)}


def restore(path, saved):
    if snapshot(path) == saved:
        return
    if saved is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, base64.b64decode(saved["content"], validate=True), saved["mode"])


def restore_runtime(path, record):
    current = snapshot(path)
    if current not in (record["runtime"], record["runtime_after"]):
        raise WorldError("controller runtime changed outside this transaction; journal retained")
    restore(path, record["runtime"])


def read_journal(path, exit_ip):
    snapshot(path)  # refuse symlinks/non-regular files
    try:
        record = json.loads(path.read_text())
        if record["schema"] != 1 or record["exit_ip"] != exit_ip:
            raise ValueError("wrong target or schema")
        plans = record["plans"]
        if len(plans) != 3 or {p["after"]["remark"] for p in plans} != set(REMARKS):
            raise ValueError("unexpected plans")
        for plan in plans:
            normalized(plan["after"])
            old = plan["before"]
            if old is not None:
                normalized(old)
                if old["remark"] != plan["after"]["remark"] or type(old["id"]) is not int or old["id"] <= 0:
                    raise ValueError("invalid original inbound")
        for field in ("runtime", "runtime_after"):
            saved = record[field]
            if saved is not None:
                if type(saved["mode"]) is not int or not 0 <= saved["mode"] <= 0o777:
                    raise ValueError("invalid mode")
                validate_runtime(json.loads(base64.b64decode(saved["content"], validate=True)))
        return record
    except (KeyError, TypeError, ValueError, WorldError) as exc:
        raise WorldError("invalid recovery journal or different WORLD; restore it before configuring") from exc


class Panel:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar()),
        )

    def request(self, route, payload=None, form=False):
        headers = {}
        body = None
        if payload is not None:
            try:
                token = self.request("/csrf-token")
            except WorldError:
                raise WorldError("CSRF endpoint unavailable; no mutation attempted") from None
            if not isinstance(token, str) or not token:
                raise WorldError("missing CSRF token")
            headers["X-CSRF-Token"] = token
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            body = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
        try:
            req = urllib.request.Request(self.base + route, data=body, headers=headers)
            with self.opener.open(req, timeout=20) as response:
                data = json.load(response)
            if not isinstance(data, dict) or data.get("success") is not True:
                raise WorldError("panel rejected a request; response body withheld")
            return data.get("obj")
        except (OSError, ValueError) as exc:
            raise WorldError("panel request failed or returned invalid JSON; body withheld") from exc

    def login(self, user, password):
        self.request("/login", {"username": user, "password": password}, form=True)

    def list(self):
        return self.request("/panel/api/inbounds/list")

    def config(self):
        return object_json(self.request("/panel/api/server/getConfigJson"))

    def put(self, row, inbound_id=None):
        path = "/add" if inbound_id is None else f"/update/{int(inbound_id)}"
        self.request("/panel/api/inbounds" + path, row)

    def delete(self, inbound_id):
        self.request(f"/panel/api/inbounds/del/{int(inbound_id)}", {})

    def healthy(self):
        status = self.request("/panel/api/server/status")
        return isinstance(status, dict) and status.get("xray", {}).get("state") == "running"


class Remote:
    def __init__(self, options):
        self.options = options
        self.ssh = ["ssh", "-i", options["ssh_key"], "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]
        self.destination = options["ssh_user"] + "@" + options["exit_ip"]

    def call(self, action, **data):
        prefix = ["sudo", "-n"] if self.options.get("become") and self.options["ssh_user"] != "root" else []
        command = shlex.join(prefix + ["python3", self.options["remote_helper"]])
        proc = subprocess.run(
            [*self.ssh, self.destination, command],
            input=json.dumps({"action": action, "binary": self.options["binary"], **data}),
            capture_output=True, text=True, timeout=50,
        )
        if proc.returncode:
            raise WorldError(f"WORLD {action} failed; remote diagnostics withheld")
        return object_json(proc.stdout)

    def keys(self, identity):
        out = self.call("keys", private_key=identity.get("private_key", ""))["output"]
        adapted = {**identity, "sni": identity.get("reality_sni"), "dest": identity.get("reality_dest")}
        return recover_keys(out, adapted if identity else {})

    def validate(self, config):
        return self.call("validate", config=config)

    def verify(self, rows, panel):
        deadline = time.monotonic() + 30
        while True:
            try:
                self.call("ports", ports=[row["port"] for row in rows])
                if panel.healthy():
                    return
            except WorldError:
                pass
            if time.monotonic() >= deadline:
                raise WorldError("WORLD did not pass local port/service checks")
            time.sleep(1)

    @contextmanager
    def panel(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        forward = f"127.0.0.1:{port}:127.0.0.1:{int(self.options['panel_port'])}"
        with subprocess.Popen(
            [*self.ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-L", forward, self.destination],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as proc:
            try:
                deadline = time.monotonic() + 12
                while True:
                    if proc.poll() is not None:
                        raise WorldError("SSH panel tunnel failed")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise WorldError("SSH panel tunnel timed out") from None
                        time.sleep(0.1)
                yield Panel(f"http://127.0.0.1:{port}{self.options['panel_path']}")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def rollback(panel, plans):
    for plan in reversed(plans):
        old, new = plan["before"], plan["after"]
        current = managed(panel.list()).get(new["remark"])
        if old is not None and current and current["id"] == old["id"] and matches(current, old):
            continue
        if old is None and current is None:
            continue
        if not matches(current, new) or (old is not None and current["id"] != old["id"]):
            raise WorldError("rollback conflict: managed inbound changed outside this transaction")
        try:
            if old is None:
                panel.delete(current["id"])
            else:
                panel.put(old, old["id"])
        except WorldError:
            # A lost response does not tell us whether the API already committed.
            pass
        current = managed(panel.list()).get(new["remark"])
        if (old is None and current is not None) or (old is not None and not matches(current, old)):
            raise WorldError("rollback API verification failed; recovery journal retained")


def transact(options, state_dir, panel, remote):
    runtime = state_dir / "runtime.json"
    journal = state_dir / ".world-transaction.json"
    saved = snapshot(runtime)
    if journal.exists() or journal.is_symlink():
        record = read_journal(journal, options["exit_ip"])
        rollback(panel, record["plans"])
        remote.verify([p["before"] for p in record["plans"] if p["before"]], panel)
        restore_runtime(runtime, record)
        journal.unlink()
        raise WorldError("unfinished WORLD transaction restored; rerun configure")
    previous = validate_runtime(json.loads(base64.b64decode(saved["content"]))) if saved else None
    items = panel.list()
    before = managed(items)
    identity = live_identity(items, options["exit_ip"], remote.keys)
    source = "live"
    if identity is None:
        if previous and previous["exit_ip"] == options["exit_ip"]:
            identity = complete({**previous, **remote.keys(previous)}, remote.keys)
            source = "runtime"
        else:
            sizes = {"2022-blake3-aes-128-gcm": 16, "2022-blake3-aes-256-gcm": 32,
                     "2022-blake3-chacha20-poly1305": 32}
            if options["ss_method"] not in sizes:
                raise WorldError("unsupported SS2022 method")
            chain_keys = remote.keys({})
            identity = {
                "exit_ip": options["exit_ip"], "uuid": str(uuid4()), "chain_uuid": str(uuid4()),
                "short_id": secrets.token_hex(4), "ss_method": options["ss_method"],
                "ss_password": base64.b64encode(secrets.token_bytes(sizes[options["ss_method"]])).decode(),
                "reality_sni": options["reality_sni"], "reality_dest": options["reality_dest"],
                "chain_path": "/chain-relay", **remote.keys({}),
                # A new WORLD never shares REALITY keys between clients and bridges.
                "chain_private_key": chain_keys["private_key"],
                "chain_public_key": chain_keys["public_key"],
                "chain_short_id": secrets.token_hex(4),
                "chain_reality_sni": options["reality_sni"],
                "chain_reality_dest": options["reality_dest"],
            }
            source = "fresh"
    if options.get("separate_chain"):
        # Explicit migration: own key/shortId if still shared, and the measured chain target.
        target = (options.get("chain_reality_sni"), options.get("chain_reality_dest"))
        if not all(isinstance(v, str) and v and not any(c.isspace() for c in v) for v in target):
            raise WorldError("separate chain identity needs a probed chain REALITY target")
        if identity["chain_private_key"] == identity["private_key"]:
            chain_keys = remote.keys({})
            identity.update(chain_private_key=chain_keys["private_key"],
                            chain_public_key=chain_keys["public_key"],
                            chain_short_id=secrets.token_hex(4))
        identity.update(chain_reality_sni=target[0], chain_reality_dest=target[1])
    if identity["ss_method"] != options["ss_method"]:
        raise WorldError("changing the existing SS method requires an explicit identity migration")
    for key in ("reality_port", "ss_port", "chain_port"):
        identity[key] = int(options[key])
    validate_runtime(identity)
    if before:
        desired = [copy.deepcopy(before[r]) for r in REMARKS]
        for row, key in zip(desired, ("reality_port", "ss_port", "chain_port")):
            row["port"] = identity[key]
        desired[2] = set_reality(desired[2], chain_view(identity))
    else:
        desired = new_payloads(identity)
    # Reject conflicts before changing anything, including swaps between managed ports.
    for row in desired:
        if any(i.get("port") == row["port"] and i.get("remark") != row["remark"] for i in items):
            raise WorldError("desired port is owned by another inbound; no changes applied")
    candidate = full_candidate(panel.config(), before, desired)
    engine = remote.validate(candidate)
    # The binary that just validated the candidate is the one that will run it.
    if options.get("xray_sha256") and engine.get("sha256") != options["xray_sha256"]:
        raise WorldError("installed WORLD Xray differs from the pinned release; no changes applied")
    if options.get("xui_version"):
        identity["world_xui_version"] = options["xui_version"]
    identity.update(
        panel_url=f"http://{options['exit_ip']}:{options['panel_port']}{options['panel_path']}/",
        panel_user=options["panel_user"], panel_port=str(options["panel_port"]),
        panel_path=options["panel_path"], world_xray=engine,
    )
    identity.pop("panel_pass", None)
    plans = [{"before": before.get(row["remark"]), "after": row} for row in desired]
    content = (json.dumps(identity, indent=2) + "\n").encode()
    after = {"content": base64.b64encode(content).decode(), "mode": 0o600}
    record = {"schema": 1, "exit_ip": options["exit_ip"], "plans": plans,
              "runtime": saved, "runtime_after": after}
    atomic_write(journal, json.dumps(record).encode())
    changed = 0
    try:
        for plan in plans:
            old, new = plan["before"], plan["after"]
            current = managed(panel.list()).get(new["remark"])
            if old is not None and (current is None or current["id"] != old["id"] or not matches(current, old)):
                raise WorldError("managed inbound changed during preflight; refusing to overwrite it")
            if old is None and current is not None:
                raise WorldError("managed inbound appeared during preflight")
            if old is not None and matches(old, new):
                continue
            panel.put(new, old["id"] if old else None)
            changed += 1
        final = managed(panel.list())
        if any(not matches(final.get(row["remark"]), row) for row in desired):
            raise WorldError("panel read-back does not match the requested inbounds")
        actual_config = panel.config()
        if not contains(canonical_core(actual_config), canonical_core(candidate)):
            raise WorldError("panel generated an unexpected config; refusing to publish runtime")
        if changed:
            remote.validate(actual_config)
        remote.verify(desired, panel)
        if snapshot(runtime) != saved:
            raise WorldError("controller runtime changed during preflight")
        state_changed = saved is None or base64.b64decode(saved["content"]) != content
        if state_changed:
            atomic_write(runtime, content)
        journal.unlink()
    except Exception as exc:
        try:
            rollback(panel, plans)
            remote.verify(list(before.values()), panel)
            restore_runtime(runtime, record)
            journal.unlink()
        except Exception as recovery:
            raise WorldError("WORLD apply failed; rollback incomplete, recovery journal retained") from recovery
        raise WorldError("WORLD apply failed; managed inbounds and runtime restored") from exc
    return {"changed": changed > 0 or state_changed, "inbounds_changed": changed, "identity_source": source}


def main():
    try:
        options = json.load(sys.stdin)
        state_dir = Path(options["state_dir"])
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = state_dir / ".world-configure.lock"
        with lock_path.open("a") as lock:
            lock_path.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            remote = Remote(options)
            with remote.panel() as panel:
                panel.login(options["panel_user"], options["panel_pass"])
                result = transact(options, state_dir, panel, remote)
        print(json.dumps(result))
    except Exception as exc:
        message = str(exc) if isinstance(exc, WorldError) else "WORLD preparation failed; inspect inputs/state (details withheld)"
        print(message, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
