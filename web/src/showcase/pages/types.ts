/** 展示页各标签页的统一入参：页面只读 day.json 与回放头，不自己加载数据。 */
export interface PageProps {
  day: import('../data/types').Day
  /** 回放时刻（毫秒） */
  cursor: number
  /** 打开右侧证据抽屉；实体编号可以为空（没有对应设备时只放标题） */
  openEvidence: (entityId: string | null, fallbackTitle: string, drawerTab?: 'rule' | 'graph' | 'experts' | 'runs', ticketId?: string) => void
  /** 跳到厂区图上的某个位置（自动巡演用） */
  focusSpot?: (spot: import('../../shared/campus').CampusSpot) => void
  /** 数据来源类别：sample=内置合成样例，backend=在线后端导出，页面据此标明合成内容，不冒充实测 */
  source?: 'exported' | 'imported' | 'sample' | 'backend'
  /** 数据来源的人类可读说明，来自加载/导入时的标签 */
  sourceLabel?: string
}
