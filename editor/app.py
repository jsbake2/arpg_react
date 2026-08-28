"""ARPG React Rule Editor — backend.

FastAPI app serving the static editor + a small CRUD API for build JSONs
backed by SQLite.

Auth modes:
  * Web UI (browser) — session cookie, set by POST /api/login.
  * Daemon — HTTP Basic with the user's password (back-compat with the
    existing daemon's editor_sync). Daemon Basic-auth defaults to
    game=d4 unless an `X-Game` header is sent.

Schema:
  users(name PK, password_hash)
  sessions(token PK, owner, expires_at)
  builds(owner, game, name) -> data JSON  -- composite PK
  keymaps(owner, game) -> data JSON       -- per-user-per-game hotkey map

Existing single-game data is migrated on startup: every row in `builds`
that lacks owner/game gets backfilled to (jbaker, d4).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Default to an XDG-friendly per-user path so a fresh deploy works without
# touching the source. Production overrides via ARPG_EDITOR_DB or the legacy
# D4_EDITOR_DB env var (currently set in the systemd unit on the live host).
_default_data_root = Path(
    os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
)
DB_PATH = Path(
    os.environ.get(
        "ARPG_EDITOR_DB",
        os.environ.get(
            "D4_EDITOR_DB",
            str(_default_data_root / "arpg-react-editor" / "builds.db"),
        ),
    )
)
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Tips JSON lives outside the static dir so we can rsync it independently
# (refresh_tips.py on the laptop writes the JSON, then deploys here).
# Override via ARPG_TIPS_DIR; default to a sibling `tips/` folder next to
# app.py — matches the server layout produced by tools/refresh_tips.py's
# rsync target (`d4-rule-editor-app/tips/`).
TIPS_DIR = Path(
    os.environ.get(
        "ARPG_TIPS_DIR",
        str(Path(__file__).resolve().parent / "tips"),
    )
)

# Daemon Basic-auth back-compat password — the old single-tenant value.
LEGACY_BASIC_PASSWORD = os.environ.get("D4_EDITOR_PASSWORD", "d4123d4")

# Seeded users — written on first start. Passwords are hashed with sha256+salt.
SEED_USERS = (
    ("jbaker", "arpg123"),
    ("matt",   "arpg123"),
)

VALID_GAMES = {"d4", "poe2", "poe1", "d3"}
SESSION_TTL_SECONDS = 14 * 24 * 3600  # 14 days


# ---------------------------------------------------------------- auth utils


def _hash_password(password: str, salt: str | None = None) -> str:
    """sha256(salt:password) — good enough for a 2-user dev tool. Format
    is `salt:hex` so we can verify without storing the plaintext."""
    if salt is None:
        salt = secrets.token_hex(8)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split(":", 1)
    except ValueError:
        return False
    expected = _hash_password(password, salt=salt)
    return hmac.compare_digest(expected, stored)


# ---------------------------------------------------------------- database


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            name           TEXT PRIMARY KEY,
            password_hash  TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            expires_at  INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS keymaps (
            owner  TEXT NOT NULL,
            game   TEXT NOT NULL,
            data   TEXT NOT NULL,
            PRIMARY KEY (owner, game)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pinned_tips (
            owner      TEXT NOT NULL,
            game       TEXT NOT NULL,
            tip_id     TEXT NOT NULL,
            pinned_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner, game, tip_id)
        )
        """
    )

    # Builds: composite (owner, game, name). Migrate the legacy single-PK
    # `builds(name)` schema if it still has the old shape.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS builds (
            owner       TEXT NOT NULL,
            game        TEXT NOT NULL,
            name        TEXT NOT NULL,
            data        TEXT NOT NULL,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner, game, name)
        )
        """
    )
    # Detect & migrate older shape — the old PK was just `name`. If the
    # migrated table is empty AND there's a legacy table, copy rows.
    pragma_cols = {row[1] for row in cur.execute("PRAGMA table_info(builds)").fetchall()}
    if "owner" not in pragma_cols:
        # Old schema still in place. Build a v2 table, copy, swap.
        cur.execute("ALTER TABLE builds RENAME TO builds_v1")
        cur.execute(
            """
            CREATE TABLE builds (
                owner       TEXT NOT NULL,
                game        TEXT NOT NULL,
                name        TEXT NOT NULL,
                data        TEXT NOT NULL,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (owner, game, name)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO builds (owner, game, name, data, updated_at)
            SELECT 'jbaker', 'd4', name, data, updated_at FROM builds_v1
            """
        )
        cur.execute("DROP TABLE builds_v1")

    # Seed users on first start.
    for name, password in SEED_USERS:
        existing = cur.execute(
            "SELECT 1 FROM users WHERE name = ?", (name,)
        ).fetchone()
        if not existing:
            cur.execute(
                "INSERT INTO users (name, password_hash) VALUES (?, ?)",
                (name, _hash_password(password)),
            )

    conn.commit()


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- auth deps


def _verify_user(conn, username: str, password: str) -> bool:
    row = conn.execute(
        "SELECT password_hash FROM users WHERE name = ?", (username,)
    ).fetchone()
    if not row:
        return False
    return _verify_password(password, row["password_hash"])


def _create_session(conn, owner: str) -> str:
    token = secrets.token_urlsafe(24)
    expires = int(time.time()) + SESSION_TTL_SECONDS
    conn.execute(
        "INSERT INTO sessions (token, owner, expires_at) VALUES (?, ?, ?)",
        (token, owner, expires),
    )
    return token


def _resolve_session(conn, token: str | None) -> str | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT owner, expires_at FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    if int(row["expires_at"]) < int(time.time()):
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return row["owner"]


def _resolve_basic(conn, header: str) -> str | None:
    """Daemon back-compat: HTTP Basic 'user:password' validates against the
    seeded users. Legacy `user:LEGACY_BASIC_PASSWORD` (single-tenant flow)
    also resolves to `jbaker` so existing daemons keep working until their
    config gets the user's real password."""
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, _, password = decoded.partition(":")
    # Real per-user check first
    if username and _verify_user(conn, username, password):
        return username
    # Legacy: any username + the old shared password = jbaker
    if hmac.compare_digest(password, LEGACY_BASIC_PASSWORD):
        return "jbaker"
    return None


def _current_owner(request: Request) -> str | None:
    """Returns the authenticated owner, or None for an open request."""
    with db() as conn:
        owner = _resolve_session(conn, request.cookies.get("session"))
        if owner is not None:
            return owner
        return _resolve_basic(conn, request.headers.get("authorization", ""))


def require_owner(request: Request) -> str:
    owner = _current_owner(request)
    if not owner:
        raise HTTPException(status_code=401, detail="login required")
    return owner


def require_game(game: str | None) -> str:
    g = (game or "d4").lower()
    if g not in VALID_GAMES:
        raise HTTPException(status_code=400, detail=f"unknown game: {g}")
    return g


# --------------------------------------------------------------------- app


app = FastAPI(title="ARPG React Rule Editor")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# -------- session auth ---------


@app.post("/api/login")
async def api_login(request: Request, response: Response):
    payload = await request.json()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    with db() as conn:
        if not _verify_user(conn, username, password):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = _create_session(conn, username)
    response.set_cookie(
        "session", token, max_age=SESSION_TTL_SECONDS,
        httponly=True, secure=True, samesite="lax",
    )
    return {"ok": True, "user": username}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def api_me(owner: str = Depends(require_owner)):
    return {"user": owner}


# -------- builds (game-scoped) ---------


def _build_query_args(request: Request) -> tuple[str, str]:
    owner = require_owner(request)
    game = require_game(
        request.query_params.get("game")
        or request.headers.get("X-Game")
    )
    return owner, game


@app.get("/api/builds")
def list_builds(request: Request) -> dict[str, Any]:
    owner, game = _build_query_args(request)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT name, updated_at FROM builds
            WHERE owner = ? AND game = ?
            ORDER BY name
            """,
            (owner, game),
        ).fetchall()
    return {
        "builds": [
            {"name": r["name"], "updated_at": r["updated_at"]}
            for r in rows
        ],
    }


@app.get("/api/builds/{name}")
def get_build(name: str, request: Request):
    owner, game = _build_query_args(request)
    with db() as conn:
        row = conn.execute(
            """
            SELECT data FROM builds
            WHERE owner = ? AND game = ? AND name = ?
            """,
            (owner, game, name),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"build '{name}' not found")
    return json.loads(row["data"])


VALID_HOTKEYS = {
    # D4 + D3 keyboard/mouse slots
    "1", "2", "3", "4", "L", "R",
    # POE2 mouse + keyboard slots
    "LMB", "MMB", "RMB", "Q", "E", "T", "F",
    # POE1 adds W to the skill bar and a 5th flask slot. The rest of
    # POE1's slots (LMB/MMB/RMB/Q/E/R/T and flasks 1-4) are already
    # covered by the two lines above.
    "W", "5",
}
VALID_SLOT_STATES = {"READY", "ACTIVE_READY", "IN_USE", "COOLDOWN", "DISABLED"}


def _sanitize_conditions(conds):
    if not isinstance(conds, list):
        return
    for c in conds:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t in ("SLOT_STATE_IS", "SLOT_STATE_IS_NOT"):
            if c.get("target") not in VALID_HOTKEYS:
                c["target"] = "1"
            if c.get("value") not in VALID_SLOT_STATES:
                c["value"] = "READY"
        elif t == "BOSS_DETECTED":
            c["target"] = None
            c["value"] = None
        else:
            c["target"] = None
            if not isinstance(c.get("value"), (int, float)):
                c["value"] = 0.5


def _sanitize_build(payload):
    for r in payload.get("rules", []) or []:
        _sanitize_conditions(r.get("conditions"))
        for step in r.get("combo_steps", []) or []:
            _sanitize_conditions(step.get("conditions"))


@app.put("/api/builds/{name}")
async def put_build(name: str, request: Request):
    owner, game = _build_query_args(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    payload["name"] = name
    _sanitize_build(payload)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO builds (owner, game, name, data, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(owner, game, name) DO UPDATE SET
                data = excluded.data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (owner, game, name, json.dumps(payload)),
        )
    return {"ok": True, "name": name, "owner": owner, "game": game}


@app.delete("/api/builds/{name}")
def delete_build(name: str, request: Request):
    owner, game = _build_query_args(request)
    with db() as conn:
        conn.execute(
            "DELETE FROM builds WHERE owner = ? AND game = ? AND name = ?",
            (owner, game, name),
        )
    return {"ok": True}


@app.post("/api/builds/{old_name}/rename")
async def rename_build(old_name: str, request: Request):
    owner, game = _build_query_args(request)
    payload = await request.json()
    new_name = (payload.get("new_name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="new_name required")
    with db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM builds WHERE owner = ? AND game = ? AND name = ?",
            (owner, game, new_name),
        ).fetchone()
        if existing and new_name != old_name:
            raise HTTPException(status_code=409, detail="name already taken")
        row = conn.execute(
            "SELECT data FROM builds WHERE owner = ? AND game = ? AND name = ?",
            (owner, game, old_name),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        data = json.loads(row["data"])
        data["name"] = new_name
        conn.execute(
            "DELETE FROM builds WHERE owner = ? AND game = ? AND name = ?",
            (owner, game, old_name),
        )
        conn.execute(
            "INSERT INTO builds (owner, game, name, data) VALUES (?, ?, ?, ?)",
            (owner, game, new_name, json.dumps(data)),
        )
    return {"ok": True, "name": new_name}


# -------- profile (per user, per game) ---------
# Holds display (screen_w/h, ui_scale) + keymap (slot -> actual key).
# Single source of truth for the daemon's per-user environment config.
# Stored in the `keymaps` table for backward-compat with the legacy schema;
# the JSON shape is now {"display": {...}, "keymap": {...}}.


def _default_profile(game: str) -> dict:
    if game == "poe2":
        slots = ["LMB", "MMB", "RMB", "Q", "E", "R", "T", "F"]
    elif game == "poe1":
        # Q/W/E/R/T skill bar + the five utility flasks on the number row.
        slots = ["LMB", "MMB", "RMB", "Q", "W", "E", "R", "T",
                 "1", "2", "3", "4", "5"]
    elif game == "d3":
        slots = ["1", "2", "3", "4", "L", "R", "Q"]
    else:
        slots = ["1", "2", "3", "4", "L", "R"]
    # D4/D3 'L'/'R' are mouse-button slots — they must map to 'lmb'/'rmb'
    # so the InputController routes them through pynput.mouse, not as the
    # literal letter L/R on the keyboard.
    #
    # This override is game-scoped on purpose. 'R' is an overloaded slot
    # label: in D4/D3 it means right-mouse, but in POE2/POE1 it's the
    # keyboard letter R on the skill bar. Applying the override to every
    # game (as this did previously) seeded POE2 profiles with R -> 'rmb',
    # so a POE2 rule targeting the R slot right-clicked instead of
    # pressing R — and silently overrode the correct per-game default in
    # config.DEFAULT_KEYMAP_BY_GAME, since the daemon merges the cached
    # profile keymap on top of it. 'L' has no such collision (POE uses
    # LMB), but scoping both keeps the rule one line and obvious.
    _MOUSE_SLOT_GAMES = {"d4", "d3"}
    _mouse_slot_overrides = {"L": "lmb", "R": "rmb"} if game in _MOUSE_SLOT_GAMES else {}
    keymap = {s: _mouse_slot_overrides.get(s, s.lower()) for s in slots}
    return {
        "display": {"screen_w": 2560, "screen_h": 1440, "ui_scale": 1.0},
        "keymap": keymap,
    }


@app.get("/api/profile")
def get_profile(request: Request):
    owner, game = _build_query_args(request)
    with db() as conn:
        row = conn.execute(
            "SELECT data FROM keymaps WHERE owner = ? AND game = ?",
            (owner, game),
        ).fetchone()
    base = _default_profile(game)
    if not row:
        return base
    saved = json.loads(row["data"])
    # Migrate legacy shape (flat keymap dict) to new shape.
    if "display" not in saved and "keymap" not in saved:
        saved = {"display": base["display"], "keymap": saved}
    # Fill any missing keys from defaults so the UI never sees holes.
    base["display"].update(saved.get("display") or {})
    merged_keymap = dict(base["keymap"])
    merged_keymap.update(saved.get("keymap") or {})
    return {"display": base["display"], "keymap": merged_keymap}


@app.put("/api/profile")
async def put_profile(request: Request):
    owner, game = _build_query_args(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    display = payload.get("display") or {}
    keymap = payload.get("keymap") or {}
    if not isinstance(display, dict) or not isinstance(keymap, dict):
        raise HTTPException(status_code=400, detail="display and keymap must be objects")
    # Coerce display fields to sensible types and clamp.
    try:
        sw = int(display.get("screen_w", 2560))
        sh = int(display.get("screen_h", 1440))
        ui = float(display.get("ui_scale", 1.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="display fields must be numeric")
    if not (640 <= sw <= 7680 and 480 <= sh <= 4320 and 0.5 <= ui <= 2.0):
        raise HTTPException(status_code=400, detail="display values out of range")
    clean = {
        "display": {"screen_w": sw, "screen_h": sh, "ui_scale": ui},
        "keymap": {str(k): str(v) for k, v in keymap.items() if v},
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO keymaps (owner, game, data)
            VALUES (?, ?, ?)
            ON CONFLICT(owner, game) DO UPDATE SET data = excluded.data
            """,
            (owner, game, json.dumps(clean)),
        )
    return {"ok": True, "profile": clean}


