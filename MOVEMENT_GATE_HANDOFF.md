# Movement-Gated Rules — Implementation Handoff

> **For:** Claude Code (or whoever's at the keyboard).
> **From:** the design conversation that ended right before you sat
> down. This file is the contract for the change. Read it top to
> bottom before editing anything.
>
> **The user's pain:** uses W/A/S/D to move. Auto-cast rules that
> press skills which interrupt movement (windups, hard-casts, mobility
> abilities with animation locks) make the character feel hitchy /
> jerky when they fire while the user is actively trying to move.
>
> **The fix:** a new pair of rule conditions that gate firing on
> "are W/A/S/D currently held?" — `MOVEMENT_KEY_HELD` and
> `MOVEMENT_KEY_NOT_HELD`. The user adds `MOVEMENT_KEY_NOT_HELD` to
> any rule whose press would interrupt movement; the engine waits to
> fire that rule until they let go of WASD.

---

## Design decisions (locked — don't relitigate)

These three were explicit user choices. Don't second-guess them.

1. **Model as new condition types, not a per-rule flag.**
   Composable with the other 9 conditions. Matches the existing
   `SLOT_STATE_IS` / `SLOT_STATE_IS_NOT` precedent (two enum values
   for the held / not-held cases, not one with a boolean `value`).

2. **WASD list is hardcoded.** No Profile UI, no per-build override.
   Single constant at the top of the monitor module; edit there if a
   future user ever needs ESDF or arrow keys.

3. **In-flight combo steps complete during movement.** Only the
   initial press is gated. This falls out for free from the
   composable design: the user adds `MOVEMENT_KEY_NOT_HELD` to the
   rule's top-level conditions, not to each combo step. The engine
   already evaluates conditions at the layer they're attached to.

## Other decisions (made during design — confirmed by user)

- **Fail open on Wayland-without-XWayland.** If `pynput.keyboard.Listener`
  fails to start (the same scenario `hotkey.py` already handles for
  the F9 toggle), `is_moving()` returns `False` forever. Rules with
  `MOVEMENT_KEY_NOT_HELD` fire normally (as if the user is never
  moving); rules with `MOVEMENT_KEY_HELD` never fire. The gate
  becomes a no-op rather than silently locking out auto-cast.

- **Modifier-filtered key tracking.** Ctrl / Alt / Cmd / Super
  combined with W/A/S/D does NOT count as movement. Protects
  against Ctrl-Tab-style shortcuts. (The user noted Ctrl+WASD won't
  happen in their workflow; included anyway because it's free
  defense-in-depth.)

- **Injectable callable for tests.** The engine takes a
  `movement_monitor: Callable[[], bool] | None` parameter alongside
  the existing `boss_detector` — same pattern. Tests pass a lambda;
  the daemon passes the real monitor's `.is_moving` method.

## What this is NOT

- **Not a fourth throttle gate.** It's a condition, evaluated in the
  existing condition-AND pass before any throttle logic. The three
  throttle gates in `rule_engine_v2.py` (DEBOUNCE_MS, per-skill
  recast_ms, per-rule cooldown_seconds) stay untouched. See
  `INSTRUCTIONS.md` (the project's working instructions) — hard
  rule #3 about throttle ordering.
- **Not a new Rule schema field.** No `interrupts_movement: bool` on
  `Rule`. Pure additive enum + engine handling. Builds without the
  condition deserialize and run exactly as before.
- **Not a new dependency.** Reuses `pynput`, already in the dep
  list.
- **Not configurable via Profile.** The user said hardcoded W/A/S/D
  is fine.

---

## Files to change

Apply in this order. Backend before UI per the working instructions.

| # | Path | What |
|---|---|---|
| 1 | `arpg_react/rules.py` | Add 2 enum values to `ConditionType` |
| 2 | `arpg_react/watchers/movement_monitor.py` | NEW file — the listener |
| 3 | `arpg_react/watchers/rule_engine_v2.py` | 3 edits: `EvalContext` field, 2 evaluator branches, constructor param |
| 4 | `arpg_react/daemon.py` | Instantiate, wire into engine, stop on shutdown |
| 5 | `tests/test_rule_engine_v2.py` | 4 new tests |
| 6 | `editor/static/app.js` | 2 entries in condition-type dropdown; hide target/value for them |
| 7 | `editor/static/index.html` | Hint copy in the RULES tab |

After backend: run `.venv/bin/python -m pytest tests/ -q`. After UI:
rsync only (no `app.py` change → no systemd restart).

---

## Change 1 — `arpg_react/rules.py`

Add two enum values to `ConditionType`. Place after `BOSS_DETECTED`.

```python
class ConditionType(str, Enum):
    HEALTH_BELOW = "HEALTH_BELOW"
    HEALTH_ABOVE = "HEALTH_ABOVE"
    RESOURCE_LEFT_BELOW = "RESOURCE_LEFT_BELOW"
    RESOURCE_LEFT_ABOVE = "RESOURCE_LEFT_ABOVE"
    RESOURCE_RIGHT_BELOW = "RESOURCE_RIGHT_BELOW"
    RESOURCE_RIGHT_ABOVE = "RESOURCE_RIGHT_ABOVE"
    SLOT_STATE_IS = "SLOT_STATE_IS"
    SLOT_STATE_IS_NOT = "SLOT_STATE_IS_NOT"
    BOSS_DETECTED = "BOSS_DETECTED"
    MOVEMENT_KEY_HELD = "MOVEMENT_KEY_HELD"          # NEW
    MOVEMENT_KEY_NOT_HELD = "MOVEMENT_KEY_NOT_HELD"  # NEW
```

No other changes to this file. `Condition`'s `target` and `value`
fields stay `Optional` — both are unused for these two condition
types.

---

## Change 2 — NEW FILE `arpg_react/watchers/movement_monitor.py`

Full file content:

```python
"""Tracks held-state of WASD movement keys.

Some auto-cast skills cancel the character's movement when fired (D4
mobility skills, POE2 hard-cast spells, anything with a windup). When
the user is actively trying to move with WASD, those presses make the
character feel jerky / unresponsive. This monitor lets rules opt out
of firing while the user is holding a movement key.

Used as a condition via ConditionType.MOVEMENT_KEY_HELD /
MOVEMENT_KEY_NOT_HELD in the rule engine. The most common pattern:
add MOVEMENT_KEY_NOT_HELD to any rule whose press would interrupt
movement, so the engine waits for the user to stop pressing WASD
before firing it.

Wayland fallback: if pynput can't start a global listener (pure
Wayland without XWayland), log once and have is_moving() return False
forever. Fail open — the gate becomes a no-op rather than locking
out auto-cast entirely.

Modifier filter: presses combined with Ctrl / Alt / Cmd / Super are
ignored. They aren't movement; they're keyboard shortcuts that
happen to involve a movement letter (Ctrl+W to close, Alt+S, etc.).
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

# Hardcoded — the working instructions called for no UI on this. Edit
# here if a future user needs ESDF or arrow keys.
MOVEMENT_KEYS: frozenset[str] = frozenset({"w", "a", "s", "d"})


class MovementMonitor:
    """Tracks which of MOVEMENT_KEYS are currently held.

    Thread-safe. Background pynput listener emits press/release events
    onto a worker thread; is_moving() reads the held set under a lock.
    """

    def __init__(self) -> None:
        self._held: set[str] = set()
        self._lock = threading.Lock()
        self._listener = None  # pynput.keyboard.Listener
        self._modifiers: set[str] = set()  # currently-held modifier keys

    def start(self) -> bool:
        """Start the background listener. Returns True on success, False
        on Wayland-without-XWayland or any other init failure. After a
        failed start, is_moving() returns False forever (fail-open)."""
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            log.warning("pynput unavailable; movement monitor disabled: %s", exc)
            return False

        try:
            listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            listener.daemon = True
            listener.start()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "global keyboard listener unavailable (Wayland w/o XWayland?); "
                "movement monitor disabled — rules with MOVEMENT_KEY_NOT_HELD "
                "will fire as if user is never moving: %s",
                exc,
            )
            return False

        self._listener = listener
        log.info("movement monitor active: tracking %s", sorted(MOVEMENT_KEYS))
        return True

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None
        with self._lock:
            self._held.clear()
            self._modifiers.clear()

    def is_moving(self) -> bool:
        """True iff at least one movement key is currently held (without
        a modifier). Cheap to call every tick."""
        with self._lock:
            return bool(self._held)

    # ------------------------------------------------------- internal

    @staticmethod
    def _modifier_name(key) -> str | None:
        """Return a stable name if `key` is a tracked modifier, else None."""
        from pynput import keyboard
        for name, members in (
            ("ctrl",  (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)),
            ("alt",   (keyboard.Key.alt,  keyboard.Key.alt_l,  keyboard.Key.alt_r)),
            ("cmd",   (keyboard.Key.cmd,  keyboard.Key.cmd_l,  keyboard.Key.cmd_r)),
        ):
            if key in members:
                return name
        return None

    def _on_press(self, key) -> None:
        mod = self._modifier_name(key)
        if mod is not None:
            with self._lock:
                self._modifiers.add(mod)
            return
        ch = getattr(key, "char", None)
        if ch is None:
            return
        ch = ch.lower()
        if ch not in MOVEMENT_KEYS:
            return
        with self._lock:
            # Don't count a movement key that arrived while a modifier
            # is held — that's a keyboard shortcut, not movement.
            if self._modifiers:
                return
            self._held.add(ch)

    def _on_release(self, key) -> None:
        mod = self._modifier_name(key)
        if mod is not None:
            with self._lock:
                self._modifiers.discard(mod)
            return
        ch = getattr(key, "char", None)
        if ch is None:
            return
        ch = ch.lower()
        if ch not in MOVEMENT_KEYS:
            return
        with self._lock:
            self._held.discard(ch)


class NullMovementMonitor:
    """Test/disabled stub — never reports movement."""

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def is_moving(self) -> bool:
        return False
```

---

## Change 3 — `arpg_react/watchers/rule_engine_v2.py`

### 3a. `EvalContext` gains an `is_moving` field

```python
@dataclass
class EvalContext:
    slot_states: dict[HotkeyKind, SlotState]
    resources: dict[str, float]      # name → 0..1
    boss_detected: bool
    is_moving: bool = False          # NEW — WASD currently held
```

Default `False` so any existing call site that doesn't pass it (none
should, after step 3c, but safety) gets the safe value.

### 3b. Two new branches in `evaluate_condition`

Add right before the final `return False`:

```python
    if t is ConditionType.BOSS_DETECTED:
        return ctx.boss_detected
    if t is ConditionType.MOVEMENT_KEY_HELD:        # NEW
        return ctx.is_moving
    if t is ConditionType.MOVEMENT_KEY_NOT_HELD:    # NEW
        return not ctx.is_moving
    return False
```

### 3c. Constructor takes a `movement_monitor` callable

Mirror the existing `boss_detector` pattern exactly.

```python
class RuleEngineV2:
    def __init__(
        self,
        build: BuildV2,
        dispatcher: AlertDispatcher,
        input_controller: InputController | None = None,
        sampler: PixelSampler | None = None,
        boss_detector: Callable[[], bool] | None = None,
        movement_monitor: Callable[[], bool] | None = None,   # NEW
    ) -> None:
        self._build = build
        self._dispatcher = dispatcher
        self._input = input_controller
        self._sampler = sampler
        self._boss_detector = boss_detector
        self._movement_monitor = movement_monitor             # NEW
        # ... rest of __init__ unchanged
```

### 3d. Populate `is_moving` at every `EvalContext` construction site

Search the file for `EvalContext(` (the constructor call). There are
several call sites — at least the main rule-walk path, the combo-step
pending-fire path, and `_maybe_log_diagnostic_snapshot`. Every one of
them needs the new kwarg added:

```python
ctx = EvalContext(
    slot_states=self.slot_states,
    resources=self.resource_fills,
    boss_detected=bool(self._boss_detector() if self._boss_detector else False),
    is_moving=bool(self._movement_monitor() if self._movement_monitor else False),  # NEW
)
```

Don't miss any. If a site builds `EvalContext` without `is_moving`,
that path will silently ignore movement state.

---

## Change 4 — `arpg_react/daemon.py`

Three edits to `run()`.

### 4a. Import the monitor

Near the top with the other `arpg_react.watchers.*` imports:

```python
from arpg_react.watchers.movement_monitor import MovementMonitor
```

### 4b. Construct and start it

After `input_controller = InputController()` (early in `run()`):

```python
movement_monitor = MovementMonitor()
movement_monitor.start()  # logs on failure; safe either way
```

### 4c. Pass `movement_monitor.is_moving` to the engine

Find where `RuleEngineV2(...)` is constructed and add the kwarg:

```python
engine = RuleEngineV2(
    build=active_build,
    dispatcher=dispatcher,
    input_controller=input_controller,
    sampler=...,
    boss_detector=...,
    movement_monitor=movement_monitor.is_moving,   # NEW
)
```

### 4d. Stop on shutdown

In the cleanup block (search for where `hotkey_controller.stop()` or
similar is called):

```python
movement_monitor.stop()
```

---

## Change 5 — `tests/test_rule_engine_v2.py`

Add to the conditions section (alongside `test_condition_boss_detected`):

```python
def test_condition_movement_key_held():
    ctx = EvalContext(slot_states={}, resources={}, boss_detected=False, is_moving=True)
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_HELD), ctx) is True
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD), ctx) is False


def test_condition_movement_key_not_held():
    ctx = EvalContext(slot_states={}, resources={}, boss_detected=False, is_moving=False)
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_HELD), ctx) is False
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD), ctx) is True
```

Add to the cast-types section (alongside the other CONDITIONAL /
COMBO tests):

```python
def test_rule_gated_by_movement_does_not_fire_while_moving():
    """Rule with MOVEMENT_KEY_NOT_HELD is held off while user is moving,
    then fires once movement stops."""
    inp = NullInputController()
    moving = {"v": True}

    rule = Rule(
        name="interrupt_skill", target=HotkeyKind.KEY_1,
        cast_type=CastType.CONDITIONAL, cooldown_seconds=0.0,
        conditions=[Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD)],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
        movement_monitor=lambda: moving["v"],
    )

    # Moving: rule blocked.
    eng.tick(NOW)
    assert inp.calls == []

    # Still moving a tick later: still blocked.
    eng.tick(NOW + timedelta(milliseconds=300))
    assert inp.calls == []

    # User releases WASD: rule fires.
    moving["v"] = False
    eng.tick(NOW + timedelta(milliseconds=600))
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1]


def test_in_flight_combo_steps_complete_during_movement():
    """Per the design: only the initial press is gated by the
    top-level MOVEMENT_KEY_NOT_HELD condition. Combo steps without
    the condition continue to fire even if the user starts moving
    after the chain was scheduled."""
    inp = NullInputController()
    moving = {"v": False}

    rule = Rule(
        name="combo", target=HotkeyKind.KEY_1, cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        cooldown_seconds=10.0,
        conditions=[Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD)],
        combo_steps=[
            ComboStep(slot=HotkeyKind.KEY_2, delay_ms=50),
            ComboStep(slot=HotkeyKind.KEY_3, delay_ms=80),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
        movement_monitor=lambda: moving["v"],
    )

    # Stationary: initial press fires, chain queues.
    eng.tick(NOW)
    # User starts moving partway through:
    moving["v"] = True
    eng.tick(NOW + timedelta(milliseconds=60))   # slot 2 due
    eng.tick(NOW + timedelta(milliseconds=140))  # slot 3 due

    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3]
```

Expected total test count after this change: prior count + 4.

---

## Change 6 — `editor/static/app.js`

Find the array/object that backs the condition-type dropdown
(search for `HEALTH_BELOW` near the condition rendering code, or for
`SLOT_STATE_IS_NOT`). Add two entries with friendly labels:

```js
{ id: "MOVEMENT_KEY_HELD",     label: "MOVING (W/A/S/D held)" },
{ id: "MOVEMENT_KEY_NOT_HELD", label: "NOT MOVING" },
```

In the per-condition render function: hide the `target` and `value`
input fields when the type is one of these two. Same pattern that
already exists for `BOSS_DETECTED` (which also doesn't use
`target`/`value`). If `BOSS_DETECTED` is rendered with a special-case
branch, extend that branch to cover the two new types too. If it's
done via a "uses target?" / "uses value?" lookup table, add the new
types as `false`/`false`.

---

## Change 7 — `editor/static/index.html`

In the RULES tab hint paragraph, append a sentence to the existing
description. Current text starts with "Each tick, rules are
evaluated top-to-bottom..." — add at the end:

> Use **NOT MOVING** as a condition on any rule whose press would
> interrupt your character's movement (windup skills, mobility
> abilities, hard-casts) — keeps things from feeling jerky while
> you're holding W/A/S/D.

