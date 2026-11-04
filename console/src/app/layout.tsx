import type { Metadata } from 'next'
import './globals.css'
import { Providers } from './providers'

export const metadata: Metadata = { title: 'Cairn 沙箱云控制台', description: 'Cairn sandbox cloud console' }
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body><Providers>{children}</Providers></body></html>
}
