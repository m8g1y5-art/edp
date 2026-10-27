/* 具身数据平台 · 前端（无构建，原生 JS）
   层级：项目 → 批次 → 录制。批次内分“预检 / 处理 / 浏览”三个标签页。 */
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const T = s => `data-tip="${String(s).replace(/"/g,'&quot;')}"`;
const api = async (url, opt={}) => {
  const r = await fetch(url, {headers:{'Content-Type':'application/json'}, ...opt, body: opt.body && typeof opt.body !== 'string' && !(opt.body instanceof FormData) ? JSON.stringify(opt.body) : opt.body});
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m } catch(e){} throw new Error(m); }
  return r.json();
};
const fmtDur = s => { s = Math.round(s||0); if (s < 60) return s + ' 秒'; if (s < 3600) return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); return Math.floor(s/3600) + ':' + String(Math.floor(s%3600/60)).padStart(2,'0') + ':' + String(s%60).padStart(2,'0'); };
// 批次实际用到的大模型（来自分段结果和物体清单，不是当前设置）。多个模型时都列出，按段数排序
const modelCell = ms => {
  if (!ms || !ms.length) return '<span class="k">—</span>';
  const cut = s => s.length > 22 ? s.slice(0, 21) + '…' : s;
  const tip = ms.map(m => `<b>${esc(m.m)}</b>：${m.segs} 段语义 · ${m.objs} 条物体清单`).join('<br>') + '<br><span class=tk>按处理结果统计；重跑过的录制按最新结果算</span>';
  return `<div ${T(tip)}>${ms.slice(0, 2).map(m => `<div class="mono nowrap" style="font-size:12px">${esc(cut(m.m))}<span class="k"> ${m.segs ? m.segs + ' 段' : m.objs + ' 条清单'}</span></div>`).join('')}${ms.length > 2 ? `<div class="k">另 ${ms.length - 2} 个</div>` : ''}</div>`;
};
const tcode = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60); return String(m).padStart(2, '0') + ':' + (t - m * 60).toFixed(2).padStart(5, '0'); };
const fmtHours = s => !s ? '—' : s < 3600 ? (s/60).toFixed(1) + ' 分钟' : (s/3600).toFixed(1) + ' 小时';
const fmtMin = m => !m ? '—' : m < 1 ? '<1 分' : m < 90 ? Math.round(m) + ' 分' : (m/60).toFixed(1) + ' 小时';
const fmtB = b => b > 1e9 ? (b/1e9).toFixed(1) + ' GB' : b > 1e6 ? (b/1e6).toFixed(0) + ' MB' : (b/1e3).toFixed(0) + ' KB';
const clock = t => { const d = new Date(t*1000); return d.toTimeString().slice(0,8); };
const fmtDate = t => { const d = new Date(t*1000), p = n => String(n).padStart(2,'0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
const GR = {3:['A','gA','能直接训练机器人'],2:['B','gB','有动作轨迹，可学“怎么做”'],1:['C','gC','只有画面，可标注任务'],0:['D','gD','暂不可用，需要处理']};
const gradeHTML = (r, cls='') => `<span class="grade ${GR[r][1]} ${cls}" ${T('<b>输入质量 '+GR[r][0]+'</b><br>'+GR[r][2]+'<br><span class=tk>A 能训练机器人 · B 有动作轨迹 · C 能标注任务 · D 不可用</span>')}>${GR[r][0]}</span>`;
const BST = {new:['等待上传','mute'], scanning:['预检中','run'], checked:['待处理','mute'], processing:['处理中','run'], done:['已完成','ok'], error:['预检出错','bad']};
const RST = {scanned:['未处理','mute'], queued:['排队','run'], running:['处理中','run'], done:['完成','ok'], failed:['有失败','bad']};
const tag = (txt, cls) => `<span class="tag ${cls}">${esc(txt)}</span>`;
/* UUID 一类的长名字只显示前 8 位，悬停看全名 */
const isLong = n => /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(String(n||''));
const short = n => isLong(n) ? String(n).slice(0, 8) : String(n ?? '');
const shortText = t => String(t ?? '').replace(/\b([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, '$1');
const IC = {
  cloud:'<svg viewBox="0 0 24 24"><path d="M7 18h10a4 4 0 00.6-7.95A6 6 0 006.1 9.6 4.2 4.2 0 007 18z"/></svg>',
  hand:'<svg viewBox="0 0 24 24"><path d="M8 13V5.5a1.5 1.5 0 013 0V12m0-1V4.5a1.5 1.5 0 013 0V12m0-6.5a1.5 1.5 0 013 0V14a7 7 0 01-7 7h-.5a6 6 0 01-5-2.7L3 14.6a1.6 1.6 0 012.6-1.8L8 15"/></svg>',
  obj:'<svg viewBox="0 0 24 24"><path d="M4 8V5a1 1 0 011-1h3M16 4h3a1 1 0 011 1v3M20 16v3a1 1 0 01-1 1h-3M8 20H5a1 1 0 01-1-1v-3"/></svg>',
  depth:'<svg viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></svg>',
  play:'<svg viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg>',
  pause:'<svg viewBox="0 0 24 24"><path d="M6.5 4.5h4v15h-4zM13.5 4.5h4v15h-4z"/></svg>',
  prev:'<svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>',
  next:'<svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
  split:'<svg viewBox="0 0 24 24"><path d="M12 3v18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg>',
  merge:'<svg viewBox="0 0 24 24"><path d="M3 12h18M8 7l4 5-4 5M16 7l-4 5 4 5"/></svg>',
};
const stepBar = ss => { const tot = Object.entries(ss||{}).filter(([k]) => k !== 'skip').reduce((a,[,v]) => a+v, 0) || 0;
  if (!tot) return '<span class="k">—</span>';
  const w = k => ((ss[k]||0) / tot * 100).toFixed(1) + '%';
  const done = (ss.ok||0) + (ss.warn||0) + (ss.fail||0);
  return `<div class="mbar" ${T(`完成 ${ss.ok||0} · 警告 ${ss.warn||0} · 失败 ${ss.fail||0} · 运行 ${ss.run||0} · 等待 ${ss.wait||0}（步）`)}><i style="width:${w('ok')};background:var(--ok)"></i><i style="width:${w('warn')};background:var(--warn)"></i><i style="width:${w('fail')};background:var(--bad)"></i><i style="width:${w('run')};background:var(--acc)"></i></div><span class="mono k">${Math.round(done/tot*100)}%</span>`; };

let timers = [];
const every = (fn, ms) => { fn(); timers.push(setInterval(fn, ms)); };
const clearTimers = () => { timers.forEach(clearInterval); timers = []; };
const S = { lastBatch: null, lastProject: null, lastProjHash: null };
try { S.lastBatch = localStorage.getItem('lastBatch'); S.lastProject = localStorage.getItem('lastProject'); } catch(e) {}
const setLast = b => { S.lastBatch = b; try { localStorage.setItem('lastBatch', b) } catch(e){} };
const setLastP = p => { S.lastProject = p; try { localStorage.setItem('lastProject', p) } catch(e){} };
const onTab = t => location.hash.startsWith('#/b/') && location.hash.split('/')[3] === t;

/* ---------------- 路由 ---------------- */
const routes = {};
let navKey = '';
async function route() {
  clearTimers(); hideTip();
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  let name = parts[0] || '';
  // 旧地址兼容：#/precheck/<批次> → #/b/<批次>/precheck
  if (['precheck','process','browse'].includes(name)) { location.replace(parts[1] ? `#/b/${parts[1]}/${name}${parts[2] ? '/' + parts[2] : ''}` : '#/'); return; }
  navKey = {'':'projects', p:'projects', b:'projects', import:'projects'}[name] ?? name;
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('on', a.dataset.r === navKey));
  if (navKey === 'projects' && name !== 'import') S.lastProjHash = location.hash || '#/';
  const fn = routes[name] || routes[''];
  const want = SCROLL[location.hash || '#/'] || 0;
  try { await fn(...parts.slice(1)); } catch (e) { $('#main').innerHTML = `<div class="empty">出错了：${esc(e.message)}</div>`; console.error(e); }
  // 内容是异步画出来的：等几帧再滚回原位置，直到页面够高
  if (want) { let n = 0; const go = () => { window.scrollTo(0, want); if (Math.abs(window.scrollY - want) > 2 && n++ < 40) setTimeout(go, 100); }; go(); }
  else window.scrollTo(0, 0);
}
// 离开页面时记住滚动位置；回到同一页面时恢复。“项目”栏记住最后看的页面，从任务队列等处点回来时回到那里
const SCROLL = {};
window.addEventListener('hashchange', e => { try { SCROLL[new URL(e.oldURL).hash || '#/'] = window.scrollY; } catch (err) {} route(); });
document.querySelectorAll('nav a').forEach(a => a.addEventListener('click', e => {
  if (a.dataset.r === 'projects' && navKey !== 'projects' && S.lastProjHash) { e.preventDefault(); location.hash = S.lastProjHash; }
}));

/* 页面头：面包屑 + 标题 + 操作 + （批次内）标签页 */
function pageHead({crumbs = [], title = '', meta = '', actions = '', tabs = '', tip = ''}) {
  // 顶栏：面包屑（最后一节就是标题）· 标签页 · 操作按钮。说明性的 meta 放成小字，长路径之类放悬停提示
  const up = crumbs.slice(0, -1).map(([t, h]) => h ? `<a href="${h}">${esc(t)}</a>` : `<span>${esc(t)}</span>`).join('<i>/</i>');
  return `<header class="phead"><div class="crumbs">${up ? up + '<i>/</i>' : ''}<h1 ${tip ? T(tip) : ''}>${title}</h1></div>${tabs}${meta ? `<span class="meta">${meta}</span>` : ''}<span class="sp"></span><div class="acts">${actions}</div></header>`;
}
function batchHead(b, tab, extra = {}) {
  const st = BST[b.status] || [b.status, 'mute'];
  const p = b.project || {};
  const tabs = `<div class="tabs">${[['precheck','预检'],['process','处理'],['browse','浏览']].map(([k, t]) => `<a href="#/b/${b.id}/${k}" class="${k===tab?'on':''}">${t}${extra[k] != null ? `<span>${extra[k]}</span>` : ''}</a>`).join('')}</div>`;
  return pageHead({crumbs: [['项目', '#/'], [p.name || '项目', p.id ? '#/p/' + p.id : null], [b.name, null]],
    title: `${esc(b.name)}${st[1] === 'ok' || st[1] === 'mute' ? '' : ' ' + tag(st[0], st[1])}`, tip: '数据来源：' + esc(b.src) + '<br>状态：' + st[0], actions: extra.actions || '', tabs});
}

/* ---------------- 左下角系统状态 ---------------- */
async function sysTick() {
  try {
    const s = await api('/api/sys');
    const g = s.gpu, v = s.vlm;
    const memp = g ? Math.round(g.mem_used / g.mem_total * 100) : 0;
    $('#sys').innerHTML = `
      <div class="si" ${T(g ? `<b>${esc(g.name)}</b><br>利用率 ${g.util}% · 显存 ${(g.mem_used/1024).toFixed(1)}/${(g.mem_total/1024).toFixed(1)} GB · ${g.temp}°C` : '没检测到 NVIDIA 显卡（nvidia-smi 不可用）')}><b>${g ? g.util + '%' : '—'}</b><div class="gauge"><i style="width:${memp}%"></i></div></div>
      <div class="si" ${T((v.ok ? (v.model ? '视觉模型：<b>' + esc(v.model) + '</b><br>在“设置”里可更换' : 'Ollama 在运行，但没有找到视觉模型') : esc(v.err || '模型未连接')) + `<br>队列 ${s.queue} 步<br>磁盘可用 ${fmtB(s.disk_free)}<br><span class=tk>${esc(s.data_dir)}</span>`)}><i class="dot" style="background:${v.model ? 'var(--ok)' : v.ok ? 'var(--warn)' : 'var(--bad)'}"></i></div>`;
    $('#qn').textContent = s.queue || '';
  } catch (e) { $('#sys').innerHTML = `<div class="si" ${T('后台未响应')}><i class="dot" style="background:var(--bad)"></i></div>`; }
}
setInterval(sysTick, 3000); sysTick();

/* ---------------- 提示气泡 / toast ---------------- */
const tip = $('#tip'); let tipEl = null;
function hideTip(){ tip.style.opacity = 0; tipEl = null; }
document.addEventListener('mousemove', e => {
  const el = e.target.closest('[data-tip]');
  if (!el) { hideTip(); return; }
  if (el !== tipEl) { tipEl = el; tip.innerHTML = el.dataset.tip; tip.style.opacity = 1; }
  const w = tip.offsetWidth, h = tip.offsetHeight; let x = e.clientX + 12, y = e.clientY + 16;
  if (x + w > innerWidth - 8) x = e.clientX - w - 12; if (y + h > innerHeight - 8) y = e.clientY - h - 12;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
let undoFn = null;
function toast(m, undo) {
  const t = $('#toast'); $('#tmsg').textContent = m; $('#undo').style.display = undo ? '' : 'none';
  undoFn = undo; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 5000);
}
$('#undo').onclick = () => { if (undoFn) undoFn(); $('#toast').classList.remove('show'); };

/* 小对话框：输入一行文字 / 选择 */
function ask(title, {value = '', placeholder = '', select = null, ok = '确定'} = {}) {
  return new Promise(res => {
    const d = document.createElement('div'); d.className = 'modal';
    d.innerHTML = `<div class="dlg"><h3>${esc(title)}</h3>${select ? `<select id="mdv">${select.map(([v, t]) => `<option value="${esc(v)}" ${v===value?'selected':''}>${esc(t)}</option>`).join('')}</select>` : `<input type="text" id="mdv" value="${esc(value)}" placeholder="${esc(placeholder)}">`}
      <div class="row"><span class="sp"></span><button class="btn" data-x>取消</button><button class="btn pri" data-ok>${esc(ok)}</button></div></div>`;
    document.body.appendChild(d);
    const inp = d.querySelector('#mdv'); inp.focus(); if (inp.select) inp.select();
    const done = v => { d.remove(); res(v); };
    d.onclick = e => { if (e.target === d || e.target.dataset.x !== undefined) done(null); if (e.target.dataset.ok !== undefined) done(inp.value.trim()); };
    inp.onkeydown = e => { if (e.key === 'Enter') done(inp.value.trim()); if (e.key === 'Escape') done(null); };
  });
}

/* ================================================================ 项目列表 */
routes[''] = async () => {
  const main = $('#main');
  const draw = async () => {
    const ps = await api('/api/projects');
    main.innerHTML = pageHead({title: '项目', meta: '一个客户或一个数据集一个项目；每次导入是项目下的一个批次',
        actions: '<button class="btn" id="newp">新建项目</button><a class="btn pri" href="#/import">导入数据</a>'}) +
      `<div class="card"><table class="t lg"><thead><tr><th>项目</th><th class="r">批次</th><th class="r">录制</th><th class="r">总时长</th><th>处理情况</th><th>最近导入</th><th></th></tr></thead><tbody>
      ${ps.map(p => `<tr class="click" data-go="#/p/${p.id}"><td><b>${esc(p.name)}</b>${p.note ? `<div class="k">${esc(p.note)}</div>` : ''}</td>
        <td class="r mono">${p.batches}</td><td class="r mono">${p.recs}</td><td class="r mono">${fmtHours(p.dur)}</td>
        <td>${p.recs ? `<span class="kpis"><span><b>${p.done}</b> 完成</span>${p.running ? `<span class="c-run"><b>${p.running}</b> 处理中</span>` : ''}${p.failed ? `<span class="c-bad"><b>${p.failed}</b> 有失败</span>` : ''}<span class="k">${p.recs - p.done - p.failed - p.running} 未处理</span></span>` : '<span class="k">还没有数据</span>'}</td>
        <td class="mono k">${p.batches ? fmtDate(p.last) : '—'}</td><td class="r"><a class="btn sm ghost" href="#/import/${p.id}">导入到此项目</a></td></tr>`).join('')}
      </tbody></table></div>`;
  };
  await draw();
  main.onclick = async e => {
    if (e.target.closest('a,button') && !e.target.closest('#newp')) return;
    if (e.target.id === 'newp') { const n = await ask('新建项目', {placeholder: '例如：某客户 · 桌面整理数据'}); if (!n) return; const r = await api('/api/projects', {method:'POST', body:{name:n}}); location.hash = '#/p/' + r.id; return; }
    const g = e.target.closest('[data-go]'); if (g) location.hash = g.dataset.go;
  };
};

/* ================================================================ 项目：批次列表 */
routes['p'] = async (pid) => {
  setLastP(pid);
  const main = $('#main');
  let projects = [];
  const draw = async () => {
    const [p, ps] = await Promise.all([api('/api/projects/' + pid), api('/api/projects')]);
    projects = ps;
    const bs = p.batches;
    const sum = k => bs.reduce((a, b) => a + (b.rec_status[k] || 0), 0);
    const n = bs.reduce((a, b) => a + b.n, 0), dur = bs.reduce((a, b) => a + (b.dur || 0), 0);
    main.innerHTML = pageHead({crumbs: [['项目', '#/'], [p.name, null]], title: esc(p.name), meta: p.note ? esc(p.note) : '',
        actions: `<button class="btn ghost" id="renp">重命名</button><button class="btn ghost danger" id="delp">删除项目</button><a class="btn pri" href="#/import/${pid}">导入新批次</a>`}) +
      `<div class="stats"><div><div class="k">批次</div><div class="v">${bs.length}</div></div><div><div class="k">录制</div><div class="v">${n}</div></div>
        <div><div class="k">总时长</div><div class="v">${fmtHours(dur)}</div></div><div><div class="k">已完成</div><div class="v c-ok">${sum('done')}</div></div>
        <div><div class="k">处理中</div><div class="v">${sum('running') + sum('queued')}</div></div><div><div class="k">有失败</div><div class="v ${sum('failed')?'c-bad':''}">${sum('failed')}</div></div></div>
      <div class="card"><h2>批次<span class="sp"></span><span class="k">每次导入是一个批次，按导入时间倒序</span></h2>
      ${bs.length ? `<table class="t lg"><thead><tr><th>批次</th><th>导入时间</th><th class="r">录制</th><th class="r">时长</th><th>状态</th><th style="width:180px">处理进度</th><th>使用模型</th><th class="r">预检问题</th><th></th></tr></thead><tbody>
      ${bs.map(b => { const st = BST[b.status] || [b.status,'mute']; const go = ['new','scanning','checked','error'].includes(b.status) ? 'precheck' : b.status === 'done' ? 'browse' : 'process';
        return `<tr class="click" data-go="#/b/${b.id}/${go}"><td><b>${esc(b.name)}</b>${b.prio ? ' <span class="tag run">置顶</span>' : ''}<div class="mono k ell" style="max-width:420px">${esc(b.src)}</div></td>
        <td class="mono k">${fmtDate(b.created)}</td><td class="r mono">${b.n}${b.skipped ? `<div class="k">跳过 ${b.skipped}</div>` : ''}</td><td class="r mono">${fmtHours(b.dur)}</td>
        <td>${tag(st[0], st[1])}${b.paused ? ' ' + tag('已暂停','warn') : ''}</td><td><div class="mcell">${stepBar(b.step_status)}</div></td>
        <td>${modelCell(b.models)}</td>
        <td class="r">${b.issues ? `<span class="mono c-warn">${b.issues}</span><span class="k"> 条</span>` : '<span class="k">—</span>'}</td>
        <td class="r nowrap"><button class="btn sm ghost" data-ren="${b.id}">重命名</button><button class="btn sm ghost" data-mv="${b.id}">移到…</button><button class="btn sm ghost danger" data-del="${b.id}">删除</button></td></tr>`; }).join('')}
      </tbody></table>` : `<div class="empty">这个项目还没有数据。<br><br><a class="btn pri" href="#/import/${pid}">导入第一批数据</a></div>`}</div>`;
  };
  every(draw, 4000);
  main.onclick = async e => {
    if (e.target.id === 'renp') { const p = projects.find(x => x.id === pid); const n = await ask('重命名项目', {value: p ? p.name : ''}); if (n) { await api('/api/projects/' + pid, {method:'PUT', body:{name:n}}); draw(); } return; }
    if (e.target.id === 'delp') { if (!confirm('删除这个项目？只能删除没有批次的项目。')) return; try { await api('/api/projects/' + pid, {method:'DELETE'}); location.hash = '#/'; } catch (err) { toast(err.message); } return; }
    const rn = e.target.closest('[data-ren]'); if (rn) { e.stopPropagation(); const n = await ask('重命名批次', {value: rn.closest('tr').querySelector('b').textContent}); if (n) { await api('/api/batches/' + rn.dataset.ren, {method:'PUT', body:{name:n}}); draw(); } return; }
    const mv = e.target.closest('[data-mv]'); if (mv) { const v = await ask('移到哪个项目？', {select: projects.map(p => [p.id, p.name]), value: pid, ok:'移动'}); if (v && v !== pid) { await api('/api/batches/' + mv.dataset.mv, {method:'PUT', body:{project_id:v}}); toast('已移到“' + projects.find(p => p.id === v).name + '”'); draw(); } return; }
    const d = e.target.closest('[data-del]'); if (d) { if (!confirm('删除这个批次的处理结果？（原始数据不会被删除；网页上传的副本会删除）')) return; await api('/api/batches/' + d.dataset.del, {method:'DELETE'}); draw(); return; }
    if (e.target.closest('a,button')) return;
    const g = e.target.closest('[data-go]'); if (g) location.hash = g.dataset.go;
  };
};

/* ================================================================ 批次入口：按状态去对应标签页 */
routes['b'] = async (bid, tab, rid) => {
  setLast(bid);
  if (!tab) { const b = await api('/api/batches/' + bid); location.replace(`#/b/${bid}/${['new','scanning','checked','error'].includes(b.status) ? 'precheck' : b.status === 'done' ? 'browse' : 'process'}`); return; }
  if (tab === 'precheck') return pagePrecheck(bid);
  if (tab === 'process') return pageProcess(bid);
  if (tab === 'browse') return pageBrowse(bid, rid);
};

/* ================================================================ 导入 */
routes['import'] = async (pid) => {
  const main = $('#main');
  const ps = await api('/api/projects');
  pid = pid || S.lastProject || (ps[0] && ps[0].id);
  if (!ps.find(p => p.id === pid)) pid = ps[0].id;
  const pname = ps.find(p => p.id === pid).name;
  main.innerHTML = pageHead({crumbs: [['项目', '#/'], [pname, '#/p/' + pid], ['导入', null]], title: '导入数据', meta: '每次导入成为项目下的一个批次'}) + `
  <div class="card" style="max-width:1000px;margin-bottom:14px"><div class="bd form">
    <div class="row"><span>导入到项目</span><div style="display:flex;gap:8px"><select id="pj" style="flex:1">${ps.map(p => `<option value="${p.id}" ${p.id===pid?'selected':''}>${esc(p.name)}</option>`).join('')}</select><button class="btn" id="newp">新建项目</button></div></div>
    <div class="row"><span>批次名称</span><input type="text" id="bname" placeholder="留空则用文件夹名，例如：10月第2批"></div>
  </div></div>
  <div class="imp">
    <div class="card"><h2>本机文件夹（推荐）</h2><div class="bd">
      <div class="pathrow"><input type="text" id="path" placeholder="例如 C:\\Users\\me\\Desktop\\ITW_Data_Sample" spellcheck="false"><button class="btn pri" id="go">开始预检</button></div>
      <div class="hint">直接读取原位置的文件，不复制，适合大数据量。<br>在资源管理器里选中文件夹，按 <kbd>Shift</kbd> + 右键 →“复制文件地址”，粘贴到这里。</div>
    </div></div>
    <div class="card"><h2>拖入文件夹</h2><div class="bd">
      <div class="drop" id="drop">把文件夹拖到这里，或 <a href="javascript:void 0" id="pick">选择文件夹</a><input type="file" id="fi" webkitdirectory multiple hidden></div>
      <div id="upst" class="hint">文件会复制到平台的数据目录里。几 GB 以内适用。</div>
    </div></div>
  </div>
  <div style="max-width:1000px;margin-top:14px" class="card"><h2>两种文件夹都可以</h2><div class="bd hint" style="margin:0">
    <b>单条录制</b>：文件夹里直接是这一条的彩色视频、深度、关键点、标定等文件。<br>
    <b>多条录制</b>：文件夹里是几十上百个子文件夹，每个子文件夹一条录制。系统会逐条识别，把文件结构相同的归成一组，预检页给出整批的结论和每一条的结果。<br>
    认识的格式：视频（mp4 / mov / mkv，含 16 位深度 mkv）、图片序列（含 16 位深度 PNG）、时间戳表、IMU / 位姿 / 关节 / 夹爪 / 力（csv / txt，按列名识别）、手部关键点 JSON、标定（yaml / json）、任务说明 JSON、URDF。认不出来的文件单独列出，不影响其他数据。</div></div>`;
  const body = extra => ({...extra, project_id: $('#pj').value, name: $('#bname').value.trim() || extra.name});
  $('#newp').onclick = async () => { const n = await ask('新建项目'); if (!n) return; const r = await api('/api/projects', {method:'POST', body:{name:n}}); location.hash = '#/import/' + r.id; };
  $('#pj').onchange = () => { setLastP($('#pj').value); };
  $('#go').onclick = async () => {
    const p = $('#path').value.trim(); if (!p) return;
    try { const r = await api('/api/batches', {method:'POST', body: body({mode:'path', path:p})}); setLast(r.id); location.hash = `#/b/${r.id}/precheck`; }
    catch (e) { toast(e.message); }
  };
  $('#path').onkeydown = e => { if (e.key === 'Enter') $('#go').click(); };
  const drop = $('#drop');
  $('#pick').onclick = e => { e.stopPropagation(); $('#fi').click(); };
  drop.onclick = () => $('#fi').click();
  $('#fi').onchange = () => uploadFiles([...$('#fi').files].map(f => [f, f.webkitRelativePath || f.name]), body);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = async e => {
    e.preventDefault(); drop.classList.remove('over');
    const items = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    const out = [];
    const walk = (ent, pre) => new Promise(res => {
      if (ent.isFile) ent.file(f => { out.push([f, pre + f.name]); res(); });
      else { const rd = ent.createReader(); const all = []; const more = () => rd.readEntries(async es => { if (!es.length) { for (const c of all) await walk(c, pre + ent.name + '/'); res(); } else { all.push(...es); more(); } }); more(); }
    });
    for (const it of items) await walk(it, '');
    uploadFiles(out, body);
  };
};

async function uploadFiles(list, body) {
  if (!list.length) return;
  const st = $('#upst');
  const root = list[0][1].split('/')[0];
  const r = await api('/api/batches', {method:'POST', body: body({mode:'upload', name: root})});
  const total = list.reduce((a, [f]) => a + f.size, 0); let sent = 0;
  let i = 0;
  while (i < list.length) {
    const fd = new FormData(); const rels = []; let sz = 0;
    while (i < list.length && rels.length < 40 && sz < 64e6) { const [f, rel] = list[i++]; fd.append('files', f, f.name); rels.push(rel); sz += f.size; }
    fd.append('paths', JSON.stringify(rels));
    await fetch(`/api/batches/${r.id}/upload`, {method:'POST', body: fd});
    sent += sz;
    st.innerHTML = `上传中 ${fmtB(sent)} / ${fmtB(total)}<div class="pbar" style="margin-top:6px"><i style="width:${sent/total*100}%"></i></div>`;
  }
  await api(`/api/batches/${r.id}/scan`, {method:'POST'});
  setLast(r.id); location.hash = `#/b/${r.id}/precheck`;
}

/* ================================================================ 任务队列（全局） */
routes['queue'] = async () => {
  const main = $('#main');
  const draw = async () => {
    const d = await api('/api/queue');
    const L = d.live;
    const act = d.batches.filter(b => b.wait + b.run + (b.bwait||0) > 0), fin = d.batches.filter(b => b.wait + b.run + (b.bwait||0) === 0);
    const row = b => { const pct = b.total ? Math.round((b.done + b.fail) / b.total * 100) : 0;
      const st = b.run ? tag('运行中','run') : b.wait ? (b.paused ? tag('已暂停','warn') : tag('排队','mute')) : b.bwait ? tag('等待批量结果','run') : b.fail ? tag('有失败','bad') : tag('完成','ok');
      return `<tr class="click" data-go="#/b/${b.id}/process"><td>${b.prio ? '<span class="tag run">置顶</span> ' : ''}<b>${esc(b.name)}</b><div class="k">${esc(b.pname || '')}</div></td><td>${st}</td>
        <td><div class="mcell"><div class="mbar"><i style="width:${b.total ? b.done/b.total*100 : 0}%;background:var(--ok)"></i><i style="width:${b.total ? b.fail/b.total*100 : 0}%;background:var(--bad)"></i><i style="width:${b.total ? b.run/b.total*100 : 0}%;background:var(--acc)"></i></div><span class="mono k">${pct}%</span></div></td>
        <td class="r mono">${b.wait}</td><td class="r mono">${b.done}</td><td class="r mono ${b.fail?'c-bad':''}">${b.fail}</td>
        <td class="r nowrap">${b.wait + b.run ? `<button class="btn sm ghost" data-top="${b.id}|${b.prio ? 0 : 1}">${b.prio ? '取消置顶' : '置顶'}</button><button class="btn sm ghost" data-pz="${b.id}|${b.paused ? 'resume' : 'pause'}">${b.paused ? '继续' : '暂停'}</button>` : ''}</td></tr>`; };
    const tb = (rows, empty) => rows.length ? `<table class="t lg"><thead><tr><th>批次</th><th>状态</th><th style="width:200px">进度</th><th class="r">等待步数</th><th class="r">完成</th><th class="r">失败</th><th></th></tr></thead><tbody>${rows.map(row).join('')}</tbody></table>` : `<div class="bd k">${empty}</div>`;
    let bq = null; try { bq = await api('/api/batchq'); } catch (e) {}
    const JST = {validating:['校验中','run'], in_progress:['处理中','run'], finalizing:['收尾中','run'], completed:['已完成','ok'], failed:['失败','bad'], expired:['已过期','warn'], cancelling:['取消中','mute'], cancelled:['已取消','mute']};
    const bqc = bq && (bq.jobs.length || Object.keys(bq.reqs).length) ? `<div class="card" style="margin-top:12px"><h2>百炼批量任务<span class="k">待提交 ${bq.reqs.pending||0} · 处理中 ${bq.reqs.submitted||0} · 已返回 ${bq.reqs.done||0} · 失败 ${bq.reqs.failed||0} 个请求 · ${bq.waiting_steps} 步在等结果</span><span class="sp"></span>
        <button class="btn sm" id="bqflush" ${T('不等凑满一包、不等时间，立刻把待提交的请求打包交上去')}>立即提交</button><button class="btn sm" id="bqpoll">刷新状态</button></h2>
        ${bq.err ? `<div class="bd c-bad k">${esc(bq.err)}</div>` : ''}
        ${bq.jobs.length ? `<table class="t"><thead><tr><th>任务</th><th>模型</th><th class="r">请求数</th><th>状态</th><th class="r">成功</th><th class="r">失败</th><th>提交时间</th><th></th></tr></thead><tbody>${bq.jobs.map(j => { const st = JST[j.status] || [j.status, 'mute'];
          return `<tr><td class="mono">${esc(j.id.slice(-12))}</td><td class="mono k">${esc(j.model)}</td><td class="r mono">${j.n}</td><td>${tag(st[0], st[1])}${j.status==='completed'&&!j.ingested?' <span class="k">取回中</span>':''}${j.msg?`<div class="k c-bad" style="white-space:normal;max-width:360px">${esc(j.msg)}</div>`:''}</td><td class="r mono">${j.done||0}</td><td class="r mono ${j.failed?'c-bad':''}">${j.failed||0}</td><td class="mono k">${fmtDate(j.created)}</td><td class="r">${['validating','in_progress','finalizing'].includes(j.status) ? `<button class="btn sm ghost danger" data-bqc="${esc(j.id)}">取消</button>` : ''}</td></tr>`; }).join('')}</tbody></table>` : '<div class="bd k">还没有提交过批量任务。请求会在凑满一包、等够设定时间，或队列空闲时自动提交。</div>'}</div>` : '';
    main.innerHTML = pageHead({title: '任务队列', meta: '所有项目共用一台机器，按“置顶优先，其余按导入先后”的顺序一步步执行'}) +
      `<div class="card live"><div class="bd">${L ? `<div class="k">正在执行</div><div class="lv"><b>${esc(L.label)}</b><span>${esc(L.rec)}</span><span class="sp"></span><span class="mono k">已 ${fmtDur(L.since)}</span></div><div class="pbar"><i style="width:${(L.progress||0)*100}%"></i></div><div class="mono k ell">${esc(L.msg || '')}</div>` : '<div class="k">空闲：没有在执行的步骤</div>'}</div></div>
      <div class="card" style="margin-top:12px"><h2>排队中<span class="k">${act.length} 个批次</span></h2>${tb(act, '队列是空的。在批次的“预检”页点“开始处理”加入队列。')}</div>
      ${bqc}
      <div class="card" style="margin-top:12px"><h2>已结束<span class="k">${fin.length} 个批次</span></h2>${tb(fin, '暂无')}</div>`;
  };
  every(draw, 2000);
  main.onclick = async e => {
    const t = e.target.closest('[data-top]'); if (t) { const [bid, on] = t.dataset.top.split('|'); await api(`/api/batches/${bid}/prio`, {method:'POST', body:{top: on === '1'}}); draw(); return; }
    const p = e.target.closest('[data-pz]'); if (p) { const [bid, a] = p.dataset.pz.split('|'); await api(`/api/batches/${bid}/${a}`, {method:'POST'}); draw(); return; }
    if (e.target.id === 'bqflush') { e.target.disabled = true; await api('/api/batchq/flush', {method:'POST'}); toast('已提交'); draw(); return; }
    if (e.target.id === 'bqpoll') { await api('/api/batchq/poll', {method:'POST'}); draw(); return; }
    const bc = e.target.closest('[data-bqc]'); if (bc) { if (!confirm('取消这个批量任务？已处理完的请求仍会计费，没处理的请求会重新排队。')) return; try { await api(`/api/batchq/${bc.dataset.bqc}/cancel`, {method:'POST'}); } catch (err) { toast(err.message); } draw(); return; }
    if (e.target.closest('a,button')) return;
    const g = e.target.closest('[data-go]'); if (g) location.hash = g.dataset.go;
  };
};

/* ================================================================ 预检 */
const ACT = {run:'执行', verify:'核查', est:'估算', gpu:'显卡执行', skip:'跳过'};
const COL = {run:'var(--acc)', verify:'var(--ok)', est:'var(--warn)', gpu:'var(--gpu)'};
const PC = {sel:null, hover:null, data:null, bid:null, open:false};

function lineHTML(r, ext, big) {
  const pos = i => `calc(5px + (100% - ${big?16:10}px) * ${i/2})`;
  const fill = r ? `<span class="fill" style="width:calc((100% - ${big?16:10}px) * ${(r-1)/2})"></span>` : '';
  const ex = ext > r && r ? `<span class="ext" style="left:${pos(r-1)};width:calc((100% - ${big?16:10}px) * ${(ext-r)/2})"></span>` : '';
  const st = PC.data.stops;
  const stops = [1,2,3].map(i => `<span class="stop ${i<=r?'on':''} ${i>r&&i<=ext?'next':''}" ${T('<b>'+st[i-1].n+'</b><br>'+st[i-1].d+'<br><span class=tk>需要：'+st[i-1].need+'</span>')}></span>`).join('');
  return `<div class="line">${fill}${ex}${stops}</div>`;
}
function injectUnknown(d) {
    const un = d.batch.info.unknown || [];
    if (un.length) d.groups.unshift({key:'__unknown', name:'无法识别', unknown:true, n:un.length, dur:0, bytes:d.batch.info.unknown_bytes, reach:0, max_reach:0, blocked:true, body:[], ups:[], plan:[], recs:[], issues:[['block','没有适配器能读取', un.slice(0,8).map(esc).join('<br>') + (un.length>8?`<br>… 共 ${un.length} 个`:'') + '<br><b>处理：</b>先跳过。需要时为这种格式开发适配器', un.length]], profile:{A:0,T:0,S:0,V:0,L:0,R:0}, p:{A:0,T:0,S:0,V:0,L:0,R:0}, est:{}});
    return d;
}

function renderPC() {
  const d = PC.data, b = d.batch, G = d.groups;
  const g = G.find(x => x.key === PC.sel);
  const need = G.filter(x => x.blocked), ok = G.filter(x => !x.blocked);
  const item = x => `<button class="g ${x.key===PC.sel?'on':''}" data-g="${esc(x.key)}">
      <div class="top"><span class="nm">${esc(x.name)}</span>${gradeHTML(x.reach)}</div>
      <div class="sz">${x.unknown ? x.n + ' 个文件 · ' + fmtB(x.bytes) : x.n + ' 条 · ' + fmtDur(x.dur)}</div>${lineHTML(x.reach, x.max_reach)}
      <div class="labels">${d.stops.map((s,i)=>`<span class="${i<x.reach?'on':''}">${s.n}</span>`).join('')}</div></button>`;
  // 悬停升级选项时预览
  const alt = PC.hover && g.alts ? g.alts[PC.hover] : null;
  const P = alt ? alt.p : g.p, E = alt ? alt.est : g.est, R = alt ? alt.reach : g.reach, PL = alt ? alt.plan : g.plan;
  const st = d.stops;
  const msg = R === 0 ? (g.unknown ? '<b>还没进站</b>：系统不认识这些文件。' : g.mismatch ? '<b>还没进站</b>：彩色与深度疑似来自不同录制，确认配对后即可出发。' : '<b>还没进站</b>：先解决下面的问题。')
    : R === 3 ? `已到终点 <b>${st[2].n}</b>。`
    : `到达 <b>${st[R-1].n}</b>。` + (g.max_reach > R ? `用下方选项还能开到 <b>${st[g.max_reach-1].n}</b>。` : `往前一站需要${st[R].need}，这批数据里没有。`);
  const dims = Object.keys(d.dims).map(k => { const lv = d.dims[k], n = lv.length - 1, b0 = g.profile[k], v = P[k], ch = alt && v !== g.p[k];
    return `<div class="dim ${ch?'pre':''}" ${T(`<b>${d.dim_n[k]} · ${k}${v}</b><br>${lv[v]}${E[k]?'（估计值）':''}<br><span class="tk">${lv.map((s,i)=>(i===v?'▸ ':'  ')+k+i+' '+s).join('<br>')}</span>`)}><div class="n">${d.dim_n[k]}</div><div class="lv">${k}${v}${E[k]?'<small>估计</small>':''}</div><div class="pips">${Array.from({length:n},(_,i)=>`<i class="${i<b0?'on':i<v?'up':''}"></i>`).join('')}</div></div>`; }).join('');
  const iss = g.issues.length ? g.issues.map(([s,h,dd,c]) => `<span class="iss ${s==='block'?'block':''}" ${T('<b>'+esc(h)+'</b><br>'+dd)}><i style="background:${s==='block'?'var(--bad)':s==='warn'?'var(--warn)':'var(--faint)'}"></i>${esc(h)}${g.n>1&&c>1&&!g.unknown?`<small>×${c}</small>`:''}</span>`).join('') : '<span class="clean">✓ 没有发现问题</span>';
  const old = new Set(g.plan.map(s => s.s + s.k));
  const stp = s => `<span class="st ${s.k} ${alt&&!old.has(s.s+s.k)?'new':''}" ${T('<b>'+esc(s.s)+' · '+ACT[s.k]+'</b><br>'+esc(s.w)+'<br><span class=tk>预计 '+fmtMin(s.min)+'</span>')}>${esc(s.s)}</span>`;
  const row = arr => arr.map(stp).join('<span class="arr">›</span>');
  const lanes = [...new Set(PL.filter(s => s.ln !== 'pre' && s.ln !== 'post').map(s => s.ln))];
  const flow = PL.length ? `<div class="fr"><span class="ll">通用</span>${row(PL.filter(s => s.ln === 'pre'))}</div>` +
    lanes.map(l => { const [n, dd] = l.split('|'); return `<div class="fr lane"><span class="ll" ${T('<b>'+esc(n)+'</b><br>'+esc(dd))}>${esc(n)}</span>${row(PL.filter(s => s.ln === l))}</div>`; }).join('') +
    (PL.some(s => s.ln === 'post') ? `<div class="fr"><span class="ll">通用</span>${row(PL.filter(s => s.ln === 'post'))}</div>` : '') : '';
  const mins = PL.reduce((a, s) => a + (s.min||0), 0);
  const cost = mins ? PL.filter(s => s.min).map(s => `<i style="width:${s.min/mins*100}%;background:${COL[s.k]}" ${T('<b>'+esc(s.s)+'</b> '+fmtMin(s.min))}></i>`).join('') : '';
  const ups = g.ups.map(u => { const a2 = g.alts[u.id]; const dm = a2.plan.reduce((a,s)=>a+(s.min||0),0) - g.plan.reduce((a,s)=>a+(s.min||0),0);
    return `<button class="chip ${g.on[u.id]?'on':''}" data-u="${u.id}" ${T('<b>'+esc(u.t)+'</b><br>'+esc(u.d))}>${g.on[u.id]?'✓ ':'+ '}${esc(u.t)}<span class="d">${dm>0.05?'+'+fmtMin(dm):dm<-0.05?'−'+fmtMin(-dm):'不增加耗时'}</span></button>`; }).join('');
  const bodyCard = g.body.length ? `<div class="body">${g.body.map(c => `<span class="comp ${c.idle?'idle':''}" ${T('<b>'+esc(c.n)+'</b><br>'+esc(c.d)+'<br><span class=tk>识别依据：'+esc(c.src||'—')+'</span>')}><i class="ic ${c.k}"></i>${esc(c.n)}${c.confirm?'<em class="cf">待确认</em>':''}</span>`).join('')}</div>` : '';
  const spark = (pts, c) => pts && pts.length ? `<svg viewBox="0 0 120 26" width="100%" height="26" preserveAspectRatio="none"><polyline fill="none" stroke="${c}" stroke-width="1.3" vector-effect="non-scaling-stroke" points="${pts.map((v,i)=>`${i*120/(pts.length-1)},${24-v*20}`).join(' ')}"/></svg>` : '';
  let mis = '';
  if (g.mismatch) { const m = g.mismatch, sg = m.suggest;
    mis = `<div class="mis">
      <div ${T('彩色与深度逐段“画面变化量”曲线。同一次录制的两条曲线应同起同落；相关系数 ' + m.corr + '（同一录制通常 > 0.8）')}><div class="k">画面运动（上：彩色　下：深度）· 相关 ${m.corr}</div>${spark(m.spark_a,'var(--acc)')}${spark(m.spark_b,'var(--bad)')}</div>
      <div ${T('两路时间戳范围的重叠比例')}><div class="k">时间重叠</div><div class="val ${m.overlap!=null&&m.overlap<0.5?'bad':''}">${m.overlap==null?'—':Math.round(m.overlap*100)+'%'}</div><div class="ref">同一录制 ≈ 100%</div></div>
      <div ${T('运动曲线的相关系数（允许 ±1 秒平移）')}><div class="k">运动相关</div><div class="val ${m.corr<0.25?'bad':''}">${m.corr}</div><div class="ref">同一录制 > 0.5</div></div>
      <div ${T(`彩色 ${m.dur_a} 秒 / 深度 ${m.dur_b} 秒`)}><div class="k">时长差</div><div class="val ${m.bad.includes('dur')?'bad':''}">${m.dur_diff} s</div><div class="ref">同一录制 < 1 s</div></div></div>
      ${sg ? `<div class="fix"><span>同批次中 <b class="mono">${esc(sg.rec_name)}/${esc(sg.stream)}</b> 与这段彩色视频吻合：时间重叠 ${sg.overlap==null?'—':Math.round(sg.overlap*100)+'%'}，运动相关 ${sg.corr}</span><span class="sp"></span><button class="btn pri" data-fix="${g.recs[0].id}">按建议重新配对</button></div>` : '<div class="fix">同批次里没找到能和它吻合的深度数据。可以先跳过，或把正确的深度文件夹放进来后重新预检。</div>'}`;
  }
  const thumbs = g.recs.length ? `<span class="thumbs">${[0,1,2,3].map(i=>`<img src="/api/media/${PC.bid}/${g.thumb_rec}/pre_${i}.jpg" onerror="this.style.visibility='hidden'">`).join('')}</span>` : '';
  const recs = g.recs.length > 0 && PCR.recs.length > 1 ? `<div><a href="javascript:void 0" data-grp="${esc(g.key)}">在下方“全部录制”里查看这组的 ${g.recs.length} 条录制 ›</a></div>` : '';
  $('#pcg').innerHTML = `
    <div class="layout ${G.length < 2 ? 'single' : ''}">
      <div class="card"><div class="lhead"><span>${G.length} 组 · ${need.length} 组需要你处理</span><span><kbd>↑</kbd> <kbd>↓</kbd></span></div>
        ${need.length ? '<div class="sep">需要你处理</div>' + need.map(item).join('') : ''}<div class="sep">可以处理</div>${ok.map(item).join('')}</div>
      <div class="det">
        <div class="title">${gradeHTML(R,'big')}<div><h2>${esc(g.name)}</h2><div class="src">输入质量 ${GR[R][0]} · ${GR[R][2]}</div></div><span class="sp"></span><span class="src">${g.unknown ? '' : g.n + ' 条 · ' + fmtDur(g.dur) + ' · ' + fmtB(g.bytes)}</span>${thumbs}</div>
        ${bodyCard}
        <div class="route">${lineHTML(R, g.max_reach, true)}<div class="labels">${st.map((s,i)=>`<span class="${i<R?'on':''}">${s.n}</span>`).join('')}</div><div class="msg">${msg}</div></div>
        ${g.unknown ? '' : `<div><div class="sec">能力画像${alt?'<span style="color:var(--acc)">· 预览</span>':''}</div><div class="dims">${dims}</div></div>`}
        <div><div class="sec">发现 ${g.issues.length} 项</div><div class="issues">${iss}</div></div>
        ${mis}
        ${PL.length ? `<div><div class="sec">处理计划<span class="mono">${fmtMin(mins)}</span>${d.vlm.model ? '' : '<span style="color:var(--warn)">· 未连接本地视觉模型，语义步骤会跳过</span>'}</div><div class="flow col">${flow}</div>
          ${cost ? `<div class="costbar">${cost}</div>` : ''}
          <div class="legend"><span><b style="background:var(--acc-bg)"></b>执行</span><span><b style="background:transparent;box-shadow:inset 0 0 0 1px var(--ok)"></b>核查</span><span><b style="background:var(--warn-bg)"></b>估算</span><span><b style="background:var(--gpu-bg)"></b>显卡</span><span><b style="background:repeating-linear-gradient(135deg,var(--line2) 0 3px,transparent 3px 5px)"></b>跳过</span><span>色条：时间花在哪</span></div></div>` : ''}
        ${ups ? `<div><div class="sec">还能更进一步</div><div class="ups">${ups}</div></div>` : ''}
        ${recs}
      </div>
    </div>
    `;
}

document.addEventListener('click', async e => {
  if (!onTab('precheck') || !PC.data) return;
  const gb = e.target.closest('[data-g]'); if (gb) { PC.sel = gb.dataset.g; PC.hover = null; PC.open = false; renderPC(); return; }
  const u = e.target.closest('[data-u]');
  if (u) { const g = PC.data.groups.find(x => x.key === PC.sel); await api(`/api/batches/${PC.bid}/upgrade`, {method:'POST', body:{group:g.key, id:u.dataset.u, on:!g.on[u.dataset.u]}}); PC.hover = null; hideTip(); await reloadPC(); return; }
  const f = e.target.closest('[data-fix]');
  if (f) { f.disabled = true; f.textContent = '重新核对中…'; await api(`/api/batches/${PC.bid}/repair`, {method:'POST', body:{rec:f.dataset.fix}}); toast('已重新配对，并重新核对彩色与深度'); await reloadPC(); return; }
  if (e.target.id === 'rescan') { await api(`/api/batches/${PC.bid}/rescan`, {method:'POST'}); PCR.detail = {}; route(); return; }
  if (e.target.id === 'start') { e.target.disabled = true; const vm = $('#vmode') ? $('#vmode').value : 'realtime'; let r; try { r = await api(`/api/batches/${PC.bid}/start`, {method:'POST', body:{vlm_mode: vm}}); } catch (err) { toast(err.message); e.target.disabled = false; return; } toast(`开始处理 ${r.n} 条录制` + (vm === 'batch' ? '（大模型走批量接口，结果陆续返回）' : '')); location.hash = `#/b/${PC.bid}/process`; }
});
document.addEventListener('mouseover', e => {
  if (!onTab('precheck') || !PC.data) return;
  const u = e.target.closest('[data-u]'); const id = u ? u.dataset.u : null;
  if (id !== PC.hover) { PC.hover = id; renderPC(); }
});
document.addEventListener('keydown', e => {
  if (!onTab('precheck') || !PC.data || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') || e.target.tagName === 'INPUT') return;
  const ids = [...document.querySelectorAll('[data-g]')].map(x => x.dataset.g);
  let i = ids.indexOf(PC.sel) + (e.key === 'ArrowDown' ? 1 : -1); i = Math.max(0, Math.min(ids.length - 1, i));
  PC.sel = ids[i]; PC.hover = null; renderPC(); e.preventDefault();
});


/* ================================================================ 预检（批次标签页）
   三层：整批结论与问题汇总 → 结构分组（同结构录制共用识别结果和处理计划）→ 全部录制逐条结果 */
const PCR = {filter:'all', q:'', page:0, chk:new Set(), open:null, group:null, recs:[], detail:{}};
const PAGE = 50;

async function pagePrecheck(bid) {
  setLast(bid); PC.bid = bid; PC.data = null;
  if (PCR.bid !== bid) Object.assign(PCR, {bid, filter:'all', q:'', page:0, chk:new Set(), open:null, group:null, detail:{}});
  const main = $('#main');
  main.innerHTML = '<div class="empty">载入中…</div>';
  const load = async () => {
    const d = await api(`/api/batches/${bid}/precheck`);
    const b = d.batch;
    setLastP(b.project && b.project.id);
    if (!d.groups) {
      main.innerHTML = batchHead(b, 'precheck') + `<div class="scanning card bd"><div style="font-weight:600">${b.status === 'new' ? '等待上传完成' : '正在识别和检查'}</div>
        <div class="pbar"><i style="width:${(b.progress||0)*100}%"></i></div><div class="k">${esc(b.msg||'')}</div>
        <div class="hint">只读文件结构、表头和抽样帧，不改动原始数据。</div></div>`;
      return false;
    }
    if (b.status === 'error') { main.innerHTML = batchHead(b, 'precheck') + `<div class="empty">预检失败：${esc(b.msg)}</div>`; return true; }
    PC.data = injectUnknown(d);
    PCR.recs = (await api(`/api/batches/${bid}/recs`)).recs;
    try { const sv = (await api('/api/sys')).vlm; PCR.backend = sv.backend || 'ollama'; PCR.batchOk = sv.batch !== false; PCR.model = sv.model || ''; } catch (e) { PCR.backend = 'ollama'; PCR.batchOk = true; }
    if (!PC.sel || !d.groups.find(g => g.key === PC.sel)) PC.sel = d.groups[0].key;
    renderPrecheck();
    return true;
  };
  if (!(await load())) { const h = setInterval(async () => { if (await load()) clearInterval(h); }, 800); timers.push(h); }
}

async function reloadPC() {
  PC.data = injectUnknown(await api(`/api/batches/${PC.bid}/precheck`));
  PCR.recs = (await api(`/api/batches/${PC.bid}/recs`)).recs;
  if (!PC.data.groups.find(g => g.key === PC.sel)) PC.sel = PC.data.groups[0].key;
  renderPrecheck();
}

function issueIndex() {
  // 汇总整批的问题：同一类问题合并，记下影响哪些录制；详细说明取自分组结果
  const det = {};
  PC.data.groups.forEach(g => g.issues.forEach(([lv, t, d]) => { if (!det[t]) det[t] = d; }));
  const m = {};
  PCR.recs.forEach(r => r.issues.forEach(([lv, t]) => {
    if (lv === 'info') return;
    const k = t; if (!m[k]) m[k] = {lv, t, d: det[t] || '', rids: []};
    if (lv === 'block') m[k].lv = 'block';
    m[k].rids.push(r.id);
  }));
  return Object.values(m).sort((a, b) => (a.lv === 'block' ? 0 : 1) - (b.lv === 'block' ? 0 : 1) || b.rids.length - a.rids.length);
}

function renderPrecheck() {
  const d = PC.data, b = d.batch, R = PCR.recs;
  const many = R.length > 1;
  const blocked = R.filter(r => r.blocked).length, skipped = R.filter(r => r.skipped && !r.blocked).length;
  const runnable = R.filter(r => !r.blocked && !r.skipped);
  const warned = runnable.filter(r => r.issues.some(i => i[0] === 'warn')).length;
  const dur = R.reduce((a, r) => a + r.dur, 0), bytes = R.reduce((a, r) => a + (r.bytes || 0), 0);
  const okG = d.groups.filter(x => !x.blocked && !x.unknown);
  const totMin = okG.reduce((s, x) => s + x.plan.reduce((a, y) => a + (y.min||0), 0) * (x.recs.filter(r => !(R.find(z => z.id === r.id) || {}).skipped).length / Math.max(1, x.recs.length)), 0);
  const iss = issueIndex();
  const issCard = many ? `<div class="card"><h2>问题汇总<span class="k">同类问题合并；“跳过”只影响处理，不会删除数据</span></h2>
      ${iss.length ? `<table class="t"><thead><tr><th style="width:24px"></th><th>问题</th><th>说明与默认处理</th><th class="r">涉及录制</th><th></th></tr></thead><tbody>${iss.map(x => {
        const allSkip = x.rids.every(id => (R.find(r => r.id === id) || {}).skipped);
        return `<tr><td><i class="sdot ${x.lv === 'block' ? 'bad' : 'warn'}"></i></td><td class="nowrap"><b>${esc(x.t)}</b></td><td class="k issd">${x.d}</td>
          <td class="r"><a href="javascript:void 0" data-fi="${esc(x.t)}" class="mono">${x.rids.length}</a></td>
          <td class="r nowrap">${x.lv === 'block' ? '<span class="k">无法处理，自动跳过</span>' : `<button class="btn sm ${allSkip ? '' : 'ghost'}" data-skipi="${esc(x.t)}|${allSkip ? 0 : 1}">${allSkip ? '恢复处理' : '跳过这些录制'}</button>`}</td></tr>`; }).join('')}</tbody></table>`
        : '<div class="bd clean">没有需要处理的问题</div>'}</div>` : '';
  const recsCard = many ? `<div class="card" id="pcrecs"></div>` : '';
  $('#main').innerHTML = batchHead(b, 'precheck', {actions: '<button class="btn ghost" id="rescan">重新预检</button>'}) +
    `<div class="stats">
      <div><div class="k">录制</div><div class="v">${R.length}</div><div class="k">${fmtHours(dur)} · ${fmtB(bytes)}</div></div>
      <div><div class="k">可以处理</div><div class="v c-ok">${runnable.length}</div><div class="k">其中 ${warned} 条有警告</div></div>
      <div><div class="k">无法处理</div><div class="v ${blocked ? 'c-bad' : ''}">${blocked}</div><div class="k">${blocked ? '缺关键数据或格式不认识' : '—'}</div></div>
      <div><div class="k">已跳过</div><div class="v">${skipped}</div><div class="k">${skipped ? '人工设定，可恢复' : '—'}</div></div>
      <div><div class="k">结构分组</div><div class="v">${d.groups.filter(g => !g.unknown).length}</div><div class="k">${b.info.files} 个文件 · 扫描 ${b.info.seconds} s</div></div>
      <div><div class="k">预计处理时间</div><div class="v">${fmtMin(totMin)}</div><div class="k">${d.vlm.model ? esc(d.vlm.model) : '<span class="c-warn">未连接视觉模型</span>'}</div></div></div>
    ${issCard}
    <div class="sect"><h3>结构分组</h3><span class="k">文件结构相同的录制归为一组，共用一份识别结果和处理计划</span></div>
    <div id="pcg"></div>
    ${many ? `<div class="sect"><h3>全部录制</h3><span class="k">点一行查看这条录制的预检详情</span></div>` : ''}${recsCard}
    <div class="dock"><span class="sum">将处理 <b>${runnable.length}</b> 条 · 预计 <b>${fmtMin(totMin)}</b>${blocked ? ` · ${blocked} 条无法处理` : ''}${skipped ? ` · ${skipped} 条已跳过` : ''}</span><span class="sp"></span>${PCR.backend === 'api' ? `<label class="k" ${T('实时：每次调用立刻返回，几小时出结果，原价。<br>批量：请求攒成文件交给阿里云百炼统一处理，价格减半，结果最晚在设定时限内返回（一般 1 到 3 天），适合大批量离线数据。<br>两种方式得到的标注结果一样')}>大模型调用 <select id="vmode"><option value="realtime" ${(b.opts.vlm_mode||'realtime')!=='batch'?'selected':''}>实时接口</option>${PCR.batchOk ? `<option value="batch" ${b.opts.vlm_mode==='batch'?'selected':''}>批量接口（半价）</option>` : `<option value="batch" disabled>批量接口（${esc(PCR.model)} 不支持）</option>`}</select></label>` : ''}<a class="btn" href="#/p/${b.project_id}">稍后</a><button class="btn pri" id="start" ${runnable.length?'':'disabled'}>开始处理</button></div>`;
  renderPC();
  if (many) renderRecTable();
}

function recFiltered() {
  const q = PCR.q.toLowerCase();
  return PCR.recs.filter(r => {
    if (PCR.group && r.group !== PCR.group) return false;
    if (PCR.issue && !r.issues.some(i => i[1] === PCR.issue)) return false;
    if (PCR.filter === 'issue' && !r.issues.some(i => i[0] !== 'info')) return false;
    if (PCR.filter === 'blocked' && !r.blocked) return false;
    if (PCR.filter === 'skipped' && !r.skipped) return false;
    if (PCR.filter === 'ok' && (r.blocked || r.skipped)) return false;
    return !q || r.name.toLowerCase().includes(q) || r.rel.toLowerCase().includes(q) || (r.task || '').toLowerCase().includes(q);
  });
}

const KN = {rgb:'彩色', depth:'深度', hand_kp:'手部关键点', pose:'头部位姿', imu:'IMU', joints:'关节', grip:'夹爪', ft:'力', dex:'灵巧手', audio:'音频'};
function recTableHTML(rows, opts) {
  // opts: {chk:Set, open:rid, detail, kind:'precheck'}
  return `<table class="t"><thead><tr><th style="width:28px"><input type="checkbox" id="chkall"></th><th style="width:30px"></th><th>录制</th><th>结构组</th><th class="r">时长</th><th>数据</th><th>预检问题</th><th>处理</th></tr></thead><tbody>
    ${rows.map(r => { const bad = r.issues.filter(i => i[0] === 'block'), warn = r.issues.filter(i => i[0] === 'warn');
      const st = r.blocked ? tag('无法处理','bad') : r.skipped ? tag('已跳过','mute') : r.status && r.status !== 'scanned' ? tag((RST[r.status]||[r.status])[0], (RST[r.status]||['','mute'])[1]) : tag('可以处理','ok');
      const kinds = [...r.streams.map(k => k === 'depth' && r.depth ? '深度' + r.depth.replace(' ','') : KN[k] || k), ...r.signals.map(k => KN[k] || k)];
      const open = opts.open === r.id;
      return `<tr class="click ${open ? 'cur' : ''}" data-rr="${r.id}"><td><input type="checkbox" data-ck="${r.id}" ${opts.chk.has(r.id)?'checked':''}></td><td>${gradeHTML(r.grade,'sm')}</td>
        <td><b>${esc(r.name)}</b>${r.rel !== r.name && r.rel !== '.' ? `<div class="mono k ell" style="max-width:340px">${esc(r.rel)}</div>` : ''}</td><td class="k">${esc(r.group_name)}</td>
        <td class="r mono">${fmtDur(r.dur)}</td><td class="k">${kinds.map(esc).join(' · ')}</td>
        <td>${bad.length ? `<span class="tag bad" ${T(bad.map(i => esc(i[1])).join('<br>'))}>${bad.length} 项阻断</span> ` : ''}${warn.length ? `<span class="tag warn" ${T(warn.map(i => esc(i[1])).join('<br>'))}>${warn.length} 项警告</span>` : ''}${!bad.length && !warn.length ? '<span class="k">—</span>' : ''}</td>
        <td>${st}</td></tr>${open ? `<tr class="xrow"><td></td><td colspan="7">${recDetailHTML(r, opts.detail[r.id])}</td></tr>` : ''}`; }).join('')}
    </tbody></table>`;
}

function recDetailHTML(r, dd) {
  if (!dd) return '<div class="k" style="padding:8px 0">读取中…</div>';
  const rec = dd.rec;
  const lv = s => s === 'block' ? 'bad' : s === 'warn' ? 'warn' : 'info';
  return `<div class="xdet">
    <div class="thumbs">${[0,1,2,3].map(i => `<img src="/api/media/${PC.bid}/${r.id}/pre_${i}.jpg" onerror="this.style.visibility='hidden'">`).join('')}</div>
    <div class="xcols">
      <div><div class="k">数据流</div>${rec.streams.map(s => `<div class="mono">${esc(s.name)} <span class="k">${esc(KN[s.kind]||s.kind)} · ${s.n} 帧 · ${(s.fps||0).toFixed(0)} fps${s.w ? ' · ' + s.w + '×' + s.h : ''}${s.dtype ? ' · ' + esc(s.dtype) : ''}</span></div>`).join('')}
        ${rec.signals.map(s => `<div class="mono">${esc((s.file||'').split(/[\\/]/).pop())} <span class="k">${esc(KN[s.kind]||s.kind)}</span></div>`).join('')}
        ${rec.task && rec.task.name ? `<div class="k" style="margin-top:6px">任务</div><div>${esc(rec.task.name)}</div>${(rec.task.steps||[]).length ? `<div class="k">${rec.task.steps.map(esc).join('<br>')}</div>` : ''}` : ''}</div>
      <div><div class="k">检查结果</div>${rec.issues.length ? rec.issues.map(([s, h, x]) => `<div class="xiss"><i class="sdot ${lv(s)}"></i><div><b>${esc(h)}</b><div class="k">${x}</div></div></div>`).join('') : '<div class="clean">没有发现问题</div>'}</div>
    </div>
    <div class="row" style="gap:8px;margin-top:8px">${r.blocked ? '' : `<button class="btn sm" data-skip1="${r.id}|${r.skipped ? 0 : 1}">${r.skipped ? '恢复处理' : '跳过这条'}</button>`}${r.status && r.status !== 'scanned' ? `<a class="btn sm" href="#/b/${PC.bid}/browse/${r.id}">打开这条录制</a>` : ''}</div></div>`;
}

function renderRecTable() {
  const el = $('#pcrecs'); if (!el) return;
  const rows = recFiltered(), R = PCR.recs;
  const pages = Math.max(1, Math.ceil(rows.length / PAGE)); PCR.page = Math.min(PCR.page, pages - 1);
  const cnt = f => { const o = PCR.filter; PCR.filter = f; const n = recFiltered().length; PCR.filter = o; return n; };
  const fl = [['all','全部'],['ok','可以处理'],['issue','有问题'],['blocked','无法处理'],['skipped','已跳过']];
  const g = PCR.group ? PC.data.groups.find(x => x.key === PCR.group) : null;
  el.innerHTML = `<div class="tbar"><span class="seg3">${fl.map(([k, t]) => `<button data-f="${k}" class="${PCR.filter===k?'on':''}">${t} <span class="mono">${cnt(k)}</span></button>`).join('')}</span>
      ${g ? `<span class="fchip">结构组：${esc(g.name)} <a href="javascript:void 0" data-clr="group">×</a></span>` : ''}${PCR.issue ? `<span class="fchip">问题：${esc(PCR.issue)} <a href="javascript:void 0" data-clr="issue">×</a></span>` : ''}
      <input type="text" id="rq" placeholder="搜索录制名、路径、任务" value="${esc(PCR.q)}"><span class="sp"></span>
      ${PCR.chk.size ? `<span class="k">已选 ${PCR.chk.size} 条</span><button class="btn sm" data-bulk="1">跳过所选</button><button class="btn sm" data-bulk="0">恢复所选</button>` : ''}</div>
    ${rows.length ? recTableHTML(rows.slice(PCR.page * PAGE, (PCR.page + 1) * PAGE), {chk: PCR.chk, open: PCR.open, detail: PCR.detail}) : '<div class="empty">没有符合条件的录制</div>'}
    ${pages > 1 ? `<div class="pager"><span class="k">${rows.length} 条 · 第 ${PCR.page + 1} / ${pages} 页</span><button class="btn sm" data-pg="-1" ${PCR.page ? '' : 'disabled'}>上一页</button><button class="btn sm" data-pg="1" ${PCR.page < pages - 1 ? '' : 'disabled'}>下一页</button></div>` : ''}`;
  const q = $('#rq'); if (q) { q.oninput = () => { PCR.q = q.value; PCR.page = 0; const p = q.selectionStart; renderRecTable(); const q2 = $('#rq'); q2.focus(); q2.setSelectionRange(p, p); }; }
}

async function setSkip(body) { const r = await api(`/api/batches/${PC.bid}/skip`, {method:'POST', body}); await reloadPC(); return r; }

document.addEventListener('click', async e => {
  if (!onTab('precheck') || !PC.data) return;
  const f = e.target.closest('#pcrecs [data-f]'); if (f) { PCR.filter = f.dataset.f; PCR.page = 0; renderRecTable(); return; }
  const c = e.target.closest('[data-clr]'); if (c) { PCR[c.dataset.clr] = null; renderRecTable(); return; }
  const pg = e.target.closest('[data-pg]'); if (pg) { PCR.page += +pg.dataset.pg; renderRecTable(); return; }
  if (e.target.id === 'chkall') { const rows = recFiltered().slice(PCR.page * PAGE, (PCR.page + 1) * PAGE); rows.forEach(r => e.target.checked ? PCR.chk.add(r.id) : PCR.chk.delete(r.id)); renderRecTable(); return; }
  const ck = e.target.closest('[data-ck]'); if (ck) { ck.checked ? PCR.chk.add(ck.dataset.ck) : PCR.chk.delete(ck.dataset.ck); renderRecTable(); return; }
  const bk = e.target.closest('[data-bulk]'); if (bk) { const r = await setSkip({rids: [...PCR.chk], skip: bk.dataset.bulk === '1'}); PCR.chk.clear(); toast(bk.dataset.bulk === '1' ? `已跳过 ${r.n} 条` : `已恢复 ${r.n} 条`); return; }
  const si = e.target.closest('[data-skipi]'); if (si) { const [t, on] = si.dataset.skipi.split('|'); const r = await setSkip({issue: t, skip: on === '1'}); toast(on === '1' ? `已跳过 ${r.n} 条“${t}”的录制` : `已恢复 ${r.n} 条录制`); return; }
  const s1 = e.target.closest('[data-skip1]'); if (s1) { const [id, on] = s1.dataset.skip1.split('|'); await setSkip({rids: [id], skip: on === '1'}); return; }
  const fi = e.target.closest('[data-fi]'); if (fi) { PCR.issue = fi.dataset.fi; PCR.filter = 'all'; PCR.page = 0; renderRecTable(); $('#pcrecs').scrollIntoView({behavior:'smooth', block:'start'}); return; }
  const gr = e.target.closest('[data-grp]'); if (gr) { PCR.group = gr.dataset.grp; PCR.filter = 'all'; PCR.page = 0; renderRecTable(); $('#pcrecs').scrollIntoView({behavior:'smooth', block:'start'}); return; }
  const rr = e.target.closest('#pcrecs tr[data-rr]');
  if (rr && !e.target.closest('input,a,button')) { const id = rr.dataset.rr; PCR.open = PCR.open === id ? null : id; renderRecTable();
    if (PCR.open && !PCR.detail[id]) { PCR.detail[id] = await api(`/api/recs/${PC.bid}/${id}`); renderRecTable(); } return; }
});


/* ================================================================ 处理（批次标签页） */
const PR = {sel:null, down:false, lastScroll:0, filter:'all', q:'', page:0, chk:new Set(), bid:null};
document.addEventListener('pointerdown', () => { PR.down = true; });
document.addEventListener('pointerup', () => { PR.down = false; });
document.addEventListener('scroll', e => { if (e.target && e.target.id === 'mx') PR.lastScroll = Date.now(); }, true);
const recState = r => { const st = r.steps.filter(s => s.key !== 'idle');
  if (st.some(s => s.status === 'run')) return 'run';
  if (st.some(s => s.status === 'batch')) return 'bwait';
  if (st.some(s => s.status === 'fail')) return 'fail';
  if (st.some(s => s.status === 'wait')) return 'wait';
  if (st.some(s => s.status === 'warn')) return 'warn';
  return st.length ? 'ok' : 'none'; };

const COLS = {'时间同步':'同步', '画面质检':'画质', '关键点核查':'关键点', '相机轨迹':'轨迹', '物体清单':'物体', '物体跟踪':'跟踪', '接触事件':'接触', '事件切点':'切点', '语义标注':'语义', '手部追踪':'手部', '任务识别':'任务', '清单外补找':'补找'};
function stepVal(s) {
  // 格子里只放一个关键数字，完整结果在悬停提示里
  const m = String(s.msg || ''), g = re => { const x = m.match(re); return x ? x[1] : null; };
  const k = s.key;
  if (k === 'ingest') return g(/(\d+)\s*帧/) || '';
  if (k === 'sync') { const v = g(/([+\-−]?[\d.]+)\s*ms/); if (v) return v.replace('-', '−') + ' ms'; const c = g(/互相关弱\s*([\-\d.]+)/); return c ? '相关 ' + c : ''; }
  if (k === 'quality') return g(/([\d.]+%)/) || '';
  if (k === 'kp_verify') { const v = g(/([\d.]+)\s*px/); return v ? v + ' px' : ''; }
  if (k === 'traj') { const v = g(/([\d.]+)\s*m\b/); return v ? v + ' m' : ''; }
  if (k === 'objects') return g(/^(\d+)\s*个/) || '';
  if (k === 'track') return g(/(\d+)\s*帧/) || g(/^(\d+)/) || '';
  if (k === 'contact') return g(/^(\d+)\s*次/) || '';
  if (k === 'cuts' || k === 'semantic') return g(/^(\d+)\s*(?:段|个)/) || '';
  if (k === 'refind') return g(/新增\s*(\d+)/) ? '+' + g(/新增\s*(\d+)/) : m.startsWith('没有清单外') ? '无' : m.startsWith('清单外') ? '0' : '';
  const n = g(/^([\d.]+\s*\S{0,3})/); return n || m.split(/[·，,]/)[0].slice(0, 8);
}
const feedMsg = m => esc(shortText(m)).replace(/\b([0-9a-f]{8})\b/g, '<span class="mono">$1</span>');

async function pageProcess(bid) {
  setLast(bid);
  if (PR.bid !== bid) Object.assign(PR, {bid, sel:null, filter:'all', q:'', page:0, chk:new Set()});
  const main = $('#main');
  main.innerHTML = '<div class="empty">载入中…</div>';
  let last = null;
  const draw = async (force) => {
    if (!force && (PR.down || Date.now() - (PR.lastScroll || 0) < 1500 || (document.activeElement && document.activeElement.id === 'pq'))) return;  // 用户正在拖动、滚动或输入时先不刷新
    const d = await api(`/api/batches/${bid}/progress`);
    last = d;
    const b = d.batch;
    setLastP(b.project && b.project.id);
    const all = d.recs.flatMap(r => r.steps.filter(s => s.key !== 'idle'));
    const work = all.filter(s => s.status !== 'skip');
    const n = k => work.filter(s => s.status === k).length;
    const doneSteps = n('ok') + n('warn') + n('fail');
    const frac = work.length ? (doneSteps + work.filter(s=>s.status==='run').reduce((a,s)=>a+s.progress,0)) / work.length : 0;
    const started = all.map(s => s.started).filter(Boolean), fin = all.map(s => s.finished).filter(Boolean);
    const first = started.length ? Math.min(...started) : null;
    const lastT = n('run') ? Date.now()/1000 : (fin.length ? Math.max(...fin) : null);
    const used = first && lastT ? lastT - first : 0;
    const remain = frac > 0.02 && frac < 1 ? used / frac * (1 - frac) : 0;
    const rc = k => d.recs.filter(r => r.status === k).length;
    const recs = d.recs.filter(r => r.steps.length);
    if (!recs.length) {
      main.innerHTML = batchHead(b, 'process') + `<div class="card empty">这个批次还没开始处理。<br><br><a class="btn pri" href="#/b/${bid}/precheck">去预检页确认并开始</a></div>`;
      return;
    }
    const cell = (r, col) => {
      const s = r.steps.find(x => x.label === col);
      if (!s) return '<td><div class="c none">–</div></td>';
      const cls = {wait:'wait', run:'run', ok:'ok', warn:'warn', fail:'fail', skip:'skip', batch:'bwait'}[s.status];
      const sel = PR.sel && PR.sel[0] === r.id && PR.sel[1] === s.key ? 'sel' : '';
      const skipTxt = /模型|未安装|Ollama|没有|连不上/.test(s.msg || '') ? '跳过' : '不需要';
      const inner = s.status === 'run' ? `<span>${Math.round(s.progress*100)}%</span>`
        : s.status === 'batch' ? `${IC.cloud}<span>${(String(s.msg||'').match(/(\d+)\s*个/) || [,''])[1]}</span>`
        : s.status === 'wait' ? '<i class="dot"></i>'
        : s.status === 'skip' ? `<i class="dot"></i><span>${skipTxt}</span>`
        : s.status === 'fail' ? '<i class="dot"></i><span>失败</span>'
        : `<i class="dot"></i><span>${esc(stepVal(s))}</span>`;
      const st = {wait:'等待',run:'运行中',ok:'完成',warn:'有警告',fail:'失败',skip:skipTxt,batch:'等待批量结果'}[s.status];
      return `<td><div class="c ${cls} ${sel}" style="--p:${s.progress*100}%" data-c="${r.id}|${s.key}" ${T('<b>'+esc(short(r.name))+' · '+esc(s.label)+'</b> · '+st+'<br>'+esc(s.msg||ACT[s.mode]||''))}>${inner}</div></td>`;
    };
    // 筛选 + 搜索 + 分页
    const cnt = {all: recs.length}; recs.forEach(r => { const k = recState(r); cnt[k] = (cnt[k] || 0) + 1; });
    const q = PR.q.toLowerCase();
    const rows = recs.filter(r => (PR.filter === 'all' || recState(r) === PR.filter) && (!q || r.name.toLowerCase().includes(q) || (r.group_name||'').toLowerCase().includes(q)));
    const pages = Math.max(1, Math.ceil(rows.length / PAGE)); PR.page = Math.min(PR.page, pages - 1);
    const vis = rows.slice(PR.page * PAGE, (PR.page + 1) * PAGE);
    let side = '';
    const cur = d.live ? d.recs.find(r => r.id === d.live.rec) : null;
    const cs = cur ? cur.steps.find(s => s.key === d.live.key) : null;
    if (PR.sel) {
      const r = d.recs.find(x => x.id === PR.sel[0]); const s = r && r.steps.find(x => x.key === PR.sel[1]);
      if (s) side = `<div class="card"><h2><span class="mono" style="color:var(--tx)">${esc(short(r.name))}</span> · ${esc(s.label)}<span class="sp"></span>${['ok','warn','fail'].includes(s.status)?`<button class="btn sm" data-rerun="${r.id}|${s.key}">重跑这一步</button>`:''}</h2><div class="bd"><dl class="kv">
        <dt>状态</dt><dd>${{wait:'等待',run:'运行中',ok:'完成',warn:'有警告',fail:'失败',skip:'不需要'}[s.status]}</dd>
        <dt>方式</dt><dd>${ACT[s.mode]||s.mode}</dd>
        <dt>结果</dt><dd>${esc(s.msg||'—')}</dd>
        ${s.started?`<dt>用时</dt><dd>${((s.finished||Date.now()/1000)-s.started).toFixed(1)} s</dd>`:''}</dl>
        ${s.status==='fail'?'<div class="hint">失败原因写在“结果”里；修好后点“重跑这一步”，或顶部“只重跑失败项”。</div>':''}
        <div style="margin-top:8px"><a class="btn sm" href="#/b/${bid}/browse/${r.id}">打开这条录制</a></div></div></div>`;
    }
    const FL = [['all','全部'],['run','处理中'],['bwait','等待批量'],['wait','排队'],['fail','有失败'],['warn','有警告'],['ok','全部正常']];
    const FD = {run:'acc', bwait:'batch', wait:'tx4', fail:'bad', warn:'warn', ok:'ok'};
    const batchMode = b.opts && b.opts.vlm_mode === 'batch';
    const gc = {}; recs.forEach(r => { gc[r.group_name] = (gc[r.group_name] || 0) + 1; });
    const mainGroup = Object.keys(gc).sort((a, c) => gc[c] - gc[a])[0];
    const scr = main.querySelector('#mx'), keepX = scr ? scr.scrollLeft : 0, fd = main.querySelector('.feed'), keepY = fd ? fd.scrollTop : 0;
    const qa = document.activeElement && document.activeElement.id === 'pq' ? document.activeElement.selectionStart : null;
    main.innerHTML = batchHead(b, 'process', {actions:
        (batchMode ? `<span class="tag batch" ${T('这个批次的大模型请求走百炼批量接口，价格减半，结果陆续返回')}>${IC.cloud.replace('<svg', '<svg style="width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:1.8"')}批量接口</span>` : '') +
        (d.paused ? '<button class="btn pri" id="resume">继续</button>' : n('wait')+n('run') ? '<button class="btn" id="pause">暂停</button>' : '') +
        (n('fail') ? '<button class="btn pri" id="retry">只重跑失败项</button>' : '')}) + `
      <div class="phero">
        <div class="prog"><span class="big">${Math.round(frac*100)}%</span><span class="sub">${d.paused ? '<span class="c-warn">已暂停</span>' : ''}<span><b>${recs.length}</b> 条录制</span><span>完成 <b>${rc('done')}</b></span>${rc('failed') ? `<span class="c-bad">失败 <b style="color:inherit">${rc('failed')}</b></span>` : ''}${batchMode ? `<span>等待批量 <b>${work.filter(s => s.status === 'batch').length}</b> 步</span>` : ''}<span>已用 <b>${fmtDur(used)}</b></span><span>${remain ? '预计剩余 <b>≈ ' + fmtDur(remain) + '</b>' : n('wait')+n('run') ? '预计剩余 估算中' : ''}</span></span></div>
        <div class="sbar" ${T(`完成 ${n('ok')} · 警告 ${n('warn')} · 失败 ${n('fail')} · 运行 ${n('run')} · 等待批量 ${n('batch')} · 等待 ${n('wait')}（步）`)}>${[['ok','ok'],['warn','warn'],['fail','bad'],['run','acc'],['batch','batch'],['wait','line2']].map(([k, c]) => n(k) ? `<i style="flex:${n(k)};background:var(--${c})"></i>` : '').join('')}</div>
        <div class="fl">${FL.filter(([k]) => k === 'all' || cnt[k]).map(([k, t]) => `<button data-pf="${k}" class="${PR.filter===k?'on':''}">${FD[k] ? `<i class="dot" style="background:var(--${FD[k]})"></i>` : ''}${t}<em>${cnt[k]||0}</em></button>`).join('')}
          <span class="sp"></span><input type="text" id="pq" placeholder="搜索录制" value="${esc(PR.q)}"></div>
      </div>
      <div class="pgrid">
        <div class="card"><div class="tbar">${PR.chk.size ? `<span class="k">已选 ${PR.chk.size} 条</span><select id="bkstep"><option value="">重跑哪一步…</option>${d.cols.map(c => `<option>${esc(c)}</option>`).join('')}</select><button class="btn sm" id="bkrun">重跑</button><button class="btn sm" id="bkall" ${T('按当前设置重新生成处理计划，整条从头处理')}>整条重新处理</button><button class="btn sm ghost" id="bkclr">取消选择</button>` : ''}</div>
          <div style="overflow-x:auto" id="mx"><table class="m"><tr><th><input type="checkbox" id="pchkall"></th><th>录制</th>${d.cols.map(c=>`<th>${esc(COLS[c] || c)}</th>`).join('')}</tr>
          ${vis.map(r => `<tr><td class="ck"><input type="checkbox" data-pck="${r.id}" ${PR.chk.has(r.id)?'checked':''}></td><td class="name" data-open="${r.id}" ${T('<b>'+esc(r.name)+'</b><br>'+esc(r.group_name)+' · '+fmtDur(r.dur)+'<br>输入质量 '+GR[r.grade][0]+' · '+GR[r.grade][2])}><div class="nm"><span class="mono">${esc(short(r.name))}</span><span class="d">${Math.round(r.dur)}s</span>${r.group_name !== mainGroup ? `<span class="badge">${esc(r.group_name)}</span>` : ''}</div></td>${d.cols.map(c => cell(r, c)).join('')}</tr>`).join('')}</table>
          ${vis.length ? '' : '<div class="empty">没有符合条件的录制</div>'}</div>
          ${pages > 1 ? `<div class="pager"><span class="k">${rows.length} 条 · 第 ${PR.page + 1} / ${pages} 页</span><button class="btn sm" data-ppg="-1" ${PR.page ? '' : 'disabled'}>上一页</button><button class="btn sm" data-ppg="1" ${PR.page < pages - 1 ? '' : 'disabled'}>下一页</button></div>` : ''}</div>
        <div class="side">
          <div class="card"><h2>${cur && cs ? '<i class="dot" style="background:var(--acc)"></i> 正在运行' : '正在运行'}</h2><div class="bd">${cur && cs ? `<div style="font-weight:600">${esc(cs.label)} <span class="mono k" style="font-weight:400">· ${esc(short(cur.name))}</span></div><div class="pbar" style="margin:10px 0 6px"><i style="width:${cs.progress*100}%"></i></div><div class="k mono ell">${esc(cs.msg||'')}</div>` : `<div class="k">${n('wait') ? (d.paused ? '已暂停' : d.other ? (d.other.deleted ? `后台还在结束一个已删除批次的旧任务（${esc(d.other.step)}，已 ${d.other.since} 秒），结束后马上开始` : `排队中：后台正在处理“${esc(d.other.batch)}”的${esc(d.other.step)}（已 ${d.other.since} 秒）。<a href="#/queue">查看任务队列</a>`) : '准备下一步…') : '全部完成'}</div>`}</div></div>
          ${side}
          <div class="card"><h2>动态</h2><ul class="feed">${d.events.map(e => `<li><time>${clock(e.ts).slice(0,5)}</time><span class="t-${e.level}">${feedMsg(e.msg)}</span></li>`).join('') || '<li><span class="k">暂无</span></li>'}</ul></div>
        </div>
      </div>`;
    const scr2 = main.querySelector('#mx'); if (scr2) scr2.scrollLeft = keepX;
    const fd2 = main.querySelector('.feed'); if (fd2) fd2.scrollTop = keepY;
    const pq = $('#pq'); pq.oninput = () => { PR.q = pq.value; PR.page = 0; draw(true).then(() => { const p2 = $('#pq'); p2.focus(); p2.setSelectionRange(p2.value.length, p2.value.length); }); };
    if (qa != null) { pq.focus(); pq.setSelectionRange(qa, qa); }
  };
  every(draw, 1500);
  main.onclick = async e => {
    const c = e.target.closest('[data-c]'); if (c) { PR.sel = c.dataset.c.split('|'); draw(true); return; }
    const o = e.target.closest('[data-open]'); if (o) { location.hash = `#/b/${bid}/browse/${o.dataset.open}`; return; }
    const rr = e.target.closest('[data-rerun]'); if (rr) { const [rid, key] = rr.dataset.rerun.split('|'); await api(`/api/recs/${bid}/${rid}/rerun`, {method:'POST', body:{key}}); toast('已重新排队'); draw(true); return; }
    const pf = e.target.closest('[data-pf]'); if (pf) { PR.filter = pf.dataset.pf; PR.page = 0; draw(true); return; }
    const pg = e.target.closest('[data-ppg]'); if (pg) { PR.page += +pg.dataset.ppg; draw(true); return; }
    const ck = e.target.closest('[data-pck]'); if (ck) { ck.checked ? PR.chk.add(ck.dataset.pck) : PR.chk.delete(ck.dataset.pck); draw(true); return; }
    if (e.target.id === 'pchkall') { document.querySelectorAll('[data-pck]').forEach(x => e.target.checked ? PR.chk.add(x.dataset.pck) : PR.chk.delete(x.dataset.pck)); draw(true); return; }
    if (e.target.id === 'bkclr') { PR.chk.clear(); draw(true); return; }
    if (e.target.id === 'bkrun') {
      const lab = $('#bkstep').value; if (!lab) { toast('先选要重跑的步骤'); return; }
      let k = 0; for (const rid of PR.chk) { const r = last.recs.find(x => x.id === rid); const s = r && r.steps.find(x => x.label === lab);
        if (s && s.status !== 'skip') { await api(`/api/recs/${bid}/${rid}/rerun`, {method:'POST', body:{key: s.key}}); k++; } }
      toast(`已把 ${k} 条录制的“${lab}”重新排队`); PR.chk.clear(); draw(true); return; }
    if (e.target.id === 'bkall') {
      if (!confirm(`按当前设置重新处理所选 ${PR.chk.size} 条录制？人工修改过的分段会被覆盖。`)) return;
      for (const rid of PR.chk) await api(`/api/recs/${bid}/${rid}/replan`, {method:'POST'});
      toast(`已重新排队 ${PR.chk.size} 条`); PR.chk.clear(); draw(true); return; }
    if (e.target.id === 'pause') { await api(`/api/batches/${bid}/pause`, {method:'POST'}); draw(true); }
    if (e.target.id === 'resume') { await api(`/api/batches/${bid}/resume`, {method:'POST'}); draw(true); }
    if (e.target.id === 'retry') { await api(`/api/batches/${bid}/retry`, {method:'POST'}); draw(true); }
  };
}

/* ================================================================ 浏览 */
const BR = {d:null, bid:null, rid:null, hands:null, polys:null, segs:[], sel:0, dirty:false, raf:null, showKp:true, showDepth:true, depthBig:false, showObj:true, lf:'all', lq:'', b:null};
/* 分段颜色：同一个动作词永远同一种颜色；“静止”和未标注是灰色 */
const segCls = (s, i) => { const v = s.verb || s.action || ''; if (!v || v === '静止') return 0; let h = 0; for (const c of v) h = (h * 31 + c.charCodeAt(0)) >>> 0; return 1 + h % 6; };
const segBg = (s, i) => `var(--s${segCls(s, i)})`, segFg = (s, i) => `var(--s${segCls(s, i)}t)`;
const segLabel = s => s.verb ? s.verb + (s.object_id ? ' · ' + s.object_id : '') : (s.action || '·');
const QC = {blur:'var(--warn)', dark:'var(--tx4)', bright:'#D8D2B8', frozen:'var(--tx3)'};
const OBC = ['#5AA0FA','#3FCF8E','#F3B34C','#B794FF','#3DD6F5','#F2706B','#C9C25A','#8796FF','#F38CC4','#7FD68A'];
const QN = {blur:'模糊', dark:'过暗', bright:'过曝', frozen:'画面冻结'};

async function pageBrowse(bid, rid) {
  if (!BR.vocab) api('/api/vocab').then(v => { BR.vocab = v.items; }).catch(() => {});
  setLast(bid);
  if (!BR.b || BR.b.id !== bid) { BR.b = await api('/api/batches/' + bid); BR.lf = 'all'; BR.lq = ''; }
  setLastP(BR.b.project && BR.b.project.id);
  if (!rid) {
    const p = await api(`/api/batches/${bid}/progress`);
    const r = p.recs.find(r => r.status === 'done') || p.recs.find(r => r.steps.length) || p.recs[0];
    if (!r) { $('#main').innerHTML = batchHead(BR.b, 'browse') + '<div class="empty">这个批次没有录制。</div>'; return; }
    location.replace(`#/b/${bid}/browse/${r.id}`); return;
  }
  if (BR.raf) cancelAnimationFrame(BR.raf);
  Object.assign(BR, {bid, rid, hands:null, sel:0, dirty:false});
  const d = await api(`/api/recs/${bid}/${rid}`);
  BR.d = d;
  BR.segs = d.segments ? JSON.parse(JSON.stringify(d.segments.segments)) : [];
  BR.oe = {add: [], set: {}, del: []}; BR.drawing = null;
  renderBrowse();
  if (d.has_hands) { BR.hands = await api(`/api/recs/${bid}/${rid}/hands2d`); drawTracks(); }
  // 处理没完成时定期刷新右侧步骤
  every(async () => { if (!BR.d) return; const st = BR.d.steps; if (st.some(s => s.status === 'wait' || s.status === 'run')) { const d2 = await api(`/api/recs/${bid}/${rid}`); const changed = JSON.stringify(d2.steps) !== JSON.stringify(st); BR.d.steps = d2.steps; if (changed) { if (!BR.dirty) { BR.d = d2; BR.segs = d2.segments ? d2.segments.segments : []; renderBrowse(); if (d2.has_hands && !BR.hands) BR.hands = await api(`/api/recs/${bid}/${rid}/hands2d`); } else renderSide(); } } }, 2500);
}

function recListHTML() {
  const d = BR.d, q = BR.lq.toLowerCase();
  const isRev = o => o.review > 0 || o.verdict === '不一致';
  const L = d.others.filter(o => (BR.lf === 'all' || (BR.lf === 'review' && isRev(o)) || (BR.lf === 'fail' && o.status === 'failed') || (BR.lf === 'done' && o.status === 'done' && !isRev(o)) || (BR.lf === 'todo' && !['done','failed'].includes(o.status))) && (!q || o.name.toLowerCase().includes(q)));
  const n = f => d.others.filter(o => f === 'review' ? isRev(o) : f === 'fail' ? o.status === 'failed' : f === 'done' ? o.status === 'done' && !isRev(o) : !['done','failed'].includes(o.status)).length;
  return `<div class="lhead2"><input type="text" id="lq" placeholder="搜索录制" value="${esc(BR.lq)}"><select id="lf"><option value="all">全部 ${d.others.length}</option><option value="review" ${BR.lf==='review'?'selected':''}>需复核 ${n('review')}</option><option value="fail" ${BR.lf==='fail'?'selected':''}>有失败 ${n('fail')}</option><option value="done" ${BR.lf==='done'?'selected':''}>已完成 ${n('done')}</option><option value="todo" ${BR.lf==='todo'?'selected':''}>未完成 ${n('todo')}</option></select></div>
    <div class="rscroll">${L.map(o => `<div class="ritem ${o.id===BR.rid?'on':''}" data-rec="${o.id}" ${T('<b>'+esc(o.name)+'</b><br>'+(RST[o.status]||[o.status])[0]+' · 输入质量 '+GR[o.grade][0])}><i class="sdot ${o.status==='done'?(o.review || o.verdict === '不一致' ? 'warn' : 'ok'):o.status==='failed'?'bad':o.status==='running'||o.status==='queued'?'run':'mute'}"></i><span class="nm">${esc(short(o.name))}</span>${o.review ? `<span class="tag warn" ${T(o.review + ' 段需要人工复核（模型失败、词不在词表或未标注）')}>复核 ${o.review}</span>` : o.verdict === '不一致' ? `<span class="tag warn" ${T('任务核对：不一致')}>任务</span>` : ''}${o.edited ? `<span class="k" ${T('有人工修改')}>✎</span>` : ''}</div>`).join('') || '<div class="k" style="padding:10px">没有符合条件的录制</div>'}</div>`;
}

function renderBrowse() {
  const d = BR.d, r = d.rec, ing = d.ingest;
  let center;
  if (!ing) {
    center = `<div class="card empty">这条录制还没处理完“入库”这一步，暂时不能播放。<br><br>
      <span class="thumbs" style="justify-content:center">${[0,1,2,3].map(i=>`<img style="width:120px;height:90px" src="/api/media/${BR.bid}/${BR.rid}/pre_${i}.jpg" onerror="this.style.display='none'">`).join('')}</span><br><a class="btn" href="#/b/${BR.bid}/process">查看处理进度</a></div>`;
  } else {
    const dep = ing.depth_preview && BR.showDepth, two = dep && BR.depthBig;
    const dlab = `深度${ing.depth_range ? ' ' + ing.depth_range[0].toFixed(2) + '–' + ing.depth_range[1].toFixed(2) + ' m' : ''}`;
    const dvid = `<video id="vd" src="/api/media/${BR.bid}/${BR.rid}/preview_depth.mp4" preload="auto" playsinline muted></video><span class="ovl">${dlab}</span>`;
    const rgbs = r.streams.find(x => x.kind === 'rgb') || {};
    const ar = rgbs.w && rgbs.h ? rgbs.w / rgbs.h : 4 / 3;
    center = `<div class="stage ${two?'two':''}" style="--ar:${(two ? ar * 1.67 : ar).toFixed(3)}">
 <div class="vwrap" style="aspect-ratio:${rgbs.w && rgbs.h ? rgbs.w + '/' + rgbs.h : '4/3'}"><video id="vr" src="/api/media/${BR.bid}/${BR.rid}/rgb" preload="auto" playsinline muted></video><canvas id="cv"></canvas>
          <span class="ovl lab"><i class="dot" style="background:var(--acc)"></i>彩色${rgbs.fps ? ' · ' + Math.round(rgbs.fps) + ' fps' : ''}</span>
          <div class="tools">${d.has_hands ? `<button id="kp" class="${BR.showKp?'on':''}" ${T('手部关键点')}>${IC.hand}</button>` : ''}${d.track ? `<button id="ob" class="${BR.showObj?'on':''}" ${T('物体轮廓')}>${IC.obj}</button>` : ''}${ing.depth_preview ? `<button id="dp" class="${BR.showDepth?'on':''}" ${T('深度画面')}>${IC.depth}</button>` : ''}</div>
          ${dep && !two ? `<div class="pip" id="pip" ${T('点击放大，和彩色并排')}>${dvid}</div>` : ''}
          <div class="cap" id="cap" style="display:none"></div></div>
        ${two ? `<div class="pip" id="pip" ${T('点击缩回画中画')}>${dvid}</div>` : ''}
      </div>
      <div class="ctrl"><button class="ib" data-step="-1" ${T('上一段')}>${IC.prev}</button><button class="play" id="play" ${T('播放 / 暂停 <kbd>空格</kbd>')}>${IC.play}</button><button class="ib" data-step="1" ${T('下一段')}>${IC.next}</button>
        <span class="time" id="tm">00:00.00 <span>/ ${tcode(r.duration)}</span></span>
        <span class="sp"></span>
        <span class="k" ${T('<kbd>空格</kbd> 播放　<kbd>←</kbd> <kbd>→</kbd> 逐帧<br><kbd>S</kbd> 在播放头处切开　<kbd>Ctrl</kbd>+<kbd>S</kbd> 保存')}><kbd>?</kbd></span>
        <select id="rate"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select></div>
      <div class="tl" id="tl"></div>
      <div class="card" style="margin-top:16px"><h2>分段标注<span class="sp"></span><span class="k" id="segsum"></span></h2><div id="seglist"></div></div>`;
  }
  const dep = r.streams.find(s => s.kind === 'depth');
  const nIss = r.issues.filter(i => i[0] !== 'info').length;
  const qall = [dep ? (dep.dtype === 'uint16' ? '深度 16 位' : '深度 8 位（相对值）') : '无深度',
    d.kp && d.kp.median_px != null ? `关键点误差 ${d.kp.median_px.toFixed(1)} px` : '',
    d.sync && d.sync.depth_t_offset != null ? `深度同步 ${(d.sync.depth_t_offset*1000).toFixed(0)} ms` : '',
    nIss ? `预检 ${nIss} 项警告` : ''].filter(Boolean);
  // 只把不正常的项摆出来；都正常时一个“质检正常”，明细在悬停提示里
  const qbad = [dep && dep.dtype !== 'uint16' ? '深度 8 位' : !dep ? '无深度' : '', d.kp && d.kp.median_px > 15 ? `关键点误差 ${d.kp.median_px.toFixed(0)} px` : '',
    d.sync && Math.abs(d.sync.depth_t_offset || 0) > 0.05 ? `深度偏移 ${(d.sync.depth_t_offset*1000).toFixed(0)} ms` : '', nIss ? `预检 ${nIss} 项警告` : ''].filter(Boolean);
  const t = d.task || {};
  $('#main').innerHTML = batchHead(BR.b, 'browse') + `
    <div class="rbar"><b ${T(esc(r.name))}>${esc(short(r.name))}</b><span class="k mono">${fmtDur(r.duration)}</span>
      <a class="qline ${qbad.length ? 'bad' : ''}" href="#/b/${BR.bid}/precheck" ${T(qall.map(esc).join('<br>') + '<br><span class=tk>点开看完整预检结果</span>')}><i class="dot" style="background:var(--${qbad.length ? 'warn' : 'ok'})"></i>${qbad.length ? qbad.map(esc).join('<i>·</i>') : '质检正常'}</a>
      ${t.verdict ? `<span class="qline ${t.verdict === '不一致' ? 'bad' : ''}" ${T('通读全部分段后核对任务' + (t.reason ? '<br>' + esc(t.reason) : ''))}>任务${esc(t.verdict)}</span>` : ''}
      <span class="sp"></span>
      <span id="savest" class="k"></span>${needRedo() ? '' : '<button class="btn ghost" id="redo">重新处理</button>'}<button class="btn pri" id="save" ${BR.dirty?'':'disabled'}>保存修改</button></div>
    ${needRedo() ? `<div class="fix" style="margin-bottom:12px"><span>${needRedo()}</span><span class="sp"></span><button class="btn pri" id="redo">重新处理这条录制</button></div>` : ''}
    <div class="bgrid">
      <div class="card rlist">${recListHTML()}</div>
      <div>${center}</div>
      <div class="inspect" id="side"></div>
    </div>`;
  renderSide();
  if (ing) setupPlayer();
}

function curSeg() { return BR.segs[BR.sel]; }
function needRedo() {
  const st = BR.d.steps, fail = st.filter(s => s.status === 'fail'), busy = st.some(s => s.status === 'wait' || s.status === 'run');
  if (busy) return '正在处理中，完成后自动刷新…';
  const empty = BR.segs.length && !BR.segs.some(s => s.action);
  if (fail.length) return `有 ${fail.length} 步失败（${fail.map(s => s.label).join('、')}）` + (empty ? '，分段还没有语义标注' : '');
  if (empty) return '分段还没有语义标注';
  return '';
}

function renderSide() {
  const d = BR.d, r = d.rec, t = d.task || {};
  const s = curSeg();
  const tname = t.user_name || (r.task && r.task.name) || t.name || '';
  const succ = () => `<span class="seg3" id="succ"><button data-s="true" class="${s&&s.success===true?'on':''}">完成</button><button data-s="false" class="${s&&s.success===false?'on':''}">未完成</button><button data-s="null" class="${s&&(s.success===null||s.success===undefined)?'on':''}">不确定</button></span>`;
  const H = {left:['L','var(--left)','左手'], right:['R','var(--right)','右手']};
  const facts = s && s.facts && s.facts.length ? `<div class="facts">${s.facts.map(e => `<div class="fact" ${T(`${H[e.hand][2]}接触 ${esc(e.obj)} ${esc(e.label||'')}${e.kind?' · '+esc(e.kind):''}${e.moved?'<br>移动 '+(e.disp_cm!=null?e.disp_cm+' cm':''):'<br>位置基本没变'}`)}><span class="h" style="background:${H[e.hand][1]}">${H[e.hand][0]}</span><span><b>${esc(e.obj)}</b> ${esc(e.kind || (e.moved ? '移动' : '接触'))}</span><span class="m">${e.t0.toFixed(1)}–${e.t1.toFixed(1)} s${e.moved && e.disp_cm != null ? ' · ' + e.disp_cm + ' cm' : ''}</span></div>`).join('')}</div>` : '';
  const segCard = BR.segs.length ? `<div class="card"><h2>分段<span class="mono k" style="font-weight:400">${BR.sel+1} / ${BR.segs.length}</span><span class="sp"></span><button class="ib" data-si="${Math.max(0, BR.sel-1)}" ${BR.sel?'':'disabled'}>${IC.prev}</button><button class="ib" data-si="${Math.min(BR.segs.length-1, BR.sel+1)}" ${BR.sel<BR.segs.length-1?'':'disabled'}>${IC.next}</button></h2><div class="bd seged">
      <div class="src"><span class="mono">${s.t0.toFixed(2)} – ${s.t1.toFixed(2)} s</span><span class="mono">${(s.t1-s.t0).toFixed(2)} s</span></div>
      <div class="row"><label>动作</label><input type="text" id="sa" list="vlist" value="${esc(s.verb !== undefined ? s.verb : s.action)}" placeholder="从词表选，例如 拿起"><datalist id="vlist">${(BR.vocab||[]).map(v => `<option value="${esc(v.verb)}">${esc(v.def||'')}</option>`).join('')}</datalist></div>
      ${s.description !== undefined ? `<div class="row"><label>手</label><input type="text" id="sh" value="${esc(s.hand||'')}" placeholder="左手 / 右手 / 双手"></div>` : ''}
      <div class="row"><label>物体</label><input type="text" id="so" value="${esc(s.object)}"></div>
      ${s.description !== undefined ? `<div class="row"><label>去向</label><input type="text" id="st" value="${esc(s.target||'')}"></div>
      <div class="row"><label>描述</label><input type="text" id="sd" value="${esc(s.description||'')}"></div>` : ''}
      <div class="row"><label ${s.evidence ? T('完成依据：' + esc(s.evidence)) : ''}>结果</label>${succ()}</div>
      ${facts}
      <div class="acts"><button class="btn sm" id="split" ${T('在播放头处切开 <kbd>S</kbd>')}>${IC.split}切开</button><button class="btn sm" id="merge" ${BR.sel<BR.segs.length-1?'':'disabled'}>${IC.merge}与下一段合并</button></div>
      ${s.err ? `<div class="k" style="color:var(--bad)">模型调用失败：${esc(s.err)}</div>` : ''}
      <div class="src"><span>${s.src==='vlm' ? esc(s.model||'模型') + (s.conf!=null?' · 置信 '+Number(s.conf).toFixed(2):'') : s.src==='manual' ? '人工修改' : s.src==='rule' ? '规则判断' : '自动切点，未标注'}</span><span ${T('切点依据')}>${esc(s.cut||'')}</span></div>
    </div></div>` : `<div class="card"><h2>分段</h2><div class="bd k">还没有分段。处理到“事件切点”后会出现。</div></div>`;
  let traj = '';
  if (d.traj && d.traj.xyz.length > 2) {
    const P = d.traj.xyz.map(p => [p[0], p[2]]);
    const g = trajGeom(P);
    const pts = P.map(p => g.f(p).join(',')).join(' ');
    const ex = d.traj.extent_m, cm = v => (v*100).toFixed(1);
    traj = `<div class="traj"><div class="bd"><div class="k mono" style="margin-bottom:6px" ${T('设备记录的头部移动范围（左右 × 上下 × 前后）')}>范围 ${cm(ex[0])}×${cm(ex[1])}×${cm(ex[2])} cm</div><svg viewBox="0 0 300 150"><polyline points="${pts}" fill="none" stroke="var(--acc)" stroke-width="1.3"/><circle id="tdot" r="3.5" fill="var(--right)" cx="0" cy="0"/><line x1="12" y1="140" x2="${12 + g.sc*0.1}" y2="140" stroke="var(--tx2)" stroke-width="1.5"/><text x="${16 + g.sc*0.1}" y="143" font-size="10" fill="var(--tx3)">10 cm</text></svg>
      <div class="hint" style="margin:6px 0 0">来自设备自带的头部 6 自由度位姿文件。比例尺固定，画面最小对应 30 cm 见方。</div></div></div>`;
  }
  const steps = `<div class="steps-mini">${d.steps.map(s => `<div><span>${esc(s.label)}</span><span class="tag ${{ok:'ok',warn:'warn',fail:'bad',run:'run',wait:'mute',skip:'mute',batch:'batch'}[s.status]}">${{ok:'完成',warn:'警告',fail:'失败',run:'运行',wait:'等待',skip:'跳过',batch:'等待批量'}[s.status]}</span><span class="m" ${T(esc(s.msg||''))}>${esc(s.msg||'')}</span>${['task','semantic','refind'].includes(s.key)&&s.status!=='skip'&&s.status!=='wait'&&s.status!=='run'?`<button class="btn sm ghost" data-rr="${s.key}">重跑</button>`:''}</div>`).join('')}</div>`;
  const vlmc = t.raw ? `<div class="vlmraw" ${T('模型原始输出')}>${esc(t.raw)}</div>` : '';
  const verdict = t.verdict ? `<div class="k">通读核对：<span style="color:${t.verdict==='不一致'?'var(--warn)':'var(--ok)'}">${esc(t.verdict)}</span>${t.seen && t.seen !== tname ? '，看到的是“'+esc(t.seen)+'”' : ''}</div>` : t.consistent!=null ? `<div class="k">模型核对：${t.consistent ? '<span style="color:var(--ok)">与画面一致</span>' : '<span style="color:var(--warn)">与画面不一致</span>'}${t.seen?'，看到的是“'+esc(t.seen)+'”':''}</div>` : t.generated && t.seen ? `<div class="k">数据里没有任务名，由模型通读后给出</div>` : '';
  const gs = segGroups(), cg = s ? gs.find(g => BR.sel >= g.from && BR.sel <= g.to) : null;
  $('#side').innerHTML = `
    ${segCard}
    <div class="card"><h2>任务<span class="sp"></span>${gradeHTML(d.grade,'sm')}</h2><div class="bd seged">
      <div class="row"><label>名称</label><input type="text" id="tname" value="${esc(tname)}" placeholder="未命名"></div>
      ${cg && gs.some(g => g.to > g.from) ? `<div class="row"><label>子任务</label><span>${esc(cg.title)}</span></div>` : ''}
      ${verdict}${vlmc}</div></div>
    ${d.objects ? `<details class="card fold" ${BR.foldObj === false ? '' : 'open'}><summary>物体<span class="sp"></span><span class="k" ${T(d.objects.info && d.objects.info.calls ? d.objects.info.calls.map(c => esc(c.what)+' '+c.sec+' 秒').join('<br>') + (d.objects.info.unmatched && d.objects.info.unmatched.length ? '<br>词表里没有的名词：'+esc(d.objects.info.unmatched.join('、')) : '') : esc(d.objects.model||''))}>${d.objects.objects.length} 个</span></summary><div class="bd">${objEditor(d)}<img class="inv" style="margin-top:12px" src="/api/media/${BR.bid}/${BR.rid}/objects.jpg"></div></details>` : ''}
    <details class="card fold" ${BR.foldSteps ? 'open' : ''}><summary>处理步骤<span class="sp"></span><span class="k">${d.steps.filter(s => s.status === 'fail').length ? '<span class="c-bad">有失败</span>' : d.steps.filter(s => ['ok','warn'].includes(s.status)).length + ' / ' + d.steps.filter(s => s.status !== 'skip').length}</span></summary><div class="bd">${steps}</div></details>
    ${traj ? `<details class="card fold" ${BR.foldTraj ? 'open' : ''}><summary>头部位置（俯视）</summary>${traj}</details>` : ''}`;
}

/* ---------------- 物体清单人工修改：改名、重新框选、删除、在画面上框选新物体 ---------------- */
const oePending = () => BR.oe ? BR.oe.add.length + Object.keys(BR.oe.set).length + BR.oe.del.length : 0;
function objEditor(d) {
  const tr = d.track, idx = id => tr ? tr.objects.findIndex(o => o.id === id) : -1, oe = BR.oe || {add: [], set: {}, del: []};
  const rows = d.objects.objects.map((o, k) => { const del = oe.del.includes(o.id), e = oe.set[o.id] || {}, ci = idx(o.id) >= 0 ? idx(o.id) : k;
    const how = o.found === '补找' ? `<span class="tag warn" ${T('语义标注发现它不在清单里，自动在 ' + Number(o.t || 0).toFixed(1) + ' 秒' + (o.seg != null ? '（第 ' + (o.seg + 1) + ' 段）' : '') + '框出并补进清单。请看一眼框得对不对：不对就重新框选或删除')}>补找</span>` : o.found === '人工' ? `<span class="tag mute" ${T('人工' + (o.t ? '在 ' + Number(o.t).toFixed(1) + ' 秒' : '') + '框出')}>人工</span>` : o.found && o.found !== '整图' && o.found !== '任务' ? `<span class="k" ${T('看整张图时没找到，放大后找到')}>放大</span>` : o.found === '任务' ? `<span class="k" ${T('任务说明里提到、单独补找到的')}>任务</span>` : '';
    return `<div class="orow ${del ? 'del' : ''}"><i class="ic" style="background:${OBC[ci % OBC.length]}"></i><b>${esc(o.id)}</b><span class="nm">${esc(e.label || o.label)}${e.label ? ' <span class="k">（改名）</span>' : ''}${e.bbox ? ' <span class="k">（重新框选）</span>' : ''}</span>${how}
      ${del ? `<button class="ib" data-oundo="${esc(o.id)}" ${T('撤销删除')}>↺</button>` : `<button class="ib" data-oren="${esc(o.id)}" ${T('改名')}>✎</button><button class="ib" data-obox="${esc(o.id)}" ${T('在当前画面上重新框选它（从这一帧起向前、向后重新跟踪）')}>${IC.obj}</button><button class="ib" data-odel="${esc(o.id)}" ${T('删除')}>×</button>`}</div>`; }).join('');
  const adds = oe.add.map((a, i) => `<div class="orow new"><i class="ic" style="background:var(--acc)"></i><b>新</b><span class="nm">${esc(a.label)} <span class="k mono">${a.t.toFixed(1)} s</span></span><button class="ib" data-ocan="${i}" ${T('取消这条新增')}>×</button></div>`).join('');
  const n = oePending();
  return `<div class="olist">${rows}${adds}</div>
    <div class="acts" style="margin-top:10px"><button class="btn sm" id="oadd" ${T('暂停在物体完整露出的那一帧，在画面上拖出一个框')}>${IC.obj}框选新物体</button></div>
    ${n ? `<div class="fix" style="margin-top:10px"><span>${n} 处修改未应用</span><span class="sp"></span><button class="btn sm ghost" id="ocancel">放弃</button><button class="btn sm pri" id="oapply" ${T('重新跟踪、判断接触、切段和语义标注。没受影响的分段直接用之前的结果')}>应用并重新处理</button></div>` : ''}`;
}
function startDraw(kind, id) {
  const v = $('#vr'), wrap = v && v.closest('.vwrap'); if (!wrap) return;
  v.pause(); const pb = $('#play'); if (pb) pb.innerHTML = IC.play;
  BR.drawing = {kind, id};
  wrap.closest('.stage').classList.add('drawing');
  const lay = document.createElement('div'); lay.className = 'drawlay'; lay.id = 'drawlay';
  lay.innerHTML = `<span class="ovl dhint">${kind === 'box' ? '重新框选 ' + esc(id) : '框选新物体'} · 在画面上拖出一个框 · Esc 取消</span><div class="drect" style="display:none"></div>`;
  wrap.appendChild(lay);
  const cv = $('#cv'), rect = lay.querySelector('.drect'); let p0 = null;
  const pos = e => { const r = cv.getBoundingClientRect(); return [Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))]; };
  lay.onmousedown = e => { p0 = pos(e); rect.style.display = ''; Object.assign(rect.style, {left: p0[0] * 100 + '%', top: p0[1] * 100 + '%', width: 0, height: 0}); e.preventDefault(); };
  lay.onmousemove = e => { if (!p0) return; const p = pos(e); Object.assign(rect.style, {left: Math.min(p0[0], p[0]) * 100 + '%', top: Math.min(p0[1], p[1]) * 100 + '%', width: Math.abs(p[0] - p0[0]) * 100 + '%', height: Math.abs(p[1] - p0[1]) * 100 + '%'}); };
  lay.onmouseup = async e => { if (!p0) return; const p = pos(e), a = p0; p0 = null;
    const src = ((BR.d.track || {}).src_size || [])[0] ? BR.d.track.src_size : [cv.width, cv.height];
    const bbox = [Math.min(a[0], p[0]) * src[0], Math.min(a[1], p[1]) * src[1], Math.max(a[0], p[0]) * src[0], Math.max(a[1], p[1]) * src[1]].map(x => Math.round(x * 10) / 10);
    if (bbox[2] - bbox[0] < 6 || bbox[3] - bbox[1] < 6) { rect.style.display = 'none'; return; }
    const t = v.currentTime, dr = BR.drawing; stopDraw();
    if (dr.kind === 'add') { const label = await ask('这个物体叫什么？', {placeholder: '例如 红色电池、剪刀', ok: '加入清单'}); if (!label) { drawKp(Math.round(t * (BR.d.ingest.rgb_fps || 30)), true); return; } BR.oe.add.push({label, t, bbox}); }
    else BR.oe.set[dr.id] = {...(BR.oe.set[dr.id] || {}), bbox, t};
    renderSide(); drawKp(Math.round(t * (BR.d.ingest.rgb_fps || 30)), true); };
}
function stopDraw() { BR.drawing = null; const l = $('#drawlay'); if (l) l.remove(); const st = document.querySelector('.stage.drawing'); if (st) st.classList.remove('drawing'); }
async function applyObjEdits() {
  if (BR.dirty && !confirm('分段有未保存的修改，应用物体修改后会重新切段，这些修改会丢失。继续？')) return;
  if (BR.segs.some(s => s.src === 'manual') && !confirm('重新切段会覆盖人工修改过的分段，继续？')) return;
  const oe = BR.oe, body = {add: oe.add, set: oe.set, delete: oe.del};
  try { const r = await api(`/api/recs/${BR.bid}/${BR.rid}/objects`, {method: 'POST', body}); toast(`物体清单已更新，正在重新处理：${r.rerun.length} 步`); }
  catch (err) { toast(err.message); return; }
  BR.oe = {add: [], set: {}, del: []}; BR.dirty = false;
  BR.d = await api(`/api/recs/${BR.bid}/${BR.rid}`); BR.segs = BR.d.segments ? JSON.parse(JSON.stringify(BR.d.segments.segments)) : [];
  renderBrowse();
}

function trajGeom(P) {
  // 固定比例：至少按 30 cm 见方显示，避免几厘米的晃动被放大成满屏
  const xs = P.map(p=>p[0]), ys = P.map(p=>p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0.3);
  const sc = 120 / span;
  return { sc, f: p => [+(150 + (p[0]-cx)*sc).toFixed(1), +(75 - (p[1]-cy)*sc).toFixed(1)] };
}

function setupPlayer() {
  const v = $('#vr'), vd = $('#vd'), cv = $('#cv'), ing = BR.d.ingest;
  const off = BR.d.sync ? BR.d.sync.depth_t_offset : 0;
  const fps = ing.rgb_fps || 30;
  v.addEventListener('loadedmetadata', () => { cv.width = v.videoWidth; cv.height = v.videoHeight; drawTracks(); drawKp(Math.round(v.currentTime * fps), true); });
  drawTracks();
  const tick = () => {
    if (!document.body.contains(v)) { BR.raf = null; return; }  // 已离开浏览页
    const t = v.currentTime;
    $('#tm').innerHTML = `${tcode(t)} <span>/ ${tcode(BR.d.rec.duration)}</span>`;
    if (vd) { const want = Math.max(0, t + off); if (v.paused ? Math.abs(vd.currentTime - want) > 0.01 : Math.abs(vd.currentTime - want) > 0.12) vd.currentTime = want; if (v.paused && !vd.paused) vd.pause(); if (!v.paused && vd.paused) vd.play().catch(()=>{}); vd.playbackRate = v.playbackRate; }
    const ph = $('#ph'); if (ph) ph.style.left = `calc(84px + (100% - 96px) * ${t / BR.d.rec.duration})`;
    // 当前时刻落在哪一段
    const i = BR.segs.findIndex(s => t >= s.t0 && t < s.t1);
    if (i >= 0 && i !== BR.sel && (!v.paused || !['INPUT','SELECT','TEXTAREA'].includes((document.activeElement || {}).tagName))) { BR.sel = i; renderSide(); markSel(); }
    const cap = $('#cap'), sg = BR.segs[i];
    if (cap) { const txt = sg ? capHTML(sg, i) : '';
      if (cap._t !== txt) { cap._t = txt; cap.innerHTML = txt; cap.style.display = txt ? '' : 'none'; } }
    drawKp(Math.round(t * fps));
    const dot = $('#tdot');
    if (dot && BR.d.traj) { const tr = BR.d.traj; let k = tr.t.findIndex(x => x >= t); if (k < 0) k = tr.t.length - 1; const P = tr.xyz.map(p => [p[0], p[2]]); const g = trajGeom(P); const [cx, cy] = g.f(P[k]); dot.setAttribute('cx', cx); dot.setAttribute('cy', cy); }
    BR.raf = requestAnimationFrame(tick);
  };
  BR.raf = requestAnimationFrame(tick);
  $('#play').onclick = () => { if (v.paused) { v.play(); $('#play').innerHTML = IC.pause; } else { v.pause(); $('#play').innerHTML = IC.play; } };
  v.onended = () => { $('#play').innerHTML = IC.play; };
  $('#rate').onchange = e => { v.playbackRate = +e.target.value; };
  const keepT = fn => { const t = v.currentTime; fn(); renderBrowse(); setTimeout(() => { const v2 = $('#vr'); if (v2) v2.currentTime = t; }, 50); };
  const kp = $('#kp'); if (kp) kp.onclick = () => { BR.showKp = !BR.showKp; kp.classList.toggle('on', BR.showKp); drawKp(Math.round(v.currentTime * fps), true); };
  const ob = $('#ob'); if (ob) ob.onclick = () => { BR.showObj = !BR.showObj; ob.classList.toggle('on', BR.showObj); drawKp(Math.round(v.currentTime * fps), true); };
  const dp = $('#dp'); if (dp) dp.onclick = () => keepT(() => { BR.showDepth = !BR.showDepth; });
  const pip = $('#pip'); if (pip) pip.onclick = () => keepT(() => { BR.depthBig = !BR.depthBig; });
  document.querySelectorAll('[data-step]').forEach(b => b.onclick = () => { const k = Math.max(0, Math.min(BR.segs.length - 1, BR.sel + +b.dataset.step)); if (!BR.segs[k]) return; BR.sel = k; v.currentTime = BR.segs[k].t0 + 0.01; renderSide(); markSel(); });
}

function capHTML(sg, i) {
  const H = {'左手':'左手','右手':'右手','双手':'双手'};
  const obj = sg.object_id ? `<span class="ctag"><b>${esc(sg.object_id)}</b>${esc(sg.object || '')}</span>` : sg.object ? `<span class="ctag">${esc(sg.object)}</span>` : '';
  const tg = sg.target ? `<span class="ctag">→ ${esc(sg.target)}</span>` : '';
  const res = sg.success === true ? '<span class="ctag ok">完成</span>' : sg.success === false ? '<span class="ctag no">未完成</span>' : '';
  const verb = sg.verb || (sg.description ? '' : sg.action);
  return `<div class="tc">${tcode(sg.t0)} – ${tcode(sg.t1)}<span>分段 ${i + 1} / ${BR.segs.length}</span></div>
    <div class="tx">${esc(sg.description || sg.action || '（未标注）')}</div>
    ${verb || sg.hand || obj || res ? `<div class="row">${verb && verb !== '静止' ? `<span class="ctag vb">${esc(verb)}</span>` : ''}${sg.hand && sg.hand !== '无' ? `<span class="ctag">${esc(sg.hand)}</span>` : ''}${obj}${tg}<span class="sp"></span>${res}</div>` : ''}`;
}

let lastKp = -1;
function drawKp(fi, force) {
  const cv = $('#cv'); if (!cv) return;
  if (fi === lastKp && !force) return; lastKp = fi;
  const g = cv.getContext('2d'); g.clearRect(0, 0, cv.width, cv.height);
  const tr = BR.d.track;
  if (BR.showObj && tr) {
    const fps = BR.d.ingest.rgb_fps || 30, ti = Math.min(Math.round(fi / fps * tr.fps), tr.t.length - 1);
    const lw0 = Math.max(1.5, cv.width / 640), touching = new Set(((BR.d.contact || {}).events || []).filter(e => fi / fps >= e.t0 && fi / fps <= e.t1).map(e => e.obj));
    const acc = getComputedStyle(document.documentElement).getPropertyValue('--acc').trim() || '#3DD6F5';
    const sc = cv.width / ((tr.src_size || [])[0] || cv.width);
    const pill = (x, y, txt, on, col) => { g.font = `${on ? 600 : 500} ${lw0 * 7.5}px Inter, sans-serif`; const tw = g.measureText(txt).width, h = lw0 * 11, w = tw + lw0 * 7;
      x = Math.max(2, Math.min(cv.width - w - 2, x - w / 2)); y = Math.max(h + 2, y - lw0 * 3);
      g.fillStyle = col || (on ? acc : 'rgba(10,12,14,.62)'); g.beginPath(); g.roundRect ? g.roundRect(x, y - h, w, h, h / 3) : g.rect(x, y - h, w, h); g.fill();
      g.fillStyle = on ? '#03181D' : '#D7DBE0'; g.fillText(txt, x + lw0 * 3.5, y - lw0 * 3.2); };
    const rr = (b, r) => { g.beginPath(); g.roundRect ? g.roundRect(b[0], b[1], b[2]-b[0], b[3]-b[1], r) : g.rect(b[0], b[1], b[2]-b[0], b[3]-b[1]); };
    const gone = new Set(BR.oe ? BR.oe.del : []);
    tr.objects.forEach((o, k) => { const b0 = o.bbox[ti]; if (!b0 || gone.has(o.id)) return; const b = b0.map(v => v * sc); const on = touching.has(o.id);
      rr(b, lw0 * 3);
      if (on) { g.globalAlpha = .16; g.fillStyle = acc; g.fill(); g.globalAlpha = 1; }
      g.strokeStyle = on ? acc : 'rgba(235,238,242,.8)'; g.lineWidth = on ? lw0 * 2 : lw0; g.setLineDash([]); g.stroke();
      const nm = (BR.oe && BR.oe.set[o.id] && BR.oe.set[o.id].label) || o.label;
      pill((b[0] + b[2]) / 2, b[1], on ? o.id + ' ' + nm : o.id, on); });
  }
  // 还没应用的人工框：虚线
  if (BR.oe && BR.d.track) {
    const tr2 = BR.d.track, sc = cv.width / ((tr2.src_size || [])[0] || cv.width), lw1 = Math.max(1.5, cv.width / 640);
    const acc = getComputedStyle(document.documentElement).getPropertyValue('--acc').trim() || '#3DD6F5';
    const pend = [...BR.oe.add.map(a => [a.bbox, '新 · ' + a.label]), ...Object.entries(BR.oe.set).filter(([, e]) => e.bbox).map(([id, e]) => [e.bbox, id + ' 重新框选'])];
    pend.forEach(([bx, txt]) => { const b = bx.map(v => v * sc); g.strokeStyle = acc; g.lineWidth = lw1 * 2; g.setLineDash([8, 5]); g.strokeRect(b[0], b[1], b[2]-b[0], b[3]-b[1]); g.setLineDash([]);
      g.font = `600 ${lw1 * 7.5}px Inter, sans-serif`; g.fillStyle = acc; g.fillText(txt, b[0], Math.max(lw1 * 10, b[1] - lw1 * 4)); });
  }
  if (!BR.showKp || !BR.hands) return;
  const fr = BR.hands.frames[Math.min(fi, BR.hands.frames.length - 1)] || [];
  const B = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[0,17],[17,18],[18,19],[19,20]];
  const lw = Math.max(1.6, cv.width / 520);
  for (const [side, conf, pts, ex] of fr) {
    const c = ex ? 'rgba(200,200,200,.7)' : side === 'left' ? '#5AA0FA' : '#FF9A6E';
    g.strokeStyle = c; g.lineWidth = lw; g.setLineDash(ex ? [6,4] : []); g.lineCap = 'round';
    for (const [a, b] of B) { const p = pts[a], q = pts[b]; if (!p || !q || p[0] == null || q[0] == null) continue; g.beginPath(); g.moveTo(p[0], p[1]); g.lineTo(q[0], q[1]); g.stroke(); }
    g.setLineDash([]); g.fillStyle = '#fff';
    for (const p of pts) { if (p[0] == null) continue; g.beginPath(); g.arc(p[0], p[1], lw * 1.15, 0, 7); g.fill(); }
    if (pts[0] && pts[0][0] != null) { g.fillStyle = c; g.font = `600 ${lw*7}px Inter, sans-serif`; g.fillText((side === 'left' ? 'L' : 'R') + (ex ? ' 剔除' : ''), pts[0][0] + 8, pts[0][1] + lw * 8); }
  }
}

function drawTracks() {
  const tl = $('#tl'); if (!tl) return;
  const D = BR.d.rec.duration, pct = t => (t / D * 100).toFixed(3) + '%';
  const segs = BR.segs.map((s, i) => `<div class="seg ${i===BR.sel?'sel':''} ${s.src==='manual'?'manual':''}" data-si="${i}" style="left:${pct(s.t0)};width:calc(${pct(s.t1-s.t0)} - 2px);background:${segBg(s, i)};color:${segFg(s, i)}" ${T('<b>'+esc(s.description||s.action||'未命名')+'</b><br>'+(s.verb?esc(s.verb)+(s.object?' · '+esc(s.object):'')+'<br>':'')+s.t0.toFixed(2)+'–'+s.t1.toFixed(2)+' s'+(s.success===true?' · 完成':s.success===false?' · 未完成':''))}>${esc(segLabel(s))}</div>`).join('');
  const q = BR.d.quality && !BR.d.quality.spans.length ? `<div class="qspan" style="left:0;width:100%;background:var(--ok-bg)" ${T('整段画面清晰，没有模糊、过暗、过曝或冻结')}></div>` : BR.d.quality ? BR.d.quality.spans.map(s => `<div class="qspan" style="left:${pct(s.t0)};width:max(3px,${pct(s.t1-s.t0+0.2)});background:${QC[s.k]}" ${T(QN[s.k]+' '+s.t0.toFixed(1)+'–'+(s.t1+0.2).toFixed(1)+' s')}></div>`).join('') : '';
  const cuts = BR.segs.slice(1).map(s => `<div class="cut" style="left:${pct(s.t0)}" ${T('切点 '+s.t0.toFixed(2)+' s<br>依据：'+esc(s.cut||''))}></div>`).join('')
    + (BR.d.imu ? BR.d.imu.events.map(t => `<div class="cut imu" style="left:${pct(t)};opacity:.5" ${T('IMU 冲击 '+t.toFixed(2)+' s')}></div>`).join('') : '')
    + (BR.d.grip ? BR.d.grip.events.map(e => `<div class="cut grip" style="left:${pct(e.t)}" ${T('夹爪'+(e.k==='open'?'张开':'闭合')+' '+e.t.toFixed(2)+' s')}></div>`).join('') : '');
  let hand = '';
  if (BR.hands) {
    const fps = BR.d.ingest.rgb_fps || 30, F = BR.hands.frames; let st = null;
    const runs = [];
    F.forEach((f, i) => { const on = f.length > 0; if (on && st === null) st = i; if (!on && st !== null) { runs.push([st, i]); st = null; } });
    if (st !== null) runs.push([st, F.length]);
    hand = runs.map(([a, b]) => `<div class="qspan" style="left:${pct(a/fps)};width:${pct((b-a)/fps)};background:var(--tx4)"></div>`).join('');
  }
  let ctt = '', cttL = '', cttR = '';
  if (BR.d.contact && BR.d.track) {
    const idx = Object.fromEntries(BR.d.track.objects.map((o, k) => [o.id, k]));
    const one = e => `<div class="qspan" style="left:${pct(e.t0)};width:max(4px,${pct(e.t1-e.t0)});background:${OBC[idx[e.obj]%OBC.length]};${e.via_tool?'opacity:.35;':''}${e.tool?'box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.55);':''}" ${T(`<b>${e.hand==='left'?'左手':'右手'} · ${esc(e.obj)} ${esc(e.label)}</b>${e.kind?' · '+esc(e.kind):''}<br>${e.t0.toFixed(1)}–${e.t1.toFixed(1)} s${e.min_dist_cm!=null?' · 指尖最近 '+e.min_dist_cm+' cm':''}${e.moved?'<br>移动了 '+(e.disp_cm!=null?e.disp_cm+' cm':'（距离未知）'):'<br>位置基本没变'}${e.conf?'<br>把握 '+e.conf+(e.ref&&e.ref!=='无'?' · 已按'+e.ref+'扣除头部运动':' · 未能扣除头部运动'):''}${e.ref_err_cm!=null?'（参照误差 '+e.ref_err_cm+' cm）':''}${e.method?'<br>判断方式：'+esc(e.method)+(e.frames_2d_pct?'（'+e.frames_2d_pct+'% 的帧没有可用深度，按二维判断）':''):''}${e.tool?'<br>一直握着：当作工具':''}${e.via_tool?'<br>握着 '+esc(e.via_tool)+' 期间碰到：只记录，不切段':''}`)}></div>`;
    cttL = BR.d.contact.events.filter(e => e.hand === 'left').map(one).join('');
    cttR = BR.d.contact.events.filter(e => e.hand !== 'left').map(one).join('');
    ctt = cttL + cttR;
  }
  const ticks = []; const stp = D > 120 ? 30 : D > 40 ? 10 : D > 15 ? 5 : 2;
  for (let t = 0; t <= D; t += stp) ticks.push(`<span style="left:${pct(t)}">${t}s</span>`);
  tl.innerHTML = `<div class="axis">${ticks.join('')}</div>
    ${grpHTML(pct)}<div class="trk"><span class="tn">分段</span><div class="tb" data-seek>${segs}</div></div>
    <div class="trk"><span class="tn">切点</span><div class="tb" data-seek>${cuts}</div></div>
    ${ctt ? `<div class="trk"><span class="tn" ${T('左手接触了哪个物体；颜色对应物体')}><i class="dot" style="background:var(--left)"></i>左手</span><div class="tb" data-seek>${cttL}</div></div>
    <div class="trk"><span class="tn" ${T('右手接触了哪个物体；颜色对应物体')}><i class="dot" style="background:var(--right)"></i>右手</span><div class="tb" data-seek>${cttR}</div></div>` : ''}
    ${BR.hands ? `<div class="trk"><span class="tn" ${T('画面里有手的时段')}>${BR.hands.src==='画面估计'?'手（估计）':'手出现'}</span><div class="tb" data-seek>${hand}</div></div>` : ''}
    <div class="trk"><span class="tn">画质</span><div class="tb" data-seek>${q || ''}</div></div>
    <div class="ph" id="ph"></div>`;
  drawSegList();
}
function segGroups() {
  // 子任务分组：按每段的 group 编号把连续的段连起来（人工切开、合并后也能对上）
  const G = (BR.d.segments || {}).groups || [];
  if (!G.length) return [];
  const out = [];
  BR.segs.forEach((s, i) => { const g = s.group; const last = out[out.length - 1];
    if (last && g != null && last.g === g) { last.to = i; last.t1 = s.t1; }
    else out.push({g, from: i, to: i, t0: s.t0, t1: s.t1, title: (g != null && G[g] ? G[g].title : '') || s.description || s.action || ''}); });
  return out;
}
function grpHTML(pct) {
  const gs = segGroups(); if (!gs.length) return '';
  return `<div class="trk"><span class="tn" ${T('大模型通读全部分段后，把连续、属于同一件事的段合成一组；分段的切点不变')}>子任务</span><div class="tb" data-seek>${gs.map((g, k) => `<div class="seg grp" style="left:${pct(g.t0)};width:calc(${pct(g.t1-g.t0)} - 1px)" ${T('<b>'+esc(g.title)+'</b><br>第 '+(g.from+1)+(g.to>g.from?'–'+(g.to+1):'')+' 段 · '+g.t0.toFixed(1)+'–'+g.t1.toFixed(1)+' s')}>${esc(g.title)}</div>`).join('')}</div></div>`;
}
function drawSegList() {
  const el = $('#seglist'); if (!el) return;
  if (!BR.segs.length) { el.innerHTML = '<div class="bd k">还没有分段。处理到“事件切点”后会出现。</div>'; return; }
  const done = BR.segs.filter(s => s.action).length;
  const el_ = (BR.d.segments || {}).elapsed;
  $('#segsum').textContent = `${BR.segs.length} 段 · 已标注 ${done}` + (el_ ? ` · 用时 ${el_ >= 90 ? (el_/60).toFixed(1) + ' 分钟' : Math.round(el_) + ' 秒'}` : '');
  const fine = BR.segs.some(s => s.description !== undefined);
  const res = s => s.success === true ? '<span class="tag ok">完成</span>' : s.success === false ? '<span class="tag bad">未完成</span>' : '<span class="k">—</span>';
  const empty = s => `<span class="k">${s.err ? '<span style="color:var(--bad)" ' + T('模型调用失败：' + esc(s.err)) + '>失败</span>' : '未标注'}</span>`;
  const sum = (BR.d.segments || {}).summary;
  el.innerHTML = (fine
    ? `<table class="t"><tr><th>#</th><th>时间</th><th>手</th><th>动作</th><th>物体</th><th>描述</th><th>去向</th><th>结果</th></tr>${BR.segs.map((s, i) => `${(() => { const g = segGroups().find(x => x.from === i); return g && segGroups().some(x => x.to > x.from) ? `<tr class="grph"><td colspan="8">子任务 · ${esc(g.title)}<span class="k mono"> ${g.t0.toFixed(1)}–${g.t1.toFixed(1)} s</span></td></tr>` : ''; })()}<tr class="click ${i===BR.sel?'cur':''}" data-si="${i}">
      <td class="mono"><i style="display:inline-block;width:8px;height:8px;border-radius:3px;background:${segFg(s, i)};margin-right:8px"></i>${i+1}</td>
      <td class="mono" style="white-space:nowrap">${s.t0.toFixed(1)}–${s.t1.toFixed(1)}</td><td>${esc(s.hand||'')}</td>
      <td ${T((s.src==='rule'?'规则判断，未调用模型':s.sec?'模型用时 '+s.sec+' 秒':'') + (s.verb_raw?' · 模型原词：'+esc(s.verb_raw):''))}>${s.verb ? `<span style="${s.verb==='静止'?'color:var(--faint)':''}">${esc(s.verb)}</span>${s.review?' <span class="tag warn" '+T('模型给的词不在词表里，已归到“其他”，请人工确认')+'>复核</span>':''}${s.src==='rule'?' <span class="k" style="font-size:11px">规则</span>':''}` : s.action ? esc(s.action) : empty(s)}</td><td>${s.object_id ? `<b class="mono">${esc(s.object_id)}</b> ` : ''}${esc(s.object || '')}${s.off_list ? ' <span class="tag warn" '+T('这个物体不在物体清单里（比如中途从抽屉里拿出来的），没有编号、轮廓和三维位置；名称来自模型看图。请人工复核')+'>清单外</span>' : ''}</td>
      <td>${esc(s.description || '')}${s.src==='manual'?' <span class="tag mute">人工</span>':''}</td><td ${s.target_vlm && s.target_vlm !== s.target ? T('按物体位置测算；模型的判断是：' + esc(s.target_vlm)) : ''}>${esc(s.target || '')}</td>
      <td ${s.evidence ? T('依据：' + esc(s.evidence)) : ''}>${res(s)}</td></tr>`).join('')}</table>`
    : `<table class="t"><tr><th>#</th><th>时间</th><th>动作</th><th>物体</th><th>结果</th><th>置信</th><th>来源</th></tr>${BR.segs.map((s, i) => `<tr class="click ${i===BR.sel?'cur':''}" data-si="${i}">
      <td class="mono"><i style="display:inline-block;width:8px;height:8px;border-radius:3px;background:${segFg(s, i)};margin-right:8px"></i>${i+1}</td>
      <td class="mono">${s.t0.toFixed(1)}–${s.t1.toFixed(1)}</td><td>${s.action ? esc(s.action) : empty(s)}</td><td>${esc(s.object || '')}</td><td>${res(s)}</td>
      <td class="mono">${s.conf != null ? Number(s.conf).toFixed(2) : ''}</td><td class="k">${s.src === 'manual' ? '人工' : s.src === 'vlm' && s.action ? '模型' : '自动切点'}</td></tr>`).join('')}</table>`)
    + (sum && sum.steps && sum.steps.length ? `<div class="sumbox"><div class="k">高层步骤（模型通读归纳）${sum.task_success===true?' · <span style="color:var(--ok)">任务完成</span>':sum.task_success===false?' · <span style="color:var(--bad)">任务未完成</span>':''}</div><ol style="margin:0;padding-left:20px">${sum.steps.map(x => `<li>${esc(x)}</li>`).join('')}</ol>${sum.text?`<div class="k" style="margin-top:6px">${esc(sum.text)}</div>`:''}</div>` : '');
}

function markSel() {
  document.querySelectorAll('#seglist tr[data-si]').forEach(el => el.classList.toggle('cur', +el.dataset.si === BR.sel));
  document.querySelectorAll('.seg').forEach(el => el.classList.toggle('sel', +el.dataset.si === BR.sel)); }

function dirty() { BR.dirty = true; const b = $('#save'); if (b) b.disabled = false; $('#savest').textContent = '有未保存的修改'; }
function editSeg(fn) { const s = curSeg(); if (!s) return; fn(s); s.src = 'manual'; dirty(); drawTracks(); }

document.addEventListener('click', async e => {
  if (!onTab('browse') || !BR.d) return;
  const ri = e.target.closest('[data-rec]'); if (ri) { if (BR.dirty && !confirm('有未保存的修改，确定离开？')) return; location.hash = `#/b/${BR.bid}/browse/${ri.dataset.rec}`; return; }
  const si = e.target.closest('[data-si]'); const v = $('#vr');
  if (si) { BR.sel = +si.dataset.si; if (v) v.currentTime = BR.segs[BR.sel].t0 + 0.01; renderSide(); markSel(); return; }
  const sk = e.target.closest('[data-seek]');
  if (sk && v) { const r = sk.getBoundingClientRect(); v.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * BR.d.rec.duration; const i = BR.segs.findIndex(s => v.currentTime >= s.t0 && v.currentTime < s.t1); if (i >= 0) { BR.sel = i; renderSide(); markSel(); } return; }
  const sb = e.target.closest('#succ button'); if (sb) { editSeg(s => s.success = JSON.parse(sb.dataset.s)); renderSide(); return; }
  if (e.target.id === 'split') { splitAt(); return; }
  if (e.target.id === 'merge') { const a = BR.segs[BR.sel], b = BR.segs[BR.sel+1]; a.t1 = b.t1; a.action = a.action || b.action; a.src = 'manual'; BR.segs.splice(BR.sel+1, 1); dirty(); drawTracks(); renderSide(); return; }
  if (e.target.id === 'save') { await saveSegs(); return; }
  if (e.target.id === 'redo') {
    if (BR.segs.some(s => s.src === 'manual') && !confirm('重新处理会覆盖人工修改过的分段，继续？')) return;
    const r = await api(`/api/recs/${BR.bid}/${BR.rid}/replan`, {method:'POST'});
    BR.d = await api(`/api/recs/${BR.bid}/${BR.rid}`);
    toast(r.vlm ? `已按当前环境重新排队（视觉模型：${r.vlm}），完成后这里自动刷新` : '已重新排队，但没有连上本地视觉模型，语义相关步骤会跳过：' + (r.err || '请检查 Ollama')); renderBrowse(); return;
  }
  if (e.target.closest('#oadd')) { startDraw('add'); return; }
  const ob = e.target.closest('[data-obox]'); if (ob) { startDraw('box', ob.dataset.obox); return; }
  const orn = e.target.closest('[data-oren]'); if (orn) { const id = orn.dataset.oren, o = BR.d.objects.objects.find(x => x.id === id); const n = await ask(`给 ${id} 改名`, {value: (BR.oe.set[id] || {}).label || (o ? o.label : '')}); if (n) { BR.oe.set[id] = {...(BR.oe.set[id] || {}), label: n}; renderSide(); } return; }
  const od = e.target.closest('[data-odel]'); if (od) { if (!BR.oe.del.includes(od.dataset.odel)) BR.oe.del.push(od.dataset.odel); delete BR.oe.set[od.dataset.odel]; renderSide(); const v2 = $('#vr'); if (v2) drawKp(Math.round(v2.currentTime * (BR.d.ingest.rgb_fps || 30)), true); return; }
  const ou = e.target.closest('[data-oundo]'); if (ou) { BR.oe.del = BR.oe.del.filter(x => x !== ou.dataset.oundo); renderSide(); return; }
  const oc = e.target.closest('[data-ocan]'); if (oc) { BR.oe.add.splice(+oc.dataset.ocan, 1); renderSide(); const v2 = $('#vr'); if (v2) drawKp(Math.round(v2.currentTime * (BR.d.ingest.rgb_fps || 30)), true); return; }
  if (e.target.closest('#ocancel')) { BR.oe = {add: [], set: {}, del: []}; renderSide(); const v2 = $('#vr'); if (v2) drawKp(Math.round(v2.currentTime * (BR.d.ingest.rgb_fps || 30)), true); return; }
  if (e.target.closest('#oapply')) { await applyObjEdits(); return; }
  const rr = e.target.closest('[data-rr]');
  if (rr) { if (rr.dataset.rr === 'semantic' && BR.segs.some(s => s.src === 'manual') && !confirm('重跑语义标注会覆盖人工修改过的分段描述，继续？')) return; await api(`/api/recs/${BR.bid}/${BR.rid}/rerun`, {method:'POST', body:{key: rr.dataset.rr}}); toast('已重新排队，完成后自动刷新'); BR.d.steps.find(s => s.key === rr.dataset.rr).status = 'wait'; renderSide(); }
});
function splitAt() {
  const v = $('#vr'); if (!v) return; const t = v.currentTime;
  const i = BR.segs.findIndex(s => t > s.t0 + 0.2 && t < s.t1 - 0.2); if (i < 0) { toast('播放头离段边界太近'); return; }
  const s = BR.segs[i], n = {...s, t0: +t.toFixed(3), action:'', object:'', success:null, src:'manual', cut:'人工'}; s.t1 = +t.toFixed(3); s.src = 'manual';
  BR.segs.splice(i + 1, 0, n); BR.sel = i + 1; dirty(); drawTracks(); renderSide();
}
async function saveSegs() {
  const tn = $('#tname');
  await api(`/api/recs/${BR.bid}/${BR.rid}/segments`, {method:'PUT', body:{segments: BR.segs, task: tn ? tn.value : null}});
  BR.dirty = false; $('#save').disabled = true; $('#savest').textContent = '已保存'; toast('修改已保存，导出时会使用');
}
document.addEventListener('change', e => {
  if (!onTab('browse')) return;
  if (e.target.id === 'lf') { BR.lf = e.target.value; document.querySelector('.rlist').innerHTML = recListHTML(); }
});
document.addEventListener('toggle', e => { if (!onTab('browse') || !e.target.classList || !e.target.classList.contains('fold')) return;
  const t = e.target.querySelector('summary').textContent; if (t.startsWith('处理步骤')) BR.foldSteps = e.target.open; else if (t.startsWith('物体')) BR.foldObj = e.target.open; else BR.foldTraj = e.target.open; }, true);
document.addEventListener('input', e => {
  if (!onTab('browse')) return;
  if (e.target.id === 'lq') { BR.lq = e.target.value; const p = e.target.selectionStart; document.querySelector('.rlist').innerHTML = recListHTML(); const q = $('#lq'); q.focus(); q.setSelectionRange(p, p); return; }
  if (e.target.id === 'sa') { const cs = curSeg(); cs.review = false; if (cs.verb !== undefined) { cs.verb = e.target.value; cs.action = e.target.value + (cs.object || ''); } else cs.action = e.target.value; curSeg().src = 'manual'; dirty(); const el = document.querySelector(`.seg[data-si="${BR.sel}"]`); if (el) { el.textContent = e.target.value || '·'; el.classList.add('manual'); } drawSegList(); }
  if (e.target.id === 'so') { curSeg().object = e.target.value; curSeg().src = 'manual'; dirty(); drawSegList(); }
  const fld = {sh:'hand', st:'target', sd:'description'}[e.target.id];
  if (fld) { curSeg()[fld] = e.target.value; curSeg().src = 'manual'; dirty(); drawSegList(); }
  if (e.target.id === 'tname') dirty();
});
document.addEventListener('keydown', e => {
  if (!onTab('browse') || e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'Escape' && BR.drawing) { stopDraw(); return; }
  const v = $('#vr'); if (!v) return;
  const fps = BR.d.ingest.rgb_fps || 30;
  if (e.key === ' ') { $('#play').click(); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { v.pause(); v.currentTime = Math.min(BR.d.rec.duration, v.currentTime + 1/fps); e.preventDefault(); }
  else if (e.key === 'ArrowLeft') { v.pause(); v.currentTime = Math.max(0, v.currentTime - 1/fps); e.preventDefault(); }
  else if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveSegs(); }
  else if (e.key === 's' || e.key === 'S') { splitAt(); }
});
window.addEventListener('beforeunload', e => { if (BR.dirty || oePending()) { e.preventDefault(); e.returnValue = ''; } });


/* ================================================================ 导出 */
const EX = {sel:new Set(), minGrade:1, proj:'', batch:'', fmt:'lerobot'};
routes['export'] = async () => {
  const main = $('#main');
  const items = await api('/api/exportable');
  const key = i => i.bid + '|' + i.rid;
  const inScope = i => (!EX.proj || i.pid === EX.proj) && (!EX.batch || i.bid === EX.batch);
  const autoSel = () => { EX.sel = new Set(items.filter(i => inScope(i) && i.grade >= EX.minGrade && i.status === 'done').map(key)); };
  if (!EX.proj && S.lastProject && items.some(i => i.pid === S.lastProject)) EX.proj = S.lastProject;
  autoSel();
  const projs = [...new Map(items.map(i => [i.pid, i.project])).entries()];
  const draw = () => {
    const vis = items.filter(inScope);
    const bats = [...new Map(items.filter(i => !EX.proj || i.pid === EX.proj).map(i => [i.bid, i.batch])).entries()];
    $('#exflt').innerHTML = `<select id="exp"><option value="">全部项目</option>${projs.map(([id, n]) => `<option value="${id}" ${EX.proj===id?'selected':''}>${esc(n)}</option>`).join('')}</select>
      <select id="exb"><option value="">全部批次</option>${bats.map(([id, n]) => `<option value="${id}" ${EX.batch===id?'selected':''}>${esc(n)}</option>`).join('')}</select>
      <span class="sp"></span><span class="k">最低质量</span><span class="seg3" id="ming">${[3,2,1,0].map(g=>`<button data-g="${g}" class="${EX.minGrade===g?'on':''}">${GR[g][0]}</button>`).join('')}</span>`;
    $('#etable').innerHTML = vis.length ? `<table class="t"><thead><tr><th style="width:28px"><input type="checkbox" id="exall"></th><th style="width:30px"></th><th>录制</th><th>任务</th><th class="r">时长</th><th class="r">分段</th><th></th></tr></thead><tbody>${vis.map(i => `<tr><td><input type="checkbox" data-k="${key(i)}" ${EX.sel.has(key(i))?'checked':''}></td><td>${gradeHTML(i.grade,'sm')}</td><td>${esc(i.name)}<div class="k">${esc(i.project || '')} / ${esc(i.batch)}</div></td><td>${esc(i.task)||'<span class="k">未命名</span>'}</td><td class="r mono">${fmtDur(i.dur)}</td><td class="r mono">${i.segs}</td><td>${i.edited?'<span class="tag ok">人工修改</span>':''}${i.status==='failed'?'<span class="tag bad">有失败步骤</span>':''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">这里还没有处理完成的录制。</div>';
    const sel = items.filter(i => EX.sel.has(key(i)));
    $('#esum').innerHTML = `已选 <b class="mono">${sel.length}</b> 条 · ${fmtHours(sel.reduce((a,i)=>a+i.dur,0))}`;
    document.querySelectorAll('.lr').forEach(el => el.style.display = EX.fmt === 'lerobot' ? '' : 'none');
    document.querySelectorAll('.old').forEach(el => el.style.display = EX.fmt === 'lerobot' ? 'none' : '');
  };
  main.innerHTML = pageHead({title: '导出数据集', meta: '把处理好的录制整理成训练可直接读取的数据集'}) + `
    <div class="egrid">
      <div class="card"><h2>选择录制</h2><div class="tbar" id="exflt"></div><div id="etable" style="max-height:62vh;overflow:auto"></div></div>
      <div class="side">
        <div class="card"><h2>导出设置</h2><div class="bd form">
          <div class="row"><span>名称</span><input type="text" id="ename" value="dataset_${new Date().toISOString().slice(5,10).replace('-','')}"></div>
          <div class="row"><span>格式</span><select id="efmt"><option value="lerobot" ${EX.fmt==='lerobot'?'selected':''}>LeRobot v3.0（推荐）</option><option value="edp" ${EX.fmt==='edp'?'selected':''}>平台原格式（每条一个文件夹）</option></select></div>
          <div class="row lr"><span ${T('LeRobot 要求整个数据集一个帧率。0 = 用录制的帧率（多数为 30）；填其他值会按时间重新取帧')}>帧率</span><input type="number" id="efps" min="0" max="60" value="0"></div>
          <div class="row lr"><span ${T('人手数据没有机器人动作。常用约定：动作 = 下一帧的手部状态')}>action 字段</span><select id="eact"><option value="next">下一帧手部状态</option><option value="none">不导出</option></select></div>
          <div class="row lr"><span>附带</span><div class="checks"><label ${T('复制原始深度文件，帧号对应 data 的 edp.depth_frame')}><input type="checkbox" id="edep"> 深度</label><label ${T('把每条录制的全部原始文件复制到 raw/')}><input type="checkbox" id="eraw"> 全部原始文件</label></div></div>
          <div class="row old"><span>深度</span><label><input type="checkbox" id="draw"> 复制原始深度帧（体积大）</label></div>
          <div id="esum" class="k"></div>
          <button class="btn pri" id="ego">导出</button>
        </div></div>
        <div class="card lr"><h2>导出后的结构</h2><div class="bd"><pre class="tree">dataset/
├─ meta/            LeRobot 标准：结构、统计、任务、子任务
├─ data/            逐帧表：左右手分列、状态、动作、
│                   分段、接触、原始帧号与时间戳
├─ videos/          头戴彩色视频（与 data 逐帧对齐）
├─ edp/             平台扩展：分段与接触详情、物体
│  ├─ fields.json   掩码、每个字段是原始还是派生
│  └─ episodes/…/source.json  原始文件清单
├─ raw/             （可选）原始文件副本
└─ README.md        字段说明与读取示例</pre></div></div>
        <div class="card"><h2>已导出</h2><div id="elist"></div></div>
      </div>
    </div>`;
  draw();
  every(async () => {
    const ex = await api('/api/exports');
    $('#elist').innerHTML = ex.length ? `<table class="t">${ex.map(x => `<tr><td>${esc(x.name)}<div class="k">${x.format && x.format.startsWith('lerobot') ? 'LeRobot v3.0' : '平台原格式'} · ${x.episodes} 条 · ${fmtB(x.bytes)} · ${esc(x.created||'')}</div>${x.error ? `<div class="k c-bad">${esc(x.error)}</div>` : ''}</td><td class="r">${x.status==='done'?`<button class="btn sm" data-open="${esc(x.path)}">打开文件夹</button>`:x.status==='error'?'<span class="tag bad">失败</span>':`<span class="tag run">导出中 ${x.progress != null ? Math.round(x.progress*100) + '%' : ''}</span>`}</td></tr>`).join('')}</table>` : '<div class="bd k">暂无</div>';
  }, 2000);
  main.onchange = e => {
    if (e.target.id === 'exp') { EX.proj = e.target.value; EX.batch = ''; autoSel(); draw(); }
    if (e.target.id === 'exb') { EX.batch = e.target.value; autoSel(); draw(); }
    if (e.target.id === 'efmt') { EX.fmt = e.target.value; draw(); }
  };
  main.onclick = async e => {
    if (e.target.id === 'exall') { items.filter(inScope).forEach(i => e.target.checked ? EX.sel.add(key(i)) : EX.sel.delete(key(i))); draw(); return; }
    const cb = e.target.closest('[data-k]'); if (cb) { cb.checked ? EX.sel.add(cb.dataset.k) : EX.sel.delete(cb.dataset.k); draw(); return; }
    const g = e.target.closest('#ming [data-g]'); if (g) { EX.minGrade = +g.dataset.g; autoSel(); draw(); return; }
    const o = e.target.closest('[data-open]'); if (o) { const r = await api('/api/open', {method:'POST', body:{path:o.dataset.open}}); if (!r.ok) toast(o.dataset.open); return; }
    if (e.target.id === 'ego') {
      const sel = [...EX.sel].map(k => { const [bid, rid] = k.split('|'); return {bid, rid}; });
      if (!sel.length) { toast('先选择要导出的录制'); return; }
      const opts = EX.fmt === 'lerobot' ? {format:'lerobot', fps: +$('#efps').value || 0, action: $('#eact').value, depth: $('#edep').checked, raw: $('#eraw').checked} : {format:'edp', depth_raw: $('#draw').checked};
      try { const r = await api('/api/export', {method:'POST', body:{name: $('#ename').value.trim(), items: sel, opts}}); toast('开始导出到 ' + r.path); }
      catch (err) { toast(err.message); }
    }
  };
};


/* ================================================================ 设置 */
/* 外观：主题和强调色存在浏览器本地（index.html 里加载时先读一次，避免闪烁） */
const ACCENTS = [['cyan','青','#3DD6F5'], ['indigo','靛蓝','#8796FF'], ['green','翠绿','#3FD99F'], ['violet','紫','#B794FF']];
function getLook() { let theme = 'dark', accent = 'cyan'; try { theme = localStorage.getItem('edp_theme') || 'dark'; accent = localStorage.getItem('edp_accent') || 'cyan'; } catch (e) {} return {theme, accent}; }
function applyLook() { const l = getLook(); const t = l.theme === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : l.theme;
  document.documentElement.dataset.theme = t; document.documentElement.dataset.accent = l.accent;
  if (BR.d && $('#cv')) { const v = $('#vr'); if (v) drawKp(Math.round(v.currentTime * ((BR.d.ingest || {}).rgb_fps || 30)), true); } }
function setLook(o) { try { if (o.theme) localStorage.setItem('edp_theme', o.theme); if (o.accent) localStorage.setItem('edp_accent', o.accent); } catch (e) {} applyLook(); }
try { matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => { if (getLook().theme === 'system') applyLook(); }); } catch (e) {}
// 运行资源：本机检测到的显卡、CPU，调度器算出的并发空位，正在跑的任务。并发数可手动限制
async function resCard(st) {
  const el = $('#resc'); if (!el) return;
  if (el.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;  // 正在填数字时不刷新
  const m = await api('/api/resources');
  const g = m.gpus.length ? m.gpus.map(x => `${esc(x.name)} · ${(x.mem_mb / 1024).toFixed(0)} GB · 同时跟踪 ${m.slots.gpu[x.i] ?? 1} 个${m.oom[x.i] ? `（显存不够过，已自动减 ${m.oom[x.i]}）` : ''}`).join('<br>') : '未检测到 NVIDIA 显卡：物体跟踪在 CPU 上跑，会很慢';
  const RES = {gpu: '显卡', vlm: '大模型', cpu: 'CPU'};
  el.innerHTML = `<div class="hint" style="margin:0">软件按本机资源自动决定同时跑几个步骤：每张显卡按显存放几个物体跟踪，入库、同步、接触计算等按 CPU 核数并行，外部接口的大模型请求另算。同一条录制的步骤始终按顺序跑。</div>
    <dl class="kv" style="margin-top:10px"><dt>显卡</dt><dd>${g}</dd><dt>CPU</dt><dd>${m.cores} 个逻辑核 · 同时 ${m.slots.cpu} 个 CPU 步骤</dd>
    <dt>大模型</dt><dd>${m.slots.local_vlm ? '本机模型：和第 1 张显卡上的跟踪轮流用' : `外部接口：同时 ${m.slots.vlm} 个请求`}</dd>
    <dt>总并发</dt><dd>最多 ${m.slots.total} 个步骤同时跑</dd>
    <dt>正在跑</dt><dd>${m.running.length ? m.running.map(r => `${esc(shortText(r.rec))} · ${esc(r.label)} <span class="k">${RES[r.res] || r.res}${r.dev != null ? ' #' + r.dev : ''} · ${r.sec} 秒</span>`).join('<br>') : '<span class="k">空闲</span>'}</dd></dl>
    <div class="row"><span ${T('所有步骤加起来最多同时跑几个。0 = 自动（按上面的空位）。<br>填 1 = 和以前一样，一次只跑一个步骤；机器上还有别的程序要用显卡、CPU 时可以调小')}>总并发上限</span><input type="number" id="rmax" min="0" max="64" value="${st.max_jobs ?? 0}"></div>
    <div class="row"><span ${T('每张显卡同时跑几个物体跟踪。0 = 自动（按显存，每个约 4 GB）。<br>显存不够时软件会自动减一个并重跑那一步')}>每张卡同时跟踪</span><input type="number" id="rgpu" min="0" max="8" value="${st.gpu_jobs ?? 0}"></div>
    <div class="row"><span ${T('用外部接口（百炼）实时调用时，同时发几个请求。太多会被限流（HTTP 429），软件会自动等一会儿重试')}>外部接口同时请求</span><input type="number" id="rapi" min="1" max="32" value="${st.api_parallel ?? 4}"></div>
    <div><button class="btn pri" id="svr">保存</button></div>`;
}
async function samCard(st) {
  const el = $('#samc'); if (!el) return;
  const m = await api('/api/sam');
  el.innerHTML = `<div class="hint" style="margin:0">让本地大模型先框出画面里的物体，再用 SAM 2 在整段视频里跟踪每个物体的轮廓，结合手部关键点算出“哪只手什么时候抓住、松开了哪个物体、移动了多远”。分段和语义标注会细很多。</div>
    <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="fine" ${st.fine_annotation!==false?'checked':''}> 处理时启用精细标注（需要视频里有手部关键点）</label>
    <dl class="kv"><dt>组件</dt><dd>${m.installed ? '<span style="color:var(--ok)">已安装</span>' : '<span style="color:var(--warn)">未安装</span>'}</dd>
    ${m.torch ? `<dt>PyTorch</dt><dd>${esc(m.torch)}</dd><dt>运行设备</dt><dd>${esc(m.device||'')}${m.cuda ? '' : '（没有用上显卡，会很慢）'}</dd>` : ''}
    <dt>SAM 2 权重</dt><dd>${m.weights ? '已下载' : '未下载'}</dd></dl>
    ${!m.ready ? `<button class="btn pri" id="saminst" ${m.installing?'disabled':''}>${m.installing ? '安装中…' : '一键安装（约 3 GB）'}</button>` : ''}
    ${m.log ? `<pre class="log">${esc(m.log.split('\n').slice(-14).join('\n'))}</pre>` : ''}`;
  return m;
}

routes['settings'] = async () => {
  const main = $('#main');
  const [st, sy] = await Promise.all([api('/api/settings'), api('/api/sys')]);
  setTimeout(() => every(() => samCard(st), 2000), 0);
  setTimeout(() => every(() => resCard(st), 3000), 0);
  const v = sy.vlm;
  const look = getLook();
  main.innerHTML = pageHead({title: '设置', meta: '对所有项目生效；改动后重新处理的录制按新设置执行'}) + `
  <div class="setgrid">
    <div class="card" style="grid-column:1/-1"><h2>外观<span class="sp"></span><span class="k">只影响这台电脑上的这个浏览器</span></h2><div class="bd look">
      <div class="row"><span>主题</span><span class="seg3" id="lktheme">${[['dark','深色'],['light','浅色'],['system','跟随系统']].map(([k, t]) => `<button data-lt="${k}" class="${look.theme===k?'on':''}">${t}</button>`).join('')}</span></div>
      <div class="row"><span>强调色</span><span class="swatches">${ACCENTS.map(([k, t, c]) => `<button data-la="${k}" class="${look.accent===k?'on':''}" style="background:${c}" ${T(t)}></button>`).join('')}</span></div>
    </div></div>
    <div class="card"><h2>视觉大模型</h2><div class="bd form">
      <div class="row"><span ${T('本机：用 Ollama 在本机显卡上跑，数据不出本机。<br>外部接口：调用阿里云百炼等 OpenAI 兼容的在线接口，速度快、模型强，但画面会发送到外部服务器')}>模型来源</span><select id="vb"><option value="ollama" ${st.vlm_backend!=='api'?'selected':''}>本机（Ollama）</option><option value="api" ${st.vlm_backend==='api'?'selected':''}>外部接口（如阿里云百炼）</option></select></div>
      <div class="row ap"><span ${T('OpenAI 兼容接口地址。阿里云百炼北京地域：https://dashscope.aliyuncs.com/compatible-mode/v1；国际站（新加坡）：https://dashscope-intl.aliyuncs.com/compatible-mode/v1')}>接口地址</span><input type="text" id="abase" value="${esc(st.api_base||'')}"></div>
      <div class="row ap"><span ${T('只保存在本机平台的数据库里')}>API Key</span><input type="password" id="akey" value="${esc(st.api_key||'')}" placeholder="sk-..."></div>
      <div class="row ap"><span ${T('百炼里支持看图的模型名，例如 qwen3.8-max（旗舰）、qwen3.7-plus（均衡）、qwen3.8-flash（便宜快速）、deepseek-v4.1-flash（便宜，不支持批量接口）。以百炼控制台“模型广场”为准')}>模型名</span><input type="text" id="amodel" list="amodels" value="${esc(st.api_model||'')}"><datalist id="amodels"><option value="qwen3.8-max"><option value="qwen3.7-plus"><option value="deepseek-v4.1-flash"><option value="qwen3.8-flash"><option value="qwen3.6-plus"></datalist></div>
      <div class="row ap"><span ${T('选“批量接口”处理的批次，大模型请求会攒成文件交给百炼统一处理，价格是实时的一半。<br>最长等待：超过这个时间没处理完，任务会过期，剩下的请求自动重交')}>批量最长等待</span><select id="bwin">${[24,48,72,168,336].map(h => `<option value="${h}" ${(st.batch_window_h||72)==h?'selected':''}>${h < 48 ? h + ' 小时' : (h/24) + ' 天'}</option>`).join('')}</select></div>
      <div class="row ap"><span ${T('每个批量文件最多放多少个请求（百炼上限 5 万）。请求不够一包时，等满下面的分钟数就先提交；队列空闲时立刻提交')}>批量打包</span><div style="display:flex;gap:6px;align-items:center"><input type="number" id="bmax" min="10" max="50000" style="width:90px" value="${st.batch_max_reqs ?? 5000}"><span class="k">个一包，最多等</span><input type="number" id="bflush" min="1" max="240" style="width:70px" value="${st.batch_flush_min ?? 10}"><span class="k">分钟</span></div></div>
      <div class="hint ap" style="margin:0;color:var(--warn)">使用外部接口时，视频画面会发送到接口服务商的服务器。客户数据请先确认对方允许。</div>
      <div class="row ol"><span>Ollama 地址</span><input type="text" id="ou" value="${esc(st.ollama_url)}"></div>
      <div class="row ol"><span>使用模型</span><select id="om"><option value="">自动（第一个视觉模型）</option>${v.models.map(m => `<option value="${esc(m.name)}" ${st.vlm_model===m.name?'selected':''}>${esc(m.name)}${m.vision?'':'（可能不支持图片）'}</option>`).join('')}</select></div>
      <div class="row ol"><span ${T('单手、单物体、一次接触的简单段，以及没有接触的段，交给这个更小更快的模型；复杂段仍用上面的主模型。留空 = 全部用主模型')}>简单段用的模型</span><select id="oml"><option value="">不用（全部用主模型）</option>${v.models.filter(m => m.vision).map(m => `<option value="${esc(m.name)}" ${st.vlm_model_light===m.name?'selected':''}>${esc(m.name)}</option>`).join('')}</select></div>
      <div class="row"><span ${T('每段按片段长度取帧：时长 × 每秒帧数，最少 4 帧，最多到上限。帧越多每帧分辨率越低（≤6 帧 768 像素，≤10 帧 640，更多 512），每帧左下角印上时间')}>每秒取几帧</span><input type="number" id="nf" min="0.5" max="6" step="0.5" value="${st.vlm_fps ?? 2}"></div>
      <div class="row"><span>每段最多几帧</span><input type="number" id="nmax" min="4" max="24" value="${st.vlm_max_frames ?? 12}"></div>
      <div class="row ol"><span ${T('模型一次能看的内容长度。越大越占显存；8 GB 显卡建议 8192，超出显存会被挪到 CPU，速度慢很多')}>上下文长度</span><input type="number" id="nctx" min="4096" max="32768" step="1024" value="${st.vlm_ctx ?? 8192}"></div>
      <div class="row"><span ${T('一次调用超过这个时间就放弃，改用更少的图再试一次')}>单次超时（秒）</span><input type="number" id="nto" min="30" max="600" step="10" value="${st.vlm_timeout ?? 180}"></div>
      <div class="row"><span ${T('同一只手碰同一个物体，中间断开不超过这个时间，算同一次接触（关键点抖动、遮挡造成的短暂中断）')}>接触合并间隔（秒）</span><input type="number" id="ngap" min="0.1" max="3" step="0.1" value="${st.contact_gap ?? 0.8}"></div>
      <div class="row"><span ${T('三维：把物体轮廓内的像素按深度还原成三维点，计算指尖到物体表面的距离（需要深度和内参，没有时自动退回二维）。二维：指尖落在外扩的物体轮廓里就算，物体密集时容易误判')}>接触判断方式</span><select id="cmode"><option value="3d" ${st.contact_mode!=='2d'?'selected':''}>三维距离（推荐）</option><option value="2d" ${st.contact_mode==='2d'?'selected':''}>二维轮廓</option></select></div>
      <div class="row"><span ${T('一次接触持续不到这个时长就不算，用来去掉误判：手从物体上方掠过、被挡住时轮廓重叠一下。<br>误碰多就调大（0.6–0.8）；快速的轻点、拨动被漏掉就调小（0.2–0.3）。<br>实际最少 2 个跟踪帧，跟踪 5 帧/秒时不会低于 0.4 秒')}>最短接触时长（秒）</span><input type="number" id="cmin" min="0.1" max="3" step="0.1" value="${st.contact_min ?? 0.4}"></div>
      <div class="row"><span ${T('一只手手指合拢、连续握住同一个物体超过这个时长（掸子、刷子、勺子），就当作“工具”。<br>这期间这只手碰到的其他物体只记录为工具的作用对象，不再单独切段，避免切得太碎。0 = 不识别工具')}>工具判定（秒）</span><input type="number" id="ctool" min="0" max="30" step="0.5" value="${st.tool_min_s ?? 4}"></div>
      <div class="row"><span ${T('指尖到物体表面的三维距离小于这个值算接触；至少 2 个指尖满足，或 1 个指尖在一半距离以内')}>接触距离（厘米）</span><input type="number" id="cdist" min="0.5" max="6" step="0.5" value="${st.contact_dist_cm ?? 2}"></div>
      <div class="row"><span ${T('握住物体时指尖常被挡住，量不到距离，就看手和物体在画面交界处的深度是否连在一起：深度差小于这个值的像素算“贴合”，贴合够多就算接触。<br>越大越容易把“手挡在物体前面”误判成接触（深度相机在边缘会给出一串过渡值）；越小越可能漏掉真握住。<br>默认 1 cm（实测比原来的 2.5 cm 少了 3 次误判，没有漏掉真实抓取）')}>交界贴合容差（厘米）</span><input type="number" id="cedge" min="0.3" max="3" step="0.1" value="${st.edge_tol_cm ?? 1}"></div>
      <div class="row"><span ${T('按接触切段时，短于这个时长的段并入相邻段')}>最短段长（秒）</span><input type="number" id="nmin" min="0.3" max="5" step="0.1" value="${st.min_seg ?? 1.0}"></div>
      <div class="row"><span ${T('只用于没有物体跟踪时的旧切分方式')}>最多分几段</span><input type="number" id="ms" min="2" max="40" value="${st.max_segments}"></div>
      <div style="display:flex;gap:8px"><button class="btn pri" id="sv">保存</button><button class="btn" id="tst">测试模型</button><button class="btn ap" id="tstb" ${T('用百炼免费的测试模型走一遍上传、创建、取回，不产生费用。最多等 90 秒')}>测试批量接口</button></div>
      <div id="tres" class="hint" style="margin:0">${v.ok && v.backend==='api' ? `外部接口：<b>${esc(v.model)}</b>（点“测试模型”确认能用）` : v.ok ? `已连接，共 ${v.models.length} 个模型；当前使用 <b>${esc(v.model||'无')}</b>` : '<span style="color:var(--bad)">' + esc(v.err||'未连接') + '</span><br>请确认 Ollama 已启动（任务栏有羊驼图标），或在命令行运行 <span class="mono">ollama serve</span>'}</div>
      ${v.models.length ? `<div class="models">${v.models.map(m => `<div><i class="dot" style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${m.vision?'var(--ok)':'var(--faint)'}"></i><b>${esc(m.name)}</b><span class="k">${esc(m.params||'')} · ${fmtB(m.size)}${m.vision?' · 视觉':''}</span></div>`).join('')}</div>` : ''}
    </div></div>
    <div class="card"><h2>大批量处理</h2><div class="bd form">
      <div class="hint" style="margin:0">数据量大时，不必每条都做最贵的精细标注。所有录制都会做入库、同步、质量评分、手部轨迹和粗分段；下面决定哪些录制再做物体跟踪和逐段精细标注。</div>
      <div class="row"><span ${T('把画面分成 4 块和大容器区域分别放大再找一遍，能补上电池、螺丝这类小物体；每条录制多调用 4–6 次模型')}>小物体放大识别</span><label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="invz" ${st.inventory_zoom!==false?'checked':''}> 开启</label></div>
      <div class="row"><span ${T('语义标注发现清单外的物体（比如中途从抽屉里拿出来的）时，自动在那一段的画面里让大模型框出来、编号 A1、A2… 加进物体清单，再重跑跟踪、接触、切点和语义。<br>每条录制只补一轮；没有清单外物体时这一步立刻跳过。补上的物体在浏览页标“补找”，可改框或删除')}>清单外补找</span><label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="refind" ${st.offlist_refind!==false?'checked':''}> 开启</label></div>
      <div class="row"><span ${T('每条录制最多列出几个物体。太少会漏掉桌上的东西（背景里的物体会先占满名额）；太多会拖慢物体跟踪，跟踪时间大致和物体数成正比。<br>一般桌面任务 15–25 个够用')}>物体清单上限</span><input type="number" id="invmax" min="4" max="40" value="${st.inventory_max ?? 20}"></div>
      <div class="row"><span ${T('用深度去掉手够不着的远处背景（货架、冰柜里的东西），把名额留给桌上的物体。<br>看框中间区域的距离，超过这个值就不列；任务说明里提到的物体不受影响。<br>没有深度或深度无效时不过滤。填 0 = 不按距离过滤')}>只列多远以内（米）</span><input type="number" id="invreach" min="0" max="5" step="0.1" value="${st.inventory_reach_m ?? 1.5}"></div>
      <div class="row"><span ${T('质量差：30% 以上的时间画面模糊或过暗。这类录制只出质量报告和基础结果')}>精细标注范围</span><select id="fsc"><option value="all" ${st.fine_scope!=='good'?'selected':''}>全部录制</option><option value="good" ${st.fine_scope==='good'?'selected':''}>跳过画面质量差的</option></select></div>
      <div class="row"><span ${T('按录制编号固定抽取，同一条录制每次结果一样。100 = 全做')}>抽样比例（%）</span><input type="number" id="fsp" min="1" max="100" value="${st.fine_sample_pct ?? 100}"></div>
      <div class="row"><span ${T('SAM 2 每秒跟踪几帧。5 帧对判断抓放已经够用，时间约为 10 帧的一半')}>跟踪帧率（帧/秒）</span><input type="number" id="tfps" min="2" max="15" value="${st.track_fps ?? 5}"></div>
      <div><button class="btn pri" id="svb">保存</button></div>
    </div></div>
    <div class="card"><h2>运行资源</h2><div class="bd form" id="resc"><div class="k">读取中…</div></div></div>
    <div class="card"><h2>精细标注（物体跟踪）</h2><div class="bd form" id="samc"><div class="k">读取中…</div></div></div>
    <div class="card"><h2>本机</h2><div class="bd"><dl class="kv">
      <dt>显卡</dt><dd>${sy.gpu ? esc(sy.gpu.name) + ' · ' + (sy.gpu.mem_total/1024).toFixed(0) + ' GB' : '未检测到'}</dd>
      <dt>Python</dt><dd>${esc(sy.python)}</dd>
      <dt>数据目录</dt><dd>${esc(sy.data_dir)}</dd>
      <dt>处理缓存</dt><dd>${fmtB(sy.work_bytes)}</dd>
      <dt>磁盘可用</dt><dd>${fmtB(sy.disk_free)} / ${fmtB(sy.disk_total)}</dd></dl>
      <div style="margin-top:12px;display:flex;gap:8px"><button class="btn" id="clean">清理预览视频缓存</button><button class="btn" data-open="${esc(sy.data_dir)}">打开数据目录</button></div>
      <div class="hint">清理后可在浏览页重跑“入库”重新生成预览。原始数据从不改动。</div></div></div>
    <div class="card" style="grid-column:1/-1"><h2>动作词表<span class="sp"></span><span class="k" id="vver"></span></h2><div class="bd" id="vocab"><div class="k">读取中…</div></div></div>
  </div>`;
  const VC = {items: []};
  const drawVocab = () => {
    $('#vocab').innerHTML = `<div class="hint" style="margin:0 0 8px">模型给每段标动作时只能从这里选一个词。定义会写进提示词，写得越清楚，模型越不容易选错；近义词用于把模型写的其他说法自动改成词表里的词（用空格分隔）。改动后版本号加一，并写进导出的标注文件。</div>
      <table class="t vocab"><tr><th style="width:110px">词</th><th>定义</th><th style="width:22%">例子</th><th style="width:22%">近义词</th><th style="width:40px"></th></tr>
      ${VC.items.map((v, i) => `<tr data-vi="${i}"><td><input data-k="verb" value="${esc(v.verb)}"></td><td><input data-k="def" value="${esc(v.def||'')}"></td><td><input data-k="ex" value="${esc(v.ex||'')}"></td><td><input data-k="syn" value="${esc(v.syn||'')}"></td><td><button class="btn sm ghost" data-vdel="${i}" ${T('删除')}>×</button></td></tr>`).join('')}</table>
      <div style="display:flex;gap:8px;margin-top:10px"><button class="btn" id="vadd">添加一个词</button><span class="sp" style="flex:1"></span><button class="btn ghost" id="vreset">恢复默认词表</button><button class="btn pri" id="vsave">保存词表</button></div>`;
  };
  api('/api/vocab').then(v => { VC.items = v.items.map(x => ({...x})); $('#vver').textContent = `第 ${v.version} 版 · ${v.items.length} 个词${v.custom ? ' · 已自定义' : ''}`; drawVocab(); });
  main.oninput = e => { const tr = e.target.closest('[data-vi]'); if (tr && e.target.dataset.k) VC.items[+tr.dataset.vi][e.target.dataset.k] = e.target.value; };
  const tog = () => { const api = $('#vb').value === 'api'; document.querySelectorAll('.ap').forEach(el => el.style.display = api ? '' : 'none'); document.querySelectorAll('.ol').forEach(el => el.style.display = api ? 'none' : ''); };
  tog(); $('#vb').onchange = tog;
  main.onclick = async e => {
    const lt = e.target.closest('[data-lt]'); if (lt) { setLook({theme: lt.dataset.lt}); document.querySelectorAll('[data-lt]').forEach(b => b.classList.toggle('on', b === lt)); return; }
    const la = e.target.closest('[data-la]'); if (la) { setLook({accent: la.dataset.la}); document.querySelectorAll('[data-la]').forEach(b => b.classList.toggle('on', b === la)); return; }
    if (e.target.id === 'vadd') { VC.items.splice(Math.max(0, VC.items.length - 1), 0, {verb:'', def:'', ex:'', syn:''}); drawVocab(); return; }
    if (e.target.dataset.vdel != null) { VC.items.splice(+e.target.dataset.vdel, 1); drawVocab(); return; }
    if (e.target.id === 'vsave' || e.target.id === 'vreset') {
      if (e.target.id === 'vreset' && !confirm('恢复成内置的默认词表？')) return;
      const v = await api('/api/vocab', {method:'PUT', body: e.target.id === 'vreset' ? {reset:true} : {items: VC.items}});
      VC.items = v.items.map(x => ({...x})); $('#vver').textContent = `第 ${v.version} 版 · ${v.items.length} 个词${v.custom ? ' · 已自定义' : ''}`; drawVocab(); BR.vocab = v.items;
      toast('词表已保存，重新处理语义标注后生效'); return; }
    if (e.target.id === 'sv') { await api('/api/settings', {method:'PUT', body:{vlm_backend: $('#vb').value, api_base: $('#abase').value.trim(), api_key: $('#akey').value.trim(), api_model: $('#amodel').value.trim(), ollama_url: $('#ou').value.trim(), vlm_model: $('#om').value, vlm_model_light: $('#oml').value, vlm_fps: +$('#nf').value, vlm_max_frames: +$('#nmax').value, max_segments: +$('#ms').value, vlm_ctx: +$('#nctx').value, vlm_timeout: +$('#nto').value, contact_gap: +$('#ngap').value, min_seg: +$('#nmin').value, contact_mode: $('#cmode').value, contact_dist_cm: +$('#cdist').value, edge_tol_cm: +$('#cedge').value || 1, contact_min: +$('#cmin').value, tool_min_s: +$('#ctool').value, batch_window_h: +$('#bwin').value, batch_max_reqs: +$('#bmax').value, batch_flush_min: +$('#bflush').value}}); toast('已保存，重新处理录制后生效'); sysTick(); route(); }
    if (e.target.id === 'svr') { const b = {max_jobs: +$('#rmax').value, gpu_jobs: +$('#rgpu').value, api_parallel: Math.max(1, +$('#rapi').value)}; await api('/api/settings', {method:'PUT', body:b}); Object.assign(st, b); toast('已保存，立即生效'); $('#rmax').blur(); resCard(st); return; }
    if (e.target.id === 'svb') { await api('/api/settings', {method:'PUT', body:{fine_scope: $('#fsc').value, fine_sample_pct: +$('#fsp').value, track_fps: +$('#tfps').value, inventory_zoom: $('#invz').checked, offlist_refind: $('#refind').checked, inventory_max: +$('#invmax').value, inventory_reach_m: +$('#invreach').value}}); toast('已保存，之后处理的录制按新设置执行'); return; }
    if (e.target.id === 'tst') { $('#tres').textContent = '正在让模型看一张测试图（首次加载模型可能要几十秒）…'; const r = await api('/api/vlm/test', {method:'POST'}); $('#tres').innerHTML = r.ok ? `<span style="color:var(--ok)">✓ ${esc(r.model)} 回答用时 ${r.sec} s：</span><span class="mono">${esc(JSON.stringify(r.answer))}</span>` : `<span style="color:var(--bad)">${esc(r.msg)}</span>`; }
    if (e.target.id === 'tstb') { $('#tres').textContent = '正在提交测试批量任务（最多等 90 秒）…'; const r = await api('/api/batchq/test', {method:'POST'}); $('#tres').innerHTML = `<span style="color:${r.ok?'var(--ok)':r.id?'var(--warn)':'var(--bad)'}">${esc(r.msg)}</span>`; return; }
    if (e.target.id === 'saminst') { await api('/api/sam/install', {method:'POST'}); toast('开始安装，大约需要下载 3 GB'); }
    if (e.target.id === 'fine') { await api('/api/settings', {method:'PUT', body:{fine_annotation: e.target.checked}}); toast(e.target.checked ? '已开启精细标注' : '已关闭精细标注'); }
    if (e.target.id === 'clean') { const r = await api('/api/cleanup', {method:'POST'}); toast('释放 ' + fmtB(r.freed)); }
    const o = e.target.closest('[data-open]'); if (o) await api('/api/open', {method:'POST', body:{path:o.dataset.open}});
  };
};

route();
