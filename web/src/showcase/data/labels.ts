import { spotOf, SPOT_LABEL } from '../../shared/campus'

/** 设备编号末两段 → 观众看得懂的名字；认不出的退回图上位置名或编号末段。 */
const DEVICE_LABEL: Record<string, string> = {
  'transformer/T1': 'T1 变压器',
  'battery/BESS1': '储能柜',
  'elevator_group/EG-PROD': '生产楼货梯',
  'elevator_group/EG-ADMIN': '行政楼客梯',
  'hvac_zone/ADMIN-3F': '行政楼 3F 空调',
  'hvac_zone/FACTORY-AHU': '车间空调机组',
  'compressor/AIR-1': '空压机',
  'pv/PV-1': '光伏',
  'lighting/ADMIN-3F': '行政楼 3F 照明',
  'lighting/FACTORY-1F': '车间 1F 照明',
}

export function deviceLabel(entityId: string): string {
  const parts = entityId.split('/').filter(Boolean)
  const known = DEVICE_LABEL[parts.slice(-2).join('/')]
  if (known) return known
  const spot = spotOf(entityId)
  if (spot) return SPOT_LABEL[spot]
  return parts.pop() ?? entityId
}

const EXPERT_LABEL: Record<string, string> = {
  energy: '能源专家',
  elevator: '电梯专家',
  hvac: '暖通专家',
  equipment: '设备监测专家',
  watchdog: '值守程序',
  main: '主 Agent',
}

export function expertLabel(expert: string): string {
  return EXPERT_LABEL[expert] ?? expert
}

/** 表格、图谱里放不下“专家”二字时用短名。 */
export function expertShort(expert: string): string {
  return expertLabel(expert).replace(/专家$/, '')
}

/** 依据编号 → 中文名；不露 rule://、document:// 这类内部编号。 */
export function refLabel(ref: string): string {
  if (ref.startsWith('rule://')) {
    if (ref.startsWith('rule://tou-2026')) return ref.includes('#peak') ? '分时电价·峰段' : '分时电价'
    if (ref.startsWith('rule://synthetic-elevator')) return '电梯分区规则'
    if (ref.startsWith('rule://synthetic-hvac')) return '暖通预冷规则'
    if (ref.toLowerCase().includes('leak')) return '空压泄漏判别规则'
    return '规则原文'
  }
  if (ref.startsWith('document://')) {
    if (ref.includes('synthetic-hvac-manual')) return '暖通手册'
    if (ref.includes('maintenance-manual')) return '空压机运行维护手册'
    return '资料原文'
  }
  if (ref.startsWith('entity://')) return deviceLabel(ref)
  if (ref.startsWith('decision://')) return '裁决记录'
  // 临时规则和知识版本也是内部编号；普通中文说明原样返回。
  if (ref.startsWith('temp://')) return '临时规则'
  const knowledge = ref.match(/^knowledge v(\d+)$/)
  if (knowledge) return `知识 v${knowledge[1]}`
  return ref.includes('://') ? '依据原文' : ref
}

/** ticket://tk-0001 → 1 号事件单；没有数字就只写事件单。 */
export function ticketLabel(ticketId: string): string {
  const tail = ticketId.split('/').pop() ?? ticketId
  const digits = tail.match(/(\d+)$/)
  return digits ? `${Number(digits[1])} 号事件单` : '事件单'
}

/** 0.79 → 把握 79% */
export const confidenceText = (value: number) => `把握 ${Math.round(value * 100)}%`

/** 折算成钱的净代价：负数=净省，正数=多花，0=不涉及电费。 */
export function costText(score: number): string {
  if (score < 0) return `折算净省 ¥${Math.abs(score).toFixed(1)}`
  if (score > 0) return `折算多花 ¥${score.toFixed(1)}`
  return '不涉及电费'
}

/** demo-electronics → 演示电子厂；其他厂名原样显示。 */
export function plantLabel(plantId: string): string {
  return plantId === 'demo-electronics' ? '演示电子厂' : plantId
}

/** 2026-07-01 → 7 月 1 日回放；格式不认时只写当天回放。 */
export function replayDateText(localDate: string): string {
  const [, month, day] = localDate.split('-')
  if (!month || !day) return '当天回放'
  return `${Number(month)} 月 ${Number(day)} 日回放`
}

const AGENT_LABEL: Record<string, string> = {
  plantkg_main: '主 Agent',
  energy_expert: '能源专家',
  elevator_expert: '电梯专家',
  hvac_expert: '暖通专家',
  equipment_expert: '设备监测专家',
  watchdog: '值守程序',
}

/** 运行记录里的内部英文名改成白话；认不出的原样显示。 */
export function agentLabel(agent: string): string {
  return AGENT_LABEL[agent] ?? agent
}

const RUN_STATUS: Record<string, string> = {
  finished: '已完成',
  running: '进行中',
  failed: '失败',
  pending: '等待中',
  timeout: '超时',
  blocked: '受阻',
  cancelled: '已取消',
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS[status] ?? status
}

/** 证据事件类型（EvidenceEventType）→ 中文记录名；认不出的不露英文枚举。 */
const EVENT_TYPE_LABEL: Record<string, string> = {
  tool_called: '调用工具',
  tool_result: '工具返回',
  retrieval: '检索资料',
  graph_query: '查设备图谱',
  rule_lookup: '查规则原文',
  rule_applied: '适用规则',
  conflict_detected: '发现冲突',
  path_switch: '改用其他依据',
  plan_created: '生成方案',
  gate_decided: '安全检查',
  pending_action_proposed: '提交待批动作',
  action_decided: '动作已裁决',
  job_submitted: '提交作业',
  job_finished: '作业完成',
  execution_recorded: '记录执行',
  version_activated: '启用知识新版本',
  candidate_status_changed: '候选资料状态更新',
}

