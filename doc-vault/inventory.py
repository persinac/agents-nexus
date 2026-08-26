"""Throwaway: inventory docs >= a given id, with origins and git-tracked status."""
import subprocess
import sqlite3
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent
conn = sqlite3.connect(VAULT / "index.db")
conn.row_factory = sqlite3.Row
start = int(sys.argv[1]) if len(sys.argv) > 1 else 41
HOME = str(Path.home())

rows = conn.execute(
    "SELECT id, title, collection, vault_name, byte_size FROM docs WHERE id >= ? ORDER BY id",
    (start,)).fetchall()

for r in rows:
    origins = [x["path"] for x in conn.execute(
        "SELECT path FROM origins WHERE doc_id=? ORDER BY path", (r["id"],))]
    tags = [x["tag"] for x in conn.execute(
        "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (r["id"],))]
    print(f"\n=== {r['id']}  {r['title']}")
    print(f"    collection {r['collection']}   {r['byte_size']//1024}K   vault={r['vault_name']}")
    print(f"    tags: {', '.join(tags)}")
    for o in origins:
        p = Path(o)
        exists = p.exists()
        tracked = "n/a"
        if exists:
            try:
                res = subprocess.run(
                    ["git", "-C", str(p.parent), "ls-files", "--error-unmatch", p.name],
                    capture_output=True, text=True, timeout=10)
                tracked = "TRACKED" if res.returncode == 0 else "untracked"
            except Exception as exc:
                tracked = f"err {exc}"
        print(f"    origin: {o.replace(HOME,'~')}")
        print(f"            exists={exists}  git={tracked}")
