"""Refresh report-site VIN membership from a Volvo Connect asset export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import openpyxl


UNKNOWN_SITE = "Unknown sites"
EXPORT_PATTERN = "Caliber Mercantile - assets - *.xlsx"
REQUIRED_COLUMNS = {
    "name": "Name",
    "groups": "Groups",
    "status": "Service Status",
    "subscriptions": "Service Subscriptions",
    "vin": "VIN",
}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _group_names(value: object) -> list[str]:
    text = _clean(value)
    if not text or text == "-":
        return []
    return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]


def has_fuel_environment(row: dict[str, object]) -> bool:
    return "fuel & environment" in _clean(row.get("subscriptions")).casefold()


def membership_from_rows(rows: list[dict[str, object]],
                         known_sites: list[str]) -> tuple[dict[str, list[dict]], dict]:
    """Map public-group assets and package-eligible ungrouped assets."""
    known = set(known_sites)
    membership = {site: [] for site in known_sites}
    membership[UNKNOWN_SITE] = []
    seen_vins: set[str] = set()
    excluded = 0

    for row in rows:
        vin = _clean(row.get("vin")).upper()
        if not vin:
            excluded += 1
            continue
        recognized = [group for group in _group_names(row.get("groups")) if group in known]
        # Public-group membership is authoritative even if service activation
        # is pending/failed. For assets outside every recognized group, the
        # Fuel & Environment subscription is the admission rule requested for
        # the Unknown-sites report.
        if not recognized and not has_fuel_environment(row):
            excluded += 1
            continue
        if vin in seen_vins:
            raise ValueError(f"Duplicate eligible VIN in asset export: {vin}")
        seen_vins.add(vin)

        if len(recognized) > 1:
            raise ValueError(f"VIN {vin} belongs to multiple report groups: {recognized}")
        site = recognized[0] if recognized else UNKNOWN_SITE
        membership[site].append({
            "name": _clean(row.get("name")) or vin,
            "id": None,
            "vin": vin,
            "reg": None,
        })

    for vehicles in membership.values():
        vehicles.sort(key=lambda item: (item["name"].casefold(), item["vin"]))
    summary = {
        "mapped": len(seen_vins),
        "excluded": excluded,
        "counts": {site: len(vehicles) for site, vehicles in membership.items()},
    }
    return membership, summary


def read_asset_export(path: Path) -> list[dict[str, object]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if "Assets" not in workbook.sheetnames:
            raise ValueError("Asset export is missing the 'Assets' worksheet")
        sheet = workbook["Assets"]
        iterator = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(iterator)]
        positions = {}
        for key, label in REQUIRED_COLUMNS.items():
            if label not in headers:
                raise ValueError(f"Asset export is missing required column: {label}")
            positions[key] = headers.index(label)
        rows = []
        for values in iterator:
            rows.append({key: values[index] for key, index in positions.items()})
        return rows
    finally:
        workbook.close()


def find_latest_export(base: Path) -> Path | None:
    explicit = os.environ.get("VOLVO_ASSET_EXPORT_FILE")
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    candidates = list((Path.home() / "Downloads").glob(EXPORT_PATTERN))
    cached = base / "asset_group_master.xlsx"
    if cached.exists():
        candidates.append(cached)
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def sync_asset_groups(config_path: Path, shifts: dict, base: Path,
                      logger=None) -> tuple[dict, dict | None]:
    """Apply the newest valid asset export and atomically refresh site_config."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    export_path = find_latest_export(base)
    if export_path is None:
        if logger:
            logger.warning("No Volvo Connect asset export found; using cached site mapping")
        return config, None

    digest = _digest(export_path)
    if (config.get("_asset_sync") or {}).get("sha256") == digest:
        return config, config.get("_asset_sync")

    known_sites = [site for site in shifts.get("sites", {}) if site != UNKNOWN_SITE]
    rows = read_asset_export(export_path)
    membership, summary = membership_from_rows(rows, known_sites)
    if len(rows) < 100 or summary["mapped"] < 1:
        raise ValueError("Asset export failed completeness validation")
    missing_groups = [site for site in known_sites if not membership[site]]
    if missing_groups:
        raise ValueError(f"Asset export contains no eligible vehicles for: {missing_groups}")

    old_by_vin = {
        vehicle.get("vin"): vehicle
        for node in (config.get("sites") or {}).values()
        for vehicle in (node.get("vehicles") or [])
        if isinstance(vehicle, dict) and vehicle.get("vin")
    }
    sites = {}
    for site, vehicles in membership.items():
        for vehicle in vehicles:
            previous = old_by_vin.get(vehicle["vin"], {})
            if previous.get("id"):
                vehicle["id"] = previous["id"]
        sites[site] = {"count": len(vehicles), "vehicles": vehicles}

    sync_info = {
        "source": str(export_path),
        "sha256": digest,
        "synced_at": datetime.now().isoformat(),
        **summary,
    }
    config["sites"] = sites
    config["_generated"] = sync_info["synced_at"]
    config["_asset_sync"] = sync_info

    backup_dir = base / "output" / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"site_config_{datetime.now():%Y%m%d_%H%M%S}.json"
    shutil.copy2(config_path, backup)
    temp_path = config_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    temp_path.replace(config_path)

    cached = base / "asset_group_master.xlsx"
    if export_path.resolve() != cached.resolve():
        shutil.copy2(export_path, cached)
    if logger:
        logger.info("Asset groups synchronized from %s: %s", export_path, summary["counts"])
    return config, sync_info
