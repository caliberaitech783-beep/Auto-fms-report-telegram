from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleet_cache import FleetCache, CollectionPending, reconcile_inventory
import volvo_api_v2 as volvo


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fleet.sqlite3'
        self.start = datetime(2026, 10, 27, 15, tzinfo=timezone.utc)
        self.stop = self.start + timedelta(hours=6)

    def test_restart_resumes_page_and_refuses_partial_report(self):
        record = {'vin': 'VIN1', 'createdDateTime': '2026-10-27T16:00:00Z',
                  'receivedDateTime': '2026-10-27T16:01:00Z'}
        def first(start, stop, auth, on_page):
            on_page([record], record['receivedDateTime'], False)
        cache = FleetCache(self.path)
        with patch.object(volvo, 'fetch_statuses', side_effect=first):
            with self.assertRaises(CollectionPending):
                cache.collect(self.start, self.stop, {}, seconds=-1)
        with self.assertRaises(CollectionPending):
            cache.snapshots(self.start, self.stop)
        cache.close()
        cache = FleetCache(self.path)
        def second(start, stop, auth, on_page):
            self.assertEqual(start, record['receivedDateTime'])
            on_page([record], stop, True)
        with patch.object(volvo, 'fetch_statuses', side_effect=second):
            cache.collect(self.start, self.stop, {})
        self.assertEqual(len(cache.snapshots(self.start, self.stop)), 1)
        cache.close()

    def test_later_report_fetches_only_new_history_with_overlap(self):
        cache = FleetCache(self.path)
        def complete(start, stop, auth, on_page):
            on_page([], stop, True)
        with patch.object(volvo, 'fetch_statuses', side_effect=complete) as fetch:
            cache.collect(self.start, self.stop, {})
            cache.collect(self.start, self.stop + timedelta(hours=2), {})
            self.assertEqual(fetch.call_args.args[0], volvo._utc_z(self.stop - timedelta(minutes=10)))
            before = fetch.call_count
            cache.collect(self.start, self.stop, {})
            self.assertEqual(fetch.call_count, before)
        cache.close()

    def test_overnight_created_times_and_late_received_records_are_retained(self):
        cache = FleetCache(self.path)
        records = [{'vin': 'VIN1', 'createdDateTime': '2026-10-27T16:00:00Z',
                    'receivedDateTime': '2026-10-27T20:59:00Z'}]
        with patch.object(volvo, 'fetch_statuses', side_effect=lambda start, stop, auth, on_page: on_page(records, stop, True)):
            cache.collect(self.start, self.stop, {})
        self.assertEqual(cache.snapshots(self.start, self.stop), records)
        cache.close()

    def test_new_vehicle_prefix_maps_uniquely_and_unknown_is_held(self):
        config = {'sites': {'Sasti': {'vehicles': [{'vin': 'old', 'name': 'V1'}]},
                            'Jayant East': {'vehicles': []}, 'Jayant West': {'vehicles': []}}}
        inventory = [{'vin': 'old', 'customerVehicleName': 'Jayant East V1'},
                     {'vin': 'new', 'customerVehicleName': 'Sasti V2'},
                     {'vin': 'east', 'customerVehicleName': 'Jayant East V3'},
                     {'vin': 'unknown', 'customerVehicleName': 'V4'},
                     {'vin': 'bad', 'customerVehicleName': 'Sastipur V5'}]
        mapped, pending = reconcile_inventory(config, inventory, list(config['sites']), True)
        self.assertEqual([v['vin'] for v in mapped['sites']['Sasti']['vehicles']], ['old', 'new'])
        self.assertEqual([v['vin'] for v in mapped['sites']['Jayant East']['vehicles']], ['east'])
        self.assertEqual({v['vin'] for v in pending}, {'unknown', 'bad'})
        self.assertEqual(len(config['sites']['Sasti']['vehicles']), 1)
        _, held = reconcile_inventory(config, inventory, list(config['sites']), False)
        self.assertEqual(len(held), 4)

    def test_new_vehicle_enters_next_report_using_updated_membership(self):
        cache = FleetCache(self.path)
        config = {'sites': {'Sasti': {'vehicles': [{'vin': 'old'}]}}}
        now = self.stop
        with patch.dict('os.environ', {'VOLVO_AUTO_SITE_PREFIX': 'true'}), \
             patch.object(volvo, 'fetch_vehicle_inventory', return_value=[{'vin': 'new', 'customerVehicleName': 'Sasti V2'}]), \
             patch.object(volvo, 'fetch_driver_directory', return_value={'1': 'Driver'}):
            updated, _, _ = cache.metadata({}, config, ['Sasti'], now)
        self.assertIn('new', volvo.vins_for_site('Sasti', updated))
        cache.close()
        cache = FleetCache(self.path)
        self.assertIn('new', volvo.vins_for_site('Sasti', cache.get('site_config')))
        cache.close()


if __name__ == '__main__':
    unittest.main()
