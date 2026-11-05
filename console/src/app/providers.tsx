'use client'
import { useEffect, type ReactNode } from 'react'
import { AuthProvider } from '@/lib/auth'
import { I18nProvider } from '@/lib/i18n'
import { QueryProvider } from '@/lib/query'

// 登录、注册和公开状态页不在控制台外壳里,也要跟随已保存的主题(没有保存时跟随系统)。
function ThemeInit() {
  useEffect(() => {
    let saved = ''; try { saved = localStorage.getItem('cp_theme') || '' } catch { /* */ }
    document.documentElement.dataset.theme = saved === 'dark' || saved === 'light' ? saved : window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }, [])
  return null
}

export function Providers({ children }: { children: ReactNode }) {
  return <I18nProvider><ThemeInit /><QueryProvider><AuthProvider>{children}</AuthProvider></QueryProvider></I18nProvider>
}
