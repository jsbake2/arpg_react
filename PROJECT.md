# ARPG React

> _(historical name: "Sanctum Signal" — kept as the Python package + CLI command for stability)_

A Diablo 4 + Path of Exile 2 companion daemon for solo PC players on Linux. Two read-only jobs, one process:

1. **Event timers** — countdowns + audio/notify alerts for Helltide, Legion, Realmwalker, and World Boss spawns.
2. **Pixel watchers** — sample one or more configured screen pixels, alert on color transitions (e.g. spell-ready glint, mana-low globe, cinder cap reached).

No screen-wide capture, no game-process reads, no input injection. The daemon runs in the background and surfaces information via `notify-send` desktop notifications, audio chimes, and optional TTS. There is no GUI window in v1 — only a transient setup overlay used to pick pixels.

This file is the design doc. It supersedes the earlier `PROJECT.md` (v1 design) and `d4-alert-project.md` (pixel watcher design) — those have been merged here.

---

## Why

Two real, recurring frictions while playing solo on PC:

- **Missing scheduled events.** Helltide is top-of-hour, Legion every 25min, Realmwalker every 15min, World Boss roughly every 3.5h. Without a tracker open you forget, alt-tab to check, miss the buff. A passive audio cue with no UI to look at is the right shape for this.
- **Missing in-game state transitions.** A glint appears on a cooldown skill, a globe drops below a threshold, a UI element flashes. Easy to miss in a busy scene. Watching a single pixel for a known good→bad transition catches these without needing image recognition or AI.

Both are **read-only and non-interactive** — no game memory, no input, no screen-OCR. This keeps the tool firmly in "accessibility daemon" territory.

---

## Goals & non-goals

### Goals
- Pure Python, runs as a single `python -m arpg_react` daemon on CachyOS / Wayland (COSMIC DE).
- Event timer alerts driven by clock math (Helltide / Legion / Realmwalker) + a public API (helltides.com) for World Boss spawns, with disk-cached fallback to clock approximation when offline.
- Per-event configurable lead times (e.g. "warn at 5min and 30sec before Helltide"), per-event mute, idempotent firing (no double-alerts inside one cycle).
- Pixel watcher with a crosshair-overlay setup mode for picking the pixel + sampling good/bad colors. Multiple watchers supported (the original `d4-alert` was single-pixel only — generalized here).
- Global hotkey to pause/resume monitoring (default F9), with audibly distinct toggle sounds — designed to be bound to a side-mouse-button.
- Persistent config in `~/.config/arpg_react/config.json`, hot-reload not required.
- Graceful degradation: API down → clock fallback. Audio backend missing → notify-send only. Hotkey unsupported under pure Wayland → log warning, monitoring stays always-on.

### Non-goals
- Screen-region OCR, multi-pixel image matching, or any vision-model use. Single-pixel-color watchers only.
- Reading game memory or process state.
- Sending input to the game.
- Always-on-top widget / floating window. Wayland always-on-top is unreliable on COSMIC and the audio+notify path is enough. (Reserved for a future tier — see below.)
- Windows-first packaging. The daemon should remain portable in principle, but the v1 environment is Linux/Wayland and we don't ship Windows installers.
- Loot filter logic, build management, route overlays. (Future tiers.)

---

## Tech stack

- **Python 3.11+**
- **httpx** — async HTTP client for the helltides.com source.
- **pydantic v2** — config schema validation.
- **Pillow** — `ImageGrab` for pixel sampling under XWayland.
- **pynput** — global hotkey listener.
- **tkinter** (system Python) — fullscreen transparent crosshair window for setup mode.
- **pyttsx3** — offline TTS (espeak-ng backend on Linux). Optional per-event.
- **System tools** — `paplay` (PipeWire) for audio, `notify-send` for desktop notifications. No Python audio lib in v1; subprocess is fine.

No PySide6, no Qt, no compiled deps beyond what `pip install` and pacman already provide. Should run from a venv with `pip install -r requirements.txt` plus `pacman -S libnotify` (already present on most CachyOS installs).

---

## Architecture

Five layers. Each layer is testable in isolation.

```
┌──────────────────────────────────────────────────────────────┐
│ daemon.py — main loop, hotkey toggle, signal handling        │
└──────────────┬─────────────────────────────┬─────────────────┘
               │                             │
       ┌───────▼───────┐             ┌───────▼────────┐
       │ timers tier   │             │ watchers tier  │
       │ (event-cycle) │             │ (pixel-poll)   │
       └───────┬───────┘             └───────┬────────┘
               │                             │
       ┌───────▼─────────────────────────────▼──────┐
       │ alerts/ — unified dispatch                 │
       │ (audio | notify | tts) + idempotency       │
       └────────────────────────────────────────────┘
```

### `timers/` — pure logic

