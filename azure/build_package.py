"""Build a deployment ZIP from an explicit source allowlist; no credentials."""
from pathlib import Path
import argparse
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    entries = {}
    for filename in ("function_app.py", "cloud_worker.py", "host.json", "requirements.txt"):
        entries[filename] = ROOT / "azure" / filename
    entries['automation/telegram_reports.py'] = ROOT / 'automation/telegram_reports.py'
    entries['automation/__init__.py'] = None
    for folder in ('Scania API Integration', 'Volvo API Integration/api_v2_recovery'):
        base = ROOT / folder
        for source in base.iterdir():
            if source.suffix == '.py' or source.name in ('shifts.json', 'sasti_dataaccess.tsv', 'dudhichua_dataaccess.tsv'):
                entries[f'{folder}/{source.name}'] = source
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, source in entries.items():
            archive.writestr(name, source.read_bytes() if source else b'')
    print(f'Built {destination.name}: {len(entries)} source files; no live configuration')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    build(parser.parse_args().output)
