// Plugin table: order is only a hint; the kernel resolves `requires`. Each feature is one plugin under src/plugins/<name>.
// To add a page: write a plugin that registers a nav entry and a route, add it here. Removing a line removes the feature.
import type { Plugin } from '../kernel'
import { attention } from './attention'
import { intake } from './intake'
import { sandboxes } from './sandboxes'
import { evaluation } from './eval'
import { nodes } from './nodes'
import { settings } from './settings'
import { rails } from './rails'
import { versions } from './versions'
import { session } from './session'
import { memory } from './memory'
import { routines } from './routines'
import { notify } from './notify'
import { connectors } from './connectors'
import { agents } from './agents'
import { budget } from './budget'
import { prompts } from './prompts'
import { cloud } from './cloud'
import { home } from './home'

export const plugins: Plugin[] = [rails, session, notify, budget, prompts, agents, routines, memory, connectors, attention, intake, sandboxes, evaluation, versions, cloud, home, nodes, settings]
