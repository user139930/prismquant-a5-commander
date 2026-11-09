/* PrismQuant A5 · 控制台前端
   原則：所有對外文字都經 esc() 再插入；數字一律等寬；每個結論都要能看到證據 ID。 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const VIEW_META = {
  brief: ['晨報', 'MORNING BRIEF', '今日決策輸入'],
  probe: ['單篇探針', 'SINGLE-NEWS PROBE', '一篇新聞，四個問題'],
  brain: ['大腦拓撲', 'BRAIN TOPOLOGY', '同一個大腦，還是不同大腦？'],
  runs: ['歷史記錄', 'HISTORY', '時間軸與重播'],
  integration: ['整合狀態', 'WIRING', '五個代理的接線狀態'],
  compliance: ['合規', 'GUARDRAILS', '對外說話前的最後一道閘'],
};

const S = {
  view: 'brief', topology: null, meta: null, health: null,
  brief: null, probe: null, brain: null, runs: [], selectedRun: null, busy: false,
};

/* ---------------------------------------------------------------- 工具 */

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function evTags(text) {
  return esc(text).replace(/(?<![A-Za-z0-9])([NP]\d{1,2})(?![0-9])/g, '<span class="ev-chip">$1</span>');
}

function dirTag(dir) {
  const up = ['偏多', '利多', '利好', '看多'];
  const dn = ['偏空', '利空', '看空'];
  if (up.includes(dir)) return `<span class="tag tag-up">${esc(dir)}</span>`;
  if (dn.includes(dir)) return `<span class="tag tag-down">${esc(dir)}</span>`;
  return `<span class="tag tag-flat">${esc(dir || '中性')}</span>`;
}

function bar(value, kind) {
  const pct = Math.max(2, Math.min(100, Math.round((value || 0) * 100)));
  return `<span class="bar ${kind ? 'b-' + kind : ''}"><i style="width:${pct}%"></i></span>`;
}

function confRing(score) {
  const c = 2 * Math.PI * 11;
  const off = c * (1 - Math.max(0, Math.min(1, score || 0)));
  return `<svg class="conf-ring" viewBox="0 0 26 26">
    <circle class="bg" cx="13" cy="13" r="11" fill="none" stroke-width="2.6"/>
    <circle class="fg" cx="13" cy="13" r="11" fill="none" stroke-width="2.6"
      stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
      transform="rotate(-90 13 13)"/></svg>`;
}

function confPill(conf) {
  const score = conf && conf.score != null ? conf.score : 0;
  const level = (conf && conf.level) || '—';
  const cls = score >= 0.7 ? 'high' : score < 0.5 ? 'low' : '';
  return `<span class="conf-pill ${cls}">${confRing(score)}<b>${score.toFixed(2)}</b><span>置信度 ${esc(level)}</span></span>`;
}

function ago(hours) {
  if (hours == null) return '時間不明';
  if (hours < 1) return Math.round(hours * 60) + ' 分鐘前';
  if (hours < 24) return hours.toFixed(1) + ' 小時前';
  return (hours / 24).toFixed(1) + ' 天前';
}

async function api(path, opt = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opt });
  const data = await res.json().catch(() => ({ detail: { message: '回應不是有效 JSON' } }));
  if (!res.ok) {
    const d = data.detail || {};
    const msg = typeof d === 'string' ? d : (d.message || ('HTTP ' + res.status));
    const err = new Error(msg);
    err.code = d.code;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg, isErr) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2600);
}

