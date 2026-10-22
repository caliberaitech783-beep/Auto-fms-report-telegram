import json
import tempfile
import unittest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import patch

import run_scheduled_reports as schedule
from report_runner import report_window


class ScheduleTests(unittest.TestCase):
    def test_six_cutoffs_and_previous_shift_date_at_three_am(self):
        shifts = json.loads((schedule.BASE / 'shifts.json').read_text())
        sites = {'sites': {site: {'api_enabled': True} for site in shifts['sites']}}
        with tempfile.TemporaryDirectory() as temp, patch.object(schedule, 'BASE', Path(temp)):
            jobs = list(schedule.due_missing_reports(datetime(2026, 10, 23, 3), shifts, sites))
            self.assertEqual(len(jobs), 2)
            for site, report, day in jobs:
                self.assertEqual((report, day), ('C3', date(2026, 10, 22)))
                start, stop = report_window(site, report, day.isoformat(), shifts)
                self.assertEqual(start.isoformat(), '2026-10-22T21:00:00+05:30')
                self.assertEqual(stop.isoformat(), '2026-10-23T03:00:00+05:30')
            jobs = list(schedule.due_missing_reports(datetime(2026, 10, 23, 23), shifts, sites))
            self.assertEqual(len(jobs), 12)
            self.assertEqual({job[1] for job in jobs}, {'A1','A2','B1','B2','C1','C3'})

    def test_activation_skips_earlier_cutoffs_and_exact_file_prevents_repeat(self):
        shifts = json.loads((schedule.BASE / 'shifts.json').read_text())
        sites = {'sites': {site: {'api_enabled': True} for site in shifts['sites']}}
        with tempfile.TemporaryDirectory() as temp, patch.object(schedule, 'BASE', Path(temp)):
            jobs = list(schedule.due_missing_reports(datetime(2026, 10, 23, 23), shifts, sites,
                                                     since=datetime(2026, 10, 23, 18, 1)))
            self.assertEqual([job[1] for job in jobs], ['C1','C1'])
            site, report, day = jobs[0]
            path = schedule.expected_path(site, report, day, '23:00')
            path.parent.mkdir(parents=True)
            path.touch()
            self.assertEqual(len(list(schedule.due_missing_reports(datetime(2026, 10, 23, 23), shifts, sites,
                                         since=datetime(2026, 10, 23, 18, 1)))), 1)
