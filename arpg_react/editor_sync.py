"""Pull builds from the web editor backend and write them to the local
builds directory so the daemon picks them up automatically.

The editor speaks HTTP Basic Auth. Password comes from the
`D4_EDITOR_PASSWORD` environment variable; without it auto-sync is a
silent no-op (the user will see a one-time warning in logs).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)


PASSWORD_FILE = Path.home() / ".config" / "arpg_react" / "editor.password"


def password_from_env() -> str | None:
    """Editor password from env var, then ~/.config/arpg_react/editor.password.

    The file fallback matters when the daemon is launched from a desktop
    entry that doesn't carry your shell env (env var isn't inherited from
    your terminal). Storing the password in user-only-readable file is
    fine for a single-user dev tool.
    """
    env = os.environ.get("D4_EDITOR_PASSWORD")
    if env:
        return env
    try:
        if PASSWORD_FILE.exists():
            text = PASSWORD_FILE.read_text().strip()
            if text:
                return text
    except OSError:
        pass
    return None


def sync_once(url: str, builds_dir: Path, password: str | None = None) -> int:
    """Pull every build from the editor backend and write to local files.

    Returns the count of builds *changed* (new or content-different);
    files that already match the server are skipped without rewriting
    so the daemon's reload-on-change path stays quiet.

    Returns 0 silently when no password is available.
    """
    if password is None:
        password = password_from_env()
    if not password:
        return 0
    if not url.endswith("/"):
        url = url + "/"

    auth = ("user", password)
    builds_dir.mkdir(parents=True, exist_ok=True)
    changed = 0

    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(urljoin(url, "api/builds"), auth=auth)
            r.raise_for_status()
            names = [b["name"] for b in r.json().get("builds", [])]
            for name in names:
                resp = client.get(urljoin(url, f"api/builds/{name}"), auth=auth)
                resp.raise_for_status()
                payload = resp.json()
                target = builds_dir / f"{name}.json"
                new_text = json.dumps(payload, indent=2, sort_keys=True)
                old_text = target.read_text() if target.exists() else None
                if old_text == new_text:
                    continue
                target.write_text(new_text)
                log.info("editor_sync: pulled %s", name)
                changed += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("editor_sync: %s", exc)
        return 0
    return changed


# ---- per-user profile (display + keymap) sync ------------------------------

def _profile_cache_path(game: str) -> Path:
    return Path.home() / ".config" / "arpg_react" / f"profile_{game}.json"


def sync_profile(url: str, game: str, password: str | None = None) -> dict | None:
    """Pull /api/profile?game=<g> from the editor backend, cache to disk,
    and return the profile dict. Returns None on any failure (and the
    daemon falls back to whatever is on disk via load_cached_profile)."""
    if password is None:
        password = password_from_env()
    if not password:
        return None
    if not url.endswith("/"):
        url = url + "/"

    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(
                urljoin(url, "api/profile"),
                params={"game": game},
                auth=("user", password),
            )
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("editor_sync: profile fetch failed: %s", exc)
        return None

    cache = _profile_cache_path(game)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def load_cached_profile(game: str) -> dict | None:
    """Read the on-disk profile cache without any network call. Used at
    daemon startup so the keymap is in place before the first sync runs."""
    cache = _profile_cache_path(game)
    if not cache.exists():
        return None
    try:
        return json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("editor_sync: bad profile cache at %s: %s", cache, exc)
        return None
