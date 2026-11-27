import csv
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from plantkg.adapters import HipeAdapter, PvgisAdapter

UTC = timezone.utc


def pv_file(tmp_path):
    p=tmp_path/'pv.json'
    p.write_text(json.dumps({'inputs':{'meteo_data':{'radiation_db':'PVGIS-ERA5'},'pv_module':{'peak_power':1}},
        'meta':{'outputs':{'hourly':{'timestamp':'hourly averages','variables':{'P':{'units':'W'}}}}},
        'outputs':{'hourly':[{'time':'20170630:1530','P':100},{'time':'20170630:1630','P':48.21}]}}))
    return p


def pv_map(**kw):
    return {'source_start':'2017-07-01','plant_start':'2026-07-01','days':1,'pvgis_version':'5.3',
        'source_peak_kwp':1,'target_peak_kwp':500,**kw}


def hipe_csv(tmp_path,rows):
    p=tmp_path/'hipe.csv'
    with p.open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['SensorDateTime','Machine','P_kW']);w.writerows(rows)
    return p


def hipe_map(**kw):
    return {'source_start':'2017-10-23','plant_start':'2026-07-01','days':1,**kw}


def test_pv_scale_once_and_visibility(tmp_path):
    b=PvgisAdapter().normalize(pv_file(tmp_path),pv_map(),{})
    rows=b.records[:4]
    assert len(b.records)==96
    assert [r.value for r in rows]==pytest.approx([24.105]*4)
    assert len({r.available_at for r in rows})==1
    assert rows[0].available_at==rows[-1].interval.end
    assert all(r.nature=='derived' for r in rows)
    assert rows[0].transform_steps[0].params['capacity_ratio']==500
    assert b.records[4].value is None


def test_pv_rejects_wrong_capacity_and_clock(tmp_path):
    p=pv_file(tmp_path)
    with pytest.raises(ValueError): PvgisAdapter().normalize(p,pv_map(source_peak_kwp=2),{})
    with pytest.raises(ValueError): PvgisAdapter().normalize(p,pv_map(target_peak_kwp=0),{})
    with pytest.raises(ValueError): PvgisAdapter().normalize(p,pv_map(),{'delay_seconds':-1})
    d=json.loads(p.read_text());d['outputs']['hourly'][0]['time']='20170630:1500';p.write_text(json.dumps(d))
    with pytest.raises(ValueError): PvgisAdapter().normalize(p,pv_map(),{})


def test_hipe_cross_boundary_zero_missing_and_scaling(tmp_path):
    rows=[]
    t=datetime(2017,10,23,0,0,tzinfo=ZoneInfo('Europe/Berlin'))
    for i in range(181): rows.append([(t+timedelta(seconds=5*i)).isoformat(),'MainTerminal',2])
    p=hipe_csv(tmp_path,rows)
    adapter=HipeAdapter(); a=adapter.normalize(p,hipe_map(),{}); one=adapter.normalize(p,hipe_map(load_scale={'factor':1}),{}); again=adapter.normalize(p,hipe_map(),{})
    cfg=[r for r in a.records if r.nature=='configured']; raw=[r for r in a.records if r.nature=='measured']
    assert len(cfg)==96 and len(raw)==96
    assert cfg[0].value==pytest.approx(50) and raw[0].value==pytest.approx(2)
    assert [r.value for r in cfg]==[r.value for r in again.records if r.nature=='configured']
    assert [r for r in one.records if r.nature=='configured'][0].value==pytest.approx(2)
    assert cfg[1].value is None and cfg[1].quality.flag=='missing'
    assert cfg[0].available_at>=cfg[0].interval.end
    assert cfg[0].transform_steps[0].params['coverage_seconds']==900


