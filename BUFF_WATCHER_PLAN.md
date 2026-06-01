# Buff Watcher — Implementation Plan

> **PIVOTED 2026-05-27 (later same day).** The user hated the
> upload-PNG-per-buff flow described below. We switched to a curated
> library: bundled templates ship with the package, builds reference
> entries by id, and CoE is the only entry for now with checkboxes
> for which elements to alert on. The relevant data model is now:
>
> ```python
> class LibraryBuffConfig(BaseModel):
>     id: str                  # "coe", ...
>     enabled: bool = True
>     elements: list[str] = [] # subset of variant keys from BUFF_LIBRARY[id]
> ```
>
> Live in `arpg_react/buffs/library.py`. New library entries get added
> in code, calibrated against bundled reference shots, locked down by
> a test, then shipped. No user-uploaded PNGs, no user-uploaded WAVs,
> no per-buff sound selector — every rising edge plays `ding.wav`.
>
> **Phases 1+2 still describe the working detection engine.**
> **Phase 3 (editor) was implemented in the library shape, not the
> upload shape below.**
> **Sections describing `reference_png_b64`, `custom_sounds`,
> `sound_id`, the BUFF_ACTIVE `buff_name = "CoE: Poison"` example,
> the upload dialog, and the Manage-Sounds modal are all OBSOLETE.**
>
> The historical contract is preserved below for context on why the
> matcher / calibration / IPC shape look the way they do.
>
> ---

> **Drafted:** 2026-05-27, during the same session that wired the D3 HP
> estimator. The user asked to stop here, drop the plan onto disk, and
> step through implementation in clean phases. This file is the contract.
> Read top to bottom before editing anything.
>
> **The user's pain:** D3 has cycling-buff mechanics — most notably
> Convention of Elements (CoE) which rotates Fire → Lightning → Cold →
> Physical → Holy → Poison on a ~4s/element, 24s loop. The user's
> Witch Doctor build wants to dump big-damage skills only during the
> POISON window. There is currently no way to surface "the buff just
> hit the wanted element" — they have to eyeball it.
>
> **The fix:** a new, *generic* "buff watcher" — captures a reference
> icon and an audible alert per buff the user cares about, scans the
> in-game buff row each tick, fires a rising-edge alert when the icon
> appears. CoE-Poison is one entry in the list; CoE-Fire is another
> they'd leave disabled. Same machinery handles shrine procs, NV stacks,
> In-geom, and anything else with a static icon that comes and goes.

---

## Why generic, not CoE-specific (LOCKED — don't relitigate)