No I/O. Functions take `now: datetime` (UTC) and return `EventStatus`. This is the layer that gets unit-tested with frozen time.

- `EventKind` — `HELLTIDE | LEGION | REALMWALKER | WORLD_BOSS`
- `EventState` — `UPCOMING | ACTIVE | ENDING_SOON`
- `EventStatus` — frozen dataclass: `kind`, `state`, `next_change` (aware UTC), `seconds_until_change`, optional `label_extra` (used by helltides source to attach boss name + zone).

Per-event modules:
- `helltide.py` — top-of-hour, 55min active, 5min dead.
- `legion.py` — 25min cadence from a fixed UTC anchor.
- `realmwalker.py` — 15min cadence.
- `world_boss.py` — clock-math fallback only (~3.5h cadence). The accurate path is the helltides source; this exists for offline degradation.

Edge cases that **must** be tested:
- Helltide at `:55:00` (transition active → upcoming-dead-window).
- Helltide at `:59:59.999` (still upcoming, 1ms to next).
- Helltide at `:00:00` (exactly active, fresh cycle).
- Legion at the 5-min active-window boundary.
- DST transitions — math is UTC-internal; display is local.
- Sub-second drift — `seconds_until_change` rounds consistently (no flicker between `1m 0s` and `59s`).

### `sources/` — pluggable timer backends

The daemon never calls `timers.helltide_status` directly. It consumes a `TimerSource` protocol so the API path and the clock-math path can be swapped without touching the daemon.

- `TimerSource` — `def status(kind: EventKind, now: datetime) -> EventStatus`. Sync; the daemon polls at 250ms and async adds no value here.
- `ClockSource` — wraps the `timers/` functions. Accepts an `anchors: dict[EventKind, datetime]` for per-kind anchor overrides (used for Realmwalker calibration and as offline fallback for Legion / World Boss).
- `HelltidesSource` — fetches `https://helltides.com/api/schedule` (verified 2026-05: returns `{world_boss, legion, helltide}` with ISO 8601 `startTime`; world_boss entries also carry `boss` + `zone[]`). Disk-cached at `~/.cache/arpg_react/helltides.json` with a 5-min refresh interval and 30-min stale threshold. On HTTP failure or stale cache, raises `SourceUnavailable`. Serves Helltide / Legion / World Boss; rejects Realmwalker (not in the feed).
- `CompositeSource` — `HELLTIDE | LEGION | WORLD_BOSS → HelltidesSource → ClockSource fallback`. `REALMWALKER → ClockSource only`.

The composite logs whenever it falls back so the daemon can mark the event row as `(approximate)`. Helltide cadence is deterministic (top-of-hour) so its clock-math fallback remains exact even when the API is down; Legion fallback is approximate (anchor drift across patches).

### `watchers/` — pixel polling

Generalization of the original `d4-alert` design. Each watcher is independent and runs in the same poll loop (250ms tick).

- `PixelWatcher` — config: `name, x, y, good_color, bad_color, tolerance, cooldown_seconds`. State: `last_state ∈ {good, bad}`, `last_alert_at`. Fires alert on `good → bad` transition, then waits for `bad → good` reset before re-arming. Cooldown enforced even on rapid flicker.
- `setup.py` — `--setup <name>` opens a tkinter fullscreen transparent overlay (XWayland), draws crosshair at live cursor position, click-to-sample bad color, click-again-to-sample good color, saves to config.
- `polling.py` — runs all configured watchers in one loop. Pixel sampling via `Pillow.ImageGrab.grab(bbox=(x,y,x+1,y+1))` — 1×1 grab is cheap. Color match is Euclidean RGB distance ≤ tolerance.

### `alerts/` — unified dispatch

Both timer events and watcher events go through the same dispatcher. This is the single integration point with the OS.

- `AudioPlayer` — backend = `subprocess.Popen(["paplay", "--volume=...", path])`. Default sounds shipped in `resources/sounds/`. Fallback chain on missing config: `freedesktop bell.oga → complete.oga → terminal bell`.
- `NotifyPlayer` — `subprocess.run(["notify-send", title, body, "--urgency=...", "--expire-time=..."])`.
- `TTSPlayer` — pyttsx3 on a worker thread (espeak-ng is blocking). Optional per-event.
- `AlertScheduler` — given a stream of `EventStatus` snapshots and per-event `AlertConfig`, emits `AlertEvent`s at the configured lead times. Tracks `(EventKind, cycle_anchor) → set of fired lead-times` so a 5-min warning fires exactly once per cycle even though the loop ticks at 250ms.

### `daemon.py` — main loop

