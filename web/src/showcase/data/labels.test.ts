import { expect, it } from 'vitest'
import {
  OUTCOME_LABELS, TICKET_STATUS_LABELS, ticketStatusLabel, WORK_ORDER_STATUS_LABELS, workOrderStatusLabel,
} from '../../shared/labels'
import {
  businessStatusLabel, deviceLabel, evidenceSummary, expertLabel, plainText, refLabel, runStatusLabel, ticketLabel, toolLabel, toolSummaryText,
} from './labels'

it('裁决 outcome、事件单与操作工单状态覆盖 contracts 枚举', () => {
  expect(OUTCOME_LABELS.work_order).toBe('转人工')
  expect(OUTCOME_LABELS.to_human).toBe('转人工')
  expect(OUTCOME_LABELS.sampling).toBe('抽查待确认')
  // 事件单与操作工单是两套状态，各查各的，不互相兜底。
  expect(TICKET_STATUS_LABELS.open).toBe('进行中')
  expect(TICKET_STATUS_LABELS.resolved).toBe('已解决')
  expect(ticketStatusLabel('rejected')).toBe('rejected')
  expect(WORK_ORDER_STATUS_LABELS.rejected).toBe('已拒绝')
  expect(WORK_ORDER_STATUS_LABELS.expired).toBe('已过期')
  expect(workOrderStatusLabel('rejected')).toBe('已拒绝')
})

it('运行状态与工具名覆盖真实导出里的枚举，认不出的保留原名', () => {
  expect(runStatusLabel('timeout')).toBe('超时')
  expect(runStatusLabel('blocked')).toBe('受阻')
  expect(runStatusLabel('cancelled')).toBe('已取消')
  expect(runStatusLabel('finished')).toBe('已完成')
  expect(runStatusLabel('unexpected')).toBe('unexpected')

  expect(toolLabel('receive_event')).toBe('接收事件')
  expect(toolLabel('open_work_order')).toBe('开维修工单')
  expect(toolLabel('switch_circuit')).toBe('切照明回路')
  expect(toolLabel('verify_receipt')).toBe('核对回执')
  expect(toolLabel('custom_tool')).toBe('custom_tool')
})

it('工具没写摘要时业务状态走中文映射', () => {
  expect(businessStatusLabel('done')).toBe('已完成')
  expect(businessStatusLabel('pending')).toBe('处理中')
  expect(businessStatusLabel('degraded')).toBe('降级结果')
  expect(businessStatusLabel('failed')).toBe('失败')
  expect(businessStatusLabel('custom_status')).toBe('custom_status')
})

it('设备、专家、事件单与依据编号都有中文名', () => {
  expect(deviceLabel('entity://demo-electronics/transformer/T1')).toBe('T1 变压器')
  expect(deviceLabel('entity://demo-electronics/compressor/AIR-1')).toBe('空压机')
  expect(expertLabel('energy')).toBe('能源专家')
  expect(ticketLabel('ticket://tk-0001')).toBe('1 号事件单')
  expect(refLabel('rule://tou-2026#peak')).toBe('分时电价·峰段')
  expect(refLabel('document://synthetic-hvac-manual')).toBe('暖通手册')
  expect(refLabel('document://maintenance-manual')).toBe('空压机运行维护手册')
})

it('自由文本里的策略编号和提案编号换成白话', () => {
  expect(plainText('proposal://pp-0001：2 个动作')).toBe('2 个动作')
  expect(plainText('按 policy-1')).toBe('按现行策略')
  expect(plainText('置信度 0.82')).toBe('把握 82%')
  expect(plainText('代价 -120.0 元')).toBe('折算净省 ¥120.0')
})

it('不带冒号的提案、事件单/工单/知识版本编号、大写策略和千分位金额也换白话', () => {
  expect(plainText('见 proposal://P-0001')).toBe('见 方案')
  expect(plainText('ticket://tk-0001 已关闭')).toBe('1 号事件单 已关闭')
  expect(plainText('已派 wo-0012，关联 tk-0003 与 vm-0004')).toBe('已派 12 号工单，关联 3 号事件单 与 知识版本 v4')
  expect(plainText('按 POLICY-2 执行')).toBe('按现行策略 执行')
  expect(plainText('（POLICY-2）')).toBe('')
  expect(plainText('代价 -1,410.0 元')).toBe('折算净省 ¥1410.0')
})

it('工具摘要里的代价金额去掉，只留动作数与把握', () => {
  expect(toolSummaryText('proposal://pp-0001：2 个动作，代价 -120.0 元，置信度 0.82')).toBe('2 个动作，把握 82%')
  expect(toolSummaryText('proposal://pp-0002：1 个动作，代价 1,410.0 元')).toBe('1 个动作')
})

it('证据摘要只放行动作类事件，JSON、英文键和内部编号不原样展示', () => {
  expect(evidenceSummary('u-reviewer confirmed：提议 work_order', 'action_decided')).toBe('值班人员已确认：提请确认操作工单')
  expect(evidenceSummary('张师傅 confirmed：提议 work_order', 'action_decided')).toBe('值班人员已确认：提请确认操作工单')
  expect(evidenceSummary('事项已不可执行，动作置 expired：已按预案执行', 'action_decided')).toBe('事项已不可执行，动作置为已过期：已按预案执行')

  // 同接口的 gate_decided 摘要是理由拼接，conflict_detected 是 JSON，都不给观众看。
  expect(evidenceSummary('计划状态 executed；动作 act-c49e 未提交', 'gate_decided')).toBe('这条记录没有可展示的说明')
  expect(evidenceSummary('{"expert": "energy", "proposal_ids": ["proposal://P-0001"]}', 'conflict_detected')).toBe('这条记录没有可展示的说明')
  expect(evidenceSummary('峰段电价规则适用，峰值时段前移', 'rule_applied')).toBe('这条记录没有可展示的说明')

  // 动作类摘要里夹带内部编号时也走兜底。
  expect(evidenceSummary('u-reviewer confirmed：已处理 act-c49e45384523348d', 'action_decided')).toBe('这条记录没有可展示的说明')
})
