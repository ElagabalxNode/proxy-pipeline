from __future__ import annotations

from pipelinectl.health import CheckResult
from pipelinectl.status import diagnose, render

ALL_GOOD = [
    CheckResult("exit_ssh", True, "info"),
    CheckResult("tcp_exit_chain", True, "info", "198.51.100.5:8443"),
    CheckResult("panel_login", True, "info"),
    CheckResult("bridge_ssh", True, "info"),
    CheckResult("entry_dns", True, "info"),
    CheckResult("bridge_tls_exit", True, "info"),
    CheckResult("bridge_chain_http", True, "info"),
    CheckResult("bridge_direct_http", True, "info"),
    CheckResult("bridge_entry_https", True, "info"),
    CheckResult("bridge_entry_cert", True, "info"),
]


def _with(*failed: str, detail: str = "") -> list[CheckResult]:
    return [CheckResult(c.name, False, "error", detail) if c.name in failed else c for c in ALL_GOOD]


def _broken(parts):
    return {p.name: p.fix for p in parts if not p.ok}


def test_everything_green_names_nothing():
    parts = diagnose(ALL_GOOD, outside=(19, 20))
    assert _broken(parts) == {} and render(parts).endswith("All parts work.")


def test_dead_exit_is_rotated_and_nothing_else_is_blamed():
    parts = diagnose(_with("exit_ssh", "tcp_exit_chain", "panel_login", "bridge_tls_exit", "bridge_chain_http"))
    assert _broken(parts) == {"Exit (WORLD)": ["pipelinectl rotate exit --yes",
                                              "(out of stock: pipelinectl stock, then add --region SLUG)"]}


def test_exit_egress_failure_is_seen_from_the_bridge():
    parts = diagnose(_with("bridge_chain_http"))
    assert list(_broken(parts)) == ["Exit (WORLD)"]


def test_bridge_without_any_egress_is_moved_to_a_new_server():
    parts = diagnose(_with("bridge_chain_http", "bridge_direct_http", "bridge_tls_exit"))
    fixes = _broken(parts)
    assert list(fixes) == ["Bridge (RU)"]
    assert fixes["Bridge (RU)"][-1] == "pipelinectl rotate bridge --bridge-ip NEW_IP"


def test_filtered_bridge_address_is_found_only_from_outside():
    # 2026-10-13: every check on the bridge was green while 30 of 40 Russian networks timed out.
    parts = diagnose(ALL_GOOD, outside=(10, 40))
    fixes = _broken(parts)
    assert list(fixes) == ["Bridge (RU)"] and "NEW_IP" in fixes["Bridge (RU)"][-1]
    assert diagnose(ALL_GOOD, outside=(0, 0))[1].ok  # nothing measured is not a failure


def test_entry_software_dns_and_certificate_have_their_own_fixes():
    parts = diagnose(_with("bridge_entry_https", "entry_dns", "bridge_entry_cert"))
    assert _broken(parts) == {
        "Bridge (RU)": ["pipelinectl configure --bridge-only"],
        "DNS name": ["pipelinectl dns ensure --replace"],
        "Certificate": ["pipelinectl cert renew --deploy"],
    }


def test_render_lists_problems_and_numbered_fixes():
    text = render(diagnose(_with("exit_ssh", detail="timed out"), providers={"bridge": "VDSina"}))
    assert "[DOWN] Exit (WORLD)" in text and "no SSH from the controller: timed out" in text
    assert "fix 1: pipelinectl rotate exit --yes" in text and "[OK  ] Bridge (RU)    VDSina" in text
    assert text.endswith("Broken: Exit (WORLD).")


def test_stale_state_is_synced_before_dns_is_touched():
    # 2026-10-14: DNS already pointed to the new address, state still to the old one; moving DNS
    # "back to the bridge" would have sent clients to the filtered address again.
    parts = diagnose(_with("entry_dns"), declared_bridge="203.0.113.2", recorded_bridge="203.0.113.1")
    fixes = _broken(parts)
    assert fixes["Bridge (RU)"] == ["pipelinectl configure --bridge-only", "pipelinectl status"]
    assert "pipelinectl dns ensure" not in " ".join(fixes["DNS name"])
    assert diagnose(ALL_GOOD, declared_bridge="203.0.113.2", recorded_bridge="203.0.113.2")[1].ok


def test_a_bridge_with_an_api_is_replaced_by_one_command():
    parts = diagnose(ALL_GOOD, outside=(5, 20), bridge_automatic=True)
    assert _broken(parts) == {"Bridge (RU)": ["pipelinectl rotate bridge   (orders, checks and moves to a new server)"]}
