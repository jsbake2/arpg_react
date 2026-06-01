"""Curated buff library — the canonical list of buffs the watcher can match.

Builds reference library entries by id (`coe`, …); for entries that have
sub-variants (CoE's six elements) the build also names which variants
to alert on. Templates ship bundled in `arpg_react/resources/buffs/`,
so nothing user-uploaded round-trips through build JSON.

The split between `library.py` (catalog + element metadata) and
`templates.py` (PNG decoding + cached numpy arrays) keeps the catalog
importable from places that don't want to pull numpy.
"""

from arpg_react.buffs.library import (
    BUFF_LIBRARY,
    LibraryBuff,
    LibraryBuffElement,
    library_entry,
    seen_name,
)

__all__ = [
    "BUFF_LIBRARY",
    "LibraryBuff",
    "LibraryBuffElement",
    "library_entry",
    "seen_name",
]
