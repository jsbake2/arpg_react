from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from arpg_react.sources.base import SourceUnavailable
from arpg_react.timers.core import (
    EventKind,
    EventState,
    EventStatus,
    ceil_seconds,
    ensure_utc,
    state_for_active,
)

log = logging.getLogger(__name__)

API_URL = "https://helltides.com/api/schedule"
REFRESH_INTERVAL = timedelta(minutes=5)
STALE_THRESHOLD = timedelta(minutes=30)
FETCH_RETRY_INTERVAL = timedelta(seconds=30)
DEFAULT_TIMEOUT = 10.0

# helltides.com publishes these three. Realmwalker is not in the response and
# must be served by ClockSource.
SCHEDULE_KEY = {
    EventKind.HELLTIDE: "helltide",
    EventKind.LEGION: "legion",
    EventKind.WORLD_BOSS: "world_boss",
}

ACTIVE_DURATION = {
    EventKind.HELLTIDE: timedelta(minutes=55),
    EventKind.LEGION: timedelta(minutes=5),
    EventKind.WORLD_BOSS: timedelta(minutes=15),
}


@dataclass
class _Cache:
    fetched_at: datetime
    schedule: dict[str, Any]


class HelltidesSource:
    """Fetches the helltides.com public schedule and serves event status.

    Disk-cached at `cache_path`. Refreshed every REFRESH_INTERVAL. On HTTP failure
    the cache is reused up to STALE_THRESHOLD; beyond that, status() raises
    SourceUnavailable so the composite can fall back to clock math.

    Serves Helltide, Legion, and World Boss. Rejects Realmwalker (not published
    by helltides.com).
    """

    def __init__(
        self,
        cache_path: Path,
        client: httpx.Client | None = None,
    ) -> None:
        self.cache_path = cache_path
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self._cache: _Cache | None = None
        self._last_attempt: datetime | None = None
        self._load_disk_cache()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def fetched_at(self) -> datetime | None:
        return self._cache.fetched_at if self._cache else None

    def is_healthy(self, now: datetime) -> bool:
        return self._cache is not None and not self._is_stale(ensure_utc(now))

    def status(self, kind: EventKind, now: datetime) -> EventStatus:
        if kind not in SCHEDULE_KEY:
            raise SourceUnavailable(
                f"helltides.com does not publish {kind.value}"
            )
        now_utc = ensure_utc(now)
        self._refresh_if_due(now_utc)
        if self._cache is None or self._is_stale(now_utc):
            raise SourceUnavailable("helltides.com schedule unavailable or stale")

        entry = self._pick_entry(kind, now_utc)
        if entry is None:
            raise SourceUnavailable(
                f"no upcoming {kind.value} entry in cached schedule"
            )

        start = _parse_iso(entry["startTime"])
        active = ACTIVE_DURATION[kind]
        end = start + active
        label_extra = self._format_label(kind, entry)

        if now_utc < start:
            return EventStatus(
                kind=kind,
                state=EventState.UPCOMING,
                next_change=start,
                seconds_until_change=ceil_seconds(start - now_utc),
                label_extra=label_extra,
            )
        seconds = ceil_seconds(end - now_utc)
        return EventStatus(
            kind=kind,
            state=state_for_active(seconds),
            next_change=end,
            seconds_until_change=seconds,
            label_extra=label_extra,
        )

    def _refresh_if_due(self, now: datetime) -> None:
        if self._cache is not None:
            age = now - self._cache.fetched_at
            if age < REFRESH_INTERVAL:
                return
        if self._last_attempt is not None:
            since_attempt = now - self._last_attempt
            if since_attempt < FETCH_RETRY_INTERVAL:
                return
        self._last_attempt = now
        try:
            self._fetch(now)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            log.warning("helltides fetch failed: %s", exc)

    def _fetch(self, now: datetime) -> None:
        log.debug("fetching helltides schedule")
        response = self._client.get(API_URL)
        response.raise_for_status()
        schedule = response.json()
        if not isinstance(schedule, dict) or "world_boss" not in schedule:
            raise KeyError("response missing world_boss key")
        self._cache = _Cache(fetched_at=now, schedule=schedule)
        self._write_disk_cache()

    def _is_stale(self, now: datetime) -> bool:
        if self._cache is None:
            return True
        return (now - self._cache.fetched_at) > STALE_THRESHOLD

    def _pick_entry(self, kind: EventKind, now: datetime) -> dict[str, Any] | None:
        assert self._cache is not None
        key = SCHEDULE_KEY[kind]
        active = ACTIVE_DURATION[kind]
        entries = self._cache.schedule.get(key) or []
        annotated: list[tuple[datetime, dict[str, Any]]] = []
        for entry in entries:
            start_raw = entry.get("startTime")
            if not start_raw:
                continue
            try:
                start = _parse_iso(start_raw)
            except ValueError:
                continue
            annotated.append((start, entry))
        annotated.sort(key=lambda pair: pair[0])
        for start, entry in annotated:
            end = start + active
            if now < end:
                return entry
        return None

    def _format_label(self, kind: EventKind, entry: dict[str, Any]) -> str | None:
        if kind is not EventKind.WORLD_BOSS:
            return None
        boss = entry.get("boss")
        zones = entry.get("zone") or []
        zone_name = None
        if zones and isinstance(zones[0], dict):
            zone_name = zones[0].get("name")
        if boss and zone_name:
            return f"{boss} — {zone_name}"
        return boss or zone_name

    def _load_disk_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
            fetched_at = _parse_iso(data["fetched_at"])
            schedule = data["schedule"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            log.warning("ignoring corrupt helltides cache at %s: %s", self.cache_path, exc)
            return
        self._cache = _Cache(fetched_at=fetched_at, schedule=schedule)

    def _write_disk_cache(self) -> None:
        if self._cache is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": self._cache.fetched_at.isoformat(),
                "schedule": self._cache.schedule,
            }
            self.cache_path.write_text(json.dumps(payload))
        except OSError as exc:
            log.warning("could not write helltides cache to %s: %s", self.cache_path, exc)


def _parse_iso(value: str) -> datetime:
    raw = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
