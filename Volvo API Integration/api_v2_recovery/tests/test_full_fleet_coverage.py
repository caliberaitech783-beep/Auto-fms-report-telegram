"""A site's assigned fleet must not disappear behind the activity threshold."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from pypdf import PdfReader
from api_pdf_report import build_pdf
from asset_group_sync import sync_asset_groups
from fleet_cache import FleetCache

BASE = Path(__file__).resolve().parents[1]
COUNTS = {'Dhoptala':30,'Dudhichua East':164,'Dudhichua West':118,'Gouri':132,
          'Jayant East':66,'Jayant Hard Bench':48,'Jayant West Top':106,'Majri':92,'Sasti':19}


def coverage_rows():
    vehicles=json.loads((BASE/'site_assignments.json').read_text())['sites']['Sasti']['vehicles']
    rows=[]
    for index, vehicle in enumerate(vehicles):
        row={'Vehicle':vehicle['name'],'_vin':vehicle['vin'],'Driver':'Unknown',
             'Total time':'02:00','Driving km':30,'Diesel litres':10,'Avg km/l':3,
             'Standstill %':20,'Standstill time':'00:24','Driving seconds':5760,
             'Avg Speed (km/h)':18.75}
        if 12<=index<17:
            row.update({'Total time':'00:20','Driving km':5})
        elif index>=17:
            row.update({'Total time':'','Driving km':'','_data_status':'No API history for this period',
                        'Diesel litres':'','Avg km/l':'','Avg Speed (km/h)':''})
        rows.append(row)
    return rows


class FullFleetTests(unittest.TestCase):
    def test_manifest_restores_all_source_assignments_and_renamed_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shutil.copyfile(BASE/'site_assignments.json',root/'site_assignments.json')
            config=root/'site_config.json'
            config.write_text(json.dumps({'sites':{'Sasti':{'vehicles':[]}}}))
            current, info=sync_asset_groups(config,json.loads((BASE/'shifts.json').read_text()),root)
            self.assertEqual({s:n['count'] for s,n in current['sites'].items() if s!='Unknown sites'},COUNTS)
            self.assertEqual(info['mapped'],775)
            vins=[v['vin'] for n in current['sites'].values() for v in n['vehicles']]
            self.assertEqual(len(set(vins)),775)
            cache=FleetCache(root/'cache.sqlite3')
            cache.put('site_config',{'sites':{'Sasti':{'vehicles':[{'vin':'STALE'}]}}})
            cache.put('assignment_revision','old')
            refreshed=cache.use_assignments(current)
            self.assertEqual(len(refreshed['sites']['Sasti']['vehicles']),19)
            self.assertNotIn('STALE',{v['vin'] for v in refreshed['sites']['Sasti']['vehicles']})
            cache.close()
            cache=FleetCache(root/'cache.sqlite3')
            self.assertEqual(len(cache.use_assignments(current)['sites']['Sasti']['vehicles']),19)
            cache.close()

    def test_nineteen_assigned_twelve_operating_all_nineteen_present_in_pdf(self):
        rows=coverage_rows()
        with tempfile.TemporaryDirectory() as folder:
            start=datetime(2026,11,6,5)
            result=build_pdf(rows,'Sasti','A',start,start+timedelta(hours=4),folder)
            self.assertEqual(result['vehicles'],19)
            self.assertEqual(result['operating_vehicles'],12)
            self.assertEqual(result['low_activity_vehicles'],5)
            self.assertEqual(result['unavailable_vehicles'],2)
            text=' '.join(' '.join(p.extract_text() for p in PdfReader(result['pdf']).pages).split())
            self.assertIn('Total vehicles: 19',text)
            for row in rows:self.assertIn(row['Vehicle'],text)
            self.assertIn('Low activity vehicles (5)',text)
            self.assertIn('API data unavailable (2)',text)
            self.assertIn('Avg Speed',text)
            self.assertNotIn('I-Shift',text)

    def test_all_unavailable_fleet_still_generates_a_complete_report(self):
        rows=[{'Vehicle':r['Vehicle'],'_vin':r['_vin'],'_data_status':'No API history for this period'}
              for r in coverage_rows()]
        with tempfile.TemporaryDirectory() as folder:
            start=datetime(2026,11,6,5)
            result=build_pdf(rows,'Sasti','A',start,start+timedelta(hours=4),folder)
            self.assertEqual(result['vehicles'],19)
            self.assertEqual(result['unavailable_vehicles'],19)
            self.assertEqual(result['operating_vehicles'],0)
            text=' '.join(p.extract_text() for p in PdfReader(result['pdf']).pages)
            for row in rows:self.assertIn(row['Vehicle'],text)


if __name__=='__main__':unittest.main()
