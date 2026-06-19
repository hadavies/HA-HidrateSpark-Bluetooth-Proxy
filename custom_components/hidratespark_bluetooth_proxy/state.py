"""Persistent state for a HidrateSpark bottle.

Tracks sip dedup, daily/lifetime totals with day rollover, weight-anchored
fill level with auto-calibration on refill, and the sip-exceeds-fill auto
refill heuristic. Persisted via Home Assistant's Store API so values survive
restarts.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CALIBRATION_EMA_ALPHA,
    CALIBRATION_MIN_ML,
    RAW_PER_ML_MAX,
    RAW_PER_ML_MIN,
    RAW_UNITS_PER_ML,
    SIP_DEDUP_TIMESTAMP_TOLERANCE_S,
    SIP_DEDUP_WINDOW,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Sip:
    """A single sip event."""

    timestamp: float  # unix seconds
    volume_ml: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "iso": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "timestamp": self.timestamp,
            "volume_ml": self.volume_ml,
        }


class BottleState:
    """In-memory state with HA-Store-backed persistence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        bottle_size_ml: int,
        raw_per_ml: float = RAW_UNITS_PER_ML,
    ) -> None:
        self._hass = hass
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry_id}"
        )

        self.bottle_size_ml = bottle_size_ml
        # Per-puck raw-per-mL scale: seeded from the selected model, then refined
        # by sip auto-calibration. The learned value is persisted and overrides
        # the seed on reload.
        self.raw_units_per_ml: float = float(raw_per_ml)
        self.current_fill_ml: int = bottle_size_ml
        self.lifetime_total_ml: int = 0
        self.last_refill_ts: Optional[float] = None
        self.last_seen: Optional[float] = None

        # Sip history (in-memory only, dedup window).
        self.sips: deque[Sip] = deque(maxlen=200)
        self.last_sip: Optional[Sip] = None

        # Daily total with day rollover.
        self._today_date: str = ""
        self._total_today_ml: int = 0
        self._sips_today: int = 0
        self._refills_today: int = 0

        # Weight calibration (16-bit raw values). "full" is the bootstrap/refill
        # anchor; "empty" is the learned tare (lightest settled reading), which
        # is the stable zero reference fill is measured up from.
        self.weight_full_raw: Optional[int] = None
        self.weight_empty_raw: Optional[int] = None
        self.weight_raw: Optional[int] = None  # most recent stable u16 reading

        # Auto-calibration window: cumulative sip volume and the weight anchor it
        # is measured against, since the last refill. Their ratio gives the
        # puck's raw-per-mL. Reset on every (re)anchor; not persisted (the
        # learned scale is what carries over).
        self._calib_anchor_raw: Optional[int] = None
        self._calib_sip_ml: float = 0.0

    # ----------------------------------------------------------- persistence

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.current_fill_ml = int(data.get("current_fill_ml") or self.bottle_size_ml)
        self.lifetime_total_ml = int(data.get("lifetime_total_ml") or 0)
        self.last_refill_ts = data.get("last_refill_ts")
        self._today_date = str(data.get("today_date") or "")
        self._total_today_ml = int(data.get("total_today_ml") or 0)
        self._sips_today = int(data.get("sips_today") or 0)
        self._refills_today = int(data.get("refills_today") or 0)
        self.weight_full_raw = data.get("weight_full_raw")
        self.weight_empty_raw = data.get("weight_empty_raw")
        learned = data.get("raw_units_per_ml")
        if learned is not None:
            self.raw_units_per_ml = float(learned)

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "current_fill_ml": self.current_fill_ml,
                "lifetime_total_ml": self.lifetime_total_ml,
                "last_refill_ts": self.last_refill_ts,
                "today_date": self._today_date,
                "total_today_ml": self._total_today_ml,
                "sips_today": self._sips_today,
                "refills_today": self._refills_today,
                "weight_full_raw": self.weight_full_raw,
                "weight_empty_raw": self.weight_empty_raw,
                "raw_units_per_ml": self.raw_units_per_ml,
            }
        )

    # --------------------------------------------------------------- mutations

    def set_bottle_size(self, size_ml: int) -> None:
        self.bottle_size_ml = size_ml
        if self.current_fill_ml > size_ml:
            self.current_fill_ml = size_ml

    def _local_date_str(self, ts: Optional[float] = None) -> str:
        """Return YYYY-MM-DD in HA's configured local timezone.

        Using HA's configured zone (Settings -> System -> General) keeps the
        'water today' counter and the new sip/refill counters in sync with
        what the user sees on the wall clock, including DST transitions.
        """
        if ts is None:
            return dt_util.now().strftime("%Y-%m-%d")
        return dt_util.as_local(
            datetime.fromtimestamp(ts, tz=timezone.utc)
        ).strftime("%Y-%m-%d")

    def _maybe_rollover(self, ts: Optional[float] = None) -> None:
        """Reset daily counters if `ts` (or now) falls on a new local date."""
        date_str = self._local_date_str(ts)
        if date_str != self._today_date:
            self._today_date = date_str
            self._total_today_ml = 0
            self._sips_today = 0
            self._refills_today = 0

    def refill(self, source: str, weight_full_raw: Optional[int]) -> None:
        self._maybe_rollover()
        self.current_fill_ml = self.bottle_size_ml
        self.last_refill_ts = time.time()
        # A "calibration" is the one-time bootstrap that establishes the full-
        # weight anchor (bottle assumed full); it isn't a user refill, so it
        # doesn't bump the daily refill counter.
        if source != "calibration":
            self._refills_today += 1
        if weight_full_raw is not None:
            self.weight_full_raw = weight_full_raw
            self._reset_calibration_window(weight_full_raw)
        _LOGGER.info(
            "REFILL (%s): fill=%dml anchor=%s refills_today=%d",
            source,
            self.current_fill_ml,
            self.weight_full_raw,
            self._refills_today,
        )

    def update_fill_from_weight(self, raw: int) -> bool:
        """Recompute current fill from a stable upright 16-bit weight reading.

        Fill is measured up from the bottle's empty weight (tare), not down from
        the "full" anchor: the tare is stable per bottle, whereas "full" varies
        with how full it was actually filled, so anchoring on it leaves phantom
        volume at empty. The tare is learned as the lightest settled reading
        seen; until a real drain has been observed we fall back to estimating
        down from the full anchor so a freshly-set-up (full) bottle still reads
        sensibly. Returns True if current_fill_ml changed.
        """
        self.weight_raw = raw
        if self.weight_full_raw is None:
            # Bootstrap: the first settled reading establishes the full-weight
            # anchor (bottle assumed full at calibration). A real refill
            # (cap open/close + weight jump) re-anchors at the true full later.
            self.weight_full_raw = raw
            self._reset_calibration_window(raw)
            self.current_fill_ml = self.bottle_size_ml
            _LOGGER.info("weight calibration: adopted %s as full anchor", raw)
            return True

        self._maybe_calibrate_scale(raw)

        full_span = self.raw_units_per_ml * self.bottle_size_ml
        # Learn the empty floor (tare) as the lightest settled reading. Only
        # accept candidates that are plausibly below the full anchor but not more
        # than a bottle's worth below it (which would be the bottle lifted off
        # the puck rather than genuinely empty).
        if (
            self.weight_full_raw - 1.3 * full_span <= raw < self.weight_full_raw
            and (self.weight_empty_raw is None or raw < self.weight_empty_raw)
        ):
            self.weight_empty_raw = raw

        if (
            self.weight_empty_raw is not None
            and self.weight_full_raw - self.weight_empty_raw >= 0.6 * full_span
        ):
            # Enough range observed: measure up from the learned empty floor, so
            # empty reads 0 regardless of how full the last fill actually was.
            new_fill = round((raw - self.weight_empty_raw) / self.raw_units_per_ml)
        else:
            # Not drained enough yet to trust the floor: estimate down from full.
            new_fill = self.bottle_size_ml - round(
                (self.weight_full_raw - raw) / self.raw_units_per_ml
            )
        new_fill = max(0, min(self.bottle_size_ml, new_fill))
        if new_fill != self.current_fill_ml:
            self.current_fill_ml = new_fill
            return True
        return False

    # ------------------------------------------------------------- calibration

    def _reset_calibration_window(self, anchor_raw: int) -> None:
        """Start a fresh raw-per-mL calibration window from a full anchor."""
        self._calib_anchor_raw = anchor_raw
        self._calib_sip_ml = 0.0

    def _maybe_calibrate_scale(self, raw: int) -> None:
        """Refine raw_units_per_ml from cumulative weight drop vs sip volume.

        Sip volumes come from the bottle's own records (independent of weight),
        so once enough has been drunk since the anchor, the ratio of the
        settled-weight drop to the cumulative sip volume is the puck's true
        scale. Smoothed in and clamped to physical bounds.
        """
        if self._calib_anchor_raw is None or self._calib_sip_ml < CALIBRATION_MIN_ML:
            return
        drop = self._calib_anchor_raw - raw
        if drop <= 0:
            return
        sample = drop / self._calib_sip_ml
        if not RAW_PER_ML_MIN <= sample <= RAW_PER_ML_MAX:
            return
        updated = (
            1 - CALIBRATION_EMA_ALPHA
        ) * self.raw_units_per_ml + CALIBRATION_EMA_ALPHA * sample
        if abs(updated - self.raw_units_per_ml) >= 0.001:
            _LOGGER.info(
                "weight auto-calibration: %.3f -> %.3f raw/mL "
                "(drop=%d over %.0f mL drunk)",
                self.raw_units_per_ml,
                updated,
                drop,
                self._calib_sip_ml,
            )
            self.raw_units_per_ml = updated

    def add_sip(self, sip: Sip) -> bool:
        """Append a sip if it isn't a duplicate. Returns True if accepted."""
        # Dedup against last N sips: same volume within ±2 s timestamp.
        for existing in list(self.sips)[-SIP_DEDUP_WINDOW:]:
            if (
                abs(existing.timestamp - sip.timestamp)
                < SIP_DEDUP_TIMESTAMP_TOLERANCE_S
                and existing.volume_ml == sip.volume_ml
            ):
                return False

        # Day rollover keyed on HA's configured local timezone so 'today'
        # matches the user's wall clock (including DST).
        self._maybe_rollover(sip.timestamp)

        self.sips.append(sip)
        self.last_sip = sip
        self.lifetime_total_ml += sip.volume_ml
        self._total_today_ml += sip.volume_ml
        self._sips_today += 1
        self.last_seen = sip.timestamp
        # Feed the raw-per-mL auto-calibration window.
        self._calib_sip_ml += sip.volume_ml

        # Sip-exceeds-fill: bottle was clearly refilled out-of-band. Only used as
        # a fallback while we have no weight anchor to track fill directly.
        if self.weight_full_raw is None and sip.volume_ml > self.current_fill_ml:
            self.current_fill_ml = max(0, self.bottle_size_ml - sip.volume_ml)
            self.last_refill_ts = sip.timestamp
            _LOGGER.info(
                "REFILL (auto: sip exceeded fill) -> %dml after %dml sip",
                self.current_fill_ml,
                sip.volume_ml,
            )
        elif self.weight_full_raw is None:
            # Sip-decrement fallback while we have no weight anchor.
            self.current_fill_ml = max(0, self.current_fill_ml - sip.volume_ml)

        return True

    @property
    def total_today_ml(self) -> int:
        # Late-day rollover: if no sip has come in yet today, we still want
        # the sensor to read 0 once midnight has passed (in HA's local zone).
        if self._local_date_str() != self._today_date:
            return 0
        return self._total_today_ml

    @property
    def sips_today(self) -> int:
        if self._local_date_str() != self._today_date:
            return 0
        return self._sips_today

    @property
    def refills_today(self) -> int:
        if self._local_date_str() != self._today_date:
            return 0
        return self._refills_today

    @property
    def current_fill_pct(self) -> int:
        if self.bottle_size_ml <= 0:
            return 0
        return round(100 * self.current_fill_ml / self.bottle_size_ml)
