from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from arpg_react import daemon
from arpg_react.config import (
    BuildConfig,
    Config,
    default_builds_dir,
    default_cache_path,
    default_config_path,
    default_socket_path,
    default_user_sounds_dir,
    list_builds,
    load_build,
    load_config,
    save_build,
    save_config,
)
from arpg_react.sources import (
    ClockSource,
    CompositeSource,
    HelltidesSource,
    TimerSource,
)
from arpg_react.timers import EventKind, EventState, EventStatus


def build_source_pair(
    config: Config, cache_path: Path
) -> tuple[TimerSource, HelltidesSource | None]:
    clock = ClockSource(anchors=config.anchor_map())
    if config.source == "clock":
        return clock, None
    helltides = HelltidesSource(cache_path=cache_path)
    return CompositeSource(clock=clock, primary=helltides), helltides


def format_status(s: EventStatus) -> str:
    mins, secs = divmod(s.seconds_until_change, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        countdown = f"{hrs}h {mins:02d}m {secs:02d}s"
    else:
        countdown = f"{mins:02d}m {secs:02d}s"

    verb = {
        EventState.UPCOMING: "starts in",
        EventState.ACTIVE: "ends in",
        EventState.ENDING_SOON: "ENDING in",
    }[s.state]

    extra = f" — {s.label_extra}" if s.label_extra else ""
    return f"{s.kind.value:12s} {s.state.value:13s} {verb} {countdown}{extra}"


def cmd_once(config: Config, cache_path: Path) -> int:
    source, _ = build_source_pair(config, cache_path)
    now = datetime.now(timezone.utc)
    print(f"now: {now.isoformat()}")
    for kind in EventKind:
        try:
            print(format_status(source.status(kind, now)))
        except Exception as exc:
            print(f"{kind.value:12s} ERROR: {exc}")
    return 0


def cmd_run(
    config: Config,
    config_path: Path,
    cache_path: Path,
    sounds_dir: Path,
    socket_path: Path,
    game: str = "d4",
) -> int:
    source, helltides = build_source_pair(config, cache_path)
    return daemon.run(
        config,
        source,
        helltides_source=helltides,
        user_sounds_dir=sounds_dir,
        socket_path=socket_path,
        config_path=config_path,
        game=game,
    )


def cmd_panel(
    socket_path: Path,
    theme: str | None = None,
    game: str | None = None,
) -> int:
    from arpg_react.panel.app import run_panel

    return run_panel(socket_path, theme_name=theme, game=game)


def cmd_app(
    socket_path: Path,
    theme: str | None = None,
    game: str | None = None,
) -> int:
    from arpg_react.launcher import run_app

    return run_app(socket_path, theme=theme, game=game)


def cmd_setup(hotkey: str, config_path: Path, build: str | None) -> int:
    from arpg_react.watchers.setup import run_setup

    return run_setup(hotkey, config_path, build_name=build)


def cmd_capture_build(name: str, url: str | None, password: str | None) -> int:
    from arpg_react.watchers.capture_remote import run_capture_build

    return run_capture_build(name, url=url, password=password)


def cmd_calibrate_skills(default_build: str | None, game: str) -> int:
    from arpg_react.calibrator import run_calibrator

    # API-driven: pulls/saves builds directly via the editor backend.
    # default_build is just the initial dropdown selection (optional —
    # window opens with a build picker).
    return run_calibrator(default_game=game, default_build=default_build)


def cmd_sync_builds(url: str | None, password: str | None) -> int:
    """Pull every build from the editor backend and write to local files."""
    import getpass
    import json as _json
    import os as _os
    from urllib.parse import urljoin

    import httpx

    base_url = url or _os.environ.get("D4_EDITOR_URL") or "https://d4.jsb-emr.us/"
    if not base_url.endswith("/"):
        base_url += "/"
    if password is None:
        password = _os.environ.get("D4_EDITOR_PASSWORD") or getpass.getpass(
            f"editor password for {base_url}: "
        )
    if not password:
        print("password required")
        return 1

    builds_dir = default_builds_dir()
    builds_dir.mkdir(parents=True, exist_ok=True)

    auth = ("user", password)
    with httpx.Client(timeout=15) as client:
        try:
            resp = client.get(urljoin(base_url, "api/builds"), auth=auth)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"failed to list builds: {exc}")
            return 1
        names = [b["name"] for b in resp.json().get("builds", [])]
        if not names:
            print("(no builds on server)")
            return 0
        for name in names:
            try:
                r = client.get(urljoin(base_url, f"api/builds/{name}"), auth=auth)
                r.raise_for_status()
                build = r.json()
                (builds_dir / f"{name}.json").write_text(
                    _json.dumps(build, indent=2)
                )
                print(f"  ↓ {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {name} failed: {exc}")
    print(f"\nsynced {len(names)} build(s) to {builds_dir}")
    return 0


