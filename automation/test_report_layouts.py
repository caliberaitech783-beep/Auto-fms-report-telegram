"""Check actual speed units, missing data, weighted summaries and PDF columns."""
import importlib.util
from pathlib import Path
import tempfile
import sys
from datetime import datetime, timedelta
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


volvo = module('layout_volvo', 'Volvo API Integration/api_v2_recovery/volvo_api_v2.py')
vpdf = module('layout_vpdf', 'Volvo API Integration/api_v2_recovery/api_pdf_report.py')
scania = module('layout_scania', 'Scania API Integration/scania_api.py')
spdf = module('layout_spdf', 'Scania API Integration/scania_pdf_report.py')


class LayoutTests(unittest.TestCase):
    def test_speed_uses_driving_time_and_handles_missing_or_zero_time(self):
        v = {'distance_km':60,'fuel_l':20,'engine_hours':4,'driving_time_s':7200,'standstill_time_s':7200}
        s = {'distance_m':60000,'fuel_ml':20000,'engine_h':4,'moving_s':7200,'idle_s':7200}
        self.assertEqual(volvo.compute_row('VIN',v,{})['Avg Speed (km/h)'],30)
        self.assertEqual(scania.compute_row('VIN',s,{})['Avg Speed (km/h)'],30)
        for seconds in (None,0):
            v['driving_time_s']=seconds
            self.assertEqual(volvo.compute_row('VIN',v,{})['Avg Speed (km/h)'],'')
            s.pop('moving_s',None)
            if seconds is not None:s['moving_s']=seconds
            self.assertIsNone(scania.compute_row('VIN',s,{})['Avg Speed (km/h)'])

    def test_summary_speed_weights_distance_and_driving_time(self):
        self.assertEqual(vpdf.fleet_average_speed([
            {'driving_km':60,'driving_seconds':3600},
            {'driving_km':60,'driving_seconds':10800},
            {'driving_km':100,'driving_seconds':None}]),30)
        self.assertEqual(spdf._summary_row([
            {'Distance driven':60,'Driving seconds':3600},
            {'Distance driven':60,'Driving seconds':10800},
            {'Distance driven':100,'Driving seconds':None}])['Avg Speed (km/h)'],30)

    def test_generated_pdf_has_speed_and_no_removed_columns_or_legends(self):
        from pypdf import PdfReader
        start=datetime(2026,11,3,5)
        v=volvo.compute_row('VIN',{'distance_km':60,'fuel_l':20,'engine_hours':4,
            'driving_time_s':7200,'standstill_time_s':7200},{'registration':'SAMPLE V1'})
        s=scania.compute_row('VIN',{'distance_m':60000,'fuel_ml':20000,'engine_h':4,
            'moving_s':7200,'idle_s':7200},{'name':'SAMPLE S1'})
        with tempfile.TemporaryDirectory() as folder:
            a=vpdf.build_pdf([v],'SAMPLE','A',start,start+timedelta(hours=4),folder)['pdf']
            b=spdf.build_pdf([s],'SAMPLE','A',start,start+timedelta(hours=4),folder)
            for path in (a,b):
                text=' '.join(' '.join(p.extract_text() for p in PdfReader(str(path)).pages).split())
                self.assertIn('Avg Speed',text)
                self.assertNotIn('I-Shift',text)
                self.assertNotIn('l/h',text)
                self.assertNotIn('litres/h',text)
        self.assertEqual(vpdf.KEYS[8],'avg_speed')
        self.assertEqual(len(vpdf.HEADERS),len(vpdf.KEYS))
        self.assertEqual(len(vpdf.KEYS),len(vpdf.WIDTHS))


if __name__ == '__main__':unittest.main()

