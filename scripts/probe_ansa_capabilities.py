# -*- coding: utf-8 -*-
"""ansa-mcp-sum — ANSA capability probe.

Runs INSIDE the ANSA host interpreter (GUI or batch) and emits a machine-readable
capability manifest to:

    <ANSA_MCP_SUM_HOME>/capabilities/cap_<YYYYmmdd-HHMMSS>.json

WHY THIS EXISTS
---------------
The MCP server exposes a *hand-written static* list of 49 tools. The real ANSA
API surface varies by version, licence module and active deck -- so tools can
reference functions that simply do not exist here (measured on ANSA 25.1.4:
`mesh.SetShellMeshParams`, `mesh.MeshShell`, `mesh.CountShellElements` are all
absent). A static tool table can never know that; a probe can.

The manifest is meant to be consumed by the MCP server to *prune* its tool
table at startup -- a tool whose backing API is missing should NOT be offered to
the model at all, instead of failing later with
`TypeError: 'NoneType' object is not callable`.

HOW TO RUN
----------
Option A (toolbar, recommended):
    copy this file next to the bridge, then in ANSA:
        ansa.ImportCode(r"D:\\ansa-mcp-sum\\scripts\\probe_ansa_capabilities.py")
    (or add it to the MCP-Sum toolbar in ansa_mcp_sum_autoload.py)

Option B (MCP tool):
    run_python_script_in_ansa(script=<this file's text>, function_name="main")

Option C (interactive console):
    exec(open(r"D:\\ansa-mcp-sum\\scripts\\probe_ansa_capabilities.py").read())

SAFETY
------
Read-only. Never mutates the model. Never calls GetEntityCardValues on bulk
entities (that is the known crash path). Every probe is individually
try/except-guarded, and every CollectEntities call is a count with a hard cap,
so a missing/renamed entity type degrades to an entry in `errors` rather than
taking the host down.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

PROBE_VERSION = "2.0"

#: ``key -> dotted path``. Every one of these was confirmed importable in
#: ANSA 25.1.4 by an earlier run of this probe (see capabilities.json/history).
MODULES_TO_PROBE = [
    ("ansa", "ansa"),
    ("base", "ansa.base"),
    ("base_checks_general", "ansa.base.checks.general"),
    ("base_checks_geometry", "ansa.base.checks.geometry"),
    ("base_checks_mesh", "ansa.base.checks.mesh"),
    ("base_checks_penetration", "ansa.base.checks.penetration"),
    ("mesh", "ansa.mesh"),
    ("batchmesh", "ansa.batchmesh"),
    ("connections", "ansa.connections"),
    ("session", "ansa.session"),
    ("constants", "ansa.constants"),
    ("guitk", "ansa.guitk"),
    ("analysis_tools", "ansa.analysis_tools"),
    ("betascript", "ansa.betascript"),
    ("cad", "ansa.cad"),
    ("calc", "ansa.calc"),
    ("dm", "ansa.dm"),
    ("kinetics", "ansa.kinetics"),
    ("morph", "ansa.morph"),
    ("report", "ansa.report"),
    ("spdrm", "ansa.spdrm"),
    ("sph", "ansa.sph"),
    ("taskmanager", "ansa.taskmanager"),
    ("utils", "ansa.utils"),
    ("vr", "ansa.vr"),
]

#: ``label -> (module_key, attr)``. Probed with hasattr + callable, never invoked.
#:
#: LABEL CONVENTION (used by the gap report):
#:   ``X``             -> a name the tools NEED
#:   ``REJECTED.X``    -> a name the tools must NOT use; resolving means the
#:                        fact layer (ansa_api.REJECTED_API) is stale
#:   ``CAND.X``        -> an open question, reported but not counted as required
FUNCTIONS_OF_INTEREST = {
    # --- base: model lifecycle / IO ---
    "base.Open": ("base", "Open"),
    "base.Save": ("base", "Save"),
    "base.SaveAs": ("base", "SaveAs"),
    "base.Clear": ("base", "Clear"),
    "base.CurrentDeck": ("base", "CurrentDeck"),
    "base.SetCurrentDeck": ("base", "SetCurrentDeck"),
    # --- base: entity query / edit (verified signatures) ---
    "base.CollectEntities": ("base", "CollectEntities"),
    "base.CollectEntitiesI": ("base", "CollectEntitiesI"),
    "base.GetEntity": ("base", "GetEntity"),
    "base.GetFirstEntity": ("base", "GetFirstEntity"),
    "base.GetEntityCardValues": ("base", "GetEntityCardValues"),
    "base.SetEntityCardValues": ("base", "SetEntityCardValues"),
    "base.CreateEntity": ("base", "CreateEntity"),
    "base.DeleteEntity": ("base", "DeleteEntity"),
    "base.NameToEnts": ("base", "NameToEnts"),
    "base.SetEntityVisibilityValues": ("base", "SetEntityVisibilityValues"),
    "base.NewPart": ("base", "NewPart"),
    "base.SetEntityPart": ("base", "SetEntityPart"),
    "base.GetEntityPart": ("base", "GetEntityPart"),
    "base.GetPartFromModuleId": ("base", "GetPartFromModuleId"),
    "base.GetEntityType": ("base", "GetEntityType"),
    # --- base: geometry inspection (all take ENTITIES) ---
    "base.GetFaceArea": ("base", "GetFaceArea"),
    "base.GetFaceOrientation": ("base", "GetFaceOrientation"),
    "base.EntityCenter": ("base", "EntityCenter"),
    "base.BoundBox": ("base", "BoundBox"),
    "base.SurfaceInfo": ("base", "SurfaceInfo"),
    "base.PerimetersOfFace": ("base", "PerimetersOfFace"),
    "base.DeleteFaces": ("base", "DeleteFaces"),
    # --- base: measurement ---
    "base.CalcShellArea": ("base", "CalcShellArea"),
    "base.CalcSolidVolume": ("base", "CalcSolidVolume"),
    "base.CalcElementMass": ("base", "CalcElementMass"),
    "base.CalcQCHECK": ("base", "CalcQCHECK"),
    "base.CalculateOffElements": ("base", "CalculateOffElements"),
    "base.CalculateAverageMinMaxElementLength": ("base", "CalculateAverageMinMaxElementLength"),
    # --- base: checks (the real quality-check entry points) ---
    "base.Check": ("base", "Check"),
    "base.Check.execute": ("base", "Check"),
    "base.ExecuteCheckTemplate": ("base", "ExecuteCheckTemplate"),
    "base.CheckIntersections": ("base", "CheckIntersections"),
    "base.checks.mesh.MeshQuality": ("base_checks_mesh", "MeshQuality"),
    "base.checks.mesh.MeshCompatibility": ("base_checks_mesh", "MeshCompatibility"),
    "base.checks.mesh.HangingEdges": ("base_checks_mesh", "HangingEdges"),
    "base.checks.mesh.FreeSolidFaces": ("base_checks_mesh", "FreeSolidFaces"),
    # --- base: geometry preparation (rewrite of the guessed Remove*/Midsurface names) ---
    "base.RemoveLogosAutomatic": ("base", "RemoveLogosAutomatic"),
    "base.FillHoleGeom": ("base", "FillHoleGeom"),
    "base.MiddleGeometry": ("base", "MiddleGeometry"),
    "base.CleanGeometry": ("base", "CleanGeometry"),
    "base.AutoSurfs": ("base", "AutoSurfs"),
    "base.CheckAndFixGeometry": ("base", "CheckAndFixGeometry"),
    "base.CalculateSolidThickness": ("base", "CalculateSolidThickness"),
    # --- base: solver deck output (NOTE: 1 == success here) ---
    "base.OutputNastran": ("base", "OutputNastran"),
    "base.OutputLSDyna": ("base", "OutputLSDyna"),
    "base.OutputAnsys": ("base", "OutputAnsys"),
    "base.OutputAbaqus": ("base", "OutputAbaqus"),
    "base.SetANSAdefaultsValues": ("base", "SetANSAdefaultsValues"),
    # --- mesh (verified) ---
    "mesh.SetMeshParamTargetLength": ("mesh", "SetMeshParamTargetLength"),
    "mesh.Mesh": ("mesh", "Mesh"),
    "mesh.CreateBestMesh": ("mesh", "CreateBestMesh"),
    "mesh.CreateFreeMesh": ("mesh", "CreateFreeMesh"),
    "mesh.CreateMapMesh": ("mesh", "CreateMapMesh"),
    "mesh.CreateAdvFrontMesh": ("mesh", "CreateAdvFrontMesh"),
    "mesh.CreateCfdMesh": ("mesh", "CreateCfdMesh"),
    "mesh.CreateStlMesh": ("mesh", "CreateStlMesh"),
    "mesh.SplitElements": ("mesh", "SplitElements"),
    "mesh.HexaBlockShellMesh": ("mesh", "HexaBlockShellMesh"),
    "mesh.NumberPerimeters": ("mesh", "NumberPerimeters"),
    "mesh.ReadQualityCriteria": ("mesh", "ReadQualityCriteria"),
    # --- connections (module is PLURAL) ---
    "connections.CreateConnectionPoint": ("connections", "CreateConnectionPoint"),
    "connections.CreateConnectionLine": ("connections", "CreateConnectionLine"),
    "connections.CreateConnectionFace": ("connections", "CreateConnectionFace"),
    "connections.EntsToConnectivityString": ("connections", "EntsToConnectivityString"),
    "connections.AutoCreateSeamlines": ("connections", "AutoCreateSeamlines"),
    "connections.AutoCreateConnectionChains": ("connections", "AutoCreateConnectionChains"),
    "connections.CreateConnectorFromInterfacePoints": ("connections", "CreateConnectorFromInterfacePoints"),
    # --- session / gui ---
    "session.ProgramArguments": ("session", "ProgramArguments"),
    "session.defbutton": ("session", "defbutton"),
    # ImportCode lives on the TOP-LEVEL `ansa` module. Probing it as
    # `ansa.session.ImportCode` is what produced a false NOT_FOUND last run.
    "ansa.ImportCode": ("ansa", "ImportCode"),
    "ansa.CompileScript": ("ansa", "CompileScript"),
    "guitk.BCWindowCreate": ("guitk", "BCWindowCreate"),
    "guitk.BCTimerCreate": ("guitk", "BCTimerCreate"),
    "guitk.BCTimerStart": ("guitk", "BCTimerStart"),
    # --- undo / history: the safety net the bridge does NOT have ---
    # Classified as REJECTED (not "required"): we KNOW it is absent, and the
    # point is to keep that fact visible. Putting it under `required` made
    # `requirements_met` permanently false, which hides real regressions.
    "REJECTED.base.Undo": ("base", "Undo"),
    # --- open questions: report them, do not treat as requirements ---
    "CAND.base.Hide": ("base", "Hide"),
    "CAND.base.Show": ("base", "Show"),
    "CAND.base.SetVisibility": ("base", "SetVisibility"),
    "CAND.base.HideEntity": ("base", "HideEntity"),
    "CAND.base.ShowEntity": ("base", "ShowEntity"),
    "CAND.base.SelectEntities": ("base", "SelectEntities"),
    "CAND.base.SetSelection": ("base", "SetSelection"),
    "CAND.base.GetSelection": ("base", "GetSelection"),
    "CAND.mesh.RemeshShells": ("mesh", "RemeshShells"),
    "CAND.mesh.MeshVolume": ("mesh", "MeshVolume"),
    "CAND.base.CastModel": ("base", "CastModel"),
    # --- names the tools must NOT rely on (kept so a build regression is visible) ---
    "REJECTED.base.GetEntityCount": ("base", "GetEntityCount"),
    "REJECTED.base.SearchEntityByName": ("base", "SearchEntityByName"),
    "REJECTED.base.SetEntityFields": ("base", "SetEntityFields"),
    "REJECTED.base.RunQualityCheck": ("base", "RunQualityCheck"),
    "REJECTED.base.GetFailedEntitiesCount": ("base", "GetFailedEntitiesCount"),
    "REJECTED.base.CalcMass": ("base", "CalcMass"),
    "REJECTED.base.RemoveLogos": ("base", "RemoveLogos"),
    "REJECTED.base.RemoveHoles": ("base", "RemoveHoles"),
    "REJECTED.base.RemoveFillets": ("base", "RemoveFillets"),
    "REJECTED.base.Midsurface": ("base", "Midsurface"),
    "REJECTED.session.ImportCode": ("session", "ImportCode"),
    "REJECTED.mesh.SetShellMeshParams": ("mesh", "SetShellMeshParams"),
    "REJECTED.mesh.MeshShell": ("mesh", "MeshShell"),
    "REJECTED.mesh.CountShellElements": ("mesh", "CountShellElements"),
    "REJECTED.mesh.DeleteMesh": ("mesh", "DeleteMesh"),
    "REJECTED.connections.ApplyConnectors": ("connections", "ApplyConnectors"),
}

# Candidate entity-type strings. Probed per-deck by COUNTING only.
# NASTRAN decks name nodes 'GRID'; LS-DYNA / Abaqus decks name them 'NODE' --
# this is the exact trap that makes get_model_info report 0 nodes.
CANDIDATE_ENTITY_TYPES = [
    # nodes / elements
    "GRID", "NODE", "ELEMENT", "SHELL", "SOLID", "BEAM", "TRIA3", "TRIA6",
    "QUAD4", "QUAD8", "HEXA", "PENTA", "TETRA", "SPRING", "MASS",
    # geometry
    "FACE", "SHELL_GEOMETRY", "EDGE", "SOLID_GEOMETRY", "VERTEX",
    # model organisation
    "PART", "SET", "PROPERTY", "MATERIAL", "INCLUDE", "FUNCTION",
    # nastran cards
    "PSHELL", "PSOLID", "PBAR", "MAT1", "MAT2", "PROD",
    # connections
    "CONNECTOR", "SPOTWELD", "BOLT", "GENERAL_CONNECTION",
]

MAX_SCRIPT_CHARS = 64 * 1024


def _home():
    return os.environ.get("ANSA_MCP_SUM_HOME") or os.path.join(
        os.path.expanduser("~"), ".ansa-mcp-sum"
    )


def _safe(fn, default=None):
    try:
        return fn(), None
    except Exception as exc:
        return default, "%s: %s" % (type(exc).__name__, exc)


def resolve_module(dotted):
    """Resolve an ``ansa.*`` module the way ANSA actually exposes it.

    Returns ``(module, strategy)`` on success or ``(None, error_text)``.

    WHY THIS IS NOT JUST ``importlib.import_module``
    ------------------------------------------------
    ANSA 25.1.4 registers its submodules as **attributes of the ``ansa``
    module**; ``ansa`` itself is not a package. So:

        import ansa.constants            -> ModuleNotFoundError: 'ansa' is not a package
        from ansa import constants       -> works (and is what ANSA's own
                                            documented examples use)

    The previous version of this probe used ``__import__(dotted, fromlist=['*'])``
    only, which reported ``ansa.constants`` and all four ``ansa.base.checks.*``
    modules as "unavailable" — a pure false negative that would have made the
    server prune tools that work perfectly well. Hence: try all three idioms and
    record which one won, so the manifest documents the real rule.
    """
    strategies = []

    # 1) ordinary import (works for genuinely packaged modules)
    try:
        import importlib
        return importlib.import_module(dotted), "importlib"
    except Exception as exc:
        strategies.append("importlib: %s: %s" % (type(exc).__name__, exc))

    # 2) ANSA's own idiom: from ansa import <sub>, then walk attributes
    if dotted.startswith("ansa."):
        parts = dotted.split(".")[1:]
        try:
            import ansa
            mod = ansa
            for part in parts:
                mod = getattr(mod, part)
            return mod, "from-ansa+attr-walk(%s)" % ".".join(parts)
        except Exception as exc:
            strategies.append("from-ansa: %s: %s" % (type(exc).__name__, exc))

    # 3) the plain import statement, as a user would type it in the console
    try:
        ns = {}
        exec(compile("import %s" % dotted, "<probe>", "exec"), ns)  # noqa: S102
        mod = ns.get(dotted.split(".")[0])
        for part in dotted.split(".")[1:]:
            mod = getattr(mod, part)
        return mod, "import-statement"
    except Exception as exc:
        strategies.append("import-statement: %s: %s" % (type(exc).__name__, exc))

    return None, " | ".join(strategies)


_MODULE_CACHE = {}


def _module(dotted):
    if dotted not in _MODULE_CACHE:
        _MODULE_CACHE[dotted] = resolve_module(dotted)
    return _MODULE_CACHE[dotted]


def _probe_modules(errors):
    out = {}
    for key, dotted in MODULES_TO_PROBE:
        mod, how = _module(dotted)
        if mod is None:
            out[key] = {"importable": False, "how": None, "error": how}
            errors.append("resolve %s -> %s" % (dotted, how))
            continue
        try:
            members = [n for n in dir(mod) if not n.startswith("_")]
            callables = []
            for name in members:
                try:
                    if callable(getattr(mod, name)):
                        callables.append(name)
                except Exception:
                    pass
            out[key] = {
                "importable": True,
                "how": how,
                "module": getattr(mod, "__name__", dotted),
                "callable_count": len(callables),
                "callables": sorted(callables),
            }
        except Exception as exc:
            out[key] = {"importable": True, "how": how, "error": str(exc)}
            errors.append("introspect %s -> %s" % (dotted, exc))
    return out


def _probe_functions(errors):
    """Self-contained: resolves each module itself, so a failed module stage
    cannot cascade into wrong function verdicts."""
    out = {}
    dotted_by_key = dict(MODULES_TO_PROBE)
    for label, (key, attr) in sorted(FUNCTIONS_OF_INTEREST.items()):
        dotted = dotted_by_key.get(key, key)
        mod, how = _module(dotted)
        if mod is None:
            out[label] = {"status": "MODULE_UNAVAILABLE", "error": how}
            continue
        try:
            exists = hasattr(mod, attr)
        except Exception as exc:
            out[label] = {"status": "PROBE_ERROR", "error": "%s: %s" % (type(exc).__name__, exc)}
            continue
        if not exists:
            out[label] = {"status": "NOT_FOUND"}
            continue
        obj = getattr(mod, attr)
        entry = {"status": "OK", "callable": callable(obj)}
        if not callable(obj):
            entry["status"] = "NOT_CALLABLE"
        # signature: helps validate kwargs before dispatch; may be unavailable
        try:
            import inspect
            entry["signature"] = str(inspect.signature(obj))
        except Exception as exc:
            entry["signature"] = None
            entry["signature_error"] = "%s: %s" % (type(exc).__name__, exc)
        out[label] = entry
    return out


def _probe_deck_context(errors):
    import ansa
    from ansa import base
    ctx = {}
    deck, err = _safe(base.CurrentDeck)
    if err:
        errors.append("CurrentDeck -> %s" % err)
    ctx["current_deck"] = deck
    # deck identifier may be an int, an object with .name, or a string
    try:
        name = getattr(deck, "name", None)
        if name is None:
            name = getattr(deck, "__name__", None)
        ctx["current_deck_name"] = name if name is not None else str(deck)
    except Exception:
        ctx["current_deck_name"] = str(deck)
    try:
        ctx["deck_type"] = str(type(deck).__name__)
    except Exception:
        pass
    # available decks, if the API exposes them
    for cand in ("GetDecks", "DeckList", "AvailableDecks", "GetDeckList"):
        if hasattr(base, cand):
            val, err = _safe(lambda f=getattr(base, cand): f())
            ctx["available_decks_via"] = cand
            if err is None:
                try:
                    ctx["available_decks"] = [str(x) for x in val][:32]
                except Exception:
                    ctx["available_decks"] = str(val)[:500]
            break
    return ctx


def _probe_entity_types(deck, errors):
    """Count-only probe. A type that does not exist on this deck -> error entry."""
    from ansa import base
    found = {}
    missing = {}
    for etype in CANDIDATE_ENTITY_TYPES:
        try:
            ents = base.CollectEntities(deck, None, etype, False)
            try:
                found[etype] = len(ents)
            except Exception:
                found[etype] = "collected(uncountable)"
        except Exception as exc:
            missing[etype] = "%s: %s" % (type(exc).__name__, exc)
    return {"valid_on_this_deck": found, "rejected": missing}


def _probe_gui():
    info = {"gui_available": None, "reason": None}
    try:
        import ansa
        from ansa import guitk
        # BCOnExitDestroy disposes of the window when the owning script ends, so
        # there is nothing to tear down by hand. (The old call here was
        # guitk.BCDestroyWindow inside a bare except - a name that does not exist
        # in 25.x, so it silently did nothing and hid the fact.)
        guitk.BCWindowCreate("__cap_probe__", guitk.constants.BCOnExitDestroy)
        info["gui_available"] = True
    except Exception as exc:
        info["gui_available"] = False
        info["reason"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def _probe_scripts_dir():
    try:
        import ansa_mcp_sum
        mod = os.path.dirname(os.path.abspath(ansa_mcp_sum.__file__))
        return {"importable": True, "path": mod}
    except Exception as exc:
        return {"importable": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def _stage(manifest, errors, name, fn, fallback=None):
    """Run one probe stage. A failing stage is recorded, never fatal.

    The whole point of this script is to characterise an environment we do not
    fully control, so partial results must survive: if the deck probe fails we
    still want the module/function inventory on disk.

    On failure the fallback CONTAINER is substituted unchanged (never merged
    with error text), so downstream code can keep its expected shape and the
    failure is reported only through `errors`.
    """
    try:
        manifest[name] = fn()
    except Exception as exc:
        errors.append("stage %s -> %s: %s" % (name, type(exc).__name__, exc))
        manifest[name] = fallback if fallback is not None else {}


def probe():
    errors = []

    manifest = {
        "probe_version": PROBE_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_at_epoch": time.time(),
        "home": _home(),
        "python": {
            "version": sys.version,
            "executable": getattr(sys, "executable", None),
            "sys_path_head": sys.path[:8],
        },
        "process": {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "program_arguments": None,
        },
        "ansa": {},
    }

    # --- version / process -------------------------------------------------
    try:
        import ansa
        manifest["ansa"]["module_file"] = getattr(ansa, "__file__", None)
        ver, err = _safe(lambda: ansa.session.ProgramArguments())
        if err is None:
            manifest["process"]["program_arguments"] = [str(a) for a in ver]
            toks = [str(a).strip().lower() for a in ver]
            manifest["process"]["batch_mode"] = ("-b" in toks or "nogui" in toks)
        else:
            errors.append("ProgramArguments -> %s" % err)
        for attr in ("__version__", "VERSION", "version"):
            if hasattr(ansa, attr):
                manifest["ansa"][attr] = str(getattr(ansa, attr))
    except Exception as exc:
        errors.append("import ansa -> %s" % exc)

    for key, dotted in (("base", "ansa.base"), ("mesh", "ansa.mesh"), ("session", "ansa.session")):
        try:
            mod = __import__(dotted, fromlist=["*"])
            for attr in ("__version__", "VERSION", "version"):
                if hasattr(mod, attr):
                    manifest["ansa"]["%s.%s" % (key, attr)] = str(getattr(mod, attr))
        except Exception:
            pass

    # --- script size budget ------------------------------------------------
    try:
        with open(__file__, "r", encoding="utf-8") as fh:
            n = len(fh.read())
        manifest["limits"] = {
            "this_script_chars": n,
            "max_script_chars_env": int(os.environ.get("ANSA_MCP_MAX_SCRIPT_CHARS", str(MAX_SCRIPT_CHARS))),
            "fits_script_budget": n <= int(os.environ.get("ANSA_MCP_MAX_SCRIPT_CHARS", str(MAX_SCRIPT_CHARS))),
        }
    except Exception:
        pass

    # --- modules / functions ----------------------------------------------
    _stage(manifest, errors, "modules", lambda: _probe_modules(errors), {})
    _stage(manifest, errors, "functions", lambda: _probe_functions(errors), {})

    # --- deck / entity types ----------------------------------------------
    _stage(manifest, errors, "deck", lambda: _probe_deck_context(errors), {})
    deck_ref = (manifest.get("deck") or {}).get("current_deck")
    _stage(
        manifest, errors, "entity_types",
        lambda: _probe_entity_types(deck_ref, errors),
        {},
    )

    # --- gui / bridge ------------------------------------------------------
    _stage(manifest, errors, "gui", _probe_gui, {"gui_available": None})
    _stage(manifest, errors, "bridge_package", _probe_scripts_dir, {})

    # --- capability gaps the MCP server should act on ----------------------
    funcs = manifest.get("functions") or {}
    mods = manifest.get("modules") or {}
    etypes = manifest.get("entity_types") or {}

    def _status(name):
        entry = funcs.get(name)
        return entry.get("status") if isinstance(entry, dict) else None

    required = {
        k: v for k, v in funcs.items()
        if not k.startswith("REJECTED.") and not k.startswith("CAND.")
    }
    rejected = {k: v for k, v in funcs.items() if k.startswith("REJECTED.")}
    candidates = {k: v for k, v in funcs.items() if k.startswith("CAND.")}

    missing = sorted(
        k for k, v in required.items()
        if isinstance(v, dict) and v.get("status") == "NOT_FOUND"
    )
    unavailable = sorted(
        k for k, v in mods.items() if isinstance(v, dict) and not v.get("importable")
    )
    unexpectedly_present = sorted(
        k for k, v in rejected.items()
        if isinstance(v, dict) and v.get("status") == "OK"
    )

    gaps = {
        # APIs the tools NEED. Anything here is a genuine gap.
        "missing_functions": missing,
        "unavailable_modules": unavailable,
        "non_callable": sorted(
            k for k, v in required.items()
            if isinstance(v, dict) and v.get("status") == "NOT_CALLABLE"
        ),
        "probe_errors": sorted(
            k for k, v in required.items()
            if isinstance(v, dict) and v.get("status") == "PROBE_ERROR"
        ),
        # APIs the tools must NOT use. Resolving means our facts are stale.
        "unexpectedly_present": unexpectedly_present,
        "rejected_entity_types": sorted((etypes.get("rejected") or {}).keys()),
        "undo_available": _status("REJECTED.base.Undo") == "OK",
        # Open questions: names we could not settle from the docs either way.
        "open_questions": {
            k[len("CAND."):]: (v.get("status") if isinstance(v, dict) else None)
            for k, v in sorted(candidates.items())
        },
        "verified_function_count": sum(
            1 for v in required.values()
            if isinstance(v, dict) and v.get("status") == "OK"
        ),
        "required_function_count": len(required),
        #: One boolean for the server: "can I trust the static tool table here?"
        "requirements_met": not missing and not unavailable and not unexpectedly_present,
    }
    manifest["capability_gaps"] = gaps

    # How each module had to be resolved — this is the rule, recorded from the
    # live interpreter rather than assumed.
    manifest["import_style"] = {
        key: (info.get("how") if isinstance(info, dict) else None)
        for key, info in sorted(mods.items())
    }
    manifest["errors"] = errors

    return manifest


def _write(manifest):
    home = _home()
    out_dir = os.path.join(home, "capabilities")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        out_dir = home
    label = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, "cap_%s.json" % label)
    latest = os.path.join(out_dir, "capabilities.json")
    blob = json.dumps(manifest, indent=2, ensure_ascii=False)
    for target in (path, latest):
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(blob + "\n")
        os.replace(tmp, target)
    return path, latest


def main():
    manifest = None
    try:
        manifest = probe()
    except Exception:
        manifest = {
            "probe_version": PROBE_VERSION,
            "fatal": True,
            "traceback": traceback.format_exc(),
        }
    written = None
    latest = None
    write_error = None
    try:
        written, latest = _write(manifest)
    except Exception as exc:
        write_error = "%s: %s" % (type(exc).__name__, exc)

    summary = {
        "ok": bool(manifest) and not manifest.get("fatal") and write_error is None,
        "manifest": written,
        "latest": latest,
        "write_error": write_error,
    }
    if manifest and not manifest.get("fatal"):
        summary["deck"] = manifest.get("deck", {}).get("current_deck_name")
        summary["gui_available"] = manifest.get("gui", {}).get("gui_available")
        gaps = manifest.get("capability_gaps", {})
        summary["requirements_met"] = gaps.get("requirements_met")
        summary["missing_functions"] = gaps.get("missing_functions")
        summary["unavailable_modules"] = gaps.get("unavailable_modules")
        summary["unexpectedly_present"] = gaps.get("unexpectedly_present")
        summary["open_questions"] = gaps.get("open_questions")
        summary["rejected_entity_types_count"] = len(gaps.get("rejected_entity_types") or [])
        summary["error_count"] = len(manifest.get("errors") or [])
        # Surface the import rule the interpreter actually needed, since that is
        # the one thing the *server* side cannot guess from outside ANSA.
        styles = manifest.get("import_style") or {}
        summary["import_style_broken"] = sorted(
            k for k, v in styles.items() if v is None
        )

    print("RESULT " + json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    main()
