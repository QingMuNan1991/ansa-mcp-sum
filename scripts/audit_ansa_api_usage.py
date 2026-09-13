#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Static audit: does the shipped code only call ANSA APIs we have verified?

Why this exists
---------------
``tools_impl.py`` calls into ``base`` / ``mesh`` / ``connections`` directly in
about a hundred places. A typo or an invented name is invisible until the
function is dispatched *inside the GUI host* — and by then a destructive step
may already have run. This script walks the AST of the ANSA-facing modules and
cross-checks every ``<module>.<attr>`` chain against
``ansa_api.VERIFIED_API`` / ``REJECTED_API``.

Usage (no ANSA required):

    PYTHONPATH=src python scripts/audit_ansa_api_usage.py            # report
    PYTHONPATH=src python scripts/audit_ansa_api_usage.py --strict   # exit 1 on any finding

Exit codes: 0 = clean (or report-only), 1 = findings under --strict,
2 = the audit itself could not run.
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: Modules that belong to ANSA. Anything else (os, json, api, ...) is ignored.
ANSA_MODULES = ("base", "mesh", "connections", "guitk", "constants", "ansa", "session", "utils")

#: Deliberately not gated: names used only inside `hasattr`/`getattr` probes and
#: names the fact layer itself resolves dynamically.
IGNORE = {
    "guitk.constants",       # namespace for BCOnExit* mode flags, not a function
    "guitk.constants.BCOnExitHide",
    "guitk.constants.BCOnExitDestroy",
    "ansa.session",          # bare module access used as a decorator namespace
    "ansa.base",
    "ansa.mesh",
    "ansa.connections",
}

#: Shipped code that talks to ANSA. Scratch/one-off exploration scripts (the
#: ``s7``–``s56`` Map-Block / geometry-fix lines, the early ``probe_*`` /
#: ``count_*`` probes, ...) were historical records of what was tried against a
#: live session and were pruned on 2026-09-13; with ``--all`` this audit still
#: covers whatever scratch scripts remain in ``scripts/``.
SHIPPED = (
    SRC / "ansa_mcp_sum" / "tools_impl.py",
    SRC / "ansa_mcp_sum" / "plugin.py",
    # Added in v1.0.2 batch 2: these are the modules that now surround the
    # bridge at runtime. They are expected to contain zero ANSA calls — that is
    # the point of listing them, so "expected to be pure" is a checked claim
    # rather than an assumption.
    SRC / "ansa_mcp_sum" / "audit.py",
    SRC / "ansa_mcp_sum" / "knowledge.py",
    SRC / "ansa_mcp_sum" / "memory.py",
    # Added in v1.0.7: the embedded-API-doc layer. It is a pure module (it only
    # reads the bundled ansa_api_index.json), and listing it here is what turns
    # "expected to be pure" into a checked claim rather than an assumption.
    SRC / "ansa_mcp_sum" / "api_doc.py",
    HERE / "ansa_mcp_sum_autoload.py",
    HERE / "probe_ansa_capabilities.py",
)


def _attr_chain(node: ast.AST) -> str | None:
    """``base.Foo`` -> 'base.Foo'; deeper chains join with dots too."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _walk_file(path: Path, ignore_lines: set[int] | None = None):
    """Yield (module_attr, lineno) for every ANSA module-attribute access."""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=str(path))
    # Positions that are only used for hasattr()/getattr() probing, not calling.
    probe_positions: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("hasattr", "getattr"):
            if node.args:
                probe_positions.add(getattr(node.args[0], "lineno", -1))
        # `for name in (...)` loops that iterate over candidate names
        if isinstance(node, (ast.Assign, ast.For, ast.Tuple, ast.List, ast.Set)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    probe_positions.add(getattr(sub, "lineno", -1))

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _attr_chain(node)
        if not chain:
            continue
        head = chain.split(".")[0]
        if head not in ANSA_MODULES:
            continue
        if chain in IGNORE:
            continue
        if getattr(node, "lineno", -1) in (ignore_lines or set()):
            continue
        if getattr(node, "lineno", -1) in probe_positions:
            continue
        out.append((chain, node.lineno))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit 1 when anything is flagged")
    ap.add_argument("--verbose", action="store_true", help="also list verified/known calls")
    ap.add_argument("--all", action="store_true",
                    help="also audit historical one-off probe scripts in scripts/")
    args = ap.parse_args()

    try:
        from ansa_mcp_sum import ansa_api
    except Exception as exc:  # pragma: no cover
        print("cannot import ansa_mcp_sum.ansa_api: %s" % exc)
        return 2

    verified = set(ansa_api.VERIFIED_API)
    # Registered optional attributes (build-dependent accessors, see
    # ansa_api.OPTIONAL_API). They are a *known* name like any other here — the
    # separate dict only fixes which lookup is allowed to return None, it does
    # not make the usage unverified. Without this union the eight
    # ansa.session.* / base.*File* uses reported as "unlisted" forever, and a
    # permanent 5-name exception is how a real typo eventually slips through.
    optional = set(getattr(ansa_api, "OPTIONAL_API", {}))
    rejected = set(ansa_api.REJECTED_API)

    overlap = (verified & optional) | (rejected & optional)
    if overlap:
        print("FACT LAYER ERROR: %s is in both OPTIONAL_API and another table"
              % ", ".join(sorted(overlap)))
        return 2

    targets = list(SHIPPED)
    if args.all:
        targets += sorted(HERE.glob("*.py"))
    targets = sorted(set(targets))

    findings: list[tuple[str, int, str, str]] = []
    known = 0
    known_optional = 0
    total = 0
    for path in targets:
        if not path.exists():
            continue
        for chain, lineno in _walk_file(path):
            total += 1
            if chain in optional and chain not in verified:
                known_optional += 1
                known += 1
                if args.verbose:
                    print("opt   %-46s %s:%d" % (chain, path.name, lineno))
                continue
            if chain in verified:
                known += 1
                if args.verbose:
                    print("ok    %-46s %s:%d" % (chain, path.name, lineno))
                continue
            if chain in rejected:
                findings.append((str(path), lineno, chain, "REJECTED (verified absent)"))
                continue
            # Unknown name: could be a genuine API we did not list, so report as
            # "unverified" rather than "wrong" — the probe manifest is what
            # decides. Only names in REJECTED are hard failures.
            if args.verbose:
                print("?     %-46s %s:%d" % (chain, path.name, lineno))

    print("=" * 78)
    print("ANSA API usage audit")
    print("  files scanned : %d" % len([t for t in targets if t.exists()]))
    print("  ANSA calls    : %d  (verified: %d, optional: %d, unlisted: %d)"
          % (total, known - known_optional, known_optional,
             total - known - len(findings)))
    print("  REJECTED uses : %d" % len(findings))
    print("=" * 78)
    if findings:
        for path, lineno, chain, verdict in findings:
            print("  FAIL %s:%d  %s  -> %s" % (Path(path).name, lineno, chain, verdict))
            replacement = ansa_api.REJECTED_API.get(chain)
            if replacement:
                print("        replace with: %s" % replacement)
    else:
        print("  no verified-absent API is called.")

    if args.strict and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
