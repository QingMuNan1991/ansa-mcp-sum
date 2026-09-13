"""ANSA API facts layer — single source of truth for deck mapping and API gating.

WHY THIS MODULE EXISTS
----------------------
The previous implementation probed the live API with ``getattr(mod, "Fn", None)``
and then called the result. That pattern converts a *missing function* into
``TypeError: 'NoneType' object is not callable`` at the worst possible moment —
mid-operation, inside the GUI host, after a destructive step. Worse, several
functions that were called simply do not exist in ANSA 25.1.4 with those names
or signatures.

Every name in :data:`VERIFIED_API` was checked against the ANSA Python API
documentation index shipped by the ``ansa-api`` MCP server (**API version
v25.1.4, 5892 functions**) — i.e. against the same version as the installed
ANSA. Names in :data:`REJECTED_API` were actively looked up and confirmed
*absent*; they must never be re-introduced.

Then the *documentation* pass was itself verified against the running build with
``scripts/probe_ansa_capabilities.py`` (run **inside** ANSA through the bridge,
result: ``~/.ansa-mcp-sum/capabilities/capabilities.json``). That second pass
matters because docs and reality disagreed in both directions:

* ``base.CheckIntersections`` is **present** although the docs' Check-factory
  story implied it was gone (caught as "unexpectedly_present");
* ``base.RemoveLogos`` / ``RemoveHoles`` / ``RemoveFillets`` / ``Midsurface`` /
  ``CastModel`` are **absent** (the real ones are ``RemoveLogosAutomatic`` /
  ``FillHoleGeom`` / ``MiddleGeometry`` / ``CleanGeometry``);
* ``session.ImportCode`` does not exist — the only ``ImportCode`` is the top
  level ``ansa.ImportCode``, which is what ``ANSA_TRANSL.py`` already used.

Two rules follow, and they are the whole point of this file:

1. **No blind fallbacks.** If a required API is missing, raise :class:`ApiMissing`
   so the tool returns ``{"ok": false, "error_code": "API_MISSING"}`` and the
   caller learns the truth. Never fabricate a default that looks like success.
2. **Deck decides the entity-type string.** ``"NODE"`` is not a NASTRAN entity
   type — ``"GRID"`` is. Counting with the wrong string returns 0 *without
   raising*, which silently corrupts every downstream decision.

Import style is part of the contract too (:data:`IMPORT_STYLE`): ANSA's
submodules are only reachable as ``from ansa import base``, **not** as
``import ansa.base``.
"""
from __future__ import annotations

try:  # pragma: no cover - only importable inside ANSA
    import ansa  # type: ignore
    from ansa import base, constants, mesh, connections  # type: ignore
    ANSA_AVAILABLE = True
except Exception:  # pragma: no cover
    ansa = base = constants = mesh = connections = None  # type: ignore
    ANSA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Import style (empirically established in ANSA 25.1.4, 2026-09-11)
# ---------------------------------------------------------------------------
#: ANSA registers its submodules as *attributes* of the ``ansa`` module, not as
#: importable packages. The probe that used ``importlib.import_module("ansa.base")``
#: therefore reported ``ansa.constants`` and ``ansa.base.checks.*`` as
#: "unavailable" — a pure false negative, since ANSA's own documented examples
#: (and ``tools_impl``, which imports successfully) use the second form below.
IMPORT_STYLE: dict[str, str] = {
    "ok": "from ansa import base, mesh, connections, constants, guitk",
    "broken": "import ansa.base   ->  ModuleNotFoundError: 'ansa' is not a package",
    "unresolved": (
        "ansa.base.checks.mesh — documented as a module, but not reachable as a "
        "package from a script; prefer the flat entry points that ARE verified: "
        "base.CalcQCHECK / base.CalculateOffElements / base.Check.* / "
        "base.ExecuteCheckTemplate"
    ),
}
#: ``ansa`` itself exposes exactly these callables (8) in 25.1.4.
ANSA_TOP_LEVEL_CALLABLES = (
    "CompileScript", "ImportCode", "PybImport", "ReadingFile",
    "ScriptCurrentDir", "ScriptHomeDir", "ScriptUserDir", "mergeToImporter",
)

