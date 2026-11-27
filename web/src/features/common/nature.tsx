/** 数据性质徽章：每个视图都要标出数据是实测、仿真还是合成（B46 契约的 DataNature）。 */
import './common.css'

export type DataNature = 'measured' | 'derived' | 'simulated' | 'synthetic' | 'configured'

export const NATURE_LABEL: Record<DataNature, string> = {
  measured: '实测', derived: '推算', simulated: '仿真', synthetic: '合成', configured: '配置',
}

const NATURE_TITLE: Record<DataNature, string> = {
  measured: '接口或运行账本里的原始读数',
  derived: '由原始读数按公式或规则算出',
  simulated: '仿真器推演的结果',
  synthetic: '演示补编的数据，没有真实来源',
  configured: '配置文件里的固定参数',
}

/** 徽章：真实接口来的数据用冷蓝（与 Agent 动作同色），演示与仿真数据用中性描边；来源没标性质时也不冒充实测。 */
export function NatureBadge({ nature, source }: { nature?: DataNature | null; source?: string }) {
  if (!nature) {
    return (
      <span className="nature-badge" title="数据来源没有标注性质，不按实测显示">
        来源未标注{source ? ` · ${source}` : ''}
      </span>
    )
  }
  const live = nature === 'measured' || nature === 'derived'
  return (
    <span className={'nature-badge' + (live ? ' live' : '')} title={NATURE_TITLE[nature]}>
      {NATURE_LABEL[nature]}{source ? ` · ${source}` : ''}
    </span>
  )
}
