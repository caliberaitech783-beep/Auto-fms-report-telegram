import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from api_pdf_report import normalize_and_filter_rows, report_filename
from api_report_runner import plan_missing_reports, scheduled_cutoffs
from asset_group_sync import UNKNOWN_SITE, membership_from_rows
from volvo_api_v2 import driver_name_for_window


IST = "+05:30"
SITES = {
    "sites": {
        "Test Site": {
            "A": ["04:00", "11:59"],
            "B": ["12:00", "19:59"],
            "C": ["20:00", "03:59"],
        }
    }
}


def when(value):
    return datetime.fromisoformat(value + IST)


def api_row(distance):
    return {
        "Vehicle": "V001", "Driver": "Driver", "Total time": "01:00",
        "Driving km": distance, "Diesel litres": 10, "Avg km/l": 1.5,
        "Standstill %": 20, "Standstill time": "00:12", "I-Shift %": 0,
        "Above economy %": 0, "Engine load %": 10, "Coasting %": 25,
        "Avg l/h": 10,
    }


class RecoveryPlanningTests(unittest.TestCase):
    def test_cloud_unknown_sites_do_not_reopen_previous_night(self):
        import os, json
        from unittest.mock import patch
        from api_report_runner import configured_sites, SHIFTS_FILE
        shifts = json.loads(SHIFTS_FILE.read_text())
        with patch.dict(os.environ, {'TELEGRAM_ROUTES_JSON':json.dumps({'Sasti':'group','Jayant':'group'})}):
            sites = configured_sites({'sites':{'Sasti':{},'Jayant East':{},'Unknown sites':{}}})
        self.assertEqual(sites,['Sasti','Jayant East'])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            previous = plan_missing_reports(when('2026-10-27T09:00:00'),sites,shifts,out)
            for job in previous:
                job['expected'].touch()
            jobs = plan_missing_reports(when('2026-10-27T11:30:00'),sites,shifts,out)
        self.assertEqual(len(jobs),2)
        self.assertEqual(min(job['start'].date().isoformat() for job in jobs),'2026-10-27')

    def test_empty_report_marker_prevents_repeated_old_window(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            previous = plan_missing_reports(when('2026-10-27T09:00:00'),['Test Site'],SITES,out)
            for job in previous:
                Path(str(job['expected'])+'.empty.json').write_text('{"reason":"no qualifying vehicles"}')
            jobs = plan_missing_reports(when('2026-10-27T11:30:00'),['Test Site'],SITES,out)
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['cutoff'],when('2026-10-27T11:30:00'))
        self.assertEqual(jobs[0]['start'],when('2026-10-27T04:00:00'))

    def test_production_sites_use_previous_evening_for_three_am(self):
        import json
        from api_report_runner import SHIFTS_FILE
        shifts = json.loads(SHIFTS_FILE.read_text())
        with tempfile.TemporaryDirectory() as directory:
            jobs = plan_missing_reports(when('2026-10-23T03:00:00'),
                       ['Sasti','Majri','Jayant East','Jayant West'], shifts, Path(directory))
        self.assertEqual(len(jobs), 4)
        for job in jobs:
            self.assertEqual(job['shift'], 'C')
            self.assertEqual(job['start'].date().isoformat(), '2026-10-22')
            self.assertEqual(job['start'].hour, 20 if 'Jayant' in job['site'] else 21)
            self.assertEqual(job['cutoff'], when('2026-10-23T03:00:00'))

    def test_cutover_excludes_earlier_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = plan_missing_reports(when('2026-10-23T23:00:00'), ['Test Site'], SITES,
                       Path(directory), since=when('2026-10-23T18:01:00'))
        self.assertEqual([job['cutoff'].hour for job in jobs], [23])

    def test_same_date_missed_reports_are_selected(self):
        cutoffs = scheduled_cutoffs(when("2026-08-06T16:00:00"))
        self.assertEqual([c.strftime("%H:%M") for c in cutoffs],
                         ["03:00", "09:00", "11:30", "16:00"])

    def test_previous_dates_are_excluded(self):
        cutoffs = scheduled_cutoffs(when("2026-08-06T16:00:00"))
        self.assertTrue(cutoffs)
        self.assertEqual({c.date().isoformat() for c in cutoffs}, {"2026-08-06"})
        self.assertNotIn("2026-08-05", {c.date().isoformat() for c in cutoffs})

    def test_future_report_times_are_excluded(self):
        cutoffs = scheduled_cutoffs(when("2026-08-06T16:00:00"))
        values = {c.strftime("%H:%M") for c in cutoffs}
        self.assertNotIn("18:00", values)
        self.assertNotIn("23:00", values)

    def test_existing_output_is_not_planned_again(self):
        now = when("2026-08-06T03:00:00")
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox = Path(temp_dir)
            start = when("2026-08-05T20:00:00")
            existing = outbox / report_filename("Test Site", "C", start, now)
            existing.touch()
            jobs = plan_missing_reports(now, ["Test Site"], SITES, outbox)
        self.assertEqual(jobs, [])


