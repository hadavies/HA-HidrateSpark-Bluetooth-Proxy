"""Constants for the HidrateSpark integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hidratespark_bluetooth_proxy"

# Configuration keys
CONF_ADDRESS: Final = "address"
CONF_SIZE_ML: Final = "size_ml"
CONF_NAME_PREFIX: Final = "name_prefix"
CONF_MODEL: Final = "model"

DEFAULT_SIZE_ML: Final = 591
DEFAULT_NAME_PREFIX: Final = "h2o"
DEFAULT_MODEL: Final = "other"

# Reconnect tuning
RECONNECT_BACKOFF_INITIAL: Final = 1.0
RECONNECT_BACKOFF_MAX: Final = 60.0

# Refill detection tuning
REFILL_SETTLE_TIMEOUT_S: Final = 30.0
REFILL_STABLE_SAMPLES: Final = 3  # consecutive steady samples = "settled"

# BLE: services
SERVICE_USER: Final = "bf2d1ba0-c473-49f2-9571-0ce69036c642"
SERVICE_REF: Final = "45855422-6565-4cd7-a2a9-fe8af41b85e8"

# BLE: characteristics — modern (HydroSync) path
CHAR_USER_DATA: Final = "bf2d1ba1-c473-49f2-9571-0ce69036c642"
CHAR_SET_POINT: Final = "b44b03f0-b850-4090-86eb-72863fb3618d"
CHAR_DEBUG: Final = "e3578b0d-caa7-46d6-b7c2-7331c08de044"

# BLE: characteristics — legacy path
CHAR_DATA_POINT: Final = "016e11b1-6c8a-4074-9e5a-076053f93784"

# BLE: standard battery
CHAR_BATTERY_LEVEL: Final = "00002a19-0000-1000-8000-00805f9b34fb"

# BLE: discovered on firmware 80.18 (nRF52832)
CHAR_WEIGHT: Final = "1807a063-4e2d-4636-981a-35e93d1c7b94"
# Cap-state notifications share UUID with the DEBUG handshake characteristic
CHAR_CAP: Final = CHAR_DEBUG

# Drain command — single byte written to the data char to ack a sip record
DRAIN_BYTE: Final = bytes([0x57])

# Weight encoding.
# The weight characteristic streams a 16-bit big-endian value (high<<8 | low).
# Earlier firmwares were assumed to put an orientation flag in the high byte and
# the weight in the low byte, but on legacy-firmware bottles (e.g. 32oz) the high
# byte rises *with* the weight (observed 0x8Exx at ~75% full, 0x90xx when full),
# so the whole u16 is the reading. We therefore treat the full u16 as the weight
# and detect a trustworthy "upright & settled" reading by stability: N consecutive
# samples within RAW_STABLE_TOLERANCE. Transient frames while the bottle is moved
# never form a streak, so they are filtered without needing a magic byte.
RAW_STABLE_TOLERANCE: Final = 4  # u16 units; settled jitter is ~±1-2
# Raw-units-per-mL scale for converting a weight delta into a volume. Generic
# fallback / seed only — the real value is a property of the specific sensor
# puck and is selected per-model and refined per-puck by auto-calibration (see
# MODELS and BottleState). Measured value 1.305 came from a full+empty
# calibration on a 946 mL PRO 32oz bottle (full u16 37115, empty 35880, 1235
# raw units over 946 mL).
RAW_UNITS_PER_ML: Final = 1.305
# A jump of this many u16 units across a cap open/close means the bottle was
# refilled (~30 mL at the scale above) rather than just opened to drink.
REFILL_MIN_DELTA_RAW: Final = 60

# Per-model weight-sensor calibration. The raw-per-mL scale belongs to the
# sensor puck, which differs by bottle width-family and generation: the 32oz
# PRO puck is physically larger than the 17/21oz one (3.82" vs 2.76" base), the
# 24oz is a different (Tritan) body, and the PRO 2 generation has a new sensor.
# So a single constant can't be right for every bottle. Each model seeds a
# default scale and bottle size; only `measured` values have been confirmed on
# real hardware. Unmeasured seeds are best-effort and self-correct via the sip
# auto-calibration, so they only affect readings before enough has been drunk.
MODELS: Final[dict[str, dict]] = {
    "pro_32oz": {"label": "HidrateSpark PRO 32oz", "size_ml": 946, "raw_per_ml": 1.305, "measured": True},
    "pro_24oz": {"label": "HidrateSpark PRO 24oz", "size_ml": 710, "raw_per_ml": 1.305, "measured": False},
    "pro_21oz": {"label": "HidrateSpark PRO 21oz", "size_ml": 621, "raw_per_ml": 1.305, "measured": False},
    "pro_17oz": {"label": "HidrateSpark PRO 17oz", "size_ml": 503, "raw_per_ml": 1.305, "measured": False},
    "pro2": {"label": "HidrateSpark PRO 2", "size_ml": 621, "raw_per_ml": 1.305, "measured": False},
    "other": {"label": "Other / unknown (custom size)", "size_ml": DEFAULT_SIZE_ML, "raw_per_ml": 1.305, "measured": False},
}

# Sip-based auto-calibration of the raw-per-mL scale. Each sip carries a known
# mL (from the bottle's own sip records, independent of weight), so the ratio of
# the cumulative settled-weight drop to the cumulative sip volume since the last
# refill yields the puck's true scale. Only trust a sample once enough has been
# drunk for a clean signal; smooth it in and clamp to sane physical bounds.
CALIBRATION_MIN_ML: Final = 250
CALIBRATION_EMA_ALPHA: Final = 0.3
RAW_PER_ML_MIN: Final = 0.4
RAW_PER_ML_MAX: Final = 4.0

# 13-step handshake from HydroSync. Each tuple is (target_char, hex_payload).
# Writes are 50 ms apart.
HANDSHAKE_COMMANDS: Final[list[tuple[str, str]]] = [
    ("DEBUG", "2100d1"),
    ("SET_POINT", "92"),
    ("DEBUG", "2200f7"),
    ("SET_POINT", "7700000032d70000"),
    ("SET_POINT", "00341b00e0790000"),
    ("SET_POINT", "02345200c0a80000"),
    ("SET_POINT", "03346e0030c00000"),
    ("SET_POINT", "04348900a0d70000"),
    ("SET_POINT", "0534a50010ef0000"),
    ("SET_POINT", "0634c00080060100"),
    ("SET_POINT", "0734dc00f01d0100"),
    ("SET_POINT", "0834000000000000"),
    ("SET_POINT", "0934000000000000"),
]
HANDSHAKE_INTERVAL_S: Final = 0.05

# Sip dedup window — wider than the upstream MQTT bridge because BLE relay
# via an ESPHome proxy can add a few seconds of timestamp jitter on replays.
SIP_DEDUP_WINDOW: Final = 50  # check against last N sips
SIP_DEDUP_TIMESTAMP_TOLERANCE_S: Final = 5

# Persistence storage
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = "hidratespark"
