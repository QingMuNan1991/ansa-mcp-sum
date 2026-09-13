"""ANSA-side implementations of the migrated tools.

Every function here runs *inside* the ANSA Python interpreter (loaded via
``plugin.py``). Each function takes the command dict and returns a plain,
JSON-serialisable ``dict``.

RESULT CONTRACT
---------------
A returned dict is a *success* unless it carries a truthy ``"error"`` key or an
explicit ``"ok": False``. ``plugin._call_tool`` enforces this — the earlier
version only looked for raised exceptions, so every tool that reported failure
through its return value (e.g. ``{"opened": False}``) was reported to the MCP
client as ``ok: true``.

API FACTS
---------
All API names and signatures live in :mod:`ansa_mcp_sum.ansa_api`, verified
against the ANSA 25.1.4 Python API index (5892 functions). Do not reintroduce
``getattr(mod, "Fn", None)`` fallbacks: a missing function must raise, not
silently become ``None`` and blow up as ``TypeError: 'NoneType' not callable``
after a destructive step has already run. See ``api.REJECTED_API`` for the
names that were confirmed absent.
"""
from __future__ import annotations

import io
import json
import math
import os
import traceback
from typing import Any, Sequence

from . import ansa_api as api

try:  # pragma: no cover - only present inside ANSA
    import ansa  # type: ignore
    from ansa import base, mesh, connections, constants, guitk  # type: ignore
    ANSA_AVAILABLE = True
except Exception:  # pragma: no cover
    ansa = base = mesh = connections = constants = guitk = None  # type: ignore
    ANSA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Entity type resolution (deck-aware; no blind fallbacks)
# ---------------------------------------------------------------------------
#: Convenience aliases only. Anything not listed is passed through uppercased.
#: NOTE: ``NODE`` and ``GRID`` are NOT interchangeable — see ``api.DECK_NODE_TYPE``.
ENTITY_TYPE_MAP = {
    "SHELL": "SHELL", "SOLID": "SOLID", "GRID": "GRID", "NODE": "NODE",
    "CBAR": "CBAR", "CQUAD": "CQUAD", "CTRIA": "CTRIA", "CTETRA": "CTETRA",
    "CHEXA": "CHEXA", "CPENTA": "CPENTA", "RBE2": "RBE2", "RBE3": "RBE3",
    "SET": "SET", "PART": "ANSAPART", "ANSAPART": "ANSAPART",
    "MATERIAL": "MAT1", "PROPERTY": "PSHELL",
    "INCLUDE": "INCLUDE", "FACE": "FACE",
    "CONNECTION": "CONNECTOR_ENTITY", "CONNECTOR": "CONNECTOR_ENTITY",
    "BOLT": "Bolt_Type", "SPOTWELD": "SpotweldPoint_Type",
    "RIVET": "Rivet_Type", "ELEMENT": "ELEMENT", "MASS": "MASS",
}

#: Semantic names accepted from callers, resolved per-deck at call time.
SEMANTIC_TYPES = {
    "NODES": "nodes", "NODE": "nodes", "GRID": "nodes",
    "ELEMENTS": "ELEMENT", "ELEMENT": "ELEMENT",
    "SHELLS": "SHELL", "SOLID": "SOLID", "SOLIDS": "SOLID",
    "FACES": "FACE", "FACE": "FACE",
    "PARTS": "ANSAPART", "PART": "ANSAPART",
}

SOLVER_OUTPUT_FN = {
    "NASTRAN": "OutputNastran", "BDF": "OutputNastran",
    "LSDYNA": "OutputLSDyna", "DYNA": "OutputLSDyna", "K": "OutputLSDyna",
    "ANSYS": "OutputAnsys", "CDB": "OutputAnsys",
    "ABAQUS": "OutputAbaqus", "INP": "OutputAbaqus",
    "RADIOSS": "OutputRadioss", "OPTISTRUCT": "OutputOptistruct",
    "STEP": "SaveFileAsStep", "IGES": "SaveFileAsIges",
    "STL": "OutputStereoLithography", "VRML": "OutputVrml",
}

#: Functions the migrated tools rely on that could NOT be confirmed against the
#: ANSA 25.1.4 API index. They are still callable, but the capability manifest
#: reports them as unverified so the server can hide the dependent tools.
UNVERIFIED_API = (
    "base.CreatePart", "base.SetCreate", "base.SetAddEntity",
    "base.ChangeElementType", "base.SetVisibility", "base.Show", "base.Hide",
    "base.SelectNear", "base.SelectNeighb", "base.CalcMass",
)


def _resolve_entity_type(name: str | None, deck: int | None = None) -> str | None:
    """Map a caller-supplied type name to a real ANSA entity-type string.

    Deck-sensitive: the semantic name ``"nodes"``/``"GRID"``/``"NODE"`` resolves
    to ``GRID`` on NASTRAN and ``NODE`` elsewhere. Passing the wrong one returns
    0 entities without raising, so it is resolved here once, centrally.
    """
    if not name:
        return None
    key = str(name).strip().strip("'\"").upper().replace(" ", "_")
    if key in ("NODES", "GRID", "NODE"):
        target = api.normalize_deck(deck)
        return api.node_type_for(target)
    if key in SEMANTIC_TYPES:
        return SEMANTIC_TYPES[key]
    return ENTITY_TYPE_MAP.get(key, key)


def _deck(deck: Any) -> int:
    return api.normalize_deck(deck if isinstance(deck, int) else None)


def _count(deck: int, type_value: str) -> int:
    """Entity count. Raises on an invalid type string instead of returning 0.

    Returning 0 for a bad type string is exactly how the previous version
    reported "27763 shells but 0 nodes" on a NASTRAN model.
    """
    return api.count(deck, type_value)


def _count_safe(deck: int, type_value: str) -> tuple[int, str | None]:
    """Count with an explicit, reportable failure instead of a silent zero."""
    try:
        return api.count(deck, type_value), None
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _collect(deck: int, type_value: str, fields=None, limit: int | None = None):
    return api.collect(deck, type_value, limit)


def _ctx(deck: int) -> dict:
    """Deck context echoed in every response so callers can see what was assumed."""
    return {
        "deck": deck,
        "deck_name": api.deck_name(deck),
        "node_type": api.node_type_for(deck),
    }