# ---------------------------------------------------------------------------
# Verified API surface (checked against ANSA 25.1.4 documentation)
# ---------------------------------------------------------------------------
#: ``"<module>.<attr>": "<verified signature>"``
VERIFIED_API: dict[str, str] = {
    # --- lifecycle / IO ---
    "base.Open": "Open(path) -> int  (0 == success)",
    "base.Save": "Save() -> int",
    "base.SaveAs": "SaveAs(path, silent=bool) -> int",
    "base.Clear": "Clear(deck=None) -> int",
    "base.CurrentDeck": "CurrentDeck() -> int",
    "base.SetCurrentDeck": "SetCurrentDeck(deck:int) -> int",
    # --- query / edit ---
    "base.CollectEntities": (
        "CollectEntities(deck:int, containers, search_types, recursive=False, "
        "filter_visible=False, prop_from_entities=False, mat_from_entities=False, "
        "model_browser_filter=None, no_expand_types=None, hidden_entities=False)"
    ),
    "base.CollectEntitiesI": "CollectEntitiesI(deck, containers, search_types, recursive, ...)  # iterator, lower memory",
    "base.GetEntity": "GetEntity(deck:int, type:str, element_id:int) -> Entity|None",
    "base.GetEntityCardValues": "GetEntityCardValues(deck:int, entity:Entity, fields) -> dict  # ONE entity, not a list",
    "base.SetEntityCardValues": "SetEntityCardValues(deck:int, entity:Entity, values:dict) -> int",
    "base.CreateEntity": "CreateEntity(deck:int, type:str, fields:dict) -> Entity|None",
    "base.DeleteEntity": "DeleteEntity(entities:Entity|Iterable, force=False, compress=True) -> int  # 0 == success, takes ENTITIES not (deck,type,id)",
    "base.NameToEnts": "NameToEnts(pattern:str, deck:int=0, match:int=None) -> list[Entity]|None  # pattern is a PCRE regex",
    # Registered because README §7.4 states the signature as a hard rule: the
    # *arity* is the trap (one argument is silently read as "this type is not
    # supported"), so it belongs with the other facts rather than in prose only.
    "base.GetEntityType": "GetEntityType(deck:int, entity:Entity) -> str  # EXACTLY two arguments; wrong arity is misread as 'type unsupported'",
    "base.SetEntityVisibilityValues": "SetEntityVisibilityValues(deck:int, fields:dict) -> int  # {'SHELL':'enable'|'off'}",
    # --- measurement (all take ENTITIES, not ids, and no deck argument) ---
    "base.CalcShellArea": "CalcShellArea(entity:Entity) -> float",
    "base.CalcSolidVolume": "CalcSolidVolume(entity:Entity) -> float",
    "base.CalcElementMass": "CalcElementMass(entities, no_nsm:bool=False, deck:int=0) -> list  # [mass, cg_x, cg_y, cg_z, Ixx, ...] 22 values, or 0",
    "base.CalcQCHECK": "CalcQCHECK(ents:Iterable, SKEWNESS_FLAG:bool, WARPING_FLAG:bool, ASPECT_FLAG:bool) -> float",
    "base.CalculateOffElements": "CalculateOffElements(entities=None, details:bool=False) -> dict  # {'TOTAL OFF':n, 'SKEW':n, ...}",
    "base.CalculateAverageMinMaxElementLength": "CalculateAverageMinMaxElementLength(elements) -> dict  # {'average','min','max'}",
    # --- checks / quality (the real entry points) ---
    "base.checks.mesh.MeshQuality": "MeshQuality() -> Check  # then obj.execute()",
    "base.Check.execute": (
        "Check.execute(exec_mode:int, report:int, history:int, entities:Iterable) -> list[CheckReport]  "
        "# exec_mode: Check.EXEC_ON_ALL|EXEC_ON_VIS|EXEC_ON_MODEL|EXEC_ON_SELECTED; report: Check.REPORT_NONE"
    ),
    "base.ExecuteCheckTemplate": "ExecuteCheckTemplate(template:str, report_level:int, make_current:bool) -> object  # has .warnings/.errors/.ok/.reports",
    # --- mesh ---
    "mesh.SetMeshParamTargetLength": "SetMeshParamTargetLength(function:str, value:float) -> int  # function in {'absolute','init_local','average_length','free'}",
    "mesh.CreateBestMesh": "CreateBestMesh() -> int  # meshes VISIBLE macros, keeps best QCHECK skewness",
    "mesh.CreateFreeMesh": "CreateFreeMesh() -> int  # VISIBLE macros",
    "mesh.CreateMapMesh": "CreateMapMesh() -> int  # VISIBLE macros, structured/quad",
    "mesh.CreateAdvFrontMesh": "CreateAdvFrontMesh() -> int  # VISIBLE macros",
    "mesh.CreateCfdMesh": "CreateCfdMesh() -> int  # VISIBLE macros",
    "mesh.CreateStlMesh": "CreateStlMesh() -> int  # VISIBLE macros",
    "mesh.SplitElements": "SplitElements(shells:list, ret_ents:bool=False) -> int",
    "mesh.ReadQualityCriteria": "ReadQualityCriteria(FILENAME:str) -> int",
    # --- connections (module is PLURAL: ansa.connections) ---
    "connections.CreateConnectionPoint": (
        "CreateConnectionPoint(type:str, position:list[float], id:int, connectivity:list) -> Entity  "
        "# type in {'SpotweldPoint_Type','Bolt_Type','GumDrop_Type','Rivet_Type','Screw_Type'}"
    ),
    "connections.CreateConnectionLine": (
        "CreateConnectionLine(type:str, curves:list, id:int, connectivity:list) -> Entity  "
        "# type in {'SpotweldLine_Type','AdhesiveLine_Type','SeamLine_Type','Hemming_Type'}"
    ),
    "connections.CreateConnectionFace": "CreateConnectionFace(type:str, faces:list, id:int, connectivity:list, create_new_face:bool) -> Entity",
    "connections.EntsToConnectivityString": "EntsToConnectivityString(entities) -> str",
    "connections.AutoCreateSeamlines": "AutoCreateSeamlines(shells, max_distance=10.0, ..., preview=True, output_mode='pid') -> list|None",
    "connections.AutoCreateConnectionChains": "AutoCreateConnectionChains(connections:list) -> list|None",
    "connections.CreateConnectorFromInterfacePoints": "CreateConnectorFromInterfacePoints(interface_points, match_only_identical_names, ...) -> dict",
    # --- geometry preparation (the family that is easy to get wrong) ---
    "base.RemoveLogosAutomatic": (
        "RemoveLogosAutomatic(height:float, size:float, source_faces=None) -> int  "
        "# 1 == success; operates on VISIBLE faces when source_faces is omitted"
    ),
    "base.FillHoleGeom": (
        "FillHoleGeom(diameter, create_point, convert_to_connection_point, create_curve, "
        "only_internal_perimeters=False, always_produce_new_faces=False, set_id=None, pid_id=None) -> int  "
        "# `diameter` may be a float OR a list of CONS; the replacement for RemoveHoles"
    ),
    "base.MiddleGeometry": (
        "MiddleGeometry(input:list, thickness_tolerance:float, min_success:float, ...) -> list[dict]  "
        "# replacement for Midsurface; keys: success_rate / variable_thickness_faces / result_faces"
    ),
    "base.MidSurfAuto": (
        "MidSurfAuto(thick:float, faces, handle_many_solids:bool, handle_as_single_solid:bool, length:float, "
        "join_distance, elem_type:int, exact_middle:bool, paste_triple_len, add_features_to_set, part:str, "
        "ret_ents:bool, property:str, ...) -> int  # 0 == success  "
        "# [!] POSITIONAL ARGS ONLY - kwargs are SILENTLY IGNORED (returns 0 in 0.0s, model unchanged). "
        "# Call as base.MidSurfAuto(1.0, faces, False, False, 3.0). "
        "# elem_type: -1 QUAD / -2 TRIA / -3 MIXED(default) / -4 ORTHO_TRIA. length must be > thick. "
        "# FACE count is UNCHANGED on success - verify with the SHELL count, not the FACE count."
    ),
    "base.DetectSolidDescription": (
        "DetectSolidDescription(faces, thickness:float=0, return_percentage:bool=False, "
        "estimate_thickness:bool=False, separate_connectivity:bool=False, "
        "separate_connectivity_stop_at_pid_bound:bool=False, fix_unchecked_faces:bool=True, "
        "estimate_thickness_num_decimals:int=1) -> list|int  "
        "# int: 1 == input IS a solid description, 0 == not. "
        "# return_percentage=True -> [shell%, thin_solid%, solid%] (shell ~1.0 means it is ALREADY a mid-surface). "
        "# estimate_thickness=True -> list[dict] of {thickness: percentage}. "
        "# return_percentage and estimate_thickness are mutually exclusive. "
        "# fix_unchecked_faces=True MODIFIES the model - pass False for read-only diagnosis."
    ),
    "mesh.GetMiddleMeshQualityScore": (
        "GetMiddleMeshQualityScore(shells:list, faces:list, tol_dict:dict=None) -> dict  "
        "# {'middle_surface_score', 'align_areas_score', 'align_perimeters_score'} as '98.9%' strings"
    ),
    "mesh.CheckMiddleMesh": (
        "CheckMiddleMesh(shells, faces, options:dict, check_for_unconnected:bool=True, "
        "return_ents:bool=True, min_nodal_thick:float=0.0) -> dict|list  "
        "# dict maps each check name -> failed entities; align_empty_* entries are normal when "
        "# no align constraints are defined"
    ),
    "base.CleanGeometry": "CleanGeometry() -> None  # fixes collapsed CONS, gaps, cracks, triple CONS on VISIBLE entities",
    "base.AutoSurfs": "AutoSurfs() -> int  # faces from a wire base",
    "base.CheckAndFixGeometry": (
        "CheckAndFixGeometry(input, options, fix_options, return_one_matrix_for_every_error, remaining_errors) -> dict|None  "
        "# DEPRECATED since v24 -> prefer ansa.base.checks.general.Geometry()"
    ),
    # --- session ---
    "session.ProgramArguments": "ProgramArguments() -> list[str]",
    "session.defbutton": "defbutton(group:str, label:str, tooltip:str)  # decorator",
    "ansa.ImportCode": "ImportCode(path:str) -> None  # NOTE: top-level ansa, NOT ansa.session",
    "ansa.CompileScript": "CompileScript(path:str) -> None",
    # --- geometry inspection (all take ENTITIES; all present in 25.1.4) ---
    "base.GetFaceArea": "GetFaceArea(FACE:Entity) -> float  # -1.0 on error",
    "base.GetFaceOrientation": "GetFaceOrientation(face:Entity) -> list[float]  # unit vector",
    "base.EntityCenter": "EntityCenter(ENTITY:Entity, CENTER_OF_GRAVITY:bool=False) -> tuple  # (x, y, z)",
    "base.BoundBox": "BoundBox(entities) -> list[float]|None  # [xmin, ymin, zmin, xmax, ymax, zmax]",
    "base.SurfaceInfo": "SurfaceInfo(faces:list) -> list[list]  # per face: [surf_id, patches_s, patches_t, radius_s, radius_t]",
    "base.PerimetersOfFace": "PerimetersOfFace(faces, perimeters_type='all') -> list[Entity]  # 'all'|'active'|'joined'",
    "base.DeleteFaces": "DeleteFaces(entities=None, filter_visible=False, convert_links=False) -> int  # entities=None means the WHOLE database",
    "base.CheckIntersections": "CheckIntersections() -> int|dict  # present in 25.1.4 despite the Check-factory docs",
    "base.SetANSAdefaultsValues": "SetANSAdefaultsValues(fields:dict, mbcontainer_type=None) -> int  # 0 == success (obsolete, BCSettingsSetValues is the new one)",
    # --- solver decks: NOTE the inverted return convention (1 == success) ---
    "base.OutputNastran": "OutputNastran(filename=..., mode='all'|'model'|'visible', ...) -> int  # 1 == SUCCESS, 0 == failure",
    "base.OutputLSDyna": "OutputLSDyna(filename=..., mode=..., ...) -> int  # 1 == SUCCESS",
    "base.OutputAnsys": "OutputAnsys(filename=..., mode=..., ...) -> int  # 1 == SUCCESS",
    "base.OutputAbaqus": "OutputAbaqus(filename=..., mode=..., ...) -> int  # 1 == SUCCESS",
    # --- meshing by explicit entity list (complements the VISIBLE-only shortcuts) ---
    "mesh.Mesh": "Mesh(entities:list|'visible') -> int  # 1 == success, 0 == invalid arguments; entities may be FACES",
    "mesh.RemeshShells": (
        "RemeshShells(shells:list|'visible', mesh_generator:str) -> list[Entity]  "
        "# re-meshes EXISTING shells; generator in {'CFD','ADVFRNT','FREE','SPOT','GRADUAL'}"
    ),
    # --- GUI toolkit: only meaningful in GUI mode (-b/nogui raises) ---
    #     Verified by the running bridge: these are what anchors the 200ms timer
    #     to the ANSA main thread. `defbutton` puts the buttons in the toolbar.
    "guitk.BCWindowCreate": "BCWindowCreate(title:str, mode) -> window",
    "guitk.BCLabelCreate": "BCLabelCreate(window, text:str)",
    "guitk.BCSpacerCreate": "BCSpacerCreate(window)",
    "guitk.BCShow": "BCShow(window)",
    "guitk.BCWindowSetAcceptFunction": "BCWindowSetAcceptFunction(window, fn, data)  # fn(window, data) -> int",
    "guitk.BCTimerCreate": "BCTimerCreate(window) -> timer",
    "guitk.BCTimerSetTimeoutFunction": "BCTimerSetTimeoutFunction(timer, fn, data)  # fn(timer, data) -> int; 0 == keep running",
    "guitk.BCTimerStart": "BCTimerStart(timer, msec:int, single_shot:bool)",
    "guitk.BCTimerStop": "BCTimerStop(timer)",
    "guitk.BCTimerIsActive": "BCTimerIsActive(timer) -> bool",
    "guitk.BCTimerSingleShot": "BCTimerSingleShot(delay_ms:int, fn, data)  # one-shot, same fn(timer, data) signature",
    #: Title-bar button control. Used by the autoload to hide the bridge window's
    #: Close button (v1.0.7: a closed window takes the timer with it and the poll
    #: dies) while keeping Min/Max. The two constants are BCEnumTitleBarButton
    #: values and are OR-ed together, so they are read at runtime rather than
    #: hardcoded as ints.
    "guitk.BCWindowShowTitleBarButtons": (
        "BCWindowShowTitleBarButtons(window, buttons:int)  # buttons = OR-ed "
        "guitk.constants.BC*Button; not supported under VR mode"
    ),
    "guitk.constants.BCMinimizeButton": (
        "guitk.constants.BCMinimizeButton  # BCEnumTitleBarButton value, OR-able"
    ),
    "guitk.constants.BCMaximizeButton": (
        "guitk.constants.BCMaximizeButton  # BCEnumTitleBarButton value, OR-able"
    ),
    # --- session / toolbar registration ---
    #: Read back off a live 25.1.4 session: ``scripts/ansa_mcp_sum_autoload.py``
    #: has registered its four toolbar buttons this way since 2026-09-10, and the
    #: call form below is the one that actually produced working buttons.
    "ansa.session.defbutton": (
        "defbutton(toolbar:str, label:str, tooltip:str='') -> decorator  "
        "# used as @ansa.session.defbutton('MCP-Sum', 'Run Once', '...')"
    ),
    "ansa.session.ProgramArguments": (
        "ProgramArguments() -> iterable  # ANSA's own argv; consumed by the capability probe"
    ),
}