- argparse: `--run | --setup <watcher_name> | --once` (one-shot status print, useful for debugging).
- 250ms tick: ask each `TimerSource` for current status, run all `PixelWatcher`s once, feed both result streams into `AlertScheduler`.
- `pynput.keyboard.GlobalHotKeys` thread for the toggle hotkey. On Wayland-without-XWayland, log a warning and run always-on.
- Signal handlers: `SIGTERM`/`SIGINT` → flush, exit.
- No background threads beyond hotkey listener + TTS worker. Async only inside `HelltidesSource` (run via `asyncio.run` inside the tick if/when refresh is due — keeps the main loop synchronous and predictable).

---

## Project layout

```
d4-automation/
├── PROJECT.md                  # this file
├── README.md                   # user-facing install + usage (written at M3)
├── pyproject.toml
├── arpg_react/
│   ├── __init__.py
│   ├── __main__.py             # entry: --run | --setup | --once
│   ├── daemon.py               # main loop
│   ├── config.py               # pydantic models, load/save
│   ├── timers/
│   │   ├── __init__.py
│   │   ├── core.py
│   │   ├── helltide.py
│   │   ├── legion.py
│   │   ├── realmwalker.py
│   │   └── world_boss.py
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py             # TimerSource protocol
│   │   ├── clock.py
│   │   ├── helltides.py        # API client + disk cache
│   │   └── composite.py
│   ├── watchers/
│   │   ├── __init__.py
│   │   ├── pixel.py
│   │   ├── polling.py
│   │   └── setup.py            # tkinter crosshair overlay
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── audio.py
│   │   ├── notify.py
│   │   ├── tts.py
│   │   └── scheduler.py
│   ├── hotkey.py
│   └── resources/
│       └── sounds/
│           ├── warning.wav
│           ├── start.wav
│           ├── end.wav
│           ├── pixel_alert.wav
│           ├── pause.wav
│           └── resume.wav
└── tests/
    ├── test_helltide.py
    ├── test_legion.py
    ├── test_realmwalker.py
    ├── test_world_boss.py
    ├── test_helltides_source.py
    ├── test_composite_source.py
    ├── test_pixel_watcher.py
    ├── test_scheduler.py
    └── test_config.py
```

---

## Config schema

`~/.config/arpg_react/config.json`. Pydantic-validated on load; missing optional fields default; unknown fields warn-and-ignore.

```json
{
  "version": 1,
  "events": {
    "helltide": {
      "muted": false,
      "warn_at_seconds": [300, 30],
      "tts_enabled": true,
      "chime_enabled": true
    },
    "legion": {
      "muted": false,
      "warn_at_seconds": [60],
      "tts_enabled": false,
      "chime_enabled": true
    },
    "realmwalker": {
      "muted": true,
      "warn_at_seconds": [30],
      "tts_enabled": false,
      "chime_enabled": true
    },
    "world_boss": {
      "muted": false,
      "warn_at_seconds": [600, 60],
      "tts_enabled": true,
      "chime_enabled": true
    }
  },
  "watchers": [
    {
      "name": "spell_ready",
      "pixel_x": 942,
      "pixel_y": 817,
      "bad_color": [255, 40, 40],
      "good_color": [80, 80, 80],
      "color_tolerance": 20,
      "cooldown_seconds": 10,
      "enabled": true
    }
  ],
  "audio": {
    "device": null,
    "master_volume": 0.7,
    "tts_voice": null,
    "tts_rate": 180
  },
  "hotkey": {
    "toggle": "f9"
  },
  "source": "composite"
}
```

`source` values: `"clock"` (no network), `"composite"` (helltides for World Boss, clock for the rest — default), `"helltides"` (API for everything that helltides publishes — discouraged but available).

---

## Milestones

### M1 — Timer core + helltides source ← currently here
- `timers/` with all four event functions, frozen-time tested.
- `sources/` with `ClockSource`, `HelltidesSource`, `CompositeSource`, plus tests for the cache + fallback paths.
- `config.py` with pydantic models.
- `python -m arpg_react --once` prints current statuses against the configured source.
- **Done when:** `pytest -q` is green, `--once` output matches wall-clock reality, World Boss row shows boss name + zone from helltides.

### M2 — Alert dispatch
- `alerts/` package: AudioPlayer, NotifyPlayer, TTSPlayer, AlertScheduler.
- Default sound assets in `resources/sounds/` (CC0).
- Idempotency tested with a mocked clock + fake players — running a 1h synthetic timeline produces exactly the expected alert count, no duplicates.
- **Done when:** `--run` (timers only, no watchers yet) reliably alerts on a real Helltide cycle.

### M3 — Pixel watchers
- `watchers/pixel.py` polling logic + `--setup` crosshair overlay.
- Multi-watcher support (config supports a list, daemon runs them all).
- Hotkey toggle + distinct pause/resume sounds.
- Wayland-without-XWayland degradation path tested.
- **Done when:** running the daemon during a real game session catches a configured pixel transition without false positives over 30+ minutes.

