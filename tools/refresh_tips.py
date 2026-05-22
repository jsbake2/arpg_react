#!/usr/bin/env python3
"""Daily curated-tips refresh.

Architecture (rewritten to slash cost — see SESSION 2026-05-21):
1. Fetch candidates programmatically (RSS feeds + reddit JSON). No AI.
2. One Haiku 4.5 call per game with the candidate list + a `submit_tips`
   strict-schema tool. No agentic loop, no server-side web_search.

This drops cost from ~$0.65/run (Sonnet + web_search loop) to ~$0.07/run
(Haiku one-shot), or roughly $25/year for a daily cadence.

Per-game atomicity preserved:
- Each game is its own API call. Partial failure leaves the other game
  alone and notify-sends the bad one.
- Each game's existing JSON is copied to `.prev` before overwrite.

Exit codes: 0 = both games updated, 1 = partial, 2 = total failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic
import feedparser
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = REPO_ROOT / "arpg_react" / "resources"
LOG_DIR = Path.home() / ".cache" / "arpg_react"
LOG_FILE = LOG_DIR / "refresh_tips.log"
SECRETS_FILE = Path.home() / ".config" / "arpg_react" / "secrets.env"

# Editor backend — read pin-union from here before curating, and rsync
# the freshly-written JSON back so the website sees today's tips.
EDITOR_BASE_URL = os.environ.get("ARPG_EDITOR_URL", "https://arpg.jsb-emr.us")
EDITOR_AUTH_USER = os.environ.get("ARPG_EDITOR_USER", "jbaker")
# Daemon-class basic auth — same shared password used by editor_sync.
EDITOR_AUTH_PASS = os.environ.get("ARPG_EDITOR_PASSWORD", "d4123d4")
# Deploy target — server-side path that the editor's TIPS_DIR points at.
DEPLOY_TARGET = os.environ.get(
    "ARPG_TIPS_DEPLOY_TARGET", "new-server:/home/jbaker/d4-rule-editor-app/tips/"
)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 8000
# Drop candidates older than this; older items aren't "current" anymore.
CANDIDATE_MAX_AGE = timedelta(days=21)
# Cap candidates per source so a chatty RSS doesn't drown out reddit.
PER_SOURCE_CAP = 15
USER_AGENT = "linux:arpg-react-tips-refresh:1.0 (contact: jbaker)"

# Must match the topics the panel renders as filter chips. Adding here
# is fine (panel just renders the new chip); removing would break old tips.
ALLOWED_TOPICS = [
    "Paragon", "Pit", "Glyphs", "Gems", "Seasonal", "Class",
    "Atlas", "Currency", "Mapping", "Endgame", "General",
]

# Sources per game. Reddit uses the JSON API (the .rss endpoint returns
# only 1 entry on reddit's side); RSS sources use feedparser.
SOURCES: dict[str, list[dict[str, str]]] = {
    "d4": [
        {"kind": "rss", "label": "Wowhead", "url": "https://www.wowhead.com/news/rss/diablo-4"},
        {"kind": "rss", "label": "DiabloFans", "url": "https://www.diablofans.com/news.rss"},
        {"kind": "reddit", "label": "r/diablo4", "subreddit": "diablo4"},
    ],
    "poe2": [
        {"kind": "rss", "label": "Path of Exile (official)", "url": "https://www.pathofexile.com/news/rss"},
        {"kind": "reddit", "label": "r/PathOfExile2", "subreddit": "PathOfExile2"},
    ],
}


@dataclass(frozen=True)
class Candidate:
    title: str
    link: str
    summary: str
    source: str
    published: str  # ISO date or empty


# ------------------------------------------------------------ candidate fetch

def _strip_html(text: str) -> str:
    """Quick-and-dirty HTML strip — RSS summaries often contain markup we
    don't want feeding into Claude as tokens. feedparser leaves it as-is."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _within_age_window(published: str) -> bool:
    if not published:
        return True  # don't drop just for missing date
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= CANDIDATE_MAX_AGE


