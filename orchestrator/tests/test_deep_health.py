from __future__ import annotations

import json
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from pipelinectl import deep_health
from pipelinectl.deep_health import _login_base_url, run_deep_health


def test_login_base_url_prefers_panel_url() -> None:
    assert _login_base_url({"panel_url": "http://x:2053/panel/"}) == "http://x:2053/panel"


def test_login_base_url_from_exit_fields() -> None:
    assert (
        _login_base_url(
            {
                "exit_ip": "1.2.3.4",
                "panel_port": "2053",
                "panel_path": "/secretpanel",
            }
        )
        == "http://1.2.3.4:2053/secretpanel"
    )


def test_run_deep_health_missing_exit_ip(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("dummy", encoding="utf-8")
    rpt = run_deep_health({}, key, "secret", "root")
    names = [c.name for c in rpt.results]
    assert "deep_prereq" in names
    assert any(not c.ok for c in rpt.results)


# ── 3X-UI >= 3.4 CSRF flow ─────────────────────────────────────────────────────


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakePanel:
    """Behaves like 3X-UI 3.4: /csrf-token hands out a token, POST /login without it -> 403."""

    def __init__(self) -> None:
        self.login_attempts: list[str | None] = []

    def open(self, req, timeout: float = 0):  # noqa: ANN001 — urllib.request.Request
        import io
        import urllib.error

        url, method = req.full_url, req.get_method()
        if url.endswith("/csrf-token") and method == "GET":
            return _Resp(b'{"obj":"tok-123","success":true}')
        if url.endswith("/login") and method == "POST":
            tok = req.get_header("X-csrf-token")
            self.login_attempts.append(tok)
            if tok != "tok-123":
                raise urllib.error.HTTPError(url, 403, "Forbidden", None, io.BytesIO(b""))
            return _Resp(b'{"success":true,"msg":"ok","obj":null}')
        if url.endswith("/panel/api/inbounds/list"):
            inbounds = [
                {"remark": r, "enable": True} for r in sorted(deep_health.EXPECTED_INBOUND_REMARKS)
            ]
            return _Resp(json.dumps({"success": True, "obj": inbounds}).encode())
        raise AssertionError(f"unexpected {method} {url}")


def test_panel_login_sends_csrf_token(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _FakePanel()
    monkeypatch.setattr(deep_health.urllib.request, "build_opener", lambda *a, **k: panel)
    data = {"panel_url": "http://198.51.100.9:2053/secretpanel/", "panel_user": "admin"}
    ok, msg, opener = deep_health._panel_login(data, "pw", CookieJar())
    assert ok, msg
    assert panel.login_attempts == ["tok-123"]
    ok_ib, msg_ib = deep_health._panel_inbounds_list(data, opener)
    assert ok_ib and msg_ib.startswith("inbounds_ok"), msg_ib


def test_panel_login_without_csrf_endpoint_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class Legacy(_FakePanel):
        def open(self, req, timeout: float = 0):  # noqa: ANN001
            import io
            import urllib.error

            if req.full_url.endswith("/csrf-token"):
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, io.BytesIO(b""))
            if req.full_url.endswith("/login"):
                self.login_attempts.append(req.get_header("X-csrf-token"))
                return _Resp(b'{"success":true}')
            return super().open(req, timeout)

    panel = Legacy()
    monkeypatch.setattr(deep_health.urllib.request, "build_opener", lambda *a, **k: panel)
    ok, msg, _ = deep_health._panel_login({"panel_url": "http://h:2053/p"}, "pw", CookieJar())
    assert ok, msg
    assert panel.login_attempts == [None]  # no header when the panel has no CSRF endpoint


def test_bridge_probe_round_returns_last_round_only() -> None:
    class P:
        returncode = 0
        stderr = ""
        stdout = "\n".join(
            [
                '{"ts":"2026-09-11T06:00:14Z","check":"tls_exit","ok":true,"ms":142,"code":"","err":""}',
                '{"ts":"2026-09-11T06:00:14Z","check":"dest_tls","ok":false,"ms":15004,"code":"","err":""}',
                '{"ts":"2026-09-11T06:01:14Z","check":"tls_exit","ok":true,"ms":140,"code":"","err":""}',
                '{"ts":"2026-09-11T06:01:14Z","check":"chain_http","ok":true,"ms":200,"code":"204","err":""}',
                '{"ts":"2026-09-11T06:01:14Z","check":"direct_http","ok":true,"ms":90,"code":"204","err":""}',
                '{"ts":"2026-09-11T06:01:14Z","check":"dest_tls","ok":true,"ms":85,"code":"","err":""}',
            ]
        )

    ok, msg, recs = deep_health._bridge_probe_round(
        "b", "ubuntu", Path("/k"), runner=lambda *a, **k: P()
    )
    assert (
        ok
        and msg == ""
        and [r["check"] for r in recs] == ["tls_exit", "chain_http", "direct_http", "dest_tls"]
    )

    class NoProbe(P):
        returncode = 3
        stdout = "NO_PROBE\n"

    ok, msg, recs = deep_health._bridge_probe_round(
        "b", "ubuntu", Path("/k"), runner=lambda *a, **k: NoProbe()
    )
    assert not ok and "not installed" in msg and recs == []
