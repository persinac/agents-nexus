"""Browser-driven proof that a highlight note's swipe lands on its quote."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
XTOL = 3.0

CASES = [
    ("markup + source break", "re-deposit of the same bytes is a no-op", 1),
    ("code + link boundary", "serve --port 8310 and open the index", 1),
    ("em dash entity", "in config.json — which the watcher rereads", 1),
    ("long, wraps lines", "A quote long enough to wrap across more than one line box, "
                          "which the old sixty-character cap would have truncated "
                          "halfway through the marking.", 2),
]
ABSENT = "no such phrase exists anywhere in this document"
ROUNDTRIP = "watcher rereads every two minutes"

PROBE = """<!doctype html><html><head><meta charset="utf-8">
<title>Anchor Probe Doc</title></head><body>
<h1>Anchor Probe Doc</h1>
<p class="dek">Wrapped, indented source with inline markup.</p>
<p>
  The vault indexes docs by content hash, so a <strong>re-deposit of the
  same bytes</strong> is a no-op and the note stays where it was put.
</p>
<p>
  Run <code>docvault.py serve --port 8310</code> and open
  <a href="#index">the index</a>. The crawl roots live
  in <code>config.json</code> &mdash; which the watcher rereads
  every two minutes.
</p>
<p style="max-width:24em">
  A quote long enough to wrap across more than one line box, which the
  old sixty-character cap would have truncated halfway through the
  marking.
</p>
FILLER
</body></html>"""

GEO = """
  const geo = (q) => [...document.querySelectorAll(q)].map(el => {
    const b = el.getBBox();
    return {left: b.x, right: b.x + b.width, top: b.y, bottom: b.y + b.height};
  });"""

READ_MARKS = "(cid) => {" + GEO + """
  return {marks: geo(`#marks [data-c="${cid}"]`),
          lines: geo(`#lines path[data-c="${cid}"]`)};
}"""

NATIVE_FIND = """(quote) => {
  const f = document.getElementById('docframe');
  f.contentDocument.getSelection().removeAllRanges();
  return f.contentWindow.find(quote, false, false, true);
}"""

# #board is inset:0 over a full-size iframe, so an iframe client rect and an
# SVG bbox on the board are the same coordinate space. Both are read in one
# call so they also share a scroll position.
READ_BOTH = "(cid) => {" + GEO + """
  const sel = document.getElementById('docframe').contentDocument.getSelection();
  if (!sel.rangeCount) return null;
  const rects = [...sel.getRangeAt(0).getClientRects()]
    .filter(b => b.height > 0 && b.width > 0)
    .map(b => ({left: b.left, right: b.right, top: b.top, bottom: b.bottom}));
  return {text: sel.toString(), rects, marks: geo(`#marks [data-c="${cid}"]`),
          lines: geo(`#lines path[data-c="${cid}"]`)};
}"""

SELECT_ACROSS = """(quote) => {
  const d = document.getElementById('docframe').contentDocument;
  const w = document.getElementById('docframe').contentWindow;
  d.getSelection().removeAllRanges();
  if (!w.find(quote, false, false, true)) return false;
  d.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
  return true;
}"""

CLEAR_SEL = ("() => document.getElementById('docframe')"
             ".contentDocument.getSelection().removeAllRanges()")


class Fail(Exception):
    pass


def log(label: str, value: str) -> None:
    print(f"  {label:<32} {value}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def vault(cmd: list[str], home: Path) -> str:
    r = subprocess.run([sys.executable, "docvault.py", *cmd], cwd=CODE_DIR,
                       env=dict(os.environ, DOCVAULT_HOME=str(home)),
                       capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"docvault.py {' '.join(cmd)}: {r.stderr.strip()[:300]}")
    return r.stdout


def post(base: str, path: str, fields: dict[str, str]) -> dict | None:
    req = urllib.request.Request(
        base + path, data=urllib.parse.urlencode(fields).encode(),
        headers={"X-Requested-With": "fetch",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        return None


def chromium_candidates() -> list[str]:
    if os.environ.get("DOCVAULT_CHROMIUM"):
        return [os.environ["DOCVAULT_CHROMIUM"]]
    found: list[str] = []
    for root in (Path.home() / "Library/Caches/ms-playwright",
                 Path.home() / ".cache/ms-playwright"):
        for pat in ("chromium_headless_shell-*/*/chrome-headless-shell",
                    "chromium-*/*/Chromium.app/Contents/MacOS/Chromium",
                    "chromium-*/chrome-linux/chrome"):
            found += [str(p) for p in sorted(root.glob(pat), reverse=True)]
    return found


def launch(pw, headed: bool):
    """Playwright's own browser first, then any Chromium already on the box."""
    try:
        return pw.chromium.launch(headless=not headed)
    except Exception as first:
        for path in chromium_candidates():
            try:
                return pw.chromium.launch(headless=not headed, executable_path=path)
            except Exception:
                continue
        raise Fail(f"no usable Chromium ({type(first).__name__}); "
                   f"run: uv run --with playwright playwright install chromium") from first