#: Names that were looked up and DO NOT exist (or have a different signature).
#: Do not "restore" these — the replacement is given.
REJECTED_API: dict[str, str] = {
    "base.GetEntityCount": "use len(base.CollectEntities(deck, None, type))",
    "base.SearchEntityByName": "use base.NameToEnts(pattern, deck, match=constants.ENM_SUBSTRING)",
    "base.SetEntityFields": "use base.SetEntityCardValues(deck, entity, values)",
    "base.New": "use base.Clear()",
    # --- per-entity visibility: probed NOT_FOUND in ansa.base (probe 2.0) ---
    # There is no "hide/show this entity" API in `base`. Only the per-TYPE
    # browser flags exist (base.SetEntityVisibilityValues). Anything that claims
    # per-entity show/hide must be refused or routed through a selection
    # workflow instead of silently doing nothing.
    "base.Show": "no per-entity API in ansa.base; use base.SetEntityVisibilityValues(deck, {type: 'on'|'off'}) for per-TYPE flags",
    "base.Hide": "no per-entity API in ansa.base; see base.SetEntityVisibilityValues",
    "base.SetVisibility": "does not exist; see base.SetEntityVisibilityValues",
    "base.ShowEntity": "does not exist (probe 2.0); see base.SetEntityVisibilityValues",
    "base.HideEntity": "does not exist (probe 2.0); see base.SetEntityVisibilityValues",
    "base.SelectEntities": "does not exist in ansa.base (probe 2.0)",
    "base.SetSelection": "does not exist in ansa.base (probe 2.0)",
    "base.GetSelection": "does not exist in ansa.base (probe 2.0)",
    # Probed absent in the running 25.1.4 build (NOT_FOUND):
    "base.RemoveLogos": "use base.RemoveLogosAutomatic(height, size, source_faces)",
    "base.RemoveHoles": "use base.FillHoleGeom(diameter, ...)",
    "base.RemoveFillets": "no direct replacement; use base.CheckAndFixGeometry / ansa.base.checks.general.Geometry()",
    "base.Midsurface": "use base.MiddleGeometry(input, thickness_tolerance, min_success, ...)",
    "base.CastModel": "no API with this name; thickness casting is base.CalculateSolidThickness(mode, max_thickness)",
    "session.ImportCode": "the only ImportCode is top-level: ansa.ImportCode(path)",
    # --- guitk teardown: there is no destroy for either object ---
    # Ansa 25.x guitk has BCTimerStop but NO BCTimerDestroy, and no
    # BCDestroyWindow at all. The doc for BCDestroy says a window is closed with
    # BCWindowAccept / BCWindowReject, and BCWindowCreate's BCOnExitDestroy
    # already disposes of the window when the owning script ends. Both facts were
    # paid for once already: the bridge's teardown called BCDestroyWindow inside a
    # try/except, so the window was never disposed and nothing ever said so.
    "guitk.BCDestroyWindow": (
        "does not exist in 25.x; use BCWindowCreate(name, BCOnExitDestroy) to have "
        "it disposed at script end, or BCWindowReject(window) to close it now"
    ),
    "guitk.BCTimerDestroy": "does not exist in 25.x; BCTimerStop is the only teardown",
    "base.RunQualityCheck": "use base.CalcQCHECK(ents, SKEWNESS_FLAG, WARPING_FLAG, ASPECT_FLAG) or base.checks.mesh.MeshQuality()",
    "base.GetFailedEntitiesCount": "use base.CalculateOffElements() -> {'TOTAL OFF': n}",
    "base.CalcShellArea(deck, id)": "CalcShellArea takes an ENTITY: base.CalcShellArea(entity)",
    "base.CalcSolidVolume(deck, id)": "CalcSolidVolume takes an ENTITY: base.CalcSolidVolume(entity)",
    "base.CalcMass": "use base.CalcElementMass(entities, deck=deck) -> list, [0] is mass",
    "mesh.SetShellMeshParams": "use mesh.SetMeshParamTargetLength('absolute', length)",
    "mesh.MeshShell": "use mesh.CreateBestMesh()/CreateFreeMesh()/CreateMapMesh() on VISIBLE macros",
    "mesh.MeshVolume": "use the batch-mesh / volume-mesh scenario API",
    "mesh.CountShellElements": "use len(base.CollectEntities(deck, None, 'SHELL'))",
    "mesh.CountSolidElements": "use len(base.CollectEntities(deck, None, 'SOLID'))",
    "mesh.DeleteMesh": "use base.DeleteEntity(shells, force=True)",
    "connections.ApplyConnectors": "no API with this name in 25.1.4; realise connectors via the connector entity workflow",
    "connections.CheckConnectors": "use base.ExecuteCheckTemplate / base.CalcQCHECK",
    # NOTE: base.CheckIntersections is NOT in this list — the running build has
    # it (probed OK), even though the Check-factory story suggested otherwise.
}