def cmd_builds(config_path: Path) -> int:
    cfg = load_config(config_path)
    builds_dir = default_builds_dir()
    names = list_builds(builds_dir)
    if not names:
        print("(no builds — run setup to create one)")
        return 0
    for name in names:
        marker = "*" if name == cfg.current_build else " "
        b = load_build(name, builds_dir)
        desc = (b.description if b else "") or ""
        slot_count = len(b.watchers) if b else 0
        suffix = f"[{slot_count} slot{'s' if slot_count != 1 else ''}]"
        if desc:
            suffix = f"{suffix}  {desc}"
        print(f"  {marker} {name}  {suffix}")
    print()
    print(f"(* = active. files in {builds_dir})")
    print("(switch with: arpg-react use <name>)")
    return 0


def cmd_use(name: str, config_path: Path) -> int:
    cfg = load_config(config_path)
    builds_dir = default_builds_dir()
    if load_build(name, builds_dir) is None:
        save_build(BuildConfig(name=name), builds_dir)
        print(f"created new empty build '{name}'")
    cfg.current_build = name
    save_config(cfg, config_path)
    print(f"active build set to '{name}'")
    print("(running daemon: switch via panel dropdown, or restart)")
    return 0


def cmd_watch(config_path: Path) -> int:
    from arpg_react.watchers.diag import cmd_watch as run

    return run(config_path)


def cmd_probe(config_path: Path) -> int:
    from arpg_react.watchers.diag import cmd_probe as run

    return run(config_path)


def cmd_install(theme: str = "diablo") -> int:
    from arpg_react.install import cmd_install as run

    return run(theme=theme)


