import type { AgentRun, Day, DayEvent, Decision, KnowledgeVersion, Proposal, QaEntry, StateFrame, Ticket, WorkOrder } from './types'

/**
 * 内置样例只有 08:05 前后一小段，展示页需要完整一天。
 * 这里补的事件、事件单、提案、裁决、工单、知识版本与时序帧全部标 synthetic / simulated，
 * 界面上标“内置样例 · 含合成补全”。B36 导出真实 day.json 后走不到这里。
 */
export function withShowcaseDemo(day: Day): Day {
  const data = structuredClone(day)
  const [year, month, date] = data.local_date.split('-').map(Number)
  // contracts/examples/day.sample.json 是 Asia/Shanghai（UTC+8）的演示日。
  const utc = (hour: number, minute = 0, second = 0) => new Date(Date.UTC(year, month - 1, date, hour - 8, minute, second)).toISOString().replace('.000Z', 'Z')

  const events: DayEvent[] = [
    { event_id: 'event://demo-doc-ingest', at: utc(9, 30), event_type: 'document_ingest', source: 'script', entity_id: null, severity: 2, summary: '新到《空压机运行维护手册》，资料入库 Agent 比对泄漏判别阈值', nature: 'synthetic', payload: { document: 'maintenance-manual' } },
    { event_id: 'event://demo-vibration', at: utc(11, 20), event_type: 'equipment_vibration', source: 'sim_limit', entity_id: 'entity://demo-electronics/compressor/AIR-1', severity: 2, summary: '空压机振动略高于基线，设备监测继续观察', nature: 'synthetic' },
    { event_id: 'event://demo-air-leak', at: utc(10, 30), event_type: 'air_pressure_abnormal', source: 'sim_limit', entity_id: 'entity://demo-electronics/compressor/AIR-1', severity: 3, summary: '空压站压力下降速率超阈值，电流同步上升，判为泄漏', nature: 'synthetic', payload: { pressure_drop_mpa_per_min: 0.08 } },
    { event_id: 'event://demo-light-fault', at: utc(10, 30), event_type: 'lighting_fault', source: 'sim_limit', entity_id: 'entity://demo-electronics/lighting/ADMIN-3F', severity: 3, summary: '行政楼 3 层照明回路故障', nature: 'synthetic' },
    { event_id: 'event://demo-elev-fault', at: utc(14, 20), event_type: 'elevator_fault', source: 'script', entity_id: 'entity://demo-electronics/elevator_group/EG-PROD', severity: 5, summary: '2 号货梯故障停在两层之间，轿厢内有人', nature: 'synthetic', payload: { car: 2 } },
    { event_id: 'event://demo-unknown', at: utc(16, 5), event_type: 'compound_anomaly', source: 'script', entity_id: 'entity://demo-electronics/lighting/ADMIN-3F', severity: 3, summary: '照明与空压同时异常，未匹配规则，进入慢通道', nature: 'synthetic' },
    { event_id: 'event://demo-evening-peak', at: utc(19, 30), event_type: 'transformer_overload', source: 'sim_limit', entity_id: 'entity://demo-electronics/transformer/T1', severity: 3, summary: '晚高峰 T1 负载 91%，储能放电削峰', nature: 'synthetic' },
    { event_id: 'event://demo-valley-charge', at: utc(21, 0), event_type: 'valley_charge', source: 'script', entity_id: 'entity://demo-electronics/battery/BESS1', severity: 1, summary: '谷段开始，储能转入充电', nature: 'synthetic' },
    // 极端场景「传感器读数异常」：读数跳变，但电流、振动与相邻测点都不支持真实过热。
    // 载荷字段只作演示：真实导出要等 B36 带出读数质量与残差，页面没有这些字段时显示空态。
    {
      event_id: 'event://demo-sensor-lie', at: utc(15, 10), event_type: 'equipment_temperature', source: 'sim_limit',
      entity_id: 'entity://demo-electronics/compressor/AIR-1', severity: 4,
      summary: '空压机排气温度 1 分钟内从 82 ℃ 跳升至 121 ℃，电流与振动未同步变化，疑似传感器读数异常',
      nature: 'synthetic',
      payload: { sensor: 'AIR-1 · 排气温度', reading: 121, baseline: 82, residual_c: 38.6, residual_limit_c: 8, quality: 0.42, candidates: ['真实过热', '信号干扰', '探头读数漂移'] },
    },
    {
      event_id: 'event://demo-sensor-verified', at: utc(15, 18), event_type: 'equipment_sensor_check', source: 'script',
      entity_id: 'entity://demo-electronics/compressor/AIR-1', severity: 2,
      summary: '设备监测专家对比电流、振动与相邻测点，判定温度探头读数漂移，不是真实过热',
      nature: 'synthetic',
      payload: {
        sensor: 'AIR-1 · 排气温度', conclusion: 'sensor_lie',
        verified_by: ['相邻测点 82.3 ℃ 稳定', '主机电流 214 A 平稳', '振动 1.2 mm/s 低于阈值'],
        excluded: ['真实过热', '信号干扰'],
        resolution: '按相邻测点控温，派探头校准工单，不触发停机',
      },
    },
  ]

  const tickets: Ticket[] = [
    { ticket_id: 'ticket://tk-0003', opened_at: utc(10, 30), closed_at: utc(10, 45), status: 'closed', rehearsal: false, round: 1, event_ids: ['event://demo-air-leak', 'event://demo-light-fault'], experts: ['equipment', 'energy'], summary: '空压泄漏与照明故障并行处置（合成演示）' },
    { ticket_id: 'ticket://tk-0004', opened_at: utc(14, 20), status: 'blocked', rehearsal: false, round: 1, event_ids: ['event://demo-elev-fault'], experts: ['elevator', 'hvac'], summary: '货梯困人涉及安全，不自动处理，等待值班确认救援方案（合成演示）' },
    { ticket_id: 'ticket://tk-0005', opened_at: utc(16, 5), closed_at: utc(16, 40), status: 'closed', rehearsal: false, round: 1, event_ids: ['event://demo-unknown'], experts: ['equipment', 'energy'], summary: '照明与空压同时异常，慢通道调查后纠正并沉淀规则（合成演示）' },
    { ticket_id: 'ticket://tk-0006', opened_at: utc(19, 30), closed_at: utc(19, 55), status: 'closed', rehearsal: false, round: 1, event_ids: ['event://demo-evening-peak'], experts: ['energy'], summary: '晚高峰储能放电削峰（合成演示）' },
    { ticket_id: 'ticket://tk-0007', opened_at: utc(15, 10), closed_at: utc(15, 35), status: 'closed', rehearsal: false, round: 1, event_ids: ['event://demo-sensor-lie', 'event://demo-sensor-verified'], experts: ['equipment'], summary: '排气温度跳变，设备监测判为传感器读数异常（合成演示）' },
  ]

  const binding = (knowledgeVersion: number, ticketId: string, triggerAt: string) => ({
    run_id: data.run_id, knowledge_version: knowledgeVersion, ticket_id: ticketId,
    round: 1, state_version: 5, trigger_at: triggerAt,
  })

  const proposals: Proposal[] = [
    {
      proposal_id: 'proposal://pp-0003', expert: 'equipment', ticket_id: 'ticket://tk-0003',
      actions: [{ entity_id: 'entity://demo-electronics/compressor/AIR-1', command: 'open_work_order', params: { kind: 'leak_repair' }, interval: { start: utc(10, 35), end: utc(12, 0) }, readiness: 'needs_human' }],
      cost: { cny: 0, wait_min: 0, comfort_min: 0 }, confidence: 0.8,
      diagnosis: '压力下降速率超阈值且电流同步上升，判为泄漏；建议停机检修',
      evidence_refs: ['rule://synthetic-leak#pressure-drop', 'document://maintenance-manual'], safety_tags: [],
      binding: binding(2, 'ticket://tk-0003', utc(10, 30)),
    },
    {
      proposal_id: 'proposal://pp-0004', expert: 'energy', ticket_id: 'ticket://tk-0003',
      actions: [{ entity_id: 'entity://demo-electronics/lighting/ADMIN-3F', command: 'switch_circuit', params: { circuit: 'B' }, interval: { start: utc(10, 32), end: utc(11, 0) }, readiness: 'ready' }],
      cost: { cny: -2.4, wait_min: 0, comfort_min: 0 }, confidence: 0.86,
      diagnosis: 'B 相回路过流跳闸，切换备用回路',
      evidence_refs: ['entity://demo-electronics/lighting/ADMIN-3F'], safety_tags: [],
      binding: binding(2, 'ticket://tk-0003', utc(10, 30)),
    },
    {
      proposal_id: 'proposal://pp-0005', expert: 'energy', ticket_id: 'ticket://tk-0006',
      actions: [{ entity_id: 'entity://demo-electronics/battery/BESS1', command: 'discharge', params: { kw: 320 }, interval: { start: utc(19, 32), end: utc(21, 0) }, readiness: 'ready' }],
      cost: { cny: -310, wait_min: 0, comfort_min: 0 }, confidence: 0.88,
      diagnosis: '晚高峰负载 91%，储能仍有 54% SOC，放电削峰',
      evidence_refs: ['entity://demo-electronics/transformer/T1'], safety_tags: [],
      binding: binding(3, 'ticket://tk-0006', utc(19, 30)),
    },
    // 早高峰这一幕的另外两条动作：样例只给了储能，电梯分区与暖通推迟预冷在这里补成合成数据。
    {
      proposal_id: 'proposal://pp-demo-elev', expert: 'elevator', ticket_id: 'ticket://tk-0001',
      actions: [{ entity_id: 'entity://demo-electronics/elevator_group/EG-ADMIN', command: 'zone_operation', params: { zones: 2 }, interval: { start: utc(8, 6), end: utc(8, 36) }, readiness: 'ready' }],
      cost: { cny: -6, wait_min: 12, comfort_min: 0 }, confidence: 0.79,
      diagnosis: '行政楼人梯早高峰拥堵，分区运行把启动功率压下来，候梯多 12 分钟',
      evidence_refs: ['rule://synthetic-elevator#partition', 'entity://demo-electronics/transformer/T1'], safety_tags: [],
      binding: binding(1, 'ticket://tk-0001', utc(8, 5)),
    },
    {
      proposal_id: 'proposal://pp-demo-hvac', expert: 'hvac', ticket_id: 'ticket://tk-0001',
      actions: [{ entity_id: 'entity://demo-electronics/hvac_zone/FACTORY-AHU', command: 'defer_precool', params: { minutes: 15 }, interval: { start: utc(8, 6), end: utc(8, 21) }, readiness: 'ready' }],
      cost: { cny: -8.5, wait_min: 0, comfort_min: 4 }, confidence: 0.85,
      diagnosis: '把预冷推后 15 分钟，避开与电梯、空压启动的叠加，车间送风仍压在设定值附近',
      evidence_refs: ['rule://synthetic-hvac#precool', 'document://synthetic-hvac-manual'], safety_tags: [],
      binding: binding(1, 'ticket://tk-0001', utc(8, 5)),
    },
    // 传感器读数异常这一幕：专家先列候选假设，再用电流、振动、相邻测点逐个排除。
    {
      proposal_id: 'proposal://pp-demo-sensor', expert: 'equipment', ticket_id: 'ticket://tk-0007',
      actions: [{ entity_id: 'entity://demo-electronics/compressor/AIR-1', command: 'calibrate_sensor', params: { sensor: 'exhaust_temp_c', fallback: '相邻测点' }, interval: { start: utc(15, 16), end: utc(15, 40) }, readiness: 'needs_human' }],
      cost: { cny: 0, wait_min: 0, comfort_min: 0 }, confidence: 0.62,
      diagnosis: '排气温度读数 1 分钟内从 82 ℃ 跳升至 121 ℃，电流、振动与相邻测点均保持稳定，判定为探头读数漂移（非真实过热）；建议切换相邻测点控温并派发校准工单',
      evidence_refs: ['entity://demo-electronics/compressor/AIR-1', 'document://maintenance-manual'], safety_tags: [],
      binding: binding(3, 'ticket://tk-0007', utc(15, 10)),
      timing: { origin_event_at: utc(15, 10), trigger_at: utc(15, 10), effective_at: utc(15, 14), requested_at: utc(15, 10, 5), returned_at: utc(15, 14, 20), latency_ms: 255000, retries: 1 },
    },
  ]

  const decisions: Decision[] = [
    {
      decision_id: 'decision://dc-0002', ticket_id: 'ticket://tk-0003', round: 1, layer: 1, at: utc(10, 31),
      conflict: { summary: '设备监测要停机维修；能源专家切换照明回路。对象不同，可并行', proposal_ids: ['proposal://pp-0003', 'proposal://pp-0004'] },
      conversion: { policy_version: 'policy-1', scores: { 'pp-0004': -2.4 }, infeasible: [], notes: ['快通道：泄漏规则已命中，无需仲裁'] },
      result: { selected_proposal_ids: ['proposal://pp-0003', 'proposal://pp-0004'], outcome: 'executed', summary: '设备监测开维修工单；能源专家切换照明备用回路，两条泳道并行处理，无冲突。' },
    },
    {
      decision_id: 'decision://dc-0003', ticket_id: 'ticket://tk-0002', round: 1, layer: 1, at: utc(13, 40),
      conflict: { summary: '参观期间行政楼保持 24 ℃，峰段前预冷会多花电费', proposal_ids: [] },
      conversion: { policy_version: 'policy-1', scores: {}, infeasible: [], notes: ['预计多耗电费 46.5 元'] },
      result: { selected_proposal_ids: [], outcome: 'to_human', summary: '临时规则 13:45–17:00 生效；峰段前预冷代价需值班确认。' },
    },
    {
      decision_id: 'decision://dc-0004', ticket_id: 'ticket://tk-0004', round: 1, layer: 3, at: utc(14, 21),
      conflict: { summary: '货梯困人涉及安全，Agent 不自动处理', proposal_ids: [] },
      conversion: { policy_version: 'policy-1', scores: {}, infeasible: [], notes: ['安全否决：转操作工单'] },
      result: { selected_proposal_ids: [], outcome: 'work_order', summary: '已派操作工单；其他货梯接管生产任务，暖通给井道通风。' },
    },
    {
      decision_id: 'decision://dc-0005', ticket_id: 'ticket://tk-0005', round: 1, layer: 3, at: utc(16, 25),
      conflict: { summary: '照明与空压同时异常，没有匹配规则，进入慢通道', proposal_ids: [] },
      conversion: { policy_version: 'policy-1', scores: {}, infeasible: [], notes: ['慢通道：多跳追溯 配电回路 → 照明 → 空压'] },
      result: { selected_proposal_ids: [], outcome: 'executed', summary: '慢通道调查后纠正：先查配电回路；沉淀为新规则并回测通过。' },
    },
    {
      decision_id: 'decision://dc-0007', ticket_id: 'ticket://tk-0007', round: 1, layer: 2, at: utc(15, 14),
      conflict: { summary: '温度读数越限，但电流、振动与相邻测点都不支持真实过热', proposal_ids: ['proposal://pp-demo-sensor'] },
      conversion: { policy_version: 'policy-1', scores: { 'pp-demo-sensor': 0 }, infeasible: [], notes: ['读数质量 0.42 低于 0.6，先按相邻测点控温'] },
      result: { selected_proposal_ids: ['proposal://pp-demo-sensor'], outcome: 'executed', summary: '判定为传感器读数异常（探头漂移）：切换相邻测点控温，派发探头校准工单，避免误停机。' },
    },
    {
      decision_id: 'decision://dc-0006', ticket_id: 'ticket://tk-0006', round: 1, layer: 2, at: utc(19, 31),
      conflict: { summary: '晚高峰 T1 负载 91%，储能 SOC 仍有余量', proposal_ids: ['proposal://pp-0005'] },
      conversion: { policy_version: 'policy-1', scores: { 'pp-0005': -310 }, infeasible: [], notes: ['储能放电优先'] },
      result: { selected_proposal_ids: ['proposal://pp-0005'], outcome: 'executed', summary: '储能放电 320 kW 削峰，预计省 310 元。' },
    },
    {
      decision_id: 'decision://dc-demo-0806', ticket_id: 'ticket://tk-0001', round: 2, layer: 2, at: utc(8, 6, 10),
      conflict: { summary: '电梯、暖通与储能三方竞争 T1 变压器余量', proposal_ids: ['proposal://pp-0001', 'proposal://pp-demo-elev', 'proposal://pp-demo-hvac'] },
      conversion: { policy_version: 'policy-1', scores: { 'pp-0001': -38.2, 'pp-demo-elev': -6, 'pp-demo-hvac': -8.5 }, infeasible: [], notes: ['三方代价按策略权重（policy-1）折算为统一等效成本', '现场重算结果与 07:50 预演一致'] },
      result: {
        selected_proposal_ids: ['proposal://pp-0001', 'proposal://pp-demo-elev', 'proposal://pp-demo-hvac'], outcome: 'executed',
        summary: '三方合并：储能放电 390 kW、电梯分区运行、暖通推迟预冷 15 分钟，目标是把 T1 负载压回安全线内。',
      },
    },
    {
      decision_id: 'decision://dc-demo-0807', ticket_id: 'ticket://tk-0001', round: 3, layer: 3, at: utc(8, 7),
      conflict: { summary: '分区运行预案只有 30 分钟，物流高峰要延长到 09:00', proposal_ids: [] },
      conversion: { policy_version: 'policy-1', scores: {}, infeasible: [], notes: ['预案时长上限 30 分钟（policy-1）'] },
      result: { selected_proposal_ids: [], outcome: 'to_human', summary: '分区运行延长至 09:00 超出预案时长，需值班批准；超时未处理将按预案于 08:36 自动恢复全区运行。' },
    },
  ]

  const workOrders: WorkOrder[] = [
    {
      work_order_id: 'wo-0002', ticket_id: 'ticket://tk-0004', created_at: utc(14, 21),
      reason: '货梯困人涉及人身安全，需值班确认救援方案',
      actions: [{ entity_id: 'entity://demo-electronics/elevator_group/EG-PROD', command: 'rescue', params: { car: 2 }, interval: { start: utc(14, 25), end: utc(15, 0) }, readiness: 'needs_human' }],
      status: 'proposed', pending_action_id: 'act-synthetic-safety',
    },
    {
      work_order_id: 'wo-demo-t1', ticket_id: 'ticket://tk-0001', created_at: utc(8, 7),
      reason: '巡检 T1 变压器温升，已派值班电工，待确认到场',
      actions: [{ entity_id: 'entity://demo-electronics/transformer/T1', command: 'inspect', params: { item: '温升' }, interval: { start: utc(8, 10), end: utc(9, 0) }, readiness: 'needs_human' }],
      status: 'proposed', pending_action_id: 'act-demo-t1',
    },
    {
      work_order_id: 'wo-demo-air', ticket_id: 'ticket://tk-0003', created_at: utc(10, 35),
      reason: '空压机判定为管网泄漏，需停机检修',
      actions: [{ entity_id: 'entity://demo-electronics/compressor/AIR-1', command: 'open_work_order', params: { kind: 'leak_repair' }, interval: { start: utc(10, 40), end: utc(12, 0) }, readiness: 'needs_human' }],
      status: 'proposed', pending_action_id: 'act-demo-air',
    },
    {
      work_order_id: 'wo-demo-sensor', ticket_id: 'ticket://tk-0007', created_at: utc(15, 14),
      reason: '排气温度探头读数漂移，切换相邻测点控温并派发校准工单',
      actions: [{ entity_id: 'entity://demo-electronics/compressor/AIR-1', command: 'calibrate_sensor', params: { sensor: 'exhaust_temp_c' }, interval: { start: utc(15, 16), end: utc(15, 40) }, readiness: 'needs_human' }],
      status: 'proposed', pending_action_id: 'act-demo-sensor',
    },
  ]

  const knowledgeVersions: KnowledgeVersion[] = [
    { version: 1, version_manifest_id: 'vm-0003', activated_at: '2026-06-30T10:00:00Z', summary: '初始知识：多领域设备手册、维保规程与分时电价政策' },
    { version: 2, version_manifest_id: 'vm-synthetic-leak', activated_at: utc(9, 35), summary: '新到《空压机运行维护手册》：泄漏判别阈值入库' },
    { version: 3, version_manifest_id: 'vm-0004', activated_at: utc(14, 0), summary: '新电价政策：峰段提前到 13:00' },
    { version: 4, version_manifest_id: 'vm-synthetic-correction', activated_at: utc(16, 20), summary: '纠错沉淀：照明与空压同时异常判为慢通道，新增排查 Skill' },
  ]

  // 主 Agent、暖通专家与 Watchdog 的运行记录：样例只有能源、电梯两位专家，这里补上，
  // 让运行席的泳道有每次工具调用的开始时间与耗时（started_at / duration_ms）。
  // contracts 的 AgentRunRecord 没有 main / watchdog 两个 kind：主 Agent 记为 triage（研判），Watchdog 记为 follow_up（核对回执）。
  const base = Date.parse(utc(8, 5))
  const at = (ms: number) => new Date(base + ms).toISOString().replace('.000Z', 'Z')
  const agentRuns: AgentRun[] = [
    {
      agent_run_id: 'ar-demo-main', kind: 'triage', agent_name: 'plantkg_main', run_mode: 'live', source: 'nexent',
      binding: binding(1, 'ticket://tk-0001', utc(8, 5)),
      timing: { origin_event_at: utc(8, 5), trigger_at: utc(8, 5), effective_at: utc(8, 6), requested_at: at(0), returned_at: at(23600), latency_ms: 23600, retries: 0 },
      status: 'finished',
      proposal_ids: ['proposal://pp-demo-elev', 'proposal://pp-demo-hvac'],
      tool_calls: [
        { call_id: 'call-demo-main-1', agent_name: 'plantkg_main', tool: '接收事件', started_at: at(0), duration_ms: 120, business_status: 'done', summary: '两条告警合并成 1 号事件单' },
        { call_id: 'call-demo-main-2', agent_name: 'plantkg_main', tool: '分派专家', started_at: at(800), duration_ms: 200, business_status: 'done', summary: '按图谱打分派发能源、电梯、暖通三位专家' },
        { call_id: 'call-demo-main-3', agent_name: 'plantkg_main', tool: 'resolve_conflict', started_at: at(16000), duration_ms: 3000, business_status: 'done', summary: '三位专家各提一个方案，都要占用 T1 余量' },
        { call_id: 'call-demo-main-4', agent_name: 'plantkg_main', tool: '下发动作', started_at: at(19500), duration_ms: 3000, business_status: 'done', summary: '3 条控制指令已回执' },
      ],
    },
    {
      agent_run_id: 'ar-demo-hvac', kind: 'expert', agent_name: 'hvac_expert', run_mode: 'live', source: 'nexent',
      binding: binding(1, 'ticket://tk-0001', utc(8, 5)),
      timing: { origin_event_at: utc(8, 5), trigger_at: utc(8, 5), effective_at: utc(8, 6), requested_at: at(1000), returned_at: at(8000), latency_ms: 8000, retries: 0 },
      status: 'finished',
      proposal_ids: ['proposal://pp-demo-hvac'],
      tool_calls: [
        { call_id: 'call-demo-hvac-1', agent_name: 'hvac_expert', tool: '查预冷计划', started_at: at(1800), duration_ms: 4500, business_status: 'done', summary: '预冷计划与峰段重叠 12 分钟' },
        { call_id: 'call-demo-hvac-2', agent_name: 'hvac_expert', tool: '推迟预冷方案', started_at: at(9600), duration_ms: 3400, business_status: 'done', summary: '推迟 15 分钟，送风仍压在设定值附近' },
      ],
    },
    {
      agent_run_id: 'ar-demo-watchdog', kind: 'follow_up', agent_name: 'watchdog', run_mode: 'live', source: 'nexent',
      binding: binding(1, 'ticket://tk-0001', utc(8, 5)),
      timing: { origin_event_at: utc(8, 5), trigger_at: utc(8, 5), effective_at: utc(8, 5), requested_at: at(0), returned_at: null, latency_ms: null, retries: 0 },
      status: 'finished',
      tool_calls: [
        { call_id: 'call-demo-watchdog-1', agent_name: 'watchdog', tool: '核对回执', started_at: at(22000), duration_ms: 3600, business_status: 'running', summary: '核对 3 条指令回执与预演是否一致' },
      ],
    },
    // 传感器读数异常的试探过程：读时序 → 交叉核对 → 残差检验 → 相邻测点 → 派工单。
    {
      agent_run_id: 'ar-demo-sensor', kind: 'expert', agent_name: 'equipment_expert', run_mode: 'live', source: 'nexent',
      binding: binding(3, 'ticket://tk-0007', utc(15, 10)),
      timing: { origin_event_at: utc(15, 10), trigger_at: utc(15, 10), effective_at: utc(15, 14), requested_at: utc(15, 10, 5), returned_at: utc(15, 14, 20), latency_ms: 255000, retries: 1 },
      status: 'finished',
      proposal_ids: ['proposal://pp-demo-sensor'],
      final_answer: '温度探头读数漂移，不是真实过热；按相邻测点控温并派校准工单。',
      tool_calls: [
        { call_id: 'call-demo-sensor-1', agent_name: 'equipment_expert', tool: '读排气温度时序', started_at: utc(15, 10, 8), duration_ms: 1200, business_status: 'done', summary: '82 ℃ 在 1 分钟内跳到 121 ℃，没有正常爬升过程' },
        { call_id: 'call-demo-sensor-2', agent_name: 'equipment_expert', tool: '交叉核对电流与振动', started_at: utc(15, 10, 40), duration_ms: 2600, business_status: 'done', summary: '电流 214 A、振动 1.2 mm/s 都在基线内，与过热不符' },
        { call_id: 'call-demo-sensor-3', agent_name: 'equipment_expert', tool: '残差检验', started_at: utc(15, 11, 30), duration_ms: 3100, business_status: 'degraded', summary: '残差 38.6 ℃ 远超 8 ℃ 限值，读数质量 0.42，判为可疑读数' },
        { call_id: 'call-demo-sensor-4', agent_name: 'equipment_expert', tool: '相邻测点比对', started_at: utc(15, 12, 10), duration_ms: 4200, business_status: 'done', summary: '相邻测点 82.3 ℃ 稳定，排除真实过热与信号干扰' },
        { call_id: 'call-demo-sensor-5', agent_name: 'equipment_expert', tool: '派探头校准工单', started_at: utc(15, 13, 40), duration_ms: 1800, business_status: 'done', summary: '按相邻测点控温，不触发停机，等值班确认校准' },
      ],
    },
  ]

  // 运维问答：样例只有 09:00 一条，这里补到每一幕，主 Agent 页在任何回放时刻都有内容。
  // 这些回答属于内置合成样例，页面会标明来源，不冒充真实 Nexent 运行记录。
  const qaEntries: QaEntry[] = [
    {
      qa_id: 'qa-demo-0806', at: utc(8, 6, 40), question: '为什么电梯不全开，要切分区？',
      answer: '全台同时启动会将 T1 推过容量上限。群控规程允许早高峰按低区、高区分组运行；候梯增加 12 分钟的综合加权代价显著低于变压器越限风险。',
      agent_run_id: 'ar-demo-main', evidence_refs: ['decision://dc-demo-0806', 'rule://synthetic-elevator#partition'],
    },
    {
      qa_id: 'qa-demo-0806b', at: utc(8, 6, 50), question: '暖通推迟 15 分钟有什么代价？',
      answer: '车间送风仍压在 18.2 ℃ 附近，付出 4 分钟舒适度余量，换掉的是与电梯、空压启动叠加的那段峰值。',
      agent_run_id: 'ar-demo-hvac', evidence_refs: ['rule://synthetic-hvac#precool'],
    },
    {
      qa_id: 'qa-demo-0806c', at: utc(8, 6, 55), question: '空压机没告警，为什么也要限启动？',
      answer: '压力还在 0.62 MPa 的安全线内，但再叠 60 kW 启动电流会把 T1 推回越限；等电梯首轮过去再放开，压力影响不到 0.01 MPa。',
      agent_run_id: 'ar-demo-main', evidence_refs: ['decision://dc-demo-0806', 'rule://synthetic-compressed-air#stagger'],
    },
    {
      qa_id: 'qa-demo-0807', at: utc(8, 7, 10), question: '储能为什么现在不放电，要等电价峰值？',
      answer: '现在放会与 10:30 光伏出力一起把 SOC 打到下限；保持 80% 到 14:00 峰段再放，两段收益不在同一时间兑现。',
      agent_run_id: 'ar-demo-main', evidence_refs: ['decision://dc-demo-0806', 'rule://tou-2026#peak'],
    },
    {
      qa_id: 'qa-demo-1032', at: utc(10, 32), question: '储能放到下限安全吗？',
      answer: '放到 SOC 20% 的下限就停，12:00 光伏高发时再充回；账本里 09:30 到 12:00 是充电窗口。',
      evidence_refs: ['entity://demo-electronics/battery/BESS1', 'rule://tou-2026#peak'],
    },
    {
      qa_id: 'qa-demo-1426', at: utc(14, 26), question: '货梯困人为什么不自动处理？',
      answer: '涉及人员安全的操作一律转人工：系统只派操作工单并给井道通风，救援动作等值班确认后再执行。',
      evidence_refs: ['decision://dc-0004'],
    },
    {
      qa_id: 'qa-demo-1630', at: utc(16, 30), question: '这次和预演有什么不同？',
      answer: '早高峰现场重算后与 07:50 的预演一致，所以走了快通道；慢通道那条多花 25 分钟定位到配电回路。',
      evidence_refs: ['decision://dc-0005', 'decision://dc-demo-0806'],
    },
  ]

  const stateSeries: StateFrame[] = []
  for (let hour = 0; hour <= 24; hour++) {
    const soc = hour === 8 ? 0.52 : hour === 9 ? 0.43 : hour === 12 ? 0.5 : hour === 13 ? 0.58 : hour === 19 ? 0.54 : hour === 20 ? 0.44 : hour === 21 ? 0.36 : hour === 24 ? 0.5 : 0.62
    const mode = hour === 8 || hour === 19 ? 'discharge' : hour === 12 ? 'charge' : 'idle'
    const queue = hour === 8 ? 42 : hour === 9 ? 18 : hour >= 7 && hour <= 21 ? 6 : 1
    const busy = hour >= 7 && hour <= 21
    stateSeries.push({
      at: utc(hour, 0),
      devices: {
        'entity://demo-electronics/battery/BESS1': { soc, mode, temperature_degC: mode === 'discharge' ? 31.4 : mode === 'charge' ? 29.8 : 27.2, current_a: mode === 'idle' ? 0 : mode === 'charge' ? -168 : 292 },
        'entity://demo-electronics/elevator_group/EG-PROD': {
          running: busy ? 4 : 1, queue, fault: hour === 14 ? 'car-2-between-floors' : null,
          vibration_mm_s: hour === 14 ? 4.6 : 1.8, temperature_degC: hour === 14 ? 46.2 : 38.5,
        },
        'entity://demo-electronics/elevator_group/EG-ADMIN': {
          running: 5, queue: hour === 8 ? 42 : busy ? 9 : 1, zone_operation: hour === 8, fault: null,
          vibration_mm_s: 1.6, temperature_degC: 36.8,
        },
        'entity://demo-electronics/hvac_zone/ADMIN-3F': { temperature_degC: hour >= 13 && hour <= 17 ? 24.1 : 26.0, humidity: 0.57, current_a: 12.4, running: true },
        'entity://demo-electronics/hvac_zone/FACTORY-AHU': {
          supply_temp_degC: hour === 8 ? 18.5 : hour === 10 ? 19.2 : 18.2, temperature_degC: 25.4,
          running: true, current_a: hour === 10 ? 21.6 : 17.8, vibration_mm_s: 2.1,
        },
        'entity://demo-electronics/transformer/T1': {
          load_ratio: hour === 8 ? 0.96 : hour === 19 ? 0.91 : 0.72,
          temperature_degC: hour === 8 ? 78.4 : hour === 19 ? 74.1 : 61.2, current_a: hour === 8 ? 892 : 640,
        },
        'entity://demo-electronics/compressor/AIR-1': {
          pressure_mpa: hour === 10 ? 0.58 : 0.72, current_a: hour === 10 ? 26 : 18, running: true,
          vibration_mm_s: hour === 10 ? 3.4 : hour === 11 ? 3.9 : 1.9, temperature_degC: hour === 10 ? 68.5 : 54.2,
        },
        'entity://demo-electronics/pv/PV-1': { output_kw: hour >= 7 && hour <= 18 ? Math.round(120 * Math.sin(((hour - 6) / 12) * Math.PI)) : 0, temperature_degC: 32 + Math.max(0, 18 - Math.abs(13 - hour)) * 1.4, running: hour >= 7 && hour <= 18 },
        'entity://demo-electronics/lighting/ADMIN-3F': { running: true, power_kw: hour === 10 ? 0 : 6.4, circuit_a: hour === 10 ? 'tripped' : 'running', circuit_b: hour === 10 ? 'running' : 'standby' },
        'entity://demo-electronics/lighting/FACTORY-1F': { running: true, power_kw: 18.6, circuit_a: 'running', circuit_b: 'standby' },
      },
      // 08:06 三方合并下发后，T1 负载在 08:10 回落到 90%（simulated：按储能放电 390 kW 与错峰估算）。
      // 动作生效后的读数用样例自带的 08:15 帧（81%），这里不再重复造点，免得和左栏读数打架。
      changes: hour === 8
        ? [[10, 0.9]].map(([minute, ratio]) => ({
          at: utc(8, minute), entity_id: 'entity://demo-electronics/transformer/T1', field: 'load_ratio', value: ratio,
        }))
        : [],
    })
  }

  // 全天账本分钟（kWh/分钟，光伏 kW ÷ 60，与设备 output_kw 同量级）：负荷按三段曲线起伏，电价按谷/平/峰，光伏与储能按上面的状态帧走。合成演示。
  // contracts 的 LedgerEntry 要求储能电量，展示页的 LedgerMinute 还没补这两个字段，这里用交叉类型带上，导出时不能丢。
  type DemoLedgerMinute = Day['ledger_minutes'][number] & { energy_start_kwh: number; energy_end_kwh: number }
  const ledger: DemoLedgerMinute[] = []
  const priceAt = (hour: number) => (hour >= 8 && hour < 11) || (hour >= 17 && hour < 21) ? 0.95 : hour >= 11 && hour < 17 ? 0.72 : 0.36
  let energyKwh = 620
  for (let hour = 0; hour < 24; hour++) {
    for (let minute = 0; minute < 60; minute++) {
      const morning = 380 * Math.exp(-((hour + minute / 60 - 8.1) ** 2) / 0.9)
      const evening = 330 * Math.exp(-((hour + minute / 60 - 19.6) ** 2) / 1.6)
      const base = 450 + 180 * Math.sin(((hour + minute / 60) / 24) * Math.PI)
      const load = Math.round((base + morning + evening) / 60 * 100) / 100
      const pv = Math.max(0, Math.round(120 * Math.sin(((hour + minute / 60 - 6) / 12) * Math.PI)) / 60)
      const discharging = (hour === 8 && minute >= 6) || (hour === 19 && minute >= 32)
      const charging = hour === 12
      const discharge = discharging ? Math.min(6.5, load) : 0
      const charge = charging ? 4 : 0
      const grid = Math.max(0, load - pv - discharge) + charge
      const start = utc(hour, minute)
      const end = hour === 23 && minute === 59 ? utc(24, 0) : utc(hour, minute + 1)
      const socStart = 0.62 - Math.min(0.42, Math.max(0, (hour + minute / 60 - 8) * 0.02))
      // 按充放效率递推累计电量，下一分钟的起点就是这一分钟的终点；clamp 到 [0, 1000] 保证非负。
      const energyStart = energyKwh
      const energyEnd = Math.min(1000, Math.max(0, energyStart + charge * 0.95 - discharge / 0.95))
      energyKwh = energyEnd
      const pvUsed = Math.min(pv, load)
      // 购电按 2 位小数入账，残差用入账后的数值重算，才能过契约的 1e-6 一致性校验。
      const gridImport = Math.round(grid * 100) / 100
      // 负荷分项按比例拆；process 取剩余量、base 为 0，合计精确等于 load_kwh。
      const elevator = load * 0.12
      const hvac = load * 0.28
      const airCompressor = load * 0.2
      const lighting = load * 0.05
      const processLoad = load - elevator - hvac - airCompressor - lighting
      ledger.push({
        interval: { start, end },
        pv_available_kwh: pv,
        pv_used_kwh: pvUsed,
        pv_curtailed_kwh: Math.max(0, Math.round((pv - load) * 100) / 100),
        grid_import_kwh: gridImport,
        grid_export_kwh: 0,
        charge_kwh: charge,
        discharge_kwh: discharge,
        load_kwh: load,
        energy_start_kwh: energyStart,
        energy_end_kwh: energyEnd,
        soc_end: Math.round(Math.max(0.2, Math.min(1, socStart + charge * 0.01 - discharge * 0.012)) * 1000) / 1000,
        price_cny_per_kwh: priceAt(hour),
        cost_cny: Math.round(grid * priceAt(hour) * 100) / 100,
        residual_kwh: pvUsed + gridImport + discharge - load - charge,
        load_breakdown: {
          elevator, hvac, air_compressor: airCompressor, lighting, process: processLoad, base: 0,
        },
      })
    }
  }

  data.events.push(...events)
  data.tickets.push(...tickets)
  data.proposals.push(...proposals)
  data.decisions.push(...decisions)
  data.work_orders.push(...workOrders)
  data.temp_rules.push({
    rule_id: 'temp://tr-demo-1330', text: '下午参观：行政楼全部楼层保持 24 ℃',
    effective: { start: utc(13, 0), end: utc(17, 0) },
    entity_ids: ['entity://demo-electronics/hvac_zone/ADMIN-3F'],
    created_at: utc(12, 30), source_event_id: null, estimated_cost_cny: 46.5,
  })
  data.temp_rules.push({
    rule_id: 'temp://tr-demo-soc', text: '储能放电下限临时放到 20%',
    effective: { start: utc(8, 6), end: utc(9, 30) },
    entity_ids: ['entity://demo-electronics/battery/BESS1'],
    created_at: utc(8, 6), source_event_id: null, estimated_cost_cny: null,
  })
  data.knowledge_versions = knowledgeVersions
  data.agent_runs.push(...agentRuns)
  data.qa.push(...qaEntries)
  // 样例里已有的状态帧与账本分钟是真数据，保留；同一时刻的合成帧只补它没有的设备。
  const frames = new Map(stateSeries.map(frame => [frame.at, frame]))
  const merged = new Map(frames)
  for (const frame of data.state_series) {
    const synthetic = frames.get(frame.at)
    merged.set(frame.at, synthetic
      ? { at: frame.at, devices: { ...synthetic.devices, ...frame.devices }, changes: [...(synthetic.changes ?? []), ...(frame.changes ?? [])] }
      : frame)
  }
  data.state_series = [...merged.values()].sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
  const seenMinutes = new Set(data.ledger_minutes.map(minute => minute.interval.start))
  data.ledger_minutes = [...data.ledger_minutes, ...ledger.filter(minute => !seenMinutes.has(minute.interval.start))]
    .sort((a, b) => Date.parse(a.interval.start) - Date.parse(b.interval.start))
  data.metrics.push(
    { metric: 'energy_kwh', group: 'baseline', value: 23180, unit: 'kWh', note: '仿真演示场景' },
    { metric: 'energy_kwh', group: 'independent', value: 22430, unit: 'kWh', note: '仿真演示场景' },
    { metric: 'energy_kwh', group: 'coordinated', value: 21460, unit: 'kWh', note: '仿真演示场景' },
    { metric: 'transformer_peak_pct', group: 'baseline', value: 112, unit: '%', note: '仿真演示场景' },
    { metric: 'transformer_peak_pct', group: 'independent', value: 104, unit: '%', note: '仿真演示场景' },
    { metric: 'transformer_peak_pct', group: 'coordinated', value: 96, unit: '%', note: '仿真演示场景' },
    { metric: 'avg_wait_min', group: 'baseline', value: 6.4, unit: 'min', note: '仿真演示场景' },
    { metric: 'avg_wait_min', group: 'independent', value: 5.0, unit: 'min', note: '仿真演示场景' },
    { metric: 'avg_wait_min', group: 'coordinated', value: 4.2, unit: 'min', note: '仿真演示场景' },
    { metric: 'comfort_violation_min', group: 'baseline', value: 210, unit: 'min', note: '仿真演示场景' },
    { metric: 'comfort_violation_min', group: 'independent', value: 126, unit: 'min', note: '仿真演示场景' },
    { metric: 'comfort_violation_min', group: 'coordinated', value: 71, unit: 'min', note: '仿真演示场景' },
    { metric: 'manual_interventions', group: 'baseline', value: 18, unit: '次', note: '仿真演示场景' },
    { metric: 'manual_interventions', group: 'independent', value: 13, unit: '次', note: '仿真演示场景' },
    { metric: 'manual_interventions', group: 'coordinated', value: 4, unit: '次', note: '仿真演示场景' },
    // 知识侧、编排对照、Harness、越学越省：B33/B36 尚未产出，这里只放标了「合成演示」的样例，
    // 页面按指标名读取，真实 day.json 有就显示、没有就空态，不把样例当实测。
    { metric: 'asset_activation_rate', group: 'knowledge', value: 0.72, unit: 'ratio', note: '标准演示场景' },
    { metric: 'standard_alignment_rate', group: 'knowledge', value: 0.78, unit: 'ratio', note: '标准演示场景' },
    { metric: 'ontology_rule_f1', group: 'knowledge', value: 0.83, unit: 'ratio', note: '标准演示场景' },
    { metric: 'build_efficiency_min_per_1k', group: 'knowledge', value: 42, unit: 'min/千条', note: '标准演示场景' },
    { metric: 'doc_qa_accuracy', group: 'knowledge', value: 0.86, unit: 'ratio', note: '合成演示，待实测数据替换' },
    { metric: 'doc_qa_accuracy_rag', group: 'knowledge', value: 0.71, unit: 'ratio', note: '合成演示：普通 RAG 对照' },
    { metric: 'orchestration_cases', group: 'coordinated', value: 16, unit: '起', note: '合成演示：未见事件样本' },
    { metric: 'orchestration_accuracy', group: 'baseline', value: 0.81, unit: 'ratio', note: '合成演示：全程序派发' },
    { metric: 'orchestration_accuracy', group: 'coordinated', value: 0.94, unit: 'ratio', note: '合成演示：快慢双通道' },
    { metric: 'orchestration_evidence_rate', group: 'baseline', value: 0.69, unit: 'ratio', note: '合成演示：全程序派发' },
    { metric: 'orchestration_evidence_rate', group: 'coordinated', value: 0.93, unit: 'ratio', note: '合成演示：快慢双通道' },
    { metric: 'orchestration_latency_s', group: 'baseline', value: 6.8, unit: 's', note: '合成演示：全程序派发' },
    { metric: 'orchestration_latency_s', group: 'coordinated', value: 11.4, unit: 's', note: '合成演示：慢通道多花时间' },
    { metric: 'harness_pass_rate@基线', group: 'coordinated', value: 0.75, unit: 'ratio', note: '合成演示：行为回归重跑' },
    { metric: 'harness_tool_calls_avg@基线', group: 'coordinated', value: 8.4, unit: '次', note: '合成演示：行为回归重跑' },
    { metric: 'harness_duration_s@基线', group: 'coordinated', value: 42.6, unit: 's', note: '合成演示：行为回归重跑' },
    { metric: 'harness_pass_rate@修复后', group: 'coordinated', value: 0.88, unit: 'ratio', note: '合成演示：行为回归重跑' },
    { metric: 'harness_tool_calls_avg@修复后', group: 'coordinated', value: 7.1, unit: '次', note: '合成演示：行为回归重跑' },
    { metric: 'harness_duration_s@修复后', group: 'coordinated', value: 36.2, unit: 's', note: '合成演示：行为回归重跑' },
    { metric: 'learning_agent_share@批次1', group: 'knowledge', value: 0.62, unit: 'ratio', note: '合成演示：资料批次' },
    { metric: 'learning_agent_share@批次2', group: 'knowledge', value: 0.54, unit: 'ratio', note: '合成演示：资料批次' },
    { metric: 'learning_agent_share@批次3', group: 'knowledge', value: 0.47, unit: 'ratio', note: '合成演示：资料批次' },
    { metric: 'learning_review_min_per_100@批次1', group: 'knowledge', value: 18.5, unit: 'min', note: '合成演示：资料批次' },
    { metric: 'learning_review_min_per_100@批次2', group: 'knowledge', value: 14.2, unit: 'min', note: '合成演示：资料批次' },
    { metric: 'learning_review_min_per_100@批次3', group: 'knowledge', value: 11.6, unit: 'min', note: '合成演示：资料批次' },
    // 限值与容量是配置项，缺了就显示 —，不在页面里写死数字。
    { metric: 'transformer_limit_pct', group: 'config', value: 85, unit: '%', note: '配置' },
    { metric: 'battery_soc_floor_pct', group: 'config', value: 20, unit: '%', note: '配置' },
    { metric: 'contract_capacity_kw', group: 'config', value: 1200, unit: 'kW', note: '配置' },
    { metric: 'compressed_air_floor_mpa', group: 'config', value: 0.6, unit: 'MPa', note: '配置' },
    { metric: 'hvac_supply_setpoint_c', group: 'config', value: 18, unit: '℃', note: '配置' },
    { metric: 'compressed_air_leak_threshold', group: 'config', value: 0.08, unit: 'MPa/min', note: '配置' },
  )
  data.events.sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
  data.decisions.sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
  return data
}



