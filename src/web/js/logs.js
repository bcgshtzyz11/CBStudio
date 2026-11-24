/* ==========================================================================
   logs.js — 右栏：实时日志、事故列表、监看规则
   ========================================================================== */
'use strict';

const LOG_CAP = 1200;          // DOM 里最多保留多少行
const LOG_FLUSH_MS = 100;      // 每 100ms 批量渲染一次（不要每条事件都碰 DOM）
const LOG_FLUSH_MAX = 300;     // 单次最多追加多少行，避免一个长任务

let pendingLogs = [];
let flushTimer = 0;

/* ---------------------------------------------------------------- 日志 */
function pushLogBatch(events) {
  if (!events || !events.length) return;
  for (const ev of events) {
    S.logs.push(ev);
    if (ev.seq) S.lastSeq = Math.max(S.lastSeq, ev.seq);
    if (!S.logPaused) pendingLogs.push(ev);
  }
  const overflow = S.logs.length - LOG_CAP;
  if (overflow > 0) S.logs.splice(0, overflow);
  if (S.logPaused || !pendingLogs.length) return;
  scheduleLogFlush();
}

/** 把渲染合并到 100ms 一次：串口 200 行/秒时不能让每行都触发一次 DOM+layout。 */
function scheduleLogFlush() {
  if (flushTimer) return;
  flushTimer = setTimeout(flushLogs, LOG_FLUSH_MS);
}

function flushLogs() {
  flushTimer = 0;
  const batch = pendingLogs;
  pendingLogs = [];
  if (!batch.length) return;
  const view = $('#logView');
  if (!view) return;
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 60;
  const frag = document.createDocumentFragment();
  let added = 0;
  for (const ev of batch) {
    if (!matchesLogFilter(ev)) continue;
    frag.appendChild(buildLogLine(ev));
    if (++added >= LOG_FLUSH_MAX) break;
  }
  if (added) view.appendChild(frag);
  let excess = view.childElementCount - LOG_CAP;
  while (excess-- > 0) view.removeChild(view.firstChild);
  if (atBottom && added) view.scrollTop = view.scrollHeight;
  updateLogFoot();
}

function buildLogLine(ev) {
  return el('div', { class: 'log-line lv-' + (ev.level || 'info') }, [
    el('span', { class: 't', text: fmtClock(ev.t) }),
    el('span', { class: 'src', text: ev.source || '' }),
    el('span', { class: 'txt', text: ev.text || '' }),
  ]);
}

function matchesLogFilter(ev) {
  const level = ev.level || 'info';
  if (S.logLevel !== 'all') {
    if (S.logLevel === 'info' && !['info', 'debug', 'action'].includes(level)) return false;
    if (S.logLevel === 'warn' && level !== 'warn') return false;
    if (S.logLevel === 'error' && level !== 'error') return false;
    if (S.logLevel === 'incident' && level !== 'incident') return false;
  }
  const term = (S.logFilter || '').trim();
  if (!term) return true;
  const text = (ev.text || '') + ' ' + (ev.source || '') + ' ' + (ev.st || '');
  if (term.length > 1 && term.startsWith('/') && term.endsWith('/')) {
    try { return new RegExp(term.slice(1, -1), 'i').test(text); } catch (e) { return true; }
  }
  return text.toLowerCase().includes(term.toLowerCase());
}

function renderLogFull() {
  const view = $('#logView');
  if (!view) return;
  pendingLogs = [];
  const frag = document.createDocumentFragment();
  for (const ev of S.logs) {
    if (matchesLogFilter(ev)) frag.appendChild(buildLogLine(ev));
  }
  clear(view);
  view.appendChild(frag);
  view.scrollTop = view.scrollHeight;
  updateLogFoot();
}

function updateLogFoot() {
  const filtered = S.logs.filter(matchesLogFilter).length;
  const foot = $('#logFoot');
  if (foot) foot.textContent = filtered + ' / ' + S.logs.length + ' 条';
  const count = $('#tabLogCount');
  if (count) count.textContent = String(S.logs.length);
  const drop = $('#logDrop');
  const serial = (S.snapshot && S.snapshot.probe && S.snapshot.probe.serial) || {};
  if (drop) drop.textContent = '串口抽样丢弃 ' + (serial.dropped || 0) + ' 行';
}

