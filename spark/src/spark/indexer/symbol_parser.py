"""Symbol parser — extracts top-level AST symbols from source files using tree-sitter.

Supports Python, TypeScript/JavaScript, Go, and Terraform (HCL).
Returns an empty list for unsupported extensions or parse failures (callers fall back
to sliding-window chunking in that case).
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Map file extension → (language_loader, extract_function)
_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",  # TS parser handles JS fine
    ".jsx": "typescript",
    ".go": "go",
    ".tf": "hcl",
}

# Lazily initialised parsers — one per language
_parsers: dict[str, object] = {}


def _get_parser(language: str):
    """Return a cached tree-sitter Parser for the given language name."""
    if language in _parsers:
        return _parsers[language]

    from tree_sitter import Language, Parser  # type: ignore

    if language == "python":
        import tree_sitter_python as ts_lang_mod  # type: ignore
        lang = Language(ts_lang_mod.language())
    elif language == "typescript":
        import tree_sitter_typescript as ts_lang_mod  # type: ignore
        lang = Language(ts_lang_mod.language_typescript())
    elif language == "go":
        import tree_sitter_go as ts_lang_mod  # type: ignore
        lang = Language(ts_lang_mod.language())
    elif language == "hcl":
        import tree_sitter_hcl as ts_lang_mod  # type: ignore
        lang = Language(ts_lang_mod.language())
    else:
        raise ValueError(f"Unsupported language: {language}")

    parser = Parser(lang)
    _parsers[language] = parser
    return parser


# ---------------------------------------------------------------------------
# Per-language extractors
# ---------------------------------------------------------------------------

def _child_text(node, type_: str) -> str:
    """Return decoded text of the first direct child with the given type."""
    for child in node.children:
        if child.type == type_:
            return child.text.decode("utf-8", errors="replace")
    return ""


def _extract_python(root, source: bytes) -> list[dict]:
    symbols = []
    for node in root.children:
        if node.type == "function_definition":
            name = _child_text(node, "identifier")
            if name:
                symbols.append({"name": name, "type": "function", "source_text": node.text.decode("utf-8", errors="replace")})
        elif node.type == "decorated_definition":
            # Walk into the inner definition for name/type, but keep the decorated text
            for inner in node.children:
                if inner.type in ("function_definition", "async_function_definition"):
                    name = _child_text(inner, "identifier")
                    sym_type = "function"
                    break
                elif inner.type == "class_definition":
                    name = _child_text(inner, "identifier")
                    sym_type = "class"
                    break
            else:
                continue
            if name:
                symbols.append({"name": name, "type": sym_type, "source_text": node.text.decode("utf-8", errors="replace")})
        elif node.type == "class_definition":
            name = _child_text(node, "identifier")
            if name:
                symbols.append({"name": name, "type": "class", "source_text": node.text.decode("utf-8", errors="replace")})
    return symbols


def _extract_typescript(root, source: bytes) -> list[dict]:
    symbols = []
    for node in root.children:
        if node.type == "function_declaration":
            name = _child_text(node, "identifier")
            if name:
                symbols.append({"name": name, "type": "function", "source_text": node.text.decode("utf-8", errors="replace")})
        elif node.type == "class_declaration":
            name = _child_text(node, "type_identifier")
            if name:
                symbols.append({"name": name, "type": "class", "source_text": node.text.decode("utf-8", errors="replace")})
            # Also extract methods from the class body
            for child in node.children:
                if child.type == "class_body":
                    for member in child.children:
                        if member.type == "method_definition":
                            mname = _child_text(member, "property_identifier")
                            if mname:
                                symbols.append({"name": mname, "type": "method", "source_text": member.text.decode("utf-8", errors="replace")})
        elif node.type == "interface_declaration":
            name = _child_text(node, "type_identifier")
            if name:
                symbols.append({"name": name, "type": "interface", "source_text": node.text.decode("utf-8", errors="replace")})
        elif node.type == "lexical_declaration":
            # Const-assigned arrow functions: const foo = (...) => ...
            for decl in node.children:
                if decl.type == "variable_declarator":
                    has_arrow = any(c.type == "arrow_function" for c in decl.children)
                    if has_arrow:
                        name = _child_text(decl, "identifier")
                        if name:
                            symbols.append({"name": name, "type": "function", "source_text": node.text.decode("utf-8", errors="replace")})
    return symbols


def _extract_go(root, source: bytes) -> list[dict]:
    symbols = []
    for node in root.children:
        if node.type == "function_declaration":
            name = _child_text(node, "identifier")
            if name:
                symbols.append({"name": name, "type": "function", "source_text": node.text.decode("utf-8", errors="replace")})
        elif node.type == "method_declaration":
            name = _child_text(node, "field_identifier")
            if name:
                symbols.append({"name": name, "type": "method", "source_text": node.text.decode("utf-8", errors="replace")})
        elif node.type == "type_declaration":
            # type MyStruct struct{} — grab name from type_spec child
            for child in node.children:
                if child.type == "type_spec":
                    name = _child_text(child, "type_identifier")
                    if name:
                        symbols.append({"name": name, "type": "type", "source_text": node.text.decode("utf-8", errors="replace")})
    return symbols


_HCL_BLOCK_TYPES = {"resource", "variable", "output", "data", "module"}


def _extract_hcl(root, source: bytes) -> list[dict]:
    symbols = []
    # config_file → body → block*
    body = None
    for child in root.children:
        if child.type == "body":
            body = child
            break
    if body is None:
        return symbols

    for node in body.children:
        if node.type != "block":
            continue
        # First child of block is identifier (the block type keyword)
        labels = []
        for child in node.children:
            if child.type == "identifier":
                labels.append(child.text.decode("utf-8", errors="replace"))
            elif child.type == "string_lit":
                # Extract the template_literal inside the string
                for inner in child.children:
                    if inner.type == "template_literal":
                        labels.append(inner.text.decode("utf-8", errors="replace"))
                        break
        if not labels or labels[0] not in _HCL_BLOCK_TYPES:
            continue
        block_type = labels[0]
        block_name = ".".join(labels[1:]) if len(labels) > 1 else labels[0]
        symbols.append({"name": block_name, "type": block_type, "source_text": node.text.decode("utf-8", errors="replace")})

    return symbols


_EXTRACTORS = {
    "python": _extract_python,
    "typescript": _extract_typescript,
    "go": _extract_go,
    "hcl": _extract_hcl,
}


def parse_symbols(file_path: Path) -> list[dict]:
    """Parse a source file and return a list of top-level symbols.

    Each symbol is a dict with keys: ``name``, ``type``, ``source_text``.

    Returns an empty list if the extension is unsupported, the file cannot be
    read, or parsing raises any exception. Callers should fall back to
    sliding-window chunking on an empty result.
    """
    language = _EXT_MAP.get(file_path.suffix.lower())
    if language is None:
        return []

    try:
        source = file_path.read_bytes()
    except OSError as exc:
        logger.warning("symbol_parser: cannot read %s: %s", file_path, exc)
        return []

    try:
        parser = _get_parser(language)
        tree = parser.parse(source)
        extractor = _EXTRACTORS[language]
        return extractor(tree.root_node, source)
    except Exception as exc:
        logger.warning("symbol_parser: failed to parse %s (%s): %s", file_path, language, exc)
        return []
