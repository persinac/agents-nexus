"""Tests for entity_extraction.py — auto-extract entity refs from note content."""

import pytest

from agent_memory.entity_extraction import extract_entities


def _names(results):
    return [name for name, _ in results]


def _typed(results):
    return {name: etype for name, etype in results}


# ── File paths ────────────────────────────────────────────────────────────────


def test_file_path_with_directory():
    refs = extract_entities("Refactored src/auth/middleware.py to use JWT.")
    assert "src/auth/middleware.py" in _names(refs)
    assert _typed(refs)["src/auth/middleware.py"] == "file"


def test_file_path_bare():
    refs = extract_entities("See config.yaml for defaults.")
    assert "config.yaml" in _names(refs)


def test_file_path_go():
    refs = extract_entities("Edited pkg/billing/invoice.go")
    assert "pkg/billing/invoice.go" in _names(refs)


def test_file_path_multiple():
    refs = extract_entities("Changed auth.py and tests/test_auth.py.")
    names = _names(refs)
    assert "auth.py" in names
    assert "tests/test_auth.py" in names


def test_file_path_typescript():
    refs = extract_entities("Updated src/hooks/useExtensionMessages.ts")
    assert "src/hooks/useExtensionMessages.ts" in _names(refs)


def test_no_false_positive_bare_word():
    # "Go" is not a file
    refs = extract_entities("We decided to use Go for the backend.")
    assert not any(n.lower() == "go" for n, _ in refs)


# ── Wikilinks ─────────────────────────────────────────────────────────────────


def test_wikilink_simple():
    refs = extract_entities("See [[auth-module]] for details.")
    assert "auth-module" in _names(refs)
    assert _typed(refs)["auth-module"] == "wikilink"


def test_wikilink_with_spaces():
    refs = extract_entities("Related to [[storefront api]].")
    assert "storefront api" in _names(refs)


def test_wikilink_multiple():
    refs = extract_entities("Connects [[svc-chatbot]] and [[agent-memory]].")
    names = _names(refs)
    assert "svc-chatbot" in names
    assert "agent-memory" in names


# ── @mentions ─────────────────────────────────────────────────────────────────


def test_mention_simple():
    refs = extract_entities("Deployed @svc-chatbot to staging.")
    assert "svc-chatbot" in _names(refs)
    assert _typed(refs)["svc-chatbot"] == "mention"


def test_mention_not_email():
    # Email addresses should not produce a mention for the domain part
    refs = extract_entities("Contact alex@example.com for help.")
    # "example" could be extracted as a mention — that's acceptable since we
    # only care about the left side of @, not domains. But let's verify "example"
    # is what we'd get (not "example.com" — that has a dot).
    names = _names(refs)
    # "example" is fine to extract; "example.com" with a dot is NOT a valid mention
    for n in names:
        assert "." not in n or n.endswith(tuple(".py .ts .js .go .rs .sql .sh".split()))


def test_mention_ignored_when_code_fence():
    refs = extract_entities("Run `@decorator` syntax here.")
    # backtick-fenced @decorator should NOT be extracted (preceded by backtick)
    assert "decorator" not in _names(refs)


# ── Deduplication ─────────────────────────────────────────────────────────────


def test_deduplication():
    refs = extract_entities("See auth.py. Also auth.py.")
    assert _names(refs).count("auth.py") == 1


def test_wikilink_does_not_duplicate_file():
    # Same name via two patterns — first occurrence wins
    refs = extract_entities("File [[auth.py]] and auth.py mentioned.")
    names = _names(refs)
    assert names.count("auth.py") == 1


# ── Mixed content ─────────────────────────────────────────────────────────────


def test_mixed_content():
    content = (
        "Investigated [[auth-module]]. "
        "Key file: src/auth/middleware.py. "
        "Talk to @backend-team about this."
    )
    typed = _typed(extract_entities(content))
    assert typed["auth-module"] == "wikilink"
    assert typed["src/auth/middleware.py"] == "file"
    assert typed["backend-team"] == "mention"


def test_empty_content():
    assert extract_entities("") == []


def test_no_entities():
    assert extract_entities("No references here at all.") == []