### M4 — Polish
- README with install + usage + troubleshooting (Wayland section).
- Single-instance lock (PID file in `~/.cache/arpg_react/`).
- Custom sound import (drop a wav into `~/.config/arpg_react/sounds/`).
- Optional systemd-user unit for autostart.
- **Done when:** install instructions are followed by a fresh CachyOS user (or VM) and the daemon runs without manual debugging.

---

## Design decisions worth flagging

**Why helltides.com is primary for everything it publishes.** The single helltides.com response carries Helltide, Legion, and World Boss timings. We pay for the HTTP call regardless of how many fields we read, so reading all three avoids any anchor-calibration drift on Legion (which historically shifts across patches) and gives us the boss name + zone label for World Boss. Clock math remains the offline fallback. Realmwalker is the only event we serve from clock math by default — helltides doesn't publish it, so its anchor is config-overridable.

**Why disk-cache the helltides response.** Two reasons. First, a slow or dropped network shouldn't make the daemon unresponsive. Second, the schedule is forward-looking — one fetch buys ~14h of accurate timings, so even if the API is down for an hour we're fine.

**Why no GUI in v1.** Wayland always-on-top is unreliable on COSMIC. A frameless PySide6 window is an ergonomic answer to a problem (passive awareness) that audio + `notify-send` already solves on this stack. The previous design assumed Windows; this one matches the actual target. Reserved as a future tier — not painted out by the architecture.

**Why store time in UTC internally.** DST in Colorado will absolutely cause a "Helltide started an hour ago" bug if we're sloppy. UTC math, local conversion only at display.

**Why keep the watcher single-pixel-only.** Multi-pixel and image-match are a different category of risk and complexity (false positives, calibration headaches, OCR libs). Single pixel + Euclidean color distance is the smallest thing that catches the actual use case (spell glints, mana threshold) without sliding toward "vision-based loot assistant" territory.

**Why `subprocess` for audio instead of pygame/playsound.** One fewer dependency and `paplay` is already on every PipeWire system. The latency is fine (humans don't notice 50ms in this context). Cross-platform is a non-goal in v1.

---

## Wayland / COSMIC constraints

| Issue | Mitigation |
|-------|-----------|
| `pynput` global hotkeys may fail on pure Wayland | Try at startup; on failure, log warning and disable toggle (monitoring runs always-on). Document XWayland fallback in README. |
| Screen capture requires XWayland | `Pillow.ImageGrab` works under XWayland with `DISPLAY=:0`. Daemon detects and warns if capture fails. |
| Always-on-top windows unreliable | Out of scope — daemon is headless. |
| System cursor change unreliable | Setup overlay draws its own crosshair; doesn't touch system cursor. |

---

## Future tiers (designed-for, not built)

- **GUI tier** — PySide6 always-on-top widget showing the four event rows. Optional, not required. The `TimerSource` protocol means this drops in without touching the timer or alert layers.
- **Watcher tier 2** — region OCR for cinder count / inventory fill. Adds `mss` dependency. Calibration UI for ROI per resolution.
- **Hardware integrations** — MQTT publisher for Home Assistant, Discord webhooks, OBS websocket for replay-buffer triggers.
- **Helltide route overlay** — transparent click-through window with rotating Mystery / Living Steel chest locations. Data scraped or shipped as bundled JSON.
- **Loot decision assistant** — vision-based, advisory only. Different scope, different risk profile. Probably a separate project, referenced here only so we don't accidentally architect against it.

---

## Open questions

1. **TTS voice on espeak-ng.** Default voice is rough. Worth shipping a tested-voice list, or just let users configure?
2. **License.** MIT vs Apache 2 vs private. The D4 companion ecosystem is open-source-friendly (helltides, maxroll-tools, d4lf all are). Defaulting to MIT unless told otherwise.
3. **Hotkey while in fullscreen game.** D4 in exclusive-fullscreen may swallow `pynput` hotkeys even via XWayland. Fallback plan: bind via the desktop environment's hotkey manager and have it run `python -m arpg_react --toggle` (writes to a unix socket / signals the daemon). Decide at M3.

---

## Reference

- `helltides.com/api/schedule` — verified 2026-05, returns `{world_boss, legion, helltide}` with ISO 8601 `startTime`, boss name, zone array. Cloudflare-fronted; no documented rate-limit but 5-min polling is well within reason.
- `d4lfteam/d4lf` — Python loot filter using OpenCV + screen capture. Reference for ROI calibration patterns when watcher tier 2 lands. **Not a dependency.**
- `pgrimaud/lametric-diablo4` (GitHub) — public example of polling helltides.com from a small daemon. Useful prior art.
- Wowhead D4 event-timer page — sanity-check cadences after each season patch.