def _card_values(deck: int, ent: Any, fields) -> dict:
    if not fields:
        return {}
    try:
        vals = api.resolve("base.GetEntityCardValues")(deck, ent, list(fields))
        if isinstance(vals, dict):
            return {str(k): _json_safe(v) for k, v in vals.items()}
    except Exception as exc:
        return {"_error": str(exc)}
    return {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _jsonable(obj: Any) -> Any:
    """Best-effort JSON coercion for arbitrary nested results."""
    try:
        json.dumps(obj, default=str)
        return obj
    except Exception:
        return str(obj)


# ===========================================================================
# SESSION & FILE I/O (8 new tools; ping_ansa handled by existing ping)
# ===========================================================================

def open_model(command: dict) -> dict:
    filepath = str(command["filepath"])
    rc = base.Open(filepath)
    return {"opened": rc == 0, "return_code": rc, "path": filepath}


def new_model(command: dict) -> dict:
    cleared = False
    err = ""
    for fn_name in ("Clear", "New"):
        fn = getattr(base, fn_name, None)
        if fn is None:
            continue
        try:
            rc = fn()
            cleared = (rc is None or rc == 0)
            if cleared:
                break
        except Exception as exc:
            err = repr(exc)
    return {"cleared": cleared, "error": err}


def save_model(command: dict) -> dict:
    rc = api.resolve("base.Save")()
    return {"saved": rc == 0, "return_code": rc,
            **({"error": f"base.Save returned {rc}", "ok": False} if rc != 0 else {})}


def _path_arg(command: dict, *keys: str) -> str:
    """Accept any of the historical key spellings for a file path.

    The server and the plugin previously disagreed (``output_path`` vs ``path``),
    which surfaced as ``KeyError: 'path'`` inside the dispatch loop.
    """
    for key in keys:
        value = command.get(key)
        if value:
            return str(value)
    raise KeyError(f"none of {keys} present in command; got keys={sorted(command)}")


def save_model_as(command: dict) -> dict:
    """Save the model under a new path."""
    path = _path_arg(command, "path", "filepath", "output_path")
    try:
        rc = api.resolve("base.SaveAs")(path, silent=True)
    except TypeError:
        rc = api.resolve("base.SaveAs")(path)
    return {"saved": rc == 0, "return_code": rc, "path": path,
            **({"error": f"base.SaveAs returned {rc}", "ok": False} if rc != 0 else {})}


def _export(solver: str, path: str) -> dict:
    key = str(solver).upper().replace(" ", "").replace("-", "")
    fn_name = SOLVER_OUTPUT_FN.get(key)
    if fn_name is None:
        return {"exported": False, "error": f"no ANSA output function for solver {solver}",
                "ok": False}
    fn = api.resolve(f"base.{fn_name}")
    deck = _deck(None)
    last_err = ""
    rc = None
    for args in ((path,), (deck, path)):
        try:
            rc = fn(*args)
            last_err = ""
            break
        except Exception as exc:
            last_err = repr(exc)
    ok = (rc == 0 or rc is None)
    out = {
        "solver": solver, "function": fn_name, "path": path,
        "return_code": rc, "exported": bool(ok),
    }
    if last_err:
        out["error"] = last_err
        out["ok"] = False
    elif not ok:
        out["error"] = f"base.{fn_name} returned {rc}"
        out["ok"] = False
    return out


def export_nastran(command: dict) -> dict:
    return _export("NASTRAN", str(command["path"]))


def export_lsdyna(command: dict) -> dict:
    return _export("LSDYNA", str(command["path"]))


def export_step(command: dict) -> dict:
    return _export("STEP", str(command["path"]))


def run_python_script_in_ansa(command: dict) -> dict:
    script = command["script"]
    fn_name = str(command.get("function_name") or "main")
    namespace: dict = {
        "__name__": "__user__", "ansa": ansa, "base": base,
        "mesh": mesh, "connections": connections, "constants": constants,
        "guitk": guitk, "json": json,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with __import__("contextlib").redirect_stdout(stdout), __import__("contextlib").redirect_stderr(stderr):
        exec(compile(script, "<user_script>", "exec"), namespace)
        if fn_name in namespace and callable(namespace[fn_name]):
            ret = namespace[fn_name]()
        else:
            return {"error": f"function {fn_name} not defined", "ok": False,
                    "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
    data = {}
    if isinstance(ret, dict):
        for k, v in ret.items():
            data[str(k)] = _json_safe(v)
    elif ret is not None:
        data["return"] = _json_safe(ret)
    data["stdout"] = stdout.getvalue()
    data["stderr"] = stderr.getvalue()
    return data


# ===========================================================================
# ENTITY QUERIES & EDITS (18)
# ===========================================================================

def count_entities(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    return {**_ctx(deck), "type": type_value,
            "requested_type": command.get("entity_type"),
            "count": _count(deck, type_value)}


def list_entities(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    fields = command.get("fields") or None
    limit = int(command.get("limit", 100))
    ents = _collect(deck, type_value, fields, limit)
    rows = {}
    for i, ent in enumerate(ents):
        row = {"_id": getattr(ent, "_id", i),
               "_name": getattr(ent, "_name", None)}
        if fields:
            row.update(_card_values(deck, ent, fields))
        rows[str(i)] = row
    return {**_ctx(deck), "type": type_value,
            "requested_type": command.get("entity_type"),
            "count": _count(deck, type_value), "returned": len(rows),
            "entities": rows}


def get_entity(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    eid = int(command["entity_id"])
    fields = command.get("fields") or None
    ent = api.resolve("base.GetEntity")(deck, type_value, eid)
    if ent is None:
        return {**_ctx(deck), "type": type_value, "entity_id": eid,
                "found": False,
                "error": f"{type_value} {eid} not found", "ok": False}
    return {**_ctx(deck), "type": type_value, "entity_id": eid, "found": True,
            "entity": _card_values(deck, ent, fields) if fields
            else {"_id": eid, "_name": getattr(ent, "_name", None)}}


def set_entity_fields(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    eid = int(command["entity_id"])
    fields = command["fields"]
    ent = api.resolve("base.GetEntity")(deck, type_value, eid)
    if ent is None:
        return {"error": f"{type_value} {eid} not found in deck {api.deck_name(deck)}",
                "ok": False, **_ctx(deck)}
    # SetEntityFields does not exist; SetEntityCardValues takes (deck, entity, dict).
    rc = api.resolve("base.SetEntityCardValues")(deck, ent, fields)
    return {**_ctx(deck), "type": type_value, "entity_id": eid,
            "updated": bool(rc), "return_code": rc}


def create_entity(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    fields = command.get("fields") or {}
    new = api.resolve("base.CreateEntity")(deck, type_value, fields)
    if new is None:
        return {"error": f"CreateEntity returned None for {type_value}",
                "ok": False, **_ctx(deck)}
    new_id = getattr(new, "_id", None)
    return {**_ctx(deck), "type": type_value, "entity_id": new_id, "created": True}


def delete_entities(command: dict) -> dict:
    """Delete entities by id.

    ``base.DeleteEntity`` takes *entities* (or a list) and returns 0 on success —
    the previous ``DeleteEntity(deck, type, id)`` call could not work at all.
    """
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    ids = [int(x) for x in command.get("entity_ids", [])]
    get_entity = api.resolve("base.GetEntity")
    ents, missing = [], []
    for eid in ids:
        ent = get_entity(deck, type_value, eid)
        if ent is None:
            missing.append(eid)
        else:
            ents.append(ent)
    deleted = 0
    rc = None
    if ents:
        rc = api.resolve("base.DeleteEntity")(ents, force=True)
        deleted = len(ents) if rc == 0 else 0
    return {**_ctx(deck), "type": type_value, "requested": len(ids),
            "deleted": deleted, "missing": missing, "return_code": rc,
            **({"error": f"DeleteEntity returned {rc}", "ok": False}
               if ents and rc != 0 else {})}


def search_entities_by_name(command: dict) -> dict:
    """Name search. Uses ``NameToEnts`` (PCRE pattern); ``SearchEntityByName`` does not exist."""
    deck = _deck(command.get("deck", -1))
    pattern = str(command["pattern"])
    match_mode = command.get("match", "substring")
    name_to_ents = api.resolve("base.NameToEnts")
    match_const = getattr(constants, {
        "regex": "ENM_REGEX", "exact": "ENM_EXACT",
        "substring": "ENM_SUBSTRING",
        "substring_ignorecase": "ENM_SUBSTRING_IGNORECASE",
    }.get(str(match_mode).lower(), "ENM_SUBSTRING"), None)
    ents = name_to_ents(pattern, deck, match_const)
    ents = list(ents) if ents else []
    return {**_ctx(deck), "pattern": pattern, "match": match_mode,
            "count": len(ents),
            "matches": [{"_id": getattr(e, "_id", None),
                         "_name": getattr(e, "_name", None)} for e in ents[:200]]}


def get_bounding_box(command: dict) -> dict:
    """Bounding box over nodes. ``entity_type`` is echoed for traceability.

    The box is always computed from node coordinates (the only entities that
    carry coordinate card values), so ``entity_type`` is informational — it is
    reported back rather than silently ignored.

    The field names are *discovered*, not assumed: see
    :data:`api.NODE_COORD_FIELD_CANDIDATES`. Asking a NASTRAN GRID for ``X/Y/Z``
    returns an empty dict, which the previous version turned into "no node
    carried X/Y/Z card values" — blaming the model for a wrong question.
    """
    deck = _deck(command.get("deck", -1))
    requested_type = command.get("entity_type")
    node_type = api.node_type_for(deck)
    nodes = api.collect(deck, node_type, limit=20000)
    if not nodes:
        return {"error": f"no {node_type} nodes found in deck {api.deck_name(deck)}",
                "ok": False, "nodes": 0, "requested_type": requested_type,
                **_ctx(deck)}
    fields = api.node_coord_fields(deck, nodes[0])
    if fields is None:
        tried = ", ".join("/".join(c) for c in api.NODE_COORD_FIELD_CANDIDATES)
        return {"error": (f"{len(nodes)} {node_type} entities on deck "
                          f"{api.deck_name(deck)} carried none of the candidate "
                          f"coordinate fields ({tried})"),
                "ok": False, "nodes": len(nodes), "requested_type": requested_type,
                "candidates_tried": [list(c) for c in api.NODE_COORD_FIELD_CANDIDATES],
                **_ctx(deck)}
    get_values = api.resolve("base.GetEntityCardValues")
    xs, ys, zs = [], [], []
    for n in nodes:
        try:
            v = get_values(deck, n, list(fields))
        except Exception:
            continue
        try:
            xs.append(float(v[fields[0]]))
            ys.append(float(v[fields[1]]))
            zs.append(float(v[fields[2]]))
        except Exception:
            continue
    if not xs:
        return {"error": f"{len(nodes)} {node_type} entities carried no values for "
                         f"{'/'.join(fields)}",
                "ok": False, "nodes": len(nodes), "coord_fields": list(fields),
                "requested_type": requested_type, **_ctx(deck)}
    return {**_ctx(deck), "requested_type": requested_type,
            "coord_fields": list(fields),
            "nodes_used": len(xs), "nodes_total": len(nodes),
            "min": [min(xs), min(ys), min(zs)],
            "max": [max(xs), max(ys), max(zs)]}


def get_node_coordinates(command: dict) -> dict:
    """Coordinates for specific node ids.

    ``GetEntity`` returns an *entity*, not a dict — the previous version passed
    a fields list as a 4th argument and then checked ``isinstance(ent, dict)``,
    so every lookup silently yielded ``None``.
    """
    deck = _deck(command.get("deck", -1))
    node_type = api.node_type_for(deck)
    get_entity = api.resolve("base.GetEntity")
    coords, missing = [], []
    for nid in [int(x) for x in command.get("node_ids", [])]:
        ent = get_entity(deck, node_type, nid)
        if ent is None:
            missing.append(nid)
            continue
        xyz = api.read_node_xyz(deck, ent)
        if xyz is None:
            # NOT [0.0, 0.0, 0.0]: an unreported origin is indistinguishable
            # from a real node sitting at the origin. The previous version
            # defaulted the card values to 0.0, so a wrong field name produced
            # confident coordinates for every node in the model.
            coords.append({"node_id": nid,
                           "error": "no coordinate card fields on this deck"})
            continue
        coords.append({"node_id": nid, "xyz": list(xyz)})
    return {**_ctx(deck), "node_type": node_type,
            "nodes": coords, "missing": missing}


def change_element_type(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    src = _resolve_entity_type(command.get("entity_type"), deck)
    dst = _resolve_entity_type(command.get("new_type"), deck)
    ids = [int(x) for x in command.get("entity_ids", [])]
    fn = api.resolve("base.ChangeElementType")
    changed, failed = 0, []
    for eid in ids:
        try:
            if fn(deck, src, dst, eid):
                changed += 1
            else:
                failed.append(eid)
        except Exception:
            failed.append(eid)
    return {**_ctx(deck), "from": src, "to": dst,
            "changed": changed, "requested": len(ids), "failed": failed[:200]}


def create_part(command: dict) -> dict:
    name = str(command["name"])
    pid = api.resolve("base.CreatePart")(name)
    return {"part_id": pid, "name": name, "created": pid is not None}


def create_set(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    name = str(command["name"])
    sid = api.resolve("base.SetCreate")(deck, name)
    return {**_ctx(deck), "set_id": sid, "name": name, "created": sid is not None}


def add_to_set(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    sid = int(command["set_id"])
    ids = [int(x) for x in command.get("entity_ids", [])]
    fn = api.resolve("base.SetAddEntity")
    added, failed = 0, []
    for eid in ids:
        try:
            if fn(deck, sid, type_value, eid):
                added += 1
            else:
                failed.append(eid)
        except Exception:
            failed.append(eid)
    return {**_ctx(deck), "set_id": sid, "added": added,
            "requested": len(ids), "failed": failed[:200]}


def get_model_summary(command: dict) -> dict:
    """Per-type entity counts plus the deck-correct node count.

    Failures are reported per type instead of being flattened to 0, so a wrong
    entity-type string is visible rather than looking like an empty model.
    """
    deck = _deck(command.get("deck", -1))
    counts, failures = {}, {}
    for t in sorted(set(ENTITY_TYPE_MAP.values())):
        value, err = _count_safe(deck, t)
        if err:
            failures[t] = err
        else:
            counts[t] = value
    nodes = api.count_nodes(deck)
    counts[nodes["entity_type"]] = nodes["count"]
    nonzero = {k: v for k, v in counts.items() if v}
    return {**_ctx(deck), "counts": counts, "nonzero": nonzero,
            "failures": failures,
            "nodes": nodes,
            "total_entities": sum(nonzero.values())}


def list_model_includes(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    incs = api.collect(deck, "INCLUDE", limit=500)
    get_values = api.resolve("base.GetEntityCardValues")
    rows = []
    for e in incs:
        row = {"_id": getattr(e, "_id", None), "_name": getattr(e, "_name", None)}
        try:
            v = get_values(deck, e, ["Name"])
            if isinstance(v, dict) and "Name" in v:
                row["Name"] = str(v["Name"])
        except Exception:
            pass
        rows.append(row)
    return {**_ctx(deck), "count": len(incs), "includes": rows}


def calc_element_mass(command: dict) -> dict:
    """Mass via ``CalcElementMass`` — which returns a 22-element list, not a scalar.

    The previous implementation did ``float(m)`` on that list, so a successful
    call could never have worked for any entity.
    """
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type"), deck)
    ids = command.get("entity_ids")
    get_entity = api.resolve("base.GetEntity")
    per, total, failed = [], 0.0, []
    for eid in [int(x) for x in (ids or [])]:
        ent = get_entity(deck, type_value, eid)
        if ent is None:
            failed.append(eid)
            continue
        mass, extra = api.element_mass(ent)
        per.append({"id": eid, "mass": mass, **extra})
        total += mass
    if not ids:
        ents = api.collect(deck, type_value, limit=20000)
        if not ents:
            return {"error": f"no {type_value} entities to measure", "ok": False,
                    **_ctx(deck)}
        mass, extra = api.element_mass(ents)
        return {**_ctx(deck), "scope": f"all {type_value}", "count": len(ents),
                "total_mass": mass, **extra}
    return {**_ctx(deck), "type": type_value, "total_mass": total,
            "per_element": per[:500], "failed": failed[:200]}


def calc_shell_area(command: dict) -> dict:
    """``CalcShellArea`` takes an ENTITY, not ``(deck, element_id)``."""
    deck = _deck(command.get("deck", -1))
    eid = int(command["element_id"])
    ent = api.resolve("base.GetEntity")(deck, "SHELL", eid)
    if ent is None:
        return {"error": f"SHELL {eid} not found", "ok": False, **_ctx(deck)}
    return {**_ctx(deck), "element_id": eid,
            "area": float(api.resolve("base.CalcShellArea")(ent))}


def calc_solid_volume(command: dict) -> dict:
    """``CalcSolidVolume`` takes an ENTITY, not ``(deck, element_id)``."""
    deck = _deck(command.get("deck", -1))
    eid = int(command["element_id"])
    ent = api.resolve("base.GetEntity")(deck, "SOLID", eid)
    if ent is None:
        return {"error": f"SOLID {eid} not found", "ok": False, **_ctx(deck)}
    return {**_ctx(deck), "element_id": eid,
            "volume": float(api.resolve("base.CalcSolidVolume")(ent))}


# ===========================================================================
# CHECKS & QUALITY (9)
# ===========================================================================

def _check_groups() -> list[tuple[str, Any]]:
    """Available ``ansa.base.checks.*`` sub-modules (discovered, not assumed)."""
    checks = getattr(base, "checks", None)
    if checks is None:
        return []
    out = []
    for name in dir(checks):
        if name.startswith("_"):
            continue
        mod = getattr(checks, name, None)
        if mod is not None and not callable(mod):
            out.append((name, mod))
    return out


def _find_check(keywords: Sequence[str]) -> tuple[str | None, list[str]]:
    """Locate a Check factory by keyword across ``ansa.base.checks.*``.

    ANSA exposes the model checks as *factory functions returning Check objects*
    that are then ``.execute()``-d. There is no ``base.CheckXxx()`` one-shot call,
    which is why the previous implementation's ``base.CheckIntersections`` etc.
    could not exist. This searches the real module namespace and reports the
    name it resolved to, instead of guessing one.
    """
    wanted = [k.lower() for k in keywords]
    candidates: list[str] = []
    for group, mod in _check_groups():
        for name in dir(mod):
            if name.startswith("_"):
                continue
            low = name.lower()
            if any(w in low for w in wanted):
                candidates.append(f"base.checks.{group}.{name}")
    if not candidates:
        return None, []
    # prefer the shortest match (most specific factory name)
    candidates.sort(key=len)
    return candidates[0], candidates


def _run_check(path: str, attributes: dict | None = None,
               entities=None, exec_mode: int | None = None) -> dict:
    """Instantiate a Check, apply attributes, execute, summarise."""
    group, _, name = path.rpartition(".")
    factory = api.resolve(path)
    check = factory()
    applied = {}
    for key, value in (attributes or {}).items():
        if hasattr(check, key):
            try:
                setattr(check, key, value)
                applied[key] = value
            except Exception as exc:
                applied[key] = f"FAILED: {exc}"
    kwargs = {}
    if exec_mode is not None:
        kwargs["exec_mode"] = exec_mode
    if entities:
        kwargs["entities"] = entities
    reports = check.execute(**kwargs) if kwargs else check.execute()
    items = []
    for rep in list(reports or [])[:50]:
        items.append({a: str(getattr(rep, a)) for a in
                      ("name", "title", "message", "errors", "warnings", "status")
                      if hasattr(rep, a)} or {"repr": str(rep)[:200]})
    return {"check": path, "attributes_applied": applied,
            "report_count": len(items), "reports": items}


def check_intersections(command: dict) -> dict:
    """Run the intersections check. Discovered from ``ansa.base.checks.*``."""
    path, candidates = _find_check(["intersection", "intersect", "collision"])
    if path is None:
        return {"error": "no intersections check found in ansa.base.checks.*",
                "ok": False, "searched": [g for g, _ in _check_groups()]}
    return {"fast": bool(command.get("fast", False)),
            "candidates": candidates[:10], **_run_check(path)}


def check_penetrations(command: dict) -> dict:
    """Run the penetrations check. ``auto_fix`` maps to the check's fix method."""
    path, candidates = _find_check(["penetration", "penetrat"])
    if path is None:
        return {"error": "no penetration check found in ansa.base.checks.*",
                "ok": False, "searched": [g for g, _ in _check_groups()]}
    attrs = {}
    if command.get("auto_fix"):
        attrs["fix_method"] = 2
    return {"auto_fix": bool(command.get("auto_fix")),
            "candidates": candidates[:10], **_run_check(path, attrs)}


def check_free_nodes(command: dict) -> dict:
    """Free/unconnected node check via ``ansa.base.checks.*``."""
    path, candidates = _find_check(["freenode", "free_node", "unconnected", "free"])
    if path is None:
        return {"error": "no free-node check found in ansa.base.checks.*",
                "ok": False, "searched": [g for g, _ in _check_groups()]}
    return {"candidates": candidates[:10], **_run_check(path)}


def run_quality_check(command: dict) -> dict:
    """Run the standard mesh quality check.

    The real entry point is ``base.checks.mesh.MeshQuality()`` returning a Check
    object, then ``.execute()``. ``base.RunQualityCheck`` does not exist.
    """
    deck = _deck(command.get("deck", -1))
    name = str(command.get("check_name") or "mesh_quality")
    if not api.has("base.checks.mesh.MeshQuality"):
        path, candidates = _find_check([name, "quality"])
        if path is None:
            return {"error": "no MeshQuality check available in this ANSA build",
                    "ok": False, **_ctx(deck)}
        return {**_ctx(deck), "check": path, "candidates": candidates[:10],
                **_run_check(path)}
    return {**_ctx(deck), "check": "base.checks.mesh.MeshQuality",
            **api.run_mesh_quality_check()}


def count_failed_elements(command: dict) -> dict:
    """Off-elements count via ``base.CalculateOffElements``.

    ``base.GetFailedEntitiesCount`` does not exist; and the old code returned a
    bare 0 for a missing API, which reads as "no failed elements".

    When ``entity_type`` is supplied the check is scoped to those entities
    (the server's ``entity_type`` argument is therefore meaningful, not ignored).
    """
    deck = _deck(command.get("deck", -1))
    details = bool(command.get("details", True))
    entity_type = _resolve_entity_type(command.get("entity_type"), deck)
    scope_note = None
    entities = None
    if entity_type:
        entities = api.collect(deck, entity_type, limit=200000)
        if not entities:
            return {"error": f"no {entity_type} entities to check", "ok": False,
                    **_ctx(deck)}
    result = api.off_elements(entities, details=details)
    payload = {**_ctx(deck), "type": entity_type, "scoped": entities is not None,
               "off_elements": result,
               "total_off": result.get("TOTAL OFF", result.get("TOTAL_OFF"))}
    if payload["total_off"] is None:
        payload["note"] = "no TOTAL OFF key; criteria names may differ in this build"
        payload["criteria"] = sorted(result.keys())
    if scope_note:
        payload["scope_note"] = scope_note
    return payload


def check_geometry(command: dict) -> dict:
    """Geometry check via ``ansa.base.checks.geometry``."""
    deck = _deck(command.get("deck", -1))
    path, candidates = _find_check(["geometry", "topology", "consistency", "duplicate"])
    if path is None:
        return {"error": "no geometry check found in ansa.base.checks.*",
                "ok": False, "searched": [g for g, _ in _check_groups()],
                **_ctx(deck)}
    return {**_ctx(deck), "candidates": candidates[:10], **_run_check(path)}


def check_sharp_edges(command: dict) -> dict:
    """Sharp-edge check; ``angle`` maps to the check's angle attribute."""
    deck = _deck(command.get("deck", -1))
    angle = float(command["angle"])
    path, candidates = _find_check(["sharp", "feature_line", "featureline"])
    if path is None:
        return {"error": "no sharp-edge check found in ansa.base.checks.*",
                "ok": False, "angle": angle, **_ctx(deck)}
    return {**_ctx(deck), "angle": angle, "candidates": candidates[:10],
            **_run_check(path, {"angle": angle, "feature_angle": angle})}


def check_rigid_dependencies(command: dict) -> dict:
    """Rigid-dependency check via ``ansa.base.checks.*``."""
    path, candidates = _find_check(["rigid", "rbe", "dependency", "dependenc"])
    if path is None:
        return {"error": "no rigid-dependency check found in ansa.base.checks.*",
                "ok": False}
    return {"candidates": candidates[:10], **_run_check(path)}


def calc_mesh_quality(command: dict) -> dict:
    """QCHECK metrics for a shell/solid selection.

    ``base.CalcQCHECK(ents, skewness, warping, aspect)`` is the real metric API;
    the previous ``base.CalcSkewness(deck, type)`` family does not exist.
    """
    deck = _deck(command.get("deck", -1))
    type_value = _resolve_entity_type(command.get("entity_type") or "SHELL", deck)
    ents = api.collect(deck, type_value, limit=20000)
    if not ents:
        return {"error": f"no {type_value} entities to evaluate", "ok": False,
                **_ctx(deck)}
    qcheck = api.resolve("base.CalcQCHECK")
    metrics = {
        "skewness": float(qcheck(ents, True, False, False)),
        "warping": float(qcheck(ents, False, True, False)),
        "aspect": float(qcheck(ents, False, False, True)),
        "total": float(qcheck(ents, True, True, True)),
    }
    out = {**_ctx(deck), "type": type_value, "count": len(ents), "qcheck": metrics}
    try:
        out["element_length"] = api.resolve(
            "base.CalculateAverageMinMaxElementLength")(ents) or {}
    except Exception as exc:
        out["element_length_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ===========================================================================
# MESH (5)
# ===========================================================================

def _find_api(module_path: str, keywords: Sequence[str]) -> tuple[str | None, list[str]]:
    """Locate a callable inside a real ANSA module by name keyword.

    Used where the documented surface offers several overlapping entry points
    whose exact names differ per build. The resolved name is always reported, so
    the caller can see what was actually called.
    """
    module = api.resolve(module_path)
    wanted = [k.lower() for k in keywords]
    hits = [f"{module_path}.{n}" for n in dir(module)
            if not n.startswith("_") and callable(getattr(module, n, None))
            and any(w in n.lower() for w in wanted)]
    hits.sort(key=len)
    return (hits[0] if hits else None), hits


#: Verified meshing algorithms. All operate on **visible** macros — there is no
#: per-entity meshing entry point in the documented API.
MESH_ALGORITHMS = {
    "best": "mesh.CreateBestMesh",
    "free": "mesh.CreateFreeMesh",
    "map": "mesh.CreateMapMesh",
    "advfront": "mesh.CreateAdvFrontMesh",
    "advancing_front": "mesh.CreateAdvFrontMesh",
    "cfd": "mesh.CreateCfdMesh",
    "stl": "mesh.CreateStlMesh",
}


def set_shell_mesh_params(command: dict) -> dict:
    """Set the target element length.

    ``mesh.SetShellMeshParams`` does not exist; ``SetMeshParamTargetLength``
    takes ``(function, value)`` where function is 'absolute' | 'init_local' |
    'average_length' | 'free'.
    """
    deck = _deck(command.get("deck", -1))
    length = float(command["length"])
    mode = str(command.get("mode") or "absolute").lower()
    if mode not in ("absolute", "init_local", "average_length", "free"):
        return {"error": f"invalid mode {mode!r}", "ok": False,
                "valid_modes": ["absolute", "init_local", "average_length", "free"],
                **_ctx(deck)}
    rc = api.resolve("mesh.SetMeshParamTargetLength")(mode, length)
    return {**_ctx(deck), "mode": mode, "target_length": length,
            "return_code": rc, "applied": rc == 1}


def mesh_shells(command: dict) -> dict:
    """Surface-mesh and report the measured delta.

    **Two modes, decided by whether the caller named entities:**

    * ``entity_ids`` given -> resolve them to FACE entities and call
      ``mesh.Mesh(faces)``, which *does* accept an explicit face list. This used
      to be refused ("cannot scope meshing with the verified API") on the theory
      that the ``Create*Mesh`` family only sees visible macros — true for that
      family, but ``mesh.Mesh`` is the exception and takes the faces directly.
    * no ``entity_ids`` -> ``mesh.Create*Mesh()`` on visible macros (whole model).

    Either way the result carries before/after counts, because the only honest
    success signal is "shells were actually created".
    """
    deck = _deck(command.get("deck", -1))
    length = float(command["length"])
    algorithm = str(command.get("algorithm") or "best").lower()
    entity_ids = [int(x) for x in command.get("entity_ids", [])]

    path = MESH_ALGORITHMS.get(algorithm)
    if path is None:
        return {"error": f"unknown algorithm {algorithm!r}", "ok": False,
                "valid_algorithms": sorted(MESH_ALGORITHMS), **_ctx(deck)}

    scoped = bool(entity_ids)
    entities: list = []
    missing_ids: list = []
    if scoped:
        # The ids are FACE ids here — faces are what gets meshed.
        face_type = "FACE"
        entities, missing_ids = api.resolve_entities(deck, face_type, entity_ids)
        if not entities:
            return {
                "error": "none of the requested entity ids resolved to a FACE",
                "ok": False,
                "requested": entity_ids[:50],
                "how_to_fix": "check the ids, or call list_entities(entity_type='FACE') first",
                **_ctx(deck),
            }

    shells_before = api.count(deck, "SHELL")
    faces_before = api.count(deck, "FACE")
    length_rc = api.resolve("mesh.SetMeshParamTargetLength")("absolute", length)

    if scoped:
        mesh_fn = api.resolve("mesh.Mesh")
        rc = mesh_fn(entities)
        used_api = "mesh.Mesh(faces)"
        note = ("mesh.Mesh uses the current mesh settings; the requested algorithm "
                f"{algorithm!r} is NOT honoured in scoped mode "
                "(set the generator via the GUI / defaults first)")
        warnings = [note]
    else:
        rc = api.resolve(path)()
        used_api = path
        warnings = []
        if not command.get("allow_full_model") and not entity_ids:
            warnings.append(
                "no entity_ids given -> this meshed every VISIBLE macro in the model"
            )

    shells_after = api.count(deck, "SHELL")
    added = shells_after - shells_before
    out = {
        **_ctx(deck), "algorithm": algorithm, "api": used_api,
        "scope": "explicit face list" if scoped else "visible macros (whole model)",
        "requested_entities": len(entity_ids),
        "resolved_entities": len(entities),
        "missing_ids": missing_ids[:50],
        "target_length": length, "target_length_return_code": length_rc,
        "faces_before": faces_before, "shells_before": shells_before,
        "shells_after": shells_after, "added": added,
        "return_code": rc, "meshed": added > 0,
    }
    if warnings:
        out["warnings"] = warnings
    if added <= 0:
        out["ok"] = False
        out.setdefault("warnings", []).append(
            "no new shells were created — check that the target faces exist / are visible"
        )
    return out


def mesh_volume(command: dict) -> dict:
    """Volume meshing.

    No generic volume-mesh entry point could be verified against the ANSA 25.1.4
    index (``mesh.MeshVolume`` does not exist). Rather than guess a name, this
    looks one up in the live module and fails loudly when there is none.

    ``entity_ids`` cannot be honoured (no verified per-entity scoping), so a
    non-empty list is refused unless ``allow_full_model`` is set — the same
    guardrail as :func:`mesh_shells`.
    """
    deck = _deck(command.get("deck", -1))
    entity_ids = [int(x) for x in command.get("entity_ids", [])]
    if entity_ids and not command.get("allow_full_model"):
        return {
            "error": "cannot scope volume meshing to specific entities with the verified API",
            "ok": False,
            "requested_entities": len(entity_ids),
            "how_to_fix": [
                "select the volumes in the ANSA GUI and click Run Once, then call again without entity_ids",
                "or pass allow_full_model=true to deliberately mesh everything visible",
            ],
            **_ctx(deck),
        }
    if not ANSA_AVAILABLE:
        raise api.ApiMissing("mesh.<volume-mesh>", "ANSA not importable")
    path, candidates = _find_api("mesh", ["volumemesh", "volume_mesh", "meshsolid", "meshsolids"])
    if path is None:
        return {"error": "no volume-meshing entry point found in ansa.mesh",
                "ok": False,
                "how_to_fix": "use run_batch_mesh with an ANSA batch-mesh script",
                "searched_module": "ansa.mesh", **_ctx(deck)}
    solids_before = api.count(deck, "SOLID")
    rc = api.resolve(path)()
    solids_after = api.count(deck, "SOLID")
    return {**_ctx(deck), "api": path, "candidates": candidates[:10],
            "solids_before": solids_before, "solids_after": solids_after,
            "added": solids_after - solids_before, "return_code": rc}


def delete_mesh(command: dict) -> dict:
    """Erase shell elements by id.

    ``mesh.DeleteMesh`` does not exist — deleting element entities is the real
    operation, via ``base.DeleteEntity(entities, force=True)`` (returns 0 on
    success).
    """
    deck = _deck(command.get("deck", -1))
    ids = {int(x) for x in command.get("entity_ids", [])}
    shells = [e for e in api.collect(deck, "SHELL", limit=200000)
              if getattr(e, "_id", None) in ids]
    if not shells:
        return {**_ctx(deck), "requested": len(ids), "found": 0, "deleted": 0,
                "note": "no SHELL entities matched those ids"}
    rc = api.resolve("base.DeleteEntity")(shells, force=True)
    deleted = len(shells) if rc == 0 else 0
    return {**_ctx(deck), "requested": len(ids), "found": len(shells),
            "deleted": deleted, "return_code": rc,
            **({"error": f"DeleteEntity returned {rc}", "ok": False} if rc != 0 else {})}


def run_batch_mesh(command: dict) -> dict:
    script_path = str(command["script_path"])
    if not os.path.isfile(script_path):
        return {"error": f"script not found: {script_path}", "ok": False}
    with open(script_path, "r", encoding="utf-8") as f:
        src = f.read()
    g = {"__name__": "__batch__", "base": base, "mesh": mesh,
         "connections": connections, "constants": constants, "json": json}
    exec(compile(src, script_path, "exec"), g)
    if "main" in g and callable(g["main"]):
        ret = g["main"]()
        if isinstance(ret, dict):
            return {k: _json_safe(v) for k, v in ret.items()}
        return {"script": script_path, "return": _json_safe(ret)}
    return {"script": script_path}


# ===========================================================================
# CONNECTIONS (3)
# ===========================================================================

#: Connection entity types, verified from the ANSA 25.1.4 connection API examples.
CONNECTOR_ENTITY_TYPE = "CONNECTOR_ENTITY"
CONNECTION_POINT_TYPES = (
    "SpotweldPoint_Type", "Bolt_Type", "GumDrop_Type", "Rivet_Type", "Screw_Type",
)
CONNECTION_LINE_TYPES = (
    "SpotweldLine_Type", "AdhesiveLine_Type", "SeamLine_Type", "Hemming_Type",
)


def apply_connectors(command: dict) -> dict:
    """Realise connectors into FE.

    No "apply connectors" entry point could be verified by name, so the live
    ``ansa.connections`` namespace is searched and the resolved name reported.
    ``connections.ApplyConnectors`` was confirmed absent.
    """
    deck = _deck(command.get("deck", -1))
    if not ANSA_AVAILABLE:
        raise api.ApiMissing("connections.<realise>", "ANSA not importable")
    before = api.count(deck, CONNECTOR_ENTITY_TYPE)
    path, candidates = _find_api(
        "connections", ["realize", "realise", "applyconnect", "apply_connect", "createconnector"]
    )
    if path is None:
        return {"error": "no connector-realisation entry point found in ansa.connections",
                "ok": False, "connectors": before,
                "how_to_fix": "use the Connections browser action, or a batch script via run_batch_mesh",
                **_ctx(deck)}
    rc = api.resolve(path)()
    after = api.count(deck, CONNECTOR_ENTITY_TYPE)
    return {**_ctx(deck), "api": path, "candidates": candidates[:10],
            "connectors_before": before, "connectors_after": after,
            "realized": max(0, after - before), "return_code": rc}


def check_connections(command: dict) -> dict:
    """Connectivity/mesh-compatibility check via ``ansa.base.checks.*``."""
    deck = _deck(command.get("deck", -1))
    if api.has("base.checks.mesh.MeshCompatibility"):
        return {**_ctx(deck), "check": "base.checks.mesh.MeshCompatibility",
                **_run_check("base.checks.mesh.MeshCompatibility")}
    path, candidates = _find_check(["connect", "compat", "coincid"])
    if path is None:
        return {"error": "no connection check found", "ok": False, **_ctx(deck)}
    return {**_ctx(deck), "check": path, "candidates": candidates[:10],
            **_run_check(path)}


def list_connectors(command: dict) -> dict:
    """List connector entities.

    The entity type is ``CONNECTOR_ENTITY`` — the old ``"CONNECTION"`` string
    matched nothing, so this tool always reported an empty model.
    """
    deck = _deck(command.get("deck", -1))
    limit = int(command.get("limit", 200))
    conns = api.collect(deck, CONNECTOR_ENTITY_TYPE, limit=limit)
    get_values = api.resolve("base.GetEntityCardValues")
    rows = []
    for e in conns:
        row = {"_id": getattr(e, "_id", None), "_name": getattr(e, "_name", None)}
        try:
            v = get_values(deck, e, ["Name", "Type"])
            if isinstance(v, dict):
                row.update({k: _json_safe(x) for k, x in v.items()})
        except Exception:
            pass
        rows.append(row)
    total, err = _count_safe(deck, CONNECTOR_ENTITY_TYPE)
    return {**_ctx(deck), "entity_type": CONNECTOR_ENTITY_TYPE,
            "count": total, "returned": len(rows), "connectors": rows,
            **({"count_error": err} if err else {})}


def create_connection_point(command: dict) -> dict:
    """Create a spotweld / bolt / gumdrop / rivet / screw connection point.

    Backed by ``connections.CreateConnectionPoint(type, position, id, connectivity)``
    — the documented entry point, and the one needed for automatically creating
    **Bolt** connections from a detection pass.

    ``connectivity`` accepts either raw connectivity strings or ANSA part
    entities' ids, which are converted with ``EntsToConnectivityString``.
    """
    deck = _deck(command.get("deck", -1))
    ctype = str(command.get("connection_type") or "Bolt_Type")
    if ctype not in CONNECTION_POINT_TYPES:
        return {"error": f"invalid connection_type {ctype!r}", "ok": False,
                "valid_types": list(CONNECTION_POINT_TYPES), **_ctx(deck)}
    position = [float(x) for x in command["position"]]
    if len(position) != 3:
        return {"error": "position must be [x, y, z]", "ok": False, **_ctx(deck)}
    cid = int(command.get("id") or 0)
    connectivity = list(command.get("connectivity") or [])
    converter = api.resolve("connections.EntsToConnectivityString")
    get_entity = api.resolve("base.GetEntity")
    resolved = []
    for item in connectivity:
        if isinstance(item, dict):
            ent = get_entity(deck, _resolve_entity_type(item.get("type"), deck),
                             int(item["id"]))
            if ent is None:
                return {"error": f"connectivity entity not found: {item}", "ok": False,
                        **_ctx(deck)}
            resolved.append(converter([ent]))
        else:
            resolved.append(str(item))
    if not resolved:
        return {"error": "connectivity is empty; a connection point needs the parts it joins",
                "ok": False, **_ctx(deck)}
    created = api.resolve("connections.CreateConnectionPoint")(
        ctype, position, cid, resolved)
    if created is None:
        return {"error": "CreateConnectionPoint returned None", "ok": False,
                "connection_type": ctype, **_ctx(deck)}
    new_id = getattr(created, "_id", cid)
    card = command.get("card_values") or {}
    card_rc = None
    if card:
        card_rc = api.resolve("base.SetEntityCardValues")(deck, created, card)
    return {**_ctx(deck), "connection_type": ctype, "entity_id": new_id,
            "position": position, "connectivity": resolved,
            "created": True, "card_values": card, "card_return_code": card_rc}


# ===========================================================================
# VISIBILITY (5)
# ===========================================================================

def _set_visibility(deck: int, entity_type: str, state: str) -> dict:
    """Toggle a deck-level visibility flag.

    ``base.SetEntityVisibilityValues(deck, {'SHELL': 'enable'|'off'})`` is the
    verified visibility API, and it works at *entity-type* granularity. There is
    no verified per-entity show/hide call, so ``entity_ids`` cannot be honoured.
    """
    fn = api.resolve("base.SetEntityVisibilityValues")
    rc = fn(deck, {entity_type: state})
    return {"return_code": rc, "applied": rc == 1}


def _visibility_unsupported(deck: int, entity_type: str, ids: list[int],
                            intent: str) -> dict:
    return {
        "error": f"per-entity visibility is not available via the verified ANSA API ({intent})",
        "ok": False,
        "entity_type": entity_type,
        "entity_ids_requested": len(ids),
        "why": "base.SetEntityVisibilityValues toggles deck-level flags only",
        "how_to_fix": [
            "select the entities in the ANSA GUI (they stay selected for the next Run Once)",
            "or use a batch script via run_batch_mesh",
        ],
        **_ctx(deck),
    }


def show_only(command: dict) -> dict:
    """Show only a type, or refuse honestly when specific ids were requested."""
    deck = _deck(command.get("deck", -1))
    entity_type = _resolve_entity_type(command.get("entity_type"), deck)
    ids = [int(x) for x in command.get("entity_ids", [])]
    if ids:
        return _visibility_unsupported(deck, entity_type, ids, "show_only")
    return {**_ctx(deck), "entity_type": entity_type,
            "action": "type visibility ON",
            **_set_visibility(deck, entity_type, "enable")}


def show_also(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    entity_type = _resolve_entity_type(command.get("entity_type"), deck)
    ids = [int(x) for x in command.get("entity_ids", [])]
    if ids:
        return _visibility_unsupported(deck, entity_type, ids, "show_also")
    return {**_ctx(deck), "entity_type": entity_type,
            "action": "type visibility ON",
            **_set_visibility(deck, entity_type, "enable")}


def hide(command: dict) -> dict:
    deck = _deck(command.get("deck", -1))
    entity_type = _resolve_entity_type(command.get("entity_type"), deck)
    ids = [int(x) for x in command.get("entity_ids", [])]
    if ids:
        return _visibility_unsupported(deck, entity_type, ids, "hide")
    return {**_ctx(deck), "entity_type": entity_type,
            "action": "type visibility OFF",
            **_set_visibility(deck, entity_type, "off")}


def _discover_selection_api(keywords: Sequence[str]) -> tuple[str | None, list[str]]:
    return _find_api("base", list(keywords))


def near(command: dict) -> dict:
    """Find entities within a radius of a seed set.

    ``base.SelectNear`` is not in the documented API; a live namespace search is
    performed and the resolved name reported instead of guessing.
    """
    deck = _deck(command.get("deck", -1))
    radius = float(command["radius"])
    entity_type = _resolve_entity_type(command.get("entity_type"), deck)
    seed = [int(x) for x in command.get("entity_ids", [])]
    path, candidates = _discover_selection_api(["selectnear", "near", "selectbyradius", "proximity"])
    if path is None:
        return {"error": "no near/radius selection API found in ansa.base", "ok": False,
                "radius": radius, "seed_count": len(seed), **_ctx(deck)}
    fn = api.resolve(path)
    get_entity = api.resolve("base.GetEntity")
    seeds = [e for e in (get_entity(deck, entity_type, i) for i in seed) if e is not None]
    try:
        found = fn(deck, entity_type, seeds, radius)
    except Exception:
        found = fn(radius, deck, entity_type, seeds)
    found = list(found) if found else []
    return {**_ctx(deck), "api": path, "candidates": candidates[:10],
            "radius": radius, "entity_type": entity_type,
            "seed_count": len(seeds), "found_count": len(found),
            "found_ids": [getattr(e, "_id", None) for e in found[:200]]}


def neighb(command: dict) -> dict:
    """Expand a selection by N connectivity hops."""
    steps = int(command["steps"])
    path, candidates = _discover_selection_api(["selectneighb", "neighb", "expandselection", "connectivity"])
    if path is None:
        return {"error": "no neighbour-expansion API found in ansa.base", "ok": False,
                "steps": steps}
    fn = api.resolve(path)
    try:
        found = fn(steps)
    except TypeError:
        found = fn(steps=steps)
    found = list(found) if found else []
    return {"api": path, "candidates": candidates[:10], "steps": steps,
            "count": len(found),
            "ids": [getattr(e, "_id", None) for e in found[:500]]}
