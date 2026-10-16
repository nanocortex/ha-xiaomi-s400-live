"""Constants for Xiaomi S400 Live."""

from __future__ import annotations

DOMAIN = "xiaomi_s400_live"

CONF_TOKEN = "token"
CONF_BINDKEY = "bindkey"
CONF_PROFILE_ID = "profile_id"
CONF_SEX = "sex"
CONF_BIRTHDAY = "birthday"
CONF_HEIGHT = "height"

SUBENTRY_PERSON = "person"

# MiBeacon product ids (bytes 2-3 of FE95 service data, little endian) of S400 variants.
S400_PRODUCT_IDS = {0x30D9, 0x3BD5, 0x48CF, 0x4B05}
MIBEACON_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"

# Disconnect when the scale sends nothing for this long.
IDLE_TIMEOUT = 20.0
# Keep the link a little after the final frame in case more data follows.
AFTER_FINAL_GRACE = 4.0
# Do not reconnect right after a session (scale keeps advertising the result).
RECONNECT_COOLDOWN = 10.0

EVENT_MEASUREMENT = f"{DOMAIN}_measurement"

# Heart rate only comes in the advertisement after the measurement; wait this long for it.
HEART_RATE_WAIT = 20.0
