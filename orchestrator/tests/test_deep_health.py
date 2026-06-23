from __future__ import annotations

from pathlib import Path

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