function setBusy(btn, busy, label) {
  if (!btn) return;
  if (busy) {
    btn.dataset.label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${esc(label || '處理中')}`;
  } else {
    btn.disabled = false;
    btn.innerHTML = btn.dataset.label || btn.innerHTML;
  }
}

/* ---------------------------------------------------------------- 導航 */

function switchView(view, keepHash) {
  S.view = view;
  $$('.nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.view === view));
  $$('.view').forEach(v => v.classList.toggle('is-active', v.id === 'view-' + view));
  const meta = VIEW_META[view] || VIEW_META.brief;
  $('#crumb').textContent = `${meta[0]} · ${meta[1]}`;
  $('#viewTitle').textContent = meta[2];
  if (!keepHash && location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  $('.main').scrollTo({ top: 0, behavior: 'smooth' });
  if (view === 'brain') loadBrain();
  if (view === 'runs') loadRuns();
  if (view === 'compliance') loadCompliance();
  if (view === 'integration') loadIntegration();
}

/* ---------------------------------------------------------------- 狀態 */

function renderRail(health) {
  const rows = [
    ['A1 新聞', health.a1],
    ['A2 政要', health.a2],
    ['A4 風控', health.a4],
  ];
  $('#railUpstream').innerHTML = rows.map(([name, st]) => {
    const ok = st && st.ok;
    const cls = ok ? 'ok' : (st && st.status === 'not_connected' ? '' : 'bad');
    const ms = st && st.latency_ms != null ? st.latency_ms + 'ms' : (ok ? '' : 'off');
    return `<div class="up-row ${cls}"><span class="dot"></span><span>${esc(name)}</span><span class="ms">${esc(ms)}</span></div>`;
  }).join('');
}

function renderStatus(payload) {
  const up = payload ? payload.upstream : null;
  const q = payload ? payload.data_quality : null;
  const a1 = up ? up.a1 : null;
  const a2 = up ? up.a2 : null;
  const brain = payload ? payload.brain : null;

  const tile = (name, val, cls) => `<div class="status-tile"><span class="st-name">${esc(name)}</span><span class="st-val ${cls}"><span class="dot"></span>${esc(val)}</span></div>`;
  const stat = (m) => {
    if (!m) return ['待檢查', ''];
    if (m.ok) return ['已連線', 'ok'];
    return [m.sample_mode ? '未連線' : '未連線', 'bad'];
  };
  const [s1, c1] = stat(a1);
  const [s2, c2] = stat(a2);

  $('#statusGrid').innerHTML =
    tile('A1 新聞', s1, c1) +
    tile('A2 政要', s2, c2) +
    tile('A4 風控', '未接入', 'warn') +
    tile('大腦', brain ? (brain.mode === 'llm' ? 'LLM · ' + brain.label : '規則 · ' + brain.label) : '—', brain && brain.degraded ? 'warn' : 'ok');

  if (payload && payload.source_note) {
    // 只顯示一次提示，放在 status 之後
  }
}

/* ---------------------------------------------------------------- 晨報 */

function secBlock(num, title, sub, inner) {
  return `<section class="sec">
    <div class="sec-head"><span class="sec-num">${num}</span><h3>${esc(title)}</h3><span class="sec-sub">${esc(sub)}</span></div>
    ${inner}
  </section>`;
}

function bullets(list) {
  if (!list || !list.length) return `<div class="hint">（無）</div>`;
  return `<ul class="bullets">${list.map(x => `<li>${evTags(x)}</li>`).join('')}</ul>`;
}

function renderBrief(p) {
  S.brief = p;
  const out = $('#briefOut');
  const conf = p.confidence || {};
  const q = p.data_quality || {};
  const brain = p.brain || {};
  const flags = p.quality_flags || {};
  const notes = [];

  if (p.source_note) notes.push(`<div class="note-line bad">⚠ ${esc(p.source_note)}</div>`);
  if (brain.degraded) notes.push(`<div class="note-line">↯ ${esc(brain.degrade_reason || '已降級')}</div>`);
  if (q.sample_mode && !p.source_note) notes.push(`<div class="note-line">ℹ 上游標記為示例資料，本頁不可用作真實決策。</div>`);

  const hero = `<div class="hero">
    <div class="hero-eyebrow">
      <span>MORNING BRIEF</span><span class="sep">/</span>
      <span>${esc((p.generated_at || '').replace('T', ' ').slice(0, 16))}</span><span class="sep">/</span>
      <span>${esc(brain.mode === 'llm' ? 'LLM' : 'RULE')} · ${esc(brain.label || '')}</span>
    </div>
    <h2>${evTags(p.headline || '')}</h2>
    <div class="hero-meta">
      ${confPill(conf)}
      <span class="meta-item">${flags.reading_minutes ?? p.reading_minutes ?? '—'} 分鐘讀完</span>
      <span class="meta-item">${q.distinct_publishers || 0} 個獨立媒體</span>
      <span class="meta-item">${(p.evidence || []).length} 條證據</span>
      <span class="meta-item">${(p.conflicts || []).length} 組分歧</span>
      ${p.usage && p.usage.total_tokens ? `<span class="meta-item">${p.usage.total_tokens.toLocaleString()} tokens${p.usage.priced && p.usage.cost_usd ? ' · ≈ US$' + p.usage.cost_usd.toFixed(4) : ''}</span>` : ''}
      <div class="hero-actions">
        <button class="btn btn-ghost btn-sm" id="btnCopy">複製全文</button>
        <button class="btn btn-ghost btn-sm" id="btnMd">匯出 .md</button>
        <button class="btn btn-ghost btn-sm" id="btnTg">Telegram 預覽</button>
        <button class="btn btn-ghost btn-sm" id="btnReplaySelf">重播</button>
      </div>
    </div>
  </div>`;

  const s = p.sections || {};
  const watchCards = (s.watch || []).map(w => `<div class="watch-card">
      <div class="w-item">${esc(w.item || '')}</div>
      <div class="w-why">${esc(w.why || '')}</div>
      <div class="w-line"><em>觸發</em><span>${esc(w.trigger || '')}</span></div>
      <div class="w-line"><em>作廢</em><span>${esc(w.invalidates || '')}</span></div>
    </div>`).join('');

  const transRows = (p.transmission || []).map(t => `<tr>
      <td>${esc(t.from)}</td>
      <td>${esc(t.channel)}</td>
      <td>${(t.to || []).map(x => `<span class="sym">${esc(x)}</span>`).join('')}</td>
      <td>${dirTag(t.direction)}</td>
      <td><div style="display:flex;align-items:center;gap:7px">${bar(t.strength_value, t.direction === '偏空' ? 'down' : t.direction === '偏多' ? 'up' : '')}<span class="mono" style="font-size:10.5px;color:var(--tx-3)">${esc(t.strength)}</span></div></td>
      <td class="num">${esc(t.horizon)}</td>
      <td>${(t.evidence || []).map(x => `<span class="ev-chip">${esc(x)}</span>`).join('')}</td>
    </tr>`).join('');

  const conflictBlocks = (p.conflicts || []).map(c => `<div class="conflict">
      <div class="conflict-top"><span class="ct-topic">${esc(c.topic)}</span><span class="tag tag-flat">方向相反</span></div>
      <div class="conflict-cols">
        <div class="conflict-col"><div class="cc-who">A1 新聞面</div><div class="cc-txt">${esc(c.a1)}</div></div>
        <div class="conflict-col"><div class="cc-who">A2 政策面</div><div class="cc-txt">${esc(c.a2)}</div></div>
      </div>
      <div class="conflict-res"><em>裁決</em>　${esc(c.resolution)}<br><em>作廢條件</em>　${esc(c.breaks_if)}</div>
    </div>`).join('');

  const scnCls = { '基準情境': 'base', '偏多情境': 'bull', '偏空情境': 'bear', '倉位含義': 'pos' };
  const scnCards = (p.scenarios || []).map(s2 => `<div class="scn ${scnCls[s2.name] || ''}">
      <div class="scn-top"><span class="scn-name">${esc(s2.name)}</span>
        <span class="scn-prob">${s2.prob == null ? '—' : Math.round(s2.prob * 100) + '%'}</span></div>
      <p>${esc(s2.path)}</p>
      <div class="trig">${(s2.triggers || []).map(t => '· ' + esc(t)).join('<br>')}</div>
    </div>`).join('');

  const evRows = (p.evidence || []).map(e => `<div class="ev-row">
      <span class="ev-id">${esc(e.id)}</span>
      <span><span class="tag ${e.agent === 'A1' ? 'tag-a1' : 'tag-a2'}">${esc(e.agent)}</span></span>
      <div>
        <div class="ev-title">${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title)}</div>
        <div class="ev-sub"><span>${esc(e.publisher || '')}</span><span>·</span><span>${esc(ago(e.age_hours))}</span><span>·</span><span>${esc(e.topic || '')}</span>${(e.instruments || []).length ? '<span>·</span><span>' + e.instruments.map(esc).join('/') + '</span>' : ''}</div>
      </div>
      <div class="ev-side">${dirTag(e.sentiment)}<span class="mono" style="font-size:10px;color:var(--tx-4)">${(e.confidence || 0).toFixed(2)}</span></div>
    </div>`).join('');

  const pubs = q.publishers || [];
  const maxPub = Math.max(1, ...pubs.map(x => x.count));
  const qualityInner = `
    <div class="quality-list">
      ${pubs.map(x => `<div class="q-item"><span class="qn" title="${esc(x.name)}">${esc(x.name)}</span>${bar(x.count / maxPub)}<span class="qc">${x.count}</span></div>`).join('') || '<div class="hint">（無新聞來源）</div>'}
    </div>
    ${(q.notes || []).map(n => `<div class="note-line info">· ${esc(n)}</div>`).join('')}
    ${q.single_source_topics && q.single_source_topics.length ? `<div class="note-line">單一來源已降級：${esc(q.single_source_topics.join('、'))}</div>` : ''}`;

  const confInner = `
    <div style="display:flex;flex-direction:column;gap:8px;align-items:flex-start">
      ${confPill(conf)}
      <span class="hint">這個分數不是模型自評，是加減分規則算出來的，每一項都列在下面。</span>
    </div>
    <ul class="conf-reasons">${(conf.reasons || []).map(r => `<li>${esc(r)}</li>`).join('')}</ul>`;

  const guard = p.guard || {};
  const guardInner = `<div class="note-line ${guard.passed ? '' : 'bad'}" style="margin-top:0">
      ${guard.passed ? '⛨' : '⛔'} ${esc(guard.note || '')}
      ${guard.blocked && guard.blocked.length ? `<br>已攔截：${guard.blocked.map(b => esc(b.phrase)).join('、')}` : ''}
      ${guard.broken_evidence && guard.broken_evidence.length ? `<br>無法追溯的證據：${guard.broken_evidence.map(esc).join('、')}` : ''}
    </div>
    <div class="hint" style="margin-top:8px">${esc(p.disclaimer || '')}</div>`;

  const cov = p.coverage || {};
  const covChips = (cov.agents || []).map(a => {
    const cls = a.present ? 'ok' : (a.status === 'empty' ? 'warn' : 'missing');
    const label = a.status === 'ok' ? a.note : (a.status === 'empty' ? '在線但無資料' : (a.status === 'offline' ? '未連線' : '未實作'));
    return `<span class="cov-chip ${cls}" title="${esc(a.note)}${a.needs ? '｜需要：' + esc(a.needs) : ''}"><b>${esc(a.id)}</b>${esc(a.name)}<em>${esc(label)}</em></span>`;
  }).join('');
  const missingNotes = (cov.agents || []).filter(a => a.status === 'missing').map(a => `${a.id} ${a.name}：${a.note}`);
  const covBlock = (cov.agents || []).length ? `<div class="coverage">
      <div class="cov-head"><span>這一頁用了誰的資料</span><span class="hint">${esc(cov.summary || '')}</span></div>
      <div class="cov-row">${covChips}</div>
      ${missingNotes.length ? `<div class="cov-note">${missingNotes.map(esc).join('　·　')}</div>` : ''}
    </div>` : '';

  out.innerHTML = (notes.length ? `<div class="banner-row">${notes.join('')}</div>` : '') + hero + covBlock +
    `<div id="diffSlot"></div>` +
    `<div class="stack">
      ${secBlock('1', '發生了什麼', 'FACTS', bullets(s.what_happened))}
      ${secBlock('2', '這意味著什麼', 'SO WHAT', bullets(s.so_what))}
      ${secBlock('3', '要盯什麼', 'WATCH', `<div class="watch-grid">${watchCards || '<div class="hint">（無）</div>'}</div>`)}
    </div>
    ${secBlock('4', '傳導矩陣', 'TRANSMISSION', `<div class="table-wrap"><table>
      <thead><tr><th>起點</th><th>通道</th><th>標的</th><th>方向</th><th>強度</th><th>時間尺度</th><th>證據</th></tr></thead>
      <tbody>${transRows || '<tr><td colspan="7" class="hint">（無）</td></tr>'}</tbody></table></div>`)}
    ${conflictBlocks ? secBlock('5', '分歧裁決', 'CONFLICT', conflictBlocks) : ''}
    ${secBlock(conflictBlocks ? '6' : '5', '情境推演', 'SCENARIOS', `<div class="scn-grid">${scnCards}
        <div class="scn pos empty">
          <div class="scn-top"><span class="scn-name">倉位含義</span><span class="scn-prob">—</span></div>
          <p>留空：需要 A4 風控回報 gate 狀態，A5 才能講倉位。目前 A4 未接入，這一段不生成。</p>
          <div class="trig">等 A4 提供 /api/v1/risk/check</div>
        </div>
        <div class="scn pos empty">
          <div class="scn-top"><span class="scn-name">執行意圖</span><span class="scn-prob">—</span></div>
          <p>留空：A3 未實作，本頁不產生任何標的建議或下單意圖，只做研究輸出。</p>
          <div class="trig">等 A3 提供 /api/v1/intents</div>
        </div>
      </div>
      <div class="hint" style="margin-top:10px">情境權重由情緒分佈與交叉驗證強度加權得出，是可解釋的相對權重，不是價格預測。</div>`)}
    ${secBlock(conflictBlocks ? '7' : '6', '證據鏈', 'EVIDENCE', `<div class="ev-list">${evRows || '<div class="hint">（無）</div>'}</div>`)}
    <div class="two-col">
      <section class="sec"><div class="sec-head"><span class="sec-num">D</span><h3>資料充分性</h3><span class="sec-sub">DATA QUALITY</span></div>${qualityInner}</section>
      <section class="sec"><div class="sec-head"><span class="sec-num">C</span><h3>置信度</h3><span class="sec-sub">CONFIDENCE</span></div>${confInner}</section>
    </div>
    ${secBlock('G', '合規與風險聲明', 'GUARDRAIL', guardInner)}
    <details class="raw-box">
      <summary>原始資料（給驗收與除錯用）</summary>
      <pre class="raw-pre">${esc(JSON.stringify(p, null, 2))}</pre>
    </details>
    <section class="sec">
      <div class="sec-head"><span class="sec-num">?</span><h3>追問指揮官</h3><span class="sec-sub">FOLLOW-UP</span></div>
      <p class="hint">只基於這份晨報的證據回答；晨報裡沒有的，會直接說沒有。</p>
      <div class="ask-chips">
        <button class="chip" data-follow="為什麼把關稅判成偏空？">為什麼關稅偏空？</button>
        <button class="chip" data-follow="如果只看港股，這份晨報該怎麼讀？">只看港股怎麼讀？</button>
        <button class="chip" data-follow="哪一條證據最不可靠？為什麼？">哪條證據最弱？</button>
      </div>
      <div class="chat-log" id="chatLog"></div>
      <div class="ask-row">
        <input id="followInput" class="input" placeholder="追問，例如：這個判斷最可能錯在哪裡？">
        <button class="btn btn-primary" id="btnFollow">追問</button>
      </div>
    </section>`;

  renderStatus(p);
  $('#footRun').textContent = `run ${p.run_id || '—'} · ${p.stats ? p.stats.latency_ms + 'ms' : ''}`;

  $('#btnCopy').onclick = () => copyText(p.brief_text || '');
  const mdBtn = $('#btnMd');
  if (mdBtn) mdBtn.onclick = () => downloadText('prismquant-brief-' + (p.run_id || 'x') + '.md', briefMarkdown(p));
  $('#btnTg').onclick = () => openTelegram(p);
  $('#btnReplaySelf').onclick = () => replayRun(p.run_id);
  $$('#briefOut .chip[data-follow]').forEach(b => b.onclick = () => followUp(b.dataset.follow));
  const fi = $('#followInput');
  if (fi) fi.addEventListener('keydown', e => { if (e.key === 'Enter') followUp(fi.value); });
  const bf = $('#btnFollow');
  if (bf) bf.onclick = () => followUp($('#followInput').value);
  loadDiff(p.run_id);
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast('已匯出 ' + filename);
}

function briefMarkdown(p) {
  const s = p.sections || {};
  const stamp = (p.generated_at || '').replace('T', ' ').slice(0, 16);
  const md = [];
  md.push('# PrismQuant 晨報 · ' + stamp);
  md.push('');
  md.push('> ' + (p.headline || ''));
  md.push('');
  md.push('- Run：`' + (p.run_id || '') + '`');
  md.push('- 模式：' + ((p.brain || {}).mode || '') + ' / ' + ((p.brain || {}).label || ''));
  md.push('- 置信度：' + ((p.confidence || {}).score || 0) + '（' + ((p.confidence || {}).level || '') + '）');
  md.push('- 獨立媒體：' + ((p.data_quality || {}).distinct_publishers || 0) + '｜證據 ' + ((p.evidence || []).length) + ' 條');
  md.push('');
  md.push('## 一、發生了什麼');
  md.push('');
  (s.what_happened || []).forEach(x => md.push('- ' + x));
  md.push('');
  md.push('## 二、這意味著什麼');
  md.push('');
  (s.so_what || []).forEach(x => md.push('- ' + x));
  md.push('');
  md.push('## 三、要盯什麼');
  md.push('');
  (s.watch || []).forEach(w => md.push('- **' + w.item + '**：' + w.why + '；觸發 ' + w.trigger + '；作廢 ' + w.invalidates));
  if ((p.conflicts || []).length) {
    md.push('');
    md.push('## 四、分歧裁決');
    md.push('');
    p.conflicts.forEach(c => {
      md.push('- **' + c.topic + '**');
      md.push('  - A1：' + c.a1);
      md.push('  - A2：' + c.a2);
      md.push('  - 裁決：' + c.resolution);
      md.push('  - 作廢：' + c.breaks_if);
    });
  }
  md.push('');
  md.push('## 證據鏈');
  md.push('');
  (p.evidence || []).forEach(e => md.push('- `' + e.id + '` [' + e.agent + '] ' + e.title + ' — ' + (e.publisher || '') + '｜' + (e.sentiment || '')));
  md.push('');
  md.push('## 置信度');
  md.push('');
  ((p.confidence || {}).reasons || []).forEach(r => md.push('- ' + r));
  md.push('');
  md.push('---');
  md.push('');
  md.push(p.disclaimer || '');
  return md.join('\n');
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已複製全文');
  } catch (e) {
    toast('瀏覽器不允許剪貼簿，請手動選取', true);
  }
}

const STAGES = [
  ['upstream', '取上游'],
  ['analyze', '規則層分析'],
  ['llm', '大模型改寫'],
  ['guard', '合規檢查'],
];

function showProgress() {
  $('#briefOut').innerHTML = `<div class="progress-card">
    <div class="pg-head"><span><b>指揮官工作中</b>　A5 → A1 / A2 → 推理 → 護欄</span><span class="pg-timer" id="pgTimer">0.0s</span></div>
    <ul class="pg-list" id="pgList">
      ${STAGES.map(([k, label]) => `<li data-stage="${k}"><span class="pg-dot"></span><span class="pg-label">${label}</span><span class="pg-msg"></span></li>`).join('')}
    </ul>
  </div>`;
}

function pushProgress(ev) {
  if (ev.stage === 'done') $$('#pgList li').forEach(x => { x.className = 'done'; });
  const li = document.querySelector(`#pgList li[data-stage="${ev.stage}"]`);
  if (li) {
    li.className = ev.done ? 'done' : 'active';
    const msg = li.querySelector('.pg-msg');
    if (msg) msg.textContent = ev.message || '';
  }
  if (ev.upstream) {
    const vals = $$('#statusGrid .st-val');
    [[0, ev.upstream.a1], [1, ev.upstream.a2]].forEach(([i, ok]) => {
      if (!vals[i]) return;
      vals[i].className = 'st-val ' + (ok ? 'ok' : 'bad');
      vals[i].innerHTML = '<span class="dot"></span>' + (ok ? '已連線' : '未連線');
    });
  }
}

