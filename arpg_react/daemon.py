from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arpg_react.alerts import (
    AlertDispatcher,
    AlertScheduler,
    NotifySendPlayer,
    PaplayAudioPlayer,
    Pyttsx3Player,
)
from arpg_react.config import (
    Config,
    HotkeyKind,
    default_builds_dir,
    detect_class_from_name,
    list_builds,
    load_build_v2,
    load_or_create_build_v2,
    save_config,
)
from arpg_react.context import (
    ContextDetector,
    GameContext,
    INPUT_SUPPRESSED,
    OverrideMode,
)
from arpg_react.editor_sync import password_from_env, sync_once
from arpg_react.hotkey import HotkeyController
from arpg_react.ipc import (
    BuildState,
    ContextFrame,
    DebugFrame,
    IPCServer,
    MonitoringStatus,
    SourceHealth,
    StatusFrame,
    alert_frame_to_dict,
    debug_frame_to_dict,
    status_frame_to_dict,
)
from arpg_react.ipc.messages import alert_frame_from_event
from arpg_react.rules import BuildV2
from arpg_react.sources import HelltidesSource, TimerSource
from arpg_react.timers import EventKind
from arpg_react.watchers import InputController
from arpg_react.watchers.detector import Detector, GameState as DetectorGameState
from arpg_react.watchers.rule_engine_v2 import RuleEngineV2

log = logging.getLogger(__name__)

TICK_SECONDS = 0.25
STATUS_BROADCAST_INTERVAL = 1.0


class _IPCLogHandler(logging.Handler):
    """Forwards `arpg_react.*` log records to connected panels as
    `debug` frames so the GUI can render them in its in-app console.

    Reentrance guard: ipc.publish() may itself log (e.g. when broadcast
    drops a dead client). Suppress emission while we're inside emit() to
    avoid feedback loops.
    """

    def __init__(self, ipc: IPCServer) -> None:
        super().__init__(level=logging.INFO)
        self._ipc = ipc
        import threading
        self._inflight = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._inflight, "active", False):
            return
        self._inflight.active = True
        try:
            frame = DebugFrame(
                ts=datetime.fromtimestamp(record.created, tz=timezone.utc),
                level=record.levelname,
                logger=record.name,
                msg=self.format(record),
            )
            self._ipc.publish(debug_frame_to_dict(frame))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._inflight.active = False


