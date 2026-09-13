# -*- coding: utf-8 -*-
"""Embedded ANSA API documentation index, replicated inside ansa-mcp-sum.

This is a self-contained replica of the ``ansa-api`` MCP server's search
engine so that ansa-mcp-sum can answer "what is the exact ANSA API for X?"
**without depending on the separate ``ansa-api`` MCP server being enabled**.

The index file (``ansa_api_index.json``) is **bundled inside this package**
at ``src/ansa_mcp_sum/ansa_api_index.json`` (10.4 MB / 5892 functions /
v25.1.4), so the project is self-contained and shareable without requiring
the ``ansa_tools`` package to be installed. It is a snapshot of the same file
the standalone ``ansa-api`` server ships. Resolution order is
``ANSA_API_INDEX_PATH`` env -> the bundled copy -> the ``ansa_tools`` package
resource (kept only as a backward-compat fallback).

Unlike the ansa-api server, this module is pure logic: no FastMCP object, no
side effects on import. It is imported by ``server.py`` and exposes
``search / lookup / list_modules / list_categories`` returning markdown text,
ready to be wrapped in a JSON tool response.
"""
from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
from typing import Any


def _tokenize(text: str) -> list[str]:
    """Split text into tokens: words, individual CJK chars, punctuation-separated segments."""
    tokens: list[str] = []
    current: list[str] = []
    for ch in text:
        # CJK Unified Ideographs and extensions
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            if current:
                tokens.append("".join(current))
                current = []
            tokens.append(ch)
        elif ch.isalnum() or ch == "_":
            current.append(ch)
        else:
            if current:
                tokens.append("".join(current))
                current = []
    if current:
        tokens.append("".join(current))
    return [t.lower() for t in tokens if t]


class AnsaApiSearcher:
    """Search ANSA API functions using a pre-built index with txt fallback."""

    def __init__(self, index_path: str, txt_docs_path: str = ""):
        self.txt_docs_path = txt_docs_path
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
        self.metadata = data.get("metadata", {})
        self.functions: list[dict] = data.get("functions", [])
        # Pre-compute lowercased keywords for each function
        for func in self.functions:
            func["_keywords_lower"] = [kw.lower() for kw in func.get("keywords", [])]

    def search(
        self,
        query: str,
        module: str | None = None,
        category: str | None = None,
        top_n: int = 5,
    ) -> list[dict]:
        """Three-layer search: keyword -> fuzzy -> txt fallback."""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # Apply filters
        candidates = self.functions
        if module:
            candidates = [f for f in candidates if f.get("module") == module]
        if category:
            candidates = [f for f in candidates if f.get("category") == category]

        # Layer 1: keyword match with module / name relevance boost
        query_lower = query.lower()
        scored: list[tuple[int, dict]] = []
        for func in candidates:
            kw_lower = func["_keywords_lower"]
            score = 0
            mod_last = func["module"].split(".")[-1]
            name_lower = func["name"].lower()
            for tok in query_tokens:
                if not tok:
                    continue
                if mod_last == tok or tok in mod_last or mod_last in tok:
                    score += 6
                if tok in name_lower or name_lower in tok:
                    score += 8
            for kw in kw_lower:
                if kw in query_lower:
                    score += 2 if kw in query_tokens else 1
            if score == 0:
                for tok in query_tokens:
                    if tok and tok in kw_lower:
                        score += 1
            if score > 0:
                scored.append((score, func))
        scored.sort(key=lambda x: -x[0])
        results = [func for _, func in scored]

        # Layer 2: fuzzy fallback on description + signature
        if len(results) < top_n:
            already = {id(f) for f in results}
            for func in candidates:
                if id(func) in already:
                    continue
                searchable = (func.get("description", "") + " " + func.get("signature", "")).lower()
                if any(tok in searchable for tok in query_tokens):
                    results.append(func)

        # Layer 3: txt file fallback
        if len(results) < top_n and self.txt_docs_path and os.path.isdir(self.txt_docs_path):
            already = {id(f) for f in results}
            found_names = {f["name"] for f in results}
            query_lower = query.lower()
            for fname in os.listdir(self.txt_docs_path):
                if not fname.endswith(".txt"):
                    continue
                fpath = os.path.join(self.txt_docs_path, fname)
                try:
                    with open(fpath, encoding="utf-8") as tf:
                        content = tf.read()
                except Exception:
                    continue
                if query_lower not in content.lower():
                    continue
                for func in candidates:
                    if id(func) in already or func["name"] in found_names:
                        continue
                    if func["name"].lower() in content.lower():
                        results.append(func)
                        found_names.add(func["name"])

        results = results[:top_n]
        clean: list[dict] = []
        for func in results:
            clean.append({k: v for k, v in func.items() if not k.startswith("_")})
        return clean

    def list_modules(self) -> list[dict]:
        modules = self.metadata.get("modules", [])
        if not modules:
            modules = sorted({f.get("module", "") for f in self.functions if f.get("module")})
        result = []
        for mod in modules:
            count = sum(1 for f in self.functions if f.get("module") == mod)
            result.append({"module": mod, "function_count": count})
        return result

    def list_categories(self) -> list[dict]:
        cats: dict[str, int] = {}
        for f in self.functions:
            cat = f.get("category", "")
            if cat:
                cats[cat] = cats.get(cat, 0) + 1
        return [{"category": k, "function_count": v} for k, v in sorted(cats.items())]


