import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('build_package', Path(__file__).with_name('build_package.py'))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackageTests(unittest.TestCase):
    def test_sources_are_readable_on_linux_and_live_settings_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'deployment.zip'
            builder.build(target)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                self.assertIn('host.json', names)
                self.assertIn('function_app.py', names)
                self.assertFalse(any('config.json' in name or '.sqlite' in name or '.env' in name for name in names))
                self.assertTrue(all((item.external_attr >> 16) & 0o777 == 0o644 for item in archive.infolist()))


if __name__ == '__main__':
    unittest.main()
