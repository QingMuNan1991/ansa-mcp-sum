# -*- coding: utf-8 -*-
"""Machine-readable version of the lessons that cost real time to learn.

The problem this solves
-----------------------
Six hard-won facts lived in ``README.md`` §7 — that ``MidSurfAuto`` silently
ignores keyword arguments, that ``CheckReport.try_fix()`` marks a report fixed
without touching geometry, that a wrong ``DECK_NODE_TYPE`` returns 0 instead of
raising. An MCP client never reads a repository's README. So the model that is
supposed to drive ANSA was, in practice, unware of every one of them, and each
new session re-paid the cost of discovering them.

BETA's own AI-Assistant closes this with a RAG index over the full product
documentation plus the Python API reference. The equivalent for a
single-operator toolchain is this: the same knowledge, written once as data,
exposed over MCP as ``ansa://pitfalls`` and ``ansa://workflows`` — and quoted
inline in the docstrings of the tools it affects, because a resource only
reaches the model if the client bothers to fetch it.

Keeping it honest
-----------------
Prose drifts. Every entry therefore carries ``must_resolve``: the API names it
asserts *do* exist. ``scripts/regression_check.py`` resolves each of them
against ``ansa_api.VERIFIED_API``, so moving a name to ``REJECTED_API`` (or
deleting it) breaks the build instead of leaving a confident comment that is
no longer true. The reverse is covered too — a pitfall may not assert an API
that the fact layer says is absent.

``verified_on`` records when and against what each lesson was measured. Nothing
here is copied from a manual; all of it is the result of a run.
"""
from __future__ import annotations

import json
from typing import Any

#: The ANSA build every entry below was measured on.
VERIFIED_ON = "ANSA 25.1.4 / NASTRAN deck"

#: Where the human-readable version lives, so the two can be diffed by eye.
SOURCE = "README.md §7"

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


