#!/usr/bin/env python3
"""Automatically refresh the Haier U+ (Europe) AC localKey and patch Home Assistant.

NOTE (2026-10): Haier killed the UWS login endpoints (`/uam/v2/mgr/getPublicKey`,
`/uam/v2/user/loginEmailAcounnt`) server-side — they return B00004 for the official
app too. The working flow is now:

    1. POST https://uhome.haieriot.net/uhome/acbiz/login  (plaintext password,
       sign = md5(body+appId+appKey+sequenceId[:14]))  ->  accessToken
    2. GET  https://uws-gea-euro.haieriot.net/uds/v1/protected/deviceinfos
       (old sha256 sign, token from step 1)  ->  device list sanity check
    3. MQTT gw-gea-euro.haieriot.net:56789 `devLocalkey`  ->  localKey + version

When the fetched key differs from the one stored in HA's core.config_entries the
entry is rewritten and HA restarted via its REST API. Meant for cron every ~30 min.

Credentials come from the environment:

    HAISMART_APP_ID / HAISMART_APP_KEY   app-level app credentials
    HAISMART_USERNAME / HAISMART_PASSWORD  Haier account
    HAISMART_DEVICE_ID  AC MAC/deviceId (or pass --device-id)
    HA_STORAGE  path to core.config_entries (default: /config/.storage/core.config_entries)
    HA_URL      Home Assistant base URL (default: http://localhost:8123)
    HA_TOKEN    HA refresh token (prefix "refresh:") or long-lived access token
    HA_CLIENT_ID  OAuth client_id bound to the refresh token (required if the
                  token is a normal type; e.g. https://ha.pi.lan/)

Returns 0 when nothing to do or update applied; 1 on real failure; 2 on wrong config.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import ssl
import sys
import time
import urllib.parse
import urllib.request

import httpx
import paho.mqtt.client as mqtt

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

UWS_BASE = "https://uws-gea-euro.haieriot.net"
UHOME_BASE = "https://uhome.haieriot.net"
GATEWAY_HOST = "gw-gea-euro.haieriot.net"
GATEWAY_PORT = 56789
APP_PACKAGE = "com.haier.uhome.haiergoodairabroad"
APP_VERSION = "4.0.1"
GATEWAY_AUTH_SALT = b"haier_sdk"
GATEWAY_USERNAME_TAG = "01"

PATH_DEVICES = "/uds/v1/protected/deviceinfos"
PATH_ACBIZ_LOGIN = "/uhome/acbiz/login"

APP_ID = os.environ.get("HAISMART_APP_ID", "")
APP_KEY = os.environ.get("HAISMART_APP_KEY", "")

# 32-hex client id in the app's dcid format. The accessToken is bound to the
# clientId sent at login, so one value per run is generated here and reused for
# every call. Set HAISMART_CLIENT_ID to pin it.
CLIENT_ID = os.environ.get("HAISMART_CLIENT_ID") or secrets.token_hex(16).upper()


def seqid() -> str:
    return datetime.datetime.now().strftime("%Y%m%d%H%M%S") + f"{secrets.randbelow(1000000):06d}"


def _squash(s: str) -> str:
    return s.replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")


def acbiz_headers(body: str) -> dict:
    s = seqid()
    ts = str(int(time.time() * 1000))
    sign = hashlib.md5((_squash(body) + APP_ID + APP_KEY + s[:14]).encode()).hexdigest().lower()
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "appId": APP_ID, "appKey": APP_KEY, "appVersion": APP_VERSION,
        "clientId": CLIENT_ID, "language": "en", "timezone": "+00:00",
        "accessToken": "", "sequenceId": s, "timestamp": ts,
        "sign": sign, "privacyVersion": "V1.0.0",
        "User-Agent": "okhttp/3.8.0",
    }


def uws_headers(path: str, body: str, token: str) -> dict:
    s = seqid()
    ts = str(int(time.time() * 1000))
    sign = hashlib.sha256((path + _squash(body) + APP_ID + APP_KEY + ts).encode()).hexdigest().lower()
    return {
        "Content-Type": "application/json;charset=utf-8",
        "appId": APP_ID, "appVersion": APP_VERSION, "clientId": CLIENT_ID,
        "timezone": "+00:00", "accessToken": token, "sequenceId": s,
        "timestamp": ts, "sign": sign, "language": "en",
        "privacyVersion": "V1.0.0",
        "User-Agent": "Apache-HttpClient/4.2.6 (java 1.5)",
    }


def login(username: str, password: str) -> str:
    """Login via the still-working uhome/acbiz endpoint (plaintext password)."""
    with httpx.Client(verify=False, timeout=30.0) as c:
        j: dict = {}
        for login_type in (2, 0, 1):
            body = json.dumps({
                "loginId": username, "password": password,
                "accType": 0, "loginType": login_type, "sequenceId": seqid(),
            }, separators=(",", ":"))
            r = c.post(UHOME_BASE + PATH_ACBIZ_LOGIN, content=body,
                       headers=acbiz_headers(body))
            j = r.json()
            token = j.get("accessToken") or (j.get("data") or {}).get("accessToken")
            if token:
                return token
        raise RuntimeError(f"acbiz login failed: {j}")


def list_devices(access_token: str):
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(UWS_BASE + PATH_DEVICES,
                  headers=uws_headers(PATH_DEVICES, "", access_token))
        j = r.json()
        return j.get("deviceinfos", []) or []


def _derive_gateway_password(username_body: str) -> str:
    block = len(GATEWAY_AUTH_SALT).to_bytes(2, "big") + GATEWAY_AUTH_SALT
    block += b"\x00" * (-len(block) % 16)
    key = hashlib.md5(username_body.encode()).digest()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return (enc.update(block) + enc.finalize()).hex()


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


def fetch_localkey(device_id, access_token, timeout=12.0):
    body = "".join(str(secrets.randbelow(10)) for _ in range(8))
    username = GATEWAY_USERNAME_TAG + body
    password = _derive_gateway_password(body)
    usdk_client_id = "haiergoodair" + "".join(str(secrets.randbelow(10)) for _ in range(8))
    client_id = hashlib.md5(f"{usdk_client_id}_{APP_PACKAGE}".encode()).hexdigest()
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


def load_entries(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_entries(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def patch_entry(path, device_id, new_key, new_version):
    data = load_entries(path)
    found = False
    for e in data["data"]["entries"]:
        if e.get("domain") == "haismart" and e.get("unique_id") == device_id:
            old_key = e["data"].get("local_key")
            old_ver = e["data"].get("localkey_version")
            e["data"]["local_key"] = new_key
            e["data"]["localkey_version"] = new_version
            found = True
            break
    if not found:
        raise RuntimeError(f"haismart entry for {device_id} not found in {path}")
    save_entries(path, data)
    return old_key, old_ver


def restart_ha(ha_url, token):
    """Restart Home Assistant (picks up a storage-file config entry change).

    Editing core.config_entries on disk does not take effect while HA runs: it
    serves config entries from memory and a config_entries/reload reuses that
    cached data. A restart is the only reliable way to load a disk-patched key.
    """
    url = f"{ha_url}/api/services/homeassistant/restart"
    req = urllib.request.Request(url, method="POST", data=b"{}",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def mint_access_token(ha_url, refresh_token, client_id=None) -> str:
    """Exchange a HA refresh token for a short-lived access token (JWT)."""
    params = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_id:
        params["client_id"] = client_id
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"{ha_url}/auth/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(resp.read().decode())
    token = j.get("access_token")
    if not token:
        raise RuntimeError(f"no access_token from /auth/token: {j}")
    return token


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", default=os.environ.get("HAISMART_DEVICE_ID", ""),
                    help="AC MAC/deviceId (or env HAISMART_DEVICE_ID)")
    ap.add_argument("--storage", default=os.environ.get("HA_STORAGE",
                    "/config/.storage/core.config_entries"))
    ap.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://localhost:8123"))
    ap.add_argument("--ha-token", default=os.environ.get("HA_TOKEN"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    username = os.environ.get("HAISMART_USERNAME")
    password = os.environ.get("HAISMART_PASSWORD")
    if not all((APP_ID, APP_KEY, username, password)):
        print("error: set HAISMART_APP_ID/APP_KEY/USERNAME/PASSWORD", file=sys.stderr)
        return 2
    if not args.device_id:
        print("error: --device-id (or env HAISMART_DEVICE_ID) is required",
              file=sys.stderr)
        return 2

    out = {"device_id": args.device_id}

    try:
        token = login(username, password)
        devices = list_devices(token)
    except Exception as err:  # noqa: BLE001
        out["error"] = f"cloud: {err}"
        print(json.dumps(out) if args.json else out["error"], file=sys.stderr)
        return 1

    if not any(d.get("deviceId") == args.device_id for d in devices):
        out["error"] = f"device {args.device_id} not on account"
        print(json.dumps(out) if args.json else out["error"], file=sys.stderr)
        return 1

    key_info = fetch_localkey(args.device_id, token)
    new_key = key_info.get("key")
    new_ver = key_info.get("vers")
    out["new_key"] = new_key
    out["new_version"] = new_ver
    if not new_key:
        out["error"] = key_info.get("_error") or key_info.get("errNo") or "no key"
        print(json.dumps(out) if args.json else out["error"], file=sys.stderr)
        return 1

    if not args.ha_token:
        old_key, old_ver = patch_entry(args.storage, args.device_id, new_key, new_ver)
        out["old_key"] = old_key
        out["old_version"] = old_ver
        out["status"] = "patched_no_restart"
        print(json.dumps(out) if args.json else
              f"patched {args.storage}; set HA_TOKEN to restart HA", file=sys.stderr)
        return 0

    try:
        ha_token = args.ha_token[8:] if args.ha_token.startswith("refresh:") else args.ha_token
        if args.ha_token.startswith("refresh:"):
            ha_token = mint_access_token(args.ha_url, ha_token,
                                         os.environ.get("HA_CLIENT_ID"))
        old_key, old_ver = patch_entry(args.storage, args.device_id, new_key, new_ver)
        out["old_key"] = old_key
        out["old_version"] = old_ver
        if new_key == old_key:
            out["status"] = "unchanged"
            print(json.dumps(out) if args.json else
                  f"key unchanged (v{new_ver}); nothing to do")
            return 0
        restart_ha(args.ha_url, ha_token)
        out["status"] = "restarted"
        print(json.dumps(out) if args.json else
              f"key updated v{old_ver}->v{new_ver} and HA restarted")
        return 0
    except Exception as err:  # noqa: BLE001
        out["status"] = "patched_no_restart"
        out["error"] = f"HA restart failed: {err}"
        print(json.dumps(out) if args.json else
              f"key patched to v{new_ver} but HA restart failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
