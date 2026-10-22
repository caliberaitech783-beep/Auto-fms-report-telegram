import unittest
from datetime import datetime, timezone

import volvo_api_v2 as api


def snap(at, distance_m, fuel_ml, engine_h, moving_s, still_s):
    return {
        "vin": "TESTVIN",
        "createdDateTime": at,
        "hrTotalVehicleDistance": distance_m,
        "engineTotalFuelUsed": fuel_ml,
        "totalEngineHours": engine_h,
        "accumulatedData": {
            "durationWheelbaseSpeedOverZero": moving_s,
            "durationWheelbaseSpeedZero": still_s,
            "engineSpeedClass": [
                {"from": 400, "to": 800, "meters": distance_m * 0.1},
                {"from": 1600, "to": 2000, "meters": distance_m * 0.2},
            ],
            "engineTorqueClass": [
                {"from": 80, "to": 100, "seconds": moving_s * 0.05},
            ],
            "volvoGroupAccumulated": {
                "transmissionModeSeconds": [
                    {"label": "AUTO", "value": moving_s * 0.90},
                    {"label": "MANUAL", "value": moving_s * 0.099},
                    {"label": "POWER", "value": moving_s * 0.001},
                ],
            },
        },
    }


class VolvoReportTests(unittest.TestCase):
    def test_naive_times_are_ist(self):
        dt = api.parse_when("2026-07-16T07:00:00")
        self.assertEqual(api._utc_z(dt), "2026-07-16T01:30:00Z")

    def test_score_dates_do_not_request_a_future_day(self):
        same_start = api.parse_when("2026-07-23T05:00:00")
        same_stop = api.parse_when("2026-07-23T11:45:00")
        self.assertEqual(api._shift_dates(same_start, same_stop),
                         ("2026-07-23", "2026-07-23"))

        overnight_start = api.parse_when("2026-07-22T20:00:00")
        overnight_stop = api.parse_when("2026-07-23T03:00:00")
        self.assertEqual(api._shift_dates(overnight_start, overnight_stop),
                         ("2026-07-22", "2026-07-23"))

        midnight_start = api.parse_when("2026-07-16T00:00:00")
        midnight_stop = api.parse_when("2026-07-17T00:00:00")
        self.assertEqual(api._shift_dates(midnight_start, midnight_stop),
                         ("2026-07-16", "2026-07-16"))

    def test_interpolates_exact_window_and_computes_row(self):
        snaps = [
            snap("2026-07-16T00:00:00Z", 100_000, 50_000, 10, 20_000, 5_000),
            snap("2026-07-16T02:00:00Z", 120_000, 60_000, 12, 25_400, 6_800),
        ]
        start = datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)
        stop = datetime(2026, 7, 16, 1, 30, tzinfo=timezone.utc)
        w = api.window_diffs(snaps, start, stop)
        self.assertAlmostEqual(w["distance_km"], 10.0)
        self.assertAlmostEqual(w["fuel_l"], 5.0)
        self.assertAlmostEqual(w["driving_time_s"], 2700.0)
        self.assertAlmostEqual(w["standstill_time_s"], 900.0)
        row = api.compute_row("TESTVIN", w, {"registration": "V TEST", "driver": "Unknown"})
        self.assertEqual(row["Total time"], "01:00")
        self.assertEqual(row["Driving km"], 10.0)
        self.assertEqual(row["Standstill %"], 25.0)
        self.assertEqual(row["I-Shift %"], 0.1)
        self.assertEqual(row["Above economy %"], 20.0)
        self.assertEqual(row["Engine load %"], 5.0)
        self.assertEqual(row["Coasting %"], 10.0)

    def test_event_snapshot_without_histograms_does_not_corrupt_bins(self):
        a = snap("2026-07-16T00:00:00Z", 0, 0, 0, 0, 0)
        event = {
            "vin": "TESTVIN",
            "createdDateTime": "2026-07-16T01:00:00Z",
            "hrTotalVehicleDistance": 10_000,
            "engineTotalFuelUsed": 5_000,
            "totalEngineHours": 1,
        }
        b = snap("2026-07-16T02:00:00Z", 20_000, 10_000, 2, 5_400, 1_800)
        start = datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)
        stop = datetime(2026, 7, 16, 1, 30, tzinfo=timezone.utc)
        w = api.window_diffs([a, event, b], start, stop)
        self.assertTrue(w["engine_speed_class_bins"])
        self.assertAlmostEqual(w["distance_km"], 10.0)


if __name__ == "__main__":
    unittest.main()
