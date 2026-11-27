import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.adapters.baseload import selected_device_baseload
from tests.adapters.test_adapters import pv_file, pv_map, hipe_csv, hipe_map

FIX = Path(__file__).resolve().parents[2] / 'fixtures' / 'timeseries'
UTC=timezone.utc


def test_tzdata_without_system_database():
    code="""from zoneinfo import reset_tzpath, ZoneInfo
from datetime import datetime
reset_tzpath([])
b=ZoneInfo('Europe/Berlin'); s=ZoneInfo('Asia/Shanghai')
a=datetime(2017,10,29,2,30,tzinfo=b,fold=0); c=a.replace(fold=1)
assert a.utcoffset().total_seconds()==7200 and c.utcoffset().total_seconds()==3600
assert s.key=='Asia/Shanghai'
"""
    subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True)


def _configured_with_value(bundle, name, value):
    records=[]
    for r in bundle.records:
        if r.nature=='configured':
            records.append(r.model_copy(update={'value':value,'locator':r.locator.model_copy(update={'meter':name}),
                'quality':r.quality.model_copy(update={'flag':'missing' if value is None else 'ok'})}))
    return bundle.model_copy(update={'bundle_id':'test-'+name,'records':tuple(records)})


@pytest.mark.parametrize('total,parts,expected',[(0.3,[0.1,0.2],0.0),(1.0,[1.0000001],0.0),
    (1.0,[0.9999999],0.0),(1.0,[0.75],0.25),(1.0,[None],None)])
def test_baseload_tolerance(tmp_path,total,parts,expected):
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1'],['2017-10-23T00:00:05+02','M','1']])
    b=HipeAdapter().normalize(p,hipe_map(load_scale={'factor':1}),{})
    a=_configured_with_value(b,'main',total); ds=[_configured_with_value(b,'d'+str(i),v) for i,v in enumerate(parts)]
    r=selected_device_baseload(a,ds).records[0]
    assert r.value==expected if expected is None else r.value==pytest.approx(expected)
    assert r.quality.flag==('missing' if expected is None else 'ok')
    assert r.transform_steps[-1].params['power_zero_kw']==1e-6
    if expected==0.0: assert r.transform_steps[-1].params['raw_residual_kw'] is not None


