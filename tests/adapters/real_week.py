"""Opt-in full real-data audit: python -m tests.adapters.real_week ZIP PVJSON OUTPUT."""
import json
import math
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.adapters.common import map_wall
from plantkg.adapters.baseload import selected_device_baseload
from zoneinfo import ZoneInfo


def main(archive, pv_path, output):
    m={'source_start':'2017-10-23','plant_start':'2026-07-01','days':7}
    with zipfile.ZipFile(archive) as z:
        names=[n for n in z.namelist() if n.endswith('.csv')]
    audit={'status':'post-repair real-data audit; capacity candidates, not business sign-off','hipe':{},'pvgis':{},'checks':{},'limitations':[]}
    configured={}
    bundles={}
    first_bundle=None
    for name in names:
        print('HIPE',name,flush=True)
        machine=name.split('_PhaseCount')[0]
        bundle=HipeAdapter().normalize(str(archive)+'!'+name,m,{})
        if machine=='MainTerminal': first_bundle=bundle
        raw=[r for r in bundle.records if r.nature=='measured']
        out=[r for r in bundle.records if r.nature=='configured']
        assert len(raw)==676 and len(out)==672
        assert all(r.available_at>=r.interval.end for r in bundle.records)
        assert all(r.locator.meter==machine for r in bundle.records)
        vals=[r.value for r in out if r.value is not None]
        configured[machine]=out
        bundles[machine]=bundle
        raw_vals=[r.value for r in raw if r.value is not None]
        kept=[r for r in raw if not r.interval.start.astimezone(ZoneInfo("Europe/Berlin")).fold]
        retained_kwh= sum(r.transform_steps[0].params["known_energy_kwh"] for r in kept)
        retained_sec=sum(r.transform_steps[0].params["coverage_seconds"] for r in kept)
        totals=lambda rr:sum(r.transform_steps[0].params['known_energy_kwh'] for r in rr)
        coverage=lambda rr:sum(r.transform_steps[0].params['coverage_seconds'] for r in rr)
        audit['hipe'][machine]={'raw_slots':len(raw),'target_slots':len(out),'raw_missing':sum(r.value is None for r in raw),
            'target_missing':sum(r.value is None for r in out),'target_partial':sum(0<r.transform_steps[0].params['coverage_seconds']<899.999 for r in out),
            'duplicate_rows':out[0].transform_steps[0].params['duplicate_rows'], 'conflicting_rows':0,
            'raw_mean_complete_kw':round(sum(raw_vals)/len(raw_vals),6),'raw_peak_complete_kw':round(max(raw_vals),6),
            'target_factor1_mean_complete_kw':round(sum(vals)/len(vals)/25,6),'target_factor1_peak_complete_kw':round(max(vals)/25,6),
            'retained_known_kwh':round(retained_kwh,6),'retained_known_seconds':round(retained_sec,3),
            'raw_known_kwh':round(totals(raw),6),'target_known_kwh':round(totals(out),6),
            'raw_known_seconds':round(coverage(raw),3),'target_known_seconds':round(coverage(out),3),
            'target_mean_complete_kw':round(sum(vals)/len(vals),6),'target_peak_complete_kw':round(max(vals),6),
            'dropped_repeat_segments':out[0].transform_steps[0].params['dropped_repeat_segments'],
            'source_sha256':out[0].content_hash}
        assert math.isclose(totals(out)/25,retained_kwh,rel_tol=1e-9,abs_tol=1e-6), (machine, totals(out)/25,retained_kwh)
        assert math.isclose(coverage(out),retained_sec,abs_tol=0.002), (machine, coverage(out),retained_sec)
        audit['hipe'][machine]['energy_and_coverage_retained_conservation']='passed'
        # A mapped, complete, retained source bucket must agree with its configured view.
        compared=0
        for r in raw:
            local=r.interval.start.astimezone(ZoneInfo('Europe/Berlin'))
            if local.fold or r.value is None: continue
            t=map_wall(r.interval.start,datetime.fromisoformat(m['source_start']).date(),datetime.fromisoformat(m['plant_start']).date(),ZoneInfo('Europe/Berlin'),ZoneInfo('Asia/Shanghai'))
            matches=[x for x in out if x.interval.start==t]
            if matches and matches[0].value is not None:
                assert math.isclose(matches[0].value,r.value*25,rel_tol=1e-9,abs_tol=1e-8)
                compared+=1
        audit['hipe'][machine]['mapped_raw_scale_compared']=compared
    assert first_bundle is not None and len(names)==11
    member=next(n for n in names if n.startswith('MainTerminal_'))
    one=HipeAdapter().normalize(str(archive)+'!'+member,{**m,'load_scale':{'factor':1}},{})
    repeat=HipeAdapter().normalize(str(archive)+'!'+member,m,{})
    target25=[r for r in first_bundle.records if r.nature=='configured']
    target1=[r for r in one.records if r.nature=='configured']
    target_again=[r for r in repeat.records if r.nature=='configured']
    assert [r.value for r in target25]==[r.value for r in target_again]
    assert all(a.value is None and b.value is None or a.value is not None and b.value is not None and math.isclose(a.value,b.value*25,rel_tol=1e-12) for a,b in zip(target25,target1))
    devices=[n for n in configured if n!='MainTerminal']
    residual=[]
    for i,total in enumerate(configured['MainTerminal']):
        parts=[configured[d][i] for d in devices]
        if total.value is not None and all(r.value is not None for r in parts):
            residual.append(total.value-sum(r.value for r in parts))
    assert residual
    audit['checks']['total_minus_all_submeters']={'comparable_slots':len(residual),'negative_slots':sum(x < -1e-6 for x in residual),'min_kw':round(min(residual),6),'max_kw':round(max(residual),6),'interpretation':'diagnostic only; not the selected-device deduction policy'}
    audit['checks']['factor_25_1_25']='passed for MainTerminal from raw input'
    baseline=selected_device_baseload(bundles['MainTerminal'],[bundles['VacuumPump1']])
    assert all(r.value is None or r.value >= 0 for r in baseline.records)
    audit['checks']['selected_vacuum_pump1_baseload']={'slots':len(baseline.records),
        'missing':sum(r.value is None for r in baseline.records), 'provenance_inputs':2,
        'note':'selected candidate meter deducted once; no production job inferred'}
    runs=[]; active=[]
    for r in configured['VacuumPump1']:
        if r.value is not None and r.value >= 12.5:
            active.append(r)
        elif active:
            runs.append(active); active=[]
    if active: runs.append(active)
    if runs:
        run=max(runs,key=len)
        audit['checks']['vacuum_pump1_candidate']={'start':run[0].interval.start.isoformat(),
            'end':run[-1].interval.end.isoformat(),'slots':len(run),'threshold_configured_kw':12.5,
            'mean_kw':round(sum(r.value for r in run)/len(run),6),'max_kw':max(r.value for r in run),
            'note':'threshold candidate only; not a confirmed job or rated power'}
    pm={'source_start':'2017-07-01','plant_start':'2026-07-01','days':7,'pvgis_version':'5.3','source_peak_kwp':1,'target_peak_kwp':500}
    pv=PvgisAdapter().normalize(pv_path,pm,{})
    assert len(pv.records)==672
    assert all(r.nature=='derived' and r.available_at>=r.interval.end for r in pv.records)
    assert all(r.quality.flag == ('missing' if r.value is None else 'estimated') for r in pv.records)
    for i in range(0,len(pv.records),4):
        group=pv.records[i:i+4]
        assert len({r.available_at for r in group})==1
        assert group[0].available_at>=group[-1].interval.end
        assert len({r.value for r in group})==1
    vals=[r.value for r in pv.records if r.value is not None]
    audit['pvgis']={'slots':len(pv.records),'missing':len(pv.records)-len(vals),'source_sha256':pv.records[0].content_hash,
        'mean_kw':round(sum(vals)/len(vals),6),'peak_kw':max(vals),'total_kwh':round(sum(vals)*0.25,6),
        'zero_slots':sum(v==0 for v in vals),'source_kwp':1,'target_kwp':500,'hour_interval_assumption':'pvgis-era5-p-centered-hour-v1'}
    audit['checks']['pvgis_hour_visibility_and_once_scaling']='passed'
    audit['limitations']=['PVGIS P centered-hour interpretation is an explicit assumption, not independently proven by official P field',
        'Q25 selected-device replacement and B13/B14 business semantics require downstream confirmation',
        'real-data check does not approve PV/storage sizing or imply same-site synchronous observation']
    Path(output).write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('saved',output,flush=True)


if __name__=='__main__':
    if len(sys.argv)!=4: raise SystemExit(__doc__)
    main(*sys.argv[1:])
