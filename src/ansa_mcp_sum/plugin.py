from __future__ import annotations

import ast
import contextlib
import inspect
import io
import math
import os
import platform
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import ansa_api
from . import audit
from . import tools_impl
from .config import AnsaMcpConfig
from .ipc import atomic_write_json, error_response, read_json, success_response, utc_timestamp

try:
    import ansa  # type: ignore
    from ansa import base, constants  # type: ignore

    ANSA_AVAILABLE = True
except Exception:
    ansa = None  # type: ignore
    base = None  # type: ignore
    constants = None  # type: ignore
    ANSA_AVAILABLE = False


CONFIG = AnsaMcpConfig.from_env()

#: Fallback only. A queued command must outlive *its own* client timeout,
#: otherwise the ANSA side silently deletes a command the server is still
#: waiting on (the old 120s value vs a 600s client timeout produced exactly
#: that — commands vanished and the client sat until timeout with nothing in
#: logs/ to explain it).
#:
#: The authoritative number is per-command: ``mcp_cli.py --timeout N`` writes N
#: into the payload and then really waits N seconds. Reading the global
#: ``CONFIG.timeout_seconds`` (600) instead deleted a ``--timeout 1800`` command
#: at 660s while its client still had ~19 minutes of patience left — the
#: command vanished, nothing errored, and the only trace was a dropped.log line.
#: This constant is what ``_command_age_limit`` returns when the payload cannot
#: be read or carries no usable timeout.
STALE_COMMAND_AGE_SECONDS = float(CONFIG.timeout_seconds) + 60.0

#: Grace on top of the client's own timeout: the client must be the one to give
#: up first, so a late-but-real result is never thrown away by the producer.
COMMAND_EXPIRY_GRACE_SECONDS = 60.0

#: A claimed command is *supposed* to be invisible to the queue, so it needs its
#: own expiry — generous enough that a slow handler (a full quality check is
#: ~20 min) is never collected while it is still running. Collecting a live claim
#: costs nothing functionally, but it throws away the only crash evidence there
#: is, so the rule here is "wait well past any plausible handler runtime".
CLAIM_EXPIRY_GRACE_SECONDS = 600.0


def _claim_command(path):
    """Take ownership of a queued command by renaming it. ``None`` if it is gone.

    This is the fix for the worst failure this bridge has produced. The file used
    to stay in ``commands/`` for the whole execution and was only removed in the
    handler's ``finally``, so a second entry into :func:`ansa_mcp_process_one`
    saw the same command as pending and ran it again. Nothing prevented that:
    a hot reload re-armed the 200 ms timer *inside* the handler and reset the
    ``_busy`` guard on the way, and one command ran **82 times**, creating 82
    duplicate ``ANSA_MCP_SUM_Bridge`` windows (README §10.21).

    ``os.replace`` on the same filesystem is atomic, so exactly one caller can
    win the rename; everybody else finds the file missing and returns. The
    command is therefore never "pending" and "executing" at the same time.
    """
    claim = path.with_name(path.name + CLAIM_SUFFIX)
    try:
        os.replace(path, claim)
    except OSError:
        # Already claimed by another caller, or removed by a cleanup sweep.
        return None
    return claim

#: Bridges state, echoed in status.json so the client can tell "queued" from
#: "executing" from "dead". Without it the only observable failure mode was a
#: 600-second timeout.
BRIDGE: dict[str, Any] = {
    "bridge_state": "idle",       # idle | executing | stopping | stopped
    "current_command_id": None,
    "current_command_type": None,
    "last_command_result": None,  # "ok" | "error"
    "last_command_type": None,    # survives the reset, unlike current_command_type
    "last_error": None,
    "processed_count": 0,
    "dropped_stale": 0,
    #: Counts failures to append to logs/audit.jsonl. A log write must never
    #: fail a command, so the failure is counted here and surfaced in
    #: status.json instead of being swallowed — an audit log that quietly does
    #: nothing is worse than no audit log, because it is trusted.
    "audit_write_errors": 0,
}

#: Live GUI objects that must outlive a re-execution of the autoload script.
#:
#: ``ansa.ImportCode`` runs ``ansa_mcp_sum_autoload.py`` once, but
#: ``bridge_doctor`` deliberately re-executes that file into the same namespace
#: to make an edited fix take effect without restarting ANSA. A module body
#: reassigns every module-level name it defines, so ``_bridge_window`` and
#: ``_poll_timer`` came back as ``None`` on every reload - and the reloaded body
#: immediately built a *new* timer on a *new* ``ANSA_MCP_SUM_Bridge`` window.
#: This module is never re-executed, so it is the only durable home for them.
#:
#: Deliberately NOT part of ``BRIDGE``: ``write_status`` splats ``BRIDGE`` into
#: status.json, and a Qt object is not JSON-serialisable.
GUI_HANDLES: dict[str, Any] = {}

#: Suffix that marks a queued command as taken by a handler. Naming that state on
#: disk is the point: it survives a crash, which is what makes "the bridge died
#: mid-command" answerable afterwards instead of a gap in the queue.
CLAIM_SUFFIX = ".running"

#: Idle heartbeats are throttled. The GUI timer fires every 200ms; writing
#: status.json on every tick meant 5 file writes/second — 18,000/hour — for a
#: file whose only job is to say "nothing happened". Nothing consumes the
#: freshness of an idle status (the server's staleness window is effectively
#: infinite for this on-demand bridge), so 5s is plenty and cuts the churn by
#: 25x. Any state *change* still writes immediately.
IDLE_STATUS_INTERVAL_SECONDS = 5.0
_LAST_IDLE_STATUS_AT = 0.0


def write_status(status: str, message: str = "") -> None:
    CONFIG.ensure_dirs()
    try:
        queue_depth = len(list(CONFIG.commands_dir.glob("cmd_*.json")))
    except Exception:
        queue_depth = None
    data = {
        "status": status,
        "message": message,
        "version": __version__,
        "timestamp": utc_timestamp(),
        "pid": os.getpid(),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "ansa_available": ANSA_AVAILABLE,
        "ansa_mcp_home": str(CONFIG.home),
        "queue_depth": queue_depth,
        **BRIDGE,
    }
    if ANSA_AVAILABLE:
        data.update(_safe_current_context())
    atomic_write_json(CONFIG.status_file, data)
    return True


def write_idle_status(message: str = "idle", force: bool = False) -> bool:
    """Heartbeat that only touches disk every ``IDLE_STATUS_INTERVAL_SECONDS``.

    Returns True when a write actually happened, so callers can stay quiet too.
    """
    global _LAST_IDLE_STATUS_AT
    now = time.time()
    if not force and (now - _LAST_IDLE_STATUS_AT) < IDLE_STATUS_INTERVAL_SECONDS:
        return False
    _LAST_IDLE_STATUS_AT = now
    write_status("running", message)
    return True


