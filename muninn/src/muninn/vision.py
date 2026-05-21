"""Claude vision client for handwriting OCR and drawing descriptions.

Both entry points (`transcribe_handwriting`, `describe_page`) share the same
Anthropic Messages API call shape and only differ in the prompt — handwriting
OCR returns transcribed text; drawing description returns a short natural-
language summary of any diagrams or sketches on the page.
"""
from __future__ import annotations

import base64
import logging
import os

import anthropic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_OCR_PROMPT = (
    "Transcribe the handwritten text from this reMarkable notebook page exactly "
    "as written. Return only the transcription, no preamble or commentary. "
    "Preserve line breaks and indentation. Use → for right arrows, ↓ for down "
    "arrows, ⇒ for double arrows, ↳ for indented bullets, ✓ for checkmarks. "
    "If a word is illegible, write [?]. If the page is blank, return an empty "
    "response."
)
DEFAULT_DRAWING_PROMPT = (
    "Look at this handwritten notebook page and describe any non-textual visual "
    "elements: diagrams, flowcharts, sketches, tables, illustrations. Focus on "
    "structure and meaning — what the diagram is conveying, not stroke-level "
    "details. Be concise (1-3 sentences per distinct visual element). If the "
    "page contains only handwritten text with no diagrams, return an empty "
    "response — do not summarize the text content."
)


def transcribe_handwriting(
    cfg: dict, png_bytes: bytes, *, model: str | None = None
) -> str | None:
    """Transcribe handwriting from a rendered page PNG.

    `model` overrides `[vision].model` for a single call. Returns None on API
    errors; empty string for a blank page.
    """
    prompt = cfg.get("vision", {}).get("ocr_prompt", DEFAULT_OCR_PROMPT)
    return _call_vision(cfg, png_bytes, prompt, model)


def describe_page(
    cfg: dict, png_bytes: bytes, *, model: str | None = None
) -> str | None:
    """Describe non-textual visual elements on a rendered page PNG.

    Returns None on API errors, empty string for text-only pages.
    """
    prompt = cfg.get("vision", {}).get("drawing_prompt", DEFAULT_DRAWING_PROMPT)
    return _call_vision(cfg, png_bytes, prompt, model)


def _call_vision(
    cfg: dict, png_bytes: bytes, prompt: str, model: str | None
) -> str | None:
    """Send (image, prompt) to Claude; return text or None on error."""
    vcfg = cfg.get("vision", {})
    api_key = vcfg.get("api_key")
    # Treat the example placeholder as unset so ANTHROPIC_API_KEY env var wins.
    if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("vision.api_key not configured and ANTHROPIC_API_KEY not set")
        return None
    resolved_model = model or vcfg.get("model", DEFAULT_MODEL)

    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(png_bytes).decode("ascii")

    try:
        resp = client.messages.create(
            model=resolved_model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except anthropic.APIStatusError as exc:
        log.error("Claude vision request failed: %s", exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.error("Claude vision connection failed: %s", exc)
        return None

    parts = [b.text for b in resp.content if b.type == "text"]
    return "".join(parts).strip()
