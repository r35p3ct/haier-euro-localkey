# Compact-12 secondary controls — confirmed on hardware (AAC1UKZ01)

This note records everything reverse-engineered about the **compact-12** wire family
(117-byte report, uPlusId `00000000000000008080000000041410`, 509 catalogue products)
while bringing up a specific unit — product model **AAC1UKZ01**, a cooling-only split
unit. It complements the upstream `PROTOCOL.md` and the comments in
`vendor/haismart_hrdp/wire_models.py`; the code is the source of truth, this is the
story and the evidence.

## What the unit is

| | |
|---|---|
| Product / cloud model | AAC1UKZ01 (deviceType `0201203a`) |
| Wire family | compact-12 — one attribute per word, sensors interleaved in the same 12-word array |
| Report | 117 bytes; indoor temp w1, mode w6, fan w7, vanes w8, state bits w9/w10, setpoint w12 |
| Group-set write | `4d5f` over the whole 12-word array, read-modify-write from a live baseline |
| Single-parameter writes | paired on/off EPP commands per attribute (`healthMode` = `4d09`/`4d08`) |
| Cloud model verdict | screenDisplayStatus / silentSleepStatus / muteStatus / rapidMode are **visible + writable**, electricHeatingStatus / freshAirStatus **invisible** |

## The "capability flags" were never capability flags

The published family map labels three word-10 bits 有无 ("has / has-not"): 强力有无,
静音有无, 屏显有无. Early captures read them all `0`, so they landed in the decoder as
`cap_strong` / `cap_quiet` / `cap_display` — assumed to be static hardware-capability
bits, decoded for diagnostics only, offered by nobody.

A **remote-control sweep** (press one remote button at a time, diff the live report
every ~2 s) proved each of them is **live state**:

```
14:08:27  cap_strong   false -> true     # ТУРБО engaged
14:08:48  cap_strong   true  -> false    # ТУРБО cancelled
14:08:51  healthy_wind_up/down flip      # Комфортный поток direction switch
14:08:55  cap_quiet    false -> true     # QUIET engaged
14:09:07  cap_display  false -> true     # panel lamp OFF   <-- inverted!
14:09:14  cap_display  true  -> false    # panel lamp back ON
```

State propagates from the IR remote into the WiFi report within seconds — the unit
reports its own state changes no matter who commanded them.

## Write path: the group-set already carries these bits

compact-12's `4d5f` op is a read-modify-write of the full 12-word array seeded from the
current report. Adding a field mapping therefore needs no new protocol mechanism — only
a confirmed position. Each candidate was verified with a real group-set write followed
by a fresh read-back:

| Field | Position | Wire sense | Result |
|---|---|---|---|
| `rapidMode` (Турбо) | w10 bit1 | 1 = on | ✅ engages/clears, no refusal |
| `muteStatus` (Тихо) | w10 bit2 | 1 = on | ✅ |
| `healthyWindUp` (Комфортный поток ↑) | w10 bit3 | 1 = on | ✅ |
| `healthyWindDown` (Комфортный поток ↓) | w10 bit4 | 1 = on | ✅ but see exclusion below |
| `screenDisplayStatus` (Подсветка) | w10 bit5 | **inverted**: 1 = display OFF | ✅ |
| `sleepCurve` (眠线, w9 bit14) | — | — | ❌ group-set write silently ignored |

Two behavioural quirks found during testing:

* **Mutual exclusion.** An op carrying both airflow bits set (`w10 = 0b11000`) was
  *ignored* — the unit kept the old value. One bit per op works; the switch layer
  therefore clears the sibling in the same op when engaging one direction.
* **Inverted lamp.** The display flag reads `1` while the panel lamp is OFF. Encoded as
  a new `WriteField.kind == "inverted"` so HA's switch shows and commands the function,
  not the flag.

## Single-parameter controls on this unit

| Attribute | Commands | Verdict on AAC1UKZ01 |
|---|---|---|
| `healthMode` (Здоровье) | `4d09` on / `4d08` off, state w9 bit3 | ✅ works, toggle + read-back confirmed repeatedly |
| `selfCleaningStatus` | `4d26` start-only, state w9 bit2 | offered (button), cycle runs unattended |
| `electricHeatingStatus` | `4d05`/`4d04` | ❌ unit refuses the command — invisible in its cloud model, hardware absent |
| `freshAirStatus` | `4d1f`/`4d1e` | ❌ refused likewise |
| child lock `4d18`/`4d19`, humidify `4d1c`/`4d1d` | documented pairs | not offered: the app renders no widget for either |

## What the app does and doesn't have

The phone app renders NO buttons for turbo/quiet/sleep/display on this product — those
functions exist only on the **IR remote** (some under its slide cover). That is why no
app capture can reveal them; the remote sweep plus direct group-set probing is what
settled it. The cloud model still lists the attributes visible+writable, which matches
the hardware accepting our writes even though the app never sends any.

## How this was verified (reproduce it yourself)

1. Run a status watcher that polls `async_read_status()` every ~2 s and diffs the
   decoded dict (any compact-12 field flip prints with a timestamp).
2. Press each remote button once, ~15 s apart; note which key flips.
3. Build a group-set op with the discovered position via
   `WireModel.encode_control(baseline_words(report), {field: 0 or 1})` and send it with
   `async_send_op(...)`; read back after ~3 s.
4. Expect: bit flips both ways for writable fields; silence/refusal otherwise.

## Files changed in this repo

| File | Change |
|---|---|
| `vendor/haismart_hrdp/wire_models.py` | `_COMPACT12_WRITE` += rapid/mute/lamp(+inverted kind)/airflow pair; new `"inverted"` encode+decode kind; comments corrected (cap_* are state, not capability) |
| `switch.py` | comfort-flow switches with mutual exclusion (`exclusive_with`); setup comment updated |
| `binary_sensor.py` | five promoted indicators removed from `COMPACT12_INDICATORS` |
| `coordinator.py` | docstrings updated to match reality |
| `strings.json` / `translations/ru.json` | names for the new switches; stale indicator translations removed |