#: Attributes that legitimately vary between builds: a renamed accessor, a
#: plugin that may or may not be loaded. These are the ONLY names an
#: ``optional`` lookup is allowed to return ``None`` for.
#:
#: Keeping them in a separate dict (rather than marking them inside
#: ``VERIFIED_API``) matters: "verified" must keep meaning "we checked that this
#: exists and has this signature", otherwise the whole fact layer stops being
#: able to answer "is this callable safe to write down?".
OPTIONAL_API: dict[str, str] = {
    "base.DataBaseName": (
        "DataBaseName() -> str  # path of the model ANSA has open. The only one "
        "of the three that exists here — see CURRENT_MODEL_PATH_CANDIDATES"
    ),
    "base.GetCurrentFileName": (
        "GetCurrentFileName() -> str  # documented elsewhere, ABSENT in 25.1.4 "
        "(probed); kept for other builds"
    ),
    "base.CurrentFileName": (
        "CurrentFileName() -> str  # older spelling of the same accessor"
    ),
}

#: Accessors for "what file is open", in preference order.
#:
#: Probed live inside ANSA 25.1.4 (2026-09-13): ``hasattr(base,
#: 'GetCurrentFileName')`` and ``hasattr(base, 'CurrentFileName')`` are both
#: **False**, while ``base.DataBaseName()`` returns the open model's path in
#: 0.0 ms. The earlier list named two accessors that do not exist, so every
#: caller silently got ``None`` and reported "model path unknown" for a model
#: that was open — a wrong answer that reads like a data problem.
CURRENT_MODEL_PATH_CANDIDATES = (
    "base.DataBaseName",
    "base.GetCurrentFileName",
    "base.CurrentFileName",
)


