"""문서의 모든 숫자가 옆의 JSON 세 벌에서 재현되는지 확인한다."""
import json
D='docs/superpowers/specs/'
dc=json.load(open(D+'2026-08-02-bandgap-coupling-precondition.json'))
lo=json.load(open(D+'2026-08-02-bandgap-coupling-precondition-amp-loops.json'))
es=json.load(open(D+'2026-08-02-bandgap-coupling-effect-size.json'))
doc=open(D+'2026-08-02-bandgap-coupling-precondition.md').read()
fails=[]
def chk(label, s):
    if s not in doc: fails.append(f'{label}: {s!r} NOT in doc')
def num(v):  # 0은 문서에서 "0.0"으로 적는다 - 소수 넷째자리 0000은 잡음이다
    return '0.0' if v == 0 else f'{v:.4f}'
for d,tag in [(dc,'dc'),(lo,'loops')]:
    chk(tag+' s1sims', str(d['stage1']['unique_sims']))
    chk(tag+' s1wall', f"{d['stage1']['wall_seconds']:.1f}")
    chk(tag+' s2new', str(d['stage2']['new_sims']))
    chk(tag+' s2wall', f"{d['stage2']['wall_seconds']:.1f}")
    chk(tag+' total', str(d['totals']['total_sims']))
    chk(tag+' totwall', f"{d['totals']['total_wall_seconds']:.1f}")
    assert d['bistability_validity']['checked'] is False, tag
chk('dc untested', '| **0** |')
chk('loops untested', '| **51** |')
assert lo['totals']['stage2_untested_candidate_count']==51
assert dc['totals']['stage2_untested_candidate_count']==0
for e in es['area_gain_ranking']['ranked_all'][:12]:
    chk('gain', f"{e['gain']:.4e}")
    chk('knob', f"`{e['refdes']}.{e['param']}`")
assert es['area_gain_ranking']['n_tunable']==167
chk('n_tunable','167'); chk('ranked166','166')
def rows(d, pair):
    return {k.split('|')[2]:v for k,v in d['stage2']['confirmations'].items() if k.startswith(pair+'|')}
for d,pair in [(dc,'BANDGAP.XRl2.l|BANDGAP.XRl2.w'),
               (lo,'TRIMAMP.Xcc.L|TRIMAMP.Xcc.W'),
               (lo,'ERRAMP.Xcc.L|BGR_CORE.Xcc.L')]:
    for m,v in rows(d,pair).items():
        chk(f'{pair}|{m} med', num(v['median_I_rel']))
        chk(f'{pair}|{m} max', num(v['max_I_rel']))
        chk(f'{pair}|{m} n', f"| {v['n_subgrids']} |")
for pair,meas in [('TRIMAMP.Xcc.L|TRIMAMP.Xcc.W','trim_pm_deg'),
                  ('ERRAMP.Xcc.L|BGR_CORE.Xcc.L','core_pm_deg'),
                  ('ERRAMP.Xcc.L|BGR_CORE.Xcc.L','buf0_pm_deg')]:
    r=es['effect_size']['pairs'][pair]['measurements'][meas]
    chk(f'{pair} {meas} base', str(r['baseline']))
    chk(f'{pair} {meas} span', f"{r['grid_span']:.4g}")
    chk(f'{pair} {meas} axA', f"{r['axis_a_span']:.4g}")
    chk(f'{pair} {meas} axB', f"{r['axis_b_span']:.4g}")
    chk(f'{pair} nvalid', f"{r['n_valid_points']}/49")
p=es['effect_size']['pairs']['BUF_P.Xcc.L|BUF_P.Xcc.W']
chk('bufp missing', f"{p['n_missing_points']}점이 없다")
for pr in es['inertness']['probes']:
    if pr['refdes']=='BANDGAP.XRl2':
        a,b=pr['changed_measurements']['vbg0_v']; chk('control probe', f"{a} → {b}")
    else:
        assert pr['identical_to_baseline'] is True, pr
top8=set(es['area_gain_ranking']['top_knobs_used'][:8])
n8=[k for k in dc['stage1']['pairs'] if all(x in top8 for x in k.split('|'))]
assert len(n8)==28 and all(
    all((v['I_rel'] or 0)==0 for v in dc['stage1']['pairs'][k]['measurements'].values()) for k in n8)
chk('28pairs','28쌍')
cb=[q for q in lo['stage2']['confirmed_pairs'] if q[0].rsplit('.',1)[0]!=q[1].rsplit('.',1)[0]]
assert len(cb)==9, cb
chk('9cross','아홉')
assert len(lo['stage1']['coupled_candidates'])==66 and len(lo['stage2']['confirmed_pairs'])==15
assert len(dc['stage1']['coupled_candidates'])==1 and len(dc['stage2']['confirmed_pairs'])==1
print('FAILURES:' if fails else 'ALL DOC NUMBERS REPRODUCE FROM JSON')
for f in fails: print('  ', f)
raise SystemExit(1 if fails else 0)
