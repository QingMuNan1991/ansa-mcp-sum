# -*- coding: utf-8 -*-
"""Runtime audit trail — what ANSA actually did, one line per command.

Why this file exists
--------------------
``scripts/audit_*.py`` are *offline code* audits: they read the source tree.
They can tell you that no shipped call hits a rejected API. They cannot tell
you that at 14:02 ``mesh_shells`` ran for 54 s on ``Casting_initial.ansa`` and
succeeded. After a crash (README §7.6, where the ANSA process simply vanished)
there was no record at all of what had been running: ``results/`` stayed empty,
``status.json`` froze at the start time, and the Windows event log said nothing.

The audit log is written **only by the plugin**, from inside ANSA, because that
is the only place that knows the two things worth recording:

* the **real** duration (the client only knows how long it waited, which a
  timeout makes meaningless);
* the **model path before and after** (the client cannot see whether the
  command opened a different file or saved over the one it started with).

Single-writer discipline is deliberate: appends from the server process too
would mean two writers on one file with no lock, and a torn JSONL line is
worse than a missing one. Client-side facts (timeouts, validation refusals)
already have a home in ``runs/<stamp>-<id>/``.

Reading it back
---------------
``read_recent()`` tolerates the two failure modes a live log actually has: a
partially flushed final line, and rotation. Anything unparseable is skipped
rather than raised — a corrupt line must not take out the tool that reports it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import AnsaMcpConfig
from .ipc import utc_timestamp

CONFIG = AnsaMcpConfig.from_env()

#: Rotate before the file becomes something no editor will open. 4 MB is
#: roughly 20k command lines — months of interactive use, weeks of batch use.
MAX_BYTES = 4 * 1024 * 1024
#: ``audit.1.jsonl`` / ``audit.2.jsonl``. Older rotations are dropped by the
#: same code that makes them, so the log has a bounded footprint with no cron.
KEEP_ROTATIONS = 2

#: Payload keys replaced by a hash instead of being written out. A single
#: ``execute_script`` payload is up to 64 KB; inlining it would make the audit
#: log unreadable and turn a debugging aid into a second copy of the script.
HASHED_KEYS = ("script",)

#: Transport/metadata keys that are not part of "what was asked".
_NON_PAYLOAD = ("id", "type", "timestamp", "timeout_seconds", "run_dir")


def _hash(text: str) -> str:
    return "sha1:" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def args_fingerprint(command: dict[str, Any]) -> str:
    """Stable fingerprint of a command's payload.

    Two calls with the same fingerprint did the same thing with the same
    arguments — which is what makes the log usable for "did I already run
    this?" without storing the arguments themselves. Key order is normalised so
    a dict built in a different order hashes the same.
    """
    payload: dict[str, Any] = {}
    for key, value in (command or {}).items():
        if key in _NON_PAYLOAD:
            continue
        if key in HASHED_KEYS and isinstance(value, str):
            payload[key] = _hash(value)
            payload[key + "_chars"] = len(value)
            continue
        payload[key] = value
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        blob = repr(sorted(payload))
    return _hash(blob)


def payload_size_chars(command: dict[str, Any]) -> int:
    """Total size of the hashed payload keys, so an oversized script is visible."""
    return sum(len(v) for k, v in (command or {}).items()
               if k in HASHED_KEYS and isinstance(v, str))


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
    except Exception:
        return
    for index in range(KEEP_ROTATIONS, 0, -1):
        older = path.with_name(f"audit.{index}.jsonl")
        newer = path.with_name(f"audit.{index - 1}.jsonl") if index > 1 else path
        try:
            if older.exists():
                older.unlink()
            if newer.exists():
                newer.replace(older)
        except Exception:
            return


def _newline_guard(path: Path) -> str:
    """``"\\n"`` when the log's previous write was torn, otherwise ``""``.

    A process killed mid-append — or anything else that writes to this file —
    can leave a partial line with no trailing newline. Appending straight onto
    it welds the new event to the fragment and the result parses as neither, so
    the event that is *lost* is the new one, not the torn one. ``read_recent``
    skipping a torn line is not enough: the writer has to close the wound before
    adding to it.

    Reads one byte, so this costs nothing.
    """
    try:
        if path.stat().st_size == 0:
            return ""
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return "" if fh.read(1) == b"\n" else "\n"
    except Exception:
        return ""


def append_event(event: dict[str, Any]) -> bool:
    """Append one event. Returns False when it could not be written.

    Never raises: a log write must not be able to fail a mesh that succeeded.
    The caller folds the return value into the bridge state instead, so a
    broken log directory shows up as ``audit_write_errors`` in ``status.json``
    rather than as silence.
    """
    path = CONFIG.audit_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(_newline_guard(path) + line)
        return True
    except Exception:
        return False


def record_command(
    *,
    command: dict[str, Any],
    ok: bool | None,
    duration_s: float,
    error_type: str | None = None,
    model_before: str | None = None,
    model_after: str | None = None,
    model_snapshot_error: str | None = None,
    pid: int | None = None,
) -> bool:
    """One line per dispatched command, written from the plugin's ``finally``."""
    command = command or {}
    event = {
        "ts": utc_timestamp(),
        "event": "command",
        "id": command.get("id"),
        "type": command.get("type"),
        "args_hash": args_fingerprint(command),
        "deck": command.get("deck", command.get("deck_before")),
        "model_before": model_before,
        "model_after": model_after,
        "model_changed": (model_before != model_after
                          if (model_before or model_after) else None),
        "duration_s": round(float(duration_s), 3),
        "ok": ok,
        "error_type": error_type,
        "pid": pid if pid is not None else os.getpid(),
    }
    # Only present when the snapshot failed. A null model_before means either
    # "nothing was open" or "we could not ask"; the two look identical in a log
    # and only one of them is a defect in this project.
    if model_snapshot_error:
        event["model_snapshot_error"] = model_snapshot_error
    size = payload_size_chars(command)
    if size:
        event["payload_chars"] = size
    return append_event(event)


