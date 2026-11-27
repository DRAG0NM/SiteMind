/** 展示页八个标签页：顺序照第二版稿（总览、主 Agent、资料与图谱、效果对比、电梯、能源与储能、暖通、设备监测）。 */
export const TABS = [
  { key: 'overview', label: '总览' },
  { key: 'agent', label: '主 Agent' },
  { key: 'knowledge', label: '资料与图谱' },
  { key: 'compare', label: '效果对比' },
  { key: 'elevator', label: '电梯' },
  { key: 'energy', label: '能源与储能' },
  { key: 'hvac', label: '暖通' },
  { key: 'equipment', label: '设备监测' },
] as const

export type TabKey = (typeof TABS)[number]['key']