class ApiMissing(RuntimeError):
    """A required ANSA API is absent in the running build.

    Raising (instead of returning a default) is deliberate: a missing API must
    surface as a failed tool call, never as a plausible-looking success.
    """

    def __init__(self, path: str, hint: str = "") -> None:
        message = f"ANSA API not available in this build: {path}"
        if hint:
            message += f" ({hint})"
        super().__init__(message)
        self.path = path
        self.hint = hint


def _absent(path: str, optional: bool, hint: str = ""):
    """The single decision point for "this API is not here".

    ``optional=True`` is reserved for :data:`OPTIONAL_API` names and yields
    ``None``; everything else raises. Splitting it out keeps the four lookup
    failures (no ANSA, bad module, bad segment, bad attr) from each having to
    remember which behaviour applies.
    """
    if optional:
        return None
    raise ApiMissing(path, hint)


def resolve(path: str, optional: bool = False):
    """Return the callable at ``"module.attr"`` or raise :class:`ApiMissing`.

    ``optional=True`` returns ``None`` instead of raising, and must only ever be
    used for names in :data:`OPTIONAL_API`. It exists because the old
    ``getattr(mod, "Fn", None)`` habit (banned by README §7.4) had the right
    instinct — some attributes really are build-dependent — but the wrong
    location: scattering the probe meant an *invented* name was indistinguishable
    from an *optional* one, and the difference only surfaced later as
    ``TypeError: 'NoneType' object is not callable`` halfway through an
    operation. Prefer :func:`resolve_optional`, which enforces the registry.
    """
    if not ANSA_AVAILABLE:
        return _absent(path, optional, "ANSA is not importable in this interpreter")
    module_name, _, attr = path.rpartition(".")
    if not module_name:
        # bare module name, e.g. resolve("base") -> the module itself
        module = {"base": base, "mesh": mesh, "connections": connections,
                  "constants": constants, "ansa": ansa}.get(attr)
        if module is None:
            return _absent(path, optional, f"unknown module {attr!r}")
        return module
    root = {
        "base": base, "mesh": mesh, "connections": connections,
        "constants": constants, "ansa": ansa,
    }.get(module_name.split(".")[0])
    if root is None:
        return _absent(path, optional, f"unknown module root {module_name!r}")
    obj: object = root
    # walk remaining dotted parts (handles "base.checks.mesh.MeshQuality")
    for part in module_name.split(".")[1:]:
        obj = getattr(obj, part, None)
        if obj is None:
            return _absent(path, optional, f"module segment {part!r} missing")
    fn = getattr(obj, attr, None)
    if fn is None:
        return _absent(path, optional, REJECTED_API.get(path, ""))
    return fn


