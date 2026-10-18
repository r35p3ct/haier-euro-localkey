#!/usr/bin/env python3
"""Fetch the local encryption key for a Haier U+ / uHome (Europe) air conditioner.

Haier's "Good Air" / Haier U+ app (package ``com.haier.uhome.haiergoodairabroad``)
uses a **different cloud** from the South-East Asia app that
`enapt/haismart-local <https://github.com/enapt/haismart-local>`_ targets. This
script reproduces the Europe login and the uSDK MQTT gateway ``devLocalkey``
request, and prints the ``local_key`` you paste into the Haismart integration's
*"I already have this unit's local key"* screen.

NOTE (2026-10): Haier disabled the UWS v2 login endpoints
(``/uam/v2/mgr/getPublicKey`` + ``/uam/v2/user/loginEmailAcounnt``) server-side;
they now return B00004 even for the official app. The login therefore goes
through the still-working ``uhome/acbiz/login`` endpoint (plaintext password,
md5 signature) and the resulting accessToken is accepted by the UWS device list.

The AC itself is then controlled fully locally over TCP port 56800 (uSS/HRDP
protocol) — no cloud, no hardware mods.

Credentials
-----------
The app-level ``appId`` / ``appKey`` are the same for every install of the app and
are not per-user secrets. They are intentionally NOT hardcoded here. Provide them
one of two ways:

* environment variables ``HAISMART_APP_ID`` / ``HAISMART_APP_KEY``, or
* ``--app-id`` / ``--app-key`` command-line flags.

See the README ("Getting the app credentials") for how to extract them from the APK.

Dependencies: ``pip install httpx cryptography paho-mqtt`` (see requirements.txt).

Usage
-----
    export HAISMART_APP_ID=MB-XXXXXXXX-0001
    export HAISMART_APP_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    python3 haier_euro_keys.py --username you@example.com

The password is prompted for and never echoed.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import hashlib
import json
import os
import secrets
import sys
import time

import httpx
import paho.mqtt.client as mqtt
import ssl
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---------------------------------------------------------------------------
# Europe (Good Air / Haier U+) cloud endpoints — reverse-engineered from the
# official Android app v4.0.1.
# ---------------------------------------------------------------------------

UWS_BASE = "https://uws-gea-euro.haieriot.net"
UHOME_BASE = "https://uhome.haieriot.net"
GATEWAY_HOST = "gw-gea-euro.haieriot.net"
GATEWAY_PORT = 56789
APP_PACKAGE = "com.haier.uhome.haiergoodairabroad"
APP_VERSION = "4.0.1"
GATEWAY_AUTH_SALT = b"haier_sdk"
GATEWAY_USERNAME_TAG = "01"

# Paths
PATH_DEVICES = "/uds/v1/protected/deviceinfos"
PATH_ACBIZ_LOGIN = "/uhome/acbiz/login"

# The accessToken is bound to the clientId sent at login; one random 32-hex
# value per run is reused for every call.
CLIENT_ID = secrets.token_hex(16).upper()


def seqid() -> str:
    return datetime.datetime.now().strftime("%Y%m%d%H%M%S") + f"{secrets.randbelow(1000000):06d}"


def _squash(s: str) -> str:
    return s.replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")


def acbiz_headers(app_id: str, app_key: str, body: str) -> dict[str, str]:
    """Headers/sign for the uhome acbiz API (sign = md5 over squashed body)."""
    s = seqid()
    ts = str(int(time.time() * 1000))
    sign = hashlib.md5((_squash(body) + app_id + app_key + s[:14]).encode()).hexdigest().lower()
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "appId": app_id, "appKey": app_key, "appVersion": APP_VERSION,
        "clientId": CLIENT_ID, "language": "en", "timezone": "+00:00",
        "accessToken": "", "sequenceId": s, "timestamp": ts,
        "sign": sign, "privacyVersion": "V1.0.0",
        "User-Agent": "okhttp/3.8.0",
    }


def uws_headers(path: str, body: str, app_id: str, app_key: str,
                access_token: str) -> dict[str, str]:
    """Headers/sign for the GDPR/uws-gea-euro API (sign = sha256 over path+body)."""
    s = seqid()
    ts = str(int(time.time() * 1000))
    sign = hashlib.sha256((path + _squash(body) + app_id + app_key + ts).encode()).hexdigest().lower()
    return {
        "Content-Type": "application/json;charset=utf-8",
        "appId": app_id, "appVersion": APP_VERSION, "clientId": CLIENT_ID,
        "timezone": "+00:00", "accessToken": access_token, "sequenceId": s,
        "timestamp": ts, "sign": sign, "language": "en",
        "privacyVersion": "V1.0.0",
        "User-Agent": "Apache-HttpClient/4.2.6 (java 1.5)",
    }


def login(app_id: str, app_key: str, username: str, password: str) -> str:
    """Sign in via uhome/acbiz/login and return the accessToken.

    loginType=2 is the email account type; 0/1 (phone/userId) are tried as
    fallback in case Haier shuffles the codes again.
    """
    with httpx.Client(verify=False, timeout=30.0) as c:
        j: dict = {}
        for login_type in (2, 0, 1):
            body = json.dumps({
                "loginId": username, "password": password,
                "accType": 0, "loginType": login_type, "sequenceId": seqid(),
            }, separators=(",", ":"))
            r = c.post(UHOME_BASE + PATH_ACBIZ_LOGIN, content=body,
                       headers=acbiz_headers(app_id, app_key, body))
            j = r.json()
            token = j.get("accessToken") or (j.get("data") or {}).get("accessToken")
            if token:
                return token
        raise RuntimeError(f"acbiz login failed: {j}")


def list_devices(app_id: str, app_key: str, access_token: str) -> list[dict]:
    """Return the account's devices (deviceId, wifiType, deviceType, ...)."""
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(UWS_BASE + PATH_DEVICES,
                  headers=uws_headers(PATH_DEVICES, "", app_id, app_key, access_token))
        r.raise_for_status()
        j = r.json()
        return j.get("deviceinfos", []) or []