async function loadStoredBrief(runId) {
  switchView('brief', true);
  $('#briefOut').innerHTML = `<div class="skel" style="height:120px"></div><div class="skel" style="height:180px"></div>`;
  try {
    const row = await api('/api/v1/runs/' + encodeURIComponent(runId));
    history.replaceState(null, '', '#brief/' + runId);
    renderBrief(row.payload);
    toast('已載入 ' + ((row.created_at || '').replace('T', ' ').slice(5, 16)) + ' 的晨報');
  } catch (e) {
    toast('找不到這份晨報，改為生成新的', true);
    runBrief('生成今天的晨報');
  }
}

async function runBrief(question) {
  if (S.busy) return;
  S.busy = true;
  const btn = $('#btnRun');
  setBusy(btn, true, S.topology && S.topology !== 'rule' ? '推理中' : '匯總中');
  showProgress();
  const t0 = performance.now();
  const timer = setInterval(() => {
    const el = $('#pgTimer');
    if (el) el.textContent = ((performance.now() - t0) / 1000).toFixed(1) + 's';
  }, 100);
  try {
    const params = new URLSearchParams({ question: question || '' });
    if (S.topology) params.set('topology', S.topology);
    const res = await fetch('/api/v1/commander/stream?' + params.toString());
    if (!res.ok || !res.body) throw new Error('串流請求失敗（HTTP ' + res.status + '）');

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(chunk.slice(6)); } catch (e) { continue; }
        if (ev.type === 'progress') pushProgress(ev);
        else if (ev.type === 'result') {
          clearInterval(timer);
          renderBrief(ev.payload);
          loadRuns(true);
        } else if (ev.type === 'error') {
          throw new Error(ev.message || '生成失敗');
        }
      }
    }
  } catch (e) {
    $('#briefOut').innerHTML = `<div class="empty"><div class="empty-mark">⚠</div><h3>這次沒跑成功</h3><p>${esc(e.message)}</p></div>`;
    toast(e.message, true);
  } finally {
    clearInterval(timer);
    S.busy = false;
    setBusy(btn, false);
  }
}