def resolve_optional(path: str):
    """``resolve(path, optional=True)``, restricted to registered names.

    An unlisted name is a typo or an invented API, not an optional one — those
    still fail loudly. This is what keeps the escape hatch from becoming a
    general-purpose fallback again.
    """
    if path not in OPTIONAL_API:
        raise ApiMissing(path, "not an OPTIONAL_API name; use resolve() instead")
    return resolve(path, optional=True)


def current_model_path() -> str | None:
    """Path of the model ANSA has open, or ``None`` when it cannot be known.

    Returns ``None`` rather than raising on purpose: callers use this for
    *reporting* (``status.json``, capability snapshots), and "model path
    unknown" is a legitimate answer that must not fail an otherwise fine
    command. The candidate list lives in :data:`CURRENT_MODEL_PATH_CANDIDATES`.

    An empty string is normalised to ``None``: ANSA answers with ``''`` when it
    is running with nothing open, and handing an empty string back as a path
    invites a caller to log it, join it, or derive a filename from it.

    Note that "no model is open" and "no accessor exists in this build" both
    surface as ``None`` here. Callers that need to tell them apart — the audit
    trail does, because only one of them is a defect — must also ask
    :func:`current_model_path_accessor`.
    """
    for path in CURRENT_MODEL_PATH_CANDIDATES:
        func = resolve_optional(path)
        if func is None:
            continue
        try:
            value = str(func()).strip()
        except Exception:
            return None
        return value or None
    return None


