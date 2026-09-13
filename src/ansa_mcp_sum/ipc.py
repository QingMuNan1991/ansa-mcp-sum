from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def new_command_id() -> str:
    return uuid.uuid4().hex[:12]


def utc_timestamp() -> float:
    return time.time()


def timestamp_label() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
    tmp_path.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    atomic_write_text(path, text + "\n")


def read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def make_run_dir(home: Path, command_id: str) -> Path:
    run_dir = home / "runs" / f"{timestamp_label()}-{command_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(exist_ok=True)
    return run_dir


def command_path(home: Path, command_id: str) -> Path:
    return home / "commands" / f"cmd_{command_id}.json"


def result_path(home: Path, command_id: str) -> Path:
    return home / "results" / f"{command_id}.json"


def count_queue_depth(home: Path) -> int:
    """Commands still waiting for the plugin to pick them up."""
    try:
        return len(list((home / "commands").glob("cmd_*.json")))
    except Exception:
        return 0


def read_bridge_state(home: Path) -> dict[str, Any]:
    """A minimal bridge snapshot, for error payloads.

    A timeout is the one failure where the client has *no* other signal: the
    command was already handed to ANSA and may yet run. Reporting the bridge
    state turns "it hung" into something actionable — ``executing`` means it is
    still working, ``idle`` means it went missing, and ``current_command_id``
    says which one. Never raises: a missing or half-written ``status.json`` is
    itself information, reported as ``bridge_state: None``.
    """
    snapshot: dict[str, Any] = {
        "bridge_state": None,
        "current_command_id": None,
        "bridge_pid": None,
        "status_age_s": None,
    }
    status_file = home / "status.json"
    try:
        snapshot["status_age_s"] = round(time.time() - status_file.stat().st_mtime, 1)
    except Exception:
        pass
    try:
        status = read_json(status_file)
    except Exception:
        return snapshot
    if not isinstance(status, dict):
        return snapshot
    snapshot["bridge_state"] = status.get("bridge_state")
    snapshot["current_command_id"] = status.get("current_command_id")
    snapshot["bridge_pid"] = status.get("pid")
    return snapshot


def ensure_under_workspace(home: Path, candidate: str | Path) -> Path:
    base = home.resolve()
    path = Path(candidate)
    if not path.is_absolute():
        path = home / path
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path is outside ANSA_MCP_HOME: {resolved}") from exc
    return resolved


def success_response(
    message: str = "completed",
    data: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
    logs: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "success": True,
        "message": message,
        "data": data or {},
        "artifacts": artifacts or [],
        "logs": logs or {},
        "warnings": warnings or [],
        "error": None,
        "timestamp": utc_timestamp(),
    }


def error_response(
    message: str,
    error_type: str = "Error",
    detail: str | None = None,
    logs: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "message": message,
        "data": data or {},
        "artifacts": [],
        "logs": logs or {},
        "warnings": warnings or [],
        "error": {
            "type": error_type,
            "detail": detail or message,
        },
        "timestamp": utc_timestamp(),
    }


def wait_for_result(path: Path, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_error: str | None = None
    while time.time() < deadline:
        if path.exists():
            try:
                return read_json(path)
            except Exception as exc:
                last_error = str(exc)
        time.sleep(0.05)
    detail = f"timeout waiting for {path}"
    if last_error:
        detail += f"; last read error: {last_error}"
    raise TimeoutError(detail)
