"""MyScript iink Cloud REST client for handwriting OCR.

Submits vector strokes (extracted from `.rm` files via rm_format.parse_strokes)
to https://cloud.myscript.com/api/v4.0/iink/batch and returns recognized text.

Auth: applicationKey header + hmac header = HMAC-SHA512(application_key + hmac_key, body).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path

import requests

from muninn import rm_format

log = logging.getLogger(__name__)

ENDPOINT = "https://cloud.myscript.com/api/v4.0/iink/batch"
DEFAULT_DPI = 226  # reMarkable screen DPI; matches rm_format coordinate space
TIMEOUT_SECONDS = 30


def extract_strokes(rm_path: Path) -> list[dict]:
    """Parse a `.rm` page and return a single MyScript stroke group.

    Returns an empty list when the page has no strokes.
    """
    strokes = rm_format.parse_strokes(rm_path)
    if not strokes:
        return []

    return [
        {
            "penStyle": None,
            "strokes": [
                {
                    "type": "stroke",
                    "x": [round(p[0], 2) for p in s.points],
                    "y": [round(p[1], 2) for p in s.points],
                    "p": [round(p[2], 3) for p in s.points],
                }
                for s in strokes
            ],
        }
    ]


def transcribe_strokes(cfg: dict, stroke_groups: list[dict]) -> str | None:
    """Submit strokes to MyScript and return recognized text.

    Returns:
        - str: recognized text on success (may be empty for blank pages)
        - None: API error (non-200, timeout, parse failure)
    """
    if not stroke_groups:
        return ""  # no ink → no transcription, not an error

    ocr_cfg = cfg.get("ocr", {})
    app_key = ocr_cfg["application_key"]
    hmac_key = ocr_cfg["hmac_key"]
    lang = ocr_cfg.get("language", "en_US")

    payload = {
        "contentType": "Text",
        "xDPI": DEFAULT_DPI,
        "yDPI": DEFAULT_DPI,
        "configuration": {
            "lang": lang,
            "text": {"mimeTypes": ["text/plain"]},
        },
        "strokeGroups": stroke_groups,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = _sign(app_key, hmac_key, body)

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "applicationKey": app_key,
        "hmac": signature,
    }

    try:
        resp = requests.post(ENDPOINT, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.error("MyScript request failed: %s", exc)
        return None

    if resp.status_code != 200:
        log.error(
            "MyScript returned HTTP %d: %s", resp.status_code, resp.text[:300]
        )
        return None

    # MyScript returns UTF-8 but doesn't declare it in Content-Type; requests
    # defaults to ISO-8859-1 which mangles bullets, arrows, em-dashes, etc.
    return resp.content.decode("utf-8", errors="replace")


def check(cfg: dict) -> bool:
    """Send a minimal test request to verify auth + endpoint reachability."""
    test_strokes = [
        {
            "penStyle": None,
            "strokes": [
                {
                    "type": "stroke",
                    "x": [100, 110, 120, 130],
                    "y": [100, 100, 100, 100],
                    "p": [0.8, 0.9, 0.9, 0.8],
                }
            ],
        }
    ]
    result = transcribe_strokes(cfg, test_strokes)
    return result is not None


def _sign(application_key: str, hmac_key: str, body: bytes) -> str:
    """HMAC-SHA512 of body using (application_key + hmac_key) as the key. Hex digest."""
    key = (application_key + hmac_key).encode("utf-8")
    return hmac.new(key, body, hashlib.sha512).hexdigest()
