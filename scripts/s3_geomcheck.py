# -*- coding: utf-8 -*-
"""Step 2: 几何检查 —— 仅调用崩溃前已实测安全的接口。

刻意排除（上次崩溃嫌疑）：
  - base.GetEntityCardValues(deck, face) 不传字段
  - base.CheckDescription.read_descriptions()
  - check.parameters()
  - base.Or() / base.RedrawAll()
"""
from ansa import base, constants
import traceback, time, collections, re

R = {}
D = constants.NASTRAN
FACES = base.CollectEntities(D, None, 'FACE', False)
R['n_faces'] = len(FACES)


def eid(e):
    m = re.search(r'id:(\d+)', str(e))
    return int(m.group(1)) if m else None


CANDIDATES = [
    ('geometry', 'NeedleFaces'), ('geometry', 'NeedleAreas'),
    ('geometry', 'OverlapFaces'), ('geometry', 'ProblematicSurfaces'),
    ('geometry', 'Cracks'), ('geometry', 'CollapsedCons'),
    ('geometry', 'SingleCons'), ('geometry', 'TripleCons'),
    ('geometry', 'UncheckedFaces'), ('geometry', 'unmeshedMacros'),
    ('penetration', 'Intersections'), ('penetration', 'InteriorIntersections'),
    ('penetration', 'Proximities'), ('penetration', 'PenParametric'),
    ('penetration', 'PropertyThickness'), ('penetration', 'UserThickness'),
    ('general', 'Connectivity'), ('general', 'FreeNodes'),
    ('general', 'UnconnectedRegions'), ('general', 'Geometry'),
]

results = []
for modname, clsname in CANDIDATES:
    item = {'check': modname + '.' + clsname}
    try:
        cls = getattr(getattr(base.checks, modname), clsname)
    except Exception as e:
        item['skip'] = 'class not found: ' + repr(e)[:70]
        results.append(item)
        continue
    try:
        obj = cls()
    except Exception:
        item['skip'] = 'ctor failed: ' + traceback.format_exc()[-160:]
        results.append(item)
        continue
    t0 = time.time()
    try:
        rep = obj.execute(exec_mode=base.Check.EXEC_ON_SELECTED, entities=FACES)
        item['ok'] = True
        if isinstance(rep, (list, tuple)):
            item['n_reports'] = len(rep)
            rs = []
            for r in rep:
                rr = {}
                for a in ('type', 'status', 'err_code', 'message'):
                    try:
                        rr[a] = str(getattr(r, a))[:150]
                    except Exception:
                        pass
                try:
                    ents = r.entities
                    rr['n_entities'] = len(ents) if ents is not None else None
                except Exception:
                    pass
                try:
                    iss = r.issues
                    if isinstance(iss, (list, tuple)):
                        rr['n_issues'] = len(iss)
                        codes = []
                        for c in iss[:30]:
                            codes.append(str(getattr(c, 'err_code', ''))
                                         + '|' + str(getattr(c, 'message', ''))[:80])
                        rr['issues'] = codes
                except Exception:
                    pass
                try:
                    rr['has_fix'] = bool(r.has_fix)
                except Exception:
                    pass
                rs.append(rr)
            item['reports'] = rs
        else:
            item['rep_type'] = type(rep).__name__
            item['rep_repr'] = str(rep)[:200]
    except Exception:
        item['ok'] = False
        item['exec_err'] = traceback.format_exc()[-400:]
    item['time_s'] = round(time.time() - t0, 2)
    results.append(item)

R['results'] = results


def ent_info(e):
    d = {'id': eid(e)}
    try:
        d['area'] = round(float(base.GetFaceArea(e)), 3)
    except Exception:
        pass
    try:
        d['n_cons'] = len(base.PerimetersOfFace([e]))
    except Exception:
        pass
    try:
        d['center'] = [round(float(v), 1) for v in base.EntityCenter(e)]
    except Exception:
        pass
    try:
        bb = base.BoundBox([e])
        d['diag'] = round(((bb[3] - bb[0]) ** 2 + (bb[4] - bb[1]) ** 2
                           + (bb[5] - bb[2]) ** 2) ** 0.5, 2)
    except Exception:
        pass
    return d


# ---- 提取问题面（只走 CheckReport.issues -> entities，实测安全）----
prob, codes = [], []
try:
    obj = base.checks.geometry.ProblematicSurfaces()
    rep = obj.execute(exec_mode=base.Check.EXEC_ON_SELECTED, entities=FACES)
    for r in rep:
        for c in (r.issues or []):
            codes.append(str(getattr(c, 'err_code', '')) + '|'
                         + str(getattr(c, 'message', ''))[:90])
            for e in (c.entities or []):
                prob.append(e)
except Exception:
    R['prob_err'] = traceback.format_exc()[-400:]
R['issue_codes'] = codes
R['problematic_faces'] = [ent_info(e) for e in prob]
R['n_problematic'] = len(prob)

# ---- 组合检查 general.Geometry 的嵌套报告 ----
try:
    obj = base.checks.general.Geometry()
    rep = obj.execute(exec_mode=base.Check.EXEC_ON_SELECTED, entities=FACES)
    nested = []
    for r in rep:
        top = {'type': str(getattr(r, 'type', ''))[:90],
               'status': str(getattr(r, 'status', '')),
               'err_code': str(getattr(r, 'err_code', '')),
               'n_entities': len(r.entities or [])}
        kids = []
        for c in (r.issues or []):
            kids.append({'type': str(getattr(c, 'type', ''))[:90],
                         'status': str(getattr(c, 'status', '')),
                         'err_code': str(getattr(c, 'err_code', '')),
                         'message': str(getattr(c, 'message', ''))[:120],
                         'n_ent': len(c.entities or [])})
        top['children'] = kids[:25]
        nested.append(top)
    R['general_geometry'] = nested
except Exception:
    R['gen_geom_err'] = traceback.format_exc()[-500:]

# ---- 自算拓扑（独立校验）----
cnt = collections.Counter()
fail = 0
for f in FACES:
    try:
        for c in base.PerimetersOfFace([f]):
            k = eid(c)
            if k is None:
                fail += 1
                continue
            cnt[k] += 1
    except Exception:
        fail += 1
hist = collections.Counter(cnt.values())
R['topology'] = {
    'perim_fail': fail,
    'n_cons_seen': len(cnt),
    'share_hist': dict((str(k), v) for k, v in sorted(hist.items())),
    'n_free_edges': hist.get(1, 0),
    'n_manifold_2': hist.get(2, 0),
    'n_nonmanifold_3plus': sum(v for k, v in hist.items() if k > 2),
}

sig = collections.Counter()
for f in FACES:
    try:
        sig[(round(float(base.GetFaceArea(f)), 3),
             tuple(round(float(v), 3) for v in base.EntityCenter(f)))] += 1
    except Exception:
        pass
dup = [{'area': k[0], 'center': list(k[1]), 'count': v}
       for k, v in sig.items() if v > 1]
R['n_duplicate_groups'] = len(dup)
R['duplicates'] = dup[:8]

try:
    R['total_face_area'] = round(sum(float(base.GetFaceArea(f)) for f in FACES), 1)
except Exception:
    pass

print('geom check done')
RESULT = R
