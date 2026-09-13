# -*- coding: utf-8 -*-
"""Step 1: 模型诊断 —— 只读、安全调用。"""
from ansa import base, constants
import traceback

R = {}
try:
    d = base.CurrentDeck()
except Exception:
    d = constants.NASTRAN
R['deck'] = int(d)


def cnt(t):
    try:
        return len(base.CollectEntities(d, None, t, False))
    except Exception as e:
        return 'ERR:' + str(e)[:60]


for t in ('FACE', 'CONS', 'SOLID', 'VOLUME', 'SHELL', 'ANSAPART', 'PROPERTY', 'MATERIAL', 'SET'):
    R['n_' + t] = cnt(t)

faces = base.CollectEntities(d, None, 'FACE', False)
R['n_faces'] = len(faces)

try:
    bb = base.BoundBox(faces)
    R['bbox'] = [round(float(v), 1) for v in bb]
    R['bbox_size'] = [round(float(bb[3] - bb[0]), 1),
                      round(float(bb[4] - bb[1]), 1),
                      round(float(bb[5] - bb[2]), 1)]
except Exception:
    R['bbox_err'] = repr(traceback.format_exc()[-200:])

# 几何描述判定：是否已是中面
try:
    R['is_solid_desc'] = base.DetectSolidDescription(faces, fix_unchecked_faces=False)
except Exception:
    R['is_solid_desc_err'] = traceback.format_exc()[-400:]

try:
    pct = base.DetectSolidDescription(faces, thickness=1.0,
                                      return_percentage=True,
                                      fix_unchecked_faces=False)
    R['pct_at_1mm'] = [round(float(v), 4) for v in pct]
except Exception:
    R['pct_err'] = traceback.format_exc()[-300:]

# 壁厚分布
try:
    est = base.DetectSolidDescription(
        faces, estimate_thickness=True,
        separate_connectivity=True,
        separate_connectivity_stop_at_pid_bound=True,
        estimate_thickness_num_decimals=2,
        fix_unchecked_faces=False)
    R['n_conn_groups'] = len(est) if est else 0
    groups = []
    for g in (est or [])[:5]:
        items = sorted(((float(k), float(v)) for k, v in g.items()), key=lambda t: -t[1])[:5]
        groups.append([[round(a, 2), round(b, 3)] for a, b in items])
    R['thickness_top'] = groups
except Exception:
    R['est_err'] = traceback.format_exc()[-600:]

print('diag done')
RESULT = R