/* ---------------------------------------------------------------- 追問 */

async function followUp(question) {
  const q = (question || '').trim();
  if (!q || !S.brief) return;
  const log = $('#chatLog');
  if (!log) return;
  log.insertAdjacentHTML('beforeend', `<div class="chat-user">${esc(q)}</div>`);
  const bid = 'cb' + Date.now();
  log.insertAdjacentHTML('beforeend',
    `<div class="chat-bot" id="${bid}"><div class="cb-body"><span class="spinner" style="border-color:rgba(200,220,255,.25);border-top-color:#8ab4f8"></span>指揮官正在找證據…</div></div>`);
  const input = $('#followInput');
  if (input) input.value = '';
  try {
    const r = await api('/api/v1/commander/ask', {
      method: 'POST',
      body: JSON.stringify({ run_id: S.brief.run_id, question: q }),
    });
    const cited = (r.cited || []).map(c => `<span class="ev-chip">${esc(c)}</span>`).join('');
    const retrieval = r.mode === 'retrieval';
    const node = document.getElementById(bid);
    if (node) {
      node.innerHTML = `
        <div class="cb-head">
          <span class="tag ${retrieval ? 'tag-flat' : 'tag-a1'}">${retrieval ? '證據檢索' : 'A5 追問'}</span>
          <span class="hint">${esc((r.brain && r.brain.model) || '')}</span>
        </div>
        <div class="cb-body">${esc(r.answer)}</div>
        <div class="cb-foot">
          ${cited ? `<span>引用 ${cited}</span>` : ''}
          ${r.limits ? `<span>· ${esc(r.limits)}</span>` : ''}
          <span>· ${r.elapsed_ms}ms</span>
        </div>`;
    }
    loadRuns(true);
  } catch (e) {
    const node = document.getElementById(bid);
    if (node) node.innerHTML = `<div class="cb-body">追問失敗：${esc(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- 與上一份的差異 */

async function loadDiff(runId) {
  const slot = $('#diffSlot');
  if (!slot || !runId) return;
  try {
    const d = await api('/api/v1/commander/diff/' + encodeURIComponent(runId));
    if (!d.has_previous) {
      slot.innerHTML = `<div class="card diff-card">
        <div class="card-head"><h3>與上一份的差異</h3><p>這是第一份晨報，沒有可比較的上一份。</p></div></div>`;
      return;
    }
    slot.innerHTML = diffMarkup(d, false);
  } catch (e) { /* 差異卡不是必需，失敗就靜默 */ }
}

/* ---------------------------------------------------------------- 兩份比較 */

function diffMarkup(d, pair) {
  const tags = [];
  (d.added_topics || []).forEach(t => tags.push(`<span class="diff-tag add">+ ${esc(t)}</span>`));
  (d.dropped_topics || []).forEach(t => tags.push(`<span class="diff-tag drop">− ${esc(t)}</span>`));
  (d.direction_flips || []).forEach(f => tags.push(`<span class="diff-tag flip">${esc(f.topic)} ${esc(f.from)}→${esc(f.to)}</span>`));
  const dc = d.confidence || { from: 0, to: 0, delta: 0 };
  const arrow = dc.delta > 0 ? '↑' : dc.delta < 0 ? '↓' : '→';
  const head = (pair && d.base && d.target)
    ? `<div class="cmp-head"><div><span class="hint">基準</span>${esc((d.base.at || '').slice(5, 16))}　${esc(d.base.headline || '')}</div>
       <div><span class="hint">對比</span>${esc((d.target.at || '').slice(5, 16))}　${esc(d.target.headline || '')}</div></div>`
    : '';
  return `<div class="card diff-card">
    ${head}
    <div class="diff-line">${esc(d.summary)}</div>
    ${tags.length ? `<div class="diff-tags">${tags.join('')}</div>` : ''}
    <div class="hint" style="margin-top:9px">置信度 ${(dc.from || 0).toFixed(2)} ${arrow} ${(dc.to || 0).toFixed(2)}　·　新增 ${(d.new_evidence || []).length} 條證據、少了 ${(d.gone_evidence || []).length} 條</div>
  </div>`;
}

/* ---------------------------------------------------------------- Telegram */

async function openTelegram(payload) {
  try {
    const r = await api('/api/v1/telegram/render', { method: 'POST', body: JSON.stringify({ payload }) });
    $('#tgMsg').textContent = r.message;
    $('#tgTime').textContent = new Date().toLocaleTimeString('zh-HK', { hour: '2-digit', minute: '2-digit' });
    $('#tgHint').textContent = '未設定 Bot token，這是預覽；設定後會直接推到頻道。';
    $('#tgModal').hidden = false;
    $('#tgCopy').onclick = () => copyText(r.message);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- 探針 */

function renderProbe(p) {
  S.probe = p;
  const c = p.confidence || {};
  const co = p.corroboration || {};
  const chain = (p.chain || []).map(s => `<div class="chain-step">
      <div class="cs-label">${esc(s.step)}</div>
      <div class="cs-body">${esc(s.detail)}</div>
    </div>`).join('');

  const assets = (p.assets || []).map(a => `<tr>
      <td><span class="sym">${esc(a.symbol)}</span></td>
      <td>${dirTag(a.direction)}</td>
      <td class="num">${esc(a.horizon)}</td>
    </tr>`).join('');

  const matched = (co.matched || []).map(m => `<div class="ev-row" style="grid-template-columns:34px 30px 1fr auto">
      <span class="ev-id">${esc(m.id || '—')}</span>
      <span><span class="tag ${m.agent === 'A1' ? 'tag-a1' : 'tag-a2'}">${esc(m.agent || '')}</span></span>
      <div class="ev-title">${esc(m.title)}<div class="ev-sub"><span>${esc(m.publisher || '')}</span><span>·</span><span>相似度 ${(m.similarity || 0).toFixed(2)}</span></div></div>
      <div class="ev-side"></div>
    </div>`).join('');

  const sameTopic = (co.same_topic || []).map(m => `<div class="ev-row" style="grid-template-columns:34px 30px 1fr auto">
      <span class="ev-id">${esc(m.id || '—')}</span>
      <span><span class="tag ${m.agent === 'A1' ? 'tag-a1' : 'tag-a2'}">${esc(m.agent || '')}</span></span>
      <div class="ev-title">${esc(m.title)}<div class="ev-sub"><span>${esc(m.publisher || '')}</span><span>·</span><span>同主題、不同事件</span></div></div>
      <div class="ev-side"></div>
    </div>`).join('');

  const out = $('#probeOut');
  out.innerHTML = `
    <div class="probe-hero">
      <div class="ph-top">
        <span class="verdict ${esc(p.verdict_level)}">${esc(p.verdict)}</span>
        <span class="tag tag-flat">${esc(p.topic)}</span>
      </div>
      <h3>${esc(p.input.title)}</h3>
      <div class="probe-meta">
        <span class="meta-item">${esc(p.input.source || '未標來源')}</span>
        <span class="meta-item">方向 ${esc(p.direction)}</span>
        <span class="meta-item">強度 ${esc(p.strength_word)} · ${p.strength}</span>
        <span class="meta-item">${p.input.chars} 字</span>
      </div>
    </div>

    <section class="sec">
      <div class="sec-head"><span class="sec-num">1</span><h3>傳導鏈</h3><span class="sec-sub">TRANSMISSION</span></div>
      <div class="chain">${chain}</div>
    </section>

    <div class="two-col">
      <section class="sec">
        <div class="sec-head"><span class="sec-num">2</span><h3>受影響標的</h3><span class="sec-sub">ASSETS</span></div>
        <div class="table-wrap"><table><thead><tr><th>標的</th><th>方向</th><th>時間尺度</th></tr></thead>
        <tbody>${assets || '<tr><td colspan="3" class="hint">（無）</td></tr>'}</tbody></table></div>
      </section>
      <section class="sec">
        <div class="sec-head"><span class="sec-num">C</span><h3>置信度</h3><span class="sec-sub">CONFIDENCE</span></div>
        <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-start">${confPill(c)}</div>
        <ul class="conf-reasons">${(c.reasons || []).map(r => `<li>${esc(r)}</li>`).join('')}</ul>
      </section>
    </div>

    <section class="sec">
      <div class="sec-head"><span class="sec-num">3</span><h3>交叉驗證</h3><span class="sec-sub">CORROBORATION</span></div>
      <div class="note-line info" style="margin-top:0">${esc(co.note || '')}</div>
      <div class="pill-row">${(co.publishers || []).map(x => `<span class="meta-item">${esc(x)}</span>`).join('')}</div>
      ${matched ? `<div style="margin-top:10px"><div class="hint" style="margin-bottom:4px">同事件報導</div><div class="ev-list">${matched}</div></div>` : ''}
      ${sameTopic ? `<div style="margin-top:10px"><div class="hint" style="margin-bottom:4px">同主題、不同事件</div><div class="ev-list">${sameTopic}</div></div>` : ''}
    </section>

    <section class="sec">
      <div class="sec-head"><span class="sec-num">4</span><h3>什麼會改變這個判斷</h3><span class="sec-sub">INVALIDATION</span></div>
      <ul class="bullets">${(p.what_would_change || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>
      ${(p.caveats || []).map(x => `<div class="note-line info">· ${esc(x)}</div>`).join('')}
      <div class="note-line" style="margin-top:10px">⛨ ${esc((p.guard || {}).note || '')}</div>
    </section>`;

  $('#footRun').textContent = `probe ${p.run_id || '—'} · ${p.stats ? p.stats.latency_ms + 'ms' : ''}`;
}

async function doProbe(text, title, source, url) {
  const btn = $('#btnProbe');
  setBusy(btn, true, '分析中');
  try {
    const p = await api('/api/v1/commander/probe', {
      method: 'POST',
      body: JSON.stringify({ title: title || '', text, source: source || '', url: url || '' }),
    });
    renderProbe(p);
    loadRuns(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

const PROBE_SAMPLE = {
  title: '美國擬對進口半導體設備加徵 25% 關稅，要求 90 天內申報供應鏈',
  source: 'SCMP',
  url: 'https://www.scmp.com/business/example-chip-tariff',
  text: '美國商務部宣布，將對進口半導體製造設備加徵 25% 關稅，並要求相關廠商在 90 天內申報供應鏈來源與最終用途。業界人士表示，此舉將直接推高先進封裝與量測設備的採購成本，部分訂單可能轉向本土供應商，但短期內產能難以替代。市場關注此舉是否會延伸至零部件與售後服務。',
};

/* ---------------------------------------------------------------- 大腦 */

async function loadBrain(force) {
  if (S.brain && !force) return renderBrain();
  try {
    S.brain = await api('/api/v1/brain');
    if (!S.topology) S.topology = (S.brain.configured || {}).topology || 'lens';
    renderBrain();
  } catch (e) {
    $('#topoGrid').innerHTML = `<div class="hint">${esc(e.message)}</div>`;
  }
}

function renderBrain() {
  const d = S.brain;
  if (!d) return;
  const plans = d.plans || {};
  const sel = S.topology || 'lens';

  $('#topoGrid').innerHTML = (d.topologies || []).map(t => {
    const isSel = t.key === sel;
    const isCfg = t.key === (d.configured || {}).topology;
    return `<button class="topo ${isSel ? 'is-sel' : ''}" data-topo="${esc(t.key)}">
      <span class="tp-key">${esc(t.key.toUpperCase())}</span>
      <span class="tp-name">${esc(t.label)}</span>
      <span class="tp-desc">${esc(t.tagline)}</span>
      ${isCfg ? '<span class="tp-badge">目前設定</span>' : ''}
    </button>`;
  }).join('');
  $$('.topo').forEach(b => b.onclick = () => {
    S.topology = b.dataset.topo;
    $('#badgeBrain').textContent = (plans[S.topology] || {}).label || S.topology;
    renderBrain();
  });

  const plan = plans[sel] || {};
  const roles = plan.roles || {};
  const node = (name, key, model, temp, core) => `<div class="bd-node ${core ? 'core' : ''}">
      <div class="bn-name">${esc(name)}</div>
      <div class="bn-model">${esc(model)}${temp != null ? ' · T=' + temp : ''}</div>
    </div>`;
  const isRule = sel === 'rule';
  const a1 = roles.a1, a2 = roles.a2, a5 = roles.a5;

  $('#brainDiagram').innerHTML = `
    <div class="bd-col">
      <div class="bd-title">感知層 · SENSE</div>
      ${node('A1 新聞情報', 'a1', isRule ? '規則引擎（無模型）' : (a1 || {}).model, isRule ? null : (a1 || {}).temperature)}
      ${node('A2 政要監控', 'a2', isRule ? '規則引擎（無模型）' : (a2 || {}).model, isRule ? null : (a2 || {}).temperature)}
    </div>
    <div class="bd-arrow">→</div>
    <div class="bd-col">
      <div class="bd-title">指揮層 · COMMAND</div>
      ${node('A5 指揮官', 'a5', isRule ? '規則引擎（無模型）' : (a5 || {}).model, isRule ? null : (a5 || {}).temperature, true)}
      ${node('A4 風控（未接入）', 'a4', '只准規則，不配大腦', null)}
    </div>
    <div class="bd-arrow">→</div>
    <div class="bd-col">
      <div class="bd-title">交付 · DELIVER</div>
      ${node('Web 控制台', 'web', '本頁 / 即時', null)}
      ${node('Telegram', 'tg', '/brief · /probe', null)}
    </div>`;

  const rows = (d.comparison_table || []).map(r => `<tr>
      <td style="color:var(--tx-3)">${esc(r.dim)}</td>
      <td>${esc(r.shared)}</td>
      <td style="background:rgba(91,156,248,.05);color:#dbe9f8">${esc(r.lens)}</td>
      <td>${esc(r.split)}</td>
    </tr>`).join('');
  $('#cmpTable').innerHTML = `<table><thead><tr><th>維度</th><th>共用大腦</th><th>單腦多視角</th><th>分層大腦</th></tr></thead><tbody>${rows}</tbody></table>`;

  const rec = d.recommendation || {};
  $('#recBody').innerHTML = `
    <span class="rec-tag">◆ 建議</span>
    <h4>${esc((d.topologies || []).find(t => t.key === rec.choice)?.label || rec.choice || '')}</h4>
    <ul class="rec-why">${(rec.why || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>
    <div class="rec-tradeoff">取捨：${esc(rec.trade_off || '')}</div>`;

  loadWatchlist();

  const btn = $('#btnCompare');
  if (!d.llm_enabled) {
    btn.disabled = true;
    $('#cmpHint').textContent = '需要 PQ_LLM_API_KEY；沒有 key 時規則層仍是完整可用的，只是沒有兩個 LLM 可對比。';
  } else {
    btn.disabled = false;
    $('#cmpHint').textContent = '會跑兩次，產生兩個 run；可在「運行記錄」看到對照。';
  }
}

async function loadWatchlist() {
  try {
    const d = await api('/api/v1/watchlist');
    renderWatchlist(d.terms || []);
  } catch (e) { /* 靜默 */ }
}

function renderWatchlist(terms) {
  const box = $('#wlChips');
  if (!box) return;
  box.innerHTML = terms.map(t => `<span class="wl-chip" data-term="${esc(t)}">${esc(t)}<i>×</i></span>`).join('')
    || '<span class="hint">（沒有關注詞）</span>';
  $$('#wlChips .wl-chip').forEach(chip => chip.onclick = async () => {
    const term = chip.dataset.term;
    const next = terms.filter(t => t !== term);
    await saveWatchlist(next);
  });
}

async function saveWatchlist(terms) {
  try {
    const d = await api('/api/v1/watchlist', { method: 'PUT', body: JSON.stringify({ terms }) });
    renderWatchlist(d.terms || []);
    $('#wlHint').textContent = '已儲存，下一次生成生效。';
  } catch (e) { toast(e.message, true); }
}

async function compareBrains() {
  const btn = $('#btnCompare');
  setBusy(btn, true, '跑兩次');
  try {
    const r = await api('/api/v1/brain/compare', {
      method: 'POST',
      body: JSON.stringify({ question: '生成今天的晨報', topologies: ['lens', 'split'] }),
    });
    $('#cmpOut').innerHTML = `<div class="diff-grid">${(r.results || []).map(x => `
      <div class="diff"><div class="d-k">${esc(x.label)}</div>
        <div class="d-v">${x.confidence.toFixed(2)}</div>
        <div class="hint">引用 ${x.citations}｜衝突 ${x.conflicts}｜${x.latency_ms}ms｜${x.tokens} tokens</div>
      </div>`).join('')}</div>`;
  } catch (e) {
    $('#cmpOut').innerHTML = `<div class="note-line bad">${esc(e.message)}</div>`;
  } finally {
    setBusy(btn, false);
  }
}

/* ---------------------------------------------------------------- 運行記錄 */

const H = { kind: 'all', q: '', compare: false, selected: [] };

async function loadRuns(silent) {
  try {
    const params = new URLSearchParams({ limit: '60' });
    if (H.kind !== 'all') params.set('kind', H.kind);
    if (H.q) params.set('q', H.q);
    const r = await api('/api/v1/runs?' + params.toString());
    S.runs = r.items || [];
    S.stats = r.stats || {};
    if (S.view === 'runs') renderHistory();
  } catch (e) {
    if (!silent) toast(e.message, true);
  }
}

function renderHistory() {
  const st = S.stats || {};
  $('#runStats').innerHTML = `
    <div class="stat"><div class="s-k">TOTAL</div><div class="s-v">${st.total ?? S.runs.length}</div><div class="s-s">累計輸出</div></div>
    <div class="stat"><div class="s-k">TODAY</div><div class="s-v">${st.today ?? '—'}</div><div class="s-s">今日</div></div>
    <div class="stat"><div class="s-k">AVG LATENCY</div><div class="s-v">${st.avg_latency_ms ?? 0}<span style="font-size:11px;color:var(--tx-4)"> ms</span></div><div class="s-s">平均</div></div>
    <div class="stat"><div class="s-k">GUARD BLOCKED</div><div class="s-v ${st.guard_blocked ? 'guard-block' : ''}">${st.guard_blocked ?? 0}</div><div class="s-s">被護欄攔截</div></div>`;

  const list = $('#historyList');
  if (!list) return;
  if (!S.runs.length) {
    list.innerHTML = `<div class="empty"><div class="empty-mark">▤</div><h3>沒有符合條件的記錄</h3><p>先回晨報頁生成一份，或換個篩選。</p></div>`;
    return;
  }
  const groups = {};
  S.runs.forEach(r => {
    const d = (r.created_at || '').slice(0, 10) || '未知日期';
    (groups[d] = groups[d] || []).push(r);
  });
  list.innerHTML = Object.keys(groups).sort().reverse().map(date => `
    <div class="tl-date"><span>${esc(date)}</span><em>${groups[date].length} 條</em></div>
    ${groups[date].map(historyCard).join('')}`).join('');

  list.querySelectorAll('[data-open]').forEach(b => b.onclick = (e) => {
    e.stopPropagation();
    const row = S.runs.find(r => r.run_id === b.dataset.open);
    if (row && row.kind === 'brief') loadStoredBrief(row.run_id);
    else showRun(b.dataset.open);
  });
  list.querySelectorAll('[data-pick]').forEach(b => b.onclick = (e) => {
    e.stopPropagation();
    togglePick(b.dataset.pick);
  });
}

function historyCard(r) {
  const kind = r.kind === 'probe' ? '探針' : r.kind === 'answer' ? '追問' : '晨報';
  const picked = H.selected.includes(r.run_id);
  const canPick = H.compare && r.kind === 'brief';
  const title = r.headline || r.question || '（無輸入）';
  return `<div class="tl-item ${picked ? 'is-pick' : ''}">
    <div class="tl-time">${esc((r.created_at || '').slice(11, 16))}</div>
    <div class="tl-card">
      <div class="tl-top">
        <span class="tag tag-flat">${kind}</span>
        <span class="tag ${r.mode === 'llm' ? 'tag-a1' : 'tag-a2'}">${r.mode === 'llm' ? 'LLM' : '規則'}</span>
        <span class="mono" style="font-size:10.5px;color:var(--tx-3)">置信 ${(r.confidence || 0).toFixed(2)}</span>
        <span class="hint">${esc(r.topology || '')}</span>
        <span class="hint" style="margin-left:auto">${r.latency_ms || 0}ms</span>
        ${canPick ? `<button class="btn btn-ghost btn-sm" data-pick="${esc(r.run_id)}">${picked ? '已選' : '選'}</button>` : ''}
        <button class="btn btn-ghost btn-sm" data-open="${esc(r.run_id)}">打開</button>
      </div>
      <div class="tl-title">${esc(title)}</div>
      ${r.headline && r.question ? `<div class="tl-sub">問：${esc(r.question)}</div>` : ''}
      <div class="tl-foot">${r.guard_passed ? '<span class="guard-pass">護欄通過</span>' : '<span class="guard-block">護欄攔截</span>'} · prompt ${esc(r.prompt_variant || '')}</div>
    </div>
  </div>`;
}

function togglePick(id) {
  if (H.selected.includes(id)) H.selected = H.selected.filter(x => x !== id);
  else {
    H.selected.push(id);
    if (H.selected.length > 2) H.selected.shift();
  }
  renderHistory();
  renderCompareBox();
}

function renderCompareBox() {
  const box = $('#compareBox');
  if (!box) return;
  if (!H.compare) { box.innerHTML = ''; return; }
  if (H.selected.length < 2) {
    box.innerHTML = `<div class="note-line info" style="margin-top:0">已選 ${H.selected.length} / 2 份：再選一份就能比較。</div>`;
    return;
  }
  box.innerHTML = `<div class="note-line info" style="margin-top:0">正在比較兩份…</div>`;
  compareTwo(H.selected[0], H.selected[1]);
}

async function compareTwo(a, b) {
  const box = $('#compareBox');
  try {
    const d = await api('/api/v1/commander/compare?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b));
    box.innerHTML = diffMarkup(d, true);
  } catch (e) {
    box.innerHTML = `<div class="note-line bad" style="margin-top:0">${esc(e.message)}</div>`;
  }
}

async function showRun(runId) {
  S.selectedRun = runId;
  $$('#runsTable tr').forEach(tr => tr.classList.toggle('is-sel', tr.dataset.run === runId));
  try {
    const row = await api('/api/v1/runs/' + encodeURIComponent(runId));
    const p = row.payload || {};
    const conf = p.confidence || {};
    $('#runDetail').innerHTML = `<div class="detail">
      <div class="detail-head">
        <span class="tag tag-flat">${esc(row.kind === 'probe' ? '探針' : row.kind === 'answer' ? '追問' : '晨報')}</span>
        <strong class="mono" style="font-size:12px">${esc(row.run_id)}</strong>
        <span class="hint">${esc((row.created_at || '').replace('T', ' '))} · ${esc(row.topology || '')} · prompt ${esc(row.prompt_variant || '')}</span>
        <button class="btn btn-ghost btn-sm" id="btnOpenRun" style="margin-left:auto">打開這份</button>
        <button class="btn btn-ghost btn-sm" id="btnReplay">重播並比對漂移</button>
      </div>
      <div class="hint" style="margin-bottom:10px">${esc(p.headline || (p.input || {}).title || '')}</div>
      <div class="diff-grid">
        <div class="diff"><div class="d-k">置信度</div><div class="d-v">${(conf.score || 0).toFixed(2)}</div></div>
        <div class="diff"><div class="d-k">模式</div><div class="d-v" style="font-size:12px">${esc((p.brain || {}).mode || '—')}</div></div>
        <div class="diff"><div class="d-k">證據</div><div class="d-v">${(p.evidence || []).length}</div></div>
        <div class="diff"><div class="d-k">分歧</div><div class="d-v">${(p.conflicts || []).length}</div></div>
      </div>
      <div id="replayOut" style="margin-top:12px"></div>
      <details class="raw-box" style="margin-top:12px">
        <summary>原始 payload（${esc(row.kind)}）</summary>
        <pre class="raw-pre">${esc(JSON.stringify(p, null, 2))}</pre>
      </details>
    </div>`;
    $('#btnReplay').onclick = () => replayRun(runId);
    const openBtn = $('#btnOpenRun');
    if (openBtn) openBtn.onclick = () => { if (row.kind === 'brief') loadStoredBrief(runId); else toast('只有晨報可以獨立打開'); };
  } catch (e) {
    toast(e.message, true);
  }
}

async function replayRun(runId) {
  if (!runId) return toast('這次 run 沒有 ID（可能未寫入）', true);
  const box = $('#replayOut') || (() => {
    const d = document.createElement('div');
    d.id = 'replayOut';
    d.style.marginTop = '12px';
    $('#briefOut').appendChild(d);
    return d;
  })();
  box.innerHTML = `<div class="note-line info">正在用同一份輸入重跑…</div>`;
  try {
    const r = await api('/api/v1/runs/' + encodeURIComponent(runId) + '/replay', { method: 'POST' });
    const d = r.drift || {};
    const cls = v => v > 0 ? 'pos' : v < 0 ? 'neg' : 'zero';
    box.innerHTML = `<div class="diff-grid">
        <div class="diff"><div class="d-k">置信度漂移</div><div class="d-v ${cls(d.confidence_delta)}">${d.confidence_delta > 0 ? '+' : ''}${d.confidence_delta}</div></div>
        <div class="diff"><div class="d-k">分歧數變化</div><div class="d-v ${cls(d.conflict_delta)}">${d.conflict_delta > 0 ? '+' : ''}${d.conflict_delta}</div></div>
        <div class="diff"><div class="d-k">證據數變化</div><div class="d-v ${cls(d.evidence_delta)}">${d.evidence_delta > 0 ? '+' : ''}${d.evidence_delta}</div></div>
        <div class="diff"><div class="d-k">耗時</div><div class="d-v">${r.elapsed_ms}ms</div></div>
      </div>
      <div class="hint" style="margin-top:8px">主線是否改變：${d.headline_changed ? '是（上游有新消息）' : '否'}；重播 run ${esc(r.replay_run_id || '')}</div>`;
    loadRuns(true);
  } catch (e) {
    box.innerHTML = `<div class="note-line bad">${esc(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- 整合狀態 */

async function loadIntegration(probe) {
  try {
    const d = await api('/api/v1/integration' + (probe ? '?probe=1' : ''));
    S.integration = d;

    const grid = $('#agentGrid');
    if (grid) {
      grid.innerHTML = (d.agents || []).map(a => {
        const label = a.status === 'ok' ? '已接入' : a.status === 'offline' ? '未連線' : '未實作';
        return `<div class="agent-card ${a.status}">
          <div class="agent-top">
            <span class="agent-id">${esc(a.id)}</span>
            <span class="agent-role">${esc(a.role)}</span>
            <span class="agent-status ${a.status}">${label}</span>
          </div>
          <div class="agent-name">${esc(a.name)}</div>
          <div class="agent-owner">負責：${esc(a.owner)}</div>
          <div class="agent-note">${esc(a.note)}</div>
        </div>`;
      }).join('');
    }

    const cl = $('#contractList');
    if (cl) {
      cl.innerHTML = (d.agents || []).filter(a => a.id !== 'A5').map(a => {
        const st = a.status === 'ok' ? '✓ 已接通' : a.status === 'offline' ? '等連線' : '等實作';
        return `<div class="contract-row ${a.status}">
          <div class="cr-id">${esc(a.id)}</div>
          <div class="cr-body">
            <div class="cr-name">${esc(a.name)} · ${esc(a.owner)}</div>
            <div class="cr-needs">${esc(a.needs)}</div>
          </div>
          <div class="cr-status">${st}</div>
        </div>`;
      }).join('');
    }

    const out = $('#probeOut');
    if (!out) return;
    if (probe && d.sample) {
      const a1 = d.sample.a1, a2 = d.sample.a2;
      out.innerHTML = `<div class="probe-grid-2">
          <div>
            <div class="probe-label">A1 ／api/v1/news/events</div>
            <pre class="raw-pre small">${esc(JSON.stringify({ meta: a1.meta, first: (a1.items || [])[0] || null }, null, 2))}</pre>
          </div>
          <div>
            <div class="probe-label">A2 ／api/v1/analyses</div>
            <pre class="raw-pre small">${esc(JSON.stringify({ meta: a2.meta, first: (a2.items || [])[0] || null }, null, 2))}</pre>
          </div>
        </div>
        <div class="note-line info" style="margin-top:10px">上面是上游的原始回應，不是 A5 整理過的。A5 只做適配，不改上游的資料。</div>`;
      const hint = $('#probeHint');
      if (hint) hint.textContent = '已重新檢測，' + new Date().toLocaleTimeString('zh-HK', { hour12: false });
    } else {
      out.innerHTML = '';
    }
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- 合規 */

async function loadCompliance() {
  try {
    const d = await api('/api/v1/compliance');
    const blocks = (d.guard_recent_blocks || []).map(r => `<tr>
        <td class="num">${esc((r.created_at || '').slice(5, 16))}</td>
        <td class="mono" style="font-size:10.5px">${esc(r.run_id)}</td>
        <td>${esc(r.question || '')}</td>
      </tr>`).join('');

    $('#complianceOut').innerHTML = `
      <div class="two-col" style="margin-top:0">
        <section class="sec">
          <div class="sec-head"><span class="sec-num">G</span><h3>A4 風控閘門</h3><span class="sec-sub">EXECUTION GATE</span></div>
          <div class="note-line bad" style="margin-top:0">A4 未接入 → gate = <b>${esc(d.a4_gate)}</b>：A5 只有建議權，任何輸出都不構成下單指令。</div>
          <ul class="bullets" style="margin-top:10px">
            <li>風控由規則與統計模型放行，大模型只負責解釋，不能否決或放行。</li>
            <li>A4 上線後，A5 必須先看到 gate 狀態才能敘述倉位含義。</li>
            <li>一鍵暫停與緊急平倉屬於 A4 / 執行層，不在 A5 職責內。</li>
          </ul>
        </section>
        <section class="sec">
          <div class="sec-head"><span class="sec-num">D</span><h3>免責聲明</h3><span class="sec-sub">DISCLAIMER</span></div>
          <p style="font-size:12.5px;color:var(--tx-2);line-height:1.8">${esc(d.disclaimer)}</p>
          <div class="note-line info" style="margin-top:10px">示例資料策略：${esc(d.sample_mode_policy)}</div>
        </section>
      </div>

      <section class="sec">
        <div class="sec-head"><span class="sec-num">1</span><h3>禁語清單</h3><span class="sec-sub">${(d.banned_phrases || []).length} TERMS</span></div>
        <p class="hint" style="margin-bottom:8px">命中任一句，該句會被整句替換為「已按合規規則刪除不當表述」，並記入運行記錄。</p>
        <div class="pill-row">${(d.banned_phrases || []).map(x => `<span class="ban-pill">${esc(x)}</span>`).join('')}</div>
      </section>

      <section class="sec">
        <div class="sec-head"><span class="sec-num">2</span><h3>資料限制</h3><span class="sec-sub">DATA CAVEATS</span></div>
        <ul class="bullets">${(d.data_caveats || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>
      </section>

      <section class="sec">
        <div class="sec-head"><span class="sec-num">3</span><h3>近期被攔截</h3><span class="sec-sub">RECENT BLOCKS</span></div>
        <div class="table-wrap"><table><thead><tr><th>時間</th><th>Run</th><th>輸入</th></tr></thead>
        <tbody>${blocks || '<tr><td colspan="3" class="hint">沒有被攔截的記錄（好事）。</td></tr>'}</tbody></table></div>
      </section>`;
  } catch (e) {
    $('#complianceOut').innerHTML = `<div class="hint">${esc(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- 初始化 */

function tickClock() {
  const d = new Date();
  $('#clock').textContent = d.toLocaleTimeString('zh-HK', { hour12: false });
}

async function init() {
  tickClock();
  setInterval(tickClock, 1000);

  $('#nav').addEventListener('click', e => {
    const b = e.target.closest('.nav-item');
    if (b) switchView(b.dataset.view);
  });

  $$('.chip').forEach(c => c.onclick = () => { $('#q').value = c.dataset.q; runBrief(c.dataset.q); });
  $('#btnRun').onclick = () => runBrief($('#q').value);
  $('#q').addEventListener('keydown', e => { if (e.key === 'Enter') runBrief($('#q').value); });

  $('#btnProbe').onclick = () => {
    const t = $('#probeText').value.trim();
    if (!t) return toast('先貼一段新聞正文', true);
    doProbe(t, $('#probeTitle').value, $('#probeSource').value, $('#probeUrl').value);
  };
  $('#btnProbeSample').onclick = () => {
    $('#probeTitle').value = PROBE_SAMPLE.title;
    $('#probeText').value = PROBE_SAMPLE.text;
    $('#probeSource').value = PROBE_SAMPLE.source;
    $('#probeUrl').value = PROBE_SAMPLE.url;
    doProbe(PROBE_SAMPLE.text, PROBE_SAMPLE.title, PROBE_SAMPLE.source, PROBE_SAMPLE.url);
  };
  $('#btnCompare').onclick = compareBrains;

  // 歷史記錄：篩選 / 搜尋 / 比較模式
  const hf = $('#histFilters');
  if (hf) hf.addEventListener('click', e => {
    const b = e.target.closest('[data-kind]');
    if (!b) return;
    H.kind = b.dataset.kind;
    $$('#histFilters .chip').forEach(c => c.classList.toggle('is-on', c === b));
    loadRuns();
  });
  let histTimer;
  const hs = $('#histSearch');
  if (hs) hs.addEventListener('input', e => {
    clearTimeout(histTimer);
    histTimer = setTimeout(() => { H.q = e.target.value.trim(); loadRuns(); }, 300);
  });
  const bc = $('#btnCompareMode');
  if (bc) bc.onclick = () => {
    H.compare = !H.compare;
    H.selected = [];
    bc.classList.toggle('btn-primary', H.compare);
    bc.textContent = H.compare ? '退出比較' : '比較兩份';
    renderHistory();
    renderCompareBox();
  };
  const bp = $('#btnIntegProbe');
  if (bp) bp.onclick = () => loadIntegration(true);
  const wlAdd = $('#btnWlAdd');
  if (wlAdd) {
    const addTerm = async () => {
      const input = $('#wlInput');
      const v = (input.value || '').trim();
      if (!v) return;
      input.value = '';
      const cur = $$('#wlChips .wl-chip').map(c => c.dataset.term);
      await saveWatchlist([...cur, v]);
    };
    wlAdd.onclick = addTerm;
    $('#wlInput').addEventListener('keydown', e => { if (e.key === 'Enter') addTerm(); });
    $('#btnWlReset').onclick = async () => {
      try {
        const d = await api('/api/v1/watchlist/reset', { method: 'POST' });
        renderWatchlist(d.terms || []);
        $('#wlHint').textContent = '已還原為預設值。';
      } catch (e) { toast(e.message, true); }
    };
  }

  $('#tgClose').onclick = () => { $('#tgModal').hidden = true; };
  $('#tgModal').addEventListener('click', e => { if (e.target.id === 'tgModal') $('#tgModal').hidden = true; });

  try {
    const [meta, health] = await Promise.all([api('/api/v1/meta'), api('/api/v1/upstream/health')]);
    S.meta = meta;
    S.health = health;
    S.topology = meta.brain;
    $('#railVersion').textContent = 'v' + meta.version;
    $('#railPrompt').textContent = 'prompt ' + meta.prompt_variant;
    $('#badgeBrain').textContent = (meta.brain || '').toUpperCase();
    $('#badgeGate').textContent = meta.a4_gate === 'advisory_only' ? 'A4 未接入 · 只有建議權' : 'A4 已接入';
    renderRail(health);
    if (!meta.llm_enabled) $('#badgeBrain').title = '未設定 API key，使用規則引擎';
  } catch (e) {
    toast('服務未就緒：' + e.message, true);
  }

  await loadRuns(true);
  await loadBrain();

  const hash = (location.hash || '#brief').replace('#', '');
  if (hash.startsWith('brief/')) {
    loadStoredBrief(hash.slice(6));
  } else if (VIEW_META[hash] && hash !== 'brief') {
    switchView(hash);
    if (hash === 'probe' && new URLSearchParams(location.search).get('autoprobe') === '1') {
      $('#probeTitle').value = PROBE_SAMPLE.title;
      $('#probeText').value = PROBE_SAMPLE.text;
      $('#probeSource').value = PROBE_SAMPLE.source;
      $('#probeUrl').value = PROBE_SAMPLE.url;
      doProbe(PROBE_SAMPLE.text, PROBE_SAMPLE.title, PROBE_SAMPLE.source, PROBE_SAMPLE.url);
    }
  } else {
    runBrief('生成今天的晨報');
  }
}

document.addEventListener('DOMContentLoaded', init);
