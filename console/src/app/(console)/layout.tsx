'use client'
import type { ReactNode } from 'react'
import { AuthGate } from '@/components/AuthGate'
import { Shell } from '@/components/Shell'

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return <AuthGate><Shell>{children}</Shell></AuthGate>
}
