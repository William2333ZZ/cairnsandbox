'use client'
import type { ReactNode } from 'react'
import { AuthProvider } from '@/lib/auth'
import { I18nProvider } from '@/lib/i18n'
import { QueryProvider } from '@/lib/query'

export function Providers({ children }: { children: ReactNode }) {
  return <I18nProvider><QueryProvider><AuthProvider>{children}</AuthProvider></QueryProvider></I18nProvider>
}
