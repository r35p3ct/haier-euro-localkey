# Haier U+ / uHome (Europe) — local key for Home Assistant

Fetch the **local encryption key** for a Haier air conditioner that pairs with the
**Haier U+ / uHome / "Good Air"** app (`com.haier.uhome.haiergoodairabroad`), so you
can control it from Home Assistant **fully locally** — no cloud, no hardware mods.

The AC is then driven with the excellent
[`enapt/haismart-local`](https://github.com/enapt/haismart-local) integration, which
speaks the local **uSS/HRDP** protocol on TCP port 56800.

## Why this exists

Haier has several phone apps, and they do **not** share one cloud:

| App | Cloud | Local protocol | Use |
|---|---|---|---|
| **Haier U+ / uHome / Good Air** (Europe, RU etc.) | `uws-gea-euro.haieriot.net` | uSS/HRDP (encrypted) | **this repo** |
| Haier / Haismart (SE-Asia) | `uhome-sgp.haieriot.net` | uSS/HRDP (encrypted) | `haismart-local` (built-in) |
| SmartAir2 / Smart Clima (older) | `gw.haieriot.net` | unencrypted bstuff | `oxystin/homebridge-haier-air-conditioner` |
| hOn (mostly Europe) | — | no local port | `Andre0512/hon` |
| Haier 智家 (China) | — | — | `banto6/haier` |

The `haismart-local` integration can control **any** uSS/HRDP unit once you have the
key — but its built-in key fetch only targets the SE-Asia cloud. This script fills
that gap for the European cloud.

## The dongle (what "works")

- Wi-Fi module **MK-QTWIFI-04** (Realtek 8711AF), firmware **e_2.5.14** / `R_1.0.00`.
- Hostname `uplus-haier-ac-XXXXXX-v0`, speaks `UHOME_OVERSEAS_AC`.
- Older SmartAir2 modules (KZW-W002, firmware e_2.3.12) use the *unencrypted*
  protocol — they are **not** what this targets (see table above).

## Quick check

If `nc -z <ac-ip> 56800` succeeds, the local protocol is listening and this will work.

## Usage

### 0. Prerequisites

```bash
pip install httpx cryptography paho-mqtt
```

### 1. Get the app credentials

The app-level `appId`/`appKey` are identical for every install of the Haier U+ app —
they are **not** your account secret, but they are also not published here. Extract
them from the APK you already have installed:

1. Pull the APK off the phone (e.g. via `adb pull`) or download it from an APK mirror.
2. Decompile it with [`jadx`](https://github.com/skylot/jadx):
   ```bash
   jadx -d out base.apk
   ```
3. Open `out/resources/AndroidManifest.xml` and find the `meta-data` entries:
   ```xml
   <meta-data android:name="APP_ID"  android:value="MB-XXXXXXXX-0001"/>
   <meta-data android:name="APP_KEY" android:value="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"/>
   ```

### 2. Fetch the key

```bash
export HAISMART_APP_ID=MB-XXXXXXXX-0001
export HAISMART_APP_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

python3 haier_euro_keys.py --username you@example.com
```

The password is prompted for interactively. Output:

```
  Кондиционер
    device ID (MAC)  XXXXXXXXXXXX
    local key        xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    key version      1377
```

`--json` gives machine-readable output.

### 3. Add to Home Assistant

1. Install [`enapt/haismart-local`](https://github.com/enapt/haismart-local)
   (HACS custom repository or manual copy into `custom_components/haismart`).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Haismart** →
   **"I already have this unit's local key"**.
4. Enter the AC's IP, paste the `local key` from step 2.

Done — you get a `climate.*` entity plus temperature, power, fault and other sensors,
all polled locally every 30 s.

## Notes

- **The key rotates.** An online unit is issued a new key several times a day, which
  is the single most common cause of "it worked then stopped". Two ways to stop it:
  - add your Haier account to the integration once (it re-fetches automatically), or
  - block the AC's internet access at your router; a blocked unit's key stops changing,
    and your stored key stays valid indefinitely.
- Home Assistant and the AC must be able to reach each other on port 56800. If they are
  on different VLANs/subnets (e.g. IoT guest network), add a firewall rule allowing the
  traffic.
- The AC accepts **one local session at a time** (~17 s cap). Running two local Haier
  integrations against the same unit will make both misbehave.

## How it works

1. `GET /uam/v2/mgr/getPublicKey` → RSA public key.
2. `POST /uam/v2/user/loginEmailAcounnt` with RSA-encrypted email + password → `accessToken`.
3. `GET /uds/v1/protected/deviceinfos` → device list.
4. MQTT gateway `gw-gea-euro.haieriot.net:56789` (`devLocalkey` request) → `local_key`.

Everything is reverse-engineered from the official app v3.3.3; see the source for the
full header/signature logic.

## License

MIT — based on concepts from
[`enapt/haismart-local`](https://github.com/enapt/haismart-local) (MIT).