def union(boxes: list[dict]) -> dict:
    return {"left": min(b["left"] for b in boxes), "right": max(b["right"] for b in boxes),
            "top": min(b["top"] for b in boxes), "bottom": max(b["bottom"] for b in boxes)}


def serve(home: Path, port: int) -> subprocess.Popen:
    srv = subprocess.Popen(
        [sys.executable, "docvault.py", "serve", "--port", str(port)], cwd=CODE_DIR,
        env=dict(os.environ, DOCVAULT_HOME=str(home)),
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/doc/1", timeout=1).read()
            return srv
        except Exception:
            time.sleep(0.25)
    srv.terminate()
    raise Fail("server never came up")


def check_shapes(page, seeded) -> int:
    fails = 0
    print("=== every quote shape anchors, measured against native find ===")
    for (label, quote, cid), (_, _, want_rects) in zip(seeded, CASES):
        if not page.evaluate(NATIVE_FIND, quote):
            log(label, "ORACLE-MISS: quote absent from the rendered doc")
            fails += 1
            continue
        page.wait_for_timeout(250)
        got = page.evaluate(READ_BOTH, cid)
        if not got or not got["rects"]:
            log(label, "no selection rects")
            fails += 1
            continue
        if not got["marks"]:
            log(label, f"NOT ANCHORED: 0 swipes for note {cid}")
            fails += 1
            continue
        want, mark = union(got["rects"]), union(got["marks"])
        dl, dr = abs(mark["left"] - want["left"]), abs(mark["right"] - want["right"])
        on_line = want["top"] - 2 <= mark["top"] and mark["bottom"] <= want["bottom"] + 2
        # One swipe per line box, or the quote is only partly marked.
        covered = len(got["marks"]) == len(got["rects"])
        ok = (max(dl, dr) <= XTOL and on_line and covered
              and len(got["rects"]) >= want_rects)
        log(label, f"{'ok' if ok else 'FAIL'} — {len(got['marks'])} swipe(s) over "
                   f"{len(got['rects'])} line box(es), dx {dl:.1f}/{dr:.1f}px, "
                   f"on-line {on_line}")
        fails += 0 if ok else 1
        if not got["lines"]:
            log(f"{label} connector", "MISSING")
            fails += 1
    return fails


def check_silent(page, absent_id: int, general_id: int) -> int:
    fails = 0
    print("=== nothing is drawn for a missing quote or a whole-doc note ===")
    page.evaluate(CLEAR_SEL)
    for label, cid in (("absent quote", absent_id), ("general note", general_id)):
        got = page.evaluate(READ_MARKS, cid)
        ok = not got["marks"] and not got["lines"]
        log(label, "no swipe, no line" if ok else "DREW SOMETHING")
        fails += 0 if ok else 1
    return fails


def check_roundtrip(page, before: int) -> int:
    fails = 0
    print("=== select-to-note round trip anchors the new note ===")
    if not page.evaluate(SELECT_ACROSS, ROUNDTRIP):
        log("select across markup", "ORACLE-MISS")
        return 1
    page.wait_for_selector("#seltip", state="visible", timeout=5000)
    page.click("#seltip")
    page.fill("#compose textarea", "created by selecting text")
    page.click("#csave")
    page.wait_for_function(
        f"document.querySelectorAll('#board .note').length > {before}", timeout=5000)
    page.wait_for_timeout(300)
    new_id = page.evaluate("() => JSON.parse(document.getElementById('cdata')"
                           ".textContent).length + 1")
    stored = page.evaluate("() => [...document.querySelectorAll('#board .note .nq')]"
                           ".map(e => e.textContent).pop()")
    got = page.evaluate(READ_MARKS, new_id)
    for label, ok in (("new note anchored", bool(got["marks"]) and bool(got["lines"])),
                      ("quote stored", ROUNDTRIP in (stored or ""))):
        log(label, "ok" if ok else "FAIL")
        fails += 0 if ok else 1
    page.reload()
    page.wait_for_selector("#board .note")
    page.wait_for_timeout(400)
    survived = bool(page.evaluate(READ_MARKS, new_id)["marks"])
    log("survives a reload", "ok" if survived else "LOST ITS ANCHOR")
    return fails + (0 if survived else 1)


def check_new_version(page, probe: Path, home: Path, body: str) -> int:
    print("=== a new version drops every anchor but keeps the notes ===")
    probe.write_text(body.replace("re-deposit of the\n  same bytes", "a redeposit")
                         .replace("serve --port 8310", "serve --port 9999"))
    vault(["put", str(probe), "--collection", "notes"], home)
    page.reload()
    page.wait_for_selector("#board .note")
    page.wait_for_timeout(400)
    swipes = page.evaluate("() => document.querySelectorAll('#marks [data-c]').length")
    notes = page.evaluate("() => document.querySelectorAll('#board .note').length")
    ok = swipes == 0 and notes >= len(CASES) + 2
    log("v2 anchors / notes", f"{'ok' if ok else 'FAIL'} — {swipes} swipe(s), "
                              f"{notes} note(s) still rendered")
    return 0 if ok else 1


def main() -> int:
    from playwright.sync_api import sync_playwright

    headed = os.environ.get("DOCVAULT_TEST_HEADED") == "1"
    home = Path(tempfile.mkdtemp(prefix="dv-anchor-"))
    work = Path(tempfile.mkdtemp(prefix="dv-anchor-src-"))
    srv = None
    fails = 0
    try:
        vault(["init"], home)
        filler = "".join(
            f"<p>Filler paragraph {i} keeps the probe long enough to scroll, so a quote "
            f"below the fold has to be scrolled to before it can be measured.</p>\n"
            for i in range(40))
        body = PROBE.replace("FILLER", filler)
        probe = work / "anchor-probe.html"
        probe.write_text(body)
        vault(["put", str(probe), "--collection", "notes"], home)

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        srv = serve(home, port)

        seeded = [(label, quote, post(base, "/doc/1/comment",
                                      {"kind": "highlight", "body": f"note for {label}",
                                       "quote": quote})["id"])
                  for label, quote, _ in CASES]
        absent = post(base, "/doc/1/comment",
                      {"kind": "highlight", "body": "quote not in doc", "quote": ABSENT})
        general = post(base, "/doc/1/comment",
                       {"kind": "general", "body": "whole-doc note"})

        with sync_playwright() as pw:
            browser = launch(pw, headed)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(base + "/doc/1")
            page.wait_for_selector("#board .note")
            page.wait_for_function(
                "document.querySelectorAll('#marks [data-c]').length > 0", timeout=10000)

            fails += check_shapes(page, seeded)
            fails += check_silent(page, absent["id"], general["id"])
            fails += check_roundtrip(page, len(CASES) + 2)
            fails += check_new_version(page, probe, home, body)
            browser.close()
    except Fail as e:
        if "no usable Chromium" in str(e):
            print(f"SKIP: {e}")
            return 0
        print(f"FAIL: {e}")
        return 1
    finally:
        if srv:
            srv.terminate()
            srv.wait(timeout=10)
        subprocess.run(["rm", "-rf", str(home), str(work)], check=False)

    print(f"\n{'all anchor checks passed' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        import playwright  # noqa: F401
    except ModuleNotFoundError:
        print("SKIP: playwright missing — run via `task docvault:test` or "
              "`uv run --with playwright python tests/test_anchor_browser.py`")
        sys.exit(0)
    sys.exit(main())
