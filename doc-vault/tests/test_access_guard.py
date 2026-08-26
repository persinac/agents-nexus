"""Assert the misconfig guard: access.enabled with a gap must NOT open the port.

Exercises access_misconfig() plus the enabled-and-complete path, without
touching the real config.json or reaching Cloudflare.
"""
import json
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import docvault  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}: want {want}, got {got}")


check("empty access block -> disabled", docvault.access_config({})["enabled"], False)
check("empty access block -> port default", docvault.access_config({})["port"], 8311)

full = {"enabled": True, "team": "example-team", "aud": "x" * 64,
        "allow": ["someone@example.com"]}
check("complete config -> no gaps", docvault.access_misconfig(full), [])

for gap, expect in [
    ({"aud": ""}, ["aud"]),
    ({"team": ""}, ["team"]),
    ({"allow": []}, ["allow"]),
    ({"team": "", "aud": "", "allow": []}, ["team", "aud", "allow"]),
    ({"team": "   "}, ["team"]),
]:
    cfg = dict(full)
    cfg.update(gap)
    check(f"gap {sorted(gap)} -> refuses", docvault.access_misconfig(cfg), expect)

try:
    docvault.verify_access_jwt("a.b.c", docvault.access_config({"access": {"team": "t"}}))
    check("verify with no aud -> raises", "returned", "AccessDenied")
except docvault.AccessDenied as exc:
    check("verify with no aud -> raises", "aud" in str(exc), True)

ALLOW = {"someone@example.com", "@example.org"}
for email, want in [
    ("someone@example.com", True),          # exact
    ("other@example.com", False),           # exact list is not a domain rule
    ("anyone@example.org", True),           # domain rule
    ("ANYONE@EXAMPLE.ORG".lower(), True),
    ("anyone@sub.example.org", False),      # subdomain must not inherit
    ("anyone@example.org.evil.com", False), # suffix must not match
    ("anyone@notexample.org", False),
    ("example.org", False),                 # bare domain, no local part
    ("@example.org", False),                # empty local part
    ("", False),
    ("weird-no-at", False),
    ("a@b@example.org", True),              # last @ wins, domain is example.org
]:
    check(f"email_allowed({email!r})", docvault.email_allowed(email, ALLOW), want)

check("bare '@' entry matches nothing", docvault.email_allowed("a@", {"@"}), False)
check("empty allow matches nothing", docvault.email_allowed("a@example.org", set()), False)

# A populated default would ship a live allowlist to a public repo in one line.
check("DEFAULT_ACCESS ships no allowlist", docvault.DEFAULT_ACCESS["allow"], [])
check("DEFAULT_ACCESS ships no team", docvault.DEFAULT_ACCESS["team"], "")
check("DEFAULT_ACCESS ships no aud", docvault.DEFAULT_ACCESS["aud"], "")
check("DEFAULT_ACCESS ships no ca_bundle", docvault.DEFAULT_ACCESS["ca_bundle"], "")
check("DEFAULT_ACCESS is off", docvault.DEFAULT_ACCESS["enabled"], False)

example = json.loads((pathlib.Path(docvault.CODE_DIR) / "config.example.json").read_text())
ex_access = example.get("access", {})
check("config.example.json ships no allowlist", ex_access.get("allow"), [])
check("config.example.json ships no team", ex_access.get("team"), "")
check("config.example.json ships no aud", ex_access.get("aud"), "")
check("config.example.json is off", ex_access.get("enabled"), False)

tracked_live = pathlib.Path(docvault.CODE_DIR) / "config.json"
check("no live config.json in the code dir", tracked_live.exists(), False)

print(f"\n{'FAILED' if fails else 'all guard checks passed'}")
sys.exit(1 if fails else 0)
