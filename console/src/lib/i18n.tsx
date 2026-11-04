'use client'
// 文案字典:默认中文,英文是第二语言(P0 就抽字典,文案越少改成本越低)。缺的键回退到中文,再回退到键名。
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { getLang, setLangKey } from './api'

export type Lang = 'zh' | 'en'
const zh: Record<string, string> = {
  'quota.sandboxes':'最多沙箱数(0 = 不限)','quota.storage':'存储上限 GiB','quota.daily':'每日预算 USD','common.save':'保存','common.saved':'已保存',
  'wh.title':'Webhooks','wh.hint':'把这个项目里发生的事推到你的 HTTPS 地址(沙箱创建、构建结束、等你批准的出口放宽、账户停用……)。每条带签名,密钥只显示一次。','wh.secret':'签名密钥(只显示这一次)','wh.add':'添加','wh.allIfNone':'都不选 = 全部事件','wh.disabled':'已关闭(连续失败)','wh.test':'发测试','wh.enable':'重新打开',
  'admin.delete':'删除账户','admin.delete.ask':'删除账户 {id}:它的沙箱、卷、成员和密钥都会被删掉(用量与账本保留,名字不能再用)。输入账户名确认:',
  'usage.daily':'每日花费(近 30 天,UTC)','usage.topsb':'花得最多的沙箱','usage.other':'其余沙箱',
  'builds.log':'构建日志','builds.log.none':'没有这次构建的日志(它跑在别的节点、节点还没升级到带日志的版本,或已超过一小时)。',
  'nav.admin':'账户与滥用','admin.accounts':'账户','admin.account':'账户','admin.state':'状态','admin.billing':'计费','admin.suspended':'已停用','admin.suspend':'停用','admin.unsuspend':'解除停用','admin.suspend.reason':'停用原因(成员会看到):','admin.credit':'充值','admin.credit.ask':'充值金额(美元,负数 = 调整 / 退款):','admin.toPrepaid':'改为预付','admin.toPostpaid':'改为后付','admin.invites':'邀请码','admin.invite.note':'备注','admin.invite.credit':'起步额度 $','admin.invite.maxsb':'最多沙箱','admin.invite.daily':'日预算 $','admin.invite.days':'有效天数','admin.invite.make':'生成邀请码','admin.invite.once':'邀请码(只显示这一次)','admin.invite.unused':'未使用','admin.abuse':'滥用信号(只标记,不自动处置)','admin.abuse.off':'检测器未开启(ABUSE_CPU_PCT=0)。','admin.abuse.none':'没有信号',
  'w.note':'备注','w.credit':'额度','w.used by':'使用者','w.value':'数值',
  'status.title':'服务状态','status.cantreach':'连不上状态接口。','status.overall.operational':'一切正常','status.overall.degraded':'部分降级','status.overall.outage':'服务中断','status.v.operational':'正常','status.v.degraded':'降级','status.v.outage':'中断','status.c.api':'接口','status.c.registry':'路由注册表','status.c.database':'数据库','status.c.storage':'存储','status.c.sandboxes':'沙箱',
  'signup.have':'有邀请码?创建账户','signup.title':'用邀请码创建账户','signup.hint':'邀请码由平台发给你,只能用一次。账户名和用户名:小写字母、数字、_ 或 -,2-32 位。','signup.code':'邀请码','signup.account':'账户名(例如 acme)','signup.user':'你的用户名','signup.go':'创建账户','signup.back':'已有令牌?登录','signup.done':'账户已创建','signup.once':'这是你的令牌,只显示这一次——先存好再继续。','signup.enter':'进入控制台',
  'usage.balance':'余额(预付)','usage.balance.sub':'累计充值 {credits};用完后账户会被暂停,充值后自动恢复',
  'sb.metrics.cpu':'CPU(占一个核的百分比)','sb.metrics.mem':'内存','sb.metrics.egress':'出站速率','sb.metrics.window':'采样窗口','sb.metrics.none':'这台沙箱所在的节点还没有实时指标(节点要升级到带 /metrics 的版本),或这台沙箱没有运行。',
  'keys.title':'API 密钥','keys.hint':'给 CI、脚本或看板用的命名密钥:只能访问当前项目;viewer 只读;可设过期天数(0 = 不过期);随时撤销,立即失效。密钥只在创建时显示一次。','keys.name':'名称','keys.role':'权限','keys.role.member':'成员(读写)','keys.role.viewer':'viewer(只读)','keys.ttl':'有效天数','keys.create':'创建密钥','keys.created':'新密钥(只显示这一次)','keys.revoke':'撤销',
  'w.expires':'过期','w.last used':'最近使用',
  'suspended.banner': '这个账户已被停用:沙箱已暂停,只能查看和清理,不能新建或恢复。原因:{reason}。请联系平台运维。',
  'w.mode':'模式','w.user':'用户','w.role':'角色','w.id':'ID','w.status':'状态','w.created':'创建时间','w.30d':'近 30 天','w.project':'项目','w.by':'提交人','w.sandbox':'沙箱','w.kind':'类型','w.amount':'用量','w.events':'事件数','w.cost':'费用','w.statements':'月结账单','w.layers':'层','w.builds':'构建','w.distribution':'分布','w.template':'模板','w.key':'键','w.node':'节点','w.spec':'规格','w.from':'来源','w.mem / disk':'内存 / 磁盘','w.name':'名称','w.size':'大小','w.input':'输入','w.result':'结果',
  'w.operator only':'仅运维可见','w.default network':'默认网络','w.charts, per-sandbox and per-label breakdown: next phase.':'图表、按沙箱和按标签的拆分:下一期。',
  'app.name': '沙箱云', 'app.switch.console': '沙箱云控制台', 'app.switch.agent': 'Agent 工作台',
  'nav.overview': '概览', 'nav.sandboxes': '沙箱', 'nav.templates': '模板与镜像', 'nav.volumes': '卷', 'nav.network': '网络', 'nav.usage': '用量与账单', 'nav.members': '成员与密钥',
  'nav.audit': '审计', 'nav.settings': '项目设置', 'nav.nodes': '节点与容量', 'nav.ops': '运维', 'nav.new': '新沙箱', 'nav.manage': '管理', 'nav.collapse': '收起 / 展开侧栏', 'nav.nodes.count': '节点 {alive}/{total}', 'nav.unreachable': '控制面不可达',
  'role.operator': '运维', 'role.owner': 'owner', 'role.member': '成员',
  'top.project': '项目', 'top.search': '搜索', 'top.logout': '退出', 'top.lang': 'English',
  'login.title': '登录控制台', 'login.hint': '粘贴你的令牌。令牌由项目的 owner 发给你,只显示一次。', 'login.token': '令牌', 'login.go': '登录', 'login.bad': '令牌不对或已过期。',
  'common.loading': '加载中', 'common.empty': '没有内容', 'common.all': '全部', 'common.refresh': '刷新', 'common.cancel': '取消', 'common.confirm': '确认', 'common.delete': '删除', 'common.copy': '复制', 'common.copied': '已复制',
  'common.none': '无', 'common.error': '出错了', 'common.retry': '重试', 'common.pause': '暂停', 'common.resume': '恢复', 'common.back': '返回',
  'status.running': '运行中', 'status.paused': '已暂停', 'status.unreachable': '失联', 'status.failed': '失败', 'status.creating': '创建中',
  'ov.spend': '今天花费', 'ov.running': '运行中沙箱', 'ov.capacity': '容量', 'ov.pending': '等你批的放宽', 'ov.recent': '最近的事', 'ov.sandboxes': '沙箱', 'ov.nodes': '节点在线',
  'ov.budget': '每日预算', 'ov.quota': '配额', 'ov.paused': '暂停', 'ov.noBudget': '没设预算',
  'ov.start.title': '建第一台沙箱', 'ov.start.body': '三步:拿到令牌,装 SDK,跑一段代码。下面的代码已经填好了这个控制面的地址和项目。', 'ov.start.install': '安装', 'ov.start.run': '运行',
  'sb.title': '沙箱', 'sb.search': '搜索 id、模板、节点、标签', 'sb.filter.status': '状态', 'sb.filter.class': '类别', 'sb.col.id': 'ID', 'sb.col.status': '状态', 'sb.col.template': '模板', 'sb.col.class': '类别',
  'sb.col.node': '节点', 'sb.col.mem': '内存', 'sb.col.age': '已运行', 'sb.col.project': '项目', 'sb.selected': '已选', 'sb.none': '没有符合条件的沙箱', 'sb.deleteConfirm': '删除这些沙箱?不能撤销。',
  'sb.tab.overview': '概览', 'sb.tab.metrics': '指标', 'sb.tab.terminal': '终端', 'sb.tab.network': '网络', 'sb.tab.checkpoints': '检查点', 'sb.tab.ports': '端口与卷', 'sb.tab.events': '事件',
  'sb.equiv': '等价调用', 'sb.rollback.warn': '回滚会丢掉这个检查点之后的所有改动,运行中的进程会回到那一刻。', 'sb.decisions': '最近的出站决定', 'sb.allowed': '放行', 'sb.denied': '拒绝',
  'sb.metrics.soon': '每 5 秒一个点的 CPU、内存、盘与出站曲线随下一期上线。下面是此刻的读数。',
  'net.queue': '等你批的出口放宽', 'net.approve': '批准', 'net.deny': '拒绝', 'net.none': '没有等待批准的放宽。',
  'members.title': '成员', 'members.rotate': '换令牌', 'members.rotated': '新令牌(只显示这一次)',
  'audit.title': '审计', 'audit.verify': '核对哈希链', 'ops.title': '节点与容量', 'ops.cordon': '停止接新', 'ops.uncordon': '恢复接新', 'ops.drain': '排空',
  'soon.title': '这一页还在迁移', 'soon.body': '内容在旧控制台里,功能没少。', 'soon.open': '打开旧控制台',
}
const en: Record<string, string> = {
  'quota.sandboxes':'Max sandboxes (0 = unlimited)','quota.storage':'Storage limit GiB','quota.daily':'Daily budget USD','common.save':'Save','common.saved':'Saved',
  'wh.title':'Webhooks','wh.hint':'Push what happens in this project to your HTTPS endpoint (sandbox created, build finished, an egress relaxation waiting for you, account suspended …). Every delivery is signed; the secret is shown once.','wh.secret':'Signing secret (shown once)','wh.add':'Add','wh.allIfNone':'none checked = all events','wh.disabled':'Disabled (repeated failures)','wh.test':'Send test','wh.enable':'Re-enable',
  'admin.delete':'Delete account','admin.delete.ask':'Delete account {id}: its sandboxes, volumes, members and keys are removed (usage and the ledger stay; the name is reserved). Type the account name to confirm:',
  'usage.daily':'Daily spend (30 days, UTC)','usage.topsb':'Most expensive sandboxes','usage.other':'All other sandboxes',
  'builds.log':'Build log','builds.log.none':'No log for this build (it ran on another node, the node predates build logs, or it is older than an hour).',
  'nav.admin':'Accounts and abuse','admin.accounts':'Accounts','admin.account':'Account','admin.state':'State','admin.billing':'Billing','admin.suspended':'Suspended','admin.suspend':'Suspend','admin.unsuspend':'Unsuspend','admin.suspend.reason':'Reason for suspending (members will see it):','admin.credit':'Add credit','admin.credit.ask':'Amount in USD (negative = adjustment / refund):','admin.toPrepaid':'Make prepaid','admin.toPostpaid':'Make postpaid','admin.invites':'Invite codes','admin.invite.note':'Note','admin.invite.credit':'Starting credit $','admin.invite.maxsb':'Max sandboxes','admin.invite.daily':'Daily budget $','admin.invite.days':'Valid days','admin.invite.make':'Create invite','admin.invite.once':'Invite code (shown once)','admin.invite.unused':'unused','admin.abuse':'Abuse signals (flag only, nothing is done automatically)','admin.abuse.off':'The detector is off (ABUSE_CPU_PCT=0).','admin.abuse.none':'No signals',
  'w.note':'Note','w.credit':'Credit','w.used by':'Used by','w.value':'Value',
  'status.title':'Service status','status.cantreach':'Cannot reach the status endpoint.','status.overall.operational':'All systems operational','status.overall.degraded':'Partially degraded','status.overall.outage':'Service outage','status.v.operational':'Operational','status.v.degraded':'Degraded','status.v.outage':'Outage','status.c.api':'API','status.c.registry':'Routing registry','status.c.database':'Database','status.c.storage':'Storage','status.c.sandboxes':'Sandboxes',
  'signup.have':'Have an invite code? Create an account','signup.title':'Create an account with an invite','signup.hint':'The platform gave you a one-time code. Account and user names: lowercase letters, digits, _ or -, 2-32 characters.','signup.code':'Invite code','signup.account':'Account name (e.g. acme)','signup.user':'Your user name','signup.go':'Create account','signup.back':'Have a token? Sign in','signup.done':'Account created','signup.once':'This is your token, shown once. Save it before you continue.','signup.enter':'Open the console',
  'usage.balance':'Balance (prepaid)','usage.balance.sub':'{credits} added in total; at zero the account is suspended, a top-up lifts it',
  'sb.metrics.cpu':'CPU (% of one core)','sb.metrics.mem':'Memory','sb.metrics.egress':'Egress rate','sb.metrics.window':'Sample window','sb.metrics.none':'This node does not report live metrics yet (it needs the version with /metrics).',
  'keys.title':'API keys','keys.hint':'Named keys for CI, scripts or dashboards: they reach this project only; a viewer is read-only; set a lifetime in days (0 = never expires); revoke any time, it stops working at once. A key is shown once, when created.','keys.name':'Name','keys.role':'Access','keys.role.member':'Member (read/write)','keys.role.viewer':'Viewer (read-only)','keys.ttl':'Days valid','keys.create':'Create key','keys.created':'New key (shown once)','keys.revoke':'Revoke',
  'w.expires':'Expires','w.last used':'Last used','w.created':'Created',
  'suspended.banner': 'This account is suspended: sandboxes are paused; you can view and clean up but not create or resume. Reason: {reason}. Contact the platform operator.',
  'w.kind':'Kind','w.amount':'Amount','w.events':'Events','w.cost':'Cost','w.statements':'Statements','w.layers':'Layers','w.builds':'Builds','w.distribution':'Distribution','w.template':'Template','w.key':'Key','w.node':'Node','w.spec':'Spec','w.from':'From','w.mem / disk':'Memory / disk','w.name':'Name','w.size':'Size','w.input':'Input','w.result':'Result','w.user':'User','w.role':'Role','w.project':'Project','w.by':'By','w.sandbox':'Sandbox',
  'w.default network':'Default network','w.mode':'mode','w.30d':'30 days','w.operator only':'Operators only','w.charts, per-sandbox and per-label breakdown: next phase.':'Charts and per-sandbox / per-label breakdowns: next phase.',
  'app.name': 'Sandbox Cloud', 'app.switch.console': 'Sandbox console', 'app.switch.agent': 'Agent workbench',
  'nav.overview': 'Overview', 'nav.sandboxes': 'Sandboxes', 'nav.templates': 'Templates and images', 'nav.volumes': 'Volumes', 'nav.network': 'Network', 'nav.usage': 'Usage and billing', 'nav.members': 'Members and keys',
  'nav.audit': 'Audit', 'nav.settings': 'Project settings', 'nav.nodes': 'Nodes and capacity', 'nav.ops': 'Operations', 'nav.new': 'New sandbox', 'nav.manage': 'Manage', 'nav.collapse': 'Collapse / expand sidebar', 'nav.nodes.count': 'Nodes {alive}/{total}', 'nav.unreachable': 'Control plane unreachable',
  'role.operator': 'Operator', 'role.owner': 'Owner', 'role.member': 'Member',
  'top.project': 'Project', 'top.search': 'Search', 'top.logout': 'Sign out', 'top.lang': '中文',
  'login.title': 'Sign in to the console', 'login.hint': 'Paste your token. A project owner gives it to you once.', 'login.token': 'Token', 'login.go': 'Sign in', 'login.bad': 'That token is wrong or expired.',
  'common.loading': 'Loading', 'common.empty': 'Nothing here', 'common.all': 'All', 'common.refresh': 'Refresh', 'common.cancel': 'Cancel', 'common.confirm': 'Confirm', 'common.delete': 'Delete', 'common.copy': 'Copy', 'common.copied': 'Copied',
  'common.none': 'None', 'common.error': 'Something went wrong', 'common.retry': 'Retry', 'common.pause': 'Pause', 'common.resume': 'Resume', 'common.back': 'Back',
  'status.running': 'Running', 'status.paused': 'Paused', 'status.unreachable': 'Unreachable', 'status.failed': 'Failed', 'status.creating': 'Creating',
  'ov.spend': 'Spend today', 'ov.running': 'Running sandboxes', 'ov.capacity': 'Capacity', 'ov.pending': 'Waiting for your approval', 'ov.recent': 'Recent activity', 'ov.sandboxes': 'Sandboxes', 'ov.nodes': 'Nodes online',
  'ov.budget': 'Daily budget', 'ov.quota': 'Quota', 'ov.paused': 'paused', 'ov.noBudget': 'No budget set',
  'ov.start.title': 'Create your first sandbox', 'ov.start.body': 'Three steps: get a token, install the SDK, run some code. The snippet below already has this control plane and project filled in.', 'ov.start.install': 'Install', 'ov.start.run': 'Run',
  'sb.title': 'Sandboxes', 'sb.search': 'Search id, template, node, label', 'sb.filter.status': 'Status', 'sb.filter.class': 'Class', 'sb.col.id': 'ID', 'sb.col.status': 'Status', 'sb.col.template': 'Template', 'sb.col.class': 'Class',
  'sb.col.node': 'Node', 'sb.col.mem': 'Memory', 'sb.col.age': 'Age', 'sb.col.project': 'Project', 'sb.selected': 'selected', 'sb.none': 'No sandboxes match', 'sb.deleteConfirm': 'Delete these sandboxes? This cannot be undone.',
  'sb.tab.overview': 'Overview', 'sb.tab.metrics': 'Metrics', 'sb.tab.terminal': 'Terminal', 'sb.tab.network': 'Network', 'sb.tab.checkpoints': 'Checkpoints', 'sb.tab.ports': 'Ports and volumes', 'sb.tab.events': 'Events',
  'sb.equiv': 'Equivalent call', 'sb.rollback.warn': 'Rolling back drops every change after this checkpoint; running processes return to that moment.', 'sb.decisions': 'Recent egress decisions', 'sb.allowed': 'Allowed', 'sb.denied': 'Denied',
  'sb.metrics.soon': 'Per-5-second CPU, memory, disk and egress charts arrive in the next phase. Current readings are below.',
  'net.queue': 'Egress relaxations waiting for approval', 'net.approve': 'Approve', 'net.deny': 'Deny', 'net.none': 'Nothing is waiting for approval.',
  'members.title': 'Members', 'members.rotate': 'Rotate token', 'members.rotated': 'New token (shown once)',
  'audit.title': 'Audit', 'audit.verify': 'Verify hash chain', 'ops.title': 'Nodes and capacity', 'ops.cordon': 'Stop new work', 'ops.uncordon': 'Accept new work', 'ops.drain': 'Drain',
  'soon.title': 'This page is still moving', 'soon.body': 'It lives in the old console; nothing is missing.', 'soon.open': 'Open the old console',
}
const DICT: Record<Lang, Record<string, string>> = { zh, en }

export function translate(lang: Lang, key: string, vars?: Record<string, string | number>) {
  let s = DICT[lang][key] ?? zh[key] ?? key
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, String(v))
  return s
}

const Ctx = createContext<{ lang: Lang; setLang: (l: Lang) => void; t: (k: string, v?: Record<string, string | number>) => string }>({ lang: 'zh', setLang: () => {}, t: k => k })
export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>('zh')
  useEffect(() => { const l = getLang(); if (l === 'en' || l === 'zh') setLangState(l) }, [])
  const setLang = (l: Lang) => { setLangState(l); setLangKey(l); document.documentElement.lang = l === 'zh' ? 'zh-CN' : 'en' }
  return <Ctx.Provider value={{ lang, setLang, t: (k, v) => translate(lang, k, v) }}>{children}</Ctx.Provider>
}
// 页面里直接写的英文词(表头、面板标题):字典里有 w.<词> 就换,没有原样显示
export function word(lang: Lang, x: unknown) {
  return typeof x === 'string' && x ? (DICT[lang]['w.' + x] ?? (lang === 'zh' ? zh['w.' + x] : undefined) ?? x) : x
}
export const useI18n = () => useContext(Ctx)