# --- uSDK MQTT gateway (devLocalkey) -----------------------------------------
# Derived auth, same scheme the SE-Asia gateway uses (see enapt/haismart-local).


def _derive_gateway_password(username_body: str) -> str:
    block = len(GATEWAY_AUTH_SALT).to_bytes(2, "big") + GATEWAY_AUTH_SALT
    block += b"\x00" * (-len(block) % 16)
    key = hashlib.md5(username_body.encode()).digest()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return (enc.update(block) + enc.finalize()).hex()


def _derive_gateway_auth() -> tuple[str, str]:
    body = "".join(str(secrets.randbelow(10)) for _ in range(8))
    return GATEWAY_USERNAME_TAG + body, _derive_gateway_password(body)


def _derive_client_id(usdk_client_id: str, package: str) -> str:
    return hashlib.md5(f"{usdk_client_id}_{package}".encode()).hexdigest()


def _localkey_request(device_id: str, access_token: str, sn: int = 1) -> str:
    inner = json.dumps({"sn": str(sn), "dev": device_id, "flag": 0},
                       separators=(",", ":"))
    data = base64.b64encode(inner.encode()).decode()
    return json.dumps({"type": "devLocalkey", "data": data,
                       "tokens": [access_token]}, separators=(",", ":"))


def _parse_localkey_response(payload: bytes) -> dict:
    try:
        outer = json.loads(payload)
        if outer.get("type") != "devLocalkey" or "data" not in outer:
            return {}
        return json.loads(base64.b64decode(outer["data"]))
    except (ValueError, KeyError, TypeError):
        return {}


def fetch_localkey(device_id: str, access_token: str,
                   usdk_client_id: str = CLIENT_ID,
                   timeout: float = 10.0) -> dict:
    """Fetch one device's localKey over the MQTT gateway."""
    username, password = _derive_gateway_auth()
    client_id = _derive_client_id(usdk_client_id, APP_PACKAGE)
    sub_topic = f"Client/{client_id}/Business/Down"
    pub_topic = f"Client/{client_id}/Business/Up"

    result: dict = {}

    def on_connect(c, _u, _f, rc, *_a):
        if rc != 0:
            result["_error"] = f"MQTT CONNECT rc={rc}"
            return
        c.subscribe(sub_topic)
        c.publish(pub_topic, _localkey_request(device_id, access_token), qos=0)

    def on_message(_c, _u, msg):
        parsed = _parse_localkey_response(msg.payload)
        if parsed:
            result.update(parsed)

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.username_pw_set(username, password)
    cli.tls_set(cert_reqs=ssl.CERT_NONE)
    cli.tls_insecure_set(True)

    cli.connect(GATEWAY_HOST, GATEWAY_PORT, int(timeout))
    cli.loop_start()
    deadline = time.time() + timeout
    while time.time() < deadline and "key" not in result and "_error" not in result:
        time.sleep(0.1)
    cli.loop_stop()
    cli.disconnect()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Haier U+ (Europe) local key")
    ap.add_argument("--username", required=True, help="Haier account email")
    ap.add_argument("--app-id", default=os.environ.get("HAISMART_APP_ID"),
                    help="appId (or env HAISMART_APP_ID)")
    ap.add_argument("--app-key", default=os.environ.get("HAISMART_APP_KEY"),
                    help="appKey (or env HAISMART_APP_KEY)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not args.app_id or not args.app_key:
        print("error: --app-id / --app-key (or HAISMART_APP_ID/HAISMART_APP_KEY) "
              "are required. See README -> Getting the app credentials.",
              file=sys.stderr)
        return 2

    password = getpass.getpass("Haier account password: ")

    try:
        token = login(args.app_id, args.app_key, args.username, password)
        devices = list_devices(args.app_id, args.app_key, token)
    except Exception as err:  # noqa: BLE001
        print(f"error: {err}", file=sys.stderr)
        return 1

    if not devices:
        print("error: no devices on the account", file=sys.stderr)
        return 2

    rows = []
    for dev in devices:
        device_id = dev.get("deviceId", "")
        key_info = fetch_localkey(device_id, token) if device_id else {}
        rows.append({
            "device_name": dev.get("deviceName", ""),
            "device_id": device_id,
            "device_type": dev.get("deviceType", ""),
            "wifi_type": dev.get("wifiType", ""),
            "local_key": key_info.get("key"),
            "key_version": key_info.get("vers"),
            "error": key_info.get("_error") or key_info.get("errNo"),
        })

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print()
            print(f"  {row['device_name'] or row['device_id']}")
            print(f"    device ID (MAC)  {row['device_id']}")
            if row.get("local_key"):
                print(f"    local key        {row['local_key']}")
                print(f"    key version      {row['key_version']}")
            else:
                print(f"    local key        NOT RETRIEVED: {row.get('error')}")
        print()
        print("  Add it in Home Assistant: Settings -> Devices & Services ->")
        print("  Add Integration -> Haismart -> 'I already have this unit's local key'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