PITFALLS: list[dict[str, Any]] = [
    {
        "id": "midsurfauto-positional-only",
        "severity": "critical",
        "title": "base.MidSurfAuto() ignores keyword arguments — and reports success anyway",
        "symptom": (
            "The call returns 0 in ~0.0 s and the model is completely unchanged: "
            "no SHELL entities, no new FACE, identical counts before and after. "
            "Nothing raises, so a naive 'did it work?' check says yes."
        ),
        "cause": (
            "The 25.1.4 binding accepts only positional arguments. Passing "
            "thick=, faces=, length= by keyword is accepted by Python and then "
            "dropped on the floor by the wrapper."
        ),
        "correct_usage": (
            "base.MidSurfAuto(1.0, faces, False, False, 3.0)  "
            "# positional: (thick, faces, exact_middle, connect_weldings, length)"
        ),
        "guard": (
            "Judge success by elapsed time AND the SHELL count, never by the "
            "return value alone. A run that finishes in 0.0 s did not run."
        ),
        "apis": ["base.MidSurfAuto", "base.CollectEntities"],
        "must_resolve": ["base.MidSurfAuto"],
        "ref": "README §7.2",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "try-fix-does-not-fix-geometry",
        "severity": "critical",
        "title": "CheckReport.try_fix() marks the report fixed; it does not fix geometry",
        "symptom": (
            "After try_fix() the check flips from status=error to status=ok with "
            "0 issues and has_fix=False — so the geometry 'passes'. A controlled "
            "A/B run shows every geometric statistic is bit-identical: same min "
            "area (0.007 mm²), same 154 faces under 1 mm², same 177 CONS, same "
            "total area (196074.8). Face ids change (8821/9280 vanish, 1/2 "
            "appear), which is what makes it *look* like work happened."
        ),
        "cause": (
            "try_fix() deletes the flagged faces and recreates them, then sets "
            "is_fixed. The replacement surface is governed entirely by the same "
            "CONS, so it comes back the same shape. "
            "FillHoleGeom(..., always_produce_new_faces=False) behaves the same "
            "way: the surface cannot extend into a gap whose boundary is fixed."
        ),
        "correct_usage": (
            "Treat CheckReport.issues -> entities as the finding. The faces "
            "ProblematicSurfaces reports are the *necessary transition surfaces* "
            "between adjacent faces, not redundant slivers."
        ),
        "guard": (
            "Never report 'geometry fixed' from try_fix(). Verify with external "
            "evidence: free-edge count, face count, area distribution. With "
            "downstream mid-surface quality already at 98.9 %, forcing geometry "
            "here is high risk and low reward."
        ),
        "apis": ["base.checks.geometry.ProblematicSurfaces", "base.FillHoleGeom",
                 "base.BoundBox", "base.GetFaceArea"],
        "must_resolve": ["base.FillHoleGeom", "base.GetFaceArea", "base.BoundBox"],
        "ref": "README §7.3",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "return-value-conventions",
        "severity": "high",
        "title": "Success values are not consistent across the API — 0 for some families, 1 for others",
        "symptom": (
            "Testing `ret == 0` makes every successful export emit a warning; "
            "testing `ret == 1` makes every successful save look like a failure."
        ),
        "cause": (
            "Three families disagree. Lifecycle/IO (Open, Save, SaveAs, "
            "SetANSAdefaultsValues, DeleteEntity) return 0 on success. Export "
            "writers (OutputNastran, OutputLSDyna, OutputAnsys, OutputAbaqus) "
            "and mesh.Mesh / mesh.CreateBestMesh return 1 on success."
        ),
        "correct_usage": (
            "Read the per-function truth from ansa_api.VERIFIED_API rather than "
            "assuming a convention. Where the return value is documented as "
            "'0 in all cases' (e.g. FillHoleGeom), pick success criteria that "
            "come from the model instead."
        ),
        "guard": (
            "A result number is only meaningful next to the family it came from. "
            "Anything that reports success must name the function it judged."
        ),
        "apis": ["base.Open", "base.Save", "base.SaveAs", "base.DeleteEntity",
                 "base.SetANSAdefaultsValues", "base.OutputNastran",
                 "base.OutputLSDyna", "base.OutputAnsys", "base.OutputAbaqus",
                 "mesh.Mesh", "mesh.CreateBestMesh"],
        "must_resolve": ["base.Open", "base.Save", "base.SaveAs",
                         "base.DeleteEntity", "base.OutputNastran",
                         "base.OutputLSDyna", "mesh.Mesh"],
        "ref": "README §7.1",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "off-elements-depend-on-criteria",
        "severity": "high",
        "title": "base.CalculateOffElements() returns a count with no meaning unless the criteria state is reported too",
        "symptom": (
            "The same mesh reports 'TOTAL OFF: 0' and looks perfect, then "
            "reports thousands after a criteria file is loaded — with nothing "
            "about the mesh changing."
        ),
        "cause": (
            "The number is 'how many elements violate the quality criteria "
            "currently loaded'. With every criterion OFF it is identically 0."
        ),
        "correct_usage": (
            "base.CalculateOffElements(details=True), and always pair the number "
            "with the active criteria (name / min length / max length / aspect "
            "ratio / warping / min-max angle) read back from the loaded .ansa_qual."
        ),
        "guard": (
            "Never quote an off-element count on its own. 0 is not 'clean', it "
            "is 'not measured'."
        ),
        "apis": ["base.CalculateOffElements", "base.CalcQCHECK",
                 "mesh.ReadQualityCriteria"],
        "must_resolve": ["base.CalculateOffElements", "base.CalcQCHECK"],
        "ref": "README §7.2",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "deck-node-type-trap",
        "severity": "high",
        "title": "The node entity type depends on the deck, and a wrong guess fails silently",
        "symptom": (
            "A node lookup returns 0 results — no exception, no warning — so an "
            "empty answer is indistinguishable from 'this model has no nodes'."
        ),
        "cause": (
            "NASTRAN calls them GRID; LSDYNA and ABAQUS call them NODE. Passing "
            "the wrong string is not validated."
        ),
        "correct_usage": (
            "Resolve through the fact layer: ansa_api.node_type_for(deck), which "
            "consults DECK_NODE_TYPE. 'nodes' as a semantic type is resolved for "
            "you by tools_impl._resolve_entity_type."
        ),
        "guard": (
            "Report the deck *name* alongside any count taken with a type string "
            "(status.json already does this), so a mismatch is visible."
        ),
        "apis": ["base.CollectEntities", "base.CurrentDeck"],
        "must_resolve": ["base.CollectEntities", "base.CurrentDeck"],
        "ref": "README §7.4",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "getentitycardvalues-without-fields",
        "severity": "critical",
        "title": "base.GetEntityCardValues(deck, entity) without a fields tuple can kill the ANSA process",
        "symptom": (
            "ANSA disappears outright. No result lands in results/, status.json's "
            "timestamp freezes at the start time, and the Windows event log "
            "records no crash. There is nothing to read afterwards."
        ),
        "cause": (
            "Partially located. Batch-calling it on FACE entities whose fields do "
            "not match, without passing the field tuple, is the suspected trigger. "
            "The same script completes and reproduces cleanly once four call "
            "classes are excluded: this one, "
            "CheckDescription.read_descriptions(), Check.parameters(), and "
            "base.Or()/RedrawAll() visibility batches."
        ),
        "correct_usage": (
            "Always pass an explicitly defined, semantically matching field tuple "
            "and wrap the call in try/except. Do not loop this call over a large "
            "unfiltered entity set."
        ),
        "guard": (
            "Because a crash erases the evidence, work one call at a time and "
            "confirm status.json's timestamp advanced between steps. "
            "logs/audit.jsonl is what makes the crashed command identifiable "
            "after the fact — it is the only record that survives."
        ),
        "apis": ["base.GetEntityCardValues"],
        "must_resolve": ["base.GetEntityCardValues"],
        "ref": "README §7.6",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "fact-layer-no-getattr-probe",
        "severity": "medium",
        "title": "Never probe an API with getattr(module, name, None)",
        "symptom": (
            "A missing function turns into "
            "TypeError: 'NoneType' object is not callable — raised halfway "
            "through an operation, after the model has already been modified."
        ),
        "cause": (
            "The probe makes 'typo' and 'not in this build' look identical, and "
            "it defers the failure to the call site."
        ),
        "correct_usage": (
            "Resolve through ansa_api.resolve(path), which raises ApiMissing with "
            "the reason. For attributes that genuinely vary by build, register "
            "the name in ansa_api.OPTIONAL_API and use resolve_optional()."
        ),
        "guard": (
            "OPTIONAL_API is a registry, not a habit: a name that is not in it "
            "still raises. The escape hatch must not become the default path."
        ),
        "apis": [],
        "must_resolve": [],
        "ref": "README §7.4",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "heartbeat-timestamps-are-throttled",
        "severity": "medium",
        "title": "A stale status.json timestamp does not mean the bridge is dead",
        "symptom": (
            "status.json has not moved in 30 s, so the bridge is declared dead — "
            "but it is healthy and idle."
        ),
        "cause": (
            "Idle heartbeats are throttled to one write per 5 s on purpose: the "
            "GUI timer fires every 200 ms, and writing on every tick meant 18,000 "
            "writes an hour for a file whose content is 'nothing happened'."
        ),
        "correct_usage": (
            "Judge liveness with the ping tool. Read status.json for *state* "
            "(bridge_state, current_command_id, queue_depth), not for freshness."
        ),
        "guard": (
            "ANSA_MCP_HEARTBEAT_STALE_SECONDS is deliberately huge in on-demand "
            "bridge mode for exactly this reason."
        ),
        "apis": [],
        "must_resolve": [],
        "ref": "README §7.5",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "stale-commands-are-dropped",
        "severity": "medium",
        "title": "A queued command older than timeout + 60 s is deleted before it ever runs",
        "symptom": (
            "A command vanishes: it is not in commands/, never appears in "
            "results/, and the client just waits out its timeout."
        ),
        "cause": (
            "cleanup_stale_commands() drops anything older than "
            "STALE_COMMAND_AGE_SECONDS (= client timeout + 60 s) on the next poll. "
            "This is deliberate: after a crash-and-restart it stops ANSA from "
            "replaying the command that killed it."
        ),
        "correct_usage": (
            "Set the client timeout below the staleness limit (the default "
            "configuration does), and reconcile a timeout via results/<id>.json "
            "before retrying."
        ),
        "guard": (
            "Every drop now writes a dropped_stale entry to logs/audit.jsonl, so "
            "'it never ran' is an answerable question rather than a suspicion."
        ),
        "apis": [],
        "must_resolve": [],
        "ref": "README §7.5",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "timeout-does-not-cancel",
        "severity": "high",
        "title": "A client-side timeout does not cancel the command inside ANSA",
        "symptom": (
            "The tool returns a Timeout error, the operator retries, and the "
            "change is applied twice."
        ),
        "cause": (
            "The bridge has no cancellation channel: the plugin is already "
            "executing. The operation may finish after the client gave up."
        ),
        "correct_usage": (
            "Read the command_id from the timeout payload, then check "
            "results/<command_id>.json and logs/audit.jsonl before deciding to "
            "retry. Do not retry a mutating command on timeout without checking."
        ),
        "guard": (
            "Timeout payloads carry command_id, result_path, queue_depth, "
            "bridge_state and an explicit 'does NOT cancel' warning."
        ),
        "apis": [],
        "must_resolve": [],
        "ref": "README §7.5 / review finding 3.1",
        "verified_on": VERIFIED_ON,
    },
    {
        "id": "nastran-grid-coords-are-x1x2x3",
        "severity": "critical",
        "title": "NASTRAN GRID coordinates live in X1/X2/X3 — asking for X/Y/Z returns {} and no error",
        "symptom": (
            "get_bounding_box(deck=1, entity_type='GRID') fails with 'no node "
            "carried X/Y/Z card values' on a model holding 16 381 visible GRID "
            "nodes. get_node_coordinates is worse: it answers xyz=[0.0, 0.0, "
            "0.0] for every node — a confident, wrong answer that looks like "
            "real data and cannot be told from a node at the origin."
        ),
        "cause": (
            "GetEntityCardValues answers an unknown field name with an empty "
            "dict instead of raising, and ANSA names these fields per deck: "
            "NASTRAN's GRID exposes X1/X2/X3, the classic decks expose X/Y/Z. "
            "A hardcoded ('X','Y','Z') therefore fails silently on every "
            "NASTRAN model, which is the majority of the tutorial material."
        ),
        "correct_usage": (
            "base.GetEntityCardValues(constants.NASTRAN, grid, ['X1','X2','X3']) "
            "-> {'X1': -418.06424, 'X2': 244.09517, 'X3': 51.38365}; the same "
            "call with ['X','Y','Z'] -> {}. Prefer the runtime discovery helpers "
            "ansa_api.node_coord_fields(deck, sample) and read_node_xyz()."
        ),
        "guard": (
            "Never default a missing card value to 0.0 — 'no value' and 'the "
            "value is zero' are different facts, and collapsing them turns an "
            "unreported origin into a measurement."
        ),
        "apis": ["base.GetEntityCardValues", "base.CollectEntities"],
        "must_resolve": ["base.GetEntityCardValues"],
        "ref": "live probe 2026-09-13 (initial.ansa, ANSA 25.1.4)",
        "verified_on": VERIFIED_ON,
    },
]