The temptation is to model CoE as a first-class concept ("the CoE
detector knows there are 6 elements; you pick one"). We discussed
this and explicitly rejected it. Reasons:

1. **Same machinery for every other buff.** The list of "buffs the user
   might want to watch" is open-ended (shrine glow, every season-buff
   stack, Pylon procs in GR, Akarat's Champion almost-out, etc.). A
   CoE-specific detector helps zero of these.
2. **The data model collapses to one shape.** Each watched buff is
   `(name, reference_icon_png, alert_sound, enabled)`. CoE-Poison and
   "Power Pylon active" use the same row in the same table.
3. **Per-element reference captures are one-time friction.** Six clicks
   to set up CoE the first time vs. a lifetime of "did the border-color
   classifier misclassify lightning as cold today" maintenance.

If a future feature genuinely needs CoE semantics ("count consecutive
poison procs", "predict the next poison window for pre-casting"), we
add it then on top of this layer. v1 is just `seen → alert`.

---

## Locked design decisions (also don't relitigate)

1. **Detection = template matching in a search region.** The buff row
   reorders as buffs come and go, so fixed pixels won't work. Each
   tick we scan a horizontal strip (the buff row) for any of the
   user's captured templates.

2. **Numpy-backed SAD scan at 0.5× downsample.** *Updated 2026-05-27
   after Phase-2 calibration.* The original Pillow-only/0.25× plan
   was measured at **~2.4 s per tick** for 6 CoE templates against a
   ~430×45 downsampled strip — unshippable on the 500 ms tick. Numpy
   per-offset SAD (with early bail) brings it to ~160 ms total. The
   0.25× downsample also threw away the color detail that lets us
   distinguish CoE-Poison from CoE-Physical; 0.5× kept a ~0.02
   separation margin between self-match and best off-element match.
   Decision: numpy is in `pyproject.toml` deps now; `DOWNSAMPLE_FRACTION
   = 0.5`. Default `match_tolerance` dropped from 0.15 → 0.08.

3. **Rising-edge alerts only.** Fire once when a buff transitions from
   absent → present. Do *not* re-alert every tick the buff is visible
   (CoE poison is up for 4 seconds — that would be 8 beeps). The
   falling edge re-arms the trigger silently.

4. **Same throttle as D3 state detector — 500ms tick.** Reuses the
   existing screen grab; the buff watcher just gets a second crop out
   of the same `ImageGrab.grab()` the D3 state detector already takes.
   No second grab per tick.

5. **One reference image per buff entry — base64-inlined in the build
   JSON.** Simple, makes export/import work, no separate filesystem
   table. 50×50 PNG ≈ 2-5 KB → 4-8 KB base64. With ~12 buffs per build
   that's ~50-100 KB of JSON — acceptable.

6. **Search region is per-game, not per-build.** D3's buff row sits in
   one place; the user shouldn't have to redefine it per build. Game
   keymap config gains a `buff_row_bbox` field; users can override in
   PROFILE if they play at non-reference resolution and our scaler
   misses.

7. **Live "currently-seen buffs" exposed on `ContextFrame`.** Same
   place `slot_states` and `resources` live. The panel can render a
   small row of "buffs the watcher sees right now" for the user to
   confirm detection is working before they trust the alert.

## Resolved on 2026-05-27 (formerly open questions)

1. **Capture flow = manual upload.** User screenshots with OS tool,
   crops to PNG, uploads via the BUFFS tab. Zero daemon/IPC work.
   Live screenshot-pull is deferred to a future iteration if the
   manual flow gets tedious.

2. **Sound = short ping, with user upload.** v1 ships a bundled
   `ding.wav` chime as the default for buff alerts (NOT TTS — TTS
   every 24s is annoying for CoE-Poison). The editor BUFFS tab also
   lets users upload their own WAVs, which are stored per-build and
   selectable per buff. Bundled chime lives in
   `arpg_react/resources/sounds/`. Custom sounds round-trip in the
   build JSON as base64 (same precedent as icons).

3. **Per-build only.** Each build owns its own list of buff entries.
   The BUFFS tab is just that build's full list — toggle `enabled`
   per entry to switch buffs on/off without deleting the capture.
   No cross-build buff library in v1 (if it becomes painful to
   re-capture CoE for every build, we add a "duplicate from another
   build" import in v2).

4. **Rule-engine integration IS in scope for v1.** Add
   `ConditionType.BUFF_ACTIVE` (with a `target: str` field naming
   the buff by its `name`). Lets users write "fire Big Bad Voodoo
   when CoE-Poison is up" without manual reaction. Falls into the
   existing condition machinery cleanly — minimal new code.

---

## Data model

### `arpg_react/rules.py`

```python
class BuffWatcherConfig(BaseModel):
    """One buff the user wants to be alerted on.

    The reference icon is a small PNG (~40-60px square) captured from
    the in-game buff row. At runtime the watcher scans the row for
    this template; a match triggers a rising-edge alert.
    """
    name: str                          # "CoE: Poison", "Power Pylon", ...
    enabled: bool = True
    reference_png_b64: str             # base64-encoded PNG bytes
    # Match tolerance — lower = stricter. SAD threshold, 0.0..1.0;
    # default 0.15 means ≤15% mean absolute pixel difference at
    # downsampled resolution counts as a match. Tuned per-buff if
    # the default produces false positives.
    match_tolerance: float = 0.15
    # Sound to play when the alert fires.
    #   - "ding" (default) — bundled chime in resources/sounds/
    #   - "chime", "pop" etc. — other bundled sounds (add as needed)
    #   - "custom:<id>"     — references BuildV2.custom_sounds[<id>]
    #   - None              — silent (still surfaces in panel + IPC,
    #                          useful when only the rule engine cares)
    sound_id: str | None = "ding"


class BuildV2(BaseModel):
    # ... existing fields ...
    buffs: list[BuffWatcherConfig] = Field(default_factory=list)
    # User-uploaded WAVs available to buff watchers (and potentially
    # other alert kinds in future). Keyed by a slugified version of
    # the upload filename; value is base64-encoded WAV bytes. Size
    # cap enforced editor-side at 100 KB / sound, 5 sounds / build.
    custom_sounds: dict[str, str] = Field(default_factory=dict)
```

### `arpg_react/rules.py` — new condition type

```python
class ConditionType(str, Enum):
    # ... existing ...
    BUFF_ACTIVE = "BUFF_ACTIVE"      # true while the named buff is matched

class Condition(BaseModel):
    type: ConditionType
    target: HotkeyKind | None = None
    value: float | str | None = None
    # NEW: for BUFF_ACTIVE, holds the buff's `name`. Reusing `value`
    # for this would clash with its float-or-state semantics; explicit
    # field is cleaner. Other condition types ignore it.
    buff_name: str | None = None
```

Engine eval branch:

```python
if t is ConditionType.BUFF_ACTIVE:
    return c.buff_name in ctx.buffs_seen
```

`EvalContext` gains `buffs_seen: set[str] = field(default_factory=set)`,
populated by the daemon from the buff watcher's last evaluate() result.

### `arpg_react/config.py`

Game-keymap config gains a `buff_row_bbox` field per game:

```python
# In the per-game DEFAULT_KEYMAPS dict, alongside slot_keys:
"d3":   { ..., "buff_row_bbox": (1700, 30, 2540, 90) },    # top-right
"poe2": { ..., "buff_row_bbox": ( 700, 1320, 1900, 1380) }, # bottom-left
"d4":   { ..., "buff_row_bbox": (1100, 50, 2400, 130) },    # top-center
```

Numbers above are placeholders — measure each from real shots before
landing the change. Scaled via the same `scale_for` machinery the D3
detector already uses.

### IPC: `arpg_react/ipc/messages.py`

`ContextFrame` gains:

```python
buffs_seen: list[str] = field(default_factory=list)
```

The list is the *names* of currently-matched watcher entries (e.g.
`["CoE: Poison"]`). Panel renders this as a small pill row in the
status area.

A new alert kind for buff appearances. Reuse `AlertFrame` with a new
`kind` value (or `severity`); details in Runtime section.

---

## Runtime: `arpg_react/watchers/buff_watcher.py` (new file)

```python
class BuffWatcher:
    """Polls the configured buff-row region for user-captured icon
    templates and fires rising-edge alerts when one appears.

    Reuses the screen grab taken by D3StateDetector (or D4Detector)
    via an `on_grab(img)` callback the daemon wires up — no second
    ImageGrab per tick.
    """

    def __init__(
        self,
        buffs: list[BuffWatcherConfig],
        search_bbox: tuple[int, int, int, int],
        dispatcher: AlertDispatcher,
    ): ...

    def evaluate(self, img: Image.Image, now: datetime) -> list[str]:
        """Returns list of names currently matched. Fires rising-edge
        alerts via dispatcher as a side effect."""
```

### Matching algorithm (Pillow-only)

For each enabled buff:

1. Once per process load, downsample its reference PNG to 25% size
   and cache the raw pixel bytes (~12×12 for a 50×50 template).
2. Each evaluate(): crop the search region from the full grab,
   downsample to 25% size (~450×15 for a 1800×60 strip).
3. Slide the template across the strip at every offset; at each
   position compute mean absolute difference per channel summed
   across the template's pixels.
4. If any position scores below `match_tolerance * 255 * 3 *
   template_size`, the buff is "seen".

This is O(strip_w * strip_h * tpl_w * tpl_h) at downsampled scale.
With our numbers: 450 × 15 × 12 × 12 ≈ 970k arithmetic ops per buff
per tick. 12 buffs × 2 ticks/sec = 23M ops/sec. Python pure-int can
do this; if it's slow we drop to 20% downsampling or batch.

### Alert dispatch

New dispatcher method `dispatch_buff_seen(name, sound_id, custom_sounds)`:
- `sound_id = "ding"` (or other bundled name) → load WAV from
  `arpg_react/resources/sounds/<id>.wav` and play via the existing
  audio playback path (whichever lib the panel already uses for
  alarms — likely just `aplay`/`afplay` via subprocess, check the
  current dispatcher).
- `sound_id = "custom:<key>"` → decode base64 from
  `custom_sounds[<key>]`, write to a temp WAV, play.
- `sound_id is None` → silent. Still publishes the IPC frame so the
  panel shows "BUFF SEEN: <name>" visually and the rule engine sees
  the buff active.

Rising-edge tracked inside `BuffWatcher` via a `_last_seen: set[str]`.

### Bundled chime

`arpg_react/resources/sounds/ding.wav` — a short (≤300ms) sharp
high-pitched chime. Generated from a single sine pulse + envelope so
it cuts through gameplay audio without being grating. Generate during
Phase 1 with a small script (no need for an asset request to the
user); commit the WAV.

---

## Daemon wiring: `arpg_react/daemon.py`

In the main tick:

```python
# Existing
d3_reading = d3_state_detector.detect(now)
# New — pass the SAME grab the d3 detector took
if buff_watcher is not None and d3_state_detector.last_grab is not None:
    seen = buff_watcher.evaluate(d3_state_detector.last_grab, now)
    state["buffs_seen"] = seen
```

`D3StateDetector` will need a small change: keep `self.last_grab:
Image | None` populated each tick so the buff watcher can reuse it.
The grab is already happening; we're just retaining the reference
for ~250ms (cleared at next tick) instead of GC-ing immediately.

For D4 and POE2, the existing main `Detector` already does its own
grab via `ImageGrab.grab()` — we'd need the same "expose last grab"
hook on that detector. v1 ships D3-only; D4/POE2 wired in v1.1.

---

## Editor: `editor/static/` (new BUFFS tab)

### `index.html`

```html
<button class="tab" data-tab="buffs">BUFFS</button>
<!-- insert after RULES, before POTION -->

<section class="tabpanel" data-tab="buffs">
  <p class="hint">
    Watch in-game buff icons and get an alert when one appears.
    Capture each icon you care about (CoE-Poison, shrine procs,
    pylon glow), give it a name, and the daemon will scan the buff
    row each tick. <strong>Alerts fire once per appearance</strong> —
    no repeat spam while the buff is up.
  </p>

  <button id="addBuffBtn" class="btn">+ ADD BUFF</button>
  <div id="buffList"></div>     <!-- cards rendered by JS -->
</section>
```

### `app.js`

- `buffs` array on the active build, rendered as cards.
- Each card: thumbnail (decoded from `reference_png_b64`), name,
  enabled toggle, tolerance slider, sound selector dropdown, delete
  button. Sound dropdown contains: bundled sounds (hardcoded list:
  "ding", "chime", "pop") + every key in `custom_sounds` + "silent".
- "Add buff" dialog: PNG file picker → reads file → base64-encodes →
  prompts for name → pushes to `buffs` → autosave.
- Separate "Manage sounds" sub-section above the buff list (or in a
  small dialog): WAV file picker → enforces 100KB / 5-per-build cap
  → base64-encodes → stores under `custom_sounds[slug]` → preview
  play button. Deletes guard against in-use sounds (warn before
  removing a sound a buff card is configured to play).

### `app.css`

Reuse the existing `.rule-card` shape for visual consistency. New:
`.buff-thumb { width: 50px; height: 50px; image-rendering: pixelated; }`.

### `app.py`

No backend changes for v1 — `BuildV2` already round-trips through
the existing `GET/PUT /api/builds/{name}` endpoints; the new `buffs`
field passes through transparently.

---

## Tests

### `tests/test_buff_watcher.py` (new)

1. **Template match — positive case.** Crop a known region from a D3
   reference shot, use it as the template, assert evaluate() finds it
   at the same screen position.
2. **Template match — negative case.** Use a deliberately-unrelated
   template (e.g. an HP-orb crop), assert no match in the buff row.
3. **Rising-edge alert fires once.** Mock dispatcher; evaluate twice
   in a row with the same matched buff; assert dispatch called once.
4. **Falling edge re-arms.** Match → no-match → match: assert dispatch
   called twice.
5. **Disabled buffs are skipped.** Mark a buff disabled, confirm zero
   work done for it.

### `tests/test_d3_state.py` (extend)

Test that `D3StateDetector.last_grab` is populated after `detect()`
and is the same PIL Image used internally.

### Reference shots to capture

User to provide:
- One full-screen D3 shot showing the buff row with **just** CoE
  visible at each of the 6 elements (6 shots minimum).
- One shot with multiple stacked buffs (CoE + shrine + NV) so we
  can verify the watcher finds CoE even when it's not at position 0.

Drop into `arpg_stuff/d3/buffs/` as `coe-fire.png`, `coe-poison.png`,
…, `coe-with-shrine.png`.

---

## Cross-game parity

- **D3** — v1 ships here. Search region: top-right of screen.
- **POE2** — v1.1. Search region: bottom-center near the flasks.
  Buff icons same shape (square ~50px). Same machinery, just measure
  the region and add the entry to `DEFAULT_KEYMAPS`.
- **D4** — v1.1. Search region: top-center under the minimap. D4
  buffs are bigger (~80px) so we'd want per-game template size
  defaults, or normalize-then-match.

Update `MEMORY.md` parity note when D3 ships: "buff watcher D3-only
as of YYYY-MM-DD; POE2/D4 to follow."

---

## Phased rollout (the user's "step through cleanly")

### Phase 1 — Detection engine, no UI

- New `BuffWatcherConfig` + `custom_sounds` on `BuildV2`.
- New `ConditionType.BUFF_ACTIVE` + `Condition.buff_name`.
- New `buff_watcher.py` with `evaluate()` + matching algorithm.
- Bundled `resources/sounds/ding.wav` generated and committed.
- Dispatcher gains `dispatch_buff_seen` with bundled + custom sound
  resolution.
- Daemon wires it in for D3 only, reusing the D3 detector's grab.
- `EvalContext.buffs_seen` populated from the watcher's last result.
- `tests/test_buff_watcher.py` with synthetic templates + rising-edge.
- `tests/test_rule_engine_v2.py` extended with a `BUFF_ACTIVE` test.
- **Ship gate:** can hand-edit a build JSON to add a buff, run the
  daemon, hear the ding when the icon appears in-game, AND have a
  rule with `BUFF_ACTIVE` fire only during the buff window.

### Phase 2 — Capture reference shots **(DONE 2026-05-27)**

- ✅ User provided 6 CoE-element shots (`coe-{element}-blue-outline.png`)
  with cyan-outline cues marking the icon.
- ✅ Buff-row bbox calibrated from `buff-area-box-blue-outline.png`
  (cyan outline → `(875, 1200, 1735, 1290)` at 2560×1440).
- ✅ Six 56×55 CoE templates extracted to `arpg_stuff/d3/buffs/`.
- ✅ Cross-match grid verified: every diagonal cell matches, every
  off-diagonal cell rejects. Tolerance pinned at 0.08; downsample
  at 0.5.
- ✅ Numpy added as a dep (perf-driven, see locked decision #2).
- ✅ `tools/calibrate_buff_match.py` saved for re-tuning if templates
  drift; `tests/test_buff_watcher.py` now contains a parametrized
  real-shot lockdown so any matcher regression fails CI immediately.
- TODO (post-Phase-3): user captures non-CoE reference shots from
  in-game and uploads via the BUFFS tab once it ships.

### Phase 3 — Editor BUFFS tab

- HTML/JS/CSS for the tab.
- Add-buff dialog with PNG file-picker upload.
- "Manage sounds" sub-section with WAV upload, size cap, preview.
- Sound-selector dropdown on each buff card.
- Rsync to remote editor; smoke-test in browser.

### Phase 4 — Cross-game + polish

- D4 + POE2 search regions, smoke-test.
- Live "buffs_seen" pill on the panel.
- Update parity memory note.

Each phase is one focused session. Phases 1 and 2 are mandatory before
the feature is useful; phases 3 and 4 are quality-of-life.

---

## Out of scope (v1 — do NOT do)

- **Multi-buff combinator alerts** ("only beep when CoE-Poison AND
  shrine"). Single-buff rising-edge is enough for the user's case.
  (The rule engine *can* combine BUFF_ACTIVE with other conditions
  though — that comes for free.)
- **Live screenshot pull from daemon → editor.** Manual upload for
  v1. Live pull is its own design (websocket image stream + browser
  draw-box UI).
- **Auto-CoE-element-detection** (color-classify the icon). Generic
  template matching covers it.
- **Buff DURATION tracking** ("alerts when buff is about to expire").
  Different problem, different watcher.
- **Cross-build buff/sound library.** Each build owns its own lists.
  No "duplicate buff X from build Y" import flow yet.
- **Sound editor.** Custom WAVs are uploaded as-is — no trimming,
  volume, or generation tools in the editor.

---

## Files touched (estimated)

```
arpg_react/rules.py                    + BuffWatcherConfig, custom_sounds,
                                         BUFF_ACTIVE, Condition.buff_name
arpg_react/config.py                   + buff_row_bbox per game
arpg_react/watchers/buff_watcher.py    NEW
arpg_react/watchers/d3_state.py        + .last_grab field
arpg_react/watchers/rule_engine_v2.py  + BUFF_ACTIVE eval branch,
                                         EvalContext.buffs_seen
arpg_react/daemon.py                   + instantiate + wire watcher,
                                         feed buffs_seen into engine
arpg_react/alerts/dispatcher.py        + dispatch_buff_seen +
                                         sound resolution (bundled/custom)
arpg_react/ipc/messages.py             + ContextFrame.buffs_seen
arpg_react/resources/sounds/ding.wav   NEW (generated, ~300ms chime)
arpg_react/panel/widgets.py            + buffs-seen pill row (Phase 4)
editor/static/index.html               + BUFFS tab markup
editor/static/app.js                   + buff list, add-buff dialog,
                                         Manage Sounds sub-section
editor/static/app.css                  + .buff-thumb, .buff-card styles
tests/test_buff_watcher.py             NEW
tests/test_d3_state.py                 + last_grab assertion
tests/test_rule_engine_v2.py           + BUFF_ACTIVE eval test
arpg_stuff/d3/buffs/                   NEW dir, reference shots
MEMORY.md                              + buff-watcher parity note
```

No new Python deps. No new editor backend endpoints.

---

## When you sit down to code

Start with Phase 1. Don't touch the editor until the daemon-side
detection works against a hand-edited build JSON. The capture flow
is the bikeshed-iest part of this feature — design it once the
detection is proven, not before.

The CoE icon shape is consistent across all 6 elements (same ring,
same animation) — only the highlighted segment's color differs. This
means the 6 templates will overlap a lot and false-positive risk is
real. Phase 2 (calibration against real shots) is mandatory before
the user trusts this in a GR push. Plan accordingly.
