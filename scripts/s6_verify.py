# -*- coding: utf-8 -*-
"""Step 5: 成果验证 —— 计数、单元类型分布、文件落地。"""
from ansa import base, constants
import os, traceback

R = {}
D = constants.NASTRAN

for t in ('FACE', 'SHELL', 'SOLID', 'ANSAPART', 'PROPERTY', 'MATERIAL', 'SET'):
    try:
        R[t] = len(base.CollectEntities(D, None, t, False))
    except Exception as e:
        R[t] = 'ERR:' + str(e)[:60]

shells = base.CollectEntities(D, None, 'SHELL', False)
R['n_shells'] = len(shells)

# 正确签名: GetEntityType(deck, entity)
try:
    types = {}
    for s in shells[:3000]:
        t = base.GetEntityType(D, s)
        types[str(t)] = types.get(str(t), 0) + 1
    R['shell_type_sample'] = types
except Exception:
    R['type_err'] = traceback.format_exc()[-300:]

# 中面几何是否单独存在
try:
    R['n_face_now'] = len(base.CollectEntities(D, None, 'FACE', False))
except Exception:
    pass

# 壳单元所在的 PID 分布
try:
    parts = base.CollectEntities(D, None, 'ANSAPART', False)
    R['part_names'] = [str(getattr(p, '_name', p))[:40] for p in parts[:10]]
except Exception:
    R['part_err'] = traceback.format_exc()[-200:]

P = r"C:\Users\Admin\Desktop\Casting_initial_midmesh_3mm.ansa"
R['file_exists'] = os.path.isfile(P)
R['file_size_mb'] = round(os.path.getsize(P) / 1048576.0, 2) if os.path.isfile(P) else None
R['file_mtime'] = __import__('time').strftime('%Y-%m-%d %H:%M:%S', __import__('time').localtime(os.path.getmtime(P))) if os.path.isfile(P) else None

print('verify done')
RESULT = R