def _safe_current_context() -> dict[str, Any]:
    """Deck / model context. Deck is reported by *name* and with the node type
    that must be used for it — the previous version reported only the raw deck
    int, which is why 'GRID vs NODE' was never noticed."""
    data: dict[str, Any] = {}
    if not ANSA_AVAILABLE:
        return data
    try:
        deck = int(base.CurrentDeck())
        data["current_deck"] = deck
        data["deck_name"] = ansa_api.deck_name(deck)
        data["node_type"] = ansa_api.node_type_for(deck)
    except Exception as exc:
        data["current_deck_error"] = str(exc)
    # Read the open-model path through the fact layer rather than a bare
    # getattr. The accessor is genuinely build-dependent, so it is registered in
    # ansa_api.OPTIONAL_API and probed in one documented place; README §7.4 bans
    # the inline `getattr(mod, "Fn", None)` form because a probe and a typo look
    # identical until the call site raises TypeError.
    model_path = ansa_api.current_model_path()
    if model_path is not None:
        data["current_model_path"] = model_path
    else:
        # Say which of the two cases this is. "Nothing open" is normal; "we have
        # no accessor for this build" is our defect, and status.json is the file
        # a human reads first when a call comes back with no model attached.
        data["current_model_path_accessor"] = (
            ansa_api.current_model_path_accessor() or "NONE (candidate list is wrong)")
    try:
        data["model_loaded"] = bool(ansa_api.count_nodes(
            ansa_api.normalize_deck(None))["count"]) or bool(
                ansa_api.count(ansa_api.normalize_deck(None), "FACE"))
    except Exception:
        data["model_loaded"] = None
    return data


def _audit(written: bool) -> None:
    """Fold an audit-write failure into the bridge state instead of raising.

    Audit is an observer: it must never be able to fail the operation it is
    observing. But a silent failure would make the log *look* complete when it
    is not, so the count is kept and reported.
    """
    if not written:
        BRIDGE["audit_write_errors"] = BRIDGE.get("audit_write_errors", 0) + 1


def _model_snapshot() -> tuple[str | None, str | None]:
    """``(path, problem)`` for the audit trail's before/after pair.

    ``problem`` is a short reason string when the path is unknown *because we
    could not ask*. "Nothing is open" returns ``(None, None)`` — a legitimate
    state, not a defect — while "no accessor resolved" returns a reason, so a
    null in the log says which one happened. The previous version collapsed
    both into ``None`` and hid a dead accessor list through an entire release;
    it was only caught by reading the log of a live session and noticing that
    ``open_model`` recorded no model change.
    """
    if not ANSA_AVAILABLE:
        return None, "ANSA is not importable in this interpreter"
    accessor = ansa_api.current_model_path_accessor()
    if accessor is None:
        return None, ("no accessor for the open model path resolved in this build: "
                      + ", ".join(ansa_api.CURRENT_MODEL_PATH_CANDIDATES))
    try:
        path = ansa_api.current_model_path()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    return (path, None) if path else (None, None)


def _command_age_limit(payload: dict) -> float:
    """How old a single queued command may get before it is abandoned.

    The client's own ``--timeout`` is authoritative: ``mcp_cli.py`` writes it
    into the payload and then really waits that long, so the producer must
    outlive the consumer or results get thrown away under a live client.
    Falls back to ``STALE_COMMAND_AGE_SECONDS`` when the file is unreadable or
    carries no usable timeout, so a corrupt command still gets collected.
    """
    try:
        client_timeout = float(payload.get("timeout_seconds"))
    except (TypeError, ValueError):
        return STALE_COMMAND_AGE_SECONDS
    if client_timeout <= 0:
        return STALE_COMMAND_AGE_SECONDS
    return client_timeout + COMMAND_EXPIRY_GRACE_SECONDS


def cleanup_stale_commands() -> int:
    """Drop commands older than their own client's patience, with a log entry.

    Two kinds of file live in ``commands/`` and they expire on different clocks:

    * ``cmd_*.json`` — still pending. Its own ``timeout_seconds`` decides.
    * ``cmd_*.json.running`` — claimed by a handler (see :func:`_claim_command`),
      i.e. executing right now. Collected only long after any plausible handler
      runtime, so a crash leaves the evidence in place long enough to be read.

    Returns the number dropped. Every drop is written to ``logs/dropped.log`` so
    a vanished command is at least attributable after the fact.
    """
    now = time.time()
    dropped = 0
    for pattern, extra_grace in (("cmd_*.json", 0.0),
                                 ("cmd_*" + CLAIM_SUFFIX,
                                  CLAIM_EXPIRY_GRACE_SECONDS)):
        for path in CONFIG.commands_dir.glob(pattern):
            try:
                age = now - path.stat().st_mtime
            except Exception:
                continue
            # Payload first: the limit is per-command, not the global default. A
            # claim file still carries the full payload — renaming it in place
            # (rather than moving it to another directory) is what keeps the
            # per-command expiry rule working after a claim.
            command_id = path.name.split(".")[0]
            command_type = None
            payload: dict = {}
            try:
                payload = read_json(path)
                command_id = payload.get("id", command_id)
                command_type = payload.get("type")
            except Exception:
                payload = {}
            limit = _command_age_limit(payload) + extra_grace
            if age <= limit:
                continue
            try:
                CONFIG.logs_dir.mkdir(parents=True, exist_ok=True)
                with open(CONFIG.logs_dir / "dropped.log", "a", encoding="utf-8") as fh:
                    fh.write(
                        f"{datetime.now().isoformat()}\tdropped\t{command_id}\t"
                        f"age={age:.1f}s\tlimit={limit:.0f}s\t"
                        f"{'claimed' if extra_grace else 'pending'}\n"
                    )
            except Exception:
                pass
            # Also into the audit stream: dropped.log is a separate file in a
            # separate format that nothing reads back, so "my command vanished"
            # was answerable only by knowing to go looking for it.
            _audit(audit.record_dropped(command_id=command_id, age_s=age,
                                        limit_s=limit,
                                        command_type=command_type))
            try:
                path.unlink(missing_ok=True)
                dropped += 1
                BRIDGE["dropped_stale"] = BRIDGE.get("dropped_stale", 0) + 1
            except Exception:
                pass
    return dropped


def _write_result(command: dict[str, Any], result: dict[str, Any]) -> None:
    command_id = command.get("id", "unknown")
    result["id"] = command_id
    atomic_write_json(CONFIG.results_dir / f"{command_id}.json", result)


def handle_ping(command: dict[str, Any]) -> dict[str, Any]:
    return success_response(
        message="pong",
        data={
            "plugin_version": __version__,
            "ansa_available": ANSA_AVAILABLE,
        },
    )


def handle_get_capabilities(command: dict[str, Any]) -> dict[str, Any]:
    data = {
        "plugin_version": __version__,
        "ansa_available": ANSA_AVAILABLE,
        "python": sys.version,
        "platform": platform.platform(),
        "home": str(CONFIG.home),
    }
    data.update(_safe_current_context())
    return success_response(message="capabilities", data=data)


def _build_live_ansa_api() -> dict[str, set]:
    """Map ansa submodule name -> set of its attribute names (live, this build)."""
    if not ANSA_AVAILABLE:
        return {}
    live: dict[str, set] = {}
    for name in dir(ansa):
        if name.startswith("_"):
            continue
        try:
            obj = getattr(ansa, name)
        except Exception:
            continue
        if inspect.ismodule(obj):
            live[name] = set(dir(obj))
    return live


