/* fc 工作台 — vanilla JS, hash routing, no build step. API paths are relative to origin. */
(() => {
'use strict';

// ------------------------------------------------------------------ utils
const app = document.getElementById('app');
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k in el && k !== 'style') el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
function toast(msg, bad) {
  const box = document.getElementById('toast');
  const d = h('div', { class: bad ? 'bad' : '' }, msg);
  box.appendChild(d);
  setTimeout(() => d.remove(), bad ? 6000 : 2500);
}
function ts(v) { // seconds-epoch | ms-epoch | ISO
  if (v == null) return null;
  if (typeof v === 'number') return v > 1e12 ? v : v * 1000;
  const t = Date.parse(v); return isNaN(t) ? null : t;
}
function age(v) {
  const t = ts(v); if (!t) return '-';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return (s / 3600).toFixed(1) + 'h';
  return (s / 86400).toFixed(1) + 'd';
}
function when(v) { const t = ts(v); return t ? new Date(t).toLocaleString() : '-'; }
function num(v, d = 3) { return (v == null || isNaN(v)) ? '-' : Number(v).toFixed(d); }
function badge(s) { return h('span', { class: 'badge ' + (s || '') }, s || '-'); }
function kv(obj, keys) {
  const dl = h('dl', { class: 'kv' });
  for (const k of keys || Object.keys(obj)) {
    let v = obj[k];
    if (v === undefined) continue;
    if (Array.isArray(v) && v.every(x => x instanceof Node)) { dl.append(h('dt', null, k), h('dd', null, ...v.flatMap((x, i) => i ? [' ', x] : [x]))); continue; }
    if (v instanceof Node) { dl.append(h('dt', null, k), h('dd', null, v)); continue; }
    if (typeof v === 'object') v = JSON.stringify(v);
    dl.append(h('dt', null, k), h('dd', null, String(v)));
  }
  return dl;
}
function table(cols, rows, rowAttrs) {
  const t = h('table', null, h('thead', null, h('tr', null, cols.map(c => h('th', null, c)))));
  const tb = h('tbody');
  if (!rows.length) tb.append(h('tr', null, h('td', { colSpan: cols.length, class: 'empty' }, '无数据')));
  const isWrap = c => c && typeof c === 'object' && !(c instanceof Node) && 'wrap' in c;
  for (const r of rows) tb.append(h('tr', rowAttrs ? rowAttrs(r) : null, r.cells.map(c => h('td', isWrap(c) ? { class: 'wrap' } : null, isWrap(c) ? c.wrap : c))));
  t.append(tb);
  return h('div', { class: 'table-wrap' }, t);
}
function setKids(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter(k => k != null)); }
function splitList(s) { return (s || '').split(/[\n,，\s]+/).map(x => x.trim()).filter(Boolean); }
function linkTo(hash, text) { return h('a', { href: hash }, text); }

