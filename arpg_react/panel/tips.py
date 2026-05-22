"""Tips tab — curated strategy tips per game, with a refresh button that
also pulls the current top-of-week posts from the game's subreddit.

Curated tips live in `arpg_react/resources/tips_{d4,poe2}.json` and are
shipped with the package; refresh those by editing the file (or asking
Claude to update them). Reddit posts are fetched on demand via the panel's
existing QNetworkAccessManager pattern — no extra dependency.

Layout shape mirrors the POE2 LINKS tab so the panel reads as one product
across tabs: stack of cards, each with a topic chip, title, body, and an
OPEN button that launches the source URL.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

from arpg_react.panel.theme import Theme

log = logging.getLogger(__name__)

# Subreddit per game. Reddit JSON endpoint is unauthenticated for public
# subs as long as the User-Agent isn't the default Python/libcurl one.
SUBREDDIT = {
    "d4":   "diablo4",
    "poe2": "PathOfExile2",
}

# Limit of remote posts to render. Reddit allows up to 100 per call; 15 is
# plenty for a single scrollable list alongside ~10–15 curated tips.
REDDIT_LIMIT = 15

# Posts below this score are noise — drop them before showing. Reddit
# top-of-week on these subs comfortably clears 100 for anything worth
# reading.
REDDIT_MIN_SCORE = 100

# User-Agent string for the Reddit request. Reddit blocks generic agents;
# the format `platform:appname:version (by /u/<account>)` is what their
# docs recommend.
REDDIT_USER_AGENT = "linux:arpg-react-panel:1.0 (companion-tool)"


# ----------------------------------------------------------------- model

@dataclass(frozen=True)
class Tip:
    id: str
    title: str
    body: str
    topic: str
    source_label: str
    source_url: str
    added_date: str        # ISO date for sorting
    classes: tuple[str, ...] = field(default_factory=tuple)
    is_remote: bool = False  # True for live-fetched (e.g. reddit) posts
    pinned: bool = False     # protected from auto-drop on next refresh

    @classmethod
    def from_curated(cls, raw: dict) -> "Tip":
        return cls(
            id=str(raw["id"]),
            title=str(raw["title"]),
            body=str(raw["body"]),
            topic=str(raw.get("topic") or "General"),
            source_label=str(raw.get("source_label") or "Source"),
            source_url=str(raw["source_url"]),
            added_date=str(raw.get("added_date") or ""),
            classes=tuple(raw.get("classes") or ()),
            is_remote=False,
            pinned=bool(raw.get("pinned") or False),
        )


# ----------------------------------------------------------------- loader

def load_curated_tips(game: str, resources_root: Path) -> list[Tip]:
    """Read the shipped JSON for a game. Empty list if the file is missing
    or malformed — never raise; the tab still renders with just remote tips."""
    path = resources_root / f"tips_{game}.json"
    if not path.exists():
        log.warning("curated tips file missing: %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to parse %s: %s", path, exc)
        return []
    out: list[Tip] = []
    for raw in data:
        try:
            out.append(Tip.from_curated(raw))
        except (KeyError, TypeError) as exc:
            log.warning("skipping malformed tip in %s: %s", path, exc)
    return out


# ----------------------------------------------------------------- reddit

class RedditFetcher(QtCore.QObject):
    """Fetch top-of-week posts from a subreddit as a list[Tip].

    Emits `finished(list[Tip])` on success, `failed(str)` on any error
    (network, JSON shape, etc.). Always emits exactly one of the two so
    the UI can clear its 'loading…' state.
    """

    finished = QtCore.pyqtSignal(list)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._net = QtNetwork.QNetworkAccessManager(self)
        self._inflight: QtNetwork.QNetworkReply | None = None

    def fetch(self, subreddit: str) -> None:
        if self._inflight is not None:
            # Already running — drop the new request rather than racing.
            return
        url = QtCore.QUrl(
            f"https://www.reddit.com/r/{subreddit}/top.json?t=week&limit={REDDIT_LIMIT}"
        )
        req = QtNetwork.QNetworkRequest(url)
        req.setHeader(
            QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader,
            REDDIT_USER_AGENT,
        )
        req.setTransferTimeout(8_000)
        self._inflight = self._net.get(req)
        self._inflight.finished.connect(self._on_finished)

    def _on_finished(self) -> None:
        reply = self._inflight
        self._inflight = None
        if reply is None:
            return
        try:
            if reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
                self.failed.emit(reply.errorString())
                return
            status = reply.attribute(
                QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            if status != 200:
                self.failed.emit(f"HTTP {status}")
                return
            raw = bytes(reply.readAll())
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.failed.emit(f"bad JSON: {exc}")
                return
            tips = self._parse(payload)
            self.finished.emit(tips)
        finally:
            reply.deleteLater()

    def _parse(self, payload: dict) -> list[Tip]:
        children = (payload.get("data") or {}).get("children") or []
        out: list[Tip] = []
        for child in children:
            data = child.get("data") or {}
            score = int(data.get("score") or 0)
            if score < REDDIT_MIN_SCORE:
                continue
            if data.get("stickied") or data.get("over_18"):
                continue
            title = (data.get("title") or "").strip()
            if not title:
                continue
            permalink = data.get("permalink") or ""
            url = f"https://reddit.com{permalink}" if permalink else (data.get("url") or "")
            flair = (data.get("link_flair_text") or "").strip()
            selftext = (data.get("selftext") or "").strip()
            created_utc = float(data.get("created_utc") or 0)
            added_date = (
                datetime.fromtimestamp(created_utc, tz=timezone.utc).date().isoformat()
                if created_utc else ""
            )
            body = self._summarize(selftext, score, flair, data)
            sub = data.get("subreddit") or ""
            out.append(
                Tip(
                    id=f"reddit-{data.get('id')}",
                    title=title,
                    body=body,
                    topic="Reddit",
                    source_label=f"r/{sub}",
                    source_url=url,
                    added_date=added_date,
                    is_remote=True,
                )
            )
        return out

    @staticmethod
    def _summarize(selftext: str, score: int, flair: str, data: dict) -> str:
        # Reddit posts come in two shapes: self-posts (text in selftext)
        # and link posts (no body, just a title + outbound URL). Show the
        # opening of selftext when present; otherwise lean on flair +
        # score + comment count so the card isn't empty.
        if selftext:
            snippet = selftext.replace("\n\n", " ").replace("\n", " ").strip()
            if len(snippet) > 240:
                snippet = snippet[:237].rstrip() + "…"
            return snippet
        comments = int(data.get("num_comments") or 0)
        parts = []
        if flair:
            parts.append(flair)
        parts.append(f"{score:,} upvotes · {comments:,} comments")
        return " · ".join(parts)


# ----------------------------------------------------------------- widget

def _fmt_age_days(date_iso: str) -> str:
    """Human-readable age stamp for a tip's added_date. Empty string if
    the date can't be parsed — caller hides the label in that case."""
    if not date_iso:
        return ""
    try:
        added = datetime.fromisoformat(date_iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - added
    days = max(int(delta.total_seconds() // 86400), 0)
    if days <= 0:
        return "today"
    if days == 1:
        return "1d ago"
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


class TipCard(QtWidgets.QFrame):
    """One tip rendered as a stacked card: topic chip + title + body + open."""

    def __init__(self, tip: Tip, theme: Theme, parent=None):
        super().__init__(parent)
        self.tip = tip
        self.setObjectName("tipCard")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        topic_chip = QtWidgets.QLabel(tip.topic.upper())
        topic_chip.setObjectName("tipTopic")

        # Pinned indicator — read-only here; pinning happens in the web
        # editor. Visible whenever ANY user has pinned this tip (the
        # refresh script stamps `pinned: true` from the union).
        pinned_chip = QtWidgets.QLabel("📌 PINNED")
        pinned_chip.setObjectName("tipPinned")
        pinned_chip.setVisible(bool(tip.pinned))

        source = QtWidgets.QLabel(tip.source_label)
        source.setObjectName("tipSource")

        age = _fmt_age_days(tip.added_date)
        age_label = QtWidgets.QLabel(age)
        age_label.setObjectName("tipAge")
        age_label.setVisible(bool(age))

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        top.addWidget(topic_chip)
        for cls in tip.classes:
            chip = QtWidgets.QLabel(cls.upper())
            chip.setObjectName("tipClass")
            top.addWidget(chip)
        if tip.pinned:
            top.addWidget(pinned_chip)
        top.addStretch(1)
        top.addWidget(source)
        if age:
            sep = QtWidgets.QLabel("·")
            sep.setObjectName("tipSource")
            top.addWidget(sep)
            top.addWidget(age_label)

        title = QtWidgets.QLabel(tip.title)
        title.setObjectName("tipTitle")
        title.setWordWrap(True)

        body = QtWidgets.QLabel(tip.body)
        body.setObjectName("tipBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)

        open_btn = QtWidgets.QPushButton("OPEN")
        open_btn.setObjectName("tipOpen")
        open_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self._open)

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addStretch(1)
        actions.addWidget(open_btn)

        wrap = QtWidgets.QVBoxLayout(self)
        wrap.setContentsMargins(12, 10, 12, 12)
        wrap.setSpacing(6)
        wrap.addLayout(top)
        wrap.addWidget(title)
        wrap.addWidget(body)
        wrap.addLayout(actions)

    def _open(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        if self.tip.source_url:
            QDesktopServices.openUrl(QUrl(self.tip.source_url))


class TipsTab(QtWidgets.QWidget):
    """Curated + live (Reddit) strategy tips for a single game.

    Filter chips at the top filter by topic. REFRESH re-pulls the
    subreddit; curated tips are always shown regardless of refresh state.
    """

    def __init__(
        self,
        theme: Theme,
        game: str,
        resources_root: Path,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.game = game
        self.resources_root = resources_root
        self.setObjectName("tabBody")

        self._curated: list[Tip] = load_curated_tips(game, resources_root)
        self._remote: list[Tip] = []
        self._active_topic: str = "ALL"

        # --- header
        title = QtWidgets.QLabel("TIPS")
        title.setObjectName("tipsTitle")

        self._refresh_btn = QtWidgets.QPushButton("REFRESH")
        self._refresh_btn.setObjectName("tipsRefresh")
        self._refresh_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setToolTip("Pull the latest top posts from the game's subreddit")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)

        self._status = QtWidgets.QLabel("")
        self._status.setObjectName("tipsStatus")

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._status)
        header.addWidget(self._refresh_btn)

        # --- filter chips row (rebuilt whenever the topic set changes)
        self._chip_row = QtWidgets.QHBoxLayout()
        self._chip_row.setContentsMargins(0, 0, 0, 0)
        self._chip_row.setSpacing(6)
        self._chip_buttons: dict[str, QtWidgets.QPushButton] = {}

        chip_wrap = QtWidgets.QWidget()
        chip_wrap.setObjectName("tipsChipWrap")
        chip_wrap.setLayout(self._chip_row)

        # --- scrollable list of cards
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setObjectName("tipsScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._list_container = QtWidgets.QWidget()
        self._list_container.setObjectName("tipsList")
        self._list_layout = QtWidgets.QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_container)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(chip_wrap)
        layout.addWidget(self._scroll, 1)

        # --- reddit fetcher (lazy — only spins up when user clicks REFRESH
        # or on initial mount). Single-shot first fetch so the user has
        # remote tips waiting without the panel feeling stalled on open.
        self._fetcher = RedditFetcher(self)
        self._fetcher.finished.connect(self._on_fetch_done)
        self._fetcher.failed.connect(self._on_fetch_failed)

        self._rebuild_chips()
        self._rebuild_list()
        QtCore.QTimer.singleShot(800, self._kickoff_initial_fetch)

    # ------------------------------------------------------ refresh flow

    def _kickoff_initial_fetch(self) -> None:
        sub = SUBREDDIT.get(self.game)
        if not sub:
            return
        self._set_status("Loading r/" + sub + "…")
        self._refresh_btn.setEnabled(False)
        self._fetcher.fetch(sub)

    def _on_refresh_clicked(self) -> None:
        sub = SUBREDDIT.get(self.game)
        if not sub:
            self._set_status("No subreddit configured")
            return
        self._set_status("Refreshing r/" + sub + "…")
        self._refresh_btn.setEnabled(False)
        self._fetcher.fetch(sub)

    def _on_fetch_done(self, tips: list) -> None:
        self._remote = list(tips)
        self._refresh_btn.setEnabled(True)
        local = datetime.now().strftime("%H:%M")
        self._set_status(f"{len(self._remote)} from reddit · {local}")
        self._rebuild_chips()
        self._rebuild_list()

    def _on_fetch_failed(self, msg: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._set_status(f"reddit fetch failed: {msg}")

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    # ------------------------------------------------------ chips + list

    def _all_tips(self) -> list[Tip]:
        return list(self._curated) + list(self._remote)

    def _topics(self) -> list[str]:
        seen: list[str] = ["ALL"]
        for tip in self._all_tips():
            t = tip.topic.upper()
            if t not in seen:
                seen.append(t)
        return seen

    def _rebuild_chips(self) -> None:
        # Drop old chips before re-adding so we don't leak buttons when
        # remote results swap the topic set.
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chip_buttons.clear()

        for topic in self._topics():
            btn = QtWidgets.QPushButton(topic)
            btn.setObjectName("tipsChip")
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            active = topic == self._active_topic
            btn.setProperty("active", "true" if active else "false")
            btn.clicked.connect(lambda _c=False, t=topic: self._on_chip_clicked(t))
            self._chip_buttons[topic] = btn
            self._chip_row.addWidget(btn)
        self._chip_row.addStretch(1)

    def _on_chip_clicked(self, topic: str) -> None:
        if topic == self._active_topic:
            return
        self._active_topic = topic
        for label, btn in self._chip_buttons.items():
            btn.setProperty("active", "true" if label == topic else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._rebuild_list()

    def _filtered_tips(self) -> list[Tip]:
        tips = self._all_tips()
        if self._active_topic != "ALL":
            tips = [t for t in tips if t.topic.upper() == self._active_topic]
        # Curated first (sorted newest-first by added_date), then remote.
        curated = sorted(
            (t for t in tips if not t.is_remote),
            key=lambda t: t.added_date,
            reverse=True,
        )
        remote = [t for t in tips if t.is_remote]
        return curated + remote

    def _rebuild_list(self) -> None:
        # Strip everything except the trailing stretch.
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        tips = self._filtered_tips()
        if not tips:
            empty = QtWidgets.QLabel("No tips for this filter yet.")
            empty.setObjectName("tipsEmpty")
            empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._list_layout.insertWidget(0, empty)
            return

        for tip in tips:
            card = TipCard(tip, self.theme)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)