WORKFLOWS: list[dict[str, Any]] = [
    {
        "id": "model-diagnosis",
        "name": "Model diagnosis (read-only)",
        "when": (
            "First contact with an unfamiliar .ansa file, and again before any "
            "geometry is touched. Cheap, and everything downstream depends on it."
        ),
        "cost": "~5 s on a 13k-face casting",
        "risk": "none (read-only)",
        "steps": [
            {
                "n": 1,
                "do": "Determine the deck and count every container",
                "api": "base.CurrentDeck(); base.CollectEntities(deck, None, T, False)",
                "types": ["FACE", "CONS", "SOLID", "VOLUME", "SHELL", "ANSAPART",
                          "PROPERTY", "MATERIAL", "SET"],
                "tool": "get_capabilities / count_entities / get_model_summary",
            },
            {
                "n": 2,
                "do": "Bounding box and overall size",
                "api": "base.BoundBox(faces) -> [xmin,ymin,zmin,xmax,ymax,zmax]",
                "tool": "get_bounding_box",
            },
            {
                "n": 3,
                "do": "Is this already a mid-surface or still a solid description?",
                "api": ("base.DetectSolidDescription(faces, fix_unchecked_faces=False); "
                        "then with thickness=1.0, return_percentage=True to get the "
                        "share of faces at the nominal thickness"),
                "note": "The percentage is what tells you which thickness to target.",
            },
            {
                "n": 4,
                "do": "Wall-thickness distribution, grouped by connectivity",
                "api": ("base.DetectSolidDescription(faces, estimate_thickness=True, "
                        "separate_connectivity=True, "
                        "separate_connectivity_stop_at_pid_bound=True, "
                        "estimate_thickness_num_decimals=2, fix_unchecked_faces=False)"),
                "note": ("Casting_initial.ansa measured 1.86-3.88 mm, which is why "
                         "the mid-surface target is 3.0 and not the nominal 1.0."),
            },
        ],
        "script": "scripts/s2_diag.py",
    },
    {
        "id": "geometry-health-check",
        "name": "Geometry health check",
        "when": "After diagnosis, before mid-surface extraction.",
        "cost": "~40 s for the full candidate sweep",
        "risk": "none, PROVIDED the four crash-path calls stay excluded (see pitfalls)",
        "steps": [
            {
                "n": 1,
                "do": "Run the check classes and collect reports",
                "api": ("cls(); obj.execute(exec_mode=base.Check.EXEC_ON_SELECTED, "
                        "entities=FACES)"),
                "candidates": [
                    "geometry.NeedleFaces", "geometry.NeedleAreas",
                    "geometry.OverlapFaces", "geometry.ProblematicSurfaces",
                    "geometry.Cracks", "geometry.CollapsedCons",
                    "geometry.SingleCons", "geometry.TripleCons",
                    "geometry.UncheckedFaces", "geometry.unmeshedMacros",
                    "penetration.Intersections", "penetration.InteriorIntersections",
                    "penetration.Proximities", "penetration.PenParametric",
                    "penetration.PropertyThickness", "penetration.UserThickness",
                    "general.Connectivity", "general.FreeNodes",
                    "general.UnconnectedRegions", "general.Geometry",
                ],
                "tool": "check_geometry / check_sharp_edges / check_free_nodes",
            },
            {
                "n": 2,
                "do": "Extract the offending faces from report issues",
                "api": ("report.issues[*].entities — the only path that was verified "
                        "safe; do NOT call CheckReport.try_fix() and expect geometry "
                        "to change"),
                "note": "Per-face facts: base.GetFaceArea, base.PerimetersOfFace, base.EntityCenter, base.BoundBox",
            },
            {
                "n": 3,
                "do": "Decide whether to act",
                "method": ("Compare the issue codes against the downstream "
                           "mid-surface score. Faces reported by "
                           "ProblematicSurfaces are necessary transition "
                           "surfaces, so if that score is already >= ~98 %, the "
                           "decision is to leave the geometry alone."),
                "note": ("This step has no API on purpose: the decision is a "
                         "judgement about whether the finding matters, and "
                         "there is nothing to call."),
            },
        ],
        "excluded_calls": [
            "base.GetEntityCardValues(deck, face) without a fields tuple",
            "base.CheckDescription.read_descriptions()",
            "Check.parameters()",
            "base.Or() / base.RedrawAll() visibility batches",
        ],
        "script": "scripts/s3_geomcheck.py",
    },
    {
        "id": "load-standards",
        "name": "Load mesh parameters and quality criteria (with before/after proofs)",
        "when": "Before meshing. Only if the model is not already carrying the project standards.",
        "cost": "~2 s",
        "risk": "low — it changes settings, not geometry",
        "steps": [
            {
                "n": 1,
                "do": "Snapshot the current settings",
                "api": "base.BCSettingsGetValues(KEYS)",
                "keys": ["mesh_parameters_name", "target_element_length",
                         "perimeter_length", "general_min_target_len",
                         "general_curvature_minimum_length", "mesh_type",
                         "element_type", "element_order",
                         "existing_mesh_treatment", "general_max_target_len"],
            },
            {
                "n": 2,
                "do": "Back the current state up before overwriting it",
                "api": "mesh.SaveMeshParams(path); mesh.SaveQualityCriteria(path)",
                "note": "Keep the timestamped backup; this is the only way back.",
            },
            {
                "n": 3,
                "do": "Load the project standards",
                "api": "mesh.ReadMeshParams(.ansa_mpar); mesh.ReadQualityCriteria(.ansa_qual)",
            },
            {
                "n": 4,
                "do": "Prove it took effect by writing the settings back out and diffing",
                "method": ("SaveMeshParams/SaveQualityCriteria to a new file, parse "
                           "both key=value files, compare key by key "
                           "(ignore ANSA_Version). Counting mismatches is the proof; "
                           "the return value is not."),
                "tool": "export_solver_deck / run_quality_check",
            },
        ],
        "script": "scripts/s4_standards.py",
    },
    {
        "id": "mid-surface-and-mesh",
        "name": "Mid-surface extraction and 3 mm shell mesh",
        "when": "Geometry accepted (health check done, health decision made).",
        "cost": "minutes; the mid-surface step dominates",
        "risk": "HIGH — this rewrites the model. Save before running.",
        "steps": [
            {
                "n": 1,
                "do": "Record the counts and save",
                "api": "base.CollectEntities for FACE/SHELL/SOLID/ELEMENT/NODE; base.SaveAs",
                "note": "These counts are the only way to judge step 2.",
            },
            {
                "n": 2,
                "do": "Extract the mid-surface",
                "api": "base.MidSurfAuto(1.0, faces, False, False, 3.0)",
                "warning": ("POSITIONAL ARGUMENTS ONLY. Keyword arguments are "
                            "silently ignored: 0 return, 0.0 s, no change."),
                "accept_if": "elapsed > 0 s AND the SHELL count increased",
            },
            {
                "n": 3,
                "do": "Score the mid-surface against the original",
                "api": ("mesh.GetMiddleMeshQualityScore(shells, faces, "
                        "{'middle_surface': '10%', 'missing_mass': 'Mild'})"),
            },
            {
                "n": 4,
                "do": "Judge mesh quality — and report it with the criteria state",
                "api": ("base.CalcQCHECK(shells, True, True, True) for total, and "
                        "one flag at a time for skew / warp / aspect; "
                        "base.CalculateOffElements()"),
                "warning": "An off-element count without the active criteria is meaningless (see pitfalls).",
                "tool": "calc_mesh_quality / count_failed_elements",
            },
            {
                "n": 5,
                "do": "Element-size distribution from area quantiles",
                "method": "percentiles of base.CalcShellArea, then sqrt() for edge length",
            },
        ],
        "script": "scripts/s5_midsurf.py",
    },
    {
        "id": "verify-results",
        "name": "Verify the result landed",
        "when": "After any mutating run. Not optional.",
        "cost": "~3 s",
        "risk": "none",
        "steps": [
            {
                "n": 1,
                "do": "Re-count every container and compare against step 1",
                "api": "base.CollectEntities(deck, None, T, False)",
                "tool": "count_entities / get_model_summary",
            },
            {
                "n": 2,
                "do": "Confirm the element type actually created",
                "api": "base.GetEntityType(deck, entity)  # EXACTLY two arguments",
                "tool": "list_entities / get_entity",
            },
            {
                "n": 3,
                "do": "Confirm the file on disk",
                "method": "exists / size / mtime of the saved .ansa",
                "tool": "read_last_log; logs/audit.jsonl records model_before and model_after per command",
            },
        ],
        "script": "scripts/s6_verify.py",
    },
]