def _entry_published_iso(entry: Any) -> str:
    """feedparser parses dates into struct_time on .published_parsed."""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return ""
    try:
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return ""


def fetch_rss(url: str, label: str) -> list[Candidate]:
    """feedparser handles RSS + Atom + most quirks; let it set its own UA."""
    logging.info("rss fetch: %s", url)
    parsed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    if parsed.bozo and not parsed.entries:
        logging.warning("rss parse failed: %s (%s)", url, parsed.bozo_exception)
        return []
    out: list[Candidate] = []
    for entry in parsed.entries[:PER_SOURCE_CAP]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")
        if len(summary) > 400:
            summary = summary[:397].rstrip() + "…"
        published = _entry_published_iso(entry)
        if not _within_age_window(published):
            continue
        out.append(Candidate(
            title=title, link=link, summary=summary,
            source=label, published=published,
        ))
    return out


def fetch_reddit(subreddit: str, label: str) -> list[Candidate]:
    """Reddit's RSS endpoint returns only 1 entry — use the JSON API for
    real coverage. Score floor drops low-signal community noise."""
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit={PER_SOURCE_CAP}"
    logging.info("reddit fetch: %s", url)
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logging.warning("reddit fetch failed for %s: %s", subreddit, exc)
        return []
    out: list[Candidate] = []
    for child in (data.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        if d.get("stickied") or d.get("over_18"):
            continue
        score = int(d.get("score") or 0)
        if score < 100:
            continue
        title = (d.get("title") or "").strip()
        if not title:
            continue
        permalink = d.get("permalink") or ""
        link = f"https://reddit.com{permalink}" if permalink else (d.get("url") or "")
        body = (d.get("selftext") or "").strip().replace("\n\n", " ").replace("\n", " ")
        if not body:
            flair = (d.get("link_flair_text") or "").strip()
            body = f"[{flair}] " if flair else ""
            body += f"{score:,} upvotes, {int(d.get('num_comments') or 0):,} comments"
        if len(body) > 400:
            body = body[:397].rstrip() + "…"
        created = float(d.get("created_utc") or 0)
        published = (
            datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
            if created else ""
        )
        out.append(Candidate(
            title=title, link=link, summary=body,
            source=label, published=published,
        ))
    return out


def fetch_candidates(game: str) -> list[Candidate]:
    seen: set[str] = set()
    all_items: list[Candidate] = []
    for source in SOURCES[game]:
        try:
            if source["kind"] == "rss":
                items = fetch_rss(source["url"], source["label"])
            elif source["kind"] == "reddit":
                items = fetch_reddit(source["subreddit"], source["label"])
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "candidate fetch failed for %s/%s: %s",
                game, source.get("label"), exc,
            )
            continue
        for c in items:
            if c.link in seen:
                continue
            seen.add(c.link)
            all_items.append(c)
    logging.info("%s candidates: %d", game, len(all_items))
    return all_items


# ----------------------------------------------------------- claude one-shot

