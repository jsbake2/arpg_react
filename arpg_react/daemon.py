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
    DEFAULT_KEYMAP_BY_GAME,
    DEFAULT_MODIFIERS_BY_GAME,
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
from arpg_react.editor_sync import (
    load_cached_profile,
    password_from_env,
    sync_once,
    sync_profile,
)
from arpg_react.hotkey import HotkeyController
from arpg_react.trigger_hotkey import TriggerHotkeyListener
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
from arpg_react.rules import BuildV2, ConditionType
from arpg_react.sources import HelltidesSource, TimerSource
from arpg_react.timers import EventKind
from arpg_react.watchers import InputController
from arpg_react.watchers.detector import (
    DETECTOR_DEFAULTS,
    Detector,
    GameState as DetectorGameState,
    scale_detector_for,
)
from arpg_react.watchers.movement_monitor import MovementMonitor
from arpg_react.watchers.rule_engine_v2 import RuleEngineV2

log = logging.getLogger(__name__)

TICK_SECONDS = 0.25
STATUS_BROADCAST_INTERVAL = 1.0


def _collect_trigger_tokens(build: BuildV2) -> set[str]:
    """Walk a build's rules + combo-step conditions for every
    HOTKEY_PRESSED token, lowercased. The TriggerHotkeyListener gets
    this union so it only installs OS-level hooks for keys the build
    actually cares about."""
    tokens: set[str] = set()
    for rule in build.rules:
        if not rule.enabled:
            continue
        for cond in rule.conditions:
            if cond.type is ConditionType.HOTKEY_PRESSED and cond.hotkey_token:
                tokens.add(cond.hotkey_token.strip().lower())
        for step in rule.combo_steps:
            for cond in step.conditions:
                if cond.type is ConditionType.HOTKEY_PRESSED and cond.hotkey_token:
                    tokens.add(cond.hotkey_token.strip().lower())
    return tokens


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
    game: str = "d4",
) -> int:
    # Per-game scoping: each game has its own subdir under
    # `~/.config/arpg_react/builds/`. The one-shot migration moves any
    # flat pre-existing `builds/*.json` into `builds/d4/` so existing
    # users don't lose D4 builds when this code first runs.
    from arpg_react.config import _migrate_legacy_builds_root
    _migrate_legacy_builds_root()
    builds_dir = builds_dir or default_builds_dir(game)
    builds_dir.mkdir(parents=True, exist_ok=True)
    # Stash the game id early so anything constructed in this function
    # body (dispatcher, watchers, IPC bits) can reference it. The block
    # below that does keymap install used to be the first place it was
    # bound, but the dispatcher needs it ~30 lines earlier now for the
    # per-game notification title prefix.
    _daemon_game = game
    audio = PaplayAudioPlayer(master_volume=config.audio.master_volume)
    notify = NotifySendPlayer()
    tts = Pyttsx3Player(voice=config.audio.tts_voice, rate=config.audio.tts_rate)

    dispatcher = AlertDispatcher(
        audio=audio,
        notify=notify,
        tts=tts,
        events_config=config.events,
        user_sounds_dir=user_sounds_dir,
        mutes=config.mutes,
        game=_daemon_game,
    )
    scheduler = AlertScheduler(events_config=config.events)
    input_controller = InputController()
    movement_monitor = MovementMonitor()
    movement_monitor.start()

    # Marker file so the launcher can tell what game this daemon was
    # spawned for — used to detect a mismatch when the user picks a
    # different game in the launch dialog than the running daemon
    # serves. Written next to the socket so it shares the same lifecycle.
    if socket_path is not None:
        try:
            (socket_path.parent / "game").write_text(game)
        except OSError as exc:  # noqa: BLE001
            log.warning("daemon: failed to write game marker: %s", exc)

    # Install the per-game default keymap first so even slots without an
    # entry on the user's profile (or with no profile at all) press the
    # right thing — e.g. D4 "L" → mouse-left, POE2 "Q" → keyboard 'q'.
    # The user's editor profile then overrides any slots they've remapped.
    keymap = dict(DEFAULT_KEYMAP_BY_GAME.get(_daemon_game, {}))
    cached_profile = load_cached_profile(_daemon_game)
    if cached_profile and cached_profile.get("keymap"):
        keymap.update(cached_profile["keymap"])
    input_controller.set_keymap(keymap)
    # Per-game press modifiers (e.g. D3 holds Shift while clicking LMB
    # so the character attacks in place instead of running to the cursor).
    input_controller.set_modifiers(
        DEFAULT_MODIFIERS_BY_GAME.get(_daemon_game, {})
    )

    # Resolve the active build for THIS game. Prefer the per-game name;
    # if missing, fall back to the first build that actually exists in
    # this game's dir; if the dir is empty, create a "placeholder" build
    # rather than reusing another game's name (that's how D4's
    # "InfiniteBalls" was leaking into D3 as an empty stub).
    requested_name = config.active_build_for(_daemon_game)
    existing = list_builds(builds_dir)
    if requested_name and requested_name in existing:
        active_name = requested_name
    elif existing:
        active_name = existing[0]
        log.info(
            "daemon[%s]: no current_build set (or %r missing); using %r",
            _daemon_game, requested_name, active_name,
        )
    else:
        active_name = "placeholder"
        log.info("daemon[%s]: no builds on disk — seeding %r", _daemon_game, active_name)
    if config.active_build_for(_daemon_game) != active_name:
        config.set_active_build_for(_daemon_game, active_name)
        from arpg_react.config import save_config
        save_config(config, config_path)
    active_build: BuildV2 = load_or_create_build_v2(active_name, builds_dir)
    context_detector = ContextDetector(
        process_candidates=list(config.game.candidates),
    )
    context_detector.set_watchers(list(active_build.slot_monitors.values()))

    # New detector — single ImageGrab/tick covers slot states, HP/mana
    # orbs, boss bar, and mount UI. Replaces the legacy per-pixel sampler
    # in the engine and the saturation-scan in ContextDetector. Coords
    # auto-scale to the user's resolution + UI scale via the cached
    # profile; fall back to the 2560×1440 100% reference defaults when
    # no profile is cached (matches jbaker's setup).
    display = (cached_profile or {}).get("display") or {}
    sw = int(display.get("screen_w") or 2560)
    sh = int(display.get("screen_h") or 1440)
    ui = float(display.get("ui_scale") or 1.0)
    detector_cfg = (
        DETECTOR_DEFAULTS
        if (sw, sh, ui) == (2560, 1440, 1.0)
        else scale_detector_for(sw, sh, ui)
    )
    # Boss/mount template patches were captured at the same reference
    # resolution; resize them so the per-pixel match still lands.
    from arpg_react.watchers.detector_refs import BOSS_REF, MOUNT_REF, scale_template
    boss_ref_scaled = scale_template(BOSS_REF, sw, sh, ui)
    mount_ref_scaled = scale_template(MOUNT_REF, sw, sh, ui)
    log.info(
        "detector: %s coords for %dx%d @ %.2fx UI scale",
        "reference" if detector_cfg is DETECTOR_DEFAULTS else "scaled",
        sw, sh, ui,
    )
    detector = Detector(
        config=detector_cfg,
        boss_ref=boss_ref_scaled,
        mount_ref=mount_ref_scaled,
    )

    # D3 has no per-slot UI calibration yet — the D4 detector's slot/orb
    # coordinates don't match D3's hotbar. Use a lightweight D3-specific
    # detector that only resolves the pause states (ESC menu, chat,
    # skill/paragon/achievements panels). Rules in D3 fire without
    # slot-state / HP / resource gating until proper D3 detector refs land.
    d3_state_detector = None
    if game == "d3":
        from arpg_react.watchers.d3_state import D3StateDetector
        d3_state_detector = D3StateDetector(screen_w=sw, screen_h=sh)
        log.info("d3 state detector active (%dx%d)", sw, sh)

    engine = RuleEngineV2(
        build=active_build,
        dispatcher=dispatcher,
        input_controller=input_controller,
        movement_monitor=movement_monitor.is_moving,
    )
    engine.set_enabled(False)

    # Global key listener for HOTKEY_PRESSED conditions ("press F8 to
    # fire boss opener"). Reconfigured on every build switch so a
    # build's set of trigger keys is what's hooked at any moment.
    trigger_listener = TriggerHotkeyListener()
    trigger_listener.configure(_collect_trigger_tokens(active_build))

    state: dict[str, Any] = {
        "engine": engine,
        "active_build": active_build,
        "events_paused": True,
        "context": GameContext.UNKNOWN,
        "override": OverrideMode.AUTO,
        # Edge-tracker for chat-open detection — alarm fires once on the
        # rising edge, suppression lifts automatically on the falling edge.
        "chat_open": False,
    }

    # Buff watcher — D3 (CoE) and POE2 (Savage Fury). For D3 we reuse the
    # state-detector's full-screen grab so no second ImageGrab fires per
    # tick. POE2 has no full-screen state detector, so the watcher grabs
    # its own buff-strip region (small, cheap — ~25 ms on Wayland via grim
    # at the strip's ~2250×108 size). Search bbox comes from per-game
    # config; if the game has no bbox configured the watcher stays None
    # and the BUFFS tab is effectively a no-op for that game.
    buff_watcher = None
    buff_strip_grab_bbox: tuple[int, int, int, int] | None = None
    if game in ("d3", "poe2"):
        from arpg_react.config import DEFAULT_BUFF_ROW_BBOX_BY_GAME
        from arpg_react.watchers.buff_watcher import BuffWatcher
        bbox = DEFAULT_BUFF_ROW_BBOX_BY_GAME.get(game)
        if bbox is not None:
            scaled_bbox = (
                int(round(bbox[0] * sw / 2560)),
                int(round(bbox[1] * sh / 1440)),
                int(round(bbox[2] * sw / 2560)),
                int(round(bbox[3] * sh / 1440)),
            )

            buff_watcher = BuffWatcher(
                search_bbox=scaled_bbox,
                on_buff_seen=dispatcher.dispatch_buff_seen,
            )
            buff_watcher.set_buffs(active_build.buffs)
            log.info(
                "buff watcher active (%s, %d buffs, bbox=%s)",
                game, len(active_build.buffs), scaled_bbox,
            )
            if game == "poe2":
                # POE2 needs its own grab — see comment block above.
                buff_strip_grab_bbox = scaled_bbox

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
    # Always start the listener at boot regardless of whether the active
    # build has rules. Gating on has_active_rules() meant: launch with a
    # placeholder build → hotkey never registers; later add rules via the
    # editor → hotkey still dead because nothing re-armed it. The toggle
    # is a no-op on a ruleless build (engine has nothing to fire), so
    # there's no harm in always listening.
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
        "ARPG React daemon starting (game=%s, source=%s, build=%s, slots=%d, rules=%d)",
        _daemon_game,
        config.source,
        active_name,
        engine.watcher_count(),
        len(active_build.rules),
    )

    last_status_broadcast = 0.0

    def _switch_build(name: str) -> None:
        if name == config.active_build_for(_daemon_game):
            return
        new_build = load_build_v2(name, builds_dir)
        if new_build is None:
            log.warning("switch_build: '%s' does not exist for game=%s", name, _daemon_game)
            return
        old_enabled = state["engine"].enabled
        config.set_active_build_for(_daemon_game, name)
        save_config(config, config_path)
        state["engine"].replace_build(new_build)
        state["engine"].set_enabled(old_enabled)
        state["active_build"] = new_build
        context_detector.set_watchers(list(new_build.slot_monitors.values()))
        if buff_watcher is not None:
            buff_watcher.set_buffs(new_build.buffs)
        trigger_listener.configure(_collect_trigger_tokens(new_build))
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
            latest = load_build_v2(
                config.active_build_for(_daemon_game) or active_name, builds_dir
            )
            if latest is not None:
                old_enabled = state["engine"].enabled
                state["engine"].replace_build(latest)
                state["engine"].set_enabled(old_enabled)
                state["active_build"] = latest
                context_detector.set_watchers(list(latest.slot_monitors.values()))
                if buff_watcher is not None:
                    buff_watcher.set_buffs(latest.buffs)
                trigger_listener.configure(_collect_trigger_tokens(latest))
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
            # Pull fresh from the editor — builds first, then profile.
            # `game` is mandatory so we only pull this daemon's game and
            # don't stomp the other game's local files.
            changed = sync_once(config.editor_url, builds_dir, _daemon_game)
            new_profile = sync_profile(config.editor_url, _daemon_game)
            if new_profile and new_profile.get("keymap"):
                merged = dict(DEFAULT_KEYMAP_BY_GAME.get(_daemon_game, {}))
                merged.update(new_profile["keymap"])
                input_controller.set_keymap(merged)
            # Always re-check the active build on disk vs in-memory, even
            # when sync wrote nothing — the file may already match the
            # server (e.g. an earlier auto-poll round wrote it) but the
            # daemon's engine still holds the build from startup. Compare
            # by Pydantic-dumped JSON so we don't blow away rule runtime
            # state when nothing actually changed.
            latest = load_build_v2(
                config.active_build_for(_daemon_game) or active_name, builds_dir
            )
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
                if buff_watcher is not None:
                    buff_watcher.set_buffs(latest.buffs)
                trigger_listener.configure(_collect_trigger_tokens(latest))
            elif changed:
                log.info("editor_sync: %d build(s) updated (active unchanged)", changed)
            else:
                log.info("editor_sync: no changes")
        elif cmd == "set_mutes":
            # Panel SETTINGS tab → flip per-alert-kind audio mutes.
            # Payload carries every key the panel currently knows about;
            # we trust the panel and don't merge — a missing key means
            # "panel doesn't have a toggle for this", which only matters
            # if we add a fourth mute later. Then we'd want a partial
            # merge instead. Cheap to revisit then.
            from arpg_react.config import MuteConfig
            new_mutes = MuteConfig(
                rule_alerts=bool(msg.get("rule_alerts", config.mutes.rule_alerts)),
                buff_alerts=bool(msg.get("buff_alerts", config.mutes.buff_alerts)),
                chat_alarm=bool(msg.get("chat_alarm", config.mutes.chat_alarm)),
            )
            config.mutes = new_mutes
            dispatcher.set_mutes(new_mutes)
            save_config(config, config_path)
            log.info(
                "mutes updated: rule=%s buff=%s chat=%s",
                new_mutes.rule_alerts, new_mutes.buff_alerts, new_mutes.chat_alarm,
            )
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
            # D3 uses a separate lightweight pause-state detector instead
            # of the D4 detector (D4's slot/orb coordinates don't apply).
            engine_obj = state["engine"]
            reading = None
            d3_reading = None
            if d3_state_detector is not None:
                try:
                    d3_reading = d3_state_detector.detect(now)
                except Exception as exc:  # noqa: BLE001
                    log.warning("d3 state detector tick failed: %s", exc)
            else:
                try:
                    reading = detector.detect()
                except Exception as exc:  # noqa: BLE001
                    log.warning("detector tick failed: %s", exc)
                    reading = None

            # Chat-open gate — independent of game_state, overrides everything.
            # When the in-game chat input is accepting text, our hotkey
            # presses would be typed into chat. Suppress input regardless
            # of override and fire a one-shot alarm on the rising edge.
            # Auto-resumes (no manual re-enable) when chat closes.
            if d3_reading is not None:
                chat_now = d3_reading.reason == "chat_open"
            else:
                chat_now = bool(reading is not None and reading.chat_open)
            chat_prev = state["chat_open"]
            if chat_now and not chat_prev:
                log.warning("chat input detected — auto-cast paused until chat closes")
                dispatcher.dispatch_chat_open_alarm()
            elif chat_prev and not chat_now:
                log.info("chat closed — auto-cast resumed")
            state["chat_open"] = chat_now

            # Game-state gates auto-input. MENU and MOUNTED stop input
            # entirely; AUTO/ON/OFF override still applies. Chat-open
            # takes precedence over all of them.
            override = state["override"]
            if override is OverrideMode.OFF:
                ctx = GameContext.DISABLED
            elif override is OverrideMode.ON:
                ctx = GameContext.IN_COMBAT
            elif d3_reading is not None:
                # D3: any non-combat pause reason → DISABLED. The chat
                # gate above already handled its rising/falling edge log.
                ctx = (
                    GameContext.DISABLED if d3_reading.is_paused
                    else GameContext.IN_COMBAT
                )
            elif chat_now:
                ctx = GameContext.DISABLED
            elif reading is None:
                ctx = GameContext.UNKNOWN
            elif game == "poe2":
                # POE2 uses the D4-style Detector but its slot/orb sample
                # coords are calibrated for D4 — POE2's HP orb sits at
                # different pixels, so the detector reads hp_fill=0 and
                # classifies every POE2 scene as `game_state = MENU`,
                # which would route every rule's press through the
                # INPUT_SUPPRESSED branch and silently swallow it. The
                # only detector output we can trust for POE2 is
                # `chat_open` (different pixels, checked above) — so for
                # everything else POE2 defaults to IN_COMBAT and we lean
                # on the F7 manual pause + the chat-open gate. When a
                # POE2-specific detector lands, drop this branch.
                ctx = GameContext.IN_COMBAT
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
            state["d3_reading"] = d3_reading

            # Buff watcher — share the d3 detector's grab (no second
            # ImageGrab) and project matched buff names into the engine
            # so BUFF_ACTIVE conditions and the panel can both react.
            # Gated on:
            #   * engine.enabled — F7 hotkey toggle. When the user
            #     pauses automation they expect *all* alerts to stop,
            #     including buff dings. Without this gate the watcher
            #     kept beeping over a paused game.
            #   * d3_reading.is_paused — in-game pause states. Buff
            #     icons don't render over open menus and we already
            #     short-circuit input.
            # On either disabled branch we also nuke the watcher's
            # rising-edge state via set_buffs(...) — a one-line trick
            # that resets _last_seen so when the user re-enables, a
            # buff that's still up will ring as a fresh rising edge.
            # Buff watcher tick. Two paths:
            #   D3  — reuse d3_state_detector.last_grab (no extra ImageGrab)
            #   POE2 — small dedicated ImageGrab of just the buff strip
            #          (~2250×108 ≈ 240k px; ~25 ms via grim on Wayland)
            #
            # Gating is intentionally minimal: engine.enabled (F7 toggle)
            # and not chat-open. We DON'T gate on game state — Savage Fury
            # can fill while the player is at a waypoint, browsing the
            # atlas, or in town, and the whole point of the alarm is to
            # hear it regardless of what the camera is doing. The earlier
            # restriction to GameContext.IN_COMBAT silently disabled
            # alerts whenever the detector classified the scene as TOWN
            # or MENU.
            buff_img = None
            poe2_should_tick = (
                buff_watcher is not None
                and buff_strip_grab_bbox is not None
                and state["engine"].enabled
                and not state["chat_open"]
            )
            d3_should_tick = (
                buff_watcher is not None
                and state["engine"].enabled
                and d3_state_detector is not None
                and d3_state_detector.last_grab is not None
                and d3_reading is not None
                and not d3_reading.is_paused
            )
            if d3_should_tick:
                buff_img = d3_state_detector.last_grab
            elif poe2_should_tick:
                try:
                    from PIL import ImageGrab
                    buff_img = ImageGrab.grab(
                        bbox=buff_strip_grab_bbox,
                    ).convert("RGB")
                except Exception as exc:  # noqa: BLE001
                    log.warning("poe2 buff strip grab failed: %s", exc)
                    buff_img = None

            if buff_img is not None:
                try:
                    buff_reading = buff_watcher.evaluate(buff_img, now)
                    engine_obj.buffs_seen = frozenset(buff_reading.seen)
                except Exception as exc:  # noqa: BLE001
                    log.warning("buff watcher tick failed: %s", exc)
            else:
                engine_obj.buffs_seen = frozenset()
                if buff_watcher is not None and buff_watcher.buffs:
                    # Re-arm rising-edge state for the next enable; the
                    # set_buffs call is the only public path that clears
                    # _last_seen without forgetting the configured buffs.
                    buff_watcher.set_buffs(buff_watcher.buffs)

            if reading is not None:
                engine_obj.apply_detector_reading(reading)
            elif d3_reading is not None and d3_reading.slot_states:
                # Translate the D3 detector's string slot_states into the
                # engine's HotkeyKind → SlotState map so rules with
                # SLOT_STATE_IS conditions can be evaluated for D3.
                from arpg_react.config import HotkeyKind
                from arpg_react.rules import SlotState as _SlotState
                fresh: dict[HotkeyKind, _SlotState] = {}
                for slot_name, state_name in d3_reading.slot_states.items():
                    try:
                        hk = HotkeyKind(slot_name)
                        st = _SlotState(state_name)
                    except ValueError:
                        continue
                    fresh[hk] = st
                engine_obj.slot_states = fresh
                # Project the D3 HP-orb estimate into the engine's HEALTH
                # resource so HEALTH_BELOW conditions work for D3 builds
                # the same way they do for D4. RESOURCE_LEFT/RIGHT stay
                # 0 — D3 has no detector for those yet.
                engine_obj.resource_fills = {
                    "HEALTH": d3_reading.hp_pct,
                    "RESOURCE_LEFT": 0.0,
                    "RESOURCE_RIGHT": 0.0,
                }
            engine_obj._input = None if ctx in INPUT_SUPPRESSED else input_controller  # noqa: SLF001
            # Hand the rule engine this tick's pressed-trigger tokens.
            # Drain even when the engine is disabled — the listener
            # would otherwise accumulate a backlog of presses that all
            # fire the instant the user re-enables automation.
            engine_obj.hotkeys_pressed = trigger_listener.drain()
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
                        current=config.active_build_for(_daemon_game) or active.name,
                        available=list_builds(builds_dir),
                        class_name=class_name,
                        build_url=active.build_url,
                    )
                    eng = state["engine"]
                    # Surface the detector's specific game state (combat /
                    # town / mounted / menu / unknown) instead of the
                    # collapsed input-gate state, so the panel can label it.
                    # D3 uses its own state reason ("esc_menu", "chat_open",
                    # "modal_panel", "combat") instead of the D4 enum.
                    last_reading = state.get("last_reading")
                    last_d3 = state.get("d3_reading")
                    if last_d3 is not None:
                        detected_state = last_d3.reason
                    elif last_reading is not None:
                        detected_state = last_reading.game_state.value
                    else:
                        detected_state = ctx.value
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
                                mutes={
                                    "rule_alerts": config.mutes.rule_alerts,
                                    "buff_alerts": config.mutes.buff_alerts,
                                    "chat_alarm": config.mutes.chat_alarm,
                                },
                            )
                        )
                    )

            time.sleep(TICK_SECONDS)
    finally:
        sync_stop.set()
        hotkey.stop()
        trigger_listener.stop()
        movement_monitor.stop()
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
