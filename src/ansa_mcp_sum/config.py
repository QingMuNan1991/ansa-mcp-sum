from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_HOME = Path(os.environ.get("ANSA_MCP_SUM_HOME", Path.home() / ".ansa-mcp-sum"))


@dataclass(frozen=True)
class AnsaMcpConfig:
    home: Path
    timeout_seconds: float = 30.0
    heartbeat_stale_seconds: float = 2147483647.0  # effectively never stale (on-demand GUI bridge)
    max_script_chars: int = 64 * 1024

    @classmethod
    def from_env(cls) -> "AnsaMcpConfig":
        return cls(
            home=Path(os.environ.get("ANSA_MCP_SUM_HOME", DEFAULT_HOME)),
            timeout_seconds=float(os.environ.get("ANSA_MCP_TIMEOUT_SECONDS", "600")),
            heartbeat_stale_seconds=float(os.environ.get("ANSA_MCP_HEARTBEAT_STALE_SECONDS", "2147483647")),
            max_script_chars=int(os.environ.get("ANSA_MCP_MAX_SCRIPT_CHARS", str(64 * 1024))),
        )

    @property
    def commands_dir(self) -> Path:
        return self.home / "commands"

    @property
    def results_dir(self) -> Path:
        return self.home / "results"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def scripts_dir(self) -> Path:
        return self.home / "scripts"

    @property
    def artifacts_dir(self) -> Path:
        """Scratch output that is *not* a command result.

        Ad-hoc probe/experiment scripts used to drop their files straight into
        the HOME root (``_proc_list.txt``, ``geom_ids_before.json``,
        ``geomfix_trace.json``), which made the state directory unreadable: a
        stray trace file sat next to ``status.json`` with no way to tell which
        one the bridge actually owns. Anything a script writes for its own use
        belongs here.
        """
        return self.home / "artifacts"

    @property
    def memory_dir(self) -> Path:
        """Durable state that outlives a session *and* an ANSA restart.

        The bridge used to have exactly two memories: ``status.json`` (reset on
        every start) and ``logs/dropped.log`` (only failures). Everything else —
        which deck this project uses, where the standard .ansa_mpar lives, what
        was learned about a specific model — lived in the operator's head or in
        a README. This is where it lives instead.
        """
        return self.home / "memory"

    @property
    def prefs_file(self) -> Path:
        """Key/value preferences: ``{"key": {"value":..., "updated":..., "note":...}}``."""
        return self.memory_dir / "prefs.json"

    @property
    def notes_file(self) -> Path:
        """Append-only free-form notes (model-level facts that do not fit a key)."""
        return self.memory_dir / "notes.md"

    @property
    def audit_file(self) -> Path:
        """One JSON object per line: what ANSA actually did, and how long it took."""
        return self.logs_dir / "audit.jsonl"

    @property
    def status_file(self) -> Path:
        return self.home / "status.json"

    @property
    def stop_file(self) -> Path:
        return self.home / "stop.flag"

    def ensure_dirs(self) -> None:
        for path in (
            self.home,
            self.commands_dir,
            self.results_dir,
            self.runs_dir,
            self.logs_dir,
            self.scripts_dir,
            self.artifacts_dir,
            self.memory_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