# --- tips ---------------------------------------------------------------
# Tips JSON files are written by tools/refresh_tips.py on the laptop and
# rsync'd here. The website is a read-only surface for the JSON itself;
# the only mutation is per-user pin state (kept in SQLite).


def _load_tips_file(game: str) -> list[dict[str, Any]]:
    """Read tips JSON from disk. Returns [] if missing or malformed —
    surface as an empty list rather than 500ing on the frontend."""
    path = TIPS_DIR / f"tips_{game}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _pinned_for_owner(conn, owner: str, game: str) -> set[str]:
    rows = conn.execute(
        "SELECT tip_id FROM pinned_tips WHERE owner = ? AND game = ?",
        (owner, game),
    ).fetchall()
    return {r["tip_id"] for r in rows}


def _pinned_union(conn, game: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT tip_id FROM pinned_tips WHERE game = ?",
        (game,),
    ).fetchall()
    return {r["tip_id"] for r in rows}


@app.get("/api/tips/{game}")
def get_tips(game: str, request: Request):
    """Return the tips JSON with per-user pin state overlaid. Each tip
    gets a `pinned_by_me` bool reflecting the current user's pin state;
    the `pinned` field (set by refresh_tips.py from the union) means
    'pinned by anyone.'"""
    owner = require_owner(request)
    g = require_game(game)
    tips = _load_tips_file(g)
    with db() as conn:
        my_pinned = _pinned_for_owner(conn, owner, g)
    for t in tips:
        t["pinned_by_me"] = t.get("id") in my_pinned
    return {"game": g, "tips": tips}