---

## Deploy

After all backend changes pass tests:

```bash
.venv/bin/python -m pytest tests/ -q
# expect prior_count + 4
```

UI changes — `app.py` is NOT modified, so no systemd restart:

```bash
rsync -av editor/static/ 10.0.0.16:/home/jbaker/d4-rule-editor-app/static/
```

---

## How to verify it actually works in-game

1. Start the daemon: `arpg-react -v run --game d4` (or poe2).
2. Look for `movement monitor active: tracking ['a', 'd', 's', 'w']`
   in the log. If you see `movement monitor disabled` instead, the
   listener failed to start — the gate will no-op (fail-open), but
   the feature won't actually do anything. See "Things to flag"
   below.
3. In the editor, add a `MOVEMENT_KEY_NOT_HELD` condition to a rule
   that fires an interrupting skill.
4. Save, wait up to 60s for editor-sync to pull it (or restart the
   daemon).
5. In-game: hold W. The rule should NOT fire. Release W. The rule
   fires on the next tick where its other conditions are met.

---

## Things to flag if they come up

### Symptom: rule never fires

Check the daemon log for `movement monitor disabled` at startup. If
present, `pynput.keyboard.Listener` failed to start. This is the
same scenario `hotkey.py` handles for the F9 toggle. If F9 works but
the movement monitor doesn't, something's off — they share the same
pynput backend.

