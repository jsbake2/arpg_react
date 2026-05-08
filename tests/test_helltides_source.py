from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from arpg_react.sources.base import SourceUnavailable
from arpg_react.sources.helltides import API_URL, HelltidesSource
from arpg_react.timers import EventKind, EventState

NOW = datetime(2026, 5, 4, 18, 0, 0, tzinfo=timezone.utc)


def _schedule_with_wb(start: datetime, boss: str = "Wandering Death", zone: str = "Fractured Peaks") -> dict:
    return {
        "world_boss": [
            {
                "id": "wb1",
                "timestamp": int(start.timestamp()),
                "boss": boss,
                "type": "world_boss",
                "startTime": start.isoformat().replace("+00:00", "Z"),
                "zone": [{"id": "fp", "name": zone, "isWhisper": False, "boss": boss}],
            }
        ],
        "legion": [],
        "helltide": [],
    }


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "helltides.json"


@respx.mock
def test_fetches_and_returns_upcoming_with_boss_label(cache_path: Path):
    start = NOW + timedelta(minutes=23)
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=_schedule_with_wb(start)))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.WORLD_BOSS, NOW)

    assert s.state is EventState.UPCOMING
    assert s.next_change == start
    assert s.seconds_until_change == 23 * 60
    assert s.label_extra == "Wandering Death — Fractured Peaks"


@respx.mock
def test_returns_active_when_inside_window(cache_path: Path):
    start = NOW - timedelta(minutes=5)  # 5min into a 15min active window
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=_schedule_with_wb(start)))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.WORLD_BOSS, NOW)

    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 10 * 60


@respx.mock
def test_returns_ending_soon_at_tail_of_window(cache_path: Path):
    start = NOW - timedelta(minutes=14, seconds=30)
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=_schedule_with_wb(start)))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.WORLD_BOSS, NOW)

    assert s.state is EventState.ENDING_SOON
    assert s.seconds_until_change == 30


@respx.mock
def test_writes_disk_cache(cache_path: Path):
    start = NOW + timedelta(minutes=10)
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=_schedule_with_wb(start)))

    HelltidesSource(cache_path=cache_path).status(EventKind.WORLD_BOSS, NOW)

    payload = json.loads(cache_path.read_text())
    assert payload["fetched_at"]
    assert payload["schedule"]["world_boss"][0]["boss"] == "Wandering Death"


@respx.mock
def test_uses_disk_cache_when_fetch_fails_within_stale_threshold(cache_path: Path):
    fresh_start = NOW + timedelta(minutes=10)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": (NOW - timedelta(minutes=10)).isoformat(),
                "schedule": _schedule_with_wb(fresh_start),
            }
        )
    )
    route = respx.get(API_URL).mock(side_effect=httpx.ConnectError("offline"))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.WORLD_BOSS, NOW)

    assert route.called
    assert s.state is EventState.UPCOMING
    assert s.seconds_until_change == 10 * 60


@respx.mock
def test_raises_when_cache_is_stale_and_fetch_fails(cache_path: Path):
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": (NOW - timedelta(hours=2)).isoformat(),
                "schedule": _schedule_with_wb(NOW + timedelta(minutes=5)),
            }
        )
    )
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("offline"))

    src = HelltidesSource(cache_path=cache_path)
    with pytest.raises(SourceUnavailable):
        src.status(EventKind.WORLD_BOSS, NOW)


@respx.mock
def test_picks_next_entry_when_first_already_ended(cache_path: Path):
    schedule = {
        "world_boss": [
            {
                "id": "old",
                "boss": "Avarice",
                "startTime": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                "zone": [{"name": "Hawezar"}],
            },
            {
                "id": "new",
                "boss": "Ashava",
                "startTime": (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                "zone": [{"name": "Dry Steppes"}],
            },
        ],
        "legion": [],
        "helltide": [],
    }
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=schedule))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.WORLD_BOSS, NOW)

    assert s.label_extra == "Ashava — Dry Steppes"
    assert s.seconds_until_change == 30 * 60


@respx.mock
def test_rejects_realmwalker_kind(cache_path: Path):
    src = HelltidesSource(cache_path=cache_path)
    with pytest.raises(SourceUnavailable):
        src.status(EventKind.REALMWALKER, NOW)


@respx.mock
def test_serves_helltide_with_55min_active_window(cache_path: Path):
    start = NOW - timedelta(minutes=10)  # in active window
    schedule = {
        "world_boss": [{
            "boss": "Ashava",
            "startTime": (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "zone": [{"name": "Dry Steppes"}],
        }],
        "legion": [],
        "helltide": [
            {"startTime": start.isoformat().replace("+00:00", "Z"), "type": "helltide"}
        ],
    }
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=schedule))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.HELLTIDE, NOW)

    assert s.kind is EventKind.HELLTIDE
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 45 * 60
    assert s.label_extra is None  # no boss/zone label for helltide


@respx.mock
def test_serves_legion_with_5min_active_window(cache_path: Path):
    start = NOW + timedelta(minutes=8)
    schedule = {
        "world_boss": [{
            "boss": "Ashava",
            "startTime": (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "zone": [{"name": "Dry Steppes"}],
        }],
        "helltide": [],
        "legion": [
            {"startTime": start.isoformat().replace("+00:00", "Z"), "type": "legion"}
        ],
    }
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=schedule))

    src = HelltidesSource(cache_path=cache_path)
    s = src.status(EventKind.LEGION, NOW)

    assert s.kind is EventKind.LEGION
    assert s.state is EventState.UPCOMING
    assert s.seconds_until_change == 8 * 60
    assert s.label_extra is None


@respx.mock
def test_does_not_refetch_within_refresh_interval(cache_path: Path):
    start = NOW + timedelta(minutes=10)
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json=_schedule_with_wb(start)))

    src = HelltidesSource(cache_path=cache_path)
    src.status(EventKind.WORLD_BOSS, NOW)
    src.status(EventKind.WORLD_BOSS, NOW + timedelta(seconds=30))
    src.status(EventKind.WORLD_BOSS, NOW + timedelta(minutes=2))

    assert route.call_count == 1


@respx.mock
def test_refetches_after_refresh_interval(cache_path: Path):
    start1 = NOW + timedelta(minutes=10)
    start2 = NOW + timedelta(minutes=15)
    route = respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(200, json=_schedule_with_wb(start1)),
            httpx.Response(200, json=_schedule_with_wb(start2, boss="Ashava")),
        ]
    )

    src = HelltidesSource(cache_path=cache_path)
    src.status(EventKind.WORLD_BOSS, NOW)
    later = NOW + timedelta(minutes=6)
    s = src.status(EventKind.WORLD_BOSS, later)

    assert route.call_count == 2
    assert "Ashava" in (s.label_extra or "")
