#!/usr/bin/env python3
"""Noi Solar / LiMu LTW cloud -> Home Assistant MQTT Discovery bridge."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

BASE_URL = os.getenv("NOI_BASE_URL", "https://lmsolar.wyadmin.com").rstrip("/")
LOGIN_PATH = "/api/login/account"
BRIDGE_PATH = "/api/hmBridge"
USER_AGENT = os.getenv("NOI_USER_AGENT", "Dart/3.10 (dart:io)")
# LTW dashboard read: slave 255, function 3, start 0x00A0, count 28, CRC bytes as app/cloud expects.
DASHBOARD_READ_FRAME = [255, 3, 0, 160, 0, 28, 255, 81]
LOG = logging.getLogger("noi-solar-ha-bridge")
STOP = False

@dataclass(frozen=True)
class SensorDef:
    key: str
    name: str
    device_class: Optional[str]
    state_class: Optional[str]
    unit: Optional[str]
    icon: Optional[str]
    precision: Optional[int] = None

SENSORS = {
    "panel_voltage": SensorDef("panel_voltage", "Panel Voltage", "voltage", "measurement", "V", None, 2),
    "panel_current": SensorDef("panel_current", "Panel Current", "current", "measurement", "A", None, 1),
    "panel_power": SensorDef("panel_power", "Panel Power", "power", "measurement", "W", None, 1),
    "battery_voltage": SensorDef("battery_voltage", "Battery Voltage", "voltage", "measurement", "V", None, 2),
    "battery_percent": SensorDef("battery_percent", "Battery Level", "battery", "measurement", "%", None, 0),
    "charge_current": SensorDef("charge_current", "Charge Current", "current", "measurement", "A", None, 1),
    "internal_temperature": SensorDef("internal_temperature", "Controller Temperature", "temperature", "measurement", "°C", None, 1),
    "status_code": SensorDef("status_code", "Status Code", None, None, None, "mdi:state-machine"),
    "flags": SensorDef("flags", "Flags", None, None, None, "mdi:flag"),
    "charging_state": SensorDef("charging_state", "Charging State", None, None, None, "mdi:battery-charging"),
    "stage_status_raw": SensorDef("stage_status_raw", "Stage/Status Raw", None, None, None, "mdi:chip"),
}

def env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value

def http_json(path: str, payload: Optional[dict] = None, token: Optional[str] = None, timeout: int = 25) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/json",
        "Accept-Language": os.getenv("NOI_ACCEPT_LANGUAGE", "en-US"),
        "Cookie": os.getenv("NOI_COOKIE", "think_lang=en-us"),
    }
    if token:
        headers["token"] = token
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} from {path}: {raw[:300]}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Non-JSON response from {path}: {raw[:300]}") from e

def find_token(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "token" in str(k).lower() and isinstance(v, str) and v:
                return v
        for v in obj.values():
            t = find_token(v)
            if t:
                return t
    elif isinstance(obj, list):
        for v in obj:
            t = find_token(v)
            if t:
                return t
    return None

def login() -> str:
    payload = {
        "account": env("NOI_ACCOUNT", required=True),
        "password": env("NOI_PASSWORD", required=True),
        "terminal": int(env("NOI_TERMINAL", "1") or "1"),
        "scene": int(env("NOI_SCENE", "1") or "1"),
    }
    js = http_json(LOGIN_PATH, payload)
    if js.get("code") != 1:
        raise RuntimeError(f"Login failed: code={js.get('code')} msg={js.get('msg')!r}")
    token = find_token(js)
    if not token:
        raise RuntimeError(f"Login OK but no token found in response keys: {list(js.keys())}")
    return token

def parse_modbus_registers(frame: list[int]) -> list[int]:
    if len(frame) < 5:
        raise ValueError(f"Bridge response too short: {frame}")
    fn, byte_count = frame[1], frame[2]
    if fn & 0x80:
        raise RuntimeError(f"Modbus exception from bridge: fn={fn:#x} code={byte_count}")
    if fn not in (3, 4):
        raise ValueError(f"Unexpected Modbus function {fn}: {frame}")
    data = frame[3:3 + byte_count]
    return [(data[i] << 8) | data[i + 1] for i in range(0, len(data) - 1, 2)]

def poll_registers(token: str, node_id: str) -> list[int]:
    js = http_json(BRIDGE_PATH, {"node_id": node_id, "data": DASHBOARD_READ_FRAME, "ret_count": 0}, token=token)
    if js.get("code") != 1:
        raise RuntimeError(f"Bridge call failed: code={js.get('code')} msg={js.get('msg')!r} data={js.get('data')!r}")
    data = js.get("data")
    if not isinstance(data, list) or not all(isinstance(x, int) for x in data):
        raise RuntimeError(f"Unexpected bridge data: {data!r}")
    return parse_modbus_registers(data)

def reg(regs: list[int], idx: int) -> Optional[int]:
    return regs[idx] if 0 <= idx < len(regs) else None

def div(v: Optional[int], d: float) -> Optional[float]:
    if v is None or v == 0xFFFF:
        return None
    return v / d

def temp(v: Optional[int]) -> Optional[float]:
    if v is None or v == 0xFFFF:
        return None
    if v & 0x8000:
        v -= 0x10000
    return v / 10

def decode(regs: list[int]) -> dict[str, Any]:
    # Register map recovered from the Noi Solar/LiMu LTW app/cloud bridge.
    values = {
        "status_code": reg(regs, 0),
        "panel_voltage": div(reg(regs, 5), 100),
        "panel_current": div(reg(regs, 6), 10),
        "panel_power": div(reg(regs, 7), 10),
        "flags": reg(regs, 8),
        "charging_state": reg(regs, 9),
        "battery_voltage": div(reg(regs, 10), 100),
        "battery_percent": reg(regs, 11),
        "stage_status_raw": reg(regs, 12),
        "charge_current": div(reg(regs, 14), 10),
        "internal_temperature": temp(reg(regs, 24)),
    }
    out = {}
    for k, v in values.items():
        p = SENSORS[k].precision
        out[k] = round(v, p) if isinstance(v, float) and p is not None else v
    out["updated_at"] = int(time.time())
    if env("PUBLISH_RAW_REGISTERS", "false").lower() in ("1", "true", "yes", "on"):
        out["raw_registers"] = regs
    return out

def mqtt_connect():
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=env("MQTT_CLIENT_ID", "noi-solar-ha-bridge"), clean_session=True)
    if env("MQTT_USERNAME"):
        client.username_pw_set(env("MQTT_USERNAME"), env("MQTT_PASSWORD"))
    if env("MQTT_TLS", "false").lower() in ("1", "true", "yes", "on"):
        client.tls_set()
    client.connect(str(env("MQTT_HOST", "192.168.1.226")), int(env("MQTT_PORT", "1883") or "1883"), keepalive=60)
    client.loop_start()
    return client

def topics(node_id: str) -> tuple[str, str]:
    base = env("MQTT_BASE_TOPIC", "noi_solar")
    slug = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in node_id)
    return f"{base}/{slug}/state", f"{base}/{slug}/availability"

def publish_discovery(client, node_id: str, state_topic: str, availability_topic: str) -> None:
    prefix = env("HA_DISCOVERY_PREFIX", "homeassistant")
    device = {
        "identifiers": [f"noi_solar_{node_id}"],
        "name": env("DEVICE_NAME", "Noi Solar LTW"),
        "manufacturer": env("DEVICE_MANUFACTURER", "Y&H / Noi Solar"),
        "model": env("DEVICE_MODEL", "LTW2430"),
        "configuration_url": "https://lmsolar.wyadmin.com/",
    }
    for key, sensor in SENSORS.items():
        oid = f"noi_solar_{node_id}_{key}".replace("-", "_")
        cfg = {
            "name": sensor.name,
            "unique_id": oid,
            "object_id": oid,
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "value_template": "{{ value_json." + key + " }}",
            "device": device,
        }
        if sensor.device_class: cfg["device_class"] = sensor.device_class
        if sensor.state_class: cfg["state_class"] = sensor.state_class
        if sensor.unit: cfg["unit_of_measurement"] = sensor.unit
        if sensor.icon: cfg["icon"] = sensor.icon
        client.publish(f"{prefix}/sensor/{oid}/config", json.dumps(cfg, separators=(",", ":")), retain=True)

def handle_signal(signum, frame):
    global STOP
    STOP = True

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", dest="print_only", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    node_id = str(env("NOI_NODE_ID", required=True))
    interval = int(env("POLL_INTERVAL", "60") or "60")
    token = login()
    last_login = time.monotonic()
    LOG.info("Login OK; polling node %s", node_id)
    client = None
    state_topic = availability_topic = ""
    if not args.print_only:
        client = mqtt_connect()
        state_topic, availability_topic = topics(node_id)
        publish_discovery(client, node_id, state_topic, availability_topic)
        LOG.info("MQTT discovery published; state topic %s", state_topic)
    while not STOP:
        try:
            if time.monotonic() - last_login > int(env("TOKEN_REFRESH_SECONDS", "3600") or "3600"):
                token = login(); last_login = time.monotonic(); LOG.info("Token refreshed")
            state = decode(poll_registers(token, node_id))
            if args.print_only:
                print(json.dumps(state, indent=2, sort_keys=True))
            else:
                assert client is not None
                client.publish(availability_topic, "online", retain=True)
                client.publish(state_topic, json.dumps(state, separators=(",", ":")), retain=False)
                LOG.info("Published: panel=%sW battery=%sV/%s%% temp=%s°C", state.get("panel_power"), state.get("battery_voltage"), state.get("battery_percent"), state.get("internal_temperature"))
        except Exception as e:
            LOG.exception("Poll failed: %s", e)
            if client is not None:
                try: client.publish(availability_topic, "offline", retain=True)
                except Exception: pass
        if args.once:
            break
        for _ in range(max(1, interval)):
            if STOP: break
            time.sleep(1)
    if client is not None:
        client.loop_stop(); client.disconnect()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
