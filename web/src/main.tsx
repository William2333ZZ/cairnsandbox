import React from 'react'
import { createRoot } from 'react-dom/client'
import './theme.css'
import { activate } from './kernel'
import { Shell } from './layout/Shell'
import { useStore } from './store'
import { plugins } from './plugins'

async function boot() {
  await activate(plugins)
  useStore.getState().startLive()
  window.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); useStore.getState().toggleSidebar() } })
  createRoot(document.getElementById('root')!).render(<React.StrictMode><Shell /></React.StrictMode>)
}
void boot()
