"""16-bit weight decoding, auto-calibration, and weight-based fill tracking."""

import unittest

from ha_stub import HomeAssistant, load_state

state = load_state()


def new_bottle(size=946):
    return state.BottleState(HomeAssistant(), "entry", size)


class WeightCalibrationTest(unittest.TestCase):
    def test_first_reading_calibrates_full_anchor(self):
        b = new_bottle(946)
        changed = b.update_fill_from_weight(37000)
        self.assertTrue(changed)
        self.assertEqual(b.weight_full_raw, 37000)
        self.assertEqual(b.current_fill_ml, 946)

    def test_empty_bottle_reads_zero(self):
        # Measured full+empty calibration on a 946 mL bottle.
        b = new_bottle(946)
        b.update_fill_from_weight(37115)  # full anchor
        self.assertEqual(b.current_fill_ml, 946)
        b.update_fill_from_weight(35880)  # truly empty
        self.assertAlmostEqual(b.current_fill_ml, 0, delta=20)

    def test_fill_tracks_proportionally(self):
        b = new_bottle(946)
        b.update_fill_from_weight(37115)  # full
        # halfway down the raw span (1235 units) -> ~half the bottle drunk
        b.update_fill_from_weight(37115 - 1235 // 2)
        self.assertAlmostEqual(b.current_fill_ml, 473, delta=15)

    def test_fill_never_exceeds_full_or_goes_negative(self):
        b = new_bottle(946)
        b.update_fill_from_weight(37000)
        b.update_fill_from_weight(99000)  # implausibly heavy
        self.assertEqual(b.current_fill_ml, 946)
        b.update_fill_from_weight(0)  # implausibly light
        self.assertEqual(b.current_fill_ml, 0)

    def test_empty_reads_zero_after_a_partial_refill(self):
        # The key tare-anchoring win: once a real drain establishes the empty
        # floor, empty reads 0 even when a later fill doesn't reach the brim.
        b = new_bottle(946)
        b.update_fill_from_weight(37115)  # full anchor
        b.update_fill_from_weight(35880)  # drained to empty -> learns the tare
        self.assertAlmostEqual(b.current_fill_ml, 0, delta=20)
        # Refill only part-way (anchor moves, tare must not).
        b.refill("cap_close", 36800)
        b.update_fill_from_weight(36800)  # ~partial fill
        self.assertGreater(b.current_fill_ml, 600)
        self.assertLess(b.current_fill_ml, 800)
        # Drink it all again: still reads ~0, no phantom residual.
        b.update_fill_from_weight(35882)
        self.assertAlmostEqual(b.current_fill_ml, 0, delta=20)

    def test_calibration_is_not_a_refill_but_cap_close_is(self):
        b = new_bottle(946)
        b.refill("calibration", 37000)
        self.assertEqual(b.refills_today, 0, "calibration must not count as a refill")
        b.refill("cap_close", 37100)
        self.assertEqual(b.refills_today, 1, "a real refill increments the counter")

    def test_model_seed_scale_is_used(self):
        b = state.BottleState(HomeAssistant(), "e", 621, raw_per_ml=0.9)
        self.assertEqual(b.raw_units_per_ml, 0.9)

    def test_scale_auto_calibrates_from_sips(self):
        # Seed a deliberately-wrong scale; the sip-vs-weight-drop auto-cal should
        # pull it toward the puck's true scale (independent of the seed).
        true_scale = 1.305
        b = state.BottleState(HomeAssistant(), "e", 946, raw_per_ml=0.9)
        anchor = 37000
        b.update_fill_from_weight(anchor)  # bootstrap + open calib window
        ts = 1000.0
        for _ in range(10):
            ts += 100
            b.add_sip(state.Sip(timestamp=ts, volume_ml=300))  # known 300 mL
            raw = anchor - round(300 * true_scale)  # real weight drop
            b.update_fill_from_weight(raw)
            # refill back to full -> resets the calibration window
            ts += 100
            b.refill("cap_close", anchor)
            b.update_fill_from_weight(anchor)
        self.assertAlmostEqual(b.raw_units_per_ml, true_scale, delta=0.05)

    def test_no_calibration_before_enough_drunk(self):
        b = state.BottleState(HomeAssistant(), "e", 946, raw_per_ml=0.9)
        b.update_fill_from_weight(37000)
        b.add_sip(state.Sip(timestamp=1000.0, volume_ml=50))  # below CALIBRATION_MIN_ML
        b.update_fill_from_weight(37000 - round(50 * 1.305))
        self.assertEqual(b.raw_units_per_ml, 0.9, "must not calibrate on a tiny sample")


if __name__ == "__main__":
    unittest.main(verbosity=2)