// ------------------------------------------------------------------ api
const tokenInput = document.getElementById('token');
tokenInput.value = localStorage.getItem('cp_token') || '';
tokenInput.addEventListener('change', () => {
  if (tokenInput.value) localStorage.setItem('cp_token', tokenInput.value); else localStorage.removeItem('cp_token');
  toast('Token 已保存'); health();
});
function headers(extra) {
  const hd = Object.assign({}, extra || {});
  const tk = localStorage.getItem('cp_token');
  if (tk) hd['Authorization'] = 'Bearer ' + tk;
  return hd;
}
async function api(method, path, body, opts = {}) {
  const init = { method, headers: headers(opts.headers) };
  if (body !== undefined) {
    if (opts.raw) init.body = body;
    else { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
  }
  const r = await fetch(path, init);
  if (opts.rawResponse) return r;
  const text = await r.text();
  let data; try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status} ${data.error || data.detail || text.slice(0, 200)}`);
  return data;
}
const GET = p => api('GET', p), POST = (p, b) => api('POST', p, b || {}), PUT = (p, b, o) => api('PUT', p, b, o), DEL = p => api('DELETE', p);
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}
async function health() {
  const el = document.getElementById('health');
  try {
    const hh = await GET('/health');
    const nodes = Object.entries(hh.nodes || {});
    el.textContent = `控制面 ${hh.ok ? '正常' : '异常'} · 节点 ${nodes.filter(([, n]) => n.alive).length}/${nodes.length}`;
    el.className = 'small ' + (hh.ok ? 'ok' : 'bad');
  } catch (e) { el.textContent = '控制面不可达'; el.className = 'small bad'; }
}

// ------------------------------------------------------------------ router
let leaveFns = [];
function onLeave(fn) { leaveFns.push(fn); }
function every(ms, fn) { fn(); const id = setInterval(fn, ms); onLeave(() => clearInterval(id)); }
const routes = [
  [/^#\/attention$/, pageAttention],
  [/^#\/intake$/, pageIntake],
  [/^#\/tickets\/([^/]+)$/, pageTicket],
  [/^#\/threads\/(.+)$/, pageThread],
  [/^#\/sandboxes\/([^/]+)$/, pageSandbox],
  [/^#\/sandboxes$/, pageSandboxes],
  [/^#\/nodes$/, pageNodes],
  [/^#\/experiments$/, pageExperiments],
  [/^#\/tasksets\/([^/]+)$/, pageTaskset],
  [/^#\/batches\/([^/]+)$/, pageBatch],
  [/^#\/runs\/([^/]+)$/, pageRun],
];
async function route() {
  for (const f of leaveFns.splice(0)) { try { f(); } catch {} }
  const hash = location.hash || '#/sandboxes';
  for (const a of document.querySelectorAll('[data-nav]')) a.classList.toggle('active', hash.startsWith('#/' + a.dataset.nav) || (a.dataset.nav === 'experiments' && /^#\/(tasksets|batches|runs)/.test(hash)) || (a.dataset.nav === 'intake' && /^#\/(tickets|threads)/.test(hash)));
  app.replaceChildren();
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { try { await fn(...m.slice(1).map(decodeURIComponent)); } catch (e) { app.append(h('p', { class: 'err' }, String(e.message || e))); } return; }
  }
  location.hash = '#/sandboxes';
}
window.addEventListener('hashchange', route);

// ------------------------------------------------------------------ 待处理(接单层:docs/007 的三个问题)
const STAGE_CLS = { 'person/delivery': 'failed', 'person/answer': 'queued', 'person/review': 'compare', 'person/blocked': 'failed', 'person/failed': 'failed', working: 'running', 'working/queued': 'queued', idle: '', ended: 'done' };
function stageBadge(t) { return h('span', { class: 'badge ' + (STAGE_CLS[t.stage] || '') }, t.stage_label || t.stage); }
function ticketRow(t) {
  return { t, cells: [stageBadge(t), linkTo('#/tickets/' + t.id, t.short), h('span', { class: 'wrap' }, t.title || '-'), t.source, t.actor || '-', (t.last_report || {}).state || '-',
    t.pr && t.pr.number ? h('a', { href: t.pr.url || '#', target: '_blank' }, '#' + t.pr.number) : '-', age(t.updated)] };
}
const TICKET_COLS = ['阶段', '工单', '标题', '来源', '请求者', '最近报告', 'PR', '更新'];
async function pageAttention() {
  app.append(h('h1', null, '待处理'));
  const need = h('div'), probs = h('div'), counts = h('div', { class: 'row muted small' });
  app.append(h('h2', null, '需要你'), need, h('h2', null, '系统问题'), probs, h('h2', null, '24 小时'), counts);
  const form = { text: h('textarea', { placeholder: '直接向 agent 提一个请求(workbench Source,名单内,explicit)', style: 'min-height:70px' }), budget: h('input', { size: 5, value: '1.0' }) };
  app.append(h('h2', null, '新请求'), h('div', { class: 'panel' }, form.text, h('div', { class: 'row' }, h('label', null, '预算 USD', form.budget),
    h('button', { class: 'primary', onclick: async () => { const r = await act(() => POST('/intake/requests', { text: form.text.value, budget_usd: Number(form.budget.value) || 1, actor: 'workbench' }), '已提交请求'); if (r) { form.text.value = ''; setTimeout(refresh, 1500); } } }, '提交'))));
  async function refresh() {
    try {
      const a = await GET('/intake/attention');
      setKids(need, a.needs_person.length ? table(TICKET_COLS, a.needs_person.map(ticketRow), r => ({ class: 'clickable', onclick: ev => { if (ev.target.tagName !== 'A') location.hash = '#/tickets/' + r.t.id; } }))
        : h('p', { class: 'muted' }, '现在没有需要你的事。'));
      const pl = (a.problems || []).map(p => h('li', null, h('b', null, p.name), ' ', p.error, ' ', h('span', { class: 'muted small' }, age(p.since) + ' 前')));
      (a.unavailable || []).forEach(d => pl.push(h('li', null, h('b', null, 'Eval 未完成'), ' ', linkTo('#/threads/' + encodeURIComponent(d.thread), d.thread), ' ', d.reason || '')));
      if (a.paused) pl.push(h('li', null, h('b', { class: 'warn' }, '处理已暂停'), ' ', h('button', { class: 'sm', onclick: () => act(() => POST('/intake/pause', { paused: false }), '已继续').then(refresh) }, '继续')));
      setKids(probs, pl.length ? h('ul', null, pl) : h('p', { class: 'muted' }, '系统健康。'));
      const c = a.counts || {};
      setKids(counts, h('span', null, `Trigger ${c.triggers_24h} · Decision ${c.decisions_24h}(本会接单 ${c.would_accept_24h})· 开着的工单 ${c.tickets_open} · 执行中 ${c.running}/${c.slots} · 策略 ${a.policy_version} · 模式 ${a.mode}`));
    } catch (e) { setKids(need, h('p', { class: 'err' }, '接单层不可达:' + e.message)); }
  }
  every(5000, refresh);
}

// ------------------------------------------------------------------ 接单:Source / Thread / Eval / 工单 / 策略
async function pageIntake() {
  app.append(h('h1', null, '接单'));
  const src = h('div'), tk = h('div'), th = h('div'), dec = h('div'), pol = h('div');
  app.append(h('h2', null, 'Source'), src, h('h2', null, '工单'), tk, h('h2', null, 'Thread'), th, h('h2', null, 'Decision'), dec, h('h2', null, '策略'), pol);
  async function refresh() {
    try {
      const a = await GET('/intake/attention'); const s = a.sources || {};
      setKids(src, table(['Source', '状态', '详情'], Object.entries(s).map(([k, v]) => ({ cells: [k, v.enabled ? h('span', { class: 'ok' }, '开') : h('span', { class: 'muted' }, '关'),
        k === 'gitea' ? `${v.repo || '-'} bot=${v.bot || '-'} ${v.cursor ? '轮询 ' + age(v.cursor.polled) + ' 前' : ''} ${v.error ? '错误: ' + v.error : ''}` : '工作台直接请求'] }))));
      const t = await GET('/intake/tickets');
      setKids(tk, table(TICKET_COLS, (t.tickets || []).map(ticketRow), r => ({ class: 'clickable', onclick: ev => { if (ev.target.tagName !== 'A') location.hash = '#/tickets/' + r.t.id; } })));
      const ths = await GET('/intake/threads');
      setKids(th, table(['Thread', 'Source', '标题', '订阅', '待评估', '更新'], (ths.threads || []).slice(0, 50).map(x => ({ cells: [linkTo('#/threads/' + encodeURIComponent(x.id), x.id), x.source, x.title || '-', (x.subscriptions || []).length, x.eval ? (x.eval.triggers || []).length : 0, age(x.updated)] }))));
      const d = await GET('/intake/decisions');
      setKids(dec, table(['时间', 'Thread', '结果', '强度', '依据', '理由', '规则', '工单'], (d.decisions || []).slice(0, 50).map(x => ({ cells: [when(x.created), linkTo('#/threads/' + encodeURIComponent(x.thread), x.thread), badge(x.outcome), x.strength || '-', x.basis || '-', h('span', { class: 'wrap' }, (x.would_accept ? '[本会接单] ' : '') + (x.reason || '')), (x.matched_rules || []).join(' ') || '-', x.ticket ? linkTo('#/tickets/' + x.ticket, x.ticket.slice(2, 10)) : '-'] }))));
    } catch (e) { setKids(tk, h('p', { class: 'err' }, e.message)); }
  }
  async function loadPolicy() {
    const p = await GET('/intake/policy');
    const mode = h('select', null, ...['off', 'observe', 'auto'].map(m => h('option', { value: m, selected: m === p.mode }, m)));
    const rules = h('textarea', { style: 'min-height:120px' }, (p.rules || []).map(r => `${r.enabled === false ? '#' : ''}${r.name}: ${r.text}`).join('\n'));
    const allowed = h('input', { size: 40, value: ((p.sources || {}).gitea || {}).allowed?.join(', ') || '' });
    const bots = h('input', { size: 30, value: ((p.sources || {}).gitea || {}).bots?.join(', ') || '' });
    const save = async () => {
      const rs = rules.value.split('\n').map(l => l.trim()).filter(Boolean).map(l => { const en = !l.startsWith('#'); l = l.replace(/^#/, ''); const i = l.indexOf(':'); return { name: l.slice(0, i).trim(), text: l.slice(i + 1).trim(), enabled: en }; });
      const r = await act(() => PUT('/intake/policy', { mode: mode.value, rules: rs, sources: { gitea: { allowed: splitList(allowed.value), bots: splitList(bots.value) } } }), '策略已保存');
      if (r) { toast('版本 ' + r.version); refresh(); }
    };
    setKids(pol, h('div', { class: 'panel' }, h('div', { class: 'row' }, h('label', null, '版本', h('span', { class: 'mono' }, p.version)), h('label', null, '接受模式', mode), h('label', null, 'gitea 白名单', allowed), h('label', null, 'gitea bot 名单', bots)),
      h('label', { class: 'small muted' }, '规则(每行 名字: 正文;行首 # 表示停用)'), rules, h('div', { class: 'row' }, h('button', { class: 'primary', onclick: save }, '保存为新版本'))));
  }
  every(8000, refresh); loadPolicy().catch(e => setKids(pol, h('p', { class: 'err' }, e.message)));
}

async function pageTicket(id) {
  const head = h('div'), body = h('div'); app.append(head, body);
  async function load() {
    const t = await GET('/intake/tickets/' + id);
    setKids(head, h('h1', null, '工单 ', h('span', { class: 'mono' }, t.short), ' ', stageBadge(t)), h('p', { class: 'wrap' }, t.title));
    const m = t.mandate || {};
    const actions = h('div', { class: 'row' });
    if (t.state === 'open') {
      actions.append(h('button', { class: 'danger', onclick: () => act(() => POST('/intake/tickets/' + id + '/stop', { reason: 'panel' }), '已停止').then(load) }, '停止'));
      if (!t.running) actions.append(h('button', { onclick: () => act(() => POST('/intake/tickets/' + id + '/retry'), '已重新派工').then(load) }, '重试'));
    }
    const ans = h('input', { size: 60, placeholder: t.attention ? '回答:' + t.attention.question : '给执行者一条消息' });
    actions.append(ans, h('button', { onclick: async () => { const r = await act(() => POST('/intake/tickets/' + id + '/reply', { text: ans.value, actor: 'panel' }), '已送达'); if (r) { ans.value = ''; load(); } } }, '发送'));
    setKids(body,
      h('div', { class: 'panel' }, kv({ 状态: t.state + (t.ended_by ? ' / ' + t.ended_by : '') + (t.outcome ? ' / ' + t.outcome : ''), Thread: linkTo('#/threads/' + encodeURIComponent(t.thread), t.thread), 分支: t.branch, PR: t.pr && t.pr.number ? h('a', { href: t.pr.url || '#', target: '_blank' }, '#' + t.pr.number + ' ' + (t.pr.state || '')) : '-',
        授权: `${m.strength} · ${m.basis} · 请求者 ${m.actor} · 预算 ${m.budget_usd} USD · 网络 ${m.network} · 策略 ${m.policy_version}` + ((m.matched_rules || []).length ? ' · 规则 ' + m.matched_rules.join(' ') : ''), 工作区层: t.workspace_layer || '-', 最近报告: t.last_report ? `${t.last_report.state}: ${t.last_report.text || ''}` : '-', 待回答: t.attention ? t.attention.question : '-' }, ['状态', 'Thread', '分支', 'PR', '授权', '工作区层', '最近报告', '待回答']), actions),
      h('h2', null, '输入'), table(['时间', '类型', '来自', '内容', '已送达'], (t.inputs || []).map(i => ({ cells: [when(i.ts), i.kind, i.actor || '-', h('span', { class: 'wrap' }, (i.text || '').slice(0, 300)), i.delivered ? '✓' : '-'] }))),
      h('h2', null, '派工'), table(['ID', '状态', '沙箱', '节点', '报告', '退出码', '费用', 'token', '耗时', '错误', '恢复'], (t.assignments || []).map(a => ({ cells: [a.id.slice(2, 10), badge(a.state), a.sandbox || '-', a.node || '-', (a.reports || []).map(r => r.state).join(' → ') || '-', a.agent_exit ?? '-', num(a.cost, 4), `${a.tokens_in || 0}/${a.tokens_out || 0}`, a.elapsed_s ? num(a.elapsed_s, 1) + 's' : '-', h('span', { class: 'wrap' }, a.error || ''), a.recovery ? '是' : '-'] }))),
      h('h2', null, '回帖'), table(['时间', '用途', '状态', '内容'], (t.replies || []).map(r => ({ cells: [when(r.created), r.purpose, badge(r.state), h('span', { class: 'wrap' }, r.text)] }))));
  }
  every(5000, () => load().catch(e => setKids(body, h('p', { class: 'err' }, e.message))));
}

async function pageThread(id) {
  const box = h('div'); app.append(h('h1', null, 'Thread ', h('span', { class: 'mono' }, id)), box);
  async function load() {
    const t = await GET('/intake/threads/' + encodeURIComponent(id));
    setKids(box, h('div', { class: 'panel' }, kv({ Source: t.source, 标题: t.title, 订阅: (t.subscriptions || []).map(x => linkTo('#/tickets/' + x, x.slice(2, 10))), 待评估: t.eval ? (t.eval.triggers || []).length : 0 }, ['Source', '标题', '订阅', '待评估'])),
      h('h2', null, '工单'), table(TICKET_COLS, (t.tickets || []).map(ticketRow)),
      h('h2', null, 'Trigger'), table(['时间', '来自', '点名', '范围', '内容', '去向', ''], (t.triggers || []).map(x => ({ cells: [when(x.created), x.actor + (x.human ? '' : ' (bot)'), x.addressed ? '是' : '-', x.scope || '-', h('span', { class: 'wrap' }, (x.text || '').slice(0, 300)), x.disposition || '-',
        h('button', { class: 'sm', onclick: () => act(() => POST('/intake/triggers/' + x.id + '/reevaluate'), '已重新排队').then(load) }, '重新评估')] }))),
      h('h2', null, 'Decision'), table(['时间', '结果', '强度', '依据', '理由', '规则', '工单'], (t.decisions || []).map(x => ({ cells: [when(x.created), badge(x.outcome), x.strength || '-', x.basis || '-', h('span', { class: 'wrap' }, (x.would_accept ? '[本会接单] ' : '') + (x.reason || '')), (x.matched_rules || []).join(' ') || '-', x.ticket ? linkTo('#/tickets/' + x.ticket, x.ticket.slice(2, 10)) : '-'] }))));
  }
  every(8000, () => load().catch(e => setKids(box, h('p', { class: 'err' }, e.message))));
}

// ------------------------------------------------------------------ 沙箱列表
async function pageSandboxes() {
  const f = {
    template: h('input', { value: 'ubuntu-24.04', size: 14 }),
    cls: h('select', null, h('option', { value: 'persistent' }, 'persistent'), h('option', { value: 'ephemeral' }, 'ephemeral')),
    vcpu: h('input', { type: 'number', value: 2, min: 1, style: 'width:60px' }),
    mem: h('input', { type: 'number', value: 2048, min: 128, step: 128, style: 'width:80px' }),
    layers: h('input', { placeholder: 'L-xxx, L-yyy', size: 18 }),
    hosts: h('input', { placeholder: 'fc-model, pypi.org', size: 22 }),
    boot: h('select', null, ['auto', 'cold', 'snapshot'].map(b => h('option', { value: b }, b))),
  };
  const form = h('form', { class: 'row', onsubmit: async ev => {
    ev.preventDefault();
    const body = { template: f.template.value, class: f.cls.value, vcpu: +f.vcpu.value, mem: +f.mem.value, boot: f.boot.value };
    const layers = splitList(f.layers.value); if (layers.length) body.layers = layers;
    const hosts = splitList(f.hosts.value); if (hosts.length) body.network = { allow_hosts: hosts, allow_direct: [] };
    const info = await act(() => POST('/sandboxes', body), '已创建');
    if (info && info.id) { refresh(); }
  } },
    h('label', null, '模板', f.template), h('label', null, '类型', f.cls), h('label', null, 'vCPU', f.vcpu), h('label', null, '内存 MiB', f.mem),
    h('label', null, '层', f.layers), h('label', null, 'allow_hosts', f.hosts), h('label', null, '启动', f.boot),
    h('button', { class: 'primary', type: 'submit' }, '创建沙箱'));
  const list = h('div');
  app.append(h('h1', null, '沙箱'), h('div', { class: 'panel' }, form), list);

  async function refresh() {
    let data;
    try { data = await GET('/sandboxes'); } catch (e) { list.replaceChildren(h('p', { class: 'err' }, e.message)); return; }
    const rows = (data.sandboxes || []).map(s => ({ cells: [
      linkTo('#/sandboxes/' + s.id, s.id), badge(s.status), s.class, s.template, s.node || '-',
      `${s.vcpu} / ${s.mem}`, age(s.created), s.boot_mode || '-', s.durable ? '是' : '否',
      h('span', { class: 'row' },
        s.status === 'running' ? h('button', { class: 'sm', onclick: () => act(() => POST(`/sandboxes/${s.id}/pause`), '已暂停').then(refresh) }, '暂停') : null,
        s.status === 'paused' ? h('button', { class: 'sm', onclick: () => act(() => POST(`/sandboxes/${s.id}/resume`), '已恢复').then(refresh) }, '恢复') : null,
        h('button', { class: 'sm', onclick: () => { const n = parseInt(prompt('fork 数量', '1') || '0'); if (n > 0) act(() => POST(`/sandboxes/${s.id}/fork`, { n }), `已 fork ${n} 个`).then(refresh); } }, 'fork'),
        h('button', { class: 'sm', onclick: () => location.hash = '#/sandboxes/' + s.id }, '打开'),
        h('button', { class: 'sm danger', onclick: () => confirm(`删除 ${s.id}?`) && act(() => DEL(`/sandboxes/${s.id}`), '已删除').then(refresh) }, '删除')),
    ] }));
    list.replaceChildren(table(['ID', '状态', '类型', '模板', '节点', 'vCPU / 内存', '创建', '启动方式', '持久', '操作'], rows));
  }
  every(5000, refresh);
}

// ------------------------------------------------------------------ 沙箱详情
async function pageSandbox(id) {
  const base = `/sandboxes/${id}`;
  const info = await GET(base);
  const head = h('div', { class: 'panel' });
  const tabs = h('div', { class: 'tabs' });
  const body = h('div');
  app.append(h('h1', null, '沙箱 ', h('span', { class: 'mono' }, id), ' ', badge(info.status)), head, tabs, body);

  function renderHead(s) {
    head.replaceChildren(
      kv(Object.assign({}, s, { created: when(s.created) + ' (' + age(s.created) + ')' }), ['status', 'class', 'template', 'node', 'vcpu', 'mem', 'boot_mode', 'durable', 'ttl_idle_s', 'ttl_max_s', 'created', 'layers', 'network']),
      h('div', { class: 'row', style: 'margin-top:10px' },
        h('button', { onclick: () => act(() => POST(base + '/pause'), '已暂停').then(renderHead) }, '暂停'),
        h('button', { onclick: () => act(() => POST(base + '/resume'), '已恢复').then(renderHead) }, '恢复'),
        h('button', { onclick: () => { const n = parseInt(prompt('fork 数量', '1') || '0'); if (n > 0) act(() => POST(base + '/fork', { n }), '已 fork').then(r => toast('新沙箱: ' + r.clones.map(c => c.id).join(', '))); } }, 'fork'),
        h('button', { onclick: () => { const name = prompt('层名称'); if (name) act(() => POST(base + '/pack', { name }), '已打包').then(l => toast(`层 ${l.id} (${l.name}, ${l.bytes} B)`)); } }, '打包成层'),
        h('button', { class: 'danger', onclick: () => confirm('删除该沙箱?') && act(() => DEL(base), '已删除').then(() => location.hash = '#/sandboxes') }, '删除')));
  }
  renderHead(info);

  const panels = { 执行: execPanel, 终端: termPanel, 文件: filesPanel, 网络: netPanel };
  let cur = null, curCleanup = null;
  function show(name) {
    if (curCleanup) { try { curCleanup(); } catch {} curCleanup = null; }
    cur = name;
    for (const b of tabs.children) b.classList.toggle('active', b.textContent === name);
    body.replaceChildren();
    curCleanup = panels[name](body) || null;
  }
  for (const name of Object.keys(panels)) tabs.append(h('button', { onclick: () => show(name) }, name));
  onLeave(() => curCleanup && curCleanup());
  show('执行');

  // ---- 执行面板: /exec/stream (NDJSON) with fallback to /exec
  function execPanel(root) {
    const cmd = h('input', { placeholder: 'ls -la /task', style: 'flex:1;min-width:300px', class: 'mono' });
    const user = h('select', null, h('option', { value: 'agent' }, 'agent'), h('option', { value: 'root' }, 'root'));
    const timeout = h('input', { type: 'number', value: 60, min: 1, style: 'width:70px' });
    const out = h('pre', { class: 'out' });
    const run = h('button', { class: 'primary', type: 'submit' }, '运行');
    let abort = null;
    const form = h('form', { class: 'row', onsubmit: async ev => {
      ev.preventDefault();
      if (!cmd.value.trim()) return;
      run.disabled = true; out.replaceChildren();
      const req = { cmd: cmd.value, user: user.value, timeout: +timeout.value };
      const t0 = Date.now();
      try {
        await streamExec(req);
      } catch (e) {
        out.append(h('span', { class: 'meta' }, `[stream 失败: ${e.message},回退到 /exec]\n`));
        try {
          const r = await POST(base + '/exec', req);
          if (r.stdout) out.append(h('span', null, r.stdout));
          if (r.stderr) out.append(h('span', { class: 'stderr' }, r.stderr));
          out.append(h('span', { class: 'meta' }, `\n[exit=${r.exit} timeout=${r.timeout} elapsed=${r.elapsed_s}s]`));
        } catch (e2) { out.append(h('span', { class: 'stderr' }, e2.message)); }
      } finally { run.disabled = false; abort = null; }
    } }, cmd, h('label', null, '用户', user), h('label', null, '超时 s', timeout), run,
      h('button', { type: 'button', onclick: () => abort && abort.abort() }, '中止'));
    root.append(h('div', { class: 'panel' }, form, out));
    async function streamExec(req) {
      abort = new AbortController();
      const r = await fetch(base + '/exec/stream', { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(req), signal: abort.signal });
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = '', done = false;
      const handle = line => {
        line = line.trim(); if (!line) return;
        let m; try { m = JSON.parse(line); } catch { out.append(h('span', { class: 'meta' }, line + '\n')); return; }
        if (m.done) { done = true; out.append(h('span', { class: 'meta' }, `\n[exit=${m.exit} timeout=${!!m.timeout} elapsed=${m.elapsed_s}s]`)); }
        else if (m.event === 'stderr') out.append(h('span', { class: 'stderr' }, m.data));
        else out.append(h('span', null, m.data == null ? '' : m.data));
        out.scrollTop = out.scrollHeight;
      };
      while (true) {
        const { value, done: eof } = await reader.read();
        if (eof) break;
        buf += dec.decode(value, { stream: true });
        let i; while ((i = buf.indexOf('\n')) >= 0) { handle(buf.slice(0, i)); buf = buf.slice(i + 1); }
      }
      if (buf.trim()) handle(buf);
      if (!done) out.append(h('span', { class: 'meta' }, '\n[流结束,未收到 done]'));
    }
    cmd.focus();
  }

  // ---- 终端面板: sessions {pty:true} + write/read polling
  function termPanel(root) {
    const pre = h('pre', { class: 'out term' });
    const input = h('input', { placeholder: '输入命令后回车', class: 'mono', disabled: true });
    const status = h('span', { class: 'muted small' }, '未连接');
    let session = null, timer = null, busy = false;
    const openBtn = h('button', { class: 'primary', onclick: open }, '打开终端');
    const closeBtn = h('button', { onclick: close, disabled: true }, '关闭');
    root.append(h('div', { class: 'panel' }, h('div', { class: 'row' }, openBtn, closeBtn, status), pre,
      h('div', { class: 'term-in' }, input, h('button', { onclick: send }, '发送'), h('button', { onclick: () => write('\u0003') }, 'Ctrl-C'))));
    input.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); send(); } });
    async function open() {
      try {
        const r = await POST(base + '/sessions', { pty: true, user: 'agent' });
        session = r.session; status.textContent = `会话 ${session}${r.pty ? ' (pty)' : ''}`;
        openBtn.disabled = true; closeBtn.disabled = false; input.disabled = false; input.focus();
        timer = setInterval(poll, 400);
      } catch (e) { toast(e.message, true); }
    }
    async function poll() {
      if (!session || busy) return; busy = true;
      try {
        const r = await POST(`${base}/sessions/${session}/read`, { wait: 0.3 });
        if (r.data_b64) { const text = b64decode(r.data_b64); if (text) { pre.append(stripAnsi(text)); pre.scrollTop = pre.scrollHeight; } }
      } catch (e) { status.textContent = '读取失败: ' + e.message; }
      finally { busy = false; }
    }
    async function write(data) {
      if (!session) return;
      try { await POST(`${base}/sessions/${session}/write`, { data_b64: b64encode(data) }); } catch (e) { toast(e.message, true); }
    }
    function send() { if (!session) return; const t = input.value; input.value = ''; write(t + '\n'); }
    async function close() {
      if (timer) clearInterval(timer); timer = null;
      if (session) { const s = session; session = null; try { await DEL(`${base}/sessions/${s}`); } catch {} }
      status.textContent = '已关闭'; openBtn.disabled = false; closeBtn.disabled = true; input.disabled = true;
    }
    return close;
  }

  // ---- 文件面板
  function filesPanel(root) {
    const path = h('input', { value: '/task/README.md', class: 'mono', style: 'flex:1;min-width:300px' });
    const ta = h('textarea');
    const info = h('span', { class: 'muted small' });
    const save = h('button', { onclick: async () => {
      const r = await act(() => PUT(`${base}/files?path=${encodeURIComponent(path.value)}`, ta.value, { raw: true, headers: { 'Content-Type': 'application/octet-stream' } }), '已保存');
      if (r) info.textContent = `已写入 ${r.bytes} 字节`;
    } }, '保存');
    async function load() {
      try {
        const r = await api('GET', `${base}/files?path=${encodeURIComponent(path.value)}`, undefined, { rawResponse: true });
        if (r.status === 404) { ta.value = ''; ta.disabled = false; save.disabled = false; info.textContent = '文件不存在,保存即新建'; return; }
        if (!r.ok) throw new Error(`读取失败 ${r.status}`);
        const buf = new Uint8Array(await r.arrayBuffer());
        if (isBinary(buf)) { ta.value = ''; ta.disabled = true; save.disabled = true; info.textContent = `二进制文件,${buf.length} 字节,不显示`; }
        else { ta.value = new TextDecoder().decode(buf); ta.disabled = false; save.disabled = false; info.textContent = `${buf.length} 字节`; }
      } catch (e) { toast(e.message, true); }
    }
    root.append(h('div', { class: 'panel' }, h('div', { class: 'row' }, path, h('button', { onclick: load }, '读取'), save, info), ta));
    path.addEventListener('keydown', ev => { if (ev.key === 'Enter') load(); });
    load();
  }

  // ---- 网络面板
  function netPanel(root) {
    const s = info;
    {
      const net = s.network || {};
      const hosts = h('textarea', { style: 'min-height:120px', value: (net.allow_hosts || []).join('\n') });
      const direct = h('textarea', { style: 'min-height:60px', value: (net.allow_direct || []).join('\n') });
      root.append(h('div', { class: 'panel' },
        h('p', { class: 'muted small' }, '每行一个 glob;出口代理按 allow_hosts 放行。'),
        h('div', { class: 'grid2' }, h('label', null, 'allow_hosts', hosts), h('label', null, 'allow_direct', direct)),
        h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'primary', onclick: async () => {
          const r = await act(() => PUT(base + '/network', { allow_hosts: splitList(hosts.value), allow_direct: splitList(direct.value) }), '网络策略已更新');
          if (r) { s.network = r.network; renderHead(s); }
        } }, '应用'))));
    }
  }
}
function b64encode(s) { return btoa(String.fromCharCode(...new TextEncoder().encode(s))); }
function b64decode(b) { const bin = atob(b); const u = new Uint8Array(bin.length); for (let i = 0; i < u.length; i++) u[i] = bin.charCodeAt(i); return new TextDecoder().decode(u); }
function stripAnsi(s) { return s.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '').replace(/\x1b\][^\x07]*\x07/g, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n'); }
function isBinary(u) { const n = Math.min(u.length, 4096); let bad = 0; for (let i = 0; i < n; i++) { const c = u[i]; if (c === 0) return true; if (c < 7 || (c > 13 && c < 32)) bad++; } return n > 0 && bad / n > 0.1; }

// ------------------------------------------------------------------ 节点
async function pageNodes() {
  const list = h('div');
  app.append(h('h1', null, '节点'), list);
  every(5000, async () => {
    let d; try { d = await GET('/nodes'); } catch (e) { list.replaceChildren(h('p', { class: 'err' }, e.message)); return; }
    const rows = (d.nodes || []).map(n => { const hh = n.health || {}; return { cells: [
      n.name, h('span', { class: n.alive ? 'ok' : 'bad' }, n.alive ? '在线' : '离线'), n.url || '-',
      `${hh.vms_running ?? '-'} / ${hh.max_vms ?? '-'}`, `${hh.mem_mib_running ?? '-'} / ${hh.mem_budget_mib ?? '-'}`,
      Array.isArray(hh.template_snapshots) ? hh.template_snapshots.length : (hh.template_snapshots ?? '-'),
      { wrap: hh.pools ? Object.entries(hh.pools).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join('; ') || '-' : '-' },
    ] }; });
    list.replaceChildren(table(['名称', '状态', 'URL', '运行 VM / 上限', '内存 MiB 运行 / 预算', '模板快照', '池'], rows));
  });
}

// ------------------------------------------------------------------ 实验
async function pageExperiments() {
  app.append(h('h1', null, '实验'));
  // 任务集
  const tsBox = h('div');
  app.append(h('h2', null, '任务集'), tsBox);
  (async () => {
    try {
      const d = await GET('/eval/tasksets');
      tsBox.replaceChildren(table(['名称', '版本', '任务数', '分组', '已检查'], (d.tasksets || []).map(t => ({ t, cells: [
        linkTo('#/tasksets/' + encodeURIComponent(t.name), t.name), t.version || '-', t.n_tasks ?? '-',
        Object.entries(t.groups || {}).map(([k, v]) => `${k}:${v}`).join(' '), t.checked ? h('span', { class: 'ok' }, '是') : h('span', { class: 'warn' }, '否'),
      ] })), r => ({ class: 'clickable', onclick: ev => { if (ev.target.tagName !== 'A') location.hash = '#/tasksets/' + encodeURIComponent(r.t.name); } })));
    } catch (e) { tsBox.replaceChildren(h('p', { class: 'err' }, e.message)); }
  })();

  // 版本
  const vBox = h('div');
  const vf = { label: h('input', { size: 14 }), layers: h('input', { placeholder: 'L-xxx, L-yyy', size: 18 }), agent_cmd: h('input', { value: 'python3 -m aios.run', size: 22, class: 'mono' }), note: h('input', { size: 20 }), parent: h('select', null, h('option', { value: '' }, '(无)')) };
  const vForm = h('form', { class: 'row', onsubmit: async ev => {
    ev.preventDefault();
    const body = { label: vf.label.value, layers: splitList(vf.layers.value), agent_cmd: vf.agent_cmd.value, note: vf.note.value };
    if (vf.parent.value) body.parent = vf.parent.value;
    const r = await act(() => POST('/eval/versions', body), '已添加版本');
    if (r) { toast('版本 ' + r.id); vf.label.value = ''; vf.layers.value = ''; vf.note.value = ''; loadVersions(); }
  } }, h('label', null, '标签', vf.label), h('label', null, '层', vf.layers), h('label', null, 'agent_cmd', vf.agent_cmd), h('label', null, '备注', vf.note), h('label', null, '父版本', vf.parent), h('button', { type: 'submit', class: 'primary' }, '添加版本'));
  app.append(h('h2', null, '版本'), h('div', { class: 'panel' }, vForm), vBox);
  let versions = [];
  const versionSelects = [];
  async function loadVersions() {
    try {
      versions = (await GET('/eval/versions')).versions || [];
      vBox.replaceChildren(table(['ID', '标签', '父版本', '层', 'agent_cmd', '备注', '创建'], versions.map(v => ({ cells: [
        h('span', { class: 'mono' }, v.id), v.label || '-', v.parent || '-', { wrap: (v.layers || []).join(', ') || '-' }, h('span', { class: 'mono' }, v.agent_cmd || '-'), { wrap: v.note || '' }, when(v.created)] }))));
      for (const sel of [vf.parent, ...versionSelects]) {
        const keep = sel.value;
        setKids(sel, sel === vf.parent ? h('option', { value: '' }, '(无)') : null, versions.map(v => h('option', { value: v.id }, `${v.id} ${v.label || ''}`)));
        if (keep) sel.value = keep;
      }
    } catch (e) { vBox.replaceChildren(h('p', { class: 'err' }, e.message)); }
  }

  // 批次
  const bBox = h('div');
  const tsSel1 = h('select'), tsSel2 = h('select'), verSel1 = h('select'), verSel2 = h('select'), baseSel = h('select');
  versionSelects.push(verSel1, verSel2);
  const k1 = h('input', { type: 'number', value: 5, min: 1, style: 'width:60px' }), k2 = h('input', { type: 'number', value: 5, min: 1, style: 'width:60px' });
  const cascade = h('input', { type: 'checkbox', checked: true });
  const baseForm = h('form', { class: 'row', onsubmit: async ev => {
    ev.preventDefault();
    const r = await act(() => POST('/eval/batches', { kind: 'baseline', taskset: tsSel1.value, version: verSel1.value, k: +k1.value }), '基线已提交');
    if (r) { toast('批次 ' + r.batch_id); loadBatches(); }
  } }, h('b', null, '跑基线'), h('label', null, '任务集', tsSel1), h('label', null, '版本', verSel1), h('label', null, 'K', k1), h('button', { type: 'submit', class: 'primary' }, '提交'));
  const cmpForm = h('form', { class: 'row', onsubmit: async ev => {
    ev.preventDefault();
    if (!baseSel.value) return toast('需要先有已完成的基线', true);
    const r = await act(() => POST('/eval/batches', { kind: 'compare', taskset: tsSel2.value, base_batch: baseSel.value, version: verSel2.value, k: +k2.value, cascade: cascade.checked }), '比较已提交');
    if (r) { toast('批次 ' + r.batch_id); loadBatches(); }
  } }, h('b', null, '比较候选'), h('label', null, '任务集', tsSel2), h('label', null, '基线批次', baseSel), h('label', null, '候选版本', verSel2), h('label', null, 'K', k2), h('label', null, '级联', cascade), h('button', { type: 'submit', class: 'primary' }, '提交'));
  app.append(h('h2', null, '批次'), h('div', { class: 'panel' }, baseForm, h('hr', { style: 'border:0;border-top:1px solid var(--line);margin:8px 0' }), cmpForm), bBox);
  GET('/eval/tasksets').then(d => { for (const sel of [tsSel1, tsSel2]) setKids(sel, (d.tasksets || []).map(t => h('option', { value: t.name }, t.name))); }).catch(() => {});
  async function loadBatches() {
    try {
      const d = await GET('/eval/batches'); const batches = d.batches || [];
      const keep = baseSel.value;
      setKids(baseSel, batches.filter(b => b.kind === 'baseline' && b.status === 'done').map(b => h('option', { value: b.id }, `${b.id} ${b.version} ${b.taskset}`)));
      if (keep) baseSel.value = keep;
      bBox.replaceChildren(table(['ID', '类型', '任务集', '版本', 'K', '标签', '状态', '进度 done/total (infra)', 'mean', 'δ', 'diff', 'accept', '创建', ''], batches.map(b => { const p = b.progress || {}, s = b.summary || {}; return { cells: [
        linkTo('#/batches/' + b.id, b.id), badge(b.kind), b.taskset, b.version, b.k, b.label || '', badge(b.status),
        `${p.done ?? 0}/${p.total ?? 0} (${p.infra_fail ?? 0})`, num(s.mean), num(s.delta), num(s.diff), s.accept == null ? '-' : (s.accept ? h('span', { class: 'ok' }, '✓') : h('span', { class: 'bad' }, '✗')), when(b.created),
        (b.status === 'queued' || b.status === 'running') ? h('button', { class: 'sm danger', onclick: () => confirm('取消批次?') && act(() => DEL('/eval/batches/' + b.id), '已取消').then(loadBatches) }, '取消') : null,
      ] }; })));
    } catch (e) { bBox.replaceChildren(h('p', { class: 'err' }, e.message)); }
  }
  await loadVersions();
  every(5000, loadBatches);
}

// ------------------------------------------------------------------ 任务集详情
async function pageTaskset(name) {
  const box = h('div');
  const checkBtn = h('button', { class: 'primary', onclick: async () => {
    const r = await act(() => POST(`/eval/tasksets/${encodeURIComponent(name)}/check`, {}), '导入检查已提交');
    if (r) { toast('批次 ' + r.batch_id); setTimeout(load, 1500); }
  } }, '运行导入检查');
  app.append(h('h1', null, '任务集 ', name), h('div', { class: 'row', style: 'margin-bottom:10px' }, checkBtn, h('a', { href: '#/experiments' }, '← 返回实验')), box);
  const chk = (c, k) => c == null ? '-' : (c[k] ? h('span', { class: 'ok' }, '✓') : h('span', { class: 'bad' }, '✗'));
  async function load() {
    try {
      const d = await GET('/eval/tasksets/' + encodeURIComponent(name));
      box.replaceChildren(h('p', { class: 'muted small' }, `版本 ${d.version || '-'} · ${(d.tasks || []).length} 个任务 · 格式见 CONTRACT §6(meta.json / prompt.md / init / verify / reference)`),
        table(['ID', '领域', '分组', '超时 s', '状态', '参考解通过', 'no-op 不通过', '重复一致', '时限内', '历史通过率'], (d.tasks || []).map(t => ({ cells: [
          h('span', { class: 'mono' }, t.id), t.domain, t.group, t.timeout_s, badge(t.status), chk(t.checks, 'reference_pass'), chk(t.checks, 'noop_fail'), chk(t.checks, 'repeat_consistent'), chk(t.checks, 'within_time'),
          t.pass_rate_hist == null ? '-' : (Array.isArray(t.pass_rate_hist) ? t.pass_rate_hist.map(x => num(x, 2)).join(' ') : h('span', { class: (t.pass_rate_hist === 0 || t.pass_rate_hist === 1) ? 'warn' : '' }, num(t.pass_rate_hist, 2) + ((t.pass_rate_hist === 0 || t.pass_rate_hist === 1) ? ' 无区分力' : ''))),
        ] }))));
    } catch (e) { box.replaceChildren(h('p', { class: 'err' }, e.message)); }
  }
  await load();
}

// ------------------------------------------------------------------ 批次详情
async function pageBatch(id) {
  const box = h('div');
  app.append(box);
  async function load() {
    const b = await GET('/eval/batches/' + id);
    const st = b.stats || {}, pr = st.paired || {}, p = b.progress || {};
    let baseStats = null;
    if (b.kind === 'compare' && b.base_batch) { try { baseStats = (await GET('/eval/batches/' + b.base_batch)).stats || {}; } catch {} }
    const stat = (k, v, cls) => h('div', { class: 'stat' }, h('div', { class: 'v ' + (cls || '') }, v), h('div', { class: 'k' }, k));
    const frag = [
      h('h1', null, '批次 ', h('span', { class: 'mono' }, b.id), ' ', badge(b.kind), ' ', badge(b.status)),
      h('p', { class: 'muted small' }, `任务集 ${b.taskset} · 版本 ${b.version} · K=${b.k} · ${b.label || ''} · 进度 ${p.done ?? 0}/${p.total ?? 0} (infra ${p.infra_fail ?? 0}) · ${when(b.created)}`,
        b.base_batch ? [' · 基线 ', linkTo('#/batches/' + b.base_batch, b.base_batch)] : null),
      h('div', { class: 'stats' },
        stat('mean', num(st.mean)), stat('avg_k', num(st.avg_k, 1)), stat('δ 噪声带', num(st.delta)),
        b.kind === 'compare' ? [stat('diff (配对)', num(pr.diff), pr.diff > 0 ? 'ok' : pr.diff < 0 ? 'bad' : ''),
          stat('ci95', pr.ci95 ? `[${num(pr.ci95[0])}, ${num(pr.ci95[1])}]` : '-'), stat('mcnemar_p', num(pr.mcnemar_p)), stat('n_tasks', pr.n_tasks ?? '-'),
          stat('accept', st.accept == null ? '-' : (st.accept ? '✓ 接受' : '✗ 拒绝'), st.accept ? 'ok' : 'bad')] : null),
      st.reason ? h('p', { class: 'small' }, '原因:', st.reason) : null,
    ];
    const matrix = b.matrix || {}, per = st.per_task || {};
    const tasks = Object.keys(matrix);
    if (tasks.length) {
      const K = Math.max(...tasks.map(t => matrix[t].length), b.k || 0);
      const cols = ['任务', ...Array.from({ length: K }, (_, i) => `k=${i}`), 'p̂'];
      if (baseStats) cols.push('基线 p̂', '配对差');
      const rows = tasks.map(t => {
        const cells = [h('span', { class: 'mono' }, t)];
        for (let i = 0; i < K; i++) {
          const c = matrix[t][i];
          if (!c) { cells.push(h('span', { class: 'muted' }, '·')); continue; }
          const ok = c.infra_fail ? h('span', { class: 'warn' }, '⚠ infra') : c.pass ? h('span', { class: 'ok' }, '✓') : h('span', { class: 'bad' }, '✗');
          cells.push(h('a', { href: '#/runs/' + c.run_id, class: 'cell' }, h('div', { class: 's' }, ok, ' ', num(c.score, 2)),
            h('div', { class: 'muted' }, `$${num(c.cost, 3)} ${c.elapsed_s != null ? Math.round(c.elapsed_s) + 's' : ''}`, (c.cheat && c.cheat.length) ? h('span', { class: 'bad' }, ` 作弊×${c.cheat.length}`) : null)));
        }
        const ph = per[t] ? per[t].p_hat : null;
        cells.push(h('span', { class: 'mono' }, num(ph, 2), per[t] && per[t].var != null ? h('span', { class: 'muted small' }, ` ±${num(per[t].var, 2)}`) : null));
        if (baseStats) {
          const bp = baseStats.per_task && baseStats.per_task[t] ? baseStats.per_task[t].p_hat : null;
          const d = (ph != null && bp != null) ? ph - bp : null;
          cells.push(h('span', { class: 'mono' }, num(bp, 2)), h('span', { class: 'mono ' + (d > 0 ? 'ok' : d < 0 ? 'bad' : '') }, d == null ? '-' : (d > 0 ? '+' : '') + num(d, 2)));
        }
        return { cells };
      });
      frag.push(h('h2', null, '任务 × 次 结果矩阵'), h('div', { class: 'panel' }, table(cols, rows)));
    } else frag.push(h('p', { class: 'empty' }, b.status === 'done' ? '无矩阵数据' : '批次尚未完成,矩阵待生成(每 5 秒刷新)'));
    setKids(box, frag);
    return b;
  }
  const b = await load();
  if (b.status === 'queued' || b.status === 'running') {
    const id2 = setInterval(async () => { const bb = await load().catch(() => null); if (bb && !(bb.status === 'queued' || bb.status === 'running')) clearInterval(id2); }, 5000);
    onLeave(() => clearInterval(id2));
  }
}

// ------------------------------------------------------------------ 运行详情
async function pageRun(id) {
  const r = await GET('/eval/runs/' + id);
  const v = r.verify || {};
  app.append(
    h('h1', null, '运行 ', h('span', { class: 'mono' }, r.id), ' ', r.infra_fail ? badge('infra_fail') : (r.pass ? h('span', { class: 'badge ok' }, 'pass') : h('span', { class: 'badge failed' }, 'fail'))),
    h('p', { class: 'small' }, '批次 ', linkTo('#/batches/' + r.batch, r.batch), ' · 任务 ', h('span', { class: 'mono' }, r.task), ' · 版本 ', r.version, ' · k=', r.k_index, ' · 沙箱 ', r.sandbox_id || '-', ' · 节点 ', r.node || '-', ' · ', when(r.created)),
    h('div', { class: 'grid2' },
      h('div', { class: 'panel' }, h('h2', { style: 'margin-top:0' }, '元数据'), kv(r, ['score', 'pass', 'infra_fail', 'attempts', 'agent_exit', 'cost', 'tokens_in', 'tokens_out', 'elapsed_s'])),
      h('div', { class: 'panel' }, h('h2', { style: 'margin-top:0' }, '验收'), kv(v, ['exit', 'score']), h('pre', { class: 'out' }, v.stdout_tail || '(无输出)'),
        h('h2', null, '作弊信号'), (r.cheat && r.cheat.length) ? h('ul', null, r.cheat.map(c => h('li', { class: 'bad' }, h('b', null, c.signal), ' ', c.detail || ''))) : h('p', { class: 'muted small' }, '无'))),
    h('h2', null, '轨迹'), trajectory(r.traj));
}
function trajectory(traj) {
  if (!traj) return h('p', { class: 'muted' }, '无轨迹');
  const msgs = Array.isArray(traj) ? traj : (traj.messages || traj.trajectory || []);
  if (!msgs.length) return h('details', null, h('summary', null, '未知格式,显示原始 JSON'), h('pre', { class: 'out' }, JSON.stringify(traj, null, 2)));
  const info = traj.info ? h('p', { class: 'muted small' }, Object.entries(traj.info).filter(([, v]) => typeof v !== 'object').map(([k, v]) => `${k}=${v}`).join(' · ')) : null;
  const steps = h('div', { class: 'steps' });
  let n = 0;
  for (const m of msgs) {
    const role = m.role || '?';
    const content = typeof m.content === 'string' ? m.content : Array.isArray(m.content) ? m.content.map(c => c.text || c.content || '').join('\n') : (m.content == null ? '' : JSON.stringify(m.content));
    const step = h('div', { class: 'step ' + role });
    if (role === 'assistant') n++;
    step.append(h('div', { class: 'role' }, role === 'assistant' ? `第 ${n} 步 · assistant` : role));
    if (role === 'assistant') {
      if (content) step.append(h('div', { class: 'text' }, content));
      for (const tc of m.tool_calls || []) {
        let cmd = null, args = tc.function && tc.function.arguments;
        try { const a = typeof args === 'string' ? JSON.parse(args) : args; cmd = a && (a.command || a.cmd); if (!cmd && a) cmd = JSON.stringify(a); } catch { cmd = String(args); }
        step.append(h('pre', { class: 'out' }, h('span', { class: 'meta' }, `$ `), cmd || '(无命令)'));
      }
    } else if (role === 'tool') {
      step.append(collapsible(content, 12));
    } else if (role === 'exit') {
      step.append(h('div', { class: 'text ok' }, content || JSON.stringify(m)));
    } else {
      step.append(collapsible(content, role === 'system' ? 3 : 20));
    }
    steps.append(step);
  }
  return h('div', null, info, steps);
}
function collapsible(text, maxLines) {
  const lines = (text || '').split('\n');
  if (lines.length <= maxLines && text.length < 2000) return h('pre', { class: 'out' }, text || '(空)');
  const head = lines.slice(0, maxLines).join('\n').slice(0, 2000);
  const pre = h('pre', { class: 'out' }, head + '\n…');
  const d = h('details', null, h('summary', null, `展开全部(${lines.length} 行,${text.length} 字符)`), h('pre', { class: 'out' }, text));
  d.addEventListener('toggle', () => pre.style.display = d.open ? 'none' : '');
  return h('div', null, pre, d);
}

// ------------------------------------------------------------------ boot
health(); setInterval(health, 15000);
route();
})();
