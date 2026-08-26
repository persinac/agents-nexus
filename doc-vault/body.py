"""Throwaway: print a vault doc's body markup with the <style> block removed."""
import re
import sqlite3
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent
conn = sqlite3.connect(VAULT / "index.db")
conn.row_factory = sqlite3.Row

doc_id = int(sys.argv[1])
r = conn.execute("SELECT title, vault_name FROM docs WHERE id=?", (doc_id,)).fetchone()
html = (VAULT / "docs" / r["vault_name"]).read_text(errors="replace")

html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.S)
html = re.sub(r"<link\b[^>]*>", "", html)
html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
html = re.sub(r"\n{3,}", "\n\n", html)
# collapse runs of spaces used for indentation, keep structure readable
html = re.sub(r"^[ \t]+", "", html, flags=re.M)
print(html.strip())