function bindLogTools() {
  $$('#logLevels .chip').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('#logLevels .chip').forEach(node => node.classList.remove('active'));
      btn.classList.add('active');
      S.logLevel = btn.dataset.level;
      renderLogFull();
    });
  });
  let timer = 0;
  $('#logSearch').addEventListener('input', ev => {
    clearTimeout(timer);
    timer = setTimeout(() => { S.logFilter = ev.target.value; renderLogFull(); }, 150);
  });
  $('#btnLogPause').addEventListener('click', () => {
    S.logPaused = !S.logPaused;
    $('#btnLogPause').classList.toggle('active', S.logPaused);
    $('#btnLogPause').textContent = S.logPaused ? '▶' : '⏸';
    toast(S.logPaused ? '日志滚动已暂停' : '日志继续', '');
  });
  $('#btnLogClear').addEventListener('click', () => {
    S.logs = [];
    renderLogFull();
  });
  $('#btnLogExport').addEventListener('click', async () => {
    const res = await API.action('log.export', {});
    if (res && res.ok) toast('已导出 ' + res.count + ' 条到 ' + res.path, 'ok');
  });
}

/* ---------------------------------------------------------------- 事故 */
function renderIncidents(snap) {
  const list = $('#incList');
  if (!list) return;
  const items = (snap.incidents || []).slice().reverse();
  const count = $('#tabIncCount');
  if (count) count.textContent = String(items.length);
  const runId = $('#incRunId');
  if (runId) runId.textContent = snap.runner && snap.runner.run_id ? snap.runner.run_id : '';

  if (!items.length) {
    list.innerHTML = '<div class="empty">还没有事故记录<br><small>规则命中或点「保留现场」后会出现在这里</small></div>';
    return;
  }
  const same = list.dataset.hash === String(items.map(i => i.id + i.time).join('|'));
  const activeId = S.activeIncident;
  clear(list);
  for (const item of items) {
    const card = el('div', {
      class: 'inc-card ' + (item.severity || '') + (item.id === activeId ? ' active' : ''),
      dataset: { id: item.id },
      onclick: () => {
        S.activeIncident = S.activeIncident === item.id ? '' : item.id;
        renderIncidents(S.snapshot);
      },
    });
    card.appendChild(el('div', { class: 'inc-top' }, [
      el('span', { class: 'inc-title', text: item.reason || '事故' }),
      el('span', { class: 'inc-time', text: (item.time || '').slice(11) }),
    ]));
    const sub = [item.rule, item.state].filter(Boolean).join(' · ');
    if (sub) card.appendChild(el('div', { class: 'inc-sub', text: sub }));
    if (item.id === activeId) {
      const detail = el('div', { class: 'inc-detail' });
      if (item.detail) detail.appendChild(el('div', { class: 'inc-sub', text: item.detail }));
      const files = el('div', { class: 'file-chips' });
      for (const name of ['incident.json', 'serial_tail.log', 'events_tail.jsonl', 'can_rx.json']) {
        files.appendChild(el('span', { class: 'file-chip', text: name }));
      }
      if ((item.files && Object.keys(item.files).length)) {
        files.appendChild(el('span', { class: 'file-chip', text: 'adb/…' }));
      }
      detail.appendChild(files);
      detail.appendChild(el('button', {
        class: 'btn tiny',
        text: '📂 打开现场目录',
        onclick: ev => { ev.stopPropagation(); API.action('incident.open', { dir: item.dir }); },
      }));
      card.appendChild(detail);
    }
    list.appendChild(card);
  }
  list.dataset.hash = String(items.map(i => i.id + i.time).join('|'));
}

function bindIncidentTools() {
  const freeze = () => API.action('run.freeze', { reason: '手动保留现场' });
  const a = $('#btnFreeze2');
  if (a) a.addEventListener('click', freeze);
  const open = $('#btnOpenRun');
  if (open) open.addEventListener('click', () => API.action('run.open', {}));
}

