# -*- coding: utf-8 -*-
"""ansa-mcp-sum — server→plugin parameter contract audit.

Static, import-only check (does not need ANSA). Run it in CI or before shipping:

    PYTHONPATH=src python scripts/audit_command_keys.py
    # exit code 0 = clean, 1 = contract problem found

WHAT IT CHECKS
--------------
1. Every ``send_command("<type>", ...)`` in ``server.py`` has a handler in
   ``plugin.HANDLERS``.
2. Every **keyword the server sends** for a command type is actually *consumed*
   by the ANSA-side implementation — verified by resolving the handler to its
   real implementation function in ``tools_impl`` and looking for the key as a
   string literal in that function's source (via ``ast``).

WHY SOURCE LITERALS INSTEAD OF A ``command["key"]`` REGEX
---------------------------------------------------------
The first version of this script only recognised the literal subscript form
``command["key"]``, which produced false positives as soon as a helper was used
(``_path_arg(command, "path", "filepath", "output_path")``) or ``.get("key")``
appeared. Reading the AST string constants of the resolved implementation is
stricter about genuinely-ignored parameters while being immune to that.

This is the check that catches the class of bug where the server sends
``output_path=`` while the implementation reads ``command["path"]`` — which
surfaced in production as ``KeyError: 'path'`` inside the dispatch loop, meaning
the tool could never have worked at all.
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ansa_mcp_sum import plugin, tools_impl  # noqa: E402

SERVER_SRC = (SRC / "ansa_mcp_sum" / "server.py").read_text(encoding="utf-8")

CALL_OPEN = re.compile(r"send_command\(\s*[\"'](?P<type>[a-z0-9_]+)[\"']")
KEYWORD = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\s*=(?!=)")
#: kwargs that are transport concerns, not payload keys
NON_PAYLOAD = {"timeout_seconds"}


def _balanced_args(src: str, start: int) -> str:
    """Return the text of a call's arguments, honouring nesting and strings."""
    depth, i, quote = 0, start, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[start:i]
        i += 1
    return src[start:]


def extract_kwargs(args: str) -> set[str]:
    """Keyword names assigned in a call's argument text.

    A bare ``name=value`` regex is not enough: it also matches *annotated*
    defaults written as ``name: type = value``, so ``deck: int = -1`` yields a
    bogus ``int`` kwarg. Those are skipped by looking at the character before
    the name (skipping whitespace) — a ``:`` means it is an annotation type.
    """
    keys: set[str] = set()
    for match in KEYWORD.finditer(args):
        name = match.group(1)
        if name in NON_PAYLOAD:
            continue
        before = args[:match.start()].rstrip()
        if before.endswith(":"):
            continue
        keys.add(name)
    return keys


def calls_in_server() -> dict[str, set[str]]:
    """command type -> set of payload kwargs the server sends."""
    out: dict[str, set[str]] = {}
    for match in CALL_OPEN.finditer(SERVER_SRC):
        open_paren = SERVER_SRC.index("(", match.end() - 1)
        args = _balanced_args(SERVER_SRC, open_paren)
        out.setdefault(match.group("type"), set()).update(extract_kwargs(args))
    return out


def _source_of(fn) -> str:
    try:
        return textwrap.dedent(inspect.getsource(fn))
    except Exception:
        return ""


def literals_of(fn) -> set[str]:
    """All string constants in a function's source."""
    try:
        tree = ast.parse(_source_of(fn))
    except Exception:
        return set()
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def resolve_impl(handler) -> list:
    """The handler plus every ``tools_impl.<fn>`` it delegates to."""
    source = _source_of(handler)
    fns = [handler]
    for name in re.findall(r"tools_impl\.([A-Za-z_][A-Za-z0-9_]*)", source):
        target = getattr(tools_impl, name, None)
        if callable(target) and target not in fns:
            fns.append(target)
    return fns


def main() -> int:
    sent = calls_in_server()
    print(f"command types found in server.py: {len(sent)}")
    print()

    unknown = sorted(t for t in sent if t not in plugin.HANDLERS)
    mismatched: list[tuple[str, list[str]]] = []

    for ctype in sorted(sent):
        handler = plugin.HANDLERS.get(ctype)
        if handler is None:
            continue
        pool: set[str] = set()
        for fn in resolve_impl(handler):
            pool |= literals_of(fn)
        missing = sorted(k for k in sent[ctype] if k not in pool)
        if missing:
            mismatched.append((ctype, missing))

    if unknown:
        print("command types sent by the server with NO handler:")
        for ctype in unknown:
            print(f"  !! {ctype}")
        print()

    if mismatched:
        print(f"{'command type':<30}keys sent but never consumed")
        print("-" * 78)
        for ctype, missing in mismatched:
            print(f"{ctype:<30}!! {', '.join(missing)}")
        print()

    reachable = set(plugin.HANDLERS) & set(sent)
    print(f"handlers registered: {len(plugin.HANDLERS)}  |  reachable from server: {len(reachable)}")
    print()

    problems = len(unknown) + len(mismatched)
    if problems:
        print(f"FAIL: {problems} contract problem(s) found")
        return 1
    print("OK: every server kwarg is consumed by its ANSA-side handler")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
