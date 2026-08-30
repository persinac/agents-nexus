#!/usr/bin/env python3
"""Write a minimal on-theme probe doc: mkdoc.py <path> <title> [marker]."""
import sys

path, title = sys.argv[1], sys.argv[2]
marker = sys.argv[3] if len(sys.argv) > 3 else "alpha"
body = "<p>" + (f"{marker} content line. " * 200) + "</p>"
with open(path, "w") as fh:
    fh.write(
        "<!doctype html><html><head><title>" + title + "</title>"
        "<meta name='doc-theme' content='garner-doc/1'></head><body>"
        "<h1>" + title + "</h1><p class='dek'>A probe.</p>" + body +
        "</body></html>")