def record_dropped(*, command_id: str | None, age_s: float, limit_s: float,
                   command_type: str | None = None) -> bool:
    """A command that was thrown away before it ever ran.

    This is the answer to "I sent it and nothing happened" — without it the
    only trace was ``logs/dropped.log``, a different file with a different
    format that nothing reads back.
    """
    return append_event({
        "ts": utc_timestamp(),
        "event": "dropped_stale",
        "id": command_id,
        "type": command_type,
        "age_s": round(float(age_s), 1),
        "limit_s": round(float(limit_s), 1),
        "ok": False,
        "error_type": "StaleCommand",
        "pid": os.getpid(),
    })


def record_bridge(*, event: str, detail: str = "") -> bool:
    """Lifecycle markers (``started`` / ``stopped``) so a gap in the log means
    something: commands before a ``stopped`` line were a different session."""
    return append_event({
        "ts": utc_timestamp(),
        "event": event,
        "detail": detail,
        "pid": os.getpid(),
    })


def audit_paths() -> list[Path]:
    """Current log first, then rotations newest-first."""
    current = CONFIG.audit_file
    older = [current.with_name(f"audit.{i}.jsonl") for i in range(1, KEEP_ROTATIONS + 1)]
    return [p for p in [current] + older if p.exists()]


def read_recent(limit: int = 50) -> list[dict[str, Any]]:
    """The newest ``limit`` events, oldest-first within the returned slice."""
    if limit <= 0:
        return []
    lines: list[str] = []
    for path in audit_paths():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines.extend(fh.readlines())
        except Exception:
            continue
        if len(lines) >= limit:
            break
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            # A torn final line is normal for a live log; skip, do not raise.
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def summarize(limit: int = 200) -> dict[str, Any]:
    """Distilled history: totals plus the last few commands.

    This is what ``ansa://memory`` hands to a model that has just woken up, so
    it is shaped as *answers*, not as a log dump: what ran, what failed, what
    is still worth worrying about.
    """
    events = read_recent(limit)
    commands = [e for e in events if e.get("event") == "command"]
    failures = [e for e in commands if e.get("ok") is False]
    dropped = [e for e in events if e.get("event") == "dropped_stale"]
    by_type: dict[str, int] = {}
    for event in commands:
        key = str(event.get("type"))
        by_type[key] = by_type.get(key, 0) + 1
    stats = {
        "available": bool(audit_paths()),
        "events_scanned": len(events),
        "commands": len(commands),
        "failures": len(failures),
        "dropped_stale": len(dropped),
        "counts_by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])[:15]),
        "slowest_s": max((e.get("duration_s") or 0) for e in commands) if commands else None,
    }
    if failures:
        last = failures[-1]
        stats["last_failure"] = {
            "id": last.get("id"),
            "type": last.get("type"),
            "error_type": last.get("error_type"),
            "ts": last.get("ts"),
        }
    return {
        "stats": stats,
        "recent": [
            {k: e.get(k) for k in
             ("ts", "event", "id", "type", "ok", "duration_s", "error_type",
              "model_before", "args_hash")}
            for e in commands[-10:]
        ],
    }
