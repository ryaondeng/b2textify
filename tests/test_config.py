"""配置层的回归：缓存目录的优先级、`.env` 只补缺。

优先级是 `--cache` > `B2T_CACHE_DIR`（环境变量，`.env` 也走这条）> toml 的 `paths.cache_dir` > 默认。
这几条一旦顺序错了，"我明明在 .env 里配了目录怎么还在 C 盘"会非常难查。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from b2textify.config import load_config


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """切到空目录：避免读到仓库自己的 .env 与 b2textify.toml。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("B2T_CACHE_DIR", raising=False)
    return work


def test_default_cache_dir_is_outside_repo() -> None:
    cfg = load_config()
    assert cfg.cache_dir.name == "cache"
    assert "b2textify" in str(cfg.cache_dir).lower()


def test_env_variable_overrides_default(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("B2T_CACHE_DIR", str(target))
    assert load_config().cache_dir == target


def test_dotenv_supplies_cache_dir(_isolated_cwd: Path, monkeypatch) -> None:
    target = _isolated_cwd / "cached"
    (_isolated_cwd / ".env").write_text(f"B2T_CACHE_DIR={target}\n", encoding="utf-8")
    assert load_config().cache_dir == target


def test_cli_flag_beats_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("B2T_CACHE_DIR", str(tmp_path / "from-env"))
    from_flag = tmp_path / "from-flag"
    assert load_config(cache_dir=from_flag).cache_dir == from_flag


def test_toml_paths_beat_default_but_lose_to_env(_isolated_cwd: Path, monkeypatch, tmp_path: Path) -> None:
    toml = _isolated_cwd / "b2textify.toml"
    toml.write_text('[paths]\ncache_dir = "from-toml"\n', encoding="utf-8")
    assert load_config().cache_dir == Path("from-toml")

    monkeypatch.setenv("B2T_CACHE_DIR", str(tmp_path / "from-env"))
    assert load_config().cache_dir == tmp_path / "from-env"
