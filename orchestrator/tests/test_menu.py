from __future__ import annotations

import yaml

from pipelinectl import menu

CONFIG = """# top comment
xray:
  chain_port: 8443   # chain
watchdog:
  auto_rotate: false
  cooldown_hours: 2

firewall:
  direct_policy: public   # who reaches direct ports
  ssh_world: true
"""


def test_set_scalar_changes_one_value_and_keeps_comments():
    text = menu.set_scalar(CONFIG, "firewall", "direct_policy", "closed")
    assert "direct_policy: closed   # who reaches direct ports" in text
    assert text.replace("closed", "public") == CONFIG
    assert yaml.safe_load(menu.set_scalar(CONFIG, "watchdog", "auto_rotate", "true"))["watchdog"] == {
        "auto_rotate": True, "cooldown_hours": 2}


def test_set_scalar_adds_missing_keys_and_sections_and_quotes_odd_values():
    text = menu.set_scalar(CONFIG, "firewall", "admin_cidrs_note", "my office")
    assert yaml.safe_load(text)["firewall"]["admin_cidrs_note"] == "my office"
    text = menu.set_scalar(CONFIG, "alerts", "telegram_chat_id", "-100123")
    assert yaml.safe_load(text)["alerts"] == {"telegram_chat_id": -100123}
    assert yaml.safe_load(text)["xray"] == {"chain_port": 8443}


def _menu(answers):
    calls, feed = [], iter(answers)

    def ask(prompt):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    menu.run_menu(ask=ask, run=lambda args: calls.append(args) or 0)
    return calls


def test_menu_runs_the_ordinary_commands():
    assert _menu(["1", "3", "0"]) == [["status"], ["validate", "--deep"]]


def test_bridge_replacement_shows_the_plan_and_asks_before_ordering():
    assert _menu(["7", "", "n", "0"]) == [["rotate", "bridge", "--dry-run"]]
    assert _menu(["7", "", "y", "y", "0"]) == [
        ["rotate", "bridge", "--dry-run"], ["rotate", "bridge", "--yes", "--delete-old"]]
    assert _menu(["7", "203.0.113.5", "y", "0"]) == [["rotate", "bridge", "--bridge-ip", "203.0.113.5"]]


def test_exit_replacement_needs_a_yes():
    assert _menu(["6", "", "n", "0"]) == [["stock"]]
    assert _menu(["6", "LT-Siauliai", "y", "0"]) == [["stock"], ["rotate", "exit", "--yes", "--region", "LT-Siauliai"]]


def test_end_of_input_leaves_the_menu():
    assert _menu([]) == []
