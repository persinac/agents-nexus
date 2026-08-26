"""Assert the misconfig guard: access.enabled with a gap must NOT open the port.

Exercises access_misconfig() plus the enabled-and-complete path, without
touching the real config.json or reaching Cloudflare.
"""
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

print(f"\n{'FAILED' if fails else 'all guard checks passed'}")
sys.exit(1 if fails else 0)
