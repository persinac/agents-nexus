"""Compare visible text of an indexed vault doc against a rewritten file.

Reports content that vanished in the rewrite. Whitespace, tag structure and
class names are ignored — only the words a reader would see are compared.
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Data paths come from docvault: the code is in the repo, the vault is not.
from docvault import DB_PATH, DOCS_DIR, extract  # noqa: E402

old_id = int(sys.argv[1])
new_path = Path(sys.argv[2]).expanduser()

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
r = conn.execute("SELECT title, vault_name FROM docs WHERE id=?", (old_id,)).fetchone()

old = extract((DOCS_DIR / r["vault_name"]).read_bytes())["body"]
new = extract(new_path.read_bytes())["body"]


def norm(t: str) -> list[str]:
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    t = t.replace("—", " ").replace("–", " ").replace(" ", " ")
    return re.findall(r"[A-Za-z0-9_$%./#|-]+", t.lower())


ow, nw = norm(old), norm(new)
os_, ns_ = set(ow), set(nw)

print(f"old: {len(ow)} words / {len(os_)} distinct")
print(f"new: {len(nw)} words / {len(ns_)} distinct")

# Class names and CSS-ish leftovers are expected to disappear; flag anything else.
IGNORE = re.compile(r"^(chip|seam|standfirst|prose|status|page|verdict|k|v|live|"
                    r"scaffold|port|bug|hold|no|body|row)$")
dropped = sorted(w for w in os_ - ns_ if not IGNORE.match(w))
added = sorted(ns_ - os_)

print(f"\nDROPPED ({len(dropped)}):")
for w in dropped:
    print(f"  {w}")
print(f"\nADDED ({len(added)}):")
for w in added:
    print(f"  {w}")

# Numbers are the highest-risk loss: a figure silently gone is a wrong doc.
num = re.compile(r"^[\d$][\d.,%$/kmb-]*$", re.I)
lost_nums = sorted(w for w in os_ - ns_ if num.match(w))
print(f"\nNUMBERS LOST ({len(lost_nums)}): {lost_nums or 'none'}")
