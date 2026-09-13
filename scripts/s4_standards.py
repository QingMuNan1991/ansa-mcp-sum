# -*- coding: utf-8 -*-
"""Step 3: 载入网格参数 + 质量标准，并做前后对比与回读校验。"""
from ansa import base, mesh, constants
import os, time, traceback

MPAR = r"F:\ANSA_Standard\mesh_ftrd_3mm.ansa_mpar"
QUAL = r"F:\ANSA_Standard\mesh_3mm.ansa_qual"
BACKUP = r"F:\ANSA_Standard\_backup"

KEYS = ("mesh_parameters_name", "target_element_length", "perimeter_length",
        "general_min_target_len", "general_curvature_minimum_length",
        "mesh_type", "element_type", "element_order", "existing_mesh_treatment",
        "general_max_target_len")

R = {}


def snap():
    try:
        return dict(base.BCSettingsGetValues(KEYS) or {})
    except Exception:
        return {'ERR': traceback.format_exc()[-200:]}


R['before'] = snap()

# ---------- 备份当前设置 ----------
try:
    if not os.path.isdir(BACKUP):
        os.makedirs(BACKUP)
except Exception:
    BACKUP = os.environ.get('TEMP', '.')
R['backup_dir'] = BACKUP
ts = time.strftime('%Y%m%d_%H%M%S')
b_mp = os.path.join(BACKUP, 'before_mesh_params_%s.ansa_mpar' % ts)
b_qc = os.path.join(BACKUP, 'before_quality_%s.ansa_qual' % ts)
try:
    R['save_before_mp'] = mesh.SaveMeshParams(b_mp)
    R['before_mp_size'] = os.path.getsize(b_mp)
except Exception:
    R['save_before_mp_err'] = traceback.format_exc()[-300:]
try:
    R['save_before_qc'] = mesh.SaveQualityCriteria(b_qc)
    R['before_qc_size'] = os.path.getsize(b_qc)
except Exception:
    R['save_before_qc_err'] = traceback.format_exc()[-300:]

# ---------- 载入 ----------
R['src_mp_exists'] = os.path.isfile(MPAR)
R['src_qc_exists'] = os.path.isfile(QUAL)

t0 = time.time()
try:
    R['read_mp'] = mesh.ReadMeshParams(MPAR)
except Exception:
    R['read_mp_err'] = traceback.format_exc()[-800:]
R['read_mp_s'] = round(time.time() - t0, 2)

t0 = time.time()
try:
    R['read_qc'] = mesh.ReadQualityCriteria(QUAL)
except Exception:
    R['read_qc_err'] = traceback.format_exc()[-800:]
R['read_qc_s'] = round(time.time() - t0, 2)

R['after'] = snap()

# ---------- 回读比对（写出去再解析） ----------
def parse(fn):
    d = {}
    try:
        f = open(fn, 'r')
        for line in f:
            if '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            if (not k) or k.startswith('#'):
                continue
            d[k] = ' '.join(v.split())
        f.close()
    except Exception:
        pass
    return d


a_mp = os.path.join(BACKUP, 'after_mesh_params_%s.ansa_mpar' % ts)
a_qc = os.path.join(BACKUP, 'after_quality_%s.ansa_qual' % ts)
try:
    mesh.SaveMeshParams(a_mp)
except Exception:
    R['save_after_mp_err'] = traceback.format_exc()[-200:]
try:
    mesh.SaveQualityCriteria(a_qc)
except Exception:
    R['save_after_qc_err'] = traceback.format_exc()[-200:]


def diff(src, dst, skip=('ANSA_Version',)):
    s, t = parse(src), parse(dst)
    match, mismatch, missing = 0, [], []
    for k in s:
        if k in skip:
            continue
        if k not in t:
            missing.append(k)
        elif t[k] == s[k]:
            match += 1
        else:
            mismatch.append([k, s[k], t[k]])
    return {'src_keys': len(s), 'after_keys': len(t), 'match': match,
            'n_mismatch': len(mismatch), 'n_missing': len(missing),
            'mismatch': mismatch[:12], 'missing_sample': missing[:12]}


R['diff_params'] = diff(MPAR, a_mp)
R['diff_quality'] = diff(QUAL, a_qc)

p_qc = parse(a_qc)
R['quality_key'] = dict((k, p_qc.get(k)) for k in
                        ('name', 'min length [shells]', 'max length [shells]',
                         'aspect ratio [shells]', 'warping [shells]',
                         'min angle quads [shells]', 'max angle quads [shells]'))

# ---------- 模型未受影响 ----------
D = {}
for t in ('FACE', 'SHELL', 'SOLID', 'ELEMENT', 'NODE'):
    try:
        D[t] = len(base.CollectEntities(constants.NASTRAN, None, t, False))
    except Exception:
        D[t] = 'ERR'
R['model'] = D

print('standards loaded')
RESULT = R