@app.get("/api/tips/{game}/pinned")
def get_my_pinned(game: str, request: Request):
    owner = require_owner(request)
    g = require_game(game)
    with db() as conn:
        return {"game": g, "pinned": sorted(_pinned_for_owner(conn, owner, g))}


@app.get("/api/tips/{game}/pinned-union")
def get_pinned_union(game: str, request: Request):
    """Union of all users' pins for this game. The refresh script hits
    this (authenticated as any user) to determine which tip IDs must be
    preserved verbatim during the next curation pass."""
    require_owner(request)  # any authenticated user can read
    g = require_game(game)
    with db() as conn:
        return {"game": g, "pinned": sorted(_pinned_union(conn, g))}


@app.post("/api/tips/{game}/pin")
async def toggle_pin(game: str, request: Request):
    owner = require_owner(request)
    g = require_game(game)
    body = await request.json()
    tip_id = (body.get("tip_id") or "").strip()
    if not tip_id:
        raise HTTPException(status_code=400, detail="tip_id required")
    pinned = bool(body.get("pinned"))
    with db() as conn:
        if pinned:
            conn.execute(
                "INSERT OR IGNORE INTO pinned_tips (owner, game, tip_id) "
                "VALUES (?, ?, ?)",
                (owner, g, tip_id),
            )
        else:
            conn.execute(
                "DELETE FROM pinned_tips WHERE owner=? AND game=? AND tip_id=?",
                (owner, g, tip_id),
            )
    return {"ok": True, "tip_id": tip_id, "pinned": pinned}


# --- pages + static -----------------------------------------------------


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
def index(request: Request):
    """Landing route — login screen if no session, game picker otherwise."""
    with db() as conn:
        owner = _resolve_session(conn, request.cookies.get("session"))
    if owner is None:
        return FileResponse(STATIC_DIR / "login.html")
    return FileResponse(STATIC_DIR / "games.html")


@app.get("/editor")
def editor_page(request: Request):
    with db() as conn:
        owner = _resolve_session(conn, request.cookies.get("session"))
    if owner is None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