def run(
    config: Config,
    source: TimerSource,
    helltides_source: HelltidesSource | None = None,
    user_sounds_dir: Path | None = None,
    socket_path: Path | None = None,
    config_path: Path | None = None,
    builds_dir: Path | None = None,
) -> int:
    builds_dir = builds_dir or default_builds_dir()
    audio = PaplayAudioPlayer(master_volume=config.audio.master_volume)
    notify = NotifySendPlayer()
    tts = Pyttsx3Player(voice=config.audio.tts_voice, rate=config.audio.tts_rate)

    dispatcher = AlertDispatcher(
        audio=audio,
        notify=notify,
        tts=tts,
        events_config=config.events,
        user_sounds_dir=user_sounds_dir,
    )
    scheduler = AlertScheduler(events_config=config.events)
    input_controller = InputController()

    active_build: BuildV2 = load_or_create_build_v2(config.current_build, builds_dir)
    context_detector = ContextDetector(
        process_candidates=list(config.game.candidates),
    )
    context_detector.set_watchers(list(active_build.slot_monitors.values()))

    # New detector — single ImageGrab/tick covers slot states, HP/mana
    # orbs, boss bar, and mount UI. Replaces the legacy per-pixel sampler
    # in the engine and the saturation-scan in ContextDetector.
    detector = Detector()

    engine = RuleEngineV2(
        build=active_build,
        dispatcher=dispatcher,
        input_controller=input_controller,
    )
    engine.set_enabled(False)

    state: dict[str, Any] = {
        "engine": engine,
        "active_build": active_build,
        "events_paused": True,
        "context": GameContext.UNKNOWN,
        "override": OverrideMode.AUTO,
    }

    commands: queue.Queue[dict[str, Any]] = queue.Queue()

    def _on_command(msg: dict[str, Any]) -> None:
        commands.put(msg)

    ipc: IPCServer | None = None
    log_relay: _IPCLogHandler | None = None
    if socket_path is not None:
        ipc = IPCServer(socket_path, on_command=_on_command)
        ipc.start()
        # Stream INFO+ records from anywhere in `arpg_react.*` to
        # connected panels for the in-GUI debug console.
        log_relay = _IPCLogHandler(ipc)
        log_relay.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("arpg_react").addHandler(log_relay)

    def _hotkey_pressed() -> None:
        commands.put({"type": "command", "command": "toggle_watchers"})

    hotkey = HotkeyController(config.hotkey.toggle, _hotkey_pressed)
    if engine.has_active_rules():
        hotkey.start()

    # Background editor poll — pulls new/updated builds every N seconds.
    # No-op when D4_EDITOR_PASSWORD isn't set; the panel's SYNC button
    # always works on demand via the `sync_builds` IPC command.
    sync_stop = threading.Event()

    def _editor_sync_loop():
        if not password_from_env():
            log.info("editor_sync: D4_EDITOR_PASSWORD not set; auto-poll disabled")
            return
        log.info(
            "editor_sync: polling %s every %ds",
            config.editor_url,
            config.editor_sync_interval_seconds,
        )
        # Initial pull happens through the command queue so the build-reload
        # logic stays single-threaded (no race vs main loop).
        while not sync_stop.wait(0):
            commands.put({"type": "command", "command": "sync_builds"})
            if sync_stop.wait(config.editor_sync_interval_seconds):
                return

    sync_thread = threading.Thread(target=_editor_sync_loop, name="editor-sync", daemon=True)
    sync_thread.start()

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        log.info("received signal %s, shutting down", signum)
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "ARPG React daemon starting (source=%s, build=%s, slots=%d, rules=%d)",
        config.source,
        config.current_build,
        engine.watcher_count(),
        len(active_build.rules),
    )

    last_status_broadcast = 0.0

    def _switch_build(name: str) -> None:
        if name == config.current_build:
            return
        new_build = load_build_v2(name, builds_dir)
        if new_build is None:
            log.warning("switch_build: '%s' does not exist", name)
            return
        old_enabled = state["engine"].enabled
        config.current_build = name
        save_config(config, config_path)
        state["engine"].replace_build(new_build)
        state["engine"].set_enabled(old_enabled)
        state["active_build"] = new_build
        context_detector.set_watchers(list(new_build.slot_monitors.values()))
        log.info("switched build → %s (rules=%d)", name, len(new_build.rules))

    def _toggle_event_muted(kind_str: str) -> None:
        try:
            kind = EventKind(kind_str)
        except ValueError:
            log.warning("toggle_event_muted: bad kind %r", kind_str)
            return
        cfg = config.events.get(kind)
        if cfg is None:
            return
        cfg.muted = not cfg.muted
        log.info("event %s muted=%s", kind.value, cfg.muted)
        save_config(config, config_path)

    def _set_override(mode_str: str) -> None:
        try:
            mode = OverrideMode(mode_str)
        except ValueError:
            log.warning("set_override: bad mode %r", mode_str)
            return
        state["override"] = mode
        log.info("context override → %s", mode.value)

    def _process_command(msg: dict[str, Any]) -> None:
        cmd = msg.get("command")
        if cmd in ("toggle_watchers", "toggle_monitoring"):
            new_state = not state["engine"].enabled
            state["engine"].set_enabled(new_state)
            dispatcher.dispatch_hotkey_state(paused=not new_state)
        elif cmd == "toggle_events_paused":
            state["events_paused"] = not state["events_paused"]
            log.info("events_paused = %s", state["events_paused"])
        elif cmd == "toggle_event_muted":
            kind = msg.get("kind")
            if isinstance(kind, str):
                _toggle_event_muted(kind)
        elif cmd == "switch_build":
            target = msg.get("build")
            if isinstance(target, str) and target:
                _switch_build(target)
        elif cmd == "reload_active_build":
            # Pull latest build JSON from disk (e.g. after web editor save).
            latest = load_build_v2(config.current_build, builds_dir)
            if latest is not None:
                old_enabled = state["engine"].enabled
                state["engine"].replace_build(latest)
                state["engine"].set_enabled(old_enabled)
                state["active_build"] = latest
                context_detector.set_watchers(list(latest.slot_monitors.values()))
                log.info("reloaded build %s from disk", latest.name)
        elif cmd == "set_override":
            mode = msg.get("mode")
            if isinstance(mode, str):
                _set_override(mode)
        elif cmd == "cycle_override":
            order = [OverrideMode.AUTO, OverrideMode.ON, OverrideMode.OFF]
            cur = state["override"]
            try:
                idx = order.index(cur)
            except ValueError:
                idx = -1
            state["override"] = order[(idx + 1) % len(order)]
            log.info("override cycled → %s", state["override"].value)
        elif cmd == "sync_builds":
            # Pull fresh from the editor.
            changed = sync_once(config.editor_url, builds_dir)
            # Always re-check the active build on disk vs in-memory, even
            # when sync wrote nothing — the file may already match the
            # server (e.g. an earlier auto-poll round wrote it) but the
            # daemon's engine still holds the build from startup. Compare
            # by Pydantic-dumped JSON so we don't blow away rule runtime
            # state when nothing actually changed.
            latest = load_build_v2(config.current_build, builds_dir)
            current = state.get("active_build")
            if latest is not None and (
                current is None
                or latest.model_dump_json(exclude_none=True)
                   != current.model_dump_json(exclude_none=True)
            ):
                log.info(
                    "editor_sync: reloading active build %s (rules=%d)",
                    latest.name, len(latest.rules),
                )
                old_enabled = state["engine"].enabled
                state["engine"].replace_build(latest)
                state["engine"].set_enabled(old_enabled)
                state["active_build"] = latest
                context_detector.set_watchers(list(latest.slot_monitors.values()))
            elif changed:
                log.info("editor_sync: %d build(s) updated (active unchanged)", changed)
            else:
                log.info("editor_sync: no changes")
        else:
            log.debug("ignoring unknown command: %s", cmd)

    try:
        while not stop:
            now = datetime.now(timezone.utc)

            while True:
                try:
                    cmd_msg = commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    _process_command(cmd_msg)
                except Exception as exc:  # noqa: BLE001
                    log.warning("command handler raised: %s", exc)

            statuses = {}
            for kind in EventKind:
                try:
                    statuses[kind] = source.status(kind, now)
                except Exception as exc:  # noqa: BLE001
                    log.warning("source error for %s: %s", kind.value, exc)

            for alert in scheduler.tick(now, statuses):
                if not state["events_paused"]:
                    dispatcher.dispatch_event_alert(alert)
                    if ipc is not None:
                        ipc.publish(alert_frame_to_dict(alert_frame_from_event(alert)))

            # Detector — one screen grab per tick, populates slot states +
            # HP/mana fills + boss + mount-UI flag.
            engine_obj = state["engine"]
            try:
                reading = detector.detect()
            except Exception as exc:  # noqa: BLE001
                log.warning("detector tick failed: %s", exc)
                reading = None

            # Game-state gates auto-input. MENU and MOUNTED stop input
            # entirely; AUTO/ON/OFF override still applies.
            override = state["override"]
            if override is OverrideMode.OFF:
                ctx = GameContext.DISABLED
            elif override is OverrideMode.ON:
                ctx = GameContext.IN_COMBAT
            elif reading is None:
                ctx = GameContext.UNKNOWN
            elif reading.game_state in (
                DetectorGameState.MENU,
                DetectorGameState.MOUNTED,
                DetectorGameState.TOWN,
            ):
                ctx = GameContext.DISABLED
            else:
                ctx = GameContext.IN_COMBAT
            state["context"] = ctx
            state["last_reading"] = reading

            if reading is not None:
                engine_obj.apply_detector_reading(reading)
            engine_obj._input = None if ctx in INPUT_SUPPRESSED else input_controller  # noqa: SLF001
            engine_obj.tick(now)

            if ipc is not None and statuses:
                monotonic = time.monotonic()
                if monotonic - last_status_broadcast >= STATUS_BROADCAST_INTERVAL:
                    last_status_broadcast = monotonic
                    health = _build_health(config.source, helltides_source, now)
                    monitoring = MonitoringStatus(
                        enabled=state["engine"].enabled,
                        watcher_count=state["engine"].watcher_count(),
                    )
                    muted = [k.value for k, v in config.events.items() if v.muted]
                    active = state["active_build"]
                    class_name = active.class_name or detect_class_from_name(active.name)
                    build_state = BuildState(
                        current=config.current_build,
                        available=list_builds(builds_dir),
                        class_name=class_name,
                        build_url=active.build_url,
                    )
                    eng = state["engine"]
                    # Surface the detector's specific game state (combat /
                    # town / mounted / menu / unknown) instead of the
                    # collapsed input-gate state, so the panel can label it.
                    last_reading = state.get("last_reading")
                    detected_state = (
                        last_reading.game_state.value if last_reading is not None
                        else ctx.value
                    )
                    context_frame = ContextFrame(
                        context=detected_state,
                        override=state["override"].value,
                        resources={
                            name: round(v, 3) for name, v in eng.resource_fills.items()
                        },
                        slot_states={
                            hk.value: s.value for hk, s in eng.slot_states.items()
                        },
                    )
                    ipc.publish(
                        status_frame_to_dict(
                            StatusFrame(
                                now=now,
                                events=statuses,
                                source=health,
                                monitoring=monitoring,
                                events_paused=state["events_paused"],
                                slots=[],
                                muted_events=muted,
                                build=build_state,
                                context=context_frame,
                            )
                        )
                    )

            time.sleep(TICK_SECONDS)
    finally:
        sync_stop.set()
        hotkey.stop()
        if log_relay is not None:
            logging.getLogger("arpg_react").removeHandler(log_relay)
        if ipc is not None:
            ipc.stop()
        tts.close()

    return 0


def _build_health(
    source_name: str,
    helltides: HelltidesSource | None,
    now: datetime,
) -> SourceHealth:
    if helltides is None:
        return SourceHealth(name=source_name, primary_healthy=None, primary_fetched_at=None)
    return SourceHealth(
        name=source_name,
        primary_healthy=helltides.is_healthy(now),
        primary_fetched_at=helltides.fetched_at,
    )
