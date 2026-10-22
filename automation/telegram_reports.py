"""Generate both fleets' due reports and deliver PDFs with persistent receipts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
LOG = logging.getLogger("telegram-reports")
PROJECTS = {
    "scania": ("Scania API Integration", "run_scheduled_reports.py"),
    "volvo": ("Volvo API Integration/api_v2_recovery", "api_report_runner.py"),
}


class TelegramError(RuntimeError):
    pass


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    cfg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token", "")
    if not cfg["bot_token"] or "YOUR_" in cfg["bot_token"]:
        raise ValueError("Set TELEGRAM_BOT_TOKEN or bot_token in the local configuration")
    for brand in PROJECTS:
        recipients = cfg.get("chats", {}).get(brand)
        if not isinstance(recipients, list) or not recipients:
            raise ValueError(f"Configure at least one chats.{brand} destination")
        if any(not str(chat).strip() or "YOUR_" in str(chat) for chat in recipients):
            raise ValueError(f"Replace the chats.{brand} placeholder")
    if cfg.get("start_date"):
        datetime.strptime(cfg["start_date"], "%Y-%m-%d")
    return cfg


def api(token: str, method: str, fields: dict, pdf: Path | None = None) -> dict:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    if pdf:
        if pdf.stat().st_size > 50_000_000:
            raise TelegramError("PDF exceeds the Telegram upload limit")
        filename = pdf.name.replace('"', '_').replace('\r', '').replace('\n', '')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
        parts.extend([pdf.read_bytes(), b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts) if pdf else urllib.parse.urlencode(fields).encode()
    content_type = (f"multipart/form-data; boundary={boundary}" if pdf
                    else "application/x-www-form-urlencoded")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=body,
        headers={"Content-Type": content_type},
    )
    # Never propagate urllib exceptions: their URLs contain the bot token.
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"Telegram HTTP {exc.code}; delivery remains pending") from None
    except (OSError, ValueError):
        raise TelegramError("Telegram connection/response failure; delivery remains pending") from None
    if not payload.get("ok"):
        raise TelegramError(f"Telegram rejected request (code {payload.get('error_code', 'unknown')})")
    return payload["result"]


@contextmanager
def exclusive_run(path: Path):
    """OS releases the lock even if a scheduled process crashes."""
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def deliver(cfg: dict, db: sqlite3.Connection, root: Path = ROOT, checkpoint=None) -> int:
    db.execute("CREATE TABLE IF NOT EXISTS sent (brand TEXT, file TEXT, digest TEXT, chat TEXT, message_id INTEGER, PRIMARY KEY (brand,file,digest,chat))")
    failures = 0
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR IGNORE INTO settings VALUES ('start_date', ?)",
               (cfg.get("start_date") or datetime.now(IST).date().isoformat(),))
    db.commit()
    start = cfg.get("start_date") or db.execute("SELECT value FROM settings WHERE key='start_date'").fetchone()[0]
    start_day = datetime.strptime(start, "%Y-%m-%d").date()
    for brand, (folder, _) in PROJECTS.items():
        project = Path(cfg.get("project_dirs", {}).get(brand, root / folder))
        outbox = project / "output" / "pdf"
        for pdf in sorted(outbox.glob("*.pdf")):
            age = datetime.now(IST).timestamp() - pdf.stat().st_mtime
            if age < cfg.get("settle_seconds", 0):
                continue
            if datetime.fromtimestamp(pdf.stat().st_mtime, IST).date() < start_day:
                continue
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            for chat in dict.fromkeys(str(c) for c in cfg["chats"][brand]):
                key = (brand, pdf.name, digest, chat)
                if db.execute("SELECT 1 FROM sent WHERE brand=? AND file=? AND digest=? AND chat=?", key).fetchone():
                    continue
                try:
                    result = api(cfg["bot_token"], "sendDocument", {
                        "chat_id": chat, "caption": f"{brand.title()} report | {pdf.stem}"[:1024],
                    }, pdf)
                    db.execute("INSERT INTO sent VALUES (?,?,?,?,?)", (*key, result["message_id"]))
                    db.commit()
                    if checkpoint:
                        checkpoint()
                    LOG.info("Delivered %s: %s", brand, pdf.name)
                except TelegramError as exc:
                    failures += 1
                    LOG.error("%s: %s: %s", brand, pdf.name, exc)
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "telegram_config.json")
    parser.add_argument("--check", action="store_true", help="Verify bot and chats without sending reports")
    parser.add_argument("--delivery-only", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.check:
        api(cfg["bot_token"], "getMe", {})
        for chat in {str(c) for chats in cfg["chats"].values() for c in chats}:
            api(cfg["bot_token"], "getChat", {"chat_id": chat})
        LOG.info("Bot and destination chats verified; no reports sent")
        return 0
    state = ROOT / "automation" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with exclusive_run(state / "run.lock"):
        failures = 0
        if not args.delivery_only:
            for brand, (folder, script) in PROJECTS.items():
                try:
                    project = Path(cfg.get("project_dirs", {}).get(brand, ROOT / folder))
                    result = subprocess.run([sys.executable, script], cwd=project, timeout=1800)
                    failures += int(result.returncode != 0)
                except (OSError, subprocess.TimeoutExpired):
                    LOG.error("%s generation failed; will retry on the next run", brand)
                    failures += 1
        with sqlite3.connect(state / "delivery.sqlite3") as db:
            failures += deliver(cfg, db)
    return int(bool(failures))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(main())
    except (ValueError, OSError, TelegramError) as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)