export function eventTypeLabel(eventType: string): string {
  return EVENT_TYPE_LABEL[eventType] ?? '其他记录'
}

/** 证据事件结果（ResultStatus）→ 中文状态。 */
const RESULT_STATUS: Record<string, string> = {
  ok: '成功',
  pending: '等待中',
  degraded: '降级完成',
  failed: '失败',
  conflict: '有冲突',
}

export function resultStatusLabel(status: string): string {
  return RESULT_STATUS[status] ?? '状态未知'
}

const BUSINESS_STATUS: Record<string, string> = {
  done: '已完成',
  pending: '处理中',
  degraded: '降级结果',
  failed: '失败',
}

/** 工具调用没写摘要时的业务状态兜底；不把 done/failed 这类英文键直接露给观众。 */
export function businessStatusLabel(status: string): string {
  return BUSINESS_STATUS[status] ?? status
}

/** 工具名映射在工作台与展示页共用一份，见 shared/labels。 */
export { toolLabel } from '../../shared/labels'

/**
 * 数据里自由文本（裁决备注、问答、工具摘要）夹带的内部写法换成白话：
 * 策略编号、提案编号、事件单/工单/知识版本编号、“代价 -120.0 元”“置信度 0.82”。导入的 day.json 也照此显示。
 */
export function plainText(text: string): string {
  return text
    .replace(/（policy-[\w.-]+）/gi, '')
    .replace(/按 ?policy-[\w.-]+/gi, '按现行策略')
    .replace(/policy-[\w.-]+/gi, '现行策略')
    .replace(/proposal:\/\/[\w.-]+[：:]\s*/g, '')
    .replace(/proposal:\/\/[\w.-]+/g, '方案')
    .replace(/ticket:\/\/tk-(\d+)/gi, (_, value: string) => `${Number(value)} 号事件单`)
    .replace(/\btk-(\d+)\b/gi, (_, value: string) => `${Number(value)} 号事件单`)
    .replace(/\bwo-(\d+)\b/gi, (_, value: string) => `${Number(value)} 号工单`)
    .replace(/\bvm-(\d+)\b/gi, (_, value: string) => `知识版本 v${Number(value)}`)
    .replace(/代价 ?(-?\d[\d,]*(?:\.\d+)?) ?元/g, (_, value: string) => costText(Number(value.replace(/,/g, ''))))
    .replace(/置信度 ?(0?\.\d+|1(?:\.0+)?)/g, (_, value: string) => confidenceText(Number(value)))
}

const ACTION_KIND_LABEL: Record<string, string> = {
  work_order: '操作工单', human_decision: '人工待办', correction: '纠错', sampling: '抽查',
  review: '复核', review_decision: '复核结论', plan_confirmation: '方案确认',
  publish: '发布', rollback: '回滚',
}

const ACTION_STATUS_LABEL: Record<string, string> = {
  proposed: '待确认', confirmed: '已确认', rejected: '已驳回', expired: '已过期',
}

/**
 * 只有动作类事件的摘要来自固定格式（提议 kind、复核人 confirmed：原因、动作置 expired）。
 * gate_decided 的摘要是安全检查理由拼接，conflict_detected 是 JSON，其他类型也不保证可读；
 * 白名单外的摘要一律不原样展示。
 */
const ACTION_EVENT_TYPES = new Set(['pending_action_proposed', 'action_decided'])

/** JSON、英文键/枚举、内部编号都不给观众看。 */
const INTERNAL_LOOKING = /[{}[\]"]|\b[a-z][a-z_]{2,}\b|:\/\//

const NO_SUMMARY = '这条记录没有可展示的说明'

/**
 * 证据摘要：后端写入的 “u-reviewer confirmed：…”“提议 work_order”“动作置 expired”
 * 是内部写法和英文枚举，换成值班人看得懂的白话；不显示复核员账号与动作类型编号。
 * 摘要只对动作类事件放行，其余类型和夹带内部写法的文本用兜底白话。
 */
export function evidenceSummary(text: string, eventType: string): string {
  if (!ACTION_EVENT_TYPES.has(eventType)) return NO_SUMMARY
  const translated = plainText(text)
    // actor 可能是中文值班名，不能只认 \w；一直取到冒号前的空白为止。
    .replace(/^([^\s：]+)\s+(confirmed|rejected)：/, (_match, _actor: string, status: string) =>
      `值班人员${status === 'confirmed' ? '已确认' : '已驳回'}：`)
    .replace(/提议\s+([a-z_]+)/g, (_match, kind: string) => `提请确认${ACTION_KIND_LABEL[kind] ?? '待办'}`)
    .replace(/动作置\s+(proposed|confirmed|rejected|expired)/g,
      (_match, status: string) => `动作置为${ACTION_STATUS_LABEL[status] ?? '已处理'}`)
  return INTERNAL_LOOKING.test(translated) ? NO_SUMMARY : translated
}

/**
 * 工具摘要里的“代价 X 元”是这个工具自己的估算，和候选表的折算口径不同；
 * 去掉金额只留动作数与把握，免得两个金额并排出现被当成映射算错。
 */
export function toolSummaryText(text: string): string {
  return plainText(text.replace(/[，,]?\s*代价 ?-?\d[\d,]*(?:\.\d+)? ?元/g, ''))
}
