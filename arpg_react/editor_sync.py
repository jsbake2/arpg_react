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
