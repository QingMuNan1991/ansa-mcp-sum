from __future__ import annotations

import inspect
import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import __version__
from . import audit
from . import api_doc
from . import knowledge
from . import memory
from .config import AnsaMcpConfig
from .ipc import (
    atomic_write_json,
    command_path,
    count_queue_depth,
    ensure_under_workspace,
    error_response,
    make_run_dir,
    new_command_id,
    read_bridge_state,
    read_json,
    result_path,
    success_response,
    utc_timestamp,
    wait_for_result,
)

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - exercised only when MCP is missing
    FastMCP = None  # type: ignore[assignment]

try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except Exception:  # pragma: no cover - mcp without the annotations model
    _ToolAnnotations = None  # type: ignore[assignment]


def _annotations(**hints: bool):
    """Build a ``ToolAnnotations``, or ``None`` when mcp does not provide one.

    ``None`` is FastMCP's own default for the parameter, so an mcp install
    without the model still registers all 70 tools — it just loses the hints.
    Degrading is right here; refusing to start over a metadata field is not.
    """
    if _ToolAnnotations is None:
        return None
    return _ToolAnnotations(**hints)


def _with_pitfalls(*ids: str):
    """Append the named pitfall guidance to a tool's docstring.

    This has to be a decorator, applied *inside* ``@mcp.tool`` (so it runs
    first), rather than the obvious ``\"\"\"doc\"\"\" + pitfall_brief([...])``.
    Python only sets ``__doc__`` when the first statement is a **bare string
    literal**; an expression evaluates fine and is then ignored, leaving the
    tool registered with an empty description. That is how ten tools briefly
    lost their descriptions — a silent failure of exactly the kind
    ``ansa://pitfalls`` exists to warn about.
    """
    def decorate(fn):
        fn.__doc__ = (inspect.getdoc(fn) or "").rstrip() + knowledge.pitfall_brief(list(ids))
        return fn
    return decorate


#: Resolves to FastMCP's ``ToolAnnotations``, or ``None`` when mcp provides none.
#: Reads only. Nothing in the model, and no file, is written.
READ_ONLY = _annotations(readOnlyHint=True, idempotentHint=True)
#: Changes visibility, never the model. Repeatable with the same result.
VIEW_ONLY = _annotations(readOnlyHint=True, idempotentHint=True)
#: Creates or overwrites output; nothing pre-existing is destroyed, and running
#: it twice with the same arguments lands in the same place.
WRITE = _annotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
#: Creates *new* state — running it twice yields two of them.
WRITE_ADDITIVE = _annotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
#: Deletes, replaces or overwrites existing state; cannot be assumed safe.
DESTRUCTIVE = _annotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)