def current_model_path_accessor() -> str | None:
    """Which accessor answers :func:`current_model_path` on this build.

    ``None`` means the candidate list is wrong for the running ANSA — a defect
    in this project, not a property of the model — and must not be reported the
    same way as "nothing is open".
    """
    for path in CURRENT_MODEL_PATH_CANDIDATES:
        if resolve_optional(path) is not None:
            return path
    return None


def has(path: str) -> bool:
    try:
        resolve(path)
        return True
    except ApiMissing:
        return False


# ---------------------------------------------------------------------------
# Deck-aware entity types — the GRID vs NODE trap
# ---------------------------------------------------------------------------
#: Deck name -> node entity type. Verified from the documentation examples:
#: NASTRAN uses ``GetEntity(constants.NASTRAN, "GRID", 1)`` while
#: LSDYNA/ABAQUS use ``GetEntity(constants.LSDYNA, "NODE", 1)``.
#: Using the wrong string does NOT raise — it returns 0 entities.
DECK_NODE_TYPE: dict[str, str] = {
    "NASTRAN": "GRID",
    "LSDYNA": "NODE",
    "ABAQUS": "NODE",
    "PAMCRASH": "NODE",
    "RADIOSS": "NODE",
    "OPTISTRUCT": "GRID",
    "ANSYS": "NODE",
    "PERMAS": "NODE",
    "FLUENT": "NODE",
}

#: Deck names recognised via ``ansa.constants``.
DECK_NAMES = tuple(DECK_NODE_TYPE.keys())

#: Deck-agnostic entity types (same string in every deck).
DECK_AGNOSTIC_TYPES = {
    "GEOMETRY": ("FACE",),
    "PARTS": ("ANSAPART",),
    "INCLUDES": ("INCLUDE",),
    "ELEMENTS": ("SHELL", "SOLID"),
    "FACES": ("FACE",),
}


def deck_id(deck_name: str) -> int | None:
    """Map a deck name to its ``ansa.constants`` value, or None."""
    if not ANSA_AVAILABLE:
        return None
    return getattr(constants, deck_name.upper().replace("-", "").replace("_", ""), None)


def deck_name(deck: int | None) -> str:
    """Map a deck int back to a name; ``"UNKNOWN(<n>)"`` when not recognised."""
    if not ANSA_AVAILABLE or deck is None:
        return "UNKNOWN"
    for name in DECK_NAMES:
        if getattr(constants, name, None) == deck:
            return name
    return f"UNKNOWN({deck})"


def current_deck() -> int:
    try:
        return int(base.CurrentDeck())
    except Exception:
        return 0


def normalize_deck(deck: int | None) -> int:
    """Resolve a caller-supplied deck to a concrete int (``None``/negative -> current)."""
    if deck is None:
        return current_deck()
    try:
        value = int(deck)
    except Exception:
        return current_deck()
    return current_deck() if value < 0 else value


def node_type_for(deck: int) -> str:
    """The node entity-type string valid for ``deck``."""
    return DECK_NODE_TYPE.get(deck_name(deck), "GRID")


def count(deck: int, entity_type: str) -> int:
    """Entity count via CollectEntities. Returns 0 only on a genuine empty set."""
    fn = resolve("base.CollectEntities")
    return len(fn(deck, None, entity_type, False) or [])


def collect(deck: int, entity_type, limit: int | None = None):
    fn = resolve("base.CollectEntities")
    ents = fn(deck, None, entity_type, False) or []
    if limit is not None:
        ents = ents[:limit]
    return ents


# ---------------------------------------------------------------------------
# Node coordinates — the X1/X2/X3 trap
# ---------------------------------------------------------------------------
#: Card-field tuples that carry node coordinates, in the order we try them.
#:
#: ANSA names these per deck. Probed live in 25.1.4 on a NASTRAN deck
#: (2026-09-13): ``GetEntityCardValues(deck, grid, ('X1','X2','X3'))`` ->
#: ``{'X1': -418.06, 'X2': 244.10, 'X3': 51.38}``, while the same call with
#: ``('X','Y','Z')`` -> ``{}``. An **empty dict, not an exception** — so a
#: hardcoded guess does not fail loudly, it makes the caller report "no node
#: carried coordinates" about a model that is full of them.
NODE_COORD_FIELD_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("X1", "X2", "X3"),   # NASTRAN GRID
    ("X", "Y", "Z"),      # classic convention
    ("NX", "NY", "NZ"),
)

#: deck int -> the tuple that worked. Filled from a real entity, so a deck we
#: have never seen self-heals instead of needing a table entry first.
_coord_fields_cache: dict[int, tuple[str, str, str]] = {}