# Default paths: prefer the shared ansa_tools index, fall back to a bundled copy.
_BUNDLED_TXT_DOCS = str(Path(__file__).resolve().parent / "txt_docs")


def _resolve_index_path() -> Path:
    """Locate ansa_api_index.json.

    Order: ``ANSA_API_INDEX_PATH`` env -> a copy bundled under this package
    (self-contained, shipped with the project) -> the ``ansa_tools`` package
    resource (backward-compat fallback: the standalone ansa-api server's copy).
    """
    candidates: list[Path] = []
    env = os.environ.get("ANSA_API_INDEX_PATH")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parent / "ansa_api_index.json")
    try:
        candidates.append(Path(str(files("ansa_tools").joinpath("ansa_api_index.json"))))
    except Exception:
        pass
    for c in candidates:
        if c and c.exists():
            return c
    raise FileNotFoundError(
        "ansa_api_index.json not found. Set ANSA_API_INDEX_PATH, or place a "
        "copy next to api_doc.py, or install the ansa_tools package."
    )


_searcher: AnsaApiSearcher | None = None


def _get_searcher() -> AnsaApiSearcher:
    global _searcher
    if _searcher is None:
        _searcher = AnsaApiSearcher(str(_resolve_index_path()), _BUNDLED_TXT_DOCS)
    return _searcher


def _format_result(func: dict) -> str:
    """Format a single search result as markdown."""
    lines = [f"### `{func['signature']}`"]
    lines.append(f"**Module:** {func.get('module', 'N/A')}")
    lines.append(f"**Category:** {func.get('category', 'N/A')}")
    lines.append("")
    lines.append(func.get("description", ""))
    lines.append("")

    params = func.get("parameters", [])
    if params:
        lines.append("**Parameters:**")
        for p in params:
            lines.append(f"- `{p['name']}` ({p.get('type', 'any')}): {p.get('desc', '')}")
        lines.append("")

    ret = func.get("returns", "")
    if ret:
        lines.append(f"**Returns:** {ret}")
        lines.append("")

    examples = func.get("examples", "")
    if examples:
        lines.append("**Example:**")
        lines.append("```python")
        lines.append(examples)
        lines.append("```")

    return "\n".join(lines)


def search(query: str, module: str | None = None, category: str | None = None,
           top_n: int = 5) -> str:
    """Search the ANSA API documentation. Returns markdown text."""
    searcher = _get_searcher()
    results = searcher.search(query, module=module, category=category, top_n=top_n)
    if not results:
        return f"No results found for query: `{query}`"
    header = f"## ANSA API Search Results for `{query}`\n"
    parts = [header]
    for i, func in enumerate(results, 1):
        parts.append(f"---\n\n**{i}. {func['name']}**\n")
        parts.append(_format_result(func))
        parts.append("")
    return "\n".join(parts)


def lookup(function_name: str, module: str | None = None) -> str:
    """Get full documentation for a specific function by exact name. Returns markdown."""
    searcher = _get_searcher()
    candidates = searcher.functions
    mod_filter = module
    if mod_filter:
        candidates = [f for f in candidates if f.get("module") == mod_filter]

    matches = [f for f in candidates if f.get("name") == function_name]
    if not matches:
        matches = [f for f in candidates if f.get("name", "").lower() == function_name.lower()]
    if not matches:
        matches = [f for f in candidates
                   if f.get("name", "").endswith(f".{function_name}")
                   or function_name.endswith(f".{f.get('name', '')}")]
    if not matches:
        return f"Function `{function_name}` not found in ANSA API index."
    parts = [f"## `{function_name}` - ANSA API\n"]
    for func in matches:
        clean = {k: v for k, v in func.items() if not k.startswith("_")}
        parts.append(_format_result(clean))
    return "\n".join(parts)


def list_modules() -> str:
    """List all ANSA API modules and function counts. Returns markdown."""
    searcher = _get_searcher()
    modules = searcher.list_modules()
    metadata = searcher.metadata
    lines = [
        "## ANSA API Modules",
        f"*Total functions: {metadata.get('total_functions', '?')}  |  "
        f"API version: {metadata.get('api_version', '?')}*",
        "",
    ]
    for item in modules:
        mod = item["module"]
        count = item["function_count"]
        lines.append(f"- `{mod}` - {count} functions")
    return "\n".join(lines)


def list_categories() -> str:
    """List all ANSA API categories and function counts. Returns markdown."""
    searcher = _get_searcher()
    categories = searcher.list_categories()
    lines = ["## ANSA API Categories", ""]
    for item in categories:
        cat = item["category"]
        count = item["function_count"]
        lines.append(f"- `{cat}` - {count} functions")
    return "\n".join(lines)