CONFIG = AnsaMcpConfig.from_env()
FACE_SORT_KEYS = {
    "id",
    "area",
    "center_x",
    "center_y",
    "center_z",
    "min_x",
    "min_y",
    "min_z",
    "max_x",
    "max_y",
    "max_z",
    "size_x",
    "size_y",
    "size_z",
}


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _payload_from_tool_result(result_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(result_text)
    except Exception as exc:
        return error_response("tool returned invalid JSON", error_type=type(exc).__name__, detail=str(exc))
    if not isinstance(payload, dict):
        return error_response("tool returned non-object JSON", error_type="ValidationError", detail=str(type(payload).__name__))
    return payload


def _vector_value(item: dict[str, Any], key: str) -> float | None:
    vector_paths = {
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
        "normal_x": ("normal", 0),
        "normal_y": ("normal", 1),
        "normal_z": ("normal", 2),
    }
    path = vector_paths.get(key)
    if not path:
        value = item.get(key)
        try:
            return float(value) if value is not None else None
        except Exception:
            return None
    try:
        if len(path) == 2:
            root, index = path
            values = item.get(root)
        else:
            root, subkey, index = path
            values = (item.get(root) or {}).get(subkey)
        return float(values[index])
    except Exception:
        return None


def _point_distance(center: Any, point: list[float]) -> float | None:
    try:
        values = [float(center[index]) for index in range(3)]
        return sum((values[index] - point[index]) ** 2 for index in range(3)) ** 0.5
    except Exception:
        return None


def _normalized_query(query: str) -> str:
    return query.strip().lower().replace("-", "_").replace(" ", "_")


def read_status() -> dict[str, Any] | None:
    if not CONFIG.status_file.exists():
        return None
    return read_json(CONFIG.status_file)


def status_is_fresh(status: dict[str, Any]) -> bool:
    timestamp = float(status.get("timestamp", 0.0) or 0.0)
    return (time.time() - timestamp) <= CONFIG.heartbeat_stale_seconds


def plugin_ready_error() -> dict[str, Any] | None:
    status = read_status()
    if not status:
        return error_response(
            "ANSA plugin status file was not found",
            error_type="PluginNotRunning",
            detail=f"Expected status file: {CONFIG.status_file}",
        )
    state = str(status.get("status") or "").strip().lower()
    if state != "running":
        bridge_state = status.get("bridge_state") or "unknown"
        return error_response(
            "ANSA plugin is not running",
            error_type="PluginNotRunning",
            detail=(f"status={state or 'unknown'} bridge_state={bridge_state}; "
                    f"the bridge stopped and is refusing commands — click "
                    f"'MCP-Sum / Auto Poll' in ANSA to resume"),
            logs={"status": status},
        )
    if not status_is_fresh(status):
        return error_response(
            "ANSA plugin heartbeat is stale",
            error_type="StaleHeartbeat",
            detail=f"Last heartbeat timestamp: {status.get('timestamp')}",
            logs={"status": status},
        )
    return None


#: Probe version this server knows how to read. A manifest written by an older
#: probe must be regenerated rather than trusted — probe 1.0 used a single
#: import strategy and produced false "unavailable" verdicts for
#: ``ansa.constants`` and ``ansa.base.checks.*``.
EXPECTED_PROBE_VERSION = "2.0"


def load_capabilities(raw: bool = False) -> dict[str, Any]:
    """Read the capability manifest produced by ``probe_ansa_capabilities.py``.

    The tool table in this module is *static*; the real ANSA API surface varies
    by version, licence module and deck. The manifest is how the running build
    tells us what actually exists, so a tool backed by a missing function can be
    hidden from the model instead of failing mid-operation.

    Returns the distilled summary by default (what a caller can act on); pass
    ``raw=True`` for the whole manifest.
    """
    path = CONFIG.home / "capabilities" / "capabilities.json"
    if not path.exists():
        return {
            "available": False,
            "path": str(path),
            "detail": "run probe_ansa_capabilities.py inside ANSA to generate it "
                      "(MCP-Sum > Probe Caps, or ansa.ImportCode(<path>))",
            "action": "probe_missing",
        }
    try:
        manifest = read_json(path)
    except Exception as exc:
        return {"available": False, "path": str(path),
                "detail": f"unreadable: {type(exc).__name__}: {exc}"}
    if raw:
        return {"available": True, "path": str(path), **manifest}

    gaps = manifest.get("capability_gaps") or {}
    try:
        probed_at = path.stat().st_mtime
    except Exception:
        probed_at = None
    probe_version = manifest.get("probe_version")
    return {
        "available": True,
        "path": str(path),
        "probed_at": probed_at,
        "probe_version": probe_version,
        # A 1.0 manifest used the broken single-strategy import and reported
        # phantom gaps (ansa.constants, ansa.base.checks.*). Do not trust it.
        "stale": probe_version != EXPECTED_PROBE_VERSION,
        "deck": manifest.get("deck"),
        "gui_available": (manifest.get("gui") or {}).get("gui_available"),
        "requirements_met": gaps.get("requirements_met"),
        # APIs the shipped tools need that this build does NOT have.
        "missing_functions": gaps.get("missing_functions") or [],
        "unavailable_modules": gaps.get("unavailable_modules") or [],
        "probe_errors": gaps.get("probe_errors") or [],
        # APIs we deliberately avoid; resolving means ansa_api.py is stale.
        "unexpectedly_present": gaps.get("unexpectedly_present") or [],
        "open_questions": gaps.get("open_questions") or {},
        "undo_available": gaps.get("undo_available"),
        "verified_function_count": gaps.get("verified_function_count"),
        "required_function_count": gaps.get("required_function_count"),
        "import_style": manifest.get("import_style") or {},
        "errors": manifest.get("errors") or [],
        "action": (
            "reprobe" if probe_version != EXPECTED_PROBE_VERSION
            else ("ok" if gaps.get("requirements_met") else "review_gaps")
        ),
    }


def send_command(command_type: str, timeout_seconds: float | None = None, **kwargs: Any) -> dict[str, Any]:
    CONFIG.ensure_dirs()
    not_ready = plugin_ready_error()
    if not_ready is not None:
        return not_ready
    command_id = new_command_id()
    timeout = timeout_seconds or CONFIG.timeout_seconds
    run_dir = make_run_dir(CONFIG.home, command_id)
    started_at = time.time()

    command = {
        "id": command_id,
        "type": command_type,
        "timestamp": utc_timestamp(),
        "timeout_seconds": timeout,
        "run_dir": str(run_dir),
        **kwargs,
    }

    atomic_write_json(run_dir / "input.json", command)
    atomic_write_json(command_path(CONFIG.home, command_id), command)

    try:
        result = wait_for_result(result_path(CONFIG.home, command_id), timeout)
    except TimeoutError as exc:
        # A timeout used to be a dead end: the client got neither the command id
        # nor any clue whether the work was still running. It cannot be — ANSA
        # is never told to stop, so the operation may well complete *after* the
        # call returns. Everything needed to reconcile that is reported here,
        # including the message the model should act on before retrying.
        result = error_response(
            "ANSA plugin did not return a result before timeout",
            error_type="Timeout",
            detail=str(exc),
            logs={
                "command_id": command_id,
                "run_dir": str(run_dir),
                "result_path": str(result_path(CONFIG.home, command_id)),
                "command_type": command_type,
                "timeout_seconds": timeout,
                "elapsed_s": round(time.time() - started_at, 1),
                "queue_depth": count_queue_depth(CONFIG.home),
                **read_bridge_state(CONFIG.home),
            },
            warnings=[
                "A timeout does NOT cancel the command inside ANSA. It may still be "
                "running, or it may already have finished after this call returned.",
                f"Before retrying, check results/{command_id}.json — retrying a "
                "command that actually ran applies the change twice.",
            ],
        )

    atomic_write_json(run_dir / "result.json", result)
    return result


def tool_ping() -> str:
    CONFIG.ensure_dirs()
    return to_json(
        success_response(
            message="ansa-mcp-server is running",
            data={
                "server_version": __version__,
                "home": str(CONFIG.home),
                "commands_dir": str(CONFIG.commands_dir),
                "results_dir": str(CONFIG.results_dir),
            },
        )
    )


def tool_check_ansa_connection() -> str:
    CONFIG.ensure_dirs()
    not_ready = plugin_ready_error()
    if not_ready is not None:
        return to_json(not_ready)
    result = send_command("ping", timeout_seconds=min(10.0, CONFIG.timeout_seconds))
    return to_json(result)


def tool_execute_script(script: str, timeout_seconds: float | None = None, validate: bool = True) -> str:
    if len(script) > CONFIG.max_script_chars:
        return to_json(
            error_response(
                "script is too large",
                error_type="ValidationError",
                detail=f"Script length {len(script)} exceeds {CONFIG.max_script_chars}",
            )
        )
    result = send_command("execute_script", timeout_seconds=timeout_seconds, script=script, validate=validate)
    return to_json(result)


# ===========================================================================
# Embedded ANSA API documentation (replica of the ansa-api MCP server).
#
# These let ansa-mcp-sum answer "what is the exact ANSA API for X?" without
# the separate ansa-api server being enabled. The index is shared with
# ansa-api (ansa_tools/ansa_api_index.json), so the two never drift.
# ===========================================================================
def tool_ansa_api_doc_search(query: str, module: str | None = None,
                             category: str | None = None, top_n: int = 5) -> str:
    try:
        text = api_doc.search(query, module=module, category=category, top_n=top_n)
    except Exception as exc:
        return to_json(error_response("ansa api doc search failed", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(success_response(message="ansa api doc search", data={"result": text}))


def tool_ansa_api_doc_lookup(function_name: str, module: str | None = None) -> str:
    try:
        text = api_doc.lookup(function_name, module=module)
    except Exception as exc:
        return to_json(error_response("ansa api doc lookup failed", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(success_response(message="ansa api doc lookup", data={"result": text}))


def tool_ansa_api_doc_modules() -> str:
    try:
        text = api_doc.list_modules()
    except Exception as exc:
        return to_json(error_response("ansa api doc modules failed", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(success_response(message="ansa api doc modules", data={"result": text}))


def tool_ansa_api_doc_categories() -> str:
    try:
        text = api_doc.list_categories()
    except Exception as exc:
        return to_json(error_response("ansa api doc categories failed", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(success_response(message="ansa api doc categories", data={"result": text}))


def tool_validate_script(script: str, timeout_seconds: float | None = None) -> str:
    if len(script) > CONFIG.max_script_chars:
        return to_json(
            error_response(
                "script is too large",
                error_type="ValidationError",
                detail=f"Script length {len(script)} exceeds {CONFIG.max_script_chars}",
            )
        )
    result = send_command("validate_script", timeout_seconds=timeout_seconds, script=script)
    return to_json(result)


def tool_get_capabilities() -> str:
    return to_json(send_command("get_capabilities"))


def tool_get_model_info() -> str:
    return to_json(send_command("get_model_info"))


def tool_geometry_inventory() -> str:
    return to_json(send_command("geometry_inventory"))


def _validated_workspace_path(output_path: str) -> str:
    resolved = ensure_under_workspace(CONFIG.home, output_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def _validated_existing_workspace_path(input_path: str) -> str:
    path = Path(input_path)
    if not path.is_absolute():
        resolved = ensure_under_workspace(CONFIG.home, path)
    else:
        resolved = path.resolve(strict=False)
    if not resolved.exists():
        raise FileNotFoundError(f"input file does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"input path is not a file: {resolved}")

    try:
        resolved.relative_to(CONFIG.home.resolve())
        return str(resolved)
    except ValueError:
        imports_dir = CONFIG.home / "imports"
        imports_dir.mkdir(parents=True, exist_ok=True)
        staged = imports_dir / f"{int(time.time())}-{new_command_id()}-{resolved.name}"
        shutil.copy2(resolved, staged)
        return str(staged)


def tool_import_file(input_path: str, mode: str = "open") -> str:
    try:
        resolved = _validated_existing_workspace_path(input_path)
    except Exception as exc:
        return to_json(error_response("invalid input path", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(send_command("import_file", input_path=resolved, mode=mode))


def tool_list_faces(limit: int = 100, include_card_values: bool = False) -> str:
    if limit < 1:
        return to_json(error_response("limit must be >= 1", error_type="ValidationError"))
    return to_json(send_command("list_faces", limit=limit, include_card_values=include_card_values))


def tool_face_properties(
    face_ids: list[int] | None = None,
    limit: int = 100,
    sort_by: str = "id",
    descending: bool = False,
) -> str:
    if limit < 1:
        return to_json(error_response("limit must be >= 1", error_type="ValidationError"))

    clean_ids: list[int] | None = None
    if face_ids is not None:
        if not isinstance(face_ids, list):
            return to_json(error_response("face_ids must be a list of integers", error_type="ValidationError"))
        clean_ids = []
        for face_id in face_ids:
            try:
                value = int(face_id)
            except Exception:
                return to_json(error_response("face_ids must be integers", error_type="ValidationError", detail=str(face_ids)))
            if value <= 0:
                return to_json(error_response("face_ids must be positive integers", error_type="ValidationError"))
            clean_ids.append(value)

    if sort_by not in FACE_SORT_KEYS:
        return to_json(
            error_response(
                "invalid sort_by",
                error_type="ValidationError",
                detail=f"sort_by must be one of: {', '.join(sorted(FACE_SORT_KEYS))}",
            )
        )
    return to_json(send_command("face_properties", face_ids=clean_ids, limit=limit, sort_by=sort_by, descending=descending))


def _select_faces_from_properties(
    faces: list[dict[str, Any]],
    query: str,
    limit: int,
    point: list[float] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    normalized = _normalized_query(query)
    warnings: list[str] = []
    candidates: list[dict[str, Any]] = []

    def add_candidate(face: dict[str, Any], score: float | None, reason: str) -> None:
        item = dict(face)
        item["selection_score"] = score
        item["selection_reason"] = reason
        candidates.append(item)

    if normalized in {"largest", "largest_face", "max_area", "area_max", "最大面", "面积最大", "最大"}:
        sortable = [(face, _vector_value(face, "area")) for face in faces]
        sortable = [(face, value) for face, value in sortable if value is not None]
        sortable.sort(key=lambda item: (item[1], item[0].get("id") or 0), reverse=True)
        for face, value in sortable[:limit]:
            add_candidate(face, value, f"largest area: {value}")
    elif normalized in {"smallest", "smallest_face", "min_area", "area_min", "最小面", "面积最小", "最小"}:
        sortable = [(face, _vector_value(face, "area")) for face in faces]
        sortable = [(face, value) for face, value in sortable if value is not None]
        sortable.sort(key=lambda item: (item[1], item[0].get("id") or 0))
        for face, value in sortable[:limit]:
            add_candidate(face, value, f"smallest area: {value}")
    elif normalized in {"top", "top_face", "upper", "upper_face", "max_z", "最高面", "顶面", "上表面", "最上面"}:
        sortable = []
        for face in faces:
            max_z = _vector_value(face, "max_z")
            if max_z is None:
                continue
            sortable.append((face, max_z, _vector_value(face, "normal_z") or 0.0, _vector_value(face, "area") or 0.0))
        sortable.sort(key=lambda item: (item[1], item[2], item[3], item[0].get("id") or 0), reverse=True)
        for face, max_z, normal_z, area in sortable[:limit]:
            add_candidate(face, max_z, f"largest max_z: {max_z}; normal_z: {normal_z}; area: {area}")
    elif normalized in {"bottom", "bottom_face", "lower", "lower_face", "min_z", "最低面", "底面", "下表面", "最下面"}:
        sortable = []
        for face in faces:
            min_z = _vector_value(face, "min_z")
            if min_z is None:
                continue
            sortable.append((face, min_z, _vector_value(face, "normal_z") or 0.0, _vector_value(face, "area") or 0.0))
        sortable.sort(key=lambda item: (item[1], item[2], -item[3], item[0].get("id") or 0))
        for face, min_z, normal_z, area in sortable[:limit]:
            add_candidate(face, min_z, f"smallest min_z: {min_z}; normal_z: {normal_z}; area: {area}")
    elif normalized in {"normal_pos_z", "normal_+z", "+z", "positive_z_normal", "法向+z", "正z法向"}:
        sortable = [(face, _vector_value(face, "normal_z")) for face in faces]
        sortable = [(face, value) for face, value in sortable if value is not None]
        sortable.sort(key=lambda item: (item[1], item[0].get("id") or 0), reverse=True)
        for face, value in sortable[:limit]:
            add_candidate(face, value, f"largest normal_z: {value}")
    elif normalized in {"normal_neg_z", "normal_-z", "-z", "negative_z_normal", "法向-z", "负z法向"}:
        sortable = [(face, _vector_value(face, "normal_z")) for face in faces]
        sortable = [(face, value) for face, value in sortable if value is not None]
        sortable.sort(key=lambda item: (item[1], item[0].get("id") or 0))
        for face, value in sortable[:limit]:
            add_candidate(face, value, f"smallest normal_z: {value}")
    elif normalized in {"nearest", "nearest_point", "near_point", "靠近点", "最近"}:
        if point is None:
            warnings.append("point is required for nearest query")
        else:
            sortable = [(face, _point_distance(face.get("center"), point)) for face in faces]
            sortable = [(face, value) for face, value in sortable if value is not None]
            sortable.sort(key=lambda item: (item[1], item[0].get("id") or 0))
            for face, value in sortable[:limit]:
                add_candidate(face, value, f"nearest center distance to {point}: {value}")
    else:
        warnings.append(f"unsupported query: {query}")

    return candidates, warnings


def tool_select_faces_by_query(query: str, limit: int = 5, point: list[float] | None = None) -> str:
    if not isinstance(query, str) or not query.strip():
        return to_json(error_response("query must be a non-empty string", error_type="ValidationError"))
    if limit < 1:
        return to_json(error_response("limit must be >= 1", error_type="ValidationError"))
    clean_point = None
    if point is not None:
        try:
            clean_point = [float(point[index]) for index in range(3)]
        except Exception:
            return to_json(error_response("point must be a 3-number list", error_type="ValidationError", detail=str(point)))

    properties = _payload_from_tool_result(tool_face_properties(limit=10000, sort_by="id", descending=False))
    if not properties.get("ok"):
        return to_json(properties)
    faces = properties.get("data", {}).get("faces", [])
    if not isinstance(faces, list):
        return to_json(error_response("face_properties returned invalid faces", error_type="ValidationError"))

    candidates, warnings = _select_faces_from_properties(faces, query, limit, clean_point)
    return to_json(
        success_response(
            message="faces selected",
            data={
                "query": query,
                "limit": limit,
                "point": clean_point,
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            warnings=warnings,
        )
    )


def tool_preview_selection(query: str, operation: str = "delete_faces", limit: int = 5, point: list[float] | None = None) -> str:
    if operation not in {"delete_faces"}:
        return to_json(error_response("unsupported operation", error_type="ValidationError", detail=operation))
    selected = _payload_from_tool_result(tool_select_faces_by_query(query=query, limit=limit, point=point))
    if not selected.get("ok"):
        return to_json(selected)
    candidates = selected.get("data", {}).get("candidates", [])
    face_ids = [candidate.get("id") for candidate in candidates if candidate.get("id") is not None]
    return to_json(
        success_response(
            message="selection preview",
            data={
                "operation": operation,
                "query": query,
                "would_delete_face_ids": face_ids,
                "requires_confirmation": True,
                "candidates": candidates,
                "recommended_next_step": "Call delete_faces only after the user confirms the face ids.",
            },
            warnings=selected.get("warnings", []),
        )
    )


def tool_delete_faces(face_ids: list[int], force: bool = True, save_as: str | None = None) -> str:
    if not face_ids:
        return to_json(error_response("face_ids must not be empty", error_type="ValidationError"))
    clean_ids: list[int] = []
    for face_id in face_ids:
        try:
            value = int(face_id)
        except Exception:
            return to_json(error_response("face_ids must be integers", error_type="ValidationError", detail=str(face_ids)))
        if value <= 0:
            return to_json(error_response("face_ids must be positive integers", error_type="ValidationError"))
        clean_ids.append(value)

    save_path = None
    if save_as:
        try:
            save_path = _validated_workspace_path(save_as)
        except Exception as exc:
            return to_json(error_response("invalid save_as path", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(send_command("delete_faces", face_ids=clean_ids, force=force, save_as=save_path))


def tool_save_model_as_ws(output_path: str, version: str | None = None, silent: bool = True) -> str:
    """Original ansa-mcp save (workspace-validated). Command type save_model_as_ws."""
    try:
        resolved = _validated_workspace_path(output_path)
    except ValueError as exc:
        return to_json(error_response("invalid output path", error_type="ValidationError", detail=str(exc)))
    return to_json(send_command("save_model_as_ws", output_path=resolved, version=version, silent=silent))


def tool_save_model_as(filepath: str) -> str:
    """tcp-bridge flavour: save to an absolute path (no workspace restriction)."""
    return to_json(send_command("save_model_as", path=filepath))


def tool_autosave_model(label: str = "autosave") -> str:
    """Snapshot the current model into ``ANSA_MCP_HOME/autosaves/``.

    Two bugs fixed here:

    * it called ``tool_save_model_as(output_path, silent=True)``, but that
      function takes a single positional argument — every call raised
      ``TypeError: tool_save_model_as() got an unexpected keyword argument 'silent'``;
    * it used a *relative* path with the non-workspace flavour, which
      ``base.SaveAs`` resolves against ANSA's CWD, not ANSA_MCP_HOME.

    Routing through the workspace-validated save fixes both: the file always
    lands under ANSA_MCP_HOME and the result reports the resolved path.
    """
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_"
                         for ch in (label.strip() or "autosave"))
    rel_path = f"autosaves/{int(time.time())}-{new_command_id()}-{safe_label}.ansa"
    result = json.loads(tool_save_model_as_ws(rel_path))
    if isinstance(result.get("data"), dict):
        result["data"]["label"] = safe_label
    return to_json(result)


def tool_restore_model(input_path: str) -> str:
    try:
        resolved = _validated_existing_workspace_path(input_path)
    except Exception as exc:
        return to_json(error_response("invalid input path", error_type=type(exc).__name__, detail=str(exc)))
    return to_json(send_command("import_file", input_path=resolved, mode="open"))


def tool_check_mesh_quality(
    check_visible: bool = False,
    fast_run: bool = False,
    include_free_nodes: bool = True,
    include_intersections: bool = True,
) -> str:
    return to_json(
        send_command(
            "check_mesh_quality",
            check_visible=check_visible,
            fast_run=fast_run,
            include_free_nodes=include_free_nodes,
            include_intersections=include_intersections,
        )
    )


def _positive_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


def _clean_positive_int_list(values: list[int] | None, name: str) -> list[int] | None:
    if values is None:
        return None
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list of positive integers")
    cleaned: list[int] = []
    for item in values:
        try:
            value = int(item)
        except Exception as exc:
            raise ValueError(f"{name} must contain only positive integers") from exc
        if value <= 0:
            raise ValueError(f"{name} must contain only positive integers")
        cleaned.append(value)
    return cleaned


def tool_surface_mesh(
    element_size: float,
    target: str = "all_surfaces",
    mesh_type: str = "auto",
    erase_existing: bool = True,
    face_ids: list[int] | None = None,
    timeout_seconds: float | None = None,
) -> str:
    try:
        clean_element_size = _positive_float(element_size, "element_size")
        clean_face_ids = _clean_positive_int_list(face_ids, "face_ids")
    except ValueError as exc:
        return to_json(error_response("invalid surface mesh arguments", error_type="ValidationError", detail=str(exc)))
    return to_json(
        send_command(
            "surface_mesh",
            timeout_seconds=timeout_seconds,
            element_size=clean_element_size,
            target=target,
            mesh_type=mesh_type,
            erase_existing=erase_existing,
            face_ids=clean_face_ids,
        )
    )


def tool_export_solver_deck(
    solver: str,
    output_path: str,
    mode: str = "all",
    options: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> str:
    try:
        resolved = _validated_workspace_path(output_path)
    except ValueError as exc:
        return to_json(error_response("invalid output path", error_type="ValidationError", detail=str(exc)))
    return to_json(
        send_command(
            "export_solver_deck",
            timeout_seconds=timeout_seconds,
            solver=solver,
            output_path=resolved,
            mode=mode,
            options=options or {},
        )
    )


def _staged_benchmark_files(root_path: str, max_cases: int) -> tuple[list[Path], str | None]:
    root = Path(root_path)
    if not root.exists():
        return [], f"root path does not exist: {root}"
    if not root.is_dir():
        return [], f"root path is not a directory: {root}"
    allowed = {".step", ".stp", ".igs", ".iges", ".ansa", ".bdf", ".nas", ".inp"}
    files = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in allowed]
    files.sort(key=lambda item: (item.stat().st_size, str(item).lower()))
    return files[:max_cases], None


def _write_batch_report(rows: list[dict[str, Any]], root_path: str) -> dict[str, str]:
    reports_dir = CONFIG.home / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"benchmark-{int(time.time())}-{new_command_id()}"
    json_path = reports_dir / f"{stem}.json"
    csv_path = reports_dir / f"{stem}.csv"
    report = {
        "root_path": root_path,
        "case_count": len(rows),
        "cases": rows,
    }
    atomic_write_json(json_path, report)

    columns = [
        "input_path",
        "ok",
        "import_ok",
        "parts",
        "faces",
        "nodes",
        "shells",
        "solids",
        "elements",
        "largest_face_id",
        "largest_face_area",
        "top_face_id",
        "top_face_max_z",
        "error",
    ]
    lines = [",".join(columns)]
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column)
            text = "" if value is None else str(value).replace('"', '""')
            if "," in text or "\n" in text or '"' in text:
                text = f'"{text}"'
            values.append(text)
        lines.append(",".join(values))
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path)}


def tool_batch_benchmark_test(root_path: str, max_cases: int = 10) -> str:
    if max_cases < 1:
        return to_json(error_response("max_cases must be >= 1", error_type="ValidationError"))
    files, error = _staged_benchmark_files(root_path, max_cases)
    if error:
        return to_json(error_response("invalid benchmark root", error_type="ValidationError", detail=error))
    if not files:
        return to_json(error_response("no supported benchmark files found", error_type="NotFound", detail=root_path))

    rows: list[dict[str, Any]] = []
    for path in files:
        row: dict[str, Any] = {"input_path": str(path), "ok": False}
        import_payload = _payload_from_tool_result(tool_import_file(str(path)))
        row["import_ok"] = bool(import_payload.get("ok"))
        if not import_payload.get("ok"):
            row["error"] = import_payload.get("error") or import_payload.get("message")
            rows.append(row)
            continue

        info_payload = _payload_from_tool_result(tool_get_model_info())
        counts = info_payload.get("data", {}).get("entity_counts", {}) if info_payload.get("ok") else {}
        for key in ("parts", "faces", "nodes", "shells", "solids", "elements"):
            row[key] = counts.get(key)

        largest_payload = _payload_from_tool_result(tool_select_faces_by_query("largest", limit=1))
        top_payload = _payload_from_tool_result(tool_select_faces_by_query("top", limit=1))
        largest_candidates = largest_payload.get("data", {}).get("candidates", []) if largest_payload.get("ok") else []
        top_candidates = top_payload.get("data", {}).get("candidates", []) if top_payload.get("ok") else []
        if largest_candidates:
            row["largest_face_id"] = largest_candidates[0].get("id")
            row["largest_face_area"] = largest_candidates[0].get("area")
        if top_candidates:
            row["top_face_id"] = top_candidates[0].get("id")
            row["top_face_max_z"] = _vector_value(top_candidates[0], "max_z")
        row["ok"] = bool(info_payload.get("ok"))
        rows.append(row)

    report_paths = _write_batch_report(rows, root_path)
    return to_json(
        success_response(
            message="benchmark batch tested",
            data={
                "root_path": root_path,
                "case_count": len(rows),
                "reports": report_paths,
                "cases": rows,
            },
            artifacts=list(report_paths.values()),
        )
    )


def tool_read_last_log() -> str:
    CONFIG.ensure_dirs()
    audit_tail = audit.summarize(limit=200)
    run_dirs = sorted((p for p in CONFIG.runs_dir.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    if not run_dirs:
        return to_json(error_response(
            "no run logs found",
            error_type="NotFound",
            # The audit trail is what answers "did it run at all?" when the run
            # directory is empty — e.g. the command was dropped as stale.
            logs={"audit": audit_tail},
        ))
    result_file = run_dirs[-1] / "result.json"
    if not result_file.exists():
        return to_json(
            error_response(
                "latest run has no result.json",
                error_type="NotFound",
                logs={"run_dir": str(run_dirs[-1]), "audit": audit_tail},
            )
        )
    result = read_json(result_file)
    return to_json(success_response(
        message="latest run log",
        data=result,
        logs={"run_dir": str(run_dirs[-1]), "audit": audit_tail},
    ))


# ===========================================================================
# 49 tools migrated from ansa-tcp-bridge (Route B) — send via file IPC.
# Each builds a command of the matching type; plugin.py dispatches to
# handle_<type> -> tools_impl.<fn>.
# ===========================================================================
def tool_ping_ansa() -> str:
    return to_json(send_command("ping_ansa"))


def tool_open_model(filepath: str) -> str:
    return to_json(send_command("open_model", filepath=filepath))


def tool_new_model() -> str:
    return to_json(send_command("new_model"))


def tool_save_model() -> str:
    return to_json(send_command("save_model"))


def tool_export_nastran(filepath: str) -> str:
    return to_json(send_command("export_nastran", path=filepath))


def tool_export_lsdyna(filepath: str) -> str:
    return to_json(send_command("export_lsdyna", path=filepath))


def tool_export_step(filepath: str) -> str:
    return to_json(send_command("export_step", path=filepath))


def tool_run_python_script_in_ansa(script: str, function_name: str = "main") -> str:
    return to_json(send_command("run_python_script_in_ansa", script=script, function_name=function_name))


def tool_count_entities(deck: int, entity_type: str) -> str:
    return to_json(send_command("count_entities", deck=deck, entity_type=entity_type))


def tool_list_entities(deck: int, entity_type: str, fields: list[str] | None = None,
                       limit: int = 100) -> str:
    return to_json(send_command("list_entities", deck=deck, entity_type=entity_type,
                                fields=fields, limit=limit))


def tool_get_entity(deck: int, entity_type: str, entity_id: int,
                   fields: list[str] | None = None) -> str:
    return to_json(send_command("get_entity", deck=deck, entity_type=entity_type,
                                entity_id=entity_id, fields=fields))


def tool_set_entity_fields(deck: int, entity_type: str, entity_id: int,
                           fields: dict) -> str:
    return to_json(send_command("set_entity_fields", deck=deck, entity_type=entity_type,
                                entity_id=entity_id, fields=fields))


def tool_create_entity(deck: int, entity_type: str, fields: dict) -> str:
    return to_json(send_command("create_entity", deck=deck, entity_type=entity_type, fields=fields))


def tool_delete_entities(deck: int, entity_type: str, entity_ids: list[int]) -> str:
    return to_json(send_command("delete_entities", deck=deck, entity_type=entity_type, entity_ids=entity_ids))


def tool_search_entities_by_name(deck: int, pattern: str) -> str:
    return to_json(send_command("search_entities_by_name", deck=deck, pattern=pattern))


def tool_get_bounding_box(deck: int, entity_type: str) -> str:
    return to_json(send_command("get_bounding_box", deck=deck, entity_type=entity_type))


def tool_get_node_coordinates(node_ids: list[int]) -> str:
    return to_json(send_command("get_node_coordinates", node_ids=node_ids))


def tool_change_element_type(deck: int, entity_type: str, entity_ids: list[int],
                             new_type: str) -> str:
    return to_json(send_command("change_element_type", deck=deck, entity_type=entity_type,
                                entity_ids=entity_ids, new_type=new_type))


def tool_create_part(name: str) -> str:
    return to_json(send_command("create_part", name=name))


def tool_create_set(deck: int, name: str) -> str:
    return to_json(send_command("create_set", deck=deck, name=name))


def tool_add_to_set(deck: int, set_id: int, entity_type: str, entity_ids: list[int]) -> str:
    return to_json(send_command("add_to_set", deck=deck, set_id=set_id,
                                entity_type=entity_type, entity_ids=entity_ids))


def tool_get_model_summary(deck: int = -1) -> str:
    return to_json(send_command("get_model_summary", deck=deck))


def tool_list_model_includes(deck: int) -> str:
    return to_json(send_command("list_model_includes", deck=deck))


def tool_calc_element_mass(deck: int, entity_type: str,
                           entity_ids: list[int] | None = None) -> str:
    return to_json(send_command("calc_element_mass", deck=deck, entity_type=entity_type,
                                entity_ids=entity_ids))


def tool_calc_shell_area(deck: int, element_id: int) -> str:
    return to_json(send_command("calc_shell_area", deck=deck, element_id=element_id))


def tool_calc_solid_volume(deck: int, element_id: int) -> str:
    return to_json(send_command("calc_solid_volume", deck=deck, element_id=element_id))


def tool_check_intersections(fast: bool = False) -> str:
    return to_json(send_command("check_intersections", fast=fast))


def tool_check_penetrations(auto_fix: bool = False) -> str:
    return to_json(send_command("check_penetrations", auto_fix=auto_fix))


def tool_check_free_nodes() -> str:
    return to_json(send_command("check_free_nodes"))


def tool_run_quality_check(check_name: str, deck: int) -> str:
    return to_json(send_command("run_quality_check", check_name=check_name, deck=deck))


def tool_count_failed_elements(deck: int, entity_type: str) -> str:
    return to_json(send_command("count_failed_elements", deck=deck, entity_type=entity_type))


def tool_check_geometry(deck: int) -> str:
    return to_json(send_command("check_geometry", deck=deck))


def tool_check_sharp_edges(angle: float, deck: int) -> str:
    return to_json(send_command("check_sharp_edges", angle=angle, deck=deck))


def tool_check_rigid_dependencies() -> str:
    return to_json(send_command("check_rigid_dependencies"))


def tool_calc_mesh_quality(deck: int, entity_type: str) -> str:
    return to_json(send_command("calc_mesh_quality", deck=deck, entity_type=entity_type))


def tool_mesh_shells(deck: int, entity_ids: list[int], length: float) -> str:
    return to_json(send_command("mesh_shells", deck=deck, entity_ids=entity_ids, length=length))


def tool_mesh_volume(deck: int, entity_ids: list[int]) -> str:
    return to_json(send_command("mesh_volume", deck=deck, entity_ids=entity_ids))


def tool_set_shell_mesh_params(deck: int, length: float) -> str:
    return to_json(send_command("set_shell_mesh_params", deck=deck, length=length))


def tool_delete_mesh(deck: int, entity_ids: list[int]) -> str:
    return to_json(send_command("delete_mesh", deck=deck, entity_ids=entity_ids))


def tool_run_batch_mesh(script_path: str) -> str:
    return to_json(send_command("run_batch_mesh", script_path=script_path))


def tool_apply_connectors(deck: int) -> str:
    return to_json(send_command("apply_connectors", deck=deck))


def tool_check_connections(deck: int) -> str:
    return to_json(send_command("check_connections", deck=deck))


def tool_list_connectors(deck: int) -> str:
    return to_json(send_command("list_connectors", deck=deck))


def tool_create_connection_point(
    position: list[float],
    connection_type: str = "Bolt_Type",
    id: int = 0,
    connectivity: list[Any] | None = None,
    card_values: dict[str, Any] | None = None,
    deck: int = -1,
) -> str:
    """Create a connection point (spotweld / bolt / gumdrop / rivet / screw).

    Backed by ``connections.CreateConnectionPoint(type, position, id,
    connectivity)``. This is the primitive needed to turn an automated
    bolt-detection pass into real Bolt connections.

    ``connectivity`` items are either connectivity strings or
    ``{"type": "ANSAPART", "id": 12345}`` pairs, which the ANSA side converts via
    ``connections.EntsToConnectivityString``.
    """
    return to_json(
        send_command(
            "create_connection_point",
            deck=deck,
            connection_type=connection_type,
            position=position,
            id=id,
            connectivity=connectivity or [],
            card_values=card_values or {},
        )
    )


def tool_show_only(deck: int, entity_type: str, entity_ids: list[int]) -> str:
    return to_json(send_command("show_only", deck=deck, entity_type=entity_type, entity_ids=entity_ids))


def tool_show_also(deck: int, entity_type: str, entity_ids: list[int]) -> str:
    return to_json(send_command("show_also", deck=deck, entity_type=entity_type, entity_ids=entity_ids))


def tool_hide(deck: int, entity_type: str, entity_ids: list[int]) -> str:
    return to_json(send_command("hide", deck=deck, entity_type=entity_type, entity_ids=entity_ids))


def tool_near(radius: float, deck: int, entity_type: str, entity_ids: list[int]) -> str:
    return to_json(send_command("near", radius=radius, deck=deck, entity_type=entity_type, entity_ids=entity_ids))


def tool_neighb(steps: int) -> str:
    return to_json(send_command("neighb", steps=steps))


# ===========================================================================
# Memory tools — the only tools that do NOT go through ANSA.
#
# Deliberately so. "Where is this project's standard .ansa_mpar?" is a question
# worth asking precisely when the bridge is stopped, and the model that
# reconnects after a crash is exactly the client that needs the answer. Routing
# memory through the plugin would make the bridge a prerequisite for
# remembering, which is the wrong dependency direction.
# ===========================================================================
def _failed(exc: Exception, message: str) -> str:
    return to_json(error_response(message, error_type=type(exc).__name__, detail=str(exc)))


def tool_remember(key: str, value: Any, note: str = "") -> str:
    """Store one preference. Overwrites, keeping the previous value."""
    try:
        record = memory.set_pref(key, value, note)
    except Exception as exc:
        return _failed(exc, f"could not remember {key!r}")
    return to_json(success_response(
        message=f"remembered {key}",
        data={"key": key, "record": record},
        logs={"prefs_file": str(CONFIG.prefs_file)},
    ))


def tool_recall(key: str = "") -> str:
    """Read one preference, or the whole memory when key is empty."""
    CONFIG.ensure_dirs()
    if key:
        try:
            record = memory.get_pref(key)
        except Exception as exc:
            return _failed(exc, f"could not recall {key!r}")
        if record is None:
            known = sorted(k for k in memory.load_prefs() if not k.startswith("_"))
            return to_json(error_response(
                f"nothing remembered under {key!r}",
                error_type="NotFound",
                detail="store it with remember(), or call recall() with no key "
                       "to see everything that is remembered",
                logs={"known_keys": known},
            ))
        return to_json(success_response(
            message=f"recalled {key}",
            data={"key": key, "record": record},
        ))
    try:
        snapshot = memory.snapshot(limit=5)
    except Exception as exc:
        return _failed(exc, "could not read memory")
    return to_json(success_response(
        message=f"{snapshot.get('preference_count', 0)} preference(s) remembered",
        data=snapshot,
    ))


def tool_remember_note(text: str, tag: str = "") -> str:
    """Append one timestamped note to memory/notes.md."""
    try:
        record = memory.append_note(text, tag)
    except Exception as exc:
        return _failed(exc, "could not append the note")
    return to_json(success_response(
        message="note appended",
        data=record,
        logs={"notes_file": str(CONFIG.notes_file)},
    ))


if FastMCP is not None:
    mcp = FastMCP("ansa-mcp-server")

    @mcp.tool(annotations=READ_ONLY)
    def ping() -> str:
        """Check that the ANSA MCP server process is running."""
        return tool_ping()

    @mcp.tool(annotations=READ_ONLY)
    def check_ansa_connection() -> str:
        """Check whether the ANSA-side plugin is loaded and responding."""
        return tool_check_ansa_connection()

    # ---- session memory: works whether or not the bridge is up ----
    @mcp.tool(annotations=READ_ONLY)
    def recall(key: str = "") -> str:
        """Read previously remembered preferences, plus notes and recent history.

        Call this at the start of a session: it returns the deck, paths and
        thresholds that earlier sessions decided on, and what the last commands
        actually did (from logs/audit.jsonl). With no key it returns everything;
        with a key it returns just that record, or NotFound plus the list of
        keys that do exist. Read ansa://memory for the same content as a resource.
        """
        return tool_recall(key)

    @mcp.tool(annotations=WRITE)
    def remember(key: str, value: Any, note: str = "") -> str:
        """Persist one preference across sessions (key/value, overwrite-safe).

        Use it for things that would otherwise be re-derived every session:
        the project deck (`remember("deck", 1, "NASTRAN")`), the standard
        `.ansa_mpar` path, the mid-surface target thickness, quality thresholds.
        The previous value is kept in the record, so an overwrite is auditable.

        For observations that do not fit a key, use remember_note instead.
        """
        return tool_remember(key, value, note)

    @mcp.tool(annotations=WRITE_ADDITIVE)
    def remember_note(text: str, tag: str = "") -> str:
        """Append a timestamped note to memory/notes.md.

        For model-level facts — measured wall thickness, why a geometry was left
        alone, a workaround found the hard way. Append-only: calling it twice
        leaves two notes.
        """
        return tool_remember_note(text, tag)

    @mcp.tool(annotations=DESTRUCTIVE)
    @_with_pitfalls("midsurfauto-positional-only", "getentitycardvalues-without-fields", "return-value-conventions")
    def execute_script(script: str, timeout_seconds: float | None = None, validate: bool = True) -> str:
        """Execute a Python script inside ANSA through the ANSA-side plugin.

        By default (validate=True) the script is pre-flight checked against the
        LIVE ANSA API first: any reference to a function that does not exist in
        the running build (e.g. a hallucinated name) is rejected BEFORE
        execution, so a bad call cannot burn a round-trip. Set validate=False
        only when you know the call is valid but not discoverable via dir(ansa).
        """
        return tool_execute_script(script, timeout_seconds, validate=validate)

    # ---- embedded ANSA API documentation (replica of ansa-api) ----
    @mcp.tool(annotations=READ_ONLY)
    def ansa_api_doc_search(query: str, module: str | None = None,
                            category: str | None = None, top_n: int = 5) -> str:
        """Search the embedded ANSA API documentation index (replica of ansa-api).

        Use this to find the EXACT function name + signature + parameters + example
        for an ANSA operation before writing a script — pass the script-level name
        (e.g. "Mesh", "GetEntity"), not the GUI/menu label, for best ranking.
        """
        return tool_ansa_api_doc_search(query, module=module, category=category, top_n=top_n)

    @mcp.tool(annotations=READ_ONLY)
    def ansa_api_doc_lookup(function_name: str, module: str | None = None) -> str:
        """Look up full docs for an exact ANSA API function name (replica of ansa-api get_ansa_function).

        Pass the precise script name (case-sensitive), e.g. "mesh.Mesh" or
        "base.GetEntity". Returns the signature, parameters, returns and an example.
        """
        return tool_ansa_api_doc_lookup(function_name, module=module)

    @mcp.tool(annotations=READ_ONLY)
    def ansa_api_doc_modules() -> str:
        """List all ANSA API modules with function counts (replica of ansa-api list_ansa_modules)."""
        return tool_ansa_api_doc_modules()

    @mcp.tool(annotations=READ_ONLY)
    def ansa_api_doc_categories() -> str:
        """List all ANSA API categories with function counts (replica of ansa-api list_ansa_categories)."""
        return tool_ansa_api_doc_categories()

    @mcp.tool(annotations=READ_ONLY)
    def validate_script(script: str, timeout_seconds: float | None = None) -> str:
        """Pre-flight check a Python script for missing ANSA API calls before executing it.

        Parses the script and verifies every ansa.<module>.<func> (and <module>.<func>)
        reference against the LIVE ANSA build. Returns the missing calls so you can
        fix hallucinations without running the script. This is the check execute_script
        runs automatically when validate=True.
        """
        return tool_validate_script(script, timeout_seconds=timeout_seconds)

    @mcp.tool(annotations=READ_ONLY)
    def get_capabilities() -> str:
        """Return plugin, Python, and ANSA capability information."""
        return tool_get_capabilities()

    @mcp.tool(annotations=READ_ONLY)
    def get_model_info() -> str:
        """Return a summary of the current ANSA model."""
        return tool_get_model_info()

    @mcp.tool(annotations=READ_ONLY)
    def geometry_inventory() -> str:
        """Return a broader ANSA geometry/entity inventory for import diagnostics."""
        return tool_geometry_inventory()

    @mcp.tool(annotations=DESTRUCTIVE)
    def import_file(input_path: str, mode: str = "open") -> str:
        """Open or import a model/geometry file from ANSA_MCP_HOME."""
        return tool_import_file(input_path, mode)

    @mcp.tool(annotations=READ_ONLY)
    def list_faces(limit: int = 100, include_card_values: bool = False) -> str:
        """List FACE entities from the current ANSA model."""
        return tool_list_faces(limit, include_card_values)

    @mcp.tool(annotations=READ_ONLY)
    def face_properties(
        face_ids: list[int] | None = None,
        limit: int = 100,
        sort_by: str = "id",
        descending: bool = False,
    ) -> str:
        """Return geometry properties for FACE entities for selection before editing."""
        return tool_face_properties(face_ids, limit, sort_by, descending)

    @mcp.tool(annotations=READ_ONLY)
    def select_faces_by_query(query: str, limit: int = 5, point: list[float] | None = None) -> str:
        """Select FACE candidates by common natural-language-like geometry queries."""
        return tool_select_faces_by_query(query, limit, point)

    @mcp.tool(annotations=READ_ONLY)
    def preview_selection(query: str, operation: str = "delete_faces", limit: int = 5, point: list[float] | None = None) -> str:
        """Preview selected FACE ids and reasons before a destructive operation."""
        return tool_preview_selection(query, operation, limit, point)

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_faces(face_ids: list[int], force: bool = True, save_as: str | None = None) -> str:
        """Delete FACE entities by ANSA face id and optionally save a copy."""
        return tool_delete_faces(face_ids, force, save_as)

    @mcp.tool(annotations=WRITE_ADDITIVE)
    def autosave_model(label: str = "autosave") -> str:
        """Save a timestamped backup of the current ANSA model under ANSA_MCP_HOME."""
        return tool_autosave_model(label)

    @mcp.tool(annotations=DESTRUCTIVE)
    def restore_model(input_path: str) -> str:
        """Restore/open a model file, usually an autosave under ANSA_MCP_HOME."""
        return tool_restore_model(input_path)

    @mcp.tool(annotations=WRITE)
    def save_model_as_ws(output_path: str, version: str | None = None, silent: bool = True) -> str:
        """Save the current ANSA model under ANSA_MCP_HOME (workspace-validated)."""
        return tool_save_model_as_ws(output_path, version, silent)

    @mcp.tool(annotations=READ_ONLY)
    def check_mesh_quality(
        check_visible: bool = False,
        fast_run: bool = False,
        include_free_nodes: bool = True,
        include_intersections: bool = True,
    ) -> str:
        """Run a basic mesh/model quality check in ANSA."""
        return tool_check_mesh_quality(check_visible, fast_run, include_free_nodes, include_intersections)

    @mcp.tool(annotations=DESTRUCTIVE)
    def surface_mesh(
        element_size: float,
        target: str = "all_surfaces",
        mesh_type: str = "auto",
        erase_existing: bool = True,
        face_ids: list[int] | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Generate a basic shell surface mesh on current ANSA FACE entities."""
        return tool_surface_mesh(element_size, target, mesh_type, erase_existing, face_ids, timeout_seconds)

    @mcp.tool(annotations=WRITE)
    def export_solver_deck(
        solver: str,
        output_path: str,
        mode: str = "all",
        options: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Export a solver deck under ANSA_MCP_HOME."""
        return tool_export_solver_deck(solver, output_path, mode, options, timeout_seconds)

    @mcp.tool(annotations=DESTRUCTIVE)
    def batch_benchmark_test(root_path: str, max_cases: int = 10) -> str:
        """Batch import benchmark files and report geometry/face selection diagnostics."""
        return tool_batch_benchmark_test(root_path, max_cases)

    @mcp.tool(annotations=READ_ONLY)
    def read_last_log() -> str:
        """Read the latest run result saved under ANSA_MCP_HOME."""
        return tool_read_last_log()

    # ---- 49 tools migrated from ansa-tcp-bridge (Route B) ----
    @mcp.tool(annotations=READ_ONLY)
    def ping_ansa() -> str:
        """Check that ANSA is reachable via the GUI bridge; returns version info."""
        return tool_ping_ansa()

    @mcp.tool(annotations=DESTRUCTIVE)
    def open_model(filepath: str) -> str:
        """Open a model file (.ansa, .bdf, .key, .inp, .igs, ...)."""
        return tool_open_model(filepath)

    @mcp.tool(annotations=DESTRUCTIVE)
    def new_model() -> str:
        """Clear the current model."""
        return tool_new_model()

    @mcp.tool(annotations=WRITE)
    def save_model() -> str:
        """Save the current model in place."""
        return tool_save_model()

    @mcp.tool(annotations=WRITE)
    def save_model_as(filepath: str) -> str:
        """Save the current model to a new .ansa file (absolute path)."""
        return tool_save_model_as(filepath)

    @mcp.tool(annotations=WRITE)
    @_with_pitfalls("return-value-conventions")
    def export_nastran(filepath: str) -> str:
        """Export as Nastran .bdf file.

        Note the return-value convention: this family reports success as 1,
        while the save/open family reports it as 0.
        """
        return tool_export_nastran(filepath)

    @mcp.tool(annotations=WRITE)
    @_with_pitfalls("return-value-conventions")
    def export_lsdyna(filepath: str) -> str:
        """Export as LS-DYNA .key file.

        Returns 1 on success (export family), unlike save/open which return 0.
        """
        return tool_export_lsdyna(filepath)

    @mcp.tool(annotations=WRITE)
    @_with_pitfalls("return-value-conventions")
    def export_step(filepath: str) -> str:
        """Export geometry as .stp file."""
        return tool_export_step(filepath)

    @mcp.tool(annotations=DESTRUCTIVE)
    @_with_pitfalls("midsurfauto-positional-only", "getentitycardvalues-without-fields", "timeout-does-not-cancel")
    def run_python_script_in_ansa(script: str, function_name: str = "main") -> str:
        """Run an arbitrary Python script inside the live ANSA session.

        Prefer the purpose-built tools: they already encode the acceptance tests
        for the silent failures quoted below.
        """
        return tool_run_python_script_in_ansa(script, function_name)

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("deck-node-type-trap")
    def count_entities(deck: int, entity_type: str) -> str:
        """Count entities of a given type on a deck (-1 = current).

        A wrong entity-type string for the deck returns 0 without raising, so a
        zero is not proof that the container is empty.
        """
        return tool_count_entities(deck, entity_type)

    @mcp.tool(annotations=READ_ONLY)
    def list_entities(deck: int, entity_type: str, fields: list[str] | None = None,
                     limit: int = 100) -> str:
        """List entities with card field values."""
        return tool_list_entities(deck, entity_type, fields, limit)

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("getentitycardvalues-without-fields")
    def get_entity(deck: int, entity_type: str, entity_id: int,
                   fields: list[str] | None = None) -> str:
        """Fetch a single entity by id.

        Pass `fields` explicitly. The underlying card-value call is the one
        known crash path in this build when driven without a field tuple.
        """
        return tool_get_entity(deck, entity_type, entity_id, fields)

    @mcp.tool(annotations=WRITE)
    def set_entity_fields(deck: int, entity_type: str, entity_id: int,
                          fields: dict) -> str:
        """Write card field values on an existing entity."""
        return tool_set_entity_fields(deck, entity_type, entity_id, fields)

    @mcp.tool(annotations=DESTRUCTIVE)
    def create_entity(deck: int, entity_type: str, fields: dict) -> str:
        """Create a new entity with the given fields."""
        return tool_create_entity(deck, entity_type, fields)

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_entities(deck: int, entity_type: str, entity_ids: list[int]) -> str:
        """Delete multiple entities."""
        return tool_delete_entities(deck, entity_type, entity_ids)

    @mcp.tool(annotations=READ_ONLY)
    def search_entities_by_name(deck: int, pattern: str) -> str:
        """Search entities by name (wildcards + regex)."""
        return tool_search_entities_by_name(deck, pattern)

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("nastran-grid-coords-are-x1x2x3", "deck-node-type-trap")
    def get_bounding_box(deck: int, entity_type: str) -> str:
        """Axis-aligned bounding box derived from GRID node coordinates."""
        return tool_get_bounding_box(deck, entity_type)

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("nastran-grid-coords-are-x1x2x3")
    def get_node_coordinates(node_ids: list[int]) -> str:
        """Return the coordinates of a list of GRID ids.

        The card-field names are resolved per deck at runtime; on a NASTRAN deck
        they are ``X1/X2/X3``. A node whose coordinates cannot be read comes
        back with an ``error`` key — never as ``[0, 0, 0]``.
        """
        return tool_get_node_coordinates(node_ids)

    @mcp.tool(annotations=WRITE)
    def change_element_type(deck: int, entity_type: str, entity_ids: list[int],
                            new_type: str) -> str:
        """Convert elements from one type to another."""
        return tool_change_element_type(deck, entity_type, entity_ids, new_type)

    @mcp.tool(annotations=WRITE_ADDITIVE)
    def create_part(name: str) -> str:
        """Create a Model Browser part."""
        return tool_create_part(name)

    @mcp.tool(annotations=WRITE_ADDITIVE)
    def create_set(deck: int, name: str) -> str:
        """Create a named SET."""
        return tool_create_set(deck, name)

    @mcp.tool(annotations=WRITE)
    def add_to_set(deck: int, set_id: int, entity_type: str,
                   entity_ids: list[int]) -> str:
        """Add entities to a SET."""
        return tool_add_to_set(deck, set_id, entity_type, entity_ids)

    @mcp.tool(annotations=READ_ONLY)
    def get_model_summary(deck: int = -1) -> str:
        """Non-zero entity counts across common types."""
        return tool_get_model_summary(deck)

    @mcp.tool(annotations=READ_ONLY)
    def list_model_includes(deck: int) -> str:
        """All INCLUDE files with ids, names, child counts."""
        return tool_list_model_includes(deck)

    @mcp.tool(annotations=READ_ONLY)
    def calc_element_mass(deck: int, entity_type: str,
                          entity_ids: list[int] | None = None) -> str:
        """Total + per-element mass."""
        return tool_calc_element_mass(deck, entity_type, entity_ids)

    @mcp.tool(annotations=READ_ONLY)
    def calc_shell_area(deck: int, element_id: int) -> str:
        """Surface area of a shell element."""
        return tool_calc_shell_area(deck, element_id)

    @mcp.tool(annotations=READ_ONLY)
    def calc_solid_volume(deck: int, element_id: int) -> str:
        """Volume of a solid element."""
        return tool_calc_solid_volume(deck, element_id)

    @mcp.tool(annotations=READ_ONLY)
    def check_intersections(fast: bool = False) -> str:
        """Detect intersecting shell surfaces."""
        return tool_check_intersections(fast)

    @mcp.tool(annotations=DESTRUCTIVE)
    def check_penetrations(auto_fix: bool = False) -> str:
        """Detect (and optionally fix) shell penetrations."""
        return tool_check_penetrations(auto_fix)

    @mcp.tool(annotations=READ_ONLY)
    def check_free_nodes() -> str:
        """Find free nodes and free edges."""
        return tool_check_free_nodes()

    @mcp.tool(annotations=READ_ONLY)
    def run_quality_check(check_name: str, deck: int) -> str:
        """Run a named quality check (e.g. 'Warping')."""
        return tool_run_quality_check(check_name, deck)

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("off-elements-depend-on-criteria")
    def count_failed_elements(deck: int, entity_type: str) -> str:
        """Count elements failing quality criteria.

        A count of 0 means "measured against the criteria currently loaded",
        not "clean" — report the two together.
        """
        return tool_count_failed_elements(deck, entity_type)

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("try-fix-does-not-fix-geometry")
    def check_geometry(deck: int) -> str:
        """Check CAD face geometry.

        Findings are the answer; do not expect repair from this call — the
        report's fix helper flips the check status without moving geometry.
        """
        return tool_check_geometry(deck)

    @mcp.tool(annotations=READ_ONLY)
    def check_sharp_edges(angle: float, deck: int) -> str:
        """Detect sharp edges above the given angle (degrees)."""
        return tool_check_sharp_edges(angle, deck)

    @mcp.tool(annotations=READ_ONLY)
    def check_rigid_dependencies() -> str:
        """Check RBE rigid body dependency issues."""
        return tool_check_rigid_dependencies()

    @mcp.tool(annotations=READ_ONLY)
    @_with_pitfalls("off-elements-depend-on-criteria")
    def calc_mesh_quality(deck: int, entity_type: str) -> str:
        """Aggregate skewness/warping/aspect-ratio/etc. for the type.

        The off-element count is only meaningful next to the criteria that were
        loaded when it was taken.
        """
        return tool_calc_mesh_quality(deck, entity_type)

    @mcp.tool(annotations=DESTRUCTIVE)
    def mesh_shells(deck: int, entity_ids: list[int], length: float) -> str:
        """Generate shell mesh on selected faces."""
        return tool_mesh_shells(deck, entity_ids, length)

    @mcp.tool(annotations=DESTRUCTIVE)
    def mesh_volume(deck: int, entity_ids: list[int]) -> str:
        """Generate volume mesh."""
        return tool_mesh_volume(deck, entity_ids)

    @mcp.tool(annotations=WRITE)
    def set_shell_mesh_params(deck: int, length: float) -> str:
        """Configure global shell mesh parameters."""
        return tool_set_shell_mesh_params(deck, length)

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_mesh(deck: int, entity_ids: list[int]) -> str:
        """Delete mesh from selected faces."""
        return tool_delete_mesh(deck, entity_ids)

    @mcp.tool(annotations=DESTRUCTIVE)
    def run_batch_mesh(script_path: str) -> str:
        """Run a batch mesh script (Python file)."""
        return tool_run_batch_mesh(script_path)

    @mcp.tool(annotations=DESTRUCTIVE)
    def apply_connectors(deck: int) -> str:
        """Realize all CONNECTION entities (spot welds, bolts, ...)."""
        return tool_apply_connectors(deck)

    @mcp.tool(annotations=READ_ONLY)
    def check_connections(deck: int) -> str:
        """Validate realized connections."""
        return tool_check_connections(deck)

    @mcp.tool(annotations=READ_ONLY)
    def list_connectors(deck: int) -> str:
        """List connector entities (id, name, type)."""
        return tool_list_connectors(deck)

    @mcp.tool(annotations=WRITE_ADDITIVE)
    def create_connection_point(
        position: list[float],
        connection_type: str = "Bolt_Type",
        id: int = 0,
        connectivity: list[Any] | None = None,
        card_values: dict[str, Any] | None = None,
        deck: int = -1,
    ) -> str:
        """Create a connection point: Bolt_Type, SpotweldPoint_Type, GumDrop_Type, Rivet_Type or Screw_Type.

        position is [x, y, z] in model units. connectivity lists the parts/properties
        the connection joins — either connectivity strings or {"type": "ANSAPART", "id": N}.
        """
        return tool_create_connection_point(
            position=position,
            connection_type=connection_type,
            id=id,
            connectivity=connectivity,
            card_values=card_values,
            deck=deck,
        )

    @mcp.tool(annotations=VIEW_ONLY)
    def show_only(deck: int, entity_type: str, entity_ids: list[int]) -> str:
        """Isolate - show ONLY these entities (hide everything else)."""
        return tool_show_only(deck, entity_type, entity_ids)

    @mcp.tool(annotations=VIEW_ONLY)
    def show_also(deck: int, entity_type: str, entity_ids: list[int]) -> str:
        """Add to visible set without hiding others."""
        return tool_show_also(deck, entity_type, entity_ids)

    @mcp.tool(annotations=VIEW_ONLY)
    def hide(deck: int, entity_type: str, entity_ids: list[int]) -> str:
        """Hide specific entities."""
        return tool_hide(deck, entity_type, entity_ids)

    @mcp.tool(annotations=VIEW_ONLY)
    def near(radius: float, deck: int, entity_type: str,
             entity_ids: list[int]) -> str:
        """Expand visible set to all within radius (mm)."""
        return tool_near(radius, deck, entity_type, entity_ids)

    @mcp.tool(annotations=VIEW_ONLY)
    def neighb(steps: int) -> str:
        """Expand visible set by N connected-neighbour hops."""
        return tool_neighb(steps)

    @mcp.resource("ansa://status")
    def ansa_status() -> str:
        """Return raw ANSA plugin status JSON."""
        status = read_status()
        return to_json(status or {"connected": False, "detail": "status.json not found"})

    @mcp.resource("ansa://capabilities")
    def ansa_capabilities() -> str:
        """Distilled ANSA capability report for the running build.

        Read this before trusting any tool that wraps an ANSA API call. The
        actionable fields are ``missing_functions`` (APIs the tools need that
        this build does not have), ``unexpectedly_present`` (APIs we avoid but
        which now exist — our facts are stale) and ``stale`` (a manifest written
        by an older probe). ``raw=True`` on the tool returns everything.
        """
        return to_json(load_capabilities())

    @mcp.resource("ansa://pitfalls")
    def ansa_pitfalls() -> str:
        """Measured failure modes of this ANSA build, as JSON.

        Every entry was reproduced on 25.1.4. Most of them are *silent*: the
        call succeeds, returns a plausible value, and does nothing — which is
        why they cost more than exceptions do. Read this before writing any
        script that manipulates geometry or reads card values; the two
        `critical` entries can produce a wrong result or take the process down.

        Each entry carries symptom / cause / correct_usage / guard, so it is
        actionable without the surrounding repository.
        """
        return knowledge.pitfalls_json()

    @mcp.resource("ansa://workflows")
    def ansa_workflows() -> str:
        """The measured 5-stage pipeline (diagnose → health check → standards →
        mid-surface + mesh → verify), as JSON.

        Each step lists the real API call and its acceptance test, plus the
        measured cost — a step that returns suspiciously fast is itself a
        failure signal (see the MidSurfAuto entry in ansa://pitfalls).
        """
        return knowledge.workflows_json()

    @mcp.resource("ansa://memory")
    def ansa_memory() -> str:
        """What this workspace remembers: preferences, notes, and command history.

        The cross-session answer to "what was I doing, and what did it do?".
        Assembled from memory/prefs.json, memory/notes.md and logs/audit.jsonl.
        Unlike status.json it survives restarts; unlike runs/ it is summarised
        rather than dumped. Same content as `recall()` with no key.
        """
        return memory.snapshot_json()
else:
    mcp = None


def main() -> None:
    if mcp is None:
        raise SystemExit("The 'mcp' package is not installed. Run: python -m pip install -e .")
    CONFIG.ensure_dirs()
    mcp.run()


if __name__ == "__main__":
    main()