/* ---------------------------------------------------------------- 规则 */
function renderRules(snap) {
  const list = $('#ruleList');
  if (!list) return;
  const rules = S.rules || [];
  clear(list);
  if (!rules.length) {
    list.innerHTML = '<div class="empty">还没有监看规则</div>';
    return;
  }
  for (const rule of rules) {
    const row = el('div', { class: 'rule-row' + (rule.enabled === false ? ' disabled' : '') });
    const main = el('div', { class: 'rule-main' }, [
      el('b', { text: rule.name || rule.id }),
      el('small', { text: RULE_LABEL[rule.type] || rule.type }),
    ]);
    const badges = el('div', { class: 'rule-badges' });
    badges.appendChild(el('span', { class: 'badge ' + (rule.severity === 'error' ? 'err' : 'warn'), text: rule.severity || 'warn' }));
    if (rule.freeze) badges.appendChild(el('span', { class: 'badge freeze', text: '保全' }));
    if (rule.stop) badges.appendChild(el('span', { class: 'badge stop', text: '停止' }));
    if (rule.type === 'regex' && rule.pattern) {
      badges.appendChild(el('span', { class: 'badge', text: rule.pattern.slice(0, 18) }));
    }
    main.appendChild(badges);
    const toggle = el('button', {
      class: 'switch' + (rule.enabled === false ? '' : ' on'),
      title: '启用 / 停用',
      style: 'flex:0 0 46px',
      onclick: ev => {
        ev.stopPropagation();
        rule.enabled = rule.enabled === false;
        renderRules(S.snapshot);
        saveRules();
      },
    }, [el('span')]);
    const edit = el('button', { class: 'icon-btn', text: '✎', title: '编辑', onclick: ev => { ev.stopPropagation(); openRuleEditor(rule); } });
    row.append(main, toggle, edit);
    row.addEventListener('click', () => openRuleEditor(rule));
    list.appendChild(row);
  }
}

function openRuleEditor(rule) {
  S.ruleEditingId = rule ? rule.id : '';
  $('#ruName').value = rule ? (rule.name || '') : '';
  $('#ruSeverity').value = rule ? (rule.severity || 'error') : 'error';
  $('#ruType').value = rule ? (rule.type || 'regex') : 'regex';
  $('#ruPattern').value = rule ? (rule.pattern || rule.can_id || '') : '';
  $('#ruWindow').value = rule ? (rule.window_ms || 0) : 0;
  $('#ruHits').value = rule ? (rule.min_hits || 1) : 1;
  $('#ruAfter').value = rule ? (rule.after_ms || 3000) : 3000;
  $('#ruBus').value = rule ? (rule.bus || '') : '';
  $('#ruCooldown').value = rule ? (rule.cooldown_ms || 5000) : 5000;
  $('#ruFreeze').checked = rule ? rule.freeze !== false : true;
  $('#ruStop').checked = rule ? rule.stop === true : false;
  $('#ruEnabled').checked = rule ? rule.enabled !== false : true;
  $('#ruleModal').classList.remove('hidden');
}

function applyRuleEditor() {
  const type = $('#ruType').value;
  const existing = (S.rules || []).find(item => item.id === S.ruleEditingId) || null;
  const rule = {
    id: S.ruleEditingId || ('r' + Math.random().toString(16).slice(2, 8)),
    name: $('#ruName').value.trim() || '新规则',
    // 编辑已有规则时保留它的探针归属（例如 SpiRx 规则属于 spi，按 SOC 上下文门控）
    probe: (existing && existing.probe)
      || (type === 'adb_offline' || type === 'log_stall' ? 'adb'
        : (type === 'can_timeout' ? 'can' : 'serial')),
    type,
    severity: $('#ruSeverity').value,
    window_ms: parseInt($('#ruWindow').value, 10) || 0,
    min_hits: parseInt($('#ruHits').value, 10) || 1,
    after_ms: parseInt($('#ruAfter').value, 10) || 3000,
    cooldown_ms: parseInt($('#ruCooldown').value, 10) || 5000,
    freeze: $('#ruFreeze').checked,
    stop: $('#ruStop').checked,
    enabled: $('#ruEnabled').checked,
  };
  const pattern = $('#ruPattern').value;
  if (type === 'can_timeout') {
    rule.can_id = pattern;
    rule.bus = $('#ruBus').value || 'ican';
  } else if (pattern) {
    rule.pattern = pattern;
  }
  const list = S.rules || [];
  const index = list.findIndex(item => item.id === rule.id);
  if (index >= 0) list[index] = rule; else list.push(rule);
  S.rules = list;
  $('#ruleModal').classList.add('hidden');
  renderRules(S.snapshot);
  saveRules();
}

async function saveRules() {
  const res = await API.action('rules.save', { rules: S.rules || [] });
  if (res && res.ok) toast('监看规则已保存', 'ok');
}

function bindRuleTools() {
  $('#btnRuleAdd').addEventListener('click', () => openRuleEditor(null));
  $('#btnRuleSave').addEventListener('click', saveRules);
  $('#btnRuleApply').addEventListener('click', applyRuleEditor);
}