Per design: fail-open. `is_moving()` returns False forever, so
`MOVEMENT_KEY_NOT_HELD` always evaluates True (passes) and
`MOVEMENT_KEY_HELD` always False (blocks). If a rule with
`MOVEMENT_KEY_NOT_HELD` STILL doesn't fire, the cause is something
else (cooldown, other condition, slot not READY).

### Symptom: rule fires during brief WASD taps

`is_moving()` is sampled at tick time (250ms tick), not debounced. A
30ms tap between ticks might be missed entirely. The gate is meant
for *sustained* movement, not micro-taps. If this becomes a real
problem in practice, the fix is a "recently held" window — e.g.
`is_moving()` returns True for ~150ms after the last release.
**Don't add this preemptively.** Wait for user feedback.

### Symptom: WASD doesn't register but other keys do

Modifier filter is engaged. Check whether some other software has a
Ctrl/Alt held when it shouldn't (sticky-keys, an autohotkey-style
remapper, COSMIC compositor weirdness). The monitor refuses to count
WASD-with-modifier as movement.

### Symptom: test count is off by something other than 4

You probably missed an `EvalContext(` construction site in step 3d.
Grep `EvalContext(` in `rule_engine_v2.py` and add `is_moving=...`
to every match. The dataclass default of `False` keeps existing
tests passing — but if you also missed step 3a (the field), Pydantic
won't complain because EvalContext is a `@dataclass`, not a
BaseModel; instead you'll get an `AttributeError` at runtime when
`evaluate_condition` tries to read `ctx.is_moving`.

