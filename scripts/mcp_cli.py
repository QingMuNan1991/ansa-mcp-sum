"""Minimal CLI to send a command to the ansa-mcp-sum file-IPC bridge.

The ANSA GUI bridge (ansa_mcp_sum_autoload.py) polls ANSA_MCP_SUM_HOME/commands/
and writes results to ANSA_MCP_SUM_HOME/results/. This script drops a command
file and waits for the result, mirroring server.py's send_command().

Usage:
  python mcp_cli.py <command_type> [--timeout 600] [--script-file PATH]
                    [--kw key=value ...] [--no-wait]

Examples:
  python mcp_cli.py ping
  python mcp_cli.py execute_script --script-file my_script.py
  python mcp_cli.py open_model --kw filepath=C:/tmp/model.k

  # Queue a command without waiting. Useful when ANSA is not running yet: the
  # bridge picks it up on its next poll (e.g. right after ANSA starts).
  python mcp_cli.py run_python_script_in_ansa --no-wait \
      --script-file scripts/probe_ansa_capabilities.py --kw function_name=main
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.environ.get("ANSA_MCP_SUM_HOME", r"C:\Users\Admin\.ansa-mcp-sum"))


def send(command_type: str, timeout_seconds: float = 600.0, wait: bool = True,
         **kwargs) -> dict:
    """Drop a command file and (optionally) wait for the bridge's result."""
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "commands").mkdir(parents=True, exist_ok=True)
    (HOME / "results").mkdir(parents=True, exist_ok=True)
    (HOME / "runs").mkdir(parents=True, exist_ok=True)

    command_id = uuid.uuid4().hex[:12]
    run_dir = HOME / "runs" / f"{command_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    command = {
        "id": command_id,
        "type": command_type,
        "timestamp": time.time(),
        "timeout_seconds": timeout_seconds,
        "run_dir": str(run_dir),
        **kwargs,
    }
    cmd_path = HOME / "commands" / f"cmd_{command_id}.json"
    cmd_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")

    result_path = HOME / "results" / f"{command_id}.json"
    if not wait:
        return {
            "ok": True,
            "queued": True,
            "id": command_id,
            "command_file": str(cmd_path),
            "result_file": str(result_path),
            "note": "not waiting; the ANSA-side bridge picks this up on its next poll",
        }

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.1)
        time.sleep(0.2)
    return {"ok": False, "success": False, "error": {"type": "Timeout",
            "detail": f"no result within {timeout_seconds}s for {command_id}"},
            "id": command_id, "command_file": str(cmd_path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("type")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--script-file", default=None)
    ap.add_argument("--kw", action="append", default=[], help="key=value pairs")
    ap.add_argument("--no-wait", action="store_true",
                    help="queue the command and return immediately (ANSA may still be starting)")
    args = ap.parse_args()

    kwargs: dict = {}
    if args.script_file:
        kwargs["script"] = Path(args.script_file).read_text(encoding="utf-8")
    for kv in args.kw:
        if "=" not in kv:
            print(f"bad --kw (need key=value): {kv}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        kwargs[k] = v

    result = send(args.type, timeout_seconds=args.timeout, wait=not args.no_wait, **kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") or result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