def _resolve_attr_path(node: Any) -> list[str] | None:
    """Return the dotted path of an Attribute/Name chain, e.g. ['ansa','base','GetEntity'].

    Returns None for anything that is not a clean dotted-name chain (e.g. a call
    through a computed/local variable), so those are skipped rather than flagged.
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return None
    parts.reverse()
    return parts


def _validate_script_source(script: str) -> dict[str, Any]:
    """Parse a script and verify every ansa.* / <module>.* call exists in live ANSA.

    Returns {"ok", "checked", "missing":[...], "found":[...], ...}. Conservative:
    only flag Call nodes whose root is 'ansa' (walk the real object) or a known
    ansa submodule; everything else (user functions, locals, getattr() strings)
    is ignored so the gate does not produce false positives.
    """
    if not ANSA_AVAILABLE:
        return {"ok": True, "checked": 0, "missing": [], "found": [],
                "note": "ANSA not importable in this interpreter; validation skipped"}
    live = _build_live_ansa_api()
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError as exc:
        return {"ok": False, "checked": 0, "missing": [], "found": [],
                "syntax_error": str(exc)}

    missing: list[dict[str, str]] = []
    found: list[str] = []
    seen: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        path = _resolve_attr_path(func)
        if not path or len(path) < 2:
            continue
        root = path[0]
        # Only ansa-rooted or known-ansa-submodule calls are validated. Anything
        # else (user functions, other libraries) is ignored so the gate never
        # emits a false positive and is never counted as "checked".
        if root != "ansa" and root not in live:
            continue
        key = ".".join(path)
        if key in seen:
            continue
        seen.add(key)
        if root == "ansa":
            obj = ansa
            prefix = "ansa"
            broken = False
            for seg in path[1:-1]:
                prefix = prefix + "." + seg
                obj = getattr(obj, seg, None)
                if obj is None:
                    missing.append({"call": key, "reason": "%s not found" % prefix})
                    broken = True
                    break
            if broken:
                continue
            final = path[-1]
            if hasattr(obj, final):
                found.append(key)
            else:
                missing.append({"call": key,
                                "reason": "function %r not found in %s" % (final, ".".join(path[:-1]))})
        else:  # root in live
            obj = getattr(ansa, root, None)
            final = path[-1]
            if hasattr(obj, final):
                found.append(key)
            else:
                missing.append({"call": key,
                                "reason": "function %r not found in module %r" % (final, root)})
    return {"ok": len(missing) == 0, "checked": len(seen),
            "missing": missing, "found": found}


def handle_validate_script(command: dict[str, Any]) -> dict[str, Any]:
    """Standalone pre-flight validator (the validate_script MCP tool)."""
    script = command.get("script")
    if not isinstance(script, str):
        return error_response("script must be a string", error_type="ValidationError")
    report = _validate_script_source(script)
    return success_response(message="script validated", data=report)


def handle_execute_script(command: dict[str, Any]) -> dict[str, Any]:
    script = command.get("script")
    if not isinstance(script, str):
        return error_response("script must be a string", error_type="ValidationError")

    # Anti-hallucination gate (Plan B): refuse to run a script that references
    # an ANSA API call which does not exist in THIS build. The 100%-accurate
    # check is against the live process, so version drift cannot produce a
    # false pass. validate defaults to True; a caller that knows a call is
    # valid but not dir()-discoverable can opt out with validate=False.
    if command.get("validate", True):
        report = _validate_script_source(script)
        if not report.get("ok"):
            return error_response(
                "script references missing ANSA API; refused to execute",
                error_type="AnsaApiMissing",
                detail="; ".join(item["reason"] for item in report.get("missing", [])),
                data={"missing": report.get("missing"), "checked": report.get("checked")},
            )

    stdout = io.StringIO()
    stderr = io.StringIO()
    namespace: dict[str, Any] = {
        "__name__": "__ansa_mcp_exec__",
        "ansa": ansa,
        "base": base,
        "constants": constants,
    }

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = compile(script, f"<ansa-mcp:{command.get('id', 'unknown')}>", "exec")
            exec(code, namespace)
        data: dict[str, Any] = {}
        if "RESULT" in namespace:
            data["RESULT"] = namespace["RESULT"]
        if "result" in namespace:
            data["result"] = namespace["result"]
        return success_response(
            message="script executed",
            data=data,
            logs={"stdout": stdout.getvalue(), "stderr": stderr.getvalue()},
        )
    except Exception as exc:
        return error_response(
            "script execution failed",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                "traceback": traceback.format_exc(),
            },
        )


def _collect_count(deck: Any, entity_type: str) -> tuple[int | None, str | None]:
    """Count entities of one type.

    ``recursive`` is fixed to the documented default (``False``). The old code
    used ``recursive=True`` here but ``False`` in ``tools_impl``, so the same
    model produced two different numbers depending on which entry point you
    asked — a silent inconsistency, not an error.
    """
    try:
        entities = base.CollectEntities(deck, None, entity_type, False)
        return len(entities), None
    except Exception as exc:
        return None, f"{entity_type}: {exc}"


def handle_get_model_info(command: dict[str, Any]) -> dict[str, Any]:
    """Model inventory with deck-correct entity-type strings.

    The previous version hardcoded ``"NODE"``/``"ELEMENT"``. On a NASTRAN deck the
    node type is ``GRID``, so it reported a model with 27763 shells and **zero**
    nodes — no exception, just a wrong fact handed to the model.
    """
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    warnings: list[str] = []
    data: dict[str, Any] = _safe_current_context()
    try:
        deck = int(base.CurrentDeck())
    except Exception as exc:
        return error_response("failed to read current deck", error_type=type(exc).__name__, detail=str(exc))

    node_type = ansa_api.node_type_for(deck)
    entity_types = {
        "parts": "ANSAPART",
        "materials": "MAT1",
        "properties": "PSHELL",
        "faces": "FACE",
        "nodes": node_type,
        "shells": "SHELL",
        "solids": "SOLID",
        "elements": "ELEMENT",
    }
    counts: dict[str, int | None] = {}
    for label, entity_type in entity_types.items():
        count, warning = _collect_count(deck, entity_type)
        counts[label] = count
        if warning:
            warnings.append(warning)

    # Cross-check the node count with the alternative spelling. A model with
    # shells but no nodes is the signature of a deck/type mismatch.
    if not counts.get("nodes"):
        alt = "NODE" if node_type == "GRID" else "GRID"
        alt_count, _ = _collect_count(deck, alt)
        if alt_count:
            counts["nodes"] = alt_count
            warnings.append(
                f"node type mismatch: deck {ansa_api.deck_name(deck)} matched {alt!r} "
                f"rather than {node_type!r} — using {alt!r}"
            )
    if counts.get("shells") and not counts.get("nodes"):
        warnings.append(
            "model reports shells but zero nodes; treat the node count as unreliable"
        )
    data["entity_counts"] = counts
    data["node_type"] = node_type
    data["deck_name"] = ansa_api.deck_name(deck)
    return success_response(message="model info", data=data, warnings=warnings)


def handle_geometry_inventory(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        deck = base.CurrentDeck()
    except Exception as exc:
        return error_response("failed to read current deck", error_type=type(exc).__name__, detail=str(exc))

    warnings: list[str] = []
    data: dict[str, Any] = _safe_current_context()
    entity_types = {
        "parts": "ANSAPART",
        "faces": "FACE",
        "macros": "MACRO",
        "cons": "CONS",
        "hot_points": "HOT POINT",
        "points": "POINT",
        "curves": "CURVE",
        "surfaces": "SURFACE",
        "volumes": "VOLUME",
        "nodes": "NODE",
        "shells": "SHELL",
        "solids": "SOLID",
        "elements": "ELEMENT",
        "materials": "MATERIAL",
        "properties": "PROPERTY",
    }
    counts: dict[str, int | None] = {}
    entity_type_map: dict[str, str] = {}
    for label, entity_type in entity_types.items():
        count, warning = _collect_count(deck, entity_type)
        counts[label] = count
        entity_type_map[label] = entity_type
        if warning:
            warnings.append(warning)
    data["entity_counts"] = counts
    data["entity_type_map"] = entity_type_map
    if counts.get("faces") == 0:
        warnings.append("No FACE entities found. Check macros/cons/curves/surfaces or STEP translator behavior.")
    return success_response(message="geometry inventory", data=data, warnings=warnings)


def handle_import_file(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        input_path = _path_from_command(command, "input_path")
        mode = str(command.get("mode") or "open").lower()
        before = _model_entity_counts()

        if mode not in {"open", "import"}:
            raise ValueError("mode must be 'open' or 'import'")

        # ANSA's base.Open handles ANSA databases and many geometry/solver input formats
        # through the normal File > Open path.
        ret = base.Open(str(input_path))
        after = _model_entity_counts()
        data = {
            "input_path": str(input_path),
            "mode": mode,
            "return_code": ret,
            "before": before,
            "after": after,
        }
        if ret == 0:
            return success_response(message="file imported", data=data)
        return error_response("file import/open failed", error_type="AnsaImportError", detail=str(data), logs={"data": data})
    except Exception as exc:
        return error_response(
            "file import/open failed",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


def _entity_basic_info(deck: Any, entity: Any, include_card_values: bool = False) -> dict[str, Any]:
    info = {
        "id": getattr(entity, "_id", None),
        "name": getattr(entity, "_name", ""),
        "type": None,
    }
    try:
        info["type"] = entity.ansa_type(deck)
    except Exception:
        info["type"] = "FACE"
    if include_card_values:
        try:
            values = base.GetEntityCardValues(deck, entity, ("Name", "ID", "PID", "Part", "Module Id"))
            info["card_values"] = values
        except Exception as exc:
            info["card_values_error"] = str(exc)
    return info


def _float_list(value: Any) -> list[float] | None:
    try:
        items = list(value)
    except Exception:
        return None
    if len(items) < 3:
        return None
    try:
        return [float(items[0]), float(items[1]), float(items[2])]
    except Exception:
        return None


def _bbox_info(value: Any) -> dict[str, Any] | None:
    try:
        items = [float(item) for item in list(value)]
    except Exception:
        return None
    if len(items) != 6:
        return None
    min_point = items[:3]
    max_point = items[3:]
    return {
        "min": min_point,
        "max": max_point,
        "size": [max_point[index] - min_point[index] for index in range(3)],
    }


def _safe_call_value(func: Any, *args: Any) -> tuple[Any, str | None]:
    try:
        return func(*args), None
    except Exception as exc:
        return None, str(exc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return repr(value)


def _face_geometry_info(deck: Any, face: Any) -> dict[str, Any]:
    info = _entity_basic_info(deck, face, include_card_values=True)
    warnings: list[str] = []

    area, area_error = _safe_call_value(base.GetFaceArea, face)
    if area_error:
        warnings.append(f"GetFaceArea: {area_error}")
        info["area"] = None
    else:
        try:
            info["area"] = float(area)
        except Exception:
            info["area"] = None
            warnings.append(f"GetFaceArea returned non-numeric value: {area!r}")

    center, center_error = _safe_call_value(base.EntityCenter, face)
    if center_error:
        warnings.append(f"EntityCenter: {center_error}")
        info["center"] = None
    else:
        info["center"] = _float_list(center)
        if info["center"] is None:
            warnings.append(f"EntityCenter returned invalid value: {center!r}")

    bbox, bbox_error = _safe_call_value(base.BoundBox, face)
    if bbox_error:
        warnings.append(f"BoundBox: {bbox_error}")
        info["bbox"] = None
    else:
        info["bbox"] = _bbox_info(bbox)
        if info["bbox"] is None:
            warnings.append(f"BoundBox returned invalid value: {bbox!r}")

    normal, normal_error = _safe_call_value(base.GetFaceOrientation, face)
    if normal_error:
        warnings.append(f"GetFaceOrientation: {normal_error}")
        info["normal"] = None
    else:
        info["normal"] = _float_list(normal)
        if info["normal"] is None:
            warnings.append(f"GetFaceOrientation returned invalid value: {normal!r}")

    surface_info, surface_error = _safe_call_value(base.SurfaceInfo, face)
    if surface_error:
        warnings.append(f"SurfaceInfo: {surface_error}")
    else:
        info["surface_info"] = _json_safe(surface_info)

    perimeters, perimeters_error = _safe_call_value(base.PerimetersOfFace, face)
    if perimeters_error:
        warnings.append(f"PerimetersOfFace: {perimeters_error}")
        info["perimeter_count"] = None
    else:
        try:
            info["perimeter_count"] = len(perimeters)
        except Exception:
            info["perimeter_count"] = None

    if warnings:
        info["property_warnings"] = warnings
    return info


def _face_sort_value(face_info: dict[str, Any], sort_by: str) -> Any:
    if sort_by == "id":
        return face_info.get("id")
    if sort_by == "area":
        return face_info.get("area")

    vector_keys = {
        "center_x": ("center", 0),
        "center_y": ("center", 1),
        "center_z": ("center", 2),
        "min_x": ("bbox", "min", 0),
        "min_y": ("bbox", "min", 1),
        "min_z": ("bbox", "min", 2),
        "max_x": ("bbox", "max", 0),
        "max_y": ("bbox", "max", 1),
        "max_z": ("bbox", "max", 2),
        "size_x": ("bbox", "size", 0),
        "size_y": ("bbox", "size", 1),
        "size_z": ("bbox", "size", 2),
    }
    path = vector_keys.get(sort_by)
    if not path:
        return None
    try:
        if len(path) == 2:
            key, index = path
            values = face_info.get(key)
        else:
            key, subkey, index = path
            values = (face_info.get(key) or {}).get(subkey)
        return values[index] if values is not None else None
    except Exception:
        return None


def _model_entity_counts() -> dict[str, int | None]:
    if not ANSA_AVAILABLE:
        return {}
    try:
        deck = base.CurrentDeck()
    except Exception:
        deck = 0
    entity_types = {
        "parts": "ANSAPART",
        "faces": "FACE",
        "nodes": "NODE",
        "shells": "SHELL",
        "solids": "SOLID",
        "elements": "ELEMENT",
    }
    counts: dict[str, int | None] = {}
    for label, entity_type in entity_types.items():
        count, _warning = _collect_count(deck, entity_type)
        counts[label] = count
    return counts


def handle_list_faces(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        limit = int(command.get("limit", 100))
        include_card_values = bool(command.get("include_card_values", False))
        if limit < 1:
            raise ValueError("limit must be >= 1")
        deck = base.CurrentDeck()
        faces = base.CollectEntities(deck, None, "FACE", recursive=True)
        listed = [_entity_basic_info(deck, face, include_card_values) for face in faces[:limit]]
        data = {
            "current_deck": str(deck),
            "total_faces": len(faces),
            "returned": len(listed),
            "faces": listed,
        }
        return success_response(message="faces listed", data=data)
    except Exception as exc:
        return error_response(
            "failed to list faces",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


def handle_face_properties(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        limit = int(command.get("limit", 100))
        sort_by = str(command.get("sort_by") or "id")
        descending = bool(command.get("descending", False))
        raw_ids = command.get("face_ids")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        deck = base.CurrentDeck()
        missing: list[int] = []
        if isinstance(raw_ids, list) and raw_ids:
            faces = []
            for raw_id in raw_ids:
                face_id = int(raw_id)
                face = base.GetEntity(deck, "FACE", face_id)
                if face:
                    faces.append(face)
                else:
                    missing.append(face_id)
            total_faces = len(faces)
        else:
            faces = base.CollectEntities(deck, None, "FACE", recursive=True)
            total_faces = len(faces)

        properties = [_face_geometry_info(deck, face) for face in faces]
        properties_with_value = [item for item in properties if _face_sort_value(item, sort_by) is not None]
        properties_without_value = [item for item in properties if _face_sort_value(item, sort_by) is None]
        properties_with_value.sort(
            key=lambda item: (_face_sort_value(item, sort_by), item.get("id") or 0),
            reverse=descending,
        )
        properties = properties_with_value + properties_without_value
        listed = properties[:limit]
        data = {
            "current_deck": str(deck),
            "total_faces": total_faces,
            "returned": len(listed),
            "sort_by": sort_by,
            "descending": descending,
            "missing_face_ids": missing,
            "faces": listed,
        }
        return success_response(message="face properties listed", data=data)
    except Exception as exc:
        return error_response(
            "failed to list face properties",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


def handle_delete_faces(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        raw_ids = command.get("face_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("face_ids must be a non-empty list")
        face_ids = [int(face_id) for face_id in raw_ids]
        force = bool(command.get("force", True))
        deck = base.CurrentDeck()
        before = _model_entity_counts()

        found = []
        found_ids = []
        missing = []
        for face_id in face_ids:
            face = base.GetEntity(deck, "FACE", face_id)
            if face:
                found.append(face)
                found_ids.append(face_id)
            else:
                missing.append(face_id)

        if not found:
            return error_response(
                "no requested faces were found",
                error_type="NotFound",
                detail=str({"face_ids": face_ids, "missing": missing}),
                logs={"before": before},
            )

        delete_ret = None
        delete_method = "DeleteFaces"
        try:
            delete_ret = base.DeleteFaces(found)
        except Exception:
            delete_method = "DeleteEntity"
            delete_ret = base.DeleteEntity(found, force=force)

        after = _model_entity_counts()
        artifacts: list[str] = []
        save_result: dict[str, Any] | None = None
        save_as = command.get("save_as")
        if isinstance(save_as, str) and save_as:
            save_path = _path_from_command(command, "save_as")
            save_ret = base.SaveAs(str(save_path), silent=True)
            exists = save_path.exists()
            save_result = {
                "output_path": str(save_path),
                "return_code": save_ret,
                "exists": exists,
                "size": save_path.stat().st_size if exists else 0,
            }
            if exists:
                artifacts.append(str(save_path))

        data = {
            "requested_face_ids": face_ids,
            "deleted_face_ids": found_ids,
            "missing_face_ids": missing,
            "delete_method": delete_method,
            "delete_return_code": delete_ret,
            "before": before,
            "after": after,
            "save_as": save_result,
        }
        return success_response(message="faces deleted", data=data, artifacts=artifacts)
    except Exception as exc:
        return error_response(
            "failed to delete faces",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


def _path_from_command(command: dict[str, Any], key: str) -> Path:
    value = command.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        path = CONFIG.home / path
    resolved = path.resolve(strict=False)
    base_dir = CONFIG.home.resolve(strict=False)
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"{key} is outside ANSA_MCP_HOME: {resolved}") from exc
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def handle_save_model_as_ws(command: dict[str, Any]) -> dict[str, Any]:
    """Workspace-validated save-as.

    This function used to be *also* named ``handle_save_model_as`` — the later
    definition silently shadowed it, so both HANDLERS keys ended up pointing at
    the same implementation and a relative path was resolved against ANSA's CWD
    instead of ANSA_MCP_HOME.
    """
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        output_path = _path_from_command(command, "output_path")
        version = command.get("version")
        silent = bool(command.get("silent", True))
        if version:
            ret = base.SaveAs(str(output_path), str(version), silent)
        else:
            ret = base.SaveAs(str(output_path), silent=silent)
        exists = output_path.exists()
        data = {
            "output_path": str(output_path),
            "return_code": ret,
            "exists": exists,
            "size": output_path.stat().st_size if exists else 0,
        }
        if ret == 0 and exists:
            return success_response(message="model saved", data=data, artifacts=[str(output_path)])
        return error_response("model save failed", error_type="AnsaSaveError", detail=str(data), logs={"data": data})
    except Exception as exc:
        return error_response(
            "model save failed",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


def _safe_len(value: Any) -> int | None:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return len(value)
    except Exception:
        return None


def handle_check_mesh_quality(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    warnings: list[str] = []
    data: dict[str, Any] = _safe_current_context()
    try:
        deck = base.CurrentDeck()
    except Exception as exc:
        return error_response("failed to read current deck", error_type=type(exc).__name__, detail=str(exc))

    entity_types = {
        "nodes": "NODE",
        "shells": "SHELL",
        "solids": "SOLID",
        "elements": "ELEMENT",
    }
    counts: dict[str, int | None] = {}
    for label, entity_type in entity_types.items():
        count, warning = _collect_count(deck, entity_type)
        counts[label] = count
        if warning:
            warnings.append(warning)
    data["entity_counts"] = counts

    failed_count = 0
    checks: dict[str, Any] = {}

    if bool(command.get("include_free_nodes", True)):
        try:
            free_nodes = base.CheckFree("visible" if command.get("check_visible") else "all")
            count = _safe_len(free_nodes)
            checks["free_nodes"] = {"count": count, "raw_type": type(free_nodes).__name__}
            failed_count += count or 0
        except Exception as exc:
            warnings.append(f"CheckFree failed: {exc}")

    if bool(command.get("include_intersections", True)):
        try:
            intersections = base.CheckIntersections(
                bool(command.get("check_visible", False)),
                bool(command.get("fast_run", False)),
                False,
            )
            count = 1 if intersections == 1 else _safe_len(intersections)
            checks["intersections"] = {"count": count, "raw_type": type(intersections).__name__}
            failed_count += count or 0
        except Exception as exc:
            warnings.append(f"CheckIntersections failed: {exc}")

    data["checks"] = checks
    data["failed_count"] = failed_count
    return success_response(message="mesh quality checked", data=data, warnings=warnings)


def _positive_float_arg(value: Any, name: str) -> float:
    try:
        result = float(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


def _normalized_shell_mesh_type(value: Any) -> str:
    if not isinstance(value, str):
        return "auto"
    text = value.strip().lower().replace("_", "-").replace(" ", "-")
    return {
        "default": "auto",
        "tri": "tria",
        "triangle": "tria",
        "triangular": "tria",
        "quad": "quad",
        "quadrilateral": "quad",
        "mixed": "mixed",
        "hybrid": "mixed",
    }.get(text, text or "auto")


def _surface_mesh_faces(deck: Any, command: dict[str, Any]) -> tuple[list[Any], list[int]]:
    raw_ids = command.get("face_ids")
    if isinstance(raw_ids, list) and raw_ids:
        faces: list[Any] = []
        missing: list[int] = []
        for raw_id in raw_ids:
            face_id = int(raw_id)
            face = base.GetEntity(deck, "FACE", face_id)
            if face:
                faces.append(face)
            else:
                missing.append(face_id)
        return faces, missing

    target = str(command.get("target") or "all_surfaces").strip().lower()
    if target not in {"", "all", "all_surfaces", "visible", "current_model", "model"}:
        raise ValueError("surface_mesh currently supports all_surfaces/visible or explicit face_ids only")
    faces = base.CollectEntities(deck, None, "FACE", recursive=True)
    return list(faces), []


def _delete_existing_shell_mesh(deck: Any) -> dict[str, int | None]:
    before = {
        "nodes": _collect_count(deck, "NODE")[0],
        "shells": _collect_count(deck, "SHELL")[0],
        "elements": _collect_count(deck, "ELEMENT")[0],
    }
    try:
        shells = base.CollectEntities(deck, None, "SHELL", recursive=True)
        nodes = base.CollectEntities(deck, None, "NODE", recursive=True)
        if shells:
            base.DeleteEntity(shells)
        if nodes:
            base.DeleteEntity(nodes)
    except Exception:
        pass
    return before


def handle_surface_mesh(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        from ansa import mesh  # type: ignore

        deck = base.CurrentDeck()
        before = _model_entity_counts()
        faces, missing_face_ids = _surface_mesh_faces(deck, command)
        if not faces:
            return error_response(
                "no FACE entities found for surface mesh",
                error_type="NotFound",
                detail=str({"missing_face_ids": missing_face_ids}),
                logs={"before": before},
            )

        element_size = _positive_float_arg(command.get("element_size"), "element_size")
        mesh_type = _normalized_shell_mesh_type(command.get("mesh_type"))
        erase_existing = bool(command.get("erase_existing", True))
        erased_counts = _delete_existing_shell_mesh(deck) if erase_existing else {}

        ret_target_length = mesh.SetMeshParamTargetLength("absolute", element_size)
        defaults_payload = {
            "mesh_change_option": "mesh",
            "existing_mesh_treatment": "erase" if erase_existing else "keep",
            "perimeter_length": str(element_size),
        }
        if mesh_type in {"tria", "quad", "mixed"}:
            defaults_payload["element_type"] = mesh_type
        try:
            base.SetANSAdefaultsValues(defaults_payload)
        except Exception:
            pass

        mesh_return = mesh.Mesh(faces)
        after = _model_entity_counts()
        shell_count = after.get("shells") or 0
        element_count = after.get("elements") or 0
        node_count = after.get("nodes") or 0
        data = {
            "target": command.get("target") or "all_surfaces",
            "face_count": len(faces),
            "missing_face_ids": missing_face_ids,
            "element_size": element_size,
            "mesh_type": mesh_type,
            "erase_existing": erase_existing,
            "set_target_length_return": ret_target_length,
            "mesh_return": mesh_return,
            "before": before,
            "erased_counts": erased_counts,
            "after": after,
            "shell_count": shell_count,
            "node_count": node_count,
            "element_count": element_count,
        }
        if mesh_return == 0 or (shell_count <= 0 and element_count <= 0):
            return error_response("surface mesh failed", error_type="AnsaMeshError", detail=str(data), logs={"data": data})
        return success_response(message="surface mesh generated", data=data)
    except Exception as exc:
        return error_response(
            "surface mesh failed",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


def _normalized_solver(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("solver must be a non-empty string")
    return value.strip().upper().replace("-", "").replace("_", "")


def _call_output_solver(solver: str, output_path: Path, mode: str, options: dict[str, Any]) -> int:
    if solver in {"NASTRAN", "BDF"}:
        return base.OutputNastran(filename=str(output_path), mode=mode, **options)
    if solver in {"ABAQUS", "INP"}:
        return base.OutputAbaqus(filename=str(output_path), mode=mode, **options)
    if solver in {"LSDYNA", "DYNA", "K"}:
        return base.OutputLSDyna(filename=str(output_path), mode=mode, **options)
    if solver in {"ANSYS", "CDB"}:
        return base.OutputAnsys(filename=str(output_path), mode=mode, **options)
    raise ValueError(f"unsupported solver: {solver}")


def handle_export_solver_deck(command: dict[str, Any]) -> dict[str, Any]:
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")

    try:
        solver = _normalized_solver(command.get("solver"))
        output_path = _path_from_command(command, "output_path")
        mode = str(command.get("mode") or "all")
        options = command.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("options must be an object")

        ret = _call_output_solver(solver, output_path, mode, options)
        exists = output_path.exists()
        data = {
            "solver": solver,
            "output_path": str(output_path),
            "mode": mode,
            "return_code": ret,
            "exists": exists,
            "size": output_path.stat().st_size if exists else 0,
        }
        if exists and data["size"] > 0:
            warnings = []
            # NOTE: the Output* family is the odd one out — it returns 1 on
            # success and 0 on failure (base.Save/SaveAs are the other way
            # round). The old check was `ret != 0`, so every *successful* export
            # came back with a warning attached.
            if ret == 0:
                warnings.append(
                    "ANSA output function returned 0 (failure per docs) although "
                    "a non-empty deck was written — check the ANSA message window"
                )
            return success_response(
                message="solver deck exported",
                data=data,
                artifacts=[str(output_path)],
                warnings=warnings,
            )
        return error_response("solver deck export failed", error_type="AnsaExportError", detail=str(data), logs={"data": data})
    except Exception as exc:
        return error_response(
            "solver deck export failed",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )


#: Keys whose presence in a tool result means the tool FAILED.
#: The migrated tools signal failure through their *return value* — e.g.
#: ``{"opened": rc == 0, "error": "..."}`` — while ``_call_tool`` used to look
#: only for raised exceptions. Every genuine failure therefore reached the MCP
#: client as ``ok: true``, which is actively harmful when an LLM is deciding
#: what to do next. An empty string does not count as a failure.
FAILURE_KEYS = ("error", "errors")


def _result_failed(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, ""
    if data.get("ok") is False:
        return True, str(data.get("error") or data.get("message") or "tool returned ok=False")
    for key in FAILURE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return True, value
        if isinstance(value, (list, dict, tuple)) and value:
            return True, f"{key}={value}"
    return False, ""


def _call_tool(fn, command: dict[str, Any], name: str) -> dict[str, Any]:
    """Run a tools_impl function and wrap its data dict into a response.

    A returned dict counts as success **only if it does not report failure**;
    see :func:`_result_failed`.
    """
    if not ANSA_AVAILABLE:
        return error_response("ANSA API is not available", error_type="AnsaUnavailable")
    try:
        data = fn(command)
    except ansa_api.ApiMissing as exc:
        # A missing API is a *build capability* problem, not a bad request —
        # report it as its own error type so the client can adapt (or stop
        # calling this tool) instead of blindly retrying.
        return error_response(
            f"{name} failed: required ANSA API is missing in this build",
            error_type="ApiMissing",
            detail=str(exc),
            logs={"api_path": exc.path, "hint": exc.hint},
        )
    except Exception as exc:
        return error_response(
            f"{name} failed",
            error_type=type(exc).__name__,
            detail=str(exc),
            logs={"traceback": traceback.format_exc()},
        )
    payload = tools_impl._jsonable(data)
    failed, detail = _result_failed(data)
    if failed:
        return error_response(
            f"{name} failed",
            error_type="AnsaToolError",
            detail=detail,
            logs={"result": payload},
        )
    return success_response(message=f"{name} ok", data=payload)


# ---- 49 tools migrated from ansa-tcp-bridge (Route B) ----
def handle_ping_ansa(command):
    return handle_ping(command)


def handle_open_model(command):
    return _call_tool(tools_impl.open_model, command, "open_model")


def handle_new_model(command):
    return _call_tool(tools_impl.new_model, command, "new_model")


def handle_save_model(command):
    return _call_tool(tools_impl.save_model, command, "save_model")


def handle_save_model_as(command):
    # tcp-bridge flavour: takes an absolute 'path'
    return _call_tool(tools_impl.save_model_as, command, "save_model_as")


def handle_export_nastran(command):
    return _call_tool(tools_impl.export_nastran, command, "export_nastran")


def handle_export_lsdyna(command):
    return _call_tool(tools_impl.export_lsdyna, command, "export_lsdyna")


def handle_export_step(command):
    return _call_tool(tools_impl.export_step, command, "export_step")


def handle_run_python_script_in_ansa(command):
    return _call_tool(tools_impl.run_python_script_in_ansa, command, "run_python_script_in_ansa")


def handle_count_entities(command):
    return _call_tool(tools_impl.count_entities, command, "count_entities")


def handle_list_entities(command):
    return _call_tool(tools_impl.list_entities, command, "list_entities")


def handle_get_entity(command):
    return _call_tool(tools_impl.get_entity, command, "get_entity")


def handle_set_entity_fields(command):
    return _call_tool(tools_impl.set_entity_fields, command, "set_entity_fields")


def handle_create_entity(command):
    return _call_tool(tools_impl.create_entity, command, "create_entity")


def handle_delete_entities(command):
    return _call_tool(tools_impl.delete_entities, command, "delete_entities")


def handle_search_entities_by_name(command):
    return _call_tool(tools_impl.search_entities_by_name, command, "search_entities_by_name")


def handle_get_bounding_box(command):
    return _call_tool(tools_impl.get_bounding_box, command, "get_bounding_box")


def handle_get_node_coordinates(command):
    return _call_tool(tools_impl.get_node_coordinates, command, "get_node_coordinates")


def handle_change_element_type(command):
    return _call_tool(tools_impl.change_element_type, command, "change_element_type")


def handle_create_part(command):
    return _call_tool(tools_impl.create_part, command, "create_part")


def handle_create_set(command):
    return _call_tool(tools_impl.create_set, command, "create_set")


def handle_add_to_set(command):
    return _call_tool(tools_impl.add_to_set, command, "add_to_set")


def handle_get_model_summary(command):
    return _call_tool(tools_impl.get_model_summary, command, "get_model_summary")


def handle_list_model_includes(command):
    return _call_tool(tools_impl.list_model_includes, command, "list_model_includes")


def handle_calc_element_mass(command):
    return _call_tool(tools_impl.calc_element_mass, command, "calc_element_mass")


def handle_calc_shell_area(command):
    return _call_tool(tools_impl.calc_shell_area, command, "calc_shell_area")


def handle_calc_solid_volume(command):
    return _call_tool(tools_impl.calc_solid_volume, command, "calc_solid_volume")


def handle_check_intersections(command):
    return _call_tool(tools_impl.check_intersections, command, "check_intersections")


def handle_check_penetrations(command):
    return _call_tool(tools_impl.check_penetrations, command, "check_penetrations")


def handle_check_free_nodes(command):
    return _call_tool(tools_impl.check_free_nodes, command, "check_free_nodes")


def handle_run_quality_check(command):
    return _call_tool(tools_impl.run_quality_check, command, "run_quality_check")


def handle_count_failed_elements(command):
    return _call_tool(tools_impl.count_failed_elements, command, "count_failed_elements")


def handle_check_geometry(command):
    return _call_tool(tools_impl.check_geometry, command, "check_geometry")


def handle_check_sharp_edges(command):
    return _call_tool(tools_impl.check_sharp_edges, command, "check_sharp_edges")


def handle_check_rigid_dependencies(command):
    return _call_tool(tools_impl.check_rigid_dependencies, command, "check_rigid_dependencies")


def handle_calc_mesh_quality(command):
    return _call_tool(tools_impl.calc_mesh_quality, command, "calc_mesh_quality")


def handle_mesh_shells(command):
    return _call_tool(tools_impl.mesh_shells, command, "mesh_shells")


def handle_mesh_volume(command):
    return _call_tool(tools_impl.mesh_volume, command, "mesh_volume")


def handle_set_shell_mesh_params(command):
    return _call_tool(tools_impl.set_shell_mesh_params, command, "set_shell_mesh_params")


def handle_delete_mesh(command):
    return _call_tool(tools_impl.delete_mesh, command, "delete_mesh")


def handle_run_batch_mesh(command):
    return _call_tool(tools_impl.run_batch_mesh, command, "run_batch_mesh")


def handle_apply_connectors(command):
    return _call_tool(tools_impl.apply_connectors, command, "apply_connectors")


def handle_check_connections(command):
    return _call_tool(tools_impl.check_connections, command, "check_connections")


def handle_list_connectors(command):
    return _call_tool(tools_impl.list_connectors, command, "list_connectors")


def handle_create_connection_point(command):
    """Create a spotweld/bolt/gumdrop/rivet/screw connection point.

    New in this revision: backed by the documented
    ``connections.CreateConnectionPoint``. This is the missing primitive for
    creating **Bolt** connections from an automated detection pass.
    """
    return _call_tool(tools_impl.create_connection_point, command,
                      "create_connection_point")


def handle_show_only(command):
    return _call_tool(tools_impl.show_only, command, "show_only")


def handle_show_also(command):
    return _call_tool(tools_impl.show_also, command, "show_also")


def handle_hide(command):
    return _call_tool(tools_impl.hide, command, "hide")


def handle_near(command):
    return _call_tool(tools_impl.near, command, "near")


def handle_neighb(command):
    return _call_tool(tools_impl.neighb, command, "neighb")


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    # ---- original ansa-mcp handlers ----
    "ping": handle_ping,
    "execute_script": handle_execute_script,
    "validate_script": handle_validate_script,
    "get_capabilities": handle_get_capabilities,
    "get_model_info": handle_get_model_info,
    "geometry_inventory": handle_geometry_inventory,
    "import_file": handle_import_file,
    "list_faces": handle_list_faces,
    "face_properties": handle_face_properties,
    "delete_faces": handle_delete_faces,
    "save_model_as_ws": handle_save_model_as_ws,  # workspace-validated variant
    "save_model_as": handle_save_model_as,        # delegated to tools_impl (accepts path/filepath/output_path)
    "check_mesh_quality": handle_check_mesh_quality,
    "surface_mesh": handle_surface_mesh,
    "export_solver_deck": handle_export_solver_deck,
    # ---- 49 tools migrated from ansa-tcp-bridge (Route B) ----
    "ping_ansa": handle_ping_ansa,
    "open_model": handle_open_model,
    "new_model": handle_new_model,
    "save_model": handle_save_model,
    # NOTE: "save_model_as" is already registered above (original ansa-mcp block).
    # A second literal key here was silently shadowed -- dict literals keep the
    # last one, so the duplicate was dead code and a trap for future edits.
    "export_nastran": handle_export_nastran,
    "export_lsdyna": handle_export_lsdyna,
    "export_step": handle_export_step,
    "run_python_script_in_ansa": handle_run_python_script_in_ansa,
    "count_entities": handle_count_entities,
    "list_entities": handle_list_entities,
    "get_entity": handle_get_entity,
    "set_entity_fields": handle_set_entity_fields,
    "create_entity": handle_create_entity,
    "delete_entities": handle_delete_entities,
    "search_entities_by_name": handle_search_entities_by_name,
    "get_bounding_box": handle_get_bounding_box,
    "get_node_coordinates": handle_get_node_coordinates,
    "change_element_type": handle_change_element_type,
    "create_part": handle_create_part,
    "create_set": handle_create_set,
    "add_to_set": handle_add_to_set,
    "get_model_summary": handle_get_model_summary,
    "list_model_includes": handle_list_model_includes,
    "calc_element_mass": handle_calc_element_mass,
    "calc_shell_area": handle_calc_shell_area,
    "calc_solid_volume": handle_calc_solid_volume,
    "check_intersections": handle_check_intersections,
    "check_penetrations": handle_check_penetrations,
    "check_free_nodes": handle_check_free_nodes,
    "run_quality_check": handle_run_quality_check,
    "count_failed_elements": handle_count_failed_elements,
    "check_geometry": handle_check_geometry,
    "check_sharp_edges": handle_check_sharp_edges,
    "check_rigid_dependencies": handle_check_rigid_dependencies,
    "calc_mesh_quality": handle_calc_mesh_quality,
    "mesh_shells": handle_mesh_shells,
    "mesh_volume": handle_mesh_volume,
    "set_shell_mesh_params": handle_set_shell_mesh_params,
    "delete_mesh": handle_delete_mesh,
    "run_batch_mesh": handle_run_batch_mesh,
    "apply_connectors": handle_apply_connectors,
    "check_connections": handle_check_connections,
    "list_connectors": handle_list_connectors,
    "create_connection_point": handle_create_connection_point,
    "show_only": handle_show_only,
    "show_also": handle_show_also,
    "hide": handle_hide,
    "near": handle_near,
    "neighb": handle_neighb,
}


def ansa_mcp_process_one() -> bool:
    CONFIG.ensure_dirs()
    dropped = cleanup_stale_commands()
    command_files = sorted(CONFIG.commands_dir.glob("cmd_*.json"))
    if not command_files:
        BRIDGE.update(bridge_state="idle", current_command_id=None,
                      current_command_type=None)
        write_idle_status("idle")
        return False

    # Claim before reading: this is what makes re-entrancy impossible. The queue
    # file must never be visible as "pending" while it is being executed, because
    # nothing else guards against a second caller — the _busy guard lives in the
    # autoload script, which a hot reload re-executes (README §10.21).
    command_path = _claim_command(command_files[0])
    if command_path is None:
        BRIDGE.update(bridge_state="idle", current_command_id=None,
                      current_command_type=None)
        write_idle_status("idle (command already claimed)")
        return False

    command: dict[str, Any] = {}
    started_at = time.time()
    #: Captured before anything runs. The pair (model_before, model_after) is
    #: the one fact only the ANSA side can supply: whether this command opened
    #: or saved over a different file than the one it started on.
    model_before, model_snapshot_error = _model_snapshot()
    outcome_ok: bool | None = None
    outcome_error: str | None = None
    BRIDGE.update(bridge_state="executing")
    try:
        command = read_json(command_path)
        BRIDGE.update(current_command_id=command.get("id"),
                      current_command_type=command.get("type"))
        write_status("running", f"executing {command.get('type')}")
        handler = HANDLERS.get(str(command.get("type", "")))
        if handler is None:
            result = error_response(
                f"unknown command type: {command.get('type')}",
                error_type="UnknownCommand",
                logs={"known_types": len(HANDLERS)},
            )
        else:
            result = handler(command)
        _write_result(command, result)
        outcome_ok = bool(result.get("ok"))
        if not outcome_ok:
            outcome_error = str((result.get("error") or {}).get("type")
                                or "CommandFailed")
        BRIDGE["last_command_type"] = command.get("type")
        BRIDGE["last_command_result"] = "ok" if outcome_ok else "error"
        BRIDGE["processed_count"] = BRIDGE.get("processed_count", 0) + 1
        if not outcome_ok:
            BRIDGE["last_error"] = str((result.get("error") or {}).get("detail")
                                       or result.get("message"))[:500]
    except Exception as exc:
        outcome_ok = False
        outcome_error = type(exc).__name__
        BRIDGE["last_command_result"] = "error"
        BRIDGE["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        if command:
            _write_result(
                command,
                error_response(
                    "failed to process command",
                    error_type=type(exc).__name__,
                    detail=str(exc),
                    logs={"traceback": traceback.format_exc()},
                ),
            )
    finally:
        # Audit before the queue file is removed: if the process dies inside
        # this command, the entry is already on disk and the surviving
        # commands/cmd_<id>.json identifies what was running (README §7.6 —
        # that crash left no record anywhere else).
        _audit(audit.record_command(
            command=command,
            ok=outcome_ok,
            duration_s=time.time() - started_at,
            error_type=outcome_error,
            model_before=model_before,
            model_after=_model_snapshot()[0],
            model_snapshot_error=model_snapshot_error,
        ))
        try:
            command_path.unlink(missing_ok=True)
        except Exception:
            pass
        BRIDGE.update(bridge_state="idle", current_command_id=None,
                      current_command_type=None)
        if dropped:
            write_status("running", f"idle (dropped {dropped} stale)")
    return True


def ansa_mcp_loop(poll_interval: float = 0.1) -> None:
    CONFIG.ensure_dirs()
    if CONFIG.stop_file.exists():
        CONFIG.stop_file.unlink(missing_ok=True)
    write_status("running", "ANSA MCP plugin loop started")
    # Lifecycle markers make a gap in the audit log mean something: commands
    # logged before a "stopped" line belong to a previous ANSA session.
    _audit(audit.record_bridge(event="started",
                              detail=f"version={__version__} home={CONFIG.home}"))
    print(f"[ansa-mcp] Started. Home: {CONFIG.home}")
    print(f"[ansa-mcp] ANSA API available: {ANSA_AVAILABLE}")
    print(f"[ansa-mcp] Create {CONFIG.stop_file} or call ansa_mcp_stop() to stop.")
    try:
        while not CONFIG.stop_file.exists():
            ansa_mcp_process_one()
            time.sleep(poll_interval)
    finally:
        _audit(audit.record_bridge(
            event="stopped",
            detail=f"processed={BRIDGE.get('processed_count', 0)} "
                   f"dropped={BRIDGE.get('dropped_stale', 0)}"))
        write_status("stopped", "ANSA MCP plugin loop stopped")
        print("[ansa-mcp] Stopped.")


def ansa_mcp_stop() -> None:
    """Stop the bridge **and update status.json**.

    Writing ``stop.flag`` alone left ``status.json`` saying ``"running"``, so the
    server's readiness gate kept passing and every new command waited out the
    full 600s timeout with nothing to explain it. The status write below is what
    actually stops the bleeding; the flag only halts the polling loop.
    """
    CONFIG.ensure_dirs()
    CONFIG.stop_file.touch()
    BRIDGE.update(bridge_state="stopped", current_command_id=None,
                  current_command_type=None)
    write_status("stopped", "stop requested — refusing further commands")
    print(f"[ansa-mcp] Stop signal written: {CONFIG.stop_file}")


if __name__ == "__main__":
    ansa_mcp_loop()
