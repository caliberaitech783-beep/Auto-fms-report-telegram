"""Optional WhatsApp Cloud API document sender.

If whatsapp_config.json is absent or disabled, reports remain queued locally.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

import requests


def load_config(base_dir: Path) -> dict | None:
    path = base_dir / "whatsapp_config.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return cfg if cfg.get("enabled") else None


def _recipients(cfg: dict, site: str) -> list[str]:
    by_site = cfg.get("recipients_by_site") or {}
    return by_site.get(site) or cfg.get("default_recipients") or []


def send_pdf(pdf: Path, site: str, caption: str, cfg: dict) -> list[dict]:
    version = cfg["graph_version"]
    phone_id = cfg["phone_number_id"]
    headers = {"Authorization": f"Bearer {cfg['access_token']}"}
    root = f"https://graph.facebook.com/{version}/{phone_id}"

    mime = mimetypes.guess_type(pdf.name)[0] or "application/pdf"
    with pdf.open("rb") as handle:
        upload = requests.post(
            f"{root}/media", headers=headers,
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (pdf.name, handle, mime)}, timeout=90,
        )
    upload.raise_for_status()
    media_id = upload.json()["id"]

    results = []
    for recipient in _recipients(cfg, site):
        template_name = cfg.get("template_name")
        if template_name:
            payload = {
                "messaging_product": "whatsapp", "to": recipient,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": cfg.get("template_language", "en")},
                    "components": [{
                        "type": "header",
                        "parameters": [{"type": "document",
                                        "document": {"id": media_id,
                                                     "filename": pdf.name}}],
                    }],
                },
            }
        else:
            # Valid only while a user-initiated customer-service window is open.
            payload = {
                "messaging_product": "whatsapp", "to": recipient,
                "type": "document",
                "document": {"id": media_id, "filename": pdf.name,
                             "caption": caption},
            }
        response = requests.post(f"{root}/messages", headers={**headers, "Content-Type": "application/json"},
                                 json=payload, timeout=60)
        response.raise_for_status()
        results.append({"recipient": recipient, "response": response.json()})
    return results