def test_hipe_gaps_duplicate_conflict_and_offsets(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-23T00:14:55+02','M','2'],['2017-10-23T00:16:30+02','M','3']])
    cfg=[r for r in HipeAdapter().normalize(p,hipe_map(),{}).records if r.nature=='configured']
    assert cfg[0].value is None and cfg[1].value is None
    assert cfg[0].transform_steps[0].params['known_energy_kwh']==pytest.approx(2*5/3600*25)
    assert cfg[1].transform_steps[0].params['known_energy_kwh']==pytest.approx(2*55/3600*25)
    assert cfg[0].transform_steps[0].params['dropped_repeat_intervals']==[]  # 没有重复小时就没有丢弃区间
    assert cfg[0].transform_steps[0].params['dropped_energy_kwh']==0.0
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+02','M','1'],['2017-10-23T00:00:00+02','M','2']])
    with pytest.raises(ValueError,match='conflicting'): HipeAdapter().normalize(p,hipe_map(),{})
    p=hipe_csv(tmp_path,[['2017-10-23T00:00:00+01','M','1']])
    with pytest.raises(ValueError,match='offset'): HipeAdapter().normalize(p,hipe_map(),{})


def test_dst_keep_first(tmp_path):
    start=datetime(2017,10,29,0,0,tzinfo=UTC)
    rows=[]
    for n in range(0, 7201, 5):
        t=start+timedelta(seconds=n); loc=t.astimezone(ZoneInfo('Europe/Berlin'))
        rows.append([loc.isoformat(),'M',1 if loc.fold==0 else 9])
    p=hipe_csv(tmp_path,rows)
    b=HipeAdapter().normalize(p,{'source_start':'2017-10-29','plant_start':'2026-07-01','days':1},{})
    cfg=[r for r in b.records if r.nature=='configured']; raw=[r for r in b.records if r.nature=='measured']
    assert len(cfg)==96 and len(raw)==100
    assert all(r.value==pytest.approx(25) for r in cfg[8:12])
    assert raw[12].value==pytest.approx(9)
    assert cfg[8].transform_steps[0].params['dropped_repeat_segments']>0


def test_dst_dropped_repeat_intervals_and_energy(tmp_path):
    start=datetime(2017,10,29,0,0,tzinfo=UTC)
    rows=[]
    for n in range(0, 7201, 5):
        t=start+timedelta(seconds=n); loc=t.astimezone(ZoneInfo('Europe/Berlin'))
        rows.append([loc.isoformat(),'M',9 if loc.fold==1 else 1])
    p=hipe_csv(tmp_path,rows)
    cfg=[r for r in HipeAdapter().normalize(p,{'source_start':'2017-10-29','plant_start':'2026-07-01','days':1},{}).records if r.nature=='configured']
    params=cfg[8].transform_steps[0].params
    assert params['dropped_repeat_segments']>0  # 保留段数（采样段）
    (dropped,)=params['dropped_repeat_intervals']  # 相邻丢弃段合并成一个源区间
    assert dropped['source_interval_utc']==['2017-10-29T01:00:00+00:00','2017-10-29T02:00:00+00:00']
    assert (dropped['source_line_start'],dropped['source_line_end'])==(722,1442)  # 一基行号：首个与末个重复小时采样的行
    assert params['dropped_energy_kwh']==pytest.approx(9.0)  # 9 kW × 重复小时的 3600 秒，按源功率 1 倍计
    assert dropped['energy_kwh']==pytest.approx(params['dropped_energy_kwh'])


def test_selected_baseload_provenance_and_duplicate_rejection(tmp_path):
    from plantkg.adapters.baseload import selected_device_baseload
    t=datetime(2017,10,23,0,0,tzinfo=ZoneInfo('Europe/Berlin'))
    rows=lambda meter,power:[[(t+timedelta(seconds=5*i)).isoformat(),meter,power] for i in range(181)]
    p=hipe_csv(tmp_path,rows('MainTerminal',2)); total=HipeAdapter().normalize(p,hipe_map(),{})
    p=hipe_csv(tmp_path,rows('VacuumPump1',1)); device=HipeAdapter().normalize(p,hipe_map(),{})
    net=selected_device_baseload(total,[device]); assert net.records[0].value==pytest.approx(25)
    assert net.records[0].available_at==max(total.records[96].available_at,device.records[96].available_at)
    assert len(net.records[0].transform_steps[-1].params['inputs'])==2
    with pytest.raises(ValueError,match='distinct'): selected_device_baseload(total,[device,device])
    p=hipe_csv(tmp_path,rows('VacuumPump2',3)); bigger=HipeAdapter().normalize(p,hipe_map(),{})
    with pytest.raises(ValueError,match='exceed'): selected_device_baseload(total,[bigger])


def test_dst_repeated_hour_end_keeps_post_transition_seconds(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-29T02:59:58+01','M','2'],['2017-10-29T03:00:03+01','M','2']])
    b=HipeAdapter().normalize(p,{'source_start':'2017-10-29','plant_start':'2026-07-01','days':1},{})
    cfg=[r for r in b.records if r.nature=='configured']
    assert cfg[12].transform_steps[0].params['coverage_seconds']==pytest.approx(3)
    assert cfg[12].transform_steps[0].params['known_energy_kwh']==pytest.approx(2*3/3600*25)


def test_visibility_inherits_next_input_and_pv_hour(tmp_path):
    p=hipe_csv(tmp_path,[['2017-10-23T00:14:59+02','M','2'],['2017-10-23T00:15:20+02','M','2']])
    b=HipeAdapter().normalize(p,hipe_map(),{'delay_seconds':10})
    raw=[r for r in b.records if r.nature=='measured'][0]
    cfg=[r for r in b.records if r.nature=='configured'][0]
    assert raw.available_at == datetime(2017,10,22,22,15,30,tzinfo=UTC)
    assert cfg.available_at == datetime(2026,6,30,16,15,30,tzinfo=UTC)
    assert raw.transform_steps[0].params['source_timestamp_text_first_and_dependency_last']==['2017-10-23T00:14:59+02','2017-10-23T00:15:20+02']
    pv=PvgisAdapter().normalize(pv_file(tmp_path),pv_map(),{'delay_seconds':10}).records[:4]
    assert all(r.available_at==pv[0].interval.start+timedelta(hours=1,seconds=10) for r in pv)
    assert all(not (r.available_at <= pv[0].available_at-timedelta(microseconds=1)) for r in pv)
