"""Scania Data Access (rFMS v4) client and shift metric calculator."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

import requests

IST = timezone(timedelta(hours=5, minutes=30))
AUTH_BASE = "https://dataaccess.scania.com/auth"
API_BASE = "https://dataaccess.scania.com/rfms4"
ACCEPT = "application/json; rfms=vehiclestatuses.v4.0"
BASE = Path(__file__).resolve().parent


def parse_when(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=IST) if result.tzinfo is None else result.astimezone(IST)


def utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def challenge_response(secret: str, challenge: str) -> str:
    digest = hmac.new(_b64url_decode(secret), _b64url_decode(challenge), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def load_config(path: str | Path | None = None) -> dict:
    config_path = Path(path or os.getenv("SCANIA_CONFIG_FILE", BASE / "scania_config.json"))
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing {config_path}. Copy scania_config.example.json to scania_config.json "
            "and add the Scania secret key."
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["client_id"] = os.getenv("SCANIA_CLIENT_ID", config.get("client_id", ""))
    config["client_secret"] = os.getenv("SCANIA_CLIENT_SECRET", config.get("client_secret", ""))
    if not config["client_id"] or not config["client_secret"]:
        raise ValueError("Both Scania client_id and client_secret are required.")
    return config


@dataclass
class ScaniaClient:
    client_id: str
    client_secret: str
    auth_base: str = AUTH_BASE
    api_base: str = API_BASE
    timeout: int = 60
    max_retries: int = 4

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self._token = ""
        self._token_expiry = 0.0

    def authenticate(self) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    f"{self.auth_base}/clientid2challenge",
                    data={"clientId": self.client_id}, timeout=self.timeout,
                )
                response.raise_for_status()
                answer = challenge_response(self.client_secret, response.json()["challenge"])
                response = self.session.post(
                    f"{self.auth_base}/response2token",
                    data={"clientId": self.client_id, "Response": answer}, timeout=self.timeout,
                )
                response.raise_for_status()
                self._token = response.json()["token"]
                self._token_expiry = time.time() + 55 * 60
                return self._token
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status and status not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Scania authentication failed: {exc}") from exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 15))
        raise RuntimeError(f"Scania authentication failed after retries: {last_error}")

    def _get(self, path: str, params: dict | None = None, accept: str = "application/json") -> dict:
        if not self._token or time.time() >= self._token_expiry:
            self.authenticate()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{self.api_base}{path}", params=params, headers=headers, timeout=self.timeout
                )
                if response.status_code == 401 and attempt == 0:
                    headers["Authorization"] = f"Bearer {self.authenticate()}"
                    continue
                if response.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt, 15))
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status and status not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Scania API request failed: {exc}") from exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 15))
        raise RuntimeError(f"Scania API request failed: {last_error}")

    def vehicles(self) -> list[dict]:
        rows: list[dict] = []
        params: dict[str, str] = {}
        for _ in range(5000):
            payload = self._get(
                "/vehicles", params=params, accept="application/json; rfms=vehicles.v4.0"
            )
            page = payload.get("vehicleResponse", {}).get("vehicles", payload.get("vehicles", []))
            rows.extend(page)
            if not payload.get("moreDataAvailable") or not page:
                return rows
            params["lastVin"] = page[-1]["vin"]
        raise RuntimeError("Scania vehicle-list pagination exceeded 5000 pages.")

    def vehicle_statuses(self, start: datetime, stop: datetime) -> list[dict]:
        params: dict[str, str] = {
            "starttime": utc_z(start),
            "stoptime": utc_z(stop),
            "datetype": "created",
        }
        rows: list[dict] = []
        seen_rows: set[str] = set()
        seen_links: set[str] = set()
        for _ in range(5000):
            payload = self._get("/vehiclestatuses", params=params, accept=ACCEPT)
            envelope = payload.get("vehicleStatusResponse", payload)
            page = envelope.get("vehicleStatuses", envelope.get("vehicleStatus", []))
            for row in page:
                fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
                if fingerprint not in seen_rows:
                    rows.append(row)
                    seen_rows.add(fingerprint)
            if not payload.get("moreDataAvailable") or not page:
                break
            link = payload.get("moreDataAvailableLink", "")
            if not link or link in seen_links:
                raise RuntimeError("Scania status pagination did not advance.")
            seen_links.add(link)
            parsed = urlsplit(link)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        else:
            raise RuntimeError("Scania pagination exceeded 5000 pages.")
        return rows


def _time(row: dict) -> datetime:
    value = datetime.fromisoformat(row["createdDateTime"].replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def group_by_vin(rows: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("vin", ""), []).append(row)
    for values in grouped.values():
        values.sort(key=_time)
    return grouped


def _flat_metrics(row: dict) -> dict[str, float]:
    acc = row.get("accumulatedData") or {}
    metrics: dict[str, float] = {}
    scalars = {
        "distance_m": row.get("hrTotalVehicleDistance"),
        "fuel_ml": row.get("engineTotalFuelUsed"),
        "engine_h": row.get("totalEngineHours"),
        "moving_s": acc.get("durationWheelbasedSpeedOverZero", acc.get("durationWheelbaseSpeedOverZero")),
        "idle_s": acc.get("durationWheelbasedSpeedZero", acc.get("durationWheelbaseSpeedZero")),
    }
    metrics.update({k: float(v) for k, v in scalars.items() if isinstance(v, (int, float))})
    for field, prefix in (
        ("drivingWithoutTorqueClass", "coast"),
        ("performanceModeClass", "mode"),
        ("scaniaPerformanceModeClass", "mode"),
    ):
        for item in acc.get(field, []) or []:
            label = str(item.get("label", "")).upper()
            if prefix == "coast" or "POWER" in label:
                for unit in ("meters", "seconds"):
                    value = item.get(unit)
                    if isinstance(value, (int, float)):
                        metrics[f"{prefix}_{unit}"] = metrics.get(f"{prefix}_{unit}", 0.0) + float(value)
    return metrics


def _interpolated(rows: list[dict], boundary: datetime, key: str) -> float | None:
    points = [(_time(r), _flat_metrics(r)[key]) for r in rows if key in _flat_metrics(r)]
    if not points:
        return None
    before = [p for p in points if p[0] <= boundary]
    after = [p for p in points if p[0] >= boundary]
    if before and after:
        left, right = before[-1], after[0]
        if left[0] == right[0]:
            return left[1]
        fraction = (boundary - left[0]).total_seconds() / (right[0] - left[0]).total_seconds()
        return left[1] + fraction * (right[1] - left[1])
    if before:
        # Cumulative counters remain valid while a parked vehicle emits no
        # TIMER records. The report fetch includes a 12-hour baseline.
        nearest = before[-1]
        if (boundary - nearest[0]).total_seconds() <= 12 * 60 * 60:
            return nearest[1]
    elif after:
        nearest = after[0]
        if (nearest[0] - boundary).total_seconds() <= 90 * 60:
            return nearest[1]
    return None


def window_diffs(rows: list[dict], start: datetime, stop: datetime) -> dict[str, float] | None:
    if len(rows) < 2:
        return None
    start_utc, stop_utc = start.astimezone(timezone.utc), stop.astimezone(timezone.utc)
    result: dict[str, float] = {}
    for key in ("distance_m", "fuel_ml", "engine_h", "moving_s", "idle_s",
                "coast_meters", "coast_seconds", "mode_meters", "mode_seconds"):
        a = _interpolated(rows, start_utc, key)
        b = _interpolated(rows, stop_utc, key)
        if a is not None and b is not None:
            result[key] = max(0.0, b - a)
    return result if {"distance_m", "fuel_ml", "engine_h"} <= result.keys() else None


def _hhmm(hours: float) -> str:
    minutes = max(0, round(hours * 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


def compute_row(vin: str, diff: dict[str, float], vehicle: dict) -> dict[str, Any]:
    engine_h = diff.get("engine_h", 0.0)
    distance_km = diff.get("distance_m", 0.0) / 1000
    fuel_l = diff.get("fuel_ml", 0.0) / 1000
    idle_h = diff.get("idle_s", 0.0) / 3600
    if not idle_h and engine_h:
        idle_h = max(0.0, engine_h - diff.get("moving_s", 0.0) / 3600)
    power_km = diff.get("mode_meters", 0.0) / 1000
    coast_m = diff.get("coast_meters", 0.0)
    return {
        "Registration number": (
            vehicle.get("name") or vehicle.get("registration")
            or vehicle.get("customerVehicleName") or vin
        ),
        "Engine running time": _hhmm(engine_h),
        "engine_minutes": round(engine_h * 60),
        "Distance driven": round(distance_km, 2),
        "Consumption km/l": round(distance_km / fuel_l, 2) if fuel_l else None,
        "Consumption litres": round(fuel_l, 2),
        "Consumption litres/h": round(fuel_l / engine_h, 2) if engine_h else None,
        "Idling %": round(idle_h / engine_h * 100, 2) if engine_h else None,
        "Idling time": _hhmm(idle_h),
        "Power km": round(power_km, 2),
        "Power %": round(power_km / distance_km * 100, 2) if distance_km else 0.0,
        "Coasting %": (
            round(coast_m / diff.get("distance_m", 0.0) * 100, 2)
            if diff.get("distance_m", 0.0) > 0 else 0.0
        ),
    }


def match_site_vehicles(api_vehicles: list[dict], site_config: dict, site: str) -> dict[str, dict]:
    wanted = site_config["sites"][site]
    vins = set(wanted.get("vins", []))
    vin_registration_map = wanted.get("vin_registration_map", {})
    manifest = wanted.get("subscription_manifest")
    if manifest:
        manifest_path = BASE / manifest
        with manifest_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                if row.get("Service package") == "DataAccess Performance":
                    vin = row.get("VIN", "").strip()
                    registration = row.get("Reg. no.", "").strip()
                    if vin:
                        vins.add(vin)
                        vin_registration_map[vin] = registration or vin
    registrations = {x.replace(" ", "").upper() for x in wanted.get("registrations", [])}
    result = {}
    for vehicle in api_vehicles:
        vin = vehicle.get("vin", "")
        registration = str(
            vehicle.get("registration")
            or vehicle.get("registrationNumber")
            or vehicle.get("licensePlate")
            or vehicle.get("customerVehicleName")
            or vehicle.get("name")
            or ""
        )
        if vin in vins or registration.replace(" ", "").upper() in registrations:
            display_name = vin_registration_map.get(vin) or registration or vin
            result[vin] = {**vehicle, "name": display_name}
    return result
