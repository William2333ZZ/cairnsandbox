// 令牌在 src/app/globals.css 的 CSS 变量里(沿用工作台的设计令牌,D51);Tailwind 只在它们上面生成类名。
import type { Config } from 'tailwindcss'
const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    colors: {
      transparent: 'transparent', current: 'currentColor',
      bg: 'var(--c-bg)', 'bg-alt': 'var(--c-bg-alt)', panel: 'var(--c-panel)', sidebar: 'var(--c-sidebar)', card: 'var(--c-card)', popover: 'var(--c-popover)', input: 'var(--c-input)',
      border: 'var(--c-border)', 'border-hover': 'var(--c-border-hover)', surface: 'var(--c-surface)', hover: 'var(--c-hover)', selected: 'var(--c-selected)', link: 'var(--c-link)',
      fg: 'var(--c-fg)', 'fg-subtle': 'var(--c-fg-subtle)', 'fg-subtlest': 'var(--c-fg-subtlest)', 'fg-inverse': 'var(--c-fg-inverse)', brand: 'var(--c-brand)', accent: 'var(--c-accent)',
      success: 'var(--c-success)', 'success-surface': 'var(--c-success-surface)', warning: 'var(--c-warning)', 'warning-surface': 'var(--c-warning-surface)',
      destructive: 'var(--c-destructive)', 'destructive-surface': 'var(--c-destructive-surface)', waiting: 'var(--c-waiting)', 'waiting-surface': 'var(--c-waiting-surface)',
    },
    fontSize: {
      'ui-xl': ['calc(var(--ui-font-size) * 1.3)', '1.35'], 'ui-lg': ['calc(var(--ui-font-size) * 1.15)', '1.4'], 'ui-base': ['var(--ui-font-size)', '1.5'],
      'ui-caption': ['calc(var(--ui-font-size) * 0.92)', '1.45'], 'ui-sm': ['calc(var(--ui-font-size) * 0.85)', '1.4'], 'ui-xs': ['calc(var(--ui-font-size) * 0.75)', '1.35'], code: ['var(--code-font-size)', '1.5'],
    },
    fontFamily: { sans: 'var(--font-sans)', mono: 'var(--font-mono)' },
    extend: { borderRadius: { DEFAULT: '6px', sm: '4px', lg: '8px', xl: '12px', '2xl': '16px' } },
  },
  plugins: [],
}
export default config