---

## Session handoff entry to write afterward

For the next `SESSION_2026-05-*.md` (today's date), record:

- Added `MOVEMENT_KEY_HELD` / `MOVEMENT_KEY_NOT_HELD` conditions.
- New `MovementMonitor` watcher tracks W/A/S/D via pynput; daemon
  starts it at boot, Wayland-w/o-XWayland degrades to no-op
  (logged once, fail-open).
- Modifier-filtered: Ctrl/Alt/Cmd+WASD does not count.
- Engine accepts injectable `movement_monitor: Callable[[], bool]`
  alongside `boss_detector`; `EvalContext` gained `is_moving: bool`.
- Editor RULES condition dropdown gets two new entries with friendly
  labels ("MOVING (W/A/S/D held)" / "NOT MOVING"); target+value
  fields hidden for them.
- 4 new tests; test count: prior + 4.
- No new dependency. No new field on `Rule`. Backward-compatible.

---

## Background — why these specific design choices

(For posterity. The user already agreed; this is here so you
understand the reasoning if you hit an edge case mid-implementation.)

**Why not a per-rule boolean flag?** Considered. Less composable.
The user picked the condition-based model — same dropdown, same
mental model as the existing 9 conditions. Adding a new field to
`Rule` would also require legacy-build migration; conditions are
just JSON in a list, so old builds round-trip fine.

**Why two enum values instead of one with a boolean `value`?**
Matches the project's own `SLOT_STATE_IS` / `SLOT_STATE_IS_NOT`
precedent. Reads better in the dropdown. Avoids the "value=True"
landmine on type-switches (the editor's condition-type switcher has
to default `value` per type; two enum values means no `value` at
all, which is cleaner).

**Why hardcode WASD instead of a Profile field?** User explicitly
chose this. They use WASD; the feature is for them. Adding it to
Profile means more UI surface, another migration of the per-game
profile schema, another thing for `matt` to configure. Skip until
someone actually needs ESDF.

**Why fail-open and not fail-closed?** The gate is a comfort
feature, not a safety mechanism. If we can't observe input, the
worst case of fail-open is "the user occasionally gets an
interrupting skill fired while moving" — which is exactly the
behavior they have today, pre-feature. Fail-closed would silently
lock out auto-cast entirely with no UI signal, which is worse.

**Why modifier-filter?** Free defense-in-depth. The user noted they
won't hit Ctrl+WASD in their workflow, but other ARPG keybinds
(future builds, other users) might involve Shift+W for sprint or
similar. Shift is intentionally NOT filtered — Shift+W is a common
"walk forward at sprint speed" combo and should still count as
movement.

**Why is the engine's monitor parameter a `Callable[[], bool]` and
not a `MovementMonitor` instance?** Lets tests inject a `lambda:
True` without instantiating the real monitor (which would try to
start a pynput listener). Same pattern as `boss_detector`. The
daemon passes `movement_monitor.is_moving` (a bound method, which IS
a `Callable[[], bool]`).

---

## Don't violate these (from the project's working instructions)

- **Pydantic v2.** Rule / Condition / EvalContext stay as they are.
  `EvalContext` is a `@dataclass` not a BaseModel — keep it that way.
- **`from __future__ import annotations`** at the top of any new
  module. The new `movement_monitor.py` already has it.
- **No new heavy deps.** Reuse pynput (already there).
- **`log = logging.getLogger(__name__)`** in library code, not
  `print`. The new module already does this.
- **Don't broaden detection beyond single-pixel.** This change
  doesn't touch the detector at all — it's an input-side feature,
  not a vision feature. Stays in spec.
- **Backend before UI.** Steps 1-5 first, run tests, then UI.

Good luck. If the test count comes out at prior+4 and the in-game
verification works, you're done.
