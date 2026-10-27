"""Resumable fleet history and live inventory; private state is stored in Azure."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import time

import volvo_api_v2 as volvo


class CollectionPending(RuntimeError):
    """A bounded collection yielded after committing its cursor."""


class FleetCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshots (vin TEXT, event TEXT, received TEXT, body TEXT, PRIMARY KEY(vin,event))')
        self.db.execute('CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(event)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))
        self.db.commit()

    def collect(self, start, stop, auth, seconds=75):
        """Resume an interrupted received-time query before extending its end.

        Pages commit before yielding. A restart never discards completed pages,
        and the report is withheld until coverage includes its whole window.
        """
        started = time.monotonic()
        requested_stop = volvo._utc_z(stop)
        requested_start = volvo._utc_z(start)
        coverage = self.get('coverage')
        pending = self.get('pending')
        if coverage and requested_start < coverage['start']:
            raise RuntimeError('Requested history precedes saved coverage; explicit backfill required')
        if coverage and coverage['stop'] >= requested_stop:
            return
        while True:
            if not pending:
                if coverage and coverage['stop'] >= requested_stop:
                    return
                cursor = (datetime.fromisoformat(coverage['stop'].replace('Z', '+00:00'))
                          - timedelta(minutes=10)) if coverage else start
                pending = {'start': coverage['start'] if coverage else requested_start,
                           'cursor': volvo._utc_z(cursor), 'stop': requested_stop}
                self.put('pending', pending)

            def page(records, cursor, complete):
                for record in records:
                    event = volvo._timestamp(record)
                    vin = record.get('vin')
                    if event and vin:
                        self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)',
                                        (vin, volvo._utc_z(event), record.get('receivedDateTime'), json.dumps(record)))
                pending['cursor'] = cursor
                if complete:
                    self.put('coverage', {'start': pending['start'], 'stop': pending['stop']})
                    self.put('pending', None)
                else:
                    self.put('pending', pending)
                self.db.commit()
                if not complete and time.monotonic() - started >= seconds:
                    raise CollectionPending('Fleet page saved; collection will resume next invocation')

            volvo.fetch_statuses(pending['cursor'], pending['stop'], auth, on_page=page)
            coverage = self.get('coverage')
            pending = self.get('pending')
            if time.monotonic() - started >= seconds and coverage['stop'] < requested_stop:
                raise CollectionPending('Saved earlier query; remaining data awaits next collection')
            # Trim only after a complete query. Keep three days for overnight windows.
            floor = stop - timedelta(days=3)
            self.db.execute('DELETE FROM snapshots WHERE event < ?', (volvo._utc_z(floor),))
            if coverage['start'] < volvo._utc_z(floor):
                coverage['start'] = volvo._utc_z(floor)
                self.put('coverage', coverage)
            self.db.commit()

    def snapshots(self, start, stop):
        coverage = self.get('coverage')
        if not coverage or coverage['start'] > volvo._utc_z(start) or coverage['stop'] < volvo._utc_z(stop):
            raise CollectionPending('Report window is not fully collected')
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT body FROM snapshots WHERE event>=? AND event<=? ORDER BY event',
            (volvo._utc_z(start), volvo._utc_z(stop)))]

    def metadata(self, auth, site_cfg, sites, now, force_inventory=False):
        previous = self.get('inventory_checked')
        # Refresh before every cutoff, while caching directory calls between cutoffs.
        report_refresh = force_inventory and self.get('inventory_cutoff') != now.isoformat()
        if report_refresh or not previous or (now - datetime.fromisoformat(previous)).total_seconds() >= 3600:
            inventory = volvo.fetch_vehicle_inventory(auth)
            self.put('inventory', inventory)
            self.put('inventory_checked', now.isoformat())
            if force_inventory:
                self.put('inventory_cutoff', now.isoformat())
        inventory = self.get('inventory', [])
        result, pending = reconcile_inventory(site_cfg, inventory, sites,
                    os.environ.get('VOLVO_AUTO_SITE_PREFIX', 'false').lower() == 'true')
        self.put('site_config', result)
        self.put('unassigned_vehicles', pending)
        checked = self.get('drivers_checked')
        if not checked or (now - datetime.fromisoformat(checked)).total_seconds() >= 21600:
            drivers = volvo.fetch_driver_directory(auth)
            if drivers:
                self.put('drivers', drivers)
                self.put('drivers_checked', now.isoformat())
        return result, {v['vin']: v.get('customerVehicleName', '') for v in inventory}, self.get('drivers', {})


def reconcile_inventory(config, inventory, sites, allow_prefix=False):
    """Retain authoritative saved mappings; never guess a VIN's destination."""
    config = json.loads(json.dumps(config))
    known = {v['vin']: site for site, node in config['sites'].items()
             for v in node.get('vehicles', []) if v.get('vin') and site in sites}
    pending = []
    for item in inventory:
        vin = item['vin']
        name = str(item.get('customerVehicleName') or '').strip()
        if vin in known:
            continue
        # A site prefix must be complete and separated from the vehicle name.
        matches = [site for site in sites if re.match(r'^' + re.escape(site) + r'(?:\s|[:|/_-])', name, re.I)] if allow_prefix else []
        if len(matches) != 1:
            pending.append({'vin': vin, 'name': name, 'reason': 'site assignment required'})
            continue
        site = matches[0]
        for node in config['sites'].values():
            node['vehicles'] = [v for v in node.get('vehicles', []) if v.get('vin') != vin]
            node['count'] = len(node['vehicles'])
        node = config['sites'][site]
        node['vehicles'].append({'vin': vin, 'name': name, 'id': None, 'reg': None})
        node['count'] = len(node['vehicles'])
        known[vin] = site
    return config, pending