def node_coord_fields(deck: int, sample) -> tuple[str, str, str] | None:
    """Field names that actually carry coordinates for nodes on ``deck``.

    ``sample`` is any real node entity: discovery needs a live example because
    an unlisted field name answers with ``{}`` rather than an error, and the
    list of names cannot be read off the API. The winner is cached per deck, so
    only the first call pays for the probe.

    Returns ``None`` when no candidate yields values — the caller must report
    that as a failure rather than substitute zeros.
    """
    if deck in _coord_fields_cache:
        return _coord_fields_cache[deck]
    if sample is None:
        return None
    get_values = resolve("base.GetEntityCardValues")
    for fields in NODE_COORD_FIELD_CANDIDATES:
        try:
            values = get_values(deck, sample, list(fields))
        except Exception:
            continue
        if isinstance(values, dict) and set(fields) <= set(values):
            _coord_fields_cache[deck] = fields
            return fields
    return None


def read_node_xyz(deck: int, entity) -> tuple[float, float, float] | None:
    """``(x, y, z)`` for one node, or ``None`` when this build will not say."""
    fields = node_coord_fields(deck, entity)
    if fields is None:
        return None
    get_values = resolve("base.GetEntityCardValues")
    try:
        values = get_values(deck, entity, list(fields))
    except Exception:
        return None
    try:
        return (float(values[fields[0]]),
                float(values[fields[1]]),
                float(values[fields[2]]))
    except Exception:
        return None


def resolve_entities(deck: int, entity_type: str, ids) -> tuple[list, list]:
    """Turn a list of numeric ids into real entity handles.

    Returns ``(entities, missing_ids)``. Needed wherever an API takes ENTITIES
    (``DeleteEntity``, ``mesh.Mesh``, ``CalcShellArea`` …) but the tool's wire
    format is ids. Reporting ``missing_ids`` matters: silently dropping an id the
    user asked for is how a "delete 10 faces" call becomes a "delete 7 faces"
    call that still reports success.
    """
    get = resolve("base.GetEntity")
    ents: list = []
    missing: list = []
    for raw in ids:
        try:
            eid = int(raw)
        except (TypeError, ValueError):
            missing.append(raw)
            continue
        ent = None
        try:
            ent = get(deck, entity_type, eid)
        except Exception:
            ent = None
        if ent is None:
            missing.append(eid)
        else:
            ents.append(ent)
    return ents, missing


def count_nodes(deck: int) -> dict:
    """Deck-correct node count.

    Returns the count plus which type string produced it, so the caller can see
    (and report) that the deck mapping was actually applied rather than assumed.
    """
    primary = node_type_for(deck)
    n = count(deck, primary)
    if n:
        return {"count": n, "entity_type": primary, "deck_name": deck_name(deck)}
    # Fall back to the alternative spelling, but say so — this is a mismatch
    # signal, not a success story.
    alt = "NODE" if primary == "GRID" else "GRID"
    alt_n = count(deck, alt)
    if alt_n:
        return {
            "count": alt_n, "entity_type": alt, "deck_name": deck_name(deck),
            "type_mismatch": True,
            "note": f"deck {deck_name(deck)} expected {primary!r} but {alt!r} matched; "
                    f"deck mapping may need updating",
        }
    return {"count": 0, "entity_type": primary, "deck_name": deck_name(deck)}


def off_elements(entities=None, details: bool = False) -> dict:
    """Quality 'off elements' summary — replaces the fabricated GetFailedEntitiesCount."""
    fn = resolve("base.CalculateOffElements")
    res = fn(entities, details) if entities is not None else fn(details=details)
    return dict(res or {})


def element_mass(entity) -> tuple[float, dict]:
    """Mass of one entity. ``CalcElementMass`` returns a 22-element list, not a scalar."""
    fn = resolve("base.CalcElementMass")
    res = fn(entity, deck=current_deck())
    if isinstance(res, (list, tuple)) and res:
        total = float(res[0])
        cg = None
        if len(res) >= 4:
            try:
                cg = [float(res[1]), float(res[2]), float(res[3])]
            except Exception:
                cg = None
        return total, {"center_of_gravity": cg, "raw_len": len(res)}
    return 0.0, {"note": "CalcElementMass returned 0 (entity not supported)"}


def run_mesh_quality_check(entities=None, exec_mode: int | None = None) -> dict:
    """Execute the standard mesh quality check and summarise the reports."""
    factory = resolve("base.checks.mesh.MeshQuality")
    check = factory()
    if exec_mode is not None:
        try:
            results = check.execute(exec_mode=exec_mode)
        except TypeError:
            results = check.execute(exec_mode, 0, 0, entities or [])
    elif entities:
        try:
            results = check.execute(
                exec_mode=getattr(base.Check, "EXEC_ON_SELECTED", 3),
                entities=entities,
            )
        except Exception:
            results = check.execute()
    else:
        results = check.execute()
    reports = list(results or [])
    summary = []
    for rep in reports:
        item = {}
        for attr in ("name", "title", "message", "errors", "warnings", "status", "level"):
            if hasattr(rep, attr):
                try:
                    item[attr] = str(getattr(rep, attr))
                except Exception:
                    pass
        if not item:
            item = {"repr": str(rep)[:200]}
        summary.append(item)
    return {"report_count": len(summary), "reports": summary[:50]}
