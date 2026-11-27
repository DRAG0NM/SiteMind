import * as React from 'react'
import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

// shadcn/ui-style local component: owned by the application, not a remote dependency.
export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)) }
export function Button({className, ...props}: React.ComponentProps<'button'>) {
  return <button className={cn('inline-flex items-center justify-center disabled:pointer-events-none disabled:opacity-50', className)} {...props}/>
}
