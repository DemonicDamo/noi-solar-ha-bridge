# Noi Solar / LiMu LTW Home Assistant MQTT Bridge

A lightweight Python bridge that polls the Noi Solar / LiMu cloud API for LTW-series MPPT solar charge controllers and publishes Home Assistant MQTT Discovery sensors.

This was built for a Y&H / Noi Solar `LTW2430` controller, but may work with similar LTW devices that use the same app/cloud backend.

## What it reads

Published sensors include:

- Panel voltage
- Panel current
- Panel power / wattage
- Battery voltage
- Battery level
- Charge current
- Controller temperature
- Charging state / raw stage status
- Status/flags raw values

## Known cloud API details

The mobile app uses the Noi Solar / LiMu cloud backend:

```text
https://lmsolar.wyadmin.com
```

Known endpoints used by this bridge:

```text
/api/login/account
/api/hmBridge
```

The bridge logs in with your app account, then calls `/api/hmBridge` with your device/node ID and a Modbus-style read frame.

## Privacy / safety

Do **not** commit your `.env` file.

The `.env.example` is safe placeholder config. Your real values stay local:

- `NOI_ACCOUNT`
- `NOI_PASSWORD`
- `NOI_NODE_ID`
- `MQTT_PASSWORD`

`NOI_NODE_ID` identifies your controller/Wi-Fi module in the cloud. It is not the server ID. Treat it as private-ish.

## Docker quick start

```bash
cp .env.example .env
nano .env
```

Set at minimum:

```env
NOI_ACCOUNT=your-email@example.com
NOI_PASSWORD=your-noi-solar-password
NOI_NODE_ID=your-device-node-id (Normally the devices serial number)
MQTT_HOST=homeassistant.local
MQTT_USERNAME=your-mqtt-user
MQTT_PASSWORD=your-mqtt-password
```

Run:

```bash
docker compose up -d --build
```

Watch logs:

```bash
docker logs -f noi-solar-ha-bridge
```

Healthy logs look like:

```text
Login OK; polling node ...
MQTT discovery published; state topic noi_solar/.../state
Published: panel=...W battery=...V/...% temp=...°C
```

## Direct Python usage

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
set -a
. ./.env
set +a
./venv/bin/python noi_solar_ha_bridge.py --once --print
```

Run continuously:

```bash
./venv/bin/python noi_solar_ha_bridge.py
```

## Home Assistant

The script publishes MQTT Discovery configs under:

```text
homeassistant/sensor/...
```

Entities should appear with names like:

```text
sensor.noi_solar_ltw_panel_power
sensor.noi_solar_ltw_panel_voltage
sensor.noi_solar_ltw_panel_current
sensor.noi_solar_ltw_battery_voltage
sensor.noi_solar_ltw_controller_temperature
```

If entities stay unavailable, check:

1. The bridge logs show successful publish events.
2. MQTT credentials are accepted by your broker.
3. Home Assistant MQTT integration is loaded.
4. Retained discovery configs exist on your MQTT broker.

## Register map notes

The bridge currently reads the LTW dashboard frame:

```python
[255, 3, 0, 160, 0, 28, 255, 81]
```

Decoded offsets from the returned register block:

| Offset | Sensor |
| --- | --- |
| 0 | status code |
| 5 | panel voltage / 100 |
| 6 | panel current / 10 |
| 7 | panel power / 10 |
| 8 | flags |
| 9 | charging state |
| 10 | battery voltage / 100 |
| 11 | battery percent |
| 12 | stage/status raw |
| 14 | charge current / 10 |
| 24 | internal temperature / 10 |

This is reverse-engineered from app/cloud behaviour, so firmware variants may be weird. Because of course they may. Solar IoT is not exactly a cathedral of standards.

## License

MIT
