"""QSS theme definitions.

Themes are simple dataclasses of color / font tokens. The QSS template in
`style_qss()` is parameterized so we can swap palettes without touching widgets.

Two themes ship: NEUTRAL (services-panel-style dark, used by default during
development) and DIABLO (placeholder values for the final crimson + gold
aesthetic — easy to refine once we pick exact colors and a Diablo-friendly
font).
"""

from __future__ import annotations

from dataclasses import dataclass

from arpg_react.timers import EventState


@dataclass(frozen=True)
class Theme:
    name: str

    bg: str
    panel_bg: str
    card_bg: str
    card_bg_hover: str
    border: str

    text: str
    text_dim: str
    text_label: str
    accent: str

    state_active: str
    state_upcoming: str
    state_ending: str
    state_unknown: str

    severity_warning: str
    severity_start: str
    severity_end: str

    healthy: str
    unhealthy: str

    # Active/paused colors for the watcher + timers toggles. "Greenish"
    # signals "running / fires alerts"; "reddish" signals "muted / paused".
    toggle_active: str
    toggle_active_hover: str
    toggle_paused: str
    toggle_paused_hover: str

    font_family: str
    font_family_display: str  # for big numbers / titles


NEUTRAL = Theme(
    name="neutral",
    bg="#0b0d12",
    panel_bg="#0f1218",
    card_bg="#141822",
    card_bg_hover="#1a1f2b",
    border="#222936",
    text="#e6e8eb",
    text_dim="#7a8294",
    text_label="#c8ccd5",
    accent="#5d8aff",
    state_active="#34d399",
    state_upcoming="#6b7280",
    state_ending="#f59e0b",
    state_unknown="#6b7280",
    severity_warning="#f59e0b",
    severity_start="#34d399",
    severity_end="#94a3b8",
    healthy="#34d399",
    unhealthy="#ef4444",
    toggle_active="#3a8c3f",
    toggle_active_hover="#4ea84f",
    toggle_paused="#a83232",
    toggle_paused_hover="#c64242",
    font_family="'Inter', 'Segoe UI', 'Cantarell', sans-serif",
    font_family_display="'Inter', 'Segoe UI', 'Cantarell', sans-serif",
)


# Placeholder Diablo-themed palette — refine when we settle on exact colors.
# Hex values are intentionally dramatic crimson + gold + parchment so it reads
# clearly during dev; tone down or swap for art-direction-approved values later.
DIABLO = Theme(
    name="diablo",
    bg="#0a0604",
    panel_bg="#120a07",
    card_bg="#1c100a",
    card_bg_hover="#26160e",
    border="#3a1f12",
    text="#e8d4a0",
    text_dim="#8a6f48",
    text_label="#b8956b",
    accent="#c9a14a",
    state_active="#c9a14a",      # gold = active
    state_upcoming="#6b3a1f",    # dim ember
    state_ending="#d97706",      # bright ember
    state_unknown="#3a2418",
    severity_warning="#d97706",
    severity_start="#c9a14a",
    severity_end="#6b3a1f",
    healthy="#c9a14a",
    unhealthy="#7a1f1f",
    # Diablo-flavored: muted poison/forest green when active, deep crimson
    # when paused. Both readable on the dark crimson card background.
    toggle_active="#4a7c2e",
    toggle_active_hover="#5e9a3c",
    toggle_paused="#7a1f1f",
    toggle_paused_hover="#a02a2a",
    font_family="'Cinzel', 'EB Garamond', 'Caudex', 'Inter', serif",
    font_family_display="'Cinzel Decorative', 'Cinzel', 'EB Garamond', serif",
)


THEMES = {NEUTRAL.name: NEUTRAL, DIABLO.name: DIABLO}


def state_color(theme: Theme, state: EventState) -> str:
    return {
        EventState.ACTIVE: theme.state_active,
        EventState.UPCOMING: theme.state_upcoming,
        EventState.ENDING_SOON: theme.state_ending,
    }.get(state, theme.state_unknown)


def state_label(state: EventState) -> str:
    return {
        EventState.ACTIVE: "Active",
        EventState.UPCOMING: "Upcoming",
        EventState.ENDING_SOON: "Ending",
    }.get(state, "Unknown")