def test_baseload_rejects_real_negative(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1'],['2017-10-23T00:00:05+02','M','1']]); b=HipeAdapter().normalize(p,hipe_map(),{})
    with pytest.raises(ValueError,match='d0.*exceed'):
        selected_device_baseload(_configured_with_value(b,'main',1),[_configured_with_value(b,'d0',1.00001)])


def test_real_pvgis_excerpt_into_adapter(tmp_path):
    x=json.loads((FIX/'pvgis_2017_excerpt.json').read_text(encoding='utf-8'))
    original=dict(zip(x['source_indices_zero_based'],x['rows']))
    assert [(original[i]['time'],original[i]['P']) for i in [4335,4336,4343,4344]]==[('20170630:1530',0),('20170630:1630',0),('20170630:2330',40.22),('20170701:0030',48.21)]
    response={'inputs':{'meteo_data':{'radiation_db':'PVGIS-ERA5'},'pv_module':{'peak_power':1}},'meta':{'outputs':{'hourly':{'timestamp':'hourly averages','variables':{'P':{'units':'W'}}}}},'outputs':{'hourly':x['rows']}}
    p=tmp_path/'pvgis_excerpt_input.json';p.write_text(json.dumps(response),encoding='utf-8')
    b=PvgisAdapter().normalize(p,pv_map(),{})
    index=x['source_indices_zero_based'].index(4344)
    rec=[r for r in b.records if r.transform_steps[0].params.get('source_index_zero_based')==index]
    assert len(rec)==4 and [r.value for r in rec]==pytest.approx([24.105]*4)
    assert all(r.quality.flag=='estimated' for r in rec)
    assert rec[0].interval.start==datetime(2026,7,1,0,tzinfo=UTC)
    assert all(r.available_at==datetime(2026,7,1,1,tzinfo=UTC) for r in rec)
    assert all(r.content_hash=='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest() for r in rec)
    assert x['source_sha256'] != hashlib.sha256(p.read_bytes()).hexdigest()
    assert b.records[0].value==0 and b.records[0].transform_steps[0].params['source_index_zero_based']==x['source_indices_zero_based'].index(4336)
    assert not any(r.transform_steps[0].params.get('source_index_zero_based')==x['source_indices_zero_based'].index(4335) for r in b.records)
    assert b.records[28].value==pytest.approx(20.11)


@pytest.mark.parametrize('meter',['MainTerminal','VacuumPump1'])
def test_real_hipe_excerpt_into_adapter(tmp_path,meter):
    x=json.loads((FIX/'hipe_excerpt.json').read_text(encoding='utf-8'))
    rows=[r for r in x['rows'] if r['Machine']==meter and r['SensorDateTime'].startswith('2017-10-29T02:')]
    assert len(rows)==10 and {r['SensorDateTime'][-3:] for r in rows}=={'+01','+02'}
    rows.sort(key=lambda r:datetime.fromisoformat(r['SensorDateTime']).astimezone(UTC))
    p=hipe_csv(tmp_path,[[r['SensorDateTime'],r['Machine'],r['P_kW']] for r in rows])
    b=HipeAdapter().normalize(p,{'source_start':'2017-10-29','plant_start':'2026-07-01','days':1},{})
    raw=[r for r in b.records if r.nature=='measured'];target=[r for r in b.records if r.nature=='configured']
    assert len(raw)==100 and len(target)==96
    assert raw[8].transform_steps[0].params['coverage_seconds']>0
    assert raw[12].transform_steps[0].params['coverage_seconds']>0
    assert target[8].transform_steps[0].params['dropped_repeat_segments']>0
    assert target[8].transform_steps[0].params['coverage_seconds']==pytest.approx(raw[8].transform_steps[0].params['coverage_seconds'])
    assert target[8].value is None and target[8].quality.flag=='missing'
    assert target[8].transform_steps[0].params['known_energy_kwh']==pytest.approx(raw[8].transform_steps[0].params['known_energy_kwh']*25)
    assert target[8].content_hash=='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()
    assert x['archive_sha256'] != hashlib.sha256(p.read_bytes()).hexdigest()
    assert all(r['source_line_1_based']>1 for r in rows)


@pytest.mark.parametrize('key',['source_start','plant_start','source_peak_kwp','target_peak_kwp'])
def test_missing_pv_mapping_key(tmp_path,key):
    m=pv_map();del m[key]
    with pytest.raises(ValueError,match=key): PvgisAdapter().normalize(pv_file(tmp_path),m,{})


@pytest.mark.parametrize('path', [('inputs',),('meta',),('outputs',),('meta','outputs','hourly','variables','P','units')])
def test_pv_missing_structure(tmp_path,path):
    p=pv_file(tmp_path); d=json.loads(p.read_text()); parent=d
    for key in path[:-1]: parent=parent[key]
    del parent[path[-1]];p.write_text(json.dumps(d))
    with pytest.raises(ValueError,match=path[-1]): PvgisAdapter().normalize(p,pv_map(),{})


def test_bundle_ids_stable_and_config_sensitive(tmp_path):
    h=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1'],['2017-10-23T00:00:05+02','M','1']]); a=HipeAdapter()
    base=a.normalize(h,hipe_map(),{}); same=a.normalize(h,dict(reversed(list(hipe_map(load_scale={'factor':25}).items()))),{}); one=a.normalize(h,hipe_map(load_scale={'factor':1}),{})
    assert base.bundle_id==same.bundle_id!=one.bundle_id
    assert base.bundle_id!=a.normalize(h,hipe_map(),{'delay_seconds':1}).bundle_id
    p=pv_file(tmp_path); pv=PvgisAdapter(); assert pv.normalize(p,pv_map(),{}).bundle_id!=pv.normalize(p,pv_map(target_peak_kwp=250),{}).bundle_id
    assert pv.normalize(p,pv_map(bundle_id='custom'),{}).bundle_id=='custom'
    total=_configured_with_value(base,'main',10); d1=_configured_with_value(base,'d1',1);d2=_configured_with_value(base,'d2',1)
    assert selected_device_baseload(total,[d1]).bundle_id!=selected_device_baseload(total,[d2]).bundle_id
    assert selected_device_baseload(total,[d1,d2]).bundle_id==selected_device_baseload(total,[d2,d1]).bundle_id


def test_bang_paths_and_zip_members(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1'],['2017-10-23T00:00:05+02','M','1']]);
    parent=tmp_path/'dir!one';parent.mkdir(); special=parent/'sample!meter.csv';special.write_bytes(p.read_bytes())
    assert HipeAdapter().normalize(special,hipe_map(),{}).records
    zpath=parent/'archive!data.zip'
    with zipfile.ZipFile(zpath,'w') as z:z.writestr('meter.csv',p.read_bytes())
    assert HipeAdapter().normalize(str(zpath)+'!meter.csv',hipe_map(),{}).records
    with pytest.raises(ValueError,match='member not found'):HipeAdapter().normalize(str(zpath)+'!missing.csv',hipe_map(),{})


def test_pv_quality_zero_negative_and_missing(tmp_path):
    p=pv_file(tmp_path);d=json.loads(p.read_text());d['outputs']['hourly'][1]['P']=0;p.write_text(json.dumps(d)); a=PvgisAdapter()
    b=a.normalize(p,pv_map(),{});assert b.records[0].value==0 and b.records[0].quality.flag=='estimated';assert b.records[4].quality.flag=='missing'
    d['outputs']['hourly'][1]['P']=-0.1;p.write_text(json.dumps(d))
    with pytest.raises(ValueError,match='negative P.*index 1'):a.normalize(p,pv_map(),{})


def test_baseload_rejects_misaligned_and_factor_mismatch(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1'],['2017-10-23T00:00:05+02','M','1']])
    b=HipeAdapter().normalize(p,hipe_map(),{}); total=_configured_with_value(b,'main',3); device=_configured_with_value(b,'pump',1)
    r=device.records[0]; st=r.transform_steps[0]; bad_step=st.model_copy(update={'params':{**st.params,'factor':1}})
    different=device.model_copy(update={'records':(r.model_copy(update={'transform_steps':(bad_step,)}),)+device.records[1:]})
    with pytest.raises(ValueError,match='factors differ'):selected_device_baseload(total,[different])
    from plantkg.core.contracts import Interval
    shifted=Interval(start=r.interval.start+timedelta(minutes=15),end=r.interval.end+timedelta(minutes=15))
    bad=device.model_copy(update={'records':(r.model_copy(update={'interval':shifted}),)+device.records[1:]})
    with pytest.raises(ValueError,match='not aligned'):selected_device_baseload(total,[bad])
    with pytest.raises(ValueError,match='distinct'):selected_device_baseload(total,[device,device])


def test_hipe_missing_required_date(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1']])
    for key in ('source_start','plant_start'):
        m=hipe_map();del m[key]
        with pytest.raises(ValueError,match=key): HipeAdapter().normalize(p,m,{})


def test_pvgis_invalid_time_and_out_of_window_negative(tmp_path):
    p=pv_file(tmp_path); data=json.loads(p.read_text()); data['outputs']['hourly'][0]['time']=None; p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match=r'outputs.hourly\[0\].time'):PvgisAdapter().normalize(p,pv_map(),{})
    data['outputs']['hourly'][0]['time']='20170630:1530'; data['outputs']['hourly'][0]['P']=-1; p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='negative P.*index 0'):PvgisAdapter().normalize(p,pv_map(),{})


def test_fixture_hashes_match_manifest():
    manifest=json.loads((FIX/'source_manifest.json').read_text(encoding='utf-8'))
    for name in ('hipe','pvgis'):
        entry=manifest[name]
        assert hashlib.sha256((FIX/entry['fixture']).read_bytes()).hexdigest()==entry['fixture_sha256']
    for name in ('synthetic_fixture','real_week_report'):
        entry=manifest[name]
        assert hashlib.sha256((FIX/entry['file']).read_bytes()).hexdigest()==entry['sha256']
