# -*- coding: utf-8 -*-
"""Step 4: 抽取中面并划分网格 —— MidSurfAuto(thick=1.0, length=3.0)。

关键：ANSA 25.1.4 的 MidSurfAuto 只认位置参数，关键字传参会被静默忽略
（不报错、返回 0、耗时 0.0s、模型零变化）。判成败要看耗时与 SHELL 计数。
位置参数顺序（实测）：(thick, faces, exact_middle, connect_weldings, length)
"""
from ansa import base, constants, mesh
import traceback, time, math

R = {}
D = constants.NASTRAN
faces = base.CollectEntities(D, None, 'FACE', False)
R['input_faces'] = len(faces)


def cnt(t):
    try:
        return len(base.CollectEntities(D, None, t, False))
    except Exception:
        return 'ERR'


R['before'] = {'FACE': cnt('FACE'), 'SHELL': cnt('SHELL'),
               'SOLID': cnt('SOLID'), 'ELEMENT': cnt('ELEMENT'), 'NODE': cnt('NODE')}

t0 = time.time()
try:
    ret = base.MidSurfAuto(1.0, faces, False, False, 3.0)
    R['ret'] = repr(ret)
    R['ok'] = True
except Exception:
    R['ok'] = False
    R['err'] = traceback.format_exc()[-2000:]
R['runtime_s'] = round(time.time() - t0, 1)

R['after'] = {'FACE': cnt('FACE'), 'SHELL': cnt('SHELL'),
              'SOLID': cnt('SOLID'), 'ELEMENT': cnt('ELEMENT'), 'NODE': cnt('NODE')}

shells = base.CollectEntities(D, None, 'SHELL', False)
R['n_shells'] = len(shells)

if shells:
    try:
        types = {}
        for s in shells:
            t = base.GetEntityType(D, s)   # 注意：必须 2 个参数 (deck, entity)
            types[t] = types.get(t, 0) + 1
        R['element_types'] = types
    except Exception:
        R['types_err'] = traceback.format_exc()[-300:]

    try:
        n = min(5000, len(shells))
        areas = sorted(float(base.CalcShellArea(s)) for s in shells[:n])
        def q(p):
            return areas[int(p * (len(areas) - 1))]
        R['n_sampled'] = n
        R['area_q'] = {'p10': round(q(.10), 3), 'p50': round(q(.50), 3),
                       'p90': round(q(.90), 3), 'p99': round(q(.99), 3),
                       'max': round(areas[-1], 2)}
        R['edge_mm'] = {'p10': round(math.sqrt(q(.10)), 2),
                        'p50': round(math.sqrt(q(.50)), 2),
                        'p90': round(math.sqrt(q(.90)), 2)}
        R['total_area'] = round(sum(float(base.CalcShellArea(s)) for s in shells), 1)
    except Exception:
        R['area_err'] = traceback.format_exc()[-400:]

    try:
        R['midmesh_score'] = mesh.GetMiddleMeshQualityScore(
            shells, faces, {'middle_surface': '10%', 'missing_mass': 'Mild'})
    except Exception:
        R['score_err'] = traceback.format_exc()[-400:]

    try:
        R['qcheck_total'] = round(float(base.CalcQCHECK(shells, True, True, True)), 5)
        R['qcheck_skew'] = round(float(base.CalcQCHECK(shells, True, False, False)), 5)
        R['qcheck_warp'] = round(float(base.CalcQCHECK(shells, False, True, False)), 5)
        R['qcheck_aspect'] = round(float(base.CalcQCHECK(shells, False, False, True)), 5)
    except Exception:
        R['qcheck_err'] = traceback.format_exc()[-400:]

    try:
        R['off_elements'] = base.CalculateOffElements()
    except Exception:
        R['off_err'] = repr(traceback.format_exc()[-300:])

print('midsurf done')
RESULT = R
