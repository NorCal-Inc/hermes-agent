"""Board rules come from the board's config, not the worker's profile config (2026-10-05)."""
from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_worker_profile_inherits_board_limit(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    _write(root / "config.yaml", "kanban:\n  gauntlet_objective_attempt_limit: 12\n")
    prof = root / "profiles" / "lead"
    _write(prof / "config.yaml", "model:\n  default: x\n")          # no kanban section
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert kb._kanban_cfg(lambda: {"model": {}}).get("gauntlet_objective_attempt_limit") == 12


def test_process_kanban_value_still_overrides(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    _write(root / "config.yaml", "kanban:\n  gauntlet_objective_attempt_limit: 12\n  a: 1\n")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    cfg = kb._kanban_cfg(lambda: {"kanban": {"gauntlet_objective_attempt_limit": 3}})
    assert cfg["gauntlet_objective_attempt_limit"] == 3 and cfg["a"] == 1


def test_missing_board_config_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "nowhere"))
    assert kb._kanban_cfg(lambda: None) == {}
    assert kb._kanban_cfg(lambda: {"kanban": {"x": 1}}) == {"x": 1}
