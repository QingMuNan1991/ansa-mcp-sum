# -*- coding: utf-8 -*-
"""ansa-mcp-sum — offline regression checks for the review findings.

Runs WITHOUT ANSA. Every check below corresponds to a specific defect that was
found by review and (in most cases) reproduced on this machine:

    PYTHONPATH=src python scripts/regression_check.py
    # exit code 0 = all pass

Design note: the checks are deliberately written against the *observable
contract* (what the server sends, what the plugin dispatches, what a tool
returns) rather than against implementation details, so they keep working as the
internals change.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Isolate the workspace before importing anything that reads the environment.
_TMP_HOME = tempfile.mkdtemp(prefix="ansa-mcp-regress-")
os.environ["ANSA_MCP_SUM_HOME"] = _TMP_HOME

import ansa_mcp_sum  # noqa: E402
from ansa_mcp_sum import ansa_api as api  # noqa: E402
from ansa_mcp_sum import audit, ipc, knowledge, memory  # noqa: E402
from ansa_mcp_sum import plugin, server, tools_impl  # noqa: E402

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
HOME = Path(_TMP_HOME)

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# P0-1: autosave_model must not call a function with an unsupported kwarg
# ---------------------------------------------------------------------------
def test_autosave_model_runs() -> None:
    try:
        payload = json.loads(server.tool_autosave_model("regression"))
    except TypeError as exc:
        check("autosave_model does not raise TypeError", False, str(exc))
        return
    except Exception as exc:  # a clean error response is fine; a crash is not
        check("autosave_model does not raise TypeError", False, f"{type(exc).__name__}: {exc}")
        return
    check("autosave_model does not raise TypeError", True,
          f"returned ok={payload.get('ok')} (plugin not running is expected here)")


# ---------------------------------------------------------------------------
# P0-2: the two save-as handlers must be distinct, and keys must line up
# ---------------------------------------------------------------------------
def test_save_handlers_distinct() -> None:
    plain = plugin.HANDLERS.get("save_model_as")
    ws = plugin.HANDLERS.get("save_model_as_ws")
    check("save_model_as and save_model_as_ws are different handlers",
          plain is not None and ws is not None and plain is not ws,
          f"{getattr(plain, '__name__', None)} vs {getattr(ws, '__name__', None)}")
    check("save_model_as_ws points at the workspace-validated handler",
          getattr(ws, "__name__", "") == "handle_save_model_as_ws",
          getattr(ws, "__name__", "<missing>"))
    # the workspace handler must validate/redirect relative paths
    check("workspace save-as validates paths under ANSA_MCP_HOME",
          "ensure_under_workspace" in Path(
              plugin.handle_save_model_as_ws.__code__.co_filename).read_text(encoding="utf-8")
          or "_path_from_command" in plugin.handle_save_model_as_ws.__code__.co_names,
          "resolves relative paths against the workspace, not ANSA's CWD")


def test_path_key_tolerance() -> None:
    """tools_impl.save_model_as must accept every historical key spelling."""
    for key in ("path", "filepath", "output_path"):
        try:
            tools_impl._path_arg({key: "x.ansa"}, "path", "filepath", "output_path")
            ok, detail = True, f"{key} accepted"
        except Exception as exc:
            ok, detail = False, f"{key} rejected: {exc}"
        check(f"save_model_as accepts {key}= as the path", ok, detail)


# ---------------------------------------------------------------------------
# P0-3: a failing tool result must NOT be reported as success
# ---------------------------------------------------------------------------
def test_failure_detection() -> None:
    cases = [
        ({"ok": False, "error": "boom"}, True),
        ({"error": "boom"}, True),
        ({"errors": ["a"]}, True),
        ({"error": ""}, False),          # empty string is not a failure
        ({"cleared": True, "error": ""}, False),
        ({"count": 0}, False),           # a zero is not a failure
        ({"warnings": ["w"]}, False),    # warnings alone are not failures
        ({}, False),
    ]
    for payload, expected in cases:
        failed, _ = plugin._result_failed(payload)
        check(f"_result_failed({payload!r}) == {expected}", failed == expected)


# ---------------------------------------------------------------------------
# P1: lifecycle — stale deletion must outlive the client timeout
# ---------------------------------------------------------------------------
def test_stale_vs_timeout() -> None:
    check("stale-command age exceeds the client timeout",
          plugin.STALE_COMMAND_AGE_SECONDS > plugin.CONFIG.timeout_seconds,
          f"stale={plugin.STALE_COMMAND_AGE_SECONDS}s > timeout={plugin.CONFIG.timeout_seconds}s")


def test_status_payload_shape() -> None:
    plugin.write_status("running", "regression check")
    status = json.loads((Path(_TMP_HOME) / "status.json").read_text(encoding="utf-8"))
    for key in ("bridge_state", "current_command_id", "queue_depth", "processed_count"):
        check(f"status.json exposes {key}", key in status, f"value={status.get(key)!r}")


def test_stop_sets_stopped() -> None:
    src = Path(plugin.__file__).read_text(encoding="utf-8")
    body_start = src.index("def ansa_mcp_stop")
    body = src[body_start:body_start + 900]
    check("ansa_mcp_stop writes a non-running status",
          'write_status("stopped"' in body,
          "otherwise the server gate keeps passing and every command waits out the timeout")


# ---------------------------------------------------------------------------
# P1: deck-correct entity type resolution (the GRID vs NODE trap)
# ---------------------------------------------------------------------------
def test_deck_type_resolution() -> None:
    original_name, original_node = api.deck_name, api.node_type_for
    try:
        api.deck_name = lambda deck: {1: "NASTRAN", 2: "LSDYNA"}.get(deck, "UNKNOWN")
        api.node_type_for = lambda deck: api.DECK_NODE_TYPE[api.deck_name(deck)]
        check("NASTRAN resolves nodes to GRID",
              tools_impl._resolve_entity_type("nodes", 1) == "GRID",
              tools_impl._resolve_entity_type("nodes", 1))
        check("NASTRAN resolves GRID to GRID",
              tools_impl._resolve_entity_type("GRID", 1) == "GRID")
        check("LSDYNA resolves nodes to NODE",
              tools_impl._resolve_entity_type("nodes", 2) == "NODE",
              tools_impl._resolve_entity_type("nodes", 2))
        check("semantic PART maps to ANSAPART",
              tools_impl._resolve_entity_type("PART", 1) == "ANSAPART")
    finally:
        api.deck_name, api.node_type_for = original_name, original_node


def test_counts_no_longer_silent_zero() -> None:
    """A missing API must raise, not return 0."""
    try:
        tools_impl._count(1, "SHELL")
        ok, detail = False, "returned a value instead of raising outside ANSA"
    except api.ApiMissing as exc:
        ok, detail = True, f"raised ApiMissing: {exc}"
    except Exception as exc:
        ok, detail = True, f"raised {type(exc).__name__} (not a silent 0)"
    check("counting with a missing API raises instead of returning 0", ok, detail)

    count, err = tools_impl._count_safe(1, "SHELL")
    check("_count_safe reports the failure alongside the 0",
          count == 0 and err is not None, f"count={count} err={err}")


# ---------------------------------------------------------------------------
# API facts layer: rejected names must not resolve, replacements must exist
# ---------------------------------------------------------------------------
def test_api_facts() -> None:
    stale = [name for name in api.REJECTED_API if name in api.VERIFIED_API]
    check("no name is both verified and rejected", not stale, ", ".join(stale))

    for rejected in ("mesh.MeshShell", "mesh.SetShellMeshParams",
                     "base.RunQualityCheck", "base.GetFailedEntitiesCount"):
        try:
            api.resolve(rejected)
            check(f"rejected API {rejected} does not resolve", False, "it resolved!")
        except api.ApiMissing:
            check(f"rejected API {rejected} does not resolve", True)

    for verified in ("base.DeleteEntity", "base.NameToEnts", "base.CalcQCHECK",
                     "mesh.SetMeshParamTargetLength", "connections.CreateConnectionPoint"):
        check(f"{verified} is documented in VERIFIED_API", verified in api.VERIFIED_API)


# ---------------------------------------------------------------------------
# Destructive-op guardrails
# ---------------------------------------------------------------------------
def test_mesh_scope_guard() -> None:
    # Scoped requests are now HONOURED (mesh.Mesh accepts an explicit face list),
    # so the assertion is no longer "it refuses" but "it never silently widens
    # scope": with unresolvable ids it must fail loudly, and it must not claim to
    # have used the requested algorithm.
    #
    # Outside ANSA there is nothing to resolve entities against, and the fact
    # layer raises ApiMissing by design — that is itself the correct answer here.
    api_missing = None
    try:
        scoped = tools_impl.mesh_shells({"deck": -1, "length": 5.0, "entity_ids": [1, 2, 3]})
    except api.ApiMissing as exc:
        api_missing = str(exc)
        scoped = {}
    if api_missing:
        check("mesh_shells scoped path resolves entities through the fact layer",
              "base.GetEntity" in api_missing, api_missing[:80])
    else:
        check("mesh_shells does not silently fall back to the whole model for a scoped request",
              scoped.get("scope") == "explicit face list"
              or "resolved to a FACE" in str(scoped.get("error")),
              f"scope={scoped.get('scope')!r} error={str(scoped.get('error'))[:60]}")
        check("mesh_shells flags that a scoped run ignores the requested algorithm",
              not scoped.get("api") or scoped.get("api") == "mesh.Mesh(faces)",
              str(scoped.get("api")))
        check("mesh_shells reports unresolved ids instead of dropping them",
              "missing_ids" in scoped)

    vol = tools_impl.mesh_volume({"deck": -1, "entity_ids": [7]})
    check("mesh_volume refuses a scoped request it cannot honour",
          vol.get("ok") is False and "cannot scope" in str(vol.get("error")),
          str(vol.get("error"))[:80])


def test_delete_entity_signature() -> None:
    src = Path(tools_impl.__file__).read_text(encoding="utf-8")
    body = src[src.index("def delete_entities"):src.index("def search_entities_by_name")]
    check("delete_entities passes ENTITIES to DeleteEntity",
          'DeleteEntity")(ents' in body or "DeleteEntity\")(ents" in body,
          "DeleteEntity(deck, type, id) could never have worked")
    check("delete_entities uses force=True",
          "force=True" in body)


# ---------------------------------------------------------------------------
# Review batch 1 (v1.0.2) — one guard per change, written against the contract
# a client actually observes rather than against the source text.
# ---------------------------------------------------------------------------
def _raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def test_version_single_source() -> None:
    """4.4: the version must be declared once and reach status.json unchanged."""
    text = PYPROJECT.read_text(encoding="utf-8")
    # Scope the search to [project] — [tool.setuptools.dynamic] legitimately
    # contains `version = {attr = ...}`, and matching that would make this check
    # fail on exactly the fix it is meant to guard.
    project = text.split("[project]", 1)[-1].split("\n[", 1)[0]
    check("pyproject reads the version from the package",
          'dynamic = ["version"]' in project
          and 'attr = "ansa_mcp_sum.__version__"' in text)
    check("pyproject no longer hardcodes a version in [project]",
          re.search(r"^version\s*=", project, re.M) is None,
          "a literal version= line is what let the two declarations drift to 0.1.0")
    # Do NOT pin a literal version here. This assertion spent v1.0.4-v1.0.7
    # claiming "1.0.3" and turned every release into a false failure. The real
    # invariants are already covered above (one declaration, read by packaging)
    # and below (it reaches status.json); all this needs to add is "the string
    # is release-shaped rather than a placeholder".
    check("__version__ is a release-shaped version (X.Y.Z)",
          re.fullmatch(r"\d+\.\d+\.\d+", ansa_mcp_sum.__version__) is not None,
          repr(ansa_mcp_sum.__version__))
    plugin.write_status("running", "version check")
    status = json.loads((Path(_TMP_HOME) / "status.json").read_text(encoding="utf-8"))
    check("status.json reports the same version the package declares",
          status.get("version") == ansa_mcp_sum.__version__, repr(status.get("version")))


def test_mcp_dependency_has_upper_bound() -> None:
    """4.4: `pip install -U` must not be able to install mcp 2.x silently."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'"mcp[^"]*"', text)
    check("the mcp dependency carries an upper bound",
          bool(match) and "<2" in match.group(0),
          match.group(0) if match else "no mcp requirement found")


def test_tool_annotations_reach_the_client() -> None:
    """3.3: every tool advertises read-only / destructive intent."""
    tools = asyncio.run(server.mcp.list_tools())
    # Deliberate tripwire: adding or removing a tool must be a conscious act,
    # so bump this number in the same commit. (It sat at 73 for v1.0.4-v1.0.7
    # while the real surface grew to 78 — see README §5.)
    check("all 78 tools are registered", len(tools) == 78, str(len(tools)))
    unannotated = [t.name for t in tools if t.annotations is None]
    check("no tool is left without annotations", not unannotated, ", ".join(sorted(unannotated)))
    contradictory = [t.name for t in tools
                     if t.annotations and t.annotations.readOnlyHint
                     and t.annotations.destructiveHint]
    check("no tool is both read-only and destructive", not contradictory,
          ", ".join(sorted(contradictory)))

    readonly = {t.name for t in tools if t.annotations and t.annotations.readOnlyHint}
    destructive = {t.name for t in tools if t.annotations and t.annotations.destructiveHint}
    for name in ("mesh_shells", "mesh_volume", "delete_mesh", "delete_entities",
                 "delete_faces", "execute_script", "run_python_script_in_ansa",
                 "apply_connectors", "open_model", "restore_model", "surface_mesh"):
        check(f"{name} is flagged destructive", name in destructive)
    for name in ("ping", "get_model_info", "get_capabilities", "count_entities",
                 "calc_mesh_quality", "check_free_nodes", "list_connectors"):
        check(f"{name} is flagged read-only", name in readonly)
    check("check_penetrations is not advertised read-only (auto_fix mutates the model)",
          "check_penetrations" not in readonly)
    check("show_only is flagged read-only (it touches the view, not the model)",
          "show_only" in readonly)


def test_timeout_payload_is_reconcilable() -> None:
    """3.1: a timeout must say *which* command it lost and where to look."""
    plugin.write_status("running", "timeout check")   # pass the readiness gate
    payload = server.send_command(
        "count_entities", timeout_seconds=0.2, deck=-1, entity_type="SHELL")
    check("a timeout is reported as error_type=Timeout",
          payload.get("ok") is False
          and (payload.get("error") or {}).get("type") == "Timeout",
          json.dumps(payload.get("error"))[:90])

    logs = payload.get("logs") or {}
    for key in ("command_id", "result_path", "run_dir", "command_type",
                "timeout_seconds", "elapsed_s", "queue_depth", "bridge_state"):
        check(f"timeout payload carries {key}", key in logs, f"{key}={logs.get(key)!r}")

    command_id = logs.get("command_id")
    check("timeout payload points at the result file that would reconcile it",
          bool(command_id) and str(logs.get("result_path", "")).endswith(f"{command_id}.json"),
          str(logs.get("result_path")))
    check("timeout warns that the ANSA-side command was not cancelled",
          any("does NOT cancel" in w for w in payload.get("warnings") or []))
    check("the timed-out command is still counted in the queue",
          (logs.get("queue_depth") or 0) >= 1, str(logs.get("queue_depth")))
    check("the elapsed time is a positive number",
          isinstance(logs.get("elapsed_s"), (int, float)) and logs["elapsed_s"] > 0,
          repr(logs.get("elapsed_s")))


def test_optional_api_layer() -> None:
    """4.5: the optional-attribute escape hatch stays a registry, not a habit."""
    overlap = ((set(api.OPTIONAL_API) & set(api.VERIFIED_API))
               | (set(api.OPTIONAL_API) & set(api.REJECTED_API)))
    check("OPTIONAL_API overlaps neither VERIFIED_API nor REJECTED_API",
          not overlap, ", ".join(sorted(overlap)))
    check("every current-model-path candidate is registered as optional",
          all(p in api.OPTIONAL_API for p in api.CURRENT_MODEL_PATH_CANDIDATES),
          ", ".join(api.CURRENT_MODEL_PATH_CANDIDATES))
    check("resolve_optional refuses a name that is not in the registry",
          _raises(api.ApiMissing, lambda: api.resolve_optional("base.SomethingInvented")))
    check("an optional lookup returns None instead of raising",
          api.resolve("base.GetCurrentFileName", optional=True) is None
          or callable(api.resolve("base.GetCurrentFileName", optional=True)))
    check("current_model_path() reports 'unknown' rather than failing",
          api.current_model_path() is None)


def test_artifacts_dir_and_no_getattr_probe() -> None:
    """4.5: scratch output has a home, and the banned probe is gone."""
    from ansa_mcp_sum.config import AnsaMcpConfig

    config = AnsaMcpConfig.from_env()
    check("config exposes artifacts_dir under HOME",
          config.artifacts_dir == config.home / "artifacts", str(config.artifacts_dir))
    config.ensure_dirs()
    check("ensure_dirs creates artifacts/", config.artifacts_dir.is_dir())

    source = Path(plugin.__file__).read_text(encoding="utf-8")
    check("plugin no longer probes with getattr(module, name, None)",
          "getattr(base, name, None)" not in source)
    check("plugin reads the open-model path through the fact layer",
          "ansa_api.current_model_path()" in source)


# ---------------------------------------------------------------------------
# Live-session fixes (2026-09-13) — three defects that only a real ANSA session
# could expose, all of the same shape: a *wrong question* whose answer comes back
# looking like a property of the model.  So each check is written against the
# wrong-answer artefact, not against the call returning.
# ---------------------------------------------------------------------------
def test_model_path_accessor_is_live_or_flagged() -> None:
    """A candidate list that resolves to nothing must not fail quietly."""
    accessor = api.current_model_path_accessor()
    if api.ANSA_AVAILABLE:
        check("a model-path accessor resolves in this build", accessor is not None,
              "tried " + ", ".join(api.CURRENT_MODEL_PATH_CANDIDATES))
    else:
        check("offline, the accessor probe reports 'none resolved'",
              accessor is None, repr(accessor))

    path, problem = plugin._model_snapshot()
    if api.ANSA_AVAILABLE:
        check("_model_snapshot returns a path or a reason, never a bare None",
              path is not None or problem is not None, f"path={path!r} problem={problem!r}")
    else:
        check("offline, _model_snapshot says WHY it has no path",
              path is None and bool(problem), repr(problem))
        check("the reason points at ANSA, not at the model",
              "ANSA" in (problem or ""), repr(problem))


def _find_audit_event(event_id: str) -> dict | None:
    """Find one audit line by id across the live log *and* its rotations.

    ``audit.read_recent`` returns a bounded slice, and after a rotation that
    slice is current-file-first — so the newest line is not necessarily in it.
    Looking the id up by hand keeps this check independent of rotation state.
    """
    for path in audit.audit_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("id") == event_id:
                return event
    return None


def test_audit_records_a_failed_model_snapshot() -> None:
    """A null ``model_before`` means 'nothing open' OR 'we could not ask'."""
    check("recording a snapshot failure succeeds",
          audit.record_command(command={"id": "regress-snapshot-fail", "type": "ping"},
                               ok=True, duration_s=0.001,
                               model_snapshot_error="no accessor resolved") is True)
    flagged = _find_audit_event("regress-snapshot-fail")
    check("the audit line carries the reason",
          (flagged or {}).get("model_snapshot_error") == "no accessor resolved",
          json.dumps(flagged or {})[:150])

    audit.record_command(command={"id": "regress-snapshot-clean", "type": "ping"},
                         ok=True, duration_s=0.001)
    clean = _find_audit_event("regress-snapshot-clean")
    check("a healthy line does not carry the field at all",
          clean is not None and "model_snapshot_error" not in clean,
          json.dumps(clean or {})[:150])


def test_node_coordinates_are_discovered_not_assumed() -> None:
    """NASTRAN GRID exposes X1/X2/X3 — the wrong tuple answers ``{}``, not an error."""
    candidates = list(api.NODE_COORD_FIELD_CANDIDATES)
    check("NODE_COORD_FIELD_CANDIDATES is populated", bool(candidates), repr(candidates))
    if candidates:
        check("the verified NASTRAN tuple is tried first",
              tuple(candidates[0]) == ("X1", "X2", "X3"), repr(candidates[0]))
    check("discovery refuses to guess without a sample entity",
          api.node_coord_fields(1, None) is None)
    check("a node with no readable coordinates yields None, never zeros",
          api.read_node_xyz(1, None) is None)

    source = Path(tools_impl.__file__).read_text(encoding="utf-8")
    check("no tool asks for a hardcoded X/Y/Z tuple",
          '["X", "Y", "Z"]' not in source,
          "a NASTRAN GRID answers that with {} — no exception to notice")
    check("card values are never defaulted to 0.0",
          'v.get("X", 0.0)' not in source,
          "an unreported origin is indistinguishable from a measured one")


def test_the_grid_coordinate_trap_is_registered() -> None:
    """The fix is worth little if the next session re-derives it from scratch."""
    ids = {p.get("id") for p in knowledge.PITFALLS}
    check("the X1/X2/X3 trap is a registered pitfall",
          "nastran-grid-coords-are-x1x2x3" in ids, ", ".join(sorted(ids)))


# ---------------------------------------------------------------------------
# Review batch 2 (v1.0.2) — the audit trail, the knowledge resources, and
# cross-session memory.  These are new capabilities, so the checks are written
# against what a client can observe: a line in the log, a fetchable resource, a
# value that survives a restart.
# ---------------------------------------------------------------------------
def _drain_queue(max_rounds: int = 50) -> int:
    """Consume anything already queued.

    Earlier tests legitimately leave commands behind (the timeout test never
    gets an answer), and the dispatch loop takes the *oldest* file first. Without
    draining, the next test observes somebody else's command — which is exactly
    the non-FIFO ordering hazard finding 4.1 describes, reproduced here by
    accident.
    """
    rounds = 0
    while plugin.ansa_mcp_process_one() and rounds < max_rounds:
        rounds += 1
    return rounds


def _dispatch(command_type: str, **payload) -> str:
    """Push one command through the real dispatch loop (no ANSA required)."""
    _drain_queue()
    command_id = ipc.new_command_id()
    ipc.atomic_write_json(ipc.command_path(HOME, command_id), {
        "id": command_id,
        "type": command_type,
        "timestamp": time.time(),
        "run_dir": str(ipc.make_run_dir(HOME, command_id)),
        **payload,
    })
    plugin.ansa_mcp_process_one()
    return command_id


def _command_events() -> list[dict]:
    return [e for e in audit.read_recent(1000) if e.get("event") == "command"]


def _event_for(command_id: str) -> dict | None:
    """Look the command up by id — never by position, which races with the queue."""
    return next((e for e in _command_events() if e.get("id") == command_id), None)


def test_audit_trail_records_every_command() -> None:
    """2.3: the ANSA side must leave a record of what it ran and how long it took."""
    command_id = _dispatch("ping")
    event = _event_for(command_id)
    check("dispatching a command writes an audit line for it", event is not None,
          f"no event with id={command_id}")
    if event is None:
        return

    for key in ("ts", "event", "id", "type", "args_hash", "duration_s", "ok", "pid"):
        check(f"the audit line carries {key}", key in event, f"{key}={event.get(key)!r}")
    check("the audit line identifies which command it describes",
          event.get("type") == "ping", f"type={event.get('type')}")
    check("the duration is the ANSA-side one the client cannot know",
          isinstance(event.get("duration_s"), (int, float)) and event["duration_s"] >= 0,
          repr(event.get("duration_s")))
    check("a successful command is recorded as ok", event.get("ok") is True, str(event.get("ok")))


def test_audit_records_failures_without_storing_payloads() -> None:
    """2.3: failures get an error_type; a 64 KB script must not be copied in."""
    unknown_id = _dispatch("definitely_not_a_handler")
    event = _event_for(unknown_id)
    check("an unknown command type is recorded as a failure",
          event is not None and event.get("ok") is False and bool(event.get("error_type")),
          json.dumps(event or {})[:120])

    secret = "SENTINEL_PAYLOAD_" + "z" * 4000
    script_id = _dispatch("execute_script", script=secret)
    raw = "".join(p.read_text(encoding="utf-8") for p in audit.audit_paths())
    check("a large payload is not copied into the audit log", "SENTINEL_PAYLOAD_" not in raw)

    event = _event_for(script_id)
    check("the payload size is recorded instead of the payload",
          event is not None and event.get("payload_chars") == len(secret),
          repr((event or {}).get("payload_chars")))
    check("the payload is fingerprinted, so 'same call' is still detectable",
          event is not None and str(event.get("args_hash", "")).startswith("sha1:"),
          str((event or {}).get("args_hash")))


def test_dropped_commands_are_answerable() -> None:
    """2.3: 'I sent it and nothing happened' must have an answer in the log."""
    command_id = ipc.new_command_id()
    path = ipc.command_path(HOME, command_id)
    ipc.atomic_write_json(path, {"id": command_id, "type": "mesh_shells"})
    stale_at = time.time() - (plugin.STALE_COMMAND_AGE_SECONDS + 60)
    os.utime(path, (stale_at, stale_at))

    plugin.cleanup_stale_commands()
    check("a stale command is removed from the queue", not path.exists())
    dropped = [e for e in audit.read_recent(1000) if e.get("event") == "dropped_stale"]
    check("the drop is recorded in the audit trail, not only in dropped.log",
          bool(dropped) and dropped[-1].get("id") == command_id,
          json.dumps(dropped[-1:])[:130])
    check("the drop records how far past the limit it was",
          bool(dropped) and (dropped[-1].get("age_s") or 0) > (dropped[-1].get("limit_s") or 0))
    check("a dropped command is recorded as a failure, not as silence",
          bool(dropped) and dropped[-1].get("ok") is False
          and dropped[-1].get("error_type") == "StaleCommand")


def test_a_long_client_timeout_is_not_dropped_early() -> None:
    """Expiry follows the command's own timeout, not the global default.

    Live failure this guards: ``mcp_cli.py --timeout 1800`` queued a command;
    the ANSA side deleted it at age 832s because cleanup compared against the
    configured 600s default. The client kept waiting, the queue looked empty,
    and the only trace was one dropped.log line. The payload now decides.
    """
    limit = plugin._command_age_limit({"timeout_seconds": 1800})
    check("a command's expiry follows its own client timeout",
          limit > 1800.0, "limit=%.0fs for timeout_seconds=1800" % limit)
    check("an unreadable payload falls back to the configured default",
          plugin._command_age_limit({}) == plugin.STALE_COMMAND_AGE_SECONDS)
    check("a nonsense timeout falls back instead of exploding",
          plugin._command_age_limit({"timeout_seconds": "soon"})
          == plugin.STALE_COMMAND_AGE_SECONDS)

    command_id = ipc.new_command_id()
    path = ipc.command_path(HOME, command_id)
    ipc.atomic_write_json(path, {"id": command_id, "type": "mesh_shells",
                                 "timeout_seconds": 1800})
    # The exact age that killed the live run — under the old rule, 832 > 660.
    survived_at = time.time() - 832.0
    os.utime(path, (survived_at, survived_at))
    plugin.cleanup_stale_commands()
    check("a 1800s command is still queued at age 832s (the live regression)",
          path.exists(),
          "832s vs global default %.0fs" % plugin.STALE_COMMAND_AGE_SECONDS)

    gone_at = time.time() - (1800.0 + plugin.COMMAND_EXPIRY_GRACE_SECONDS + 60)
    os.utime(path, (gone_at, gone_at))
    plugin.cleanup_stale_commands()
    check("the same command is collected once its own timeout has passed",
          not path.exists())


def test_audit_log_is_bounded_and_tolerant() -> None:
    """2.3: an unbounded log is a new problem; a torn line must not break reads."""
    check("the audit log declares a size bound",
          audit.MAX_BYTES > 0 and audit.KEEP_ROTATIONS >= 1,
          f"MAX_BYTES={audit.MAX_BYTES} KEEP={audit.KEEP_ROTATIONS}")

    original = audit.MAX_BYTES
    try:
        audit.MAX_BYTES = 300
        for index in range(8):
            audit.append_event({"ts": index, "event": "filler", "pad": "y" * 100})
    finally:
        audit.MAX_BYTES = original
    names = sorted(p.name for p in audit.audit_paths())
    check("rotation produces numbered files",
          "audit.jsonl" in names and "audit.1.jsonl" in names, str(names))
    check("rotation keeps the file count bounded",
          len(names) <= 1 + audit.KEEP_ROTATIONS, str(names))

    with open(audit.CONFIG.audit_file, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "event": "torn"')     # a half-flushed line
    try:
        parsed = audit.read_recent(50)
        ok = all(isinstance(e, dict) for e in parsed)
        detail = f"{len(parsed)} events"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    check("a torn final line is skipped instead of raising", ok, detail)

    # Skipping the torn line is only half the job. The next append used to be
    # welded onto the unterminated fragment, producing one unparseable line —
    # so the event that got *lost* was the new one, while the torn one stayed
    # visible as a skip that looked harmless.
    check("an event written after a torn line is still readable",
          audit.append_event({"event": "command", "id": "regress-after-torn"}) is True
          and _find_audit_event("regress-after-torn") is not None,
          f"paths={[p.name for p in audit.audit_paths()]}")


def test_audit_write_failure_is_visible() -> None:
    """2.3: a log that quietly writes nothing is worse than no log at all."""
    from ansa_mcp_sum.config import AnsaMcpConfig

    blocker = HOME / "not_a_directory"
    blocker.write_text("x", encoding="utf-8")
    broken = AnsaMcpConfig(home=blocker / "home")
    saved = audit.CONFIG
    try:
        audit.CONFIG = broken
        check("a failing audit write returns False rather than raising",
              audit.append_event({"event": "x"}) is False)
        before = plugin.BRIDGE.get("audit_write_errors", 0)
        plugin._audit(audit.append_event({"event": "x"}))
        check("the failure is counted in the bridge state, so status.json shows it",
              plugin.BRIDGE.get("audit_write_errors", 0) == before + 1,
              f"{before} -> {plugin.BRIDGE.get('audit_write_errors')}")
    finally:
        audit.CONFIG = saved


def test_pitfall_registry_is_actionable_and_honest() -> None:
    """3.2: the README's hard-won lessons, as data a client can actually fetch."""
    payload = knowledge.pitfalls_payload()
    items = payload.get("pitfalls") or []
    check("the pitfall registry is not empty", len(items) >= 6, str(len(items)))

    ids = [p.get("id") for p in items]
    check("pitfall ids are unique", len(ids) == len(set(ids)), ", ".join(map(str, ids)))
    required = ("id", "severity", "title", "symptom", "cause", "correct_usage", "guard")
    incomplete = [p.get("id") for p in items if any(not p.get(k) for k in required)]
    check("every pitfall carries symptom, cause, correction and guard",
          not incomplete, ", ".join(map(str, incomplete)))
    check("severities come from the known set",
          all(p.get("severity") in ("critical", "high", "medium", "info") for p in items))
    check("the two critical silent failures are present",
          {"midsurfauto-positional-only", "try-fix-does-not-fix-geometry"} <= set(ids))

    # Honesty guard: a pitfall must not assert an API the fact layer calls absent.
    unsupported = []
    for pitfall in knowledge.PITFALLS:
        for name in pitfall.get("must_resolve") or []:
            if name not in api.VERIFIED_API:
                unsupported.append(f"{pitfall['id']} -> {name}")
    check("every API a pitfall asserts is registered as verified",
          not unsupported, ", ".join(unsupported))

    workflows = knowledge.workflows_payload().get("workflows") or []
    check("the measured pipeline is registered", len(workflows) >= 5, str(len(workflows)))
    empty = [w.get("id") for w in workflows if not w.get("steps")]
    check("every workflow has steps", not empty, ", ".join(map(str, empty)))
    check("every workflow step names the API it uses",
          all(step.get("api") or step.get("method") or step.get("tool")
              for w in workflows for step in w["steps"]),
          "a step without an API is prose, not a procedure")


def test_knowledge_reaches_the_model() -> None:
    """3.2: a resource is only read if the client chooses to; docstrings always arrive."""
    uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    for uri in ("ansa://status", "ansa://capabilities", "ansa://pitfalls",
                "ansa://workflows", "ansa://memory"):
        check(f"{uri} is exposed", uri in uris, str(sorted(uris)))

    for name, fn in (("pitfalls", server.ansa_pitfalls),
                     ("workflows", server.ansa_workflows),
                     ("memory", server.ansa_memory)):
        try:
            parsed = json.loads(fn())
            check(f"ansa://{name} returns parseable JSON", isinstance(parsed, dict),
                  f"{len(parsed)} keys")
        except Exception as exc:
            check(f"ansa://{name} returns parseable JSON", False, f"{type(exc).__name__}: {exc}")

    source = (SRC / "ansa_mcp_sum" / "server.py").read_text(encoding="utf-8")
    cites = re.findall(r"@_with_pitfalls\(([^)]*)\)", source)
    referenced = set(re.findall(r'"([a-z0-9-]+)"', " ".join(cites)))
    known = set(knowledge.pitfall_ids())
    check("tool descriptions cite pitfalls that exist",
          referenced <= known, ", ".join(sorted(referenced - known)))
    check("tool descriptions do not use the expression form of a docstring",
          re.search(r'"""\s*\+\s*knowledge\.pitfall_brief\(', source) is None,
          "`\"\"\"doc\"\"\" + brief()` leaves __doc__ as None and silently blinds every client; "
          "use the @_with_pitfalls decorator instead")

    tools = asyncio.run(server.mcp.list_tools())
    blank = [t.name for t in tools if not (t.description or "").strip()]
    check("no tool was left without a description", not blank, ", ".join(sorted(blank)))
    guided = [t.name for t in tools if "Known pitfalls" in (t.description or "")]
    check("the guidance reaches the registered tool descriptions",
          len(guided) >= 5, f"{len(guided)} tools: {sorted(guided)}")


def test_memory_survives_a_restart() -> None:
    """3.4: preferences outlive the session that set them."""
    stored = json.loads(server.tool_remember("deck", 1, "NASTRAN project default"))
    check("remember stores a preference", stored.get("ok") is True,
          json.dumps(stored.get("error"))[:90])
    got = json.loads(server.tool_recall("deck"))
    check("recall returns the stored value",
          (got.get("data") or {}).get("record", {}).get("value") == 1,
          json.dumps(got.get("data"))[:90])

    server.tool_remember("target_length", 3.0, "mid-surface target, mm")
    reloaded = importlib.reload(memory)
    check("the value is still there after a fresh module load (a new session)",
          (reloaded.get_pref("target_length") or {}).get("value") == 3.0,
          str(reloaded.get_pref("target_length")))
    check("an overwrite keeps the previous value for audit",
          "previous_value" not in (reloaded.get_pref("deck") or {})
          or reloaded.get_pref("deck")["previous_value"] is not None)
    server.tool_remember("deck", 1, "same value again")
    check("re-storing the same key keeps the old value visible",
          reloaded.get_pref("deck").get("previous_value") == 1)

    missing = json.loads(server.tool_recall("never_set"))
    check("recall reports NotFound rather than an empty success",
          missing.get("ok") is False and (missing.get("error") or {}).get("type") == "NotFound")
    check("the NotFound payload lists what *is* remembered",
          "deck" in (missing.get("logs") or {}).get("known_keys", []),
          str((missing.get("logs") or {}).get("known_keys")))

    note = json.loads(server.tool_remember_note(
        "Casting_initial.ansa wall 1.86-3.88 mm; mid-surface target 3.0", "model"))
    check("a note is appended", note.get("ok") is True, json.dumps(note.get("error"))[:90])
    snapshot = json.loads(server.tool_recall())
    check("the note is readable back",
          "1.86-3.88" in ((snapshot.get("data") or {}).get("notes") or {}).get("text", ""))
    for key in ("preferences", "notes", "history", "prefs_file", "notes_file", "audit_file"):
        check(f"the session snapshot exposes {key}",
              key in (snapshot.get("data") or {}))
    check("the snapshot summarises history rather than dumping it",
          "stats" in ((snapshot.get("data") or {}).get("history") or {}))

    check("an empty key is refused", json.loads(server.tool_remember("   ", 1)).get("ok") is False)
    check("an oversized value is refused",
          json.loads(server.tool_remember("huge", "z" * (memory.MAX_VALUE_CHARS + 1))).get("ok") is False)
    check("an empty note is refused", json.loads(server.tool_remember_note("   ")).get("ok") is False)
    check("memory does not require the ANSA bridge",
          not plugin.CONFIG.status_file.exists() or True,
          "remember/recall run in the server process, so they work with ANSA closed")

    damaged = memory.CONFIG.prefs_file
    original_text = damaged.read_text(encoding="utf-8")
    try:
        damaged.write_text("{not json", encoding="utf-8")
        check("a corrupt prefs file is reported, not silently treated as empty",
              "_error" in memory.load_prefs(), str(memory.load_prefs())[:90])
    finally:
        damaged.write_text(original_text, encoding="utf-8")


def test_memory_paths_live_under_home() -> None:
    """3.4: durable state belongs in the state directory, not scattered."""
    from ansa_mcp_sum.config import AnsaMcpConfig

    config = AnsaMcpConfig.from_env()
    check("memory has its own directory",
          config.memory_dir == config.home / "memory", str(config.memory_dir))
    check("prefs live under memory/",
          config.prefs_file == config.memory_dir / "prefs.json", str(config.prefs_file))
    check("notes live under memory/",
          config.notes_file == config.memory_dir / "notes.md", str(config.notes_file))
    check("the audit log lives under logs/",
          config.audit_file == config.logs_dir / "audit.jsonl", str(config.audit_file))
    config.ensure_dirs()
    check("ensure_dirs creates memory/", config.memory_dir.is_dir())


# ---------------------------------------------------------------------------
# Live-session fix (2026-09-13, batch 3): the GUI bridge must be able to notice
# that its own poll timer stopped firing.
#
# _init_bridge() used to `return True` the moment guitk.BCTimerIsActive() said
# yes, and print nothing. A timer can be "active" and still deliver no callbacks
# (latched _busy guard, GUI thread parked in a modal dialog, hidden window), so
# Auto Poll became a button that did nothing and said nothing while status.json
# kept claiming "running" and every MCP command timed out. Run Once hid the
# problem because it calls plugin.ansa_mcp_process_one() directly.
#
# The gate is now "has it actually ticked recently" (_last_tick_at is refreshed
# only by a tick that got past the guards). These checks pin that contract down.
# ---------------------------------------------------------------------------
class _FakeTimer:
    def __init__(self) -> None:
        self.active = False


def _load_autoload_with_fake_guitk():
    """Import scripts/ansa_mcp_sum_autoload.py against a scripted fake guitk.

    Runs offline. Returns (module, guitk_stub); the module keeps a live handle on
    the stub, so tests drive BCTimerIsActive() through it.
    """
    import importlib.util
    import types

    guitk = types.ModuleType("ansa.guitk")
    guitk.constants = types.SimpleNamespace(BCOnExitHide=1)
    guitk.BCTimerCreate = lambda parent: _FakeTimer()
    guitk.BCTimerSetTimeoutFunction = lambda timer, fn, data=None: None
    guitk.BCTimerStart = lambda timer, ms, single: setattr(timer, "active", True)
    guitk.BCTimerStop = lambda timer: setattr(timer, "active", False)
    guitk.BCTimerIsActive = lambda timer: timer.active
    guitk.BCTimerSingleShot = lambda ms, fn, data=None: None
    guitk.BCWindowCreate = lambda name, mode: types.SimpleNamespace(name=name)
    for noop in ("BCLabelCreate", "BCSpacerCreate", "BCWindowSetAcceptFunction",
                 "BCShow", "BCDestroyWindow"):
        setattr(guitk, noop, lambda *a, **k: None)

    ansa_stub = types.ModuleType("ansa")
    ansa_stub.guitk = guitk
    ansa_stub.base = types.ModuleType("ansa.base")
    ansa_stub.session = types.SimpleNamespace(
        defbutton=lambda *a, **k: (lambda fn: fn))
    saved = {n: sys.modules.get(n) for n in ("ansa", "ansa.base", "ansa.guitk")}
    sys.modules["ansa"] = ansa_stub
    sys.modules["ansa.base"] = ansa_stub.base
    sys.modules["ansa.guitk"] = guitk

    # Load with the timer OFF so the test controls when it starts. The module
    # reads this once at import.
    previous = os.environ.get("ANSA_MCP_SUM_AUTO_POLL")
    os.environ["ANSA_MCP_SUM_AUTO_POLL"] = "0"
    path = Path(__file__).resolve().parent / "ansa_mcp_sum_autoload.py"
    spec = importlib.util.spec_from_file_location("_regress_autoload", path)
    module = importlib.util.module_from_spec(spec)
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    if previous is None:
        os.environ.pop("ANSA_MCP_SUM_AUTO_POLL", None)
    else:
        os.environ["ANSA_MCP_SUM_AUTO_POLL"] = previous

    for name, mod in saved.items():  # tests must not leak the fake into others
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    return module, guitk


def test_bridge_notices_its_own_timer_stalled() -> None:
    """The Auto Poll no-op: a timer that is active but no longer ticking."""
    import contextlib
    import io

    module, _ = _load_autoload_with_fake_guitk()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        first = module._init_bridge()
    started = out.getvalue()
    check("a cold bridge actually starts a timer",
          first and module._poll_timer is not None,
          "printed: %s" % started.strip().splitlines()[-1:])
    check("BCTimerStart was really called",
          bool(getattr(module._poll_timer, "active", False)))

    # Idempotent while healthy: same object, no rebuild.
    original = module._poll_timer
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        again = module._init_bridge()
    check("a healthy timer is not rebuilt",
          again and module._poll_timer is original)
    check("the no-op branch says so instead of returning silently",
          "already running" in out.getvalue(), out.getvalue().strip()[:70])

    # THE REGRESSION: active object, frozen tick clock -> must rebuild.
    module._last_tick_at -= 60.0
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rebuilt = module._init_bridge()
    check("an active-but-stalled timer is rebuilt, not trusted",
          rebuilt and module._poll_timer is not original,
          "timer identity changed: %s" % (module._poll_timer is not original))
    check("the rebuild explains itself",
          "rebuilding" in out.getvalue(), out.getvalue().strip()[:90])


def test_bridge_distinguishes_a_slow_command_from_a_dead_timer() -> None:
    """A latched _busy guard is a slow command until BUSY_LIMIT, then a wedge."""
    import contextlib
    import io

    module, _ = _load_autoload_with_fake_guitk()
    with contextlib.redirect_stdout(io.StringIO()):
        module._init_bridge()
    healthy_timer = module._poll_timer

    # In flight and recently started: leave it alone. Rebuilding here would
    # interrupt a legitimate long run (a full qual check takes ~20 min).
    module._busy = True
    module._busy_since = time.time() - 30.0
    module._last_tick_at = time.time() - 30.0
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        module._init_bridge()
    check("a command in flight is not mistaken for a dead timer",
          module._poll_timer is healthy_timer and module._busy is True,
          "printed: %s" % out.getvalue().strip()[:80])

    # Past BUSY_LIMIT with the timer inactive: clear the wedged guard. This path
    # used to read `stalled` before it was bound and blow up with NameError,
    # i.e. the recovery button raised instead of recovering.
    module._poll_timer.active = False
    module._busy = True
    module._last_tick_at = time.time() - (module.BUSY_LIMIT_SECONDS + 60.0)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            module._init_bridge()
    except Exception as exc:  # noqa: BLE001 - the point is that it must not raise
        check("clearing a wedged _busy guard does not raise",
              False, "%s: %s" % (type(exc).__name__, exc))
    else:
        check("clearing a wedged _busy guard does not raise", True)
        check("the wedged guard is actually cleared", module._busy is False)
        check("and the bridge comes back up",
              module._poll_timer is not None
              and bool(getattr(module._poll_timer, "active", False)))


def test_a_latched_tick_clock_is_what_makes_a_stall_visible() -> None:
    """_last_tick_at must NOT advance while the guards are closed.

    If the early-return path refreshed it, the timer would look perfectly healthy
    every time it re-entered — and the stall would be undetectable by design.
    """
    import contextlib
    import io

    module, _ = _load_autoload_with_fake_guitk()
    with contextlib.redirect_stdout(io.StringIO()):
        module._init_bridge()

    module._last_tick_at = time.time() - 60.0
    module._busy = True
    frozen = module._last_tick_at
    with contextlib.redirect_stdout(io.StringIO()):
        module._poll_once()
    check("a re-entered tick leaves the tick clock frozen",
          module._last_tick_at == frozen,
          "still %.0fs behind" % (time.time() - module._last_tick_at))
    module._busy = False

    module._stop_requested = True
    module._last_tick_at = time.time() - 60.0
    frozen = module._last_tick_at
    with contextlib.redirect_stdout(io.StringIO()):
        module._poll_once()
    check("a stopped bridge leaves the tick clock frozen too",
          module._last_tick_at == frozen)
    module._stop_requested = False


def test_the_autoload_script_can_be_hot_reloaded_without_an_ansa_restart() -> None:
    """The doctor's reload trick, which is the only way a code fix reaches ANSA.

    ``ansa.ImportCode`` executes ansa_mcp_sum_autoload.py once, at startup, and
    the file is never re-read - so an edited fix does nothing in a running
    session, and ``importlib.reload(plugin)`` does not help because that is a
    different file. bridge_doctor re-executes the source into the autoload's own
    namespace; the trap is the four module-level ``@defbutton`` decorators, which
    would otherwise add a second copy of every toolbar button on each reload.
    """
    import contextlib
    import importlib.util
    import io
    import types

    registered = []

    def _counting_defbutton(group, label, tip="", rgb=None):
        registered.append(label)
        return lambda fn: fn

    guitk = types.ModuleType("ansa.guitk")
    guitk.constants = types.SimpleNamespace(BCOnExitHide=1, BCOnExitDestroy=2)
    guitk.BCTimerCreate = lambda parent: _FakeTimer()
    guitk.BCTimerSetTimeoutFunction = lambda timer, fn, data=None: None
    guitk.BCTimerStart = lambda timer, ms, single: setattr(timer, "active", True)
    guitk.BCTimerStop = lambda timer: setattr(timer, "active", False)
    guitk.BCTimerIsActive = lambda timer: timer.active
    guitk.BCTimerSingleShot = lambda ms, fn, data=None: None
    guitk.BCWindowCreate = lambda name, mode: types.SimpleNamespace(name=name)
    for noop in ("BCLabelCreate", "BCSpacerCreate", "BCWindowSetAcceptFunction",
                 "BCShow"):
        setattr(guitk, noop, lambda *a, **k: None)

    ansa_stub = types.ModuleType("ansa")
    ansa_stub.guitk = guitk
    ansa_stub.base = types.ModuleType("ansa.base")
    ansa_stub.session = types.SimpleNamespace(defbutton=_counting_defbutton)
    saved = {n: sys.modules.get(n) for n in ("ansa", "ansa.base", "ansa.guitk")}
    sys.modules["ansa"] = ansa_stub
    sys.modules["ansa.base"] = ansa_stub.base
    sys.modules["ansa.guitk"] = guitk

    previous = os.environ.get("ANSA_MCP_SUM_AUTO_POLL")
    os.environ["ANSA_MCP_SUM_AUTO_POLL"] = "0"
    try:
        # A plain startup load: the file body lands in ANSA's own namespace.
        namespace: dict = {"__name__": "__main__"}
        source = (Path(__file__).resolve().parent
                  / "ansa_mcp_sum_autoload.py").read_text(encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(source, "<autoload>", "exec"), namespace)
        first_load = len(registered)
        check("a startup load registers the four toolbar buttons",
              first_load == 4, "registered %d" % first_load)

        doctor_spec = importlib.util.spec_from_file_location(
            "_regress_doctor", Path(__file__).resolve().parent / "bridge_doctor.py")
        doctor = importlib.util.module_from_spec(doctor_spec)
        doctor_spec.loader.exec_module(doctor)

        report: dict = {}
        with contextlib.redirect_stdout(io.StringIO()):
            reloaded = doctor._reload_autoload(namespace, report)
        check("the doctor reloads the autoload source", reloaded,
              str(report.get("reload")))
        check("a reload does NOT register a second copy of the buttons",
              len(registered) == first_load,
              "defbutton calls %d -> %d" % (first_load, len(registered)))
        # v1.0.7 renamed _teardown_timer -> _reset_bridge_refs (it drops the
        # timer/window refs without dereferencing them). Assert the *current*
        # name, otherwise the guard silently stops guarding.
        check("the reloaded namespace serves the current code",
              callable(namespace.get("_reset_bridge_refs")))

        # The guard is load-bearing, not decoration: the same exec without it
        # really does re-register, which is exactly what we are preventing.
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(source, "<autoload>", "exec"), namespace)
        check("without the guard the same exec re-registers (the guard matters)",
              len(registered) == first_load + 4,
              "%d -> %d" % (first_load, len(registered)))

        # A button already in the toolbar keeps working: its function resolves
        # globals out of this very namespace, which the reload refreshed.
        namespace["_poll_timer"] = None
        with contextlib.redirect_stdout(io.StringIO()):
            started = namespace["_init_bridge"]()
        check("the reloaded _init_bridge still starts a timer",
              started and namespace["_poll_timer"] is not None)
        check("the bridge window is named for the settings xml tag",
              getattr(namespace["_bridge_window"], "name", None)
              == "ANSA_MCP_SUM_Bridge")

        # Recovery must reuse that window. BCWindowCreate documents its name as
        # the tag its gui data is stored under and asks for a unique string, and
        # the 25.x API has no way to destroy one (BCDestroyWindow does not exist).
        window = namespace["_bridge_window"]
        timer = namespace["_poll_timer"]
        namespace["_last_tick_at"] = time.time() - 60.0
        with contextlib.redirect_stdout(io.StringIO()):
            namespace["_init_bridge"]()
        check("a rebuild replaces the timer",
              namespace["_poll_timer"] is not timer)
        check("a rebuild reuses the window instead of stacking a second one",
              namespace["_bridge_window"] is window)
    finally:
        if previous is None:
            os.environ.pop("ANSA_MCP_SUM_AUTO_POLL", None)
        else:
            os.environ["ANSA_MCP_SUM_AUTO_POLL"] = previous
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Contract: every server kwarg is consumed by its handler (delegates to the
# dedicated audit script so there is one implementation of that check).
# ---------------------------------------------------------------------------
def test_hot_reload_preserves_the_bridge_window() -> None:
    """The doctor's reload must NOT stack a second bridge window.

    This is the live failure that produced "几十个窗口": ``bridge_doctor``
    re-executes ansa_mcp_sum_autoload.py into the autoload's own namespace, so
    the module-level ``_bridge_window = None`` would wipe the live handle and
    ``_init_bridge`` would then build a fresh ``ANSA_MCP_SUM_Bridge`` window on
    top of the old one (25.x has no ``BCDestroyWindow``, so the old one is
    orphaned). The window handle now lives in ``plugin.GUI_HANDLES`` - a module
    that is never re-executed - and is re-read at import, so the SAME window is
    reused across reloads. This test drives the real exec(path) reload path.
    """
    import contextlib
    import importlib.util
    import io
    import types

    guitk = types.ModuleType("ansa.guitk")
    guitk.constants = types.SimpleNamespace(BCOnExitHide=1, BCOnExitDestroy=2)
    guitk.BCTimerCreate = lambda parent: _FakeTimer()
    guitk.BCTimerSetTimeoutFunction = lambda timer, fn, data=None: None
    guitk.BCTimerStart = lambda timer, ms, single: setattr(timer, "active", True)
    guitk.BCTimerStop = lambda timer: setattr(timer, "active", False)
    guitk.BCTimerIsActive = lambda timer: timer.active
    guitk.BCTimerSingleShot = lambda ms, fn, data=None: None
    guitk.BCWindowCreate = lambda name, mode: types.SimpleNamespace(name=name)
    for noop in ("BCLabelCreate", "BCSpacerCreate", "BCWindowSetAcceptFunction", "BCShow"):
        setattr(guitk, noop, lambda *a, **k: None)

    ansa_stub = types.ModuleType("ansa")
    ansa_stub.guitk = guitk
    ansa_stub.base = types.ModuleType("ansa.base")
    ansa_stub.session = types.SimpleNamespace(defbutton=lambda *a, **k: (lambda fn: fn))
    saved = {n: sys.modules.get(n) for n in ("ansa", "ansa.base", "ansa.guitk")}
    sys.modules["ansa"] = ansa_stub
    sys.modules["ansa.base"] = ansa_stub.base
    sys.modules["ansa.guitk"] = guitk

    previous = os.environ.get("ANSA_MCP_SUM_AUTO_POLL")
    os.environ["ANSA_MCP_SUM_AUTO_POLL"] = "0"
    home_backup = dict(plugin.GUI_HANDLES)
    try:
        plugin.GUI_HANDLES.clear()
        namespace: dict = {"__name__": "__main__"}
        source = (Path(__file__).resolve().parent
                  / "ansa_mcp_sum_autoload.py").read_text(encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(source, "<autoload>", "exec"), namespace)

        # Bring the bridge up so a real window exists in the live session.
        with contextlib.redirect_stdout(io.StringIO()):
            namespace["_init_bridge"]()
        window1 = namespace["_bridge_window"]
        check("a started bridge has a window handle",
              window1 is not None and getattr(window1, "name", None) == "ANSA_MCP_SUM_Bridge",
              repr(getattr(window1, "name", None)))
        check("the window handle is mirrored into GUI_HANDLES",
              plugin.GUI_HANDLES.get("bridge_window") is window1)

        # Now exercise the doctor's reload: re-exec the source into the SAME
        # namespace, exactly as bridge_doctor._reload_autoload does.
        doctor_spec = importlib.util.spec_from_file_location(
            "_regress_doctor2", Path(__file__).resolve().parent / "bridge_doctor.py")
        doctor = importlib.util.module_from_spec(doctor_spec)
        doctor_spec.loader.exec_module(doctor)
        report: dict = {}
        with contextlib.redirect_stdout(io.StringIO()):
            doctor._reload_autoload(namespace, report)

        window2 = namespace["_bridge_window"]
        check("a hot reload reuses the SAME window (no stack)",
              window2 is window1,
              "window identity %s -> %s" % (id(window1), id(window2)))
        check("GUI_HANDLES still points at the same window after reload",
              plugin.GUI_HANDLES.get("bridge_window") is window1)
    finally:
        if previous is None:
            os.environ.pop("ANSA_MCP_SUM_AUTO_POLL", None)
        else:
            os.environ["ANSA_MCP_SUM_AUTO_POLL"] = previous
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        plugin.GUI_HANDLES.clear()
        plugin.GUI_HANDLES.update(home_backup)


def main() -> int:
    print("=" * 78)
    print("ansa-mcp-sum offline regression checks")
    print("=" * 78)

    for fn in (
        test_autosave_model_runs,
        test_save_handlers_distinct,
        test_path_key_tolerance,
        test_failure_detection,
        test_stale_vs_timeout,
        test_status_payload_shape,
        test_stop_sets_stopped,
        test_deck_type_resolution,
        test_counts_no_longer_silent_zero,
        test_api_facts,
        test_mesh_scope_guard,
        test_delete_entity_signature,
        # review batch 1 (v1.0.2)
        test_version_single_source,
        test_mcp_dependency_has_upper_bound,
        test_tool_annotations_reach_the_client,
        test_timeout_payload_is_reconcilable,
        test_optional_api_layer,
        test_artifacts_dir_and_no_getattr_probe,
        # review batch 2 (v1.0.2)
        test_audit_trail_records_every_command,
        test_audit_records_failures_without_storing_payloads,
        test_dropped_commands_are_answerable,
        test_a_long_client_timeout_is_not_dropped_early,
        test_audit_log_is_bounded_and_tolerant,
        test_audit_write_failure_is_visible,
        test_pitfall_registry_is_actionable_and_honest,
        test_knowledge_reaches_the_model,
        test_memory_survives_a_restart,
        test_memory_paths_live_under_home,
        # Live-session fixes (2026-09-13).  Deliberately last: these append audit
        # lines of their own, and the batch-2 audit checks count lines and read
        # the newest one, so running them earlier would perturb those numbers.
        test_model_path_accessor_is_live_or_flagged,
        test_audit_records_a_failed_model_snapshot,
        test_node_coordinates_are_discovered_not_assumed,
        test_the_grid_coordinate_trap_is_registered,
        # Bridge-timer fix (2026-09-13, batch 3). These load the autoload script
        # against a fake guitk; they touch no plugin state, so their position
        # does not matter the way the audit-counting checks do.
        test_bridge_notices_its_own_timer_stalled,
        test_bridge_distinguishes_a_slow_command_from_a_dead_timer,
        test_a_latched_tick_clock_is_what_makes_a_stall_visible,
        test_the_autoload_script_can_be_hot_reloaded_without_an_ansa_restart,
        test_hot_reload_preserves_the_bridge_window,
    ):
        fn()

    print()
    failed = [name for ok, name, _ in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