class DistanceFilteringTests(unittest.TestCase):
    def test_distance_below_15_km_is_excluded(self):
        self.assertEqual(normalize_and_filter_rows([api_row(14.99)]), [])

    def test_exactly_15_km_is_included(self):
        rows = normalize_and_filter_rows([api_row(15.0)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["driving_km"], 15.0)


class DriverResolutionTests(unittest.TestCase):
    def test_driver_name_uses_id_reported_inside_window(self):
        snapshots = [
            {"createdDateTime": "2026-08-13T00:00:00Z",
             "driver1Id": {"oemDriverIdentification": {"oemDriverIdentification": "111"}}},
            {"createdDateTime": "2026-08-13T01:00:00Z",
             "driver1Id": {"oemDriverIdentification": {"oemDriverIdentification": "222"}}},
            {"createdDateTime": "2026-08-13T02:00:00Z",
             "driver1Id": {"oemDriverIdentification": {"oemDriverIdentification": "222"}}},
        ]
        name = driver_name_for_window(
            snapshots, when("2026-08-13T05:00:00"),
            when("2026-08-13T08:00:00"), {"111": "Old Driver", "222": "Current Driver"})
        self.assertEqual(name, "Current Driver")

    def test_missing_driver_id_remains_unknown(self):
        snapshots = [{"createdDateTime": "2026-08-13T01:00:00Z",
                      "driver1Id": {"oemDriverIdentification": {
                          "oemDriverIdentification": ""}}}]
        name = driver_name_for_window(
            snapshots, when("2026-08-13T05:00:00"),
            when("2026-08-13T08:00:00"), {})
        self.assertEqual(name, "Unknown")


def asset_row(name, group, status="Active", subscription="Fuel & Environment"):
    return {"name": name, "groups": group, "status": status,
            "subscriptions": subscription, "vin": f"VIN-{name}"}


class AssetGroupSyncTests(unittest.TestCase):
    def test_grouped_eligible_vehicle_uses_public_group(self):
        membership, summary = membership_from_rows(
            [asset_row("101", "Sasti")], ["Sasti", "Majri"])
        self.assertEqual([v["vin"] for v in membership["Sasti"]], ["VIN-101"])
        self.assertEqual(summary["mapped"], 1)

    def test_ungrouped_eligible_vehicle_uses_unknown_sites(self):
        membership, _ = membership_from_rows(
            [asset_row("102", "-")], ["Sasti"])
        self.assertEqual([v["vin"] for v in membership[UNKNOWN_SITE]], ["VIN-102"])

    def test_unrecognized_group_uses_unknown_sites(self):
        membership, _ = membership_from_rows(
            [asset_row("103", "New public group")], ["Sasti"])
        self.assertEqual(len(membership[UNKNOWN_SITE]), 1)

    def test_grouped_vehicle_without_package_remains_in_group(self):
        membership, summary = membership_from_rows(
            [asset_row("104", "Sasti", subscription="Positioning")], ["Sasti"])
        self.assertEqual(len(membership["Sasti"]), 1)
        self.assertEqual(summary["mapped"], 1)

    def test_ungrouped_vehicle_without_package_is_excluded(self):
        membership, summary = membership_from_rows(
            [asset_row("105", "-", subscription="Positioning")], ["Sasti"])
        self.assertEqual(membership[UNKNOWN_SITE], [])
        self.assertEqual(summary["mapped"], 0)

    def test_failed_but_subscribed_ungrouped_vehicle_uses_unknown_sites(self):
        membership, _ = membership_from_rows(
            [asset_row("107", "-", status="Failed")], ["Sasti"])
        self.assertEqual(len(membership[UNKNOWN_SITE]), 1)

    def test_duplicate_eligible_vin_is_rejected(self):
        row = asset_row("106", "Sasti")
        with self.assertRaisesRegex(ValueError, "Duplicate eligible VIN"):
            membership_from_rows([row, dict(row)], ["Sasti"])


if __name__ == "__main__":
    unittest.main()
