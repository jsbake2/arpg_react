# D4 In-Game Loot Filter — Decode Spike

> **Goal:** figure out what's inside D4's loot-filter export string.
> Until we know the format, we can't generate import codes from
> Maxroll/Mobalytics build URLs. Everything downstream is gated on this.

The whole spike is **~30 minutes of your time + one round-trip with me**.
If the format turns out to be `base64(zlib(JSON))` or similar, the
end-to-end "paste build URL → click GENERATE → import code on clipboard"
feature is a few days of work. If it's protobuf-with-checksums or
something custom, we pivot to plan B (printable filter recipe — see
bottom of this doc).

---

## What I need from you

Capture **five** export strings from D4 with progressively richer
content, exactly as listed below. We need the simplest possible filter
first — that's our Rosetta stone. Without a small filter to compare
against, you can't tell which bytes mean what.

### Game prep (one-time)

1. Launch D4 → **Options → Gameplay → Gameplay**.
2. Make sure **Advanced Tooltip Information** is `ON`. (Not strictly
   needed for filters, but required for any later OCR work — turn it on
   now so you don't forget.)
3. Disable **HDR** if it's on. (Same reason — affects screen reading.)
4. Open **Options → Gameplay → Loot Filters** to confirm you can reach
   the screen.

### Filter A — The Empty Baseline

1. **New Filter** → name it `SPIKE_A_EMPTY`.
2. Don't add any rules.
3. Click the three-dot menu next to the filter name → **Export**.
4. Paste into a plain-text editor (Notepad, gedit, kate — **not**
   Discord, **not** a browser, **not** Slack — those mangle invisible
   characters).
5. Save the string and a one-line description for me.

### Filter B — One Rule, One Condition

1. **New Filter** → name it `SPIKE_B_MIN`.
2. Add a single rule:
   - Action: **Hide**
   - Condition: **Item Type = Sword** (any single item type works)
3. Export. Save.

### Filter C — One Rule, Multiple Conditions

1. **New Filter** → name it `SPIKE_C_MULTI_COND`.
2. Add a single rule:
   - Action: **Hide**
   - Conditions:
     - **Item Type = Sword**
     - **Item Power ≤ 800**
     - **Rarity = Magic OR Common**
3. Export. Save.

### Filter D — Multiple Rules, Different Actions

D4's recolor takes a hex color. Use the **exact** hex codes below so
the bytes show up cleanly in the decoded output and are easy to diff
against the rest of the filter.

1. **New Filter** → name it `SPIKE_D_MULTI_RULE`.
2. Add three rules:
   - **Rule 1:** Hide — Item Type = Sword (no color involved)
   - **Rule 2:** Recolor `#FFD700` (CSS "gold") — Rarity = Legendary
   - **Rule 3:** Recolor `#FF3030` (a clear red) — Greater Affixes ≥ 1
3. Export. Save.

The picked hexes give us byte sequences `FF D7 00` and `FF 30 30` —
distinctive enough to locate the color field in the decoded output.

### Filter E — Affix Conditions (the real test)

1. **New Filter** → name it `SPIKE_E_AFFIX`.
2. Add one rule:
   - Action: **Show**
   - Conditions:
     - **Item Type = Helmet**
     - **Has Required Affixes:** pick any two specific affixes
       (e.g. *Maximum Life*, *Cooldown Reduction*) — write down which
       two you picked
     - **Has Optional Affixes:** pick one (e.g. *Critical Strike Chance*)
3. Export. Save.

### Round-trip check (so we know the strings are good)

For Filter E only:

1. Create another **New Filter** named `SPIKE_E_REIMPORT`.
2. Paste in the export string for `SPIKE_E_AFFIX`.
3. Confirm D4 accepts it and the rules + conditions match what you set.
4. If D4 rejects the import, you copied through Discord/a browser by
   accident. Re-export, paste through a plain-text editor, retry.

---

## What to send me

Reply in our session with:

```
=== Filter A (empty baseline) ===
description: empty filter, no rules
string:
<paste the exported string here>

=== Filter B (1 rule, 1 condition: Hide Item Type = Sword) ===
string:
<paste>

=== Filter C (1 rule, 3 conditions: Hide Sword + IP≤800 + Magic|Common) ===
string:
<paste>

=== Filter D (3 rules, mixed actions: Hide Sword + Recolor #FFD700 Legendary + Recolor #FF3030 GA≥1) ===
string:
<paste>

=== Filter E (affix conditions: helmet + 2 required + 1 optional) ===
description: required affixes = <which ones>; optional = <which one>
string:
<paste>

=== Round-trip ===
Filter E re-imported successfully? yes / no
```

Drop them straight into the chat — **do not** put them in a code fence
that might add backticks; just plain paste each one on its own block.

---

## What I'll do with them

1. **Try base64 decode** — most game configs are base64. If that yields
   nothing, try url-safe base64 (`-_` instead of `+/`).
2. **Check magic bytes** — `1f 8b` = gzip, `78 01/9c/da` = zlib,
   `0a` start could be protobuf, `7b` (`{`) = plain JSON.
3. **Diff the empty filter against the 1-rule filter** — bytes that
   differ are where rules live. Pin the rule structure.
4. **Diff filters with the same rule but different conditions** —
   isolate condition encoding.
5. **Check for a length prefix or version byte** at the start.
6. Report back: format, where each piece lives, and whether we can
   construct round-tripping strings ourselves.

If the format is decodable, the next phase is the Maxroll/Mobalytics
URL parser → D4 filter rule generator → encoder pipeline.

If it's not decodable (protobuf-with-checksums, encrypted, or has
hashes we can't replicate), I'll switch to plan B below.

---

## Plan B if encoding turns out to be hostile

Generate a **printable text filter recipe** instead — a one-page
document that lists exactly which conditions and affixes to set, in
the order to enter them, derived from the build URL. You'd open D4's
filter UI and tap them in manually. Takes ~2 minutes per build vs the
current ~10–15 minutes of cross-referencing Maxroll/Mobalytics by
hand. Less magical than auto-generating an import code, but works
forever and never breaks when Blizzard updates the format.

Either way, the build-URL → affix-priority parser is real value. We
ship that regardless of how the encoding spike ends.

---

## What you don't need to do

- Don't try to decode the strings yourself.
- Don't share them in Discord (encoding mangling) — keep them in our
  session or in a plain text file.
- Don't worry about uniques / sigils / talismans / paragon for the
  spike. We only need the rule + condition + action vocabulary that
  Filters A–E exercise. Once those decode, the rest follows.
