"""Synchronize one report site's VINs from a Volvo Connect Excel export."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

import volvo_api_v2 as volvo


def vehicle_key(value: object) -> str:
    """Match harmless display-name variations such as 'V-604' and 'V 604'."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def exported_vehicle_names(path: Path) -> list[str]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Report data"]
        names: list[str] = []
        for row in sheet.iter_rows(min_row=3, values_only=True):
            name = str(row[0] or "").strip()
            if not name or name.casefold() == "all:":
                continue
            names.append(name)
        return list(dict.fromkeys(names))
    finally:
        workbook.close()


def build_live_name_index(display_names: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    index: dict[str, list[tuple[str, str]]] = {}
    for vin, name in display_names.items():
        index.setdefault(vehicle_key(name), []).append((vin, name))
    return index


def sync_site(site: str, export_path: Path, config_path: Path) -> dict:
    names = exported_vehicle_names(export_path)
    if not names:
        raise ValueError(f"No vehicles found in {export_path}")

    auth = volvo.auth_header(*volvo.load_credentials())
    display_names = volvo.fetch_vehicle_display_names(auth)
    index = build_live_name_index(display_names)

    resolved: list[dict[str, object]] = []
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for exported_name in names:
        matches = index.get(vehicle_key(exported_name), [])
        if not matches:
            missing.append(exported_name)
        elif len(matches) > 1:
            ambiguous[exported_name] = [vin for vin, _ in matches]
        else:
            vin, live_name = matches[0]
            resolved.append({"name": live_name, "id": None, "vin": vin, "reg": None})

    if missing or ambiguous:
        raise ValueError(
            "Export could not be mapped uniquely to the live Volvo fleet: "
            f"missing={missing}, ambiguous={ambiguous}"
        )

    config = json.loads(config_path.read_text(encoding="utf-8"))
    sites = config.get("sites")
    if not isinstance(sites, dict) or site not in sites:
        raise KeyError(f"Unknown site {site!r}; available sites: {sorted((sites or {}).keys())}")

    previous_by_vin = {
        item.get("vin"): item
        for node in sites.values()
        for item in (node.get("vehicles") or [])
        if isinstance(item, dict) and item.get("vin")
    }
    target_vins = {item["vin"] for item in resolved}
    for item in resolved:
        old = previous_by_vin.get(item["vin"], {})
        if old.get("id"):
            item["id"] = old["id"]

    # A VIN can belong to only one report site. Moving it in Volvo Connect must
    # remove it from its previous local group as well as add it to the new one.
    removed_from: dict[str, int] = {}
    for other_site, node in sites.items():
        if other_site == site:
            continue
        vehicles = node.get("vehicles") or []
        kept = [v for v in vehicles if v.get("vin") not in target_vins]
        removed = len(vehicles) - len(kept)
        if removed:
            node["vehicles"] = kept
            node["count"] = len(kept)
            removed_from[other_site] = removed

    sites[site]["vehicles"] = resolved
    sites[site]["count"] = len(resolved)
    config["_generated"] = datetime.now().isoformat()

    backup = config_path.with_name(
        f"{config_path.stem}.before_{site.replace(' ', '_')}_sync_"
        f"{datetime.now():%Y%m%d_%H%M%S}{config_path.suffix}"
    )
    shutil.copy2(config_path, backup)
    config_path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    return {
        "site": site,
        "vehicles": len(resolved),
        "names": [item["name"] for item in resolved],
        "removed_from_other_sites": removed_from,
        "backup": str(backup),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    result = sync_site(args.site, args.export, args.config)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
