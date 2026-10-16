# Xiaomi S400 Live for Home Assistant

Custom integration for the **Xiaomi Body Composition Scale S400** (MJTZC01YM). It gives you **live weight** while you stand on the scale and stores each **final measurement on the correct person**.

Unlike the built-in `xiaomi_ble` integration, which passively listens for advertisements, this one logs in to the scale over a BLE GATT connection (Mi Home v2 auth). The scale then streams data directly:

| | `xiaomi_ble` (passive) | this integration |
|---|---|---|
| Live weight while standing | no | yes, about every 0.3 s |
| Final result | 3 to 5 s after stepping off | as soon as it stabilizes |
| Profile + weight + impedance in one frame | arrive as separate entity updates | one atomic frame, never mixed between people |
| Per-person entities and body composition | needs BodyMiScale and templates | built in |
| Heart rate | yes | yes, if you also enter the BLE KEY (read from the broadcast) |
| Needs | bindkey | login **token** (24 hex chars), optionally BLE KEY for heart rate |

Works with a local Bluetooth adapter **and with ESPHome Bluetooth proxies** (ESP32), since all connections go through Home Assistant's Bluetooth stack.

> ⚠️ Early version (0.1.x). The protocol was reverse-engineered and tested on scale firmware `2.1.1_0006`. A firmware update may break it.

## Requirements

1. Home Assistant 2025.7 or newer.
2. A Bluetooth adapter in range of the scale, or an **ESP32 with ESPHome Bluetooth proxy in active mode**:
   ```yaml
   esp32:
     board: esp32dev
     framework:
       type: esp-idf
   esp32_ble_tracker:
     scan_parameters:
       active: true
   bluetooth_proxy:
     active: true
   ```
   Active mode is required because the integration opens a GATT connection. This uses one of the proxy's connection slots (usually 3) for a few seconds per weigh-in.
3. The scale's **token** from [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor). Run it, log in to your Xiaomi account and find the scale (listed as `yunmai.scales.ms103/ms104/ms107` or `xiaomi.scales.ms110`). Copy the **TOKEN** field (24 hex chars). The **BLE KEY** (32 hex chars) is a different value.
   The token changes when the scale is reset or removed and re-added in Mi Home. When that happens, HA shows a re-authentication prompt where you paste the new token.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → URL `https://github.com/nanocortex/ha-xiaomi-s400-live`, type **Integration**.
2. Install **Xiaomi S400 Live**, restart Home Assistant.
3. **Settings → Devices & services → Add integration → Xiaomi S400 Live** (or accept the discovered scale).
4. Pick the scale or enter its MAC address, then paste the token. Optionally also paste the **BLE KEY** (32 hex chars) to get heart rate.
5. On the integration card click **Add person** for each user: name, scale profile ID, sex, date of birth, height.

### Finding your profile ID

Weigh yourself alone and check the **Profile ID** sensor on the scale device. Numbering follows the user order in the Mi Fitness / Mi Home app.

## Entities

**Scale device**

- `Live weight`: updates while you stand on the scale
- `On scale`, `Stable`: binary sensors (useful for automations)
- `Weight`, `Impedance`, `Impedance low`, `Profile ID`, `Last measurement`: the last final measurement from anyone
- Diagnostics: `Connected`, `Last error`, `Raw final frame` (disabled by default)

**One device per person**

Weight, impedance (2x), heart rate, BMI, body fat, water, protein, muscle mass, bone mass, visceral fat, basal metabolism, metabolic age, ideal weight, body type, last measurement. They update **only** when that person's profile ID is measured, and keep their value across restarts.

**Event**

`xiaomi_s400_live_measurement` fires on every final measurement with `person`, `profile_id`, `weight`, `impedance`, `impedance_low`, `heart_rate`, `measured_at` and all body composition values. With a BLE KEY configured, the event waits up to 20 s for the heart rate advertisement.

Body composition values are estimates computed from weight, impedance and your profile, using formulas ported from the Mi Fit app. They are not medical-grade.

## How it works

1. Whenever the scale is reachable, the integration connects (via a proxy if needed) and runs the Mi Home v2 LOGIN: random key exchange, HKDF-SHA256 key derivation from the token, HMAC verification.
2. It **keeps the connection open** and waits. The scale only streams weight to a client that is already logged in when someone steps on it. Connecting after the advertisement is too late, because by then the scale has already finished measuring.
3. It reads AES-CCM encrypted frames: `live` (weight, stable flag) and `final` (profile, weight, timestamp, two impedances).
4. After a measurement it disconnects for 15 s so the scale can broadcast the heart rate. If the scale drops an idle link, the integration reconnects within a few seconds.

This keeps one connection slot of your Bluetooth adapter or ESPHome proxy busy most of the time.

## Troubleshooting

Enable debug logs:

```yaml
logger:
  logs:
    custom_components.xiaomi_s400_live: debug
```

The log shows every frame sent and received, plus the decoded final frame fields. Please attach it when reporting issues.

**Heart rate**: the GATT final frame doesn't contain it (fields 7 to 29 are always zero). The scale only sends it in its encrypted MiBeacon broadcast right after the measurement. That is why heart rate needs the BLE KEY, and why it shows up a moment after the weight.

## Credits

- Protocol and decoders: [nokistin/xiaomi-s400-live](https://github.com/nokistin/xiaomi-s400-live) (Apache-2.0), vendored and adapted in `custom_components/xiaomi_s400_live/s400/`
- Mi Home v2 auth: [dnandha/miauth](https://github.com/dnandha/miauth) (Apache-2.0)
- Body composition formulas: [mnm-matin/miscale](https://github.com/mnm-matin/miscale) (MIT)

License: Apache-2.0.
