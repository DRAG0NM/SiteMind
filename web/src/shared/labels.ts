/** 裁决 outcome 与事件单状态的中文标签；工作台、展示页、Overview 共用一张表。 */

/** contracts/day.schema.json 的 DecisionResult.outcome 枚举。 */
export type DecisionOutcome = 'executed' | 'work_order' | 'to_human' | 'sampling' | 'blocked'

export const OUTCOME_LABELS: Record<DecisionOutcome, string> = {
  executed: '自动执行',
  work_order: '转人工',
  to_human: '转人工',
  sampling: '抽查待确认',
  blocked: '转人工',
}

/** contracts/day.schema.json 的 Ticket.status 枚举。 */
export const TICKET_STATUS_LABELS: Record<string, string> = {
  open: '进行中',
  waiting: '待确认',
  blocked: '受阻',
  resolved: '已解决',
  closed: '已关闭',
}

/** contracts/day.schema.json 的 WorkOrder.status 枚举；和事件单状态是两套，不能混用。 */
export const WORK_ORDER_STATUS_LABELS: Record<string, string> = {
  proposed: '待确认',
  confirmed: '已确认',
  rejected: '已拒绝',
  expired: '已过期',
  done: '已完成',
}

export const outcomeLabel = (outcome: string) => OUTCOME_LABELS[outcome as DecisionOutcome] ?? outcome

/** 态势墙的叙事口径：一句话里说“已自动处置 / 已升级 / 已知会值班”，未知按已处理。 */
export const WALL_OUTCOME_LABELS: Record<DecisionOutcome, string> = {
  executed: '已自动处置',
  work_order: '已升级',
  to_human: '已知会值班',
  sampling: '抽查待确认',
  blocked: '已知会值班',
}

export const wallOutcomeLabel = (outcome: string) => WALL_OUTCOME_LABELS[outcome as DecisionOutcome] ?? '已处理'

export const ticketStatusLabel = (status: string) => TICKET_STATUS_LABELS[status] ?? status

export const workOrderStatusLabel = (status: string) => WORK_ORDER_STATUS_LABELS[status] ?? status

/** 空压站事件的短状态：压力/泄漏类才说压力异常，其余报事件进行中；工作台与 3D 共用。 */
export const airEventStatus = (eventType: string | null | undefined) =>
  /pressure|leak/i.test(eventType ?? '') ? '压力异常' : '事件进行中'

/** 工具调用的中文名：工作台泳道与展示页运行记录共用，别在两处各抄一份。 */
export const TOOL_LABEL: Record<string, string> = {
  receive_event: '接收事件',
  dispatch_experts: '分派专家',
  resolve_conflict: '冲突裁决',
  dispatch_actions: '下发动作',
  energy_propose: '提出能源方案',
  elevator_propose: '提出电梯方案',
  hvac_propose: '提出暖通方案',
  equipment_propose: '提出设备方案',
  verify_receipt: '核对回执',
  open_work_order: '开维修工单',
  switch_circuit: '切照明回路',
  ts_query: '查询时序数据',
  sensor_query: '查询传感器读数',
  ledger: '查电费账本',
  graph_query: '查设备图谱',
  lookup_rules: '查规则原文',
  retrieve_evidence: '检索资料原文',
  workorder_search: '查历史工单',
  get_job: '查作业进度',
  get_plan: '读方案详情',
  explain: '解释判断依据',
  recall: '检索记忆',
  ask_operator: '询问值班员',
  escalate: '转人工处理',
  trial: '试算方案',
  run_day: '运行一天仿真',
  propose_plan_confirmation: '提请确认方案',
  propose_publish: '提请发布',
  propose_rollback: '提请回滚',
  prepare_tariff_review: '准备电价复核',
  safety_tags: '查安全标签',
  trace_impact: '查影响范围',
  find_sources: '找依据出处',
  priority_rules: '查优先规则',
  playbooks: '查处置手册',
}

export const toolLabel = (tool: string) => TOOL_LABEL[tool] ?? tool
