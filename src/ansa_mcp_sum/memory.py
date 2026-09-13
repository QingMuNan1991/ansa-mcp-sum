# -*- coding: utf-8 -*-
"""Cross-session memory: preferences that survive a restart, and notes that
survive a reinstall of the operator's head.

Before this existed the bridge had two kinds of remembered state, and neither
was usable as knowledge: ``status.json`` (overwritten on every start) and
``logs/dropped.log`` (only failures). Everything else that a returning session
actually needs — which deck a project uses, where its ``.ansa_mpar`` lives,
that ``Casting_initial.ansa`` is 1.86-3.88 mm thick so 3.0 is the mid-surface
target — existed only in conversation. Every new session re-derived it.

Two stores, on purpose
----------------------
``prefs.json`` is keyed and machine-usable: a deck id, a path, a threshold.
``notes.md`` is append-only prose: the observations that do not fit a key.
Collapsing them into one would mean either a key-value store full of essays or
a text file nothing can query.

Durability rules
----------------
* Writes are atomic (temp file + replace) because the same HOME can be open in
  two client processes.
* ``load_prefs()`` never raises on a corrupt file — it reports the damage and
  starts clean, because a broken preference file must not stop the bridge.
  The damage is reported rather than hidden; silently resetting would be the
  same class of bug as a silent zero in the API layer.
* Nothing here needs ANSA. Memory works with the bridge stopped, which is
  precisely when "what was I doing?" is worth asking.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import audit
from .config import AnsaMcpConfig
from .ipc import atomic_write_json, atomic_write_text, utc_timestamp

CONFIG = AnsaMcpConfig.from_env()

#: Guard against a runaway loop turning memory into a log. Generous: this is
#: meant for preferences, not for bulk data.
MAX_VALUE_CHARS = 4000
#: Keys are used in output, so keep them boring and greppable.
MAX_KEY_CHARS = 120
#: How much of notes.md ``snapshot()`` returns.
NOTES_TAIL_CHARS = 4000


def _normalized_key(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        raise ValueError("key must not be empty")
    if len(key) > MAX_KEY_CHARS:
        raise ValueError(f"key must be <= {MAX_KEY_CHARS} characters")
    return key


def _check_value_size(value: Any) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, default=repr))
    except Exception:
        size = len(repr(value))
    if size > MAX_VALUE_CHARS:
        raise ValueError(
            f"value is {size} characters; the limit is {MAX_VALUE_CHARS}. "
            "Memory is for preferences, not payloads — put the data somewhere "
            "under HOME and remember the path instead."
        )


def load_prefs() -> dict[str, Any]:
    """Every stored preference. Returns ``{}`` for a missing file.

    A corrupt file yields ``{"_error": ...}`` rather than an exception: the
    caller can then surface it instead of silently reporting 'nothing
    remembered', which is what a bare ``{}`` would mean.
    """
    path = CONFIG.prefs_file
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {"_error": f"unreadable prefs file: {type(exc).__name__}: {exc}",
                "_path": str(path)}
    if not isinstance(data, dict):
        return {"_error": f"prefs file root is {type(data).__name__}, expected an object",
                "_path": str(path)}
    return data


def save_prefs(prefs: dict[str, Any]) -> None:
    CONFIG.ensure_dirs()
    atomic_write_json(CONFIG.prefs_file, prefs)


def set_pref(key: str, value: Any, note: str = "") -> dict[str, Any]:
    """Upsert one preference; returns the stored record."""
    key = _normalized_key(key)
    _check_value_size(value)
    prefs = load_prefs()
    # A damaged file would otherwise be carried forward forever.
    if "_error" in prefs:
        prefs = {}
    record = {
        "value": value,
        "updated": utc_timestamp(),
        "updated_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": str(note or ""),
    }
    previous = prefs.get(key)
    if previous is not None:
        record["previous_value"] = (previous or {}).get("value")
    prefs[key] = record
    save_prefs(prefs)
    return record


def get_pref(key: str) -> dict[str, Any] | None:
    return load_prefs().get(_normalized_key(key))


def delete_pref(key: str) -> bool:
    key = _normalized_key(key)
    prefs = load_prefs()
    if key not in prefs:
        return False
    prefs.pop(key, None)
    save_prefs(prefs)
    return True


def append_note(text: str, tag: str = "") -> dict[str, Any]:
    """Append one timestamped block to notes.md (append-only, never rewrites)."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("note text must not be empty")
    CONFIG.ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"## {stamp}" + (f"  [{tag}]" if tag else "")
    block = f"{header}\n\n{text}\n\n"
    existing = ""
    path = CONFIG.notes_file
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read()
    except Exception:
        existing = ""
    if not existing:
        existing = ("# ANSA MCP notes\n\n"
                    "Model-level facts that do not fit a key/value preference.\n"
                    "Append-only; the newest entry is at the bottom.\n\n")
    # Rewrite rather than open("a"): two clients can share a HOME, and an
    # interleaved append would splice two notes into one unreadable block. The
    # file is small and the write is atomic.
    atomic_write_text(path, existing + block)
    return {"path": str(path), "chars": len(block), "at": stamp}


def read_notes(tail_chars: int = NOTES_TAIL_CHARS) -> dict[str, Any]:
    path = CONFIG.notes_file
    if not path.exists():
        return {"available": False, "path": str(path), "text": ""}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception as exc:
        return {"available": False, "path": str(path),
                "text": "", "error": f"{type(exc).__name__}: {exc}"}
    truncated = len(text) > tail_chars
    return {
        "available": True,
        "path": str(path),
        "chars": len(text),
        "truncated": truncated,
        "text": text[-tail_chars:] if truncated else text,
    }


def snapshot(limit: int = 10) -> dict[str, Any]:
    """Everything a freshly started session needs, in one object.

    Order matters: preferences and notes first (what is true now), history last
    (what happened). A model reading this top-down gets the current state before
    it gets the log.
    """
    prefs = load_prefs()
    prefs_view = {}
    for key, record in prefs.items():
        if isinstance(record, dict) and "value" in record:
            prefs_view[key] = {
                "value": record.get("value"),
                "updated_local": record.get("updated_local"),
                "note": record.get("note") or None,
            }
        else:
            prefs_view[key] = {"value": record}
    return {
        "home": str(CONFIG.home),
        "prefs_file": str(CONFIG.prefs_file),
        "notes_file": str(CONFIG.notes_file),
        "audit_file": str(CONFIG.audit_file),
        "preferences": prefs_view,
        "preference_count": len(prefs_view),
        "notes": read_notes(),
        "history": audit.summarize(limit=limit * 20),
    }


def snapshot_json(limit: int = 10) -> str:
    return json.dumps(snapshot(limit=limit), indent=2, ensure_ascii=False)
