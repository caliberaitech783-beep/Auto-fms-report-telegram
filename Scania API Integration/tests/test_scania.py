import base64
import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pypdf import PdfReader

from scania_api import _time, challenge_response, compute_row, match_site_vehicles
from scania_pdf_report import apply_adjusted_idling, build_pdf
from report_runner import report_window
from run_scheduled_reports import due_missing_reports, expected_path, report_due


class ScaniaTests(unittest.TestCase):
    def test_scheduled_report_catchup_finds_only_due_missing_files(self):
        now = datetime(2026, 8, 6, 12, 0)
        shifts = {"sites": {"Test Site": {"reports": {
            "A1": ["05:00", "10:30"], "A2": ["05:00", "12:30"],
        }}}}
        sites = {"sites": {"Test Site": {"api_enabled": True}}}
        due = list(due_missing_reports(now, shifts, sites, lookback_days=0))
        self.assertEqual(due, [("Test Site", "A1", now.date())])
        self.assertTrue(report_due(now.date(), "10:30", now))
        self.assertFalse(report_due(now.date(), "12:30", now))
        self.assertEqual(
            expected_path("Test Site", "A1", now.date(), "10:30").name,
            "Test_Site_A1_20260806_1030.pdf",
        )

    def test_default_catchup_excludes_previous_dates(self):
        now = datetime(2026, 8, 6, 12, 0)
        shifts = {"sites": {"Test Site": {"reports": {
            "A1": ["05:00", "10:30"],
        }}}}
        sites = {"sites": {"Test Site": {"api_enabled": True}}}
        due = list(due_missing_reports(now, shifts, sites))
        self.assertEqual(due, [("Test Site", "A1", now.date())])

    def test_challenge_response_matches_hmac(self):
        secret_raw = b"secret-key"
        challenge_raw = b"challenge"
        enc = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
        result = challenge_response(enc(secret_raw), enc(challenge_raw))
        self.assertEqual(result, "hQ3ENpyYlI0IZDc1CLyrOiYrtqayVSP95dWP7sK9W5A")

    def test_metric_calculation(self):
        row = compute_row("VIN1", {
            "engine_h": 3, "distance_m": 60000, "fuel_ml": 50000,
            "idle_s": 3600, "mode_meters": 1200, "coast_meters": 12000,
        }, {"name": "S548"})
        self.assertEqual(row["Engine running time"], "3:00")
        self.assertEqual(row["Consumption km/l"], 1.2)
        self.assertEqual(row["Idling %"], 33.33)
        self.assertEqual(row["Power %"], 2.0)
        self.assertEqual(row["Coasting %"], 20.0)
        stationary = compute_row("VIN2", {
            "engine_h": 1, "distance_m": 0, "fuel_ml": 2000, "idle_s": 3600,
        }, {"name": "S 1"})
        self.assertEqual(stationary["Coasting %"], 0.0)

    def test_exact_production_report_windows(self):
        shifts = {"sites": {"Sasti Chandrapur": {"reports": {
            "B2": ["13:00", "17:00"], "C2": ["21:00", "01:00"]
        }}}}
        start, stop = report_window("Sasti Chandrapur", "B2", "2026-07-23", shifts)
        self.assertEqual((start.hour, stop.hour), (13, 17))
        start, stop = report_window("Sasti Chandrapur", "C2", "2026-07-23", shifts)
        self.assertEqual((start.day, stop.day, stop.hour), (23, 24, 1))

    def test_subscribed_vin_mapping_uses_registration(self):
        vin = "MYKG8X4F005616232"
        config = {"sites": {"Sasti Chandrapur": {
            "vins": [vin],
            "registrations": [],
            "vin_registration_map": {vin: "S 38"},
        }}}
        matched = match_site_vehicles([{"vin": vin, "brand": "SCANIA"}], config, "Sasti Chandrapur")
        self.assertEqual(matched[vin]["name"], "S 38")

    def test_full_subscription_manifest_contains_82_vehicles(self):
        config = json.loads(Path("site_config.json").read_text(encoding="utf-8"))
        rows = []
        for line in Path("sasti_dataaccess.tsv").read_text(encoding="utf-8").splitlines()[1:]:
            vin = line.split("\t", 1)[0]
            rows.append({"vin": vin, "brand": "SCANIA"})
        matched = match_site_vehicles(rows, config, "Sasti Chandrapur")
        self.assertEqual(len(rows), 82)
        self.assertEqual(len(matched), 82)
        self.assertEqual(matched["MYKG8X4N005595662"]["name"], "S 53")

    def test_dudhichua_subscription_manifest_contains_22_vehicles(self):
        config = json.loads(Path("site_config.json").read_text(encoding="utf-8"))
        rows = []
        for line in Path("dudhichua_dataaccess.tsv").read_text(encoding="utf-8").splitlines()[1:]:
            vin = line.split("\t", 1)[0]
            rows.append({"vin": vin, "brand": "SCANIA"})
        matched = match_site_vehicles(rows, config, "Dudhichua Singrauli")
        self.assertEqual(len(rows), 22)
        self.assertEqual(len(matched), 22)
        self.assertEqual(matched["MYKG8X4S005727527"]["name"], "S483")
        self.assertEqual(matched["MYKG8X4S005729563"]["name"], "S550")

    def test_scania_naive_timestamp_is_treated_as_utc(self):
        value = _time({"createdDateTime": "2026-07-28T11:30:00"})
        self.assertIsNotNone(value.tzinfo)
        self.assertEqual(value.utcoffset(), timedelta(0))

    def test_pdf_is_a4_portrait_and_has_thresholds(self):
        rows = []
        for index in range(48):
            rows.append({
                "Registration number": f"S {index + 1}", "Engine running time": "2:30",
                "engine_minutes": 150, "Distance driven": 30 + index / 10,
                "Consumption km/l": 0.6, "Consumption litres": 50,
                "Consumption litres/h": 20, "Idling %": 35 - index / 3,
                "Idling time": "0:50", "Power km": 0, "Power %": 0,
                "Coasting %": 15,
            })
        with tempfile.TemporaryDirectory() as directory:
            start = datetime(2026, 7, 20, 13, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            path = build_pdf(rows, "Sasti Chandrapur", "B", start, start + timedelta(hours=8), directory)
            pdf = PdfReader(str(path))
            self.assertGreaterEqual(len(pdf.pages), 1)
            box = pdf.pages[0].mediabox
            self.assertLess(float(box.width), float(box.height))
            self.assertAlmostEqual(float(box.width), 595.28, delta=1)
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            self.assertIn("Summary\n(48)", text)
            self.assertIn("Thresholds:", text)
            self.assertIn("coasting < 20%", text)
            self.assertIn("Adjusted", text)
            self.assertIn("adjusted idling > 28%", text)

    def test_adjusted_idling_matches_macro_logic(self):
        rows = [
            {"engine_minutes": 60, "Distance driven": 20, "Consumption litres/h": 10, "Idling %": 40},
            {"engine_minutes": 60, "Distance driven": 30, "Consumption litres/h": 30, "Idling %": 40},
            {"engine_minutes": 60, "Distance driven": 5, "Consumption litres/h": 100, "Idling %": 40},
        ]
        average = apply_adjusted_idling(rows)
        self.assertEqual(average, 20)
        self.assertEqual(rows[0]["Adjusted Idling %"], 20.0)
        self.assertEqual(rows[1]["Adjusted Idling %"], 40.0)
        self.assertEqual(rows[2]["Adjusted Idling %"], 40.0)

    def test_low_activity_lph_outlier_does_not_distort_adjusted_idling(self):
        rows = [
            {"engine_minutes": 180, "Distance driven": 35, "Consumption litres/h": 20,
             "Idling %": 40},
            {"engine_minutes": 17, "Distance driven": 988, "Consumption litres/h": 6610,
             "Idling %": 95},
        ]
        average = apply_adjusted_idling(rows)
        self.assertEqual(average, 20)
        self.assertEqual(rows[0]["Adjusted Idling %"], 40.0)

    def test_pdf_is_created_when_no_vehicle_meets_runtime_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            start = datetime(2026, 7, 29, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            path = build_pdf([], "Dudhichua Singrauli", "A2", start,
                             start + timedelta(hours=6, minutes=45), directory)
            text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
            self.assertIn("No operating vehicles met both the 00:30", text)
            self.assertIn("Operating: 0 | Low activity: 0", text)

    def test_pdf_moves_low_activity_vehicles_to_second_page(self):
        base = {
            "Engine running time": "1:00", "engine_minutes": 60,
            "Consumption km/l": 0.6, "Consumption litres": 25,
            "Consumption litres/h": 25, "Idling %": 20, "Idling time": "0:12",
            "Power km": 0, "Power %": 0, "Coasting %": 20,
        }
        rows = [
            {**base, "Registration number": "LOW", "Distance driven": 14.99},
            {**base, "Registration number": "EDGE", "Distance driven": 15.0},
            {**base, "Registration number": "ACTIVE", "Distance driven": 15.01},
            {**base, "Registration number": "SHORT_TIME", "Distance driven": 20.0,
             "Engine running time": "0:29", "engine_minutes": 29},
        ]
        with tempfile.TemporaryDirectory() as directory:
            start = datetime(2026, 8, 5, 13, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            path = build_pdf(rows, "Sasti Chandrapur", "B1", start,
                             start + timedelta(hours=2), directory)
            pdf = PdfReader(str(path))
            self.assertEqual(len(pdf.pages), 2)
            page1 = pdf.pages[0].extract_text() or ""
            page2 = pdf.pages[1].extract_text() or ""
            self.assertIn("ACTIVE", page1)
            self.assertNotIn("LOW", page1)
            self.assertNotIn("EDGE", page1)
            self.assertIn("LOW ACTIVITY VEHICLES", page2)
            self.assertIn("LOW", page2)
            self.assertIn("EDGE", page2)
            self.assertIn("SHORT_T", page2)
            self.assertIn("IGNITION\nONLY", page2)
            self.assertIn("Summary\n(1)", page1)


if __name__ == "__main__":
    unittest.main()
