from __future__ import annotations

from pathlib import Path

import pytest

from pipelinectl import cherry


def test_read_kv_file_env_and_tfvars_styles(tmp_path: Path) -> None:
    f = tmp_path / "vars"
    f.write_text(
        "# comment\n"
        'CHERRY_API_TOKEN="eyJ.env.token"\n'
        "export PLAIN=value # trailing\n"
        'cherry_api_token  = "eyJ.tf.token" # portal → API Keys\n'
        'exit_region = "NL-Amsterdam"\n'
        "exit_plan   = 'B2-1-1gb-20s-shared'\n"
        "broken line without equals\n",
        encoding="utf-8",
    )
    kv = cherry.read_kv_file(f)
    assert kv["CHERRY_API_TOKEN"] == "eyJ.env.token"
    assert kv["PLAIN"] == "value"
    assert kv["cherry_api_token"] == "eyJ.tf.token"
    assert kv["exit_region"] == "NL-Amsterdam"
    assert kv["exit_plan"] == "B2-1-1gb-20s-shared"
    assert "broken" not in " ".join(kv)


def test_read_kv_file_missing(tmp_path: Path) -> None:
    assert cherry.read_kv_file(tmp_path / "nope") == {}


def test_api_token_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "terraform").mkdir()
    (tmp_path / "terraform" / "terraform.tfvars").write_text('cherry_api_token = "from-tfvars"\n')
    monkeypatch.setenv("PROXY_PIPELINE_ROOT", str(tmp_path))
    monkeypatch.delenv("CHERRY_API_TOKEN", raising=False)
    assert cherry.api_token() == "from-tfvars"
    (tmp_path / ".env").write_text('CHERRY_API_TOKEN="from-dotenv"\n')
    assert cherry.api_token() == "from-dotenv"
    monkeypatch.setenv("CHERRY_API_TOKEN", "from-env")
    assert cherry.api_token() == "from-env"


def _fake_fetch(path: str, token: str):
    assert token == "t"
    if path == "/teams":
        return [{"id": 1}, {"id": 2}]
    if path == "/teams/1/plans":
        return [
            {
                "slug": "B2-1-1gb-20s-shared",
                "available_regions": [
                    {"slug": "NL-Amsterdam", "stock_qty": 126},
                    {"slug": "SE-Stockholm", "stock_qty": 0},
                    {"name": "LT-Siauliai", "stock_qty": "621"},
                ],
            },
            {
                "slug": "B1-1-1gb-20s-shared",
                "available_regions": [{"slug": "NL-Amsterdam", "stock_qty": 53}],
            },
            {"slug": "E5-1620v4", "available_regions": [{"slug": "LT-Siauliai", "stock_qty": 9}]},
        ]
    if path == "/teams/2/plans":  # second team sees more stock for the same plan -> max wins
        return [
            {
                "slug": "B2-1-1gb-20s-shared",
                "available_regions": [{"slug": "NL-Amsterdam", "stock_qty": 200}],
            }
        ]
    raise AssertionError(path)


def test_plans_stock_merges_teams_and_filters() -> None:
    data = cherry.plans_stock("t", fetch=_fake_fetch, plan_filter=cherry.is_cloud_vps)
    assert set(data) == {"B2-1-1gb-20s-shared", "B1-1-1gb-20s-shared"}  # bare-metal E5 filtered out
    assert data["B2-1-1gb-20s-shared"] == {
        "NL-Amsterdam": 200,
        "SE-Stockholm": 0,
        "LT-Siauliai": 621,
    }
    assert data["B1-1-1gb-20s-shared"] == {"NL-Amsterdam": 53}


def test_region_stock_semantics() -> None:
    data = cherry.plans_stock(
        "t", fetch=_fake_fetch, plan_filter=lambda s: s == "B1-1-1gb-20s-shared"
    )
    assert cherry.region_stock(data, "B1-1-1gb-20s-shared", "NL-Amsterdam") == 53
    assert cherry.region_stock(data, "B1-1-1gb-20s-shared", "DE-Frankfurt") == 0  # not sold there
    assert (
        cherry.region_stock(data, "B2-1-1gb-20s-shared", "NL-Amsterdam") is None
    )  # plan not fetched


def test_is_cloud_vps() -> None:
    assert cherry.is_cloud_vps("B1-1-1gb-20s-shared") and cherry.is_cloud_vps("B2-2-2gb-40s-shared")
    assert not cherry.is_cloud_vps("E5-1620v4") and not cherry.is_cloud_vps("")