SYSTEM_PROMPT = """You are a curator maintaining a list of actionable strategy tips for an action-RPG companion app. The user plays Diablo 4 and Path of Exile 2 solo on PC and wants tips that give an edge during play — things not obvious from the in-game UI.

Each run, you receive TWO inputs for a given game:
- PRIOR TIPS: the existing curated list (typically 12-15 tips). These are already-vetted, high-signal tips.
- CANDIDATES: recent items from authoritative sources (Wowhead, official patch notes, top-of-week reddit, etc.). These may be news, strategy guides, community posts, or promotional content.

Your job is to produce an UPDATED tip list by intelligently merging:

1. KEEP prior tips that are still relevant. Most tips remain useful across many days — strategy doesn't change overnight. Default to keeping them unless they're contradicted or made stale by something in CANDIDATES.
2. DROP prior tips that are now wrong, outdated, or about a previous patch (CANDIDATES revealing a nerf, mechanic change, or content removal is the signal).
3. ADD new tips from CANDIDATES only when an item reveals genuinely actionable, non-obvious information that isn't already covered. Most news items will NOT translate into a tip — skip patch announcements, sales, transmog cosmetics, balance-summary news, and "watch this video" content.
4. Aim for 12-15 final tips. If candidates don't yield anything tip-worthy, fewer than 12 is fine — quality over quantity.

Tip schema:
- id: slugified game-prefix-keyword, lowercase, hyphenated, unique. PRESERVE prior tip IDs when you keep them; generate new IDs for new tips.
- title: short, scannable, imperative (under 70 chars)
- body: 2-4 sentences explaining WHAT to do AND WHY. Synthesize from candidate summaries; don't fabricate facts.
- topic: ONE of: Paragon, Pit, Glyphs, Gems, Seasonal, Class, Atlas, Currency, Mapping, Endgame, General
- classes: list of class names if class-specific; empty list otherwise
- source_label: short source name (e.g. "Maxroll", "Wowhead", "Mobalytics", "r/diablo4")
- source_url: the URL of the underlying source — for kept prior tips, preserve the existing URL; for new tips, use the candidate's URL exactly as provided. DO NOT invent URLs.
- added_date: for new tips, today's date. For KEPT prior tips, preserve their existing added_date.

Call the `submit_tips` tool exactly once with the full final list (kept + new). Do not write JSON in chat."""

SUBMIT_TIPS_TOOL: dict[str, Any] = {
    "name": "submit_tips",
    "description": "Submit the final structured list of curated tips. Call this exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "tips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "topic": {"type": "string", "enum": ALLOWED_TOPICS},
                        "classes": {"type": "array", "items": {"type": "string"}},
                        "source_label": {"type": "string"},
                        "source_url": {"type": "string"},
                        "added_date": {"type": "string"},
                    },
                    "required": [
                        "id", "title", "body", "topic", "classes",
                        "source_label", "source_url", "added_date",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tips"],
        "additionalProperties": False,
    },
}


def _format_candidates(candidates: list[Candidate]) -> str:
    lines = []
    for i, c in enumerate(candidates, 1):
        pub = c.published[:10] if c.published else "—"
        lines.append(
            f"[{i}] {c.title}\n"
            f"    source: {c.source} ({pub})\n"
            f"    url: {c.link}\n"
            f"    summary: {c.summary or '(no summary)'}"
        )
    return "\n\n".join(lines)


def _load_prior_tips(game: str) -> list[dict[str, Any]]:
    """Read the currently-shipping tips JSON so the model can do a diff/merge
    rather than re-curate from scratch every day."""
    path = RESOURCES_DIR / f"tips_{game}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logging.warning("could not parse prior tips for %s: %s", game, exc)
        return []


