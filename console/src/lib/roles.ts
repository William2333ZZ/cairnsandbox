// 三种角色决定界面长出什么(docs/research/console-redesign):operator = 部署者(admin 令牌),owner = 账户的 owner,member = 其余。
export type Role = 'operator' | 'owner' | 'member'
export type Me = { user: string | null; admin: boolean; auth: boolean; account?: string; role?: string; projects?: string[]; suspended?: { reason: string; at: number } }

export function roleOf(me: Me | null): Role {
  if (!me) return 'member'
  if (me.admin) return 'operator'
  return me.role === 'owner' ? 'owner' : 'member'
}

export type NavItem = { id: string; href: string; roles: Role[]; group: 'main' | 'ops' }
const ALL: Role[] = ['operator', 'owner', 'member']
export const NAV: NavItem[] = [
  { id: 'overview', href: '/overview/', roles: ALL, group: 'main' },
  { id: 'sandboxes', href: '/sandboxes/', roles: ALL, group: 'main' },
  { id: 'templates', href: '/templates/', roles: ALL, group: 'main' },
  { id: 'volumes', href: '/volumes/', roles: ALL, group: 'main' },
  { id: 'network', href: '/network/', roles: ALL, group: 'main' },
  { id: 'usage', href: '/usage/', roles: ALL, group: 'main' },
  { id: 'members', href: '/members/', roles: ALL, group: 'main' },
  { id: 'audit', href: '/audit/', roles: ALL, group: 'main' },
  { id: 'settings', href: '/settings/', roles: ['operator', 'owner'], group: 'main' },
  { id: 'nodes', href: '/ops/', roles: ['operator'], group: 'ops' },
  { id: 'admin', href: '/admin/', roles: ['operator'], group: 'ops' },
]
export const navFor = (role: Role) => NAV.filter(n => n.roles.includes(role))
/** 能做的事:owner 与 operator 改配额、批放宽、管成员;所有人建与用沙箱。 */
export const canManage = (role: Role) => role === 'owner' || role === 'operator'