def style_qss(theme: Theme) -> str:
    return f"""
* {{ font-family: {theme.font_family}; color: {theme.text}; }}

QMainWindow, QWidget#root {{ background: {theme.bg}; }}

QWidget#panel {{ background: {theme.panel_bg}; }}

QFrame#card {{
    background: {theme.card_bg};
    border: 1px solid {theme.border};
    border-radius: 10px;
}}

QLabel#kindName {{
    color: {theme.text};
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}

QLabel#countdown {{
    font-family: {theme.font_family_display};
    font-size: 26px;
    font-weight: 700;
    color: {theme.text};
    margin: 0;
    padding: 0;
}}

QLabel#labelExtra {{
    color: {theme.text_dim};
    font-size: 11px;
    font-style: italic;
    margin: 0;
    padding: 0;
    min-height: 14px;
}}

QLabel#stateLabel {{
    color: {theme.text_label};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}}

QLabel#footerText {{
    color: {theme.text_dim};
    font-size: 11px;
}}

QLabel#footerHealth[healthy="true"] {{ color: {theme.healthy}; }}
QLabel#footerHealth[healthy="false"] {{ color: {theme.unhealthy}; }}

QLabel#headerTitle {{
    color: {theme.text};
    font-family: {theme.font_family_display};
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 1.5px;
}}

QLabel#headerSub {{
    color: {theme.text_dim};
    font-size: 11px;
}}

QPushButton#pauseButton {{
    border: 2px solid transparent;
    border-radius: 5px;
    padding: 4px 12px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    min-width: 64px;
}}
QPushButton#pauseButton[paused="false"] {{
    background: {theme.toggle_active};
    border-color: {theme.toggle_active};
    color: #f3f7ed;
}}
QPushButton#pauseButton[paused="false"]:hover {{
    background: {theme.toggle_active_hover};
    border-color: {theme.toggle_active_hover};
}}
QPushButton#pauseButton[paused="true"] {{
    background: {theme.toggle_paused};
    border-color: {theme.toggle_paused};
    color: #f5e6e6;
}}
QPushButton#pauseButton[paused="true"]:hover {{
    background: {theme.toggle_paused_hover};
    border-color: {theme.toggle_paused_hover};
}}

QPushButton#contextBadge {{
    background: {theme.card_bg};
    color: {theme.text_dim};
    border: 1px solid {theme.border};
    border-radius: 4px;
    padding: 3px 10px;
    font-family: {theme.font_family};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}}
QPushButton#contextBadge:hover {{
    background: {theme.card_bg_hover};
    color: {theme.text};
}}
QPushButton#contextBadge[context="in_combat"] {{
    color: {theme.toggle_active};
    border-color: {theme.toggle_active};
}}
QPushButton#contextBadge[context="disabled"] {{
    color: {theme.toggle_paused};
    border-color: {theme.toggle_paused};
}}
QPushButton#contextBadge[override="on"] {{
    background: {theme.toggle_active};
    color: #f3f7ed;
    border-color: {theme.toggle_active};
}}
QPushButton#contextBadge[override="off"] {{
    background: {theme.toggle_paused};
    color: #f5e6e6;
    border-color: {theme.toggle_paused};
}}

QToolButton#eventMute {{
    background: {theme.card_bg_hover};
    color: {theme.text_label};
    border: 1px solid {theme.border};
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1px;
    min-width: 28px;
    min-height: 14px;
}}
QToolButton#eventMute:hover {{
    background: {theme.border};
}}
QToolButton#eventMute[muted="true"] {{
    background: {theme.toggle_paused};
    color: #f5e6e6;
    border-color: {theme.toggle_paused};
}}

QFrame#hotkeyBar {{
    background: {theme.panel_bg};
    border-top: 1px solid {theme.border};
}}

QFrame#keyCap {{
    background: transparent;
}}

QPushButton#microToggle {{
    background: {theme.card_bg};
    color: {theme.text_dim};
    border: 1px solid {theme.border};
    border-radius: 4px;
    font-size: 10px;
    font-weight: 700;
    padding: 0;
}}
QPushButton#microToggle:hover {{
    background: {theme.card_bg_hover};
}}
QPushButton#microToggle[active="true"] {{
    background: {theme.toggle_active};
    color: #f3f7ed;
    border-color: {theme.toggle_active};
}}
QPushButton#microToggle:disabled {{
    color: {theme.border};
}}

QLabel#slotStatus {{
    color: {theme.toggle_paused};
    font-size: 8px;
    font-weight: 700;
    letter-spacing: 1px;
}}

QTabWidget#mainTabs::pane {{
    background: {theme.panel_bg};
    border: 1px solid {theme.border};
    border-radius: 4px;
    margin-top: -1px;
}}
QTabWidget#mainTabs QTabBar {{
    qproperty-drawBase: 0;
}}
QTabWidget#mainTabs QTabBar::tab {{
    background: transparent;
    color: {theme.text_dim};
    border: 1px solid transparent;
    border-bottom: 1px solid {theme.border};
    padding: 6px 18px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
    margin-right: 2px;
}}
QTabWidget#mainTabs QTabBar::tab:hover {{
    color: {theme.text};
}}
QTabWidget#mainTabs QTabBar::tab:selected {{
    background: {theme.panel_bg};
    color: {theme.accent};
    border: 1px solid {theme.border};
    border-bottom: 1px solid {theme.panel_bg};
}}

QWidget#tabBody {{ background: {theme.panel_bg}; }}

QLabel#buildLabel {{
    color: {theme.text_dim};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
}}
QComboBox#buildCombo {{
    background: {theme.card_bg};
    color: {theme.text};
    border: 1px solid {theme.border};
    border-radius: 5px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
    min-height: 20px;
}}
QComboBox#buildCombo:hover {{
    background: {theme.card_bg_hover};
    border-color: {theme.accent};
}}
QComboBox#buildCombo::drop-down {{
    border: none; width: 22px;
}}
QComboBox#buildCombo::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {theme.text_dim};
    margin-right: 8px;
}}
QComboBox#buildCombo QAbstractItemView {{
    background: {theme.panel_bg};
    color: {theme.text};
    border: 1px solid {theme.border};
    selection-background-color: {theme.card_bg_hover};
    selection-color: {theme.text};
    padding: 4px;
}}

QFrame#buildBanner {{
    background: {theme.card_bg};
    border: 1px solid {theme.border};
    border-radius: 8px;
}}
QLabel#classSigil {{
    background: transparent;
}}
QLabel#className {{
    color: {theme.accent};
    font-family: {theme.font_family_display};
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QPushButton#buildUrlButton {{
    background: transparent;
    color: {theme.accent};
    border: 1px solid {theme.accent};
    border-radius: 4px;
    padding: 4px 14px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QPushButton#buildUrlButton:hover {{
    background: {theme.accent};
    color: {theme.bg};
}}

QPushButton#buildSync {{
    background: transparent;
    color: {theme.text_dim};
    border: 1px solid {theme.border};
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QPushButton#buildSync:hover {{
    color: {theme.accent};
    border-color: {theme.accent};
}}

QFrame#debugConsole {{
    background: transparent;
    border: none;
}}
QLabel#debugTitle {{
    color: {theme.text_dim};
    font-family: {theme.font_family_display};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
}}
QPushButton#debugClear {{
    background: transparent;
    color: {theme.text_dim};
    border: 1px solid {theme.border};
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QPushButton#debugClear:hover {{
    color: {theme.accent};
    border-color: {theme.accent};
}}
QLabel#comingSoonTitle {{
    color: {theme.accent};
    font-family: {theme.font_family_display};
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 4px;
}}
QLabel#comingSoonSub {{
    color: {theme.text_dim};
    font-size: 12px;
    letter-spacing: 3px;
    text-transform: uppercase;
}}
QLabel#comingSoonBlurb {{
    color: {theme.text_label};
    font-size: 12px;
    line-height: 1.5;
}}

QPlainTextEdit#debugView {{
    background: {theme.bg};
    color: {theme.text_dim};
    border: 1px solid {theme.border};
    border-radius: 4px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 10px;
    padding: 4px 6px;
}}
"""
