import json
from unittest.mock import patch

import pytest
import requests

from muninn import ocr


def test_sign_is_deterministic():
    body = b'{"contentType":"Text"}'
    a = ocr._sign("app-key", "hmac-key", body)
    b = ocr._sign("app-key", "hmac-key", body)
    assert a == b
    assert len(a) == 128  # SHA-512 hex digest length


def test_sign_changes_when_keys_change():
    body = b'{"contentType":"Text"}'
    base = ocr._sign("app", "hmac", body)
    assert ocr._sign("app2", "hmac", body) != base
    assert ocr._sign("app", "hmac2", body) != base


def test_sign_known_vector():
    # HMAC-SHA512 of empty body with concatenated key.
    # Manually-computed test vector to catch regressions in key-concat ordering.
    import hashlib
    import hmac

    expected = hmac.new(b"abXY", b"", hashlib.sha512).hexdigest()
    assert ocr._sign("ab", "XY", b"") == expected


def test_transcribe_returns_empty_string_for_no_strokes():
    cfg = {"ocr": {"application_key": "x", "hmac_key": "y"}}
    assert ocr.transcribe_strokes(cfg, []) == ""


def _cfg():
    return {"ocr": {"application_key": "app", "hmac_key": "hmac", "language": "en_US"}}


def _stroke_groups():
    return [
        {"penStyle": None, "strokes": [{"type": "stroke", "x": [1.0], "y": [2.0], "p": [0.5]}]}
    ]


class _FakeResp:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text


def test_transcribe_happy_path_decodes_utf8():
    # MyScript returns raw bytes; we must decode as UTF-8 even when the response
    # has no charset (default requests behavior would mangle to ISO-8859-1).
    body_utf8 = "café — bullet • arrow →".encode("utf-8")
    fake = _FakeResp(200, content=body_utf8)
    with patch.object(ocr.requests, "post", return_value=fake):
        result = ocr.transcribe_strokes(_cfg(), _stroke_groups())
    assert result == "café — bullet • arrow →"


def test_transcribe_returns_none_on_non_200():
    fake = _FakeResp(403, text="forbidden")
    with patch.object(ocr.requests, "post", return_value=fake):
        result = ocr.transcribe_strokes(_cfg(), _stroke_groups())
    assert result is None


def test_transcribe_returns_none_on_request_exception():
    with patch.object(
        ocr.requests, "post", side_effect=requests.ConnectionError("boom")
    ):
        result = ocr.transcribe_strokes(_cfg(), _stroke_groups())
    assert result is None


def test_transcribe_sets_auth_headers_and_signs_body():
    fake = _FakeResp(200, content=b"hello")
    captured = {}

    def _post(url, data, headers, timeout):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return fake

    with patch.object(ocr.requests, "post", side_effect=_post):
        ocr.transcribe_strokes(_cfg(), _stroke_groups())

    assert captured["url"] == ocr.ENDPOINT
    assert captured["headers"]["applicationKey"] == "app"
    # Verify the hmac header matches a recomputation against the actual body
    expected = ocr._sign("app", "hmac", captured["data"])
    assert captured["headers"]["hmac"] == expected
    # Verify the body parses and contains our stroke groups
    payload = json.loads(captured["data"])
    assert payload["contentType"] == "Text"
    assert payload["strokeGroups"] == _stroke_groups()