def fetch_pinned_union(game: str) -> list[str]:
    """Fetch the union of all users' pinned tip IDs for this game from
    the editor backend. Returns [] on any failure — better to lose pin
    enforcement for one run than to fail the whole refresh."""
    url = f"{EDITOR_BASE_URL}/api/tips/{game}/pinned-union"
    try:
        resp = httpx.get(
            url,
            auth=(EDITOR_AUTH_USER, EDITOR_AUTH_PASS),
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        ids = data.get("pinned") or []
        logging.info("%s: pinned-union = %d ids", game, len(ids))
        return list(ids)
    except Exception as exc:  # noqa: BLE001
        logging.warning("%s: pinned-union fetch failed (%s) — proceeding without pin protection", game, exc)
        return []


def curate(
    client: anthropic.Anthropic,
    game: str,
    candidates: list[Candidate],
    today: str,
    pinned_ids: list[str],
) -> list[dict[str, Any]]:
    if len(candidates) < 5:
        raise RuntimeError(f"too few candidates to curate from: {len(candidates)}")

    game_name = {"d4": "Diablo 4", "poe2": "Path of Exile 2"}[game]
    prior_tips = _load_prior_tips(game)
    prior_json = json.dumps(prior_tips, indent=2, ensure_ascii=False) if prior_tips else "(no prior tips yet)"
    pinned_block = (
        "Pinned tip IDs — these MUST appear in your output verbatim "
        "(same id, title, body, source_url, added_date as in PRIOR TIPS). "
        "Do not drop, rename, or modify them. The user has explicitly "
        "protected these.\n"
        f"PINNED IDS ({len(pinned_ids)}): {json.dumps(pinned_ids)}"
        if pinned_ids
        else "Pinned tip IDs: (none — all prior tips are eligible for drop if obsolete)"
    )

    user_msg = (
        f"Today is {today}. Update the {game_name} tip list.\n\n"
        f"--- PINNED ---\n{pinned_block}\n\n"
        f"--- PRIOR TIPS ({len(prior_tips)}) ---\n\n"
        f"{prior_json}\n\n"
        f"--- CANDIDATES ({len(candidates)}) — recent items to consider ---\n\n"
        f"{_format_candidates(candidates)}\n\n"
        f"Rules: PINNED IDs are immutable — copy them through unchanged. "
        f"Keep other prior tips that are still relevant. Drop any contradicted "
        f"by CANDIDATES (but never drop a PINNED tip). Add new tips from "
        f"CANDIDATES only when they reveal genuinely actionable info not "
        f"already covered. Aim for 12-15 final tips total. For NEW tips, set "
        f"added_date='{today}'; for KEPT tips, preserve their existing "
        f"added_date and id. Now call submit_tips."
    )

    system = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=[SUBMIT_TIPS_TOOL],
        tool_choice={"type": "tool", "name": "submit_tips"},
        messages=[{"role": "user", "content": user_msg}],
    )
    usage = response.usage
    logging.info(
        "%s claude: stop=%s in=%d out=%d cache_read=%d cache_write=%d",
        game, response.stop_reason,
        usage.input_tokens, usage.output_tokens,
        usage.cache_read_input_tokens or 0,
        usage.cache_creation_input_tokens or 0,
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_tips":
            tips = block.input.get("tips") or []
            if not isinstance(tips, list) or len(tips) < 5:
                raise RuntimeError(f"submit_tips returned {len(tips) if isinstance(tips, list) else 'non-list'}")
            # Stamp `pinned: True` on tips whose IDs are in the union, so
            # the panel and website can render the pin indicator without
            # an extra round-trip. Also surfaces if the model dropped a
            # pinned tip (we re-add it from the prior list as a safety net).
            pinned_set = set(pinned_ids)
            seen_ids = {t.get("id") for t in tips}
            for t in tips:
                t["pinned"] = t.get("id") in pinned_set
            missing = pinned_set - seen_ids
            if missing:
                logging.warning(
                    "%s: model dropped pinned tips %s — restoring from prior",
                    game, sorted(missing),
                )
                prior_by_id = {p["id"]: p for p in prior_tips}
                for mid in missing:
                    if mid in prior_by_id:
                        restored = dict(prior_by_id[mid])
                        restored["pinned"] = True
                        tips.append(restored)
            return tips
    raise RuntimeError(f"no submit_tips block in response (stop={response.stop_reason})")


# ------------------------------------------------------------------- output

def deploy_to_server(games: list[str]) -> bool:
    """rsync the freshly-written JSON to the editor server. Returns True
    on success, False on any failure — failure to deploy is a soft error;
    the next refresh will retry."""
    if not games:
        return True
    files = [str(RESOURCES_DIR / f"tips_{g}.json") for g in games]
    cmd = ["rsync", "-az", "--chmod=F644", *files, DEPLOY_TARGET]
    logging.info("deploy: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            logging.warning("deploy rsync failed (%d): %s", result.returncode, result.stderr.strip())
            return False
        logging.info("deploy OK")
        return True
    except subprocess.TimeoutExpired:
        logging.warning("deploy rsync timed out")
        return False
    except Exception as exc:  # noqa: BLE001
        logging.warning("deploy rsync error: %s", exc)
        return False


def write_tips(game: str, tips: list[dict[str, Any]]) -> None:
    dest = RESOURCES_DIR / f"tips_{game}.json"
    prev = RESOURCES_DIR / f"tips_{game}.json.prev"
    if dest.exists():
        shutil.copy2(dest, prev)
    with tempfile.NamedTemporaryFile(
        "w", dir=RESOURCES_DIR, prefix=f".tmp_tips_{game}_", suffix=".json",
        delete=False, encoding="utf-8",
    ) as tf:
        json.dump(tips, tf, indent=2, ensure_ascii=False)
        tf.write("\n")
        tmp_path = Path(tf.name)
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, dest)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stderr),
        ],
    )


