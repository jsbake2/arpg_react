from datetime import datetime, timezone
from pathlib import Path

from arpg_react.config import Config, load_config, save_config
from arpg_react.sources import ClockSource
from arpg_react.timers import EventKind, EventState


def test_default_config_has_all_event_kinds():
    cfg = Config()
    assert set(cfg.events) == set(EventKind)
    assert cfg.events[EventKind.HELLTIDE].warn_at_seconds == [180, 120, 30]
    assert cfg.events[EventKind.HELLTIDE].chime_enabled is False
    assert cfg.events[EventKind.HELLTIDE].tts_enabled is False
    assert cfg.events[EventKind.REALMWALKER].muted is True
    assert cfg.source == "composite"


def test_load_returns_default_when_file_missing(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.json")
    assert isinstance(cfg, Config)


def test_save_then_load_roundtrip(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = Config()
    cfg.events[EventKind.HELLTIDE].muted = True
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.events[EventKind.HELLTIDE].muted is True


def test_anchor_map_excludes_unset():
    cfg = Config()
    cfg.anchors.realmwalker = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    m = cfg.anchor_map()
    assert list(m) == [EventKind.REALMWALKER]


def test_realmwalker_anchor_threads_through_clock_source():
    anchor = datetime(2026, 5, 4, 18, 0, 0, tzinfo=timezone.utc)
    src = ClockSource(anchors={EventKind.REALMWALKER: anchor})
    s = src.status(EventKind.REALMWALKER, anchor)
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 8 * 60