def _sorted_pitfalls() -> list[dict[str, Any]]:
    return sorted(PITFALLS, key=lambda p: (_SEVERITY_ORDER.get(p["severity"], 9), p["id"]))


def pitfalls_payload() -> dict[str, Any]:
    """The ``ansa://pitfalls`` resource body.

    Ordered by severity, not by id: a model reading this has a budget, and the
    two entries that can silently corrupt a result should be the ones it sees.
    """
    items = []
    for pitfall in _sorted_pitfalls():
        items.append({
            k: pitfall[k] for k in
            ("id", "severity", "title", "symptom", "cause", "correct_usage",
             "guard", "apis", "ref", "verified_on")
        })
    return {
        "count": len(items),
        "verified_on": VERIFIED_ON,
        "source": SOURCE,
        "read_this_before": (
            "Driving ANSA through Python. Each entry below was measured on this "
            "build; several are silent failures where the obvious success check "
            "returns the wrong answer."
        ),
        "pitfalls": items,
    }


def workflows_payload() -> dict[str, Any]:
    """The ``ansa://workflows`` resource body — the measured s2-s6 pipeline."""
    return {
        "count": len(WORKFLOWS),
        "verified_on": VERIFIED_ON,
        "source": "scripts/s2_diag.py .. s6_verify.py (the run that produced "
                  "Casting_initial_mesh_3mm / 98.9 % mid-surface score)",
        "note": (
            "Each step lists the real API call and, where it matters, the "
            "acceptance test. Step costs are the measured ones, so a step that "
            "returns suspiciously fast is itself a failure signal."
        ),
        "workflows": WORKFLOWS,
    }


def pitfalls_json() -> str:
    return json.dumps(pitfalls_payload(), indent=2, ensure_ascii=False)


def workflows_json() -> str:
    return json.dumps(workflows_payload(), indent=2, ensure_ascii=False)


def pitfall_ids() -> list[str]:
    return [p["id"] for p in PITFALLS]


def pitfall_brief(ids: list[str] | tuple[str, ...]) -> str:
    """One-paragraph warning block for a tool docstring.

    Docstrings are the only knowledge channel that reliably reaches a model:
    the client always sends tool descriptions, and almost never fetches
    resources on its own. So the pitfalls that a given tool can walk into are
    quoted where that tool is defined.
    """
    wanted = [p for p in _sorted_pitfalls() if p["id"] in set(ids)]
    if not wanted:
        return ""
    lines = ["", "Known pitfalls for this call (full text: ansa://pitfalls):"]
    for pitfall in wanted:
        lines.append(f"- {pitfall['title']}")
        lines.append(f"  do this instead: {pitfall['correct_usage']}")
    return "\n".join(lines)