def load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "ANTHROPIC_API_KEY":
                return value.strip().strip('"').strip("'")
    raise RuntimeError(
        f"ANTHROPIC_API_KEY not set in env or {SECRETS_FILE}."
    )


def notify(title: str, body: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "arpg-react", title, body],
            check=False, timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("notify-send failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Curated-tips daily refresh")
    parser.add_argument("--game", choices=["d4", "poe2", "both"], default="both")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + curate, but don't touch JSON files")
    parser.add_argument("--dump-candidates", action="store_true",
                        help="Print fetched candidates and exit (no API call)")
    args = parser.parse_args(argv)

    setup_logging()
    logging.info(
        "--- refresh_tips start (game=%s dry_run=%s dump=%s) ---",
        args.game, args.dry_run, args.dump_candidates,
    )

    games = ["d4", "poe2"] if args.game == "both" else [args.game]

    if args.dump_candidates:
        for game in games:
            cands = fetch_candidates(game)
            print(f"\n=== {game.upper()} ({len(cands)} candidates) ===")
            for c in cands:
                print(json.dumps(asdict(c), indent=2))
        return 0

    try:
        api_key = load_api_key()
    except Exception as exc:
        logging.error("api key load failed: %s", exc)
        notify("ARPG tips refresh FAILED", str(exc), "critical")
        return 2

    client = anthropic.Anthropic(api_key=api_key)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results: dict[str, tuple[bool, str]] = {}
    written_games: list[str] = []
    for game in games:
        try:
            pinned_ids = fetch_pinned_union(game)
            candidates = fetch_candidates(game)
            tips = curate(client, game, candidates, today, pinned_ids)
            logging.info("%s: %d tips curated from %d candidates (pinned=%d)",
                         game, len(tips), len(candidates), len(pinned_ids))
            if not args.dry_run:
                write_tips(game, tips)
                written_games.append(game)
                logging.info("%s: wrote %s", game, RESOURCES_DIR / f"tips_{game}.json")
            results[game] = (True, f"{len(tips)} tips")
        except Exception as exc:  # noqa: BLE001
            logging.exception("%s: refresh failed: %s", game, exc)
            results[game] = (False, str(exc))

    # Deploy whatever succeeded. Soft-fails — log + notify but don't flip
    # the per-game success flags, since the JSON is already correct locally.
    if written_games and not args.dry_run:
        if not deploy_to_server(written_games):
            notify(
                "ARPG tips deploy failed",
                f"JSON updated locally for {', '.join(written_games)} but rsync to server failed. Check log.",
                "normal",
            )

    successes = [g for g, (ok, _) in results.items() if ok]
    failures = [g for g, (ok, _) in results.items() if not ok]

    if not failures:
        msg = " · ".join(f"{g}: {results[g][1]}" for g in successes)
        logging.info("--- refresh_tips OK · %s ---", msg)
        return 0
    if successes:
        body = (
            f"Updated: {', '.join(successes)}.\n"
            f"Failed: {', '.join(f'{g} ({results[g][1][:80]})' for g in failures)}"
        )
        notify("ARPG tips refresh PARTIAL", body, "normal")
        logging.warning("--- refresh_tips PARTIAL ---")
        return 1
    body = "\n".join(f"{g}: {results[g][1][:120]}" for g in failures)
    notify("ARPG tips refresh FAILED", body, "critical")
    logging.error("--- refresh_tips TOTAL FAILURE ---")
    return 2


if __name__ == "__main__":
    sys.exit(main())