def cmd_gen_sounds(sounds_dir: Path, kind: str) -> int:
    from arpg_react.alerts.synth import (
        BELL_PARTIALS,
        GONG_PARTIALS,
        synth_bell_wav,
    )

    presets = {
        "bell": dict(partials=BELL_PARTIALS, fundamental=520.0, duration=2.5),
        "gong": dict(partials=GONG_PARTIALS, fundamental=180.0, duration=4.0),
    }
    if kind not in presets:
        print(f"unknown preset '{kind}'. choose one of: {', '.join(presets)}")
        return 2

    out_path = sounds_dir / "pixel_alert.wav"
    synth_bell_wav(out_path, **presets[kind])
    print(f"wrote {kind} to {out_path}")
    print("the daemon will pick this up next time it starts.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arpg-react")
    parser.add_argument("--config", type=Path, default=None, help="config file path")
    parser.add_argument("--cache", type=Path, default=None, help="helltides cache path")
    parser.add_argument(
        "--sounds-dir",
        type=Path,
        default=None,
        help="directory of user-supplied warning.wav / start.wav / end.wav",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="unix socket path for daemon ↔ panel IPC",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("once", help="print current event statuses and exit")
    run_parser = sub.add_parser(
        "run", help="run daemon (timer alerts + watchers + IPC server)"
    )
    run_parser.add_argument(
        "--game",
        choices=("d4", "poe2"),
        default="d4",
        help="active game — selects per-game profile cache, slot list, and detector defaults (default: d4)",
    )
    panel_parser = sub.add_parser(
        "panel", help="launch PyQt panel (subscribes to a running daemon)"
    )
    panel_parser.add_argument(
        "--theme",
        choices=("neutral", "diablo", "azurite"),
        default=None,
        help="visual theme override (default: matches the chosen game)",
    )
    panel_parser.add_argument(
        "--game",
        choices=("d4", "poe2"),
        default=None,
        help="skip the game-select dialog and launch straight into this game's panel",
    )
    app_parser = sub.add_parser(
        "app", help="all-in-one: auto-start daemon and open panel"
    )
    app_parser.add_argument(
        "--theme",
        choices=("neutral", "diablo", "azurite"),
        default=None,
        help="visual theme override (default: matches the chosen game)",
    )
    app_parser.add_argument(
        "--game",
        choices=("d4", "poe2"),
        default=None,
        help="skip the game-select dialog and launch straight into this game's panel",
    )
    setup_parser = sub.add_parser(
        "setup",
        help="set up a pixel watcher for a D4 hotkey slot (1-4, L, R)",
    )
    setup_parser.add_argument(
        "hotkey",
        help="hotkey slot to monitor: 1, 2, 3, 4, L (left mouse), or R (right mouse)",
    )
    setup_parser.add_argument(
        "--build",
        default=None,
        help="build to add the watcher to (defaults to active build)",
    )
    sub.add_parser("builds", help="list builds")
    use_parser = sub.add_parser("use", help="switch active build (creates if missing)")
    use_parser.add_argument("name", help="build name")
    cap_parser = sub.add_parser(
        "capture-build",
        help="capture a full build (slots + resources) and push to the editor backend",
    )
    cap_parser.add_argument("name", help="build name")
    cap_parser.add_argument("--url", default=None)
    cap_parser.add_argument("--password", default=None)
    calib_parser = sub.add_parser(
        "calibrate-skills",
        help="OCR-driven per-slot timing capture (POE2 skill panel etc.)",
    )
    calib_parser.add_argument(
        "--build", default=None,
        help="optional: pre-select a build in the dropdown",
    )
    calib_parser.add_argument(
        "--game", default="poe2", choices=("d4", "poe2"),
        help="initial game to show (default: poe2)",
    )
    sync_parser = sub.add_parser(
        "sync-builds",
        help="pull all builds from the web editor backend to local files",
    )
    sync_parser.add_argument("--url", default=None)
    sync_parser.add_argument("--password", default=None)
    sub.add_parser("watch", help="diag: live colors at each configured watcher pixel")
    sub.add_parser("probe", help="diag: live color under the cursor")
    install_parser = sub.add_parser(
        "install", help="install desktop launcher entry + icon"
    )
    install_parser.add_argument(
        "--theme",
        choices=("neutral", "diablo"),
        default="diablo",
        help="theme baked into the launcher (default: neutral)",
    )
    gen_parser = sub.add_parser(
        "gen-sounds", help="synthesize a bell/gong .wav for the pixel-alert sound"
    )
    gen_parser.add_argument(
        "kind",
        nargs="?",
        choices=("bell", "gong"),
        default="bell",
        help="which preset (default: bell)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("arpg_react").setLevel(
        logging.DEBUG if args.verbose else logging.INFO
    )

    config_path = args.config or default_config_path()
    config = load_config(config_path)
    cache_path = args.cache or default_cache_path()
    socket_path = args.socket or default_socket_path()

    cmd = args.cmd or "once"
    if cmd == "once":
        return cmd_once(config, cache_path)
    if cmd == "run":
        game = getattr(args, "game", "d4")
        sounds_dir = args.sounds_dir or default_user_sounds_dir()
        return cmd_run(config, config_path, cache_path, sounds_dir, socket_path, game=game)
    if cmd == "panel":
        return cmd_panel(socket_path, theme=args.theme, game=args.game)
    if cmd == "app":
        return cmd_app(socket_path, theme=args.theme, game=args.game)
    if cmd == "setup":
        return cmd_setup(args.hotkey, config_path, args.build)
    if cmd == "builds":
        return cmd_builds(config_path)
    if cmd == "use":
        return cmd_use(args.name, config_path)
    if cmd == "capture-build":
        return cmd_capture_build(args.name, args.url, args.password)
    if cmd == "calibrate-skills":
        return cmd_calibrate_skills(args.build, args.game)
    if cmd == "sync-builds":
        return cmd_sync_builds(args.url, args.password)
    if cmd == "watch":
        return cmd_watch(args.config or default_config_path())
    if cmd == "probe":
        return cmd_probe(args.config or default_config_path())
    if cmd == "gen-sounds":
        return cmd_gen_sounds(
            args.sounds_dir or default_user_sounds_dir(), args.kind
        )
    if cmd == "install":
        return cmd_install(args.theme)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
