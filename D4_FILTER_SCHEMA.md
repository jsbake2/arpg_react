# D4 In-Game Loot Filter — Reverse-Engineered Schema

> Reverse-engineered from the spike samples in `D4_FILTERS/` on
> 2026-05-08. Format: **base64-encoded protobuf**, no compression,
> no checksums. All five sample exports decode and re-encode
> byte-identical via `tools/d4_filter_codec.py`.

## Format pipeline

```
clipboard string  →  base64 decode  →  protobuf bytes  →  Filter message
                                                           ↓
clipboard string  ←  base64 encode  ←  protobuf bytes  ←  Filter message
```

That's it. No magic header, no version byte, no hash. The bytes you
see after base64 decoding are exactly a serialized protobuf message.

## Top-level message: `Filter`

| field | wire type   | name        | notes |
|-------|-------------|-------------|-------|
| 1     | repeated msg | `rules`     | empty when filter has no rules |
| 2     | string      | `name`      | filter display name |
| 3     | varint      | `slot_id`   | per-filter creation index (1, 2, 3…). On import D4 likely reassigns; needs verification. |
| 4     | varint      | `?`         | always `1` in samples — except SPIKE_B_MIN where it was `2`. Some flag (active/dirty?). Set to 1 by default; may need experimentation. |

## Nested message: `Rule` (Filter field 1)

| field | wire type | name      | notes |
|-------|-----------|-----------|-------|
| 2     | varint    | `action`  | `0` = Show, `2` = Recolor, `3` = Hide |
| 3     | fixed32   | `color`   | ARGB packed. `0xFFFF0000` is the unset placeholder used for Hide/Show rules. For Recolor: `0xFFFF + RR + GG + BB` little-endian on the wire — e.g. `#FFD700` → `0xFFFFD700`. |
| 4     | repeated msg | `conditions` | at least one required by D4 UI |
| 5     | varint    | `enabled` | always `1` in samples |

### `action` enum

| value | meaning |
|-------|---------|
| 0     | Show (overrides hides) |
| 2     | Recolor |
| 3     | Hide |

> **TODO:** other actions exist in the UI (e.g. Hide All, Hide Text Label). Capture them later to fill in the rest.

### `color` field

ARGB stored as little-endian fixed32. The two leading bytes of the
ARGB representation are alpha + red. For an RGB color `#RRGGBB`:

```
encoded = 0xFFFF0000 | (RR << 16) | (GG << 8) | BB
```

Wait — that's not right with what we observed. Re-check from samples:

| user picked | encoded fixed32 (little-endian on wire) |
|-------------|------------------------------------------|
| `#FFD700`   | `0xFFFFD700` (bytes `00 D7 FF FF`)       |
| `#FF3030`   | `0xFFFF3030` (bytes `30 30 FF FF`)       |

Reading the encoded value as ARGB high-to-low: `A=FF R=FF G=D7 B=00`
which equals `#FFD700` ✓ (and the alpha is always `FF`, opaque). So:

```
encoded = (0xFF << 24) | (0xFF << 16) | (GG << 8) | BB    # for "red" channel held at FF
```

Hmm — but that means the format never carries an actual red channel
distinct from `0xFF`. That can't be right. Either:

- D4's UI clamps the red channel to FF in the picker (unlikely);
- or the format is actually `0xAARRGGBB` and what we're seeing is
  `A=0xFF, RR=0xFF, GG=user_G, BB=user_B` which means the picker is
  storing the wrong byte and we got unlucky with two colors that
  share `RR=0xFF`;
- or the format is something more nuanced than ARGB.

Need a third sample with a non-`FF` red component (e.g. `#3030FF`
or `#00AA88`) to disambiguate. Cheap to test.

## Nested message: `Condition` (Rule field 4)

| field | wire type   | name        | notes |
|-------|-------------|-------------|-------|
| 1     | varint      | `type`      | see `condition_type` table below |
| 2     | repeated fixed32 | `ids`  | item-type IDs / affix IDs |
| 4     | varint      | `amount`    | rarity bitmask, GA count, affix min-count |
| 5     | varint      | `ip_max`    | Item Power upper bound (Item Power conditions only) |
| 6     | varint      | `?`         | seen on Greater-Affixes condition; possibly the comparison operator |

### `condition_type` enum

| value | meaning                        | other fields used |
|-------|--------------------------------|-------------------|
| 0     | Item Power range               | field 5 = upper bound (e.g. `800`); field 4 likely lower bound (untested) |
| 1     | Rarity                          | field 4 = bitmask (1=Common, 2=Magic, 8=Legendary; bits 2/4/5/6/7 = Rare/Unique/Mythic/Set/? — untested) |
| 4     | Greater Affixes                 | field 4 = count, field 6 = operator (≥ vs = vs ≤?) |
| 5     | Item Type                       | field 2 = single fixed32 item-type ID |
| 6     | Affixes group A                 | field 2 = list of affix IDs, field 4 = min count |
| 7     | Affixes group B                 | field 2 = list of affix IDs, field 4 = min count |

> **TODO:** {6,7} are "required" and "optional" in the UI; need a
> known-clean sample to label which is which.

> **TODO:** other condition types exist in the UI (codex upgrade,
> specific unique, talisman set bonus, item properties). Capture and
> classify.

## Sample data tables (incomplete — extend as we learn)

### Item type IDs

| ID         | item type |
|------------|-----------|
| `0x06d14c` | Sword (specific 1H or 2H — need to disambiguate) |
| `0x06d16e` | Helmet |

### Affix IDs

| ID         | affix |
|------------|-------|
| `0x001beace` | one of {Maximum Life, Cooldown Reduction} |
| `0x001bead8` | (need user to identify) |
| `0x001beab8` | (need user to identify) |

### Rarity bitmask bits

| bit | rarity |
|-----|--------|
| 0   | Common (Normal) |
| 1   | Magic |
| 2   | Rare (untested) |
| 3   | Legendary |
| 4   | Unique (untested) |
| 5   | Mythic Unique (untested) |
| 6   | (untested) |
| 7   | (untested) |

## Open questions before we can ship a build-URL → filter generator

1. **Full item-type ID table.** ~50 weapon/armor/jewelry types in D4. Need every ID. Likely lifted from `arpg_stuff/` calibration captures or scraped from the d4lf project's affix/item assets.
2. **Full affix ID table.** ~200+ affixes. d4lf has `assets/lang/enUS/affixes.json` with name→ID mapping; check whether the IDs match these protobuf encodings.
3. **Confirm `slot_id` (Filter field 3) behavior on import.** Encode a filter with `slot_id = 99` and try importing — does D4 take it as-is, reject, or reassign?
4. **Confirm `Filter field 4`** (the always-1-but-once-2 varint). Test with both values and see what changes in-game.
5. **Color channel format.** Capture `#3030FF` and `#00AA88` to nail down the byte layout.
6. **Condition types not yet seen** — capture one sample per UI condition we haven't covered (Codex upgrade, specific unique, talisman set, item properties).
7. **Required vs Optional affix type** — generate one filter via our encoder, compare against a known-clean D4 export, identify which of {6,7} is which.

## Tools

- `tools/d4_filter_codec.py` — decoder + encoder, no external deps. CLI:
  - `python tools/d4_filter_codec.py decode <file>`
  - `python tools/d4_filter_codec.py decode-all D4_FILTERS/`

All 5 spike samples round-trip byte-identical (verified 2026-05-08).
That means our encoder produces bytes indistinguishable from D4's, so
D4 should accept anything we generate that conforms to this schema.
