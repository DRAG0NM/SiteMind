import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function stroke({ size = 20, children, ...rest }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...rest}>{children}</svg>
  )
}

/** 品牌标：厂房轮廓。 */
export function BrandMark({ size = 30, ...rest }: IconProps) {
  return (
    <svg width={size} height={size * 1.27} viewBox="0 0 30 38" fill="none" aria-hidden="true" {...rest}>
      <path d="M2 36V14l9 6v-6l9 6V6h8v30z" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  )
}

export const PauseIcon = (props: IconProps) => (
  <svg width={props.size ?? 12} height={(props.size ?? 12) * 1.17} viewBox="0 0 12 14" aria-hidden="true" {...props}>
    <path d="M1 0h3v14H1zM8 0h3v14H8z" fill="currentColor" />
  </svg>
)

export const PlayIcon = (props: IconProps) => (
  <svg width={props.size ?? 12} height={(props.size ?? 12) * 1.17} viewBox="0 0 12 14" aria-hidden="true" {...props}>
    <path d="M1 0l11 7-11 7z" fill="currentColor" />
  </svg>
)

export const ResetViewIcon = (props: IconProps) => stroke({ ...props, children: <><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4" /><circle cx="12" cy="12" r="2.5" /></> })
export const ZoomBuildingIcon = (props: IconProps) => stroke({ ...props, children: <><circle cx="11" cy="11" r="6" /><path d="M20 20l-4.5-4.5M11 8.5v5M8.5 11h5" /></> })
export const LoadColorIcon = (props: IconProps) => stroke({ ...props, children: <path d="M12 3s7 7.2 7 11.5A7 7 0 0 1 5 14.5C5 10.2 12 3 12 3z" /> })
export const RoofCutIcon = (props: IconProps) => stroke({ ...props, children: <path d="M3 20h18M5 20V9l7-5 7 5v11M12 4v16" /> })
export const SectionIcon = (props: IconProps) => stroke({ ...props, children: <path d="M12 3l9 5-9 5-9-5zM3 13l9 5 9-5M3 17l9 5 9-5" /> })

export const WallScreenIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <><rect x="1" y="1" width="16" height="6" /><rect x="1" y="9" width="7" height="4" /><rect x="10" y="9" width="7" height="4" /></> })
export const AgentScreenIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <><rect x="2" y="1" width="14" height="9" /><path d="M6 13h6" /></> })
export const DeskScreenIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <><rect x="3" y="2" width="14" height="9" /><path d="M7 15h6M8 19h8" /></> })

export const MetricsScreenIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <><path d="M2 20h18" /><path d="M4 16V9M9 16V5M14 16V11M19 16V7" /></> })
export const KnowledgeScreenIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <><circle cx="6" cy="6" r="2.5" /><circle cx="17" cy="7" r="2.5" /><circle cx="11" cy="17" r="2.5" /><path d="M8.2 7.2l6.3-.3M7.2 8.2l2.7 6.6M15.7 9.2l-3.4 5.6" /></> })
export const CloseIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <path d="M5 5l14 14M19 5L5 19" /> })
export const CheckIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 18, children: <path d="M3 9.5l4 4 8-9" /> })
export const ArrowRightIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 16, children: <path d="M4 12h16M14 5l7 7-7 7" /> })
export const SkipIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 16, children: <><path d="M5 5l7 7-7 7M13 5l7 7-7 7" /></> })
export const RestoreIcon = (props: IconProps) => stroke({ ...props, size: props.size ?? 16, children: <path d="M4 12a8 8 0 1 0 3-6.2M4 4v5h5" /> })
