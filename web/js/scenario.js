/* ==========================================================================
   scenario.js — 中栏：剧本条、状态卡时间线、拖动排序、状态编辑器
   ========================================================================== */
'use strict';

let renderedScenarioHash = 0;
let draggingIndex = -1;
let dropIndex = -1;

/* ---------------------------------------------------------------- 剧本条 */
function renderScenarioBar() {
  const sc = S.scenario;
  if (!sc) return;
  const name = $('#scenarioName');
  if (name && document.activeElement !== name) name.value = sc.name || '';
  const loops = $('#scenarioLoops');
  if (loops && document.activeElement !== loops) loops.value = sc.loops;
  const interval = $('#scenarioInterval');
  if (interval && document.activeElement !== interval) interval.value = sc.loop_interval_ms;
  const mode = $('#scenarioCanMode');
  if (mode && document.activeElement !== mode) mode.value = (sc.can && sc.can.mode) || 'none';
  const mute = $('#scenarioMute');
  if (mute) mute.checked = sc.auto_mute_power_off !== false;

  const single = estimateScenario(sc);
  const loopCount = Number(sc.loops) || 0;
  const estimate = $('#scenarioEstimate');
  if (estimate) {
    estimate.textContent = '单轮 ' + fmtMs(single) + (loopCount ? ' · 总 ' + fmtMs(single * loopCount) : ' · 无限');
  }

  const files = $('#scenarioFiles');
  if (files) {
    const list = (S.snapshot && S.snapshot.scenario_files) || [];
    const current = files.value;
    clear(files);
    files.appendChild(el('option', { value: '', text: '已保存剧本…' }));
    for (const item of list) {
      files.appendChild(el('option', {
        value: item.file,
        text: item.name + ' · ' + item.states + ' 段' + (item.loops ? ' ×' + item.loops : ' ×∞'),
      }));
    }
    files.value = list.find(i => i.file === current) ? current : '';
  }

  const titleEl = $('#runScenario');
  if (titleEl && !S.snapshot) titleEl.textContent = sc.name || '未命名剧本';
}

function bindScenarioBar() {
  const name = $('#scenarioName');
  name.addEventListener('input', () => { if (S.scenario) { S.scenario.name = name.value; S.scenarioDirty = true; renderScenarioBar(); } });
  $('#scenarioLoops').addEventListener('change', ev => {
    if (!S.scenario) return;
    S.scenario.loops = Math.max(0, parseInt(ev.target.value, 10) || 0);
    S.scenarioDirty = true; renderScenarioBar(); markDirtyHint();
  });
  $('#scenarioInterval').addEventListener('change', ev => {
    if (!S.scenario) return;
    S.scenario.loop_interval_ms = Math.max(0, parseInt(ev.target.value, 10) || 0);
    S.scenarioDirty = true; renderScenarioBar();
  });
  $('#scenarioCanMode').addEventListener('change', ev => {
    if (!S.scenario) return;
    S.scenario.can = S.scenario.can || {};
    S.scenario.can.mode = ev.target.value;
    S.scenarioDirty = true;
  });
  $('#scenarioMute').addEventListener('change', ev => {
    if (!S.scenario) return;
    S.scenario.auto_mute_power_off = ev.target.checked;
    S.scenarioDirty = true;
  });

  $('#btnAddState').addEventListener('click', () => {
    if (!S.scenario) return;
    const index = S.selection >= 0 ? S.selection + 1 : S.scenario.states.length;
    const state = {
      id: 's' + Math.random().toString(16).slice(2, 8),
      name: '新状态 ' + (S.scenario.states.length + 1),
      channels: {}, hold_ms: 2000,
      wait: { type: 'none', timeout_ms: 5000, value: '', bus: '' },
      checks: [], actions: [], on_fail: 'continue', note: '',
    };
    S.scenario.states.splice(index, 0, state);
    S.selection = index;
    S.scenarioDirty = true;
    renderTimeline(true);
    openStateEditor(index);
  });

  $('#btnScenarioSave').addEventListener('click', saveScenario);
  $('#btnScenarioLoad').addEventListener('click', loadScenarioFile);
  $('#btnScenarioNew').addEventListener('click', newScenario);
}

function markDirtyHint() {
  const name = $('#scenarioName');
  if (name && S.scenario) {
    name.style.borderBottomColor = S.scenarioDirty ? 'var(--warn)' : 'transparent';
    name.title = S.scenarioDirty ? '有未保存的修改' : '';
  }
}

async function saveScenario() {
  if (!S.scenario) return;
  const check = validateScenarioLocal(S.scenario);
  if (check.errors.length) { toast(check.errors[0], 'error'); return; }
  const res = await API.action('scenario.save', { scenario: S.scenario });
  if (res && res.ok) {
    S.scenarioDirty = false;
    markDirtyHint();
    toast('剧本已保存', 'ok');
    if (res.scenario) S.scenario = res.scenario;
    renderScenarioBar();
    renderTimeline(true);
  }
}

async function loadScenarioFile() {
  const file = $('#scenarioFiles').value;
  if (!file) { toast('先选一个已保存的剧本', 'warn'); return; }
  const res = await API.action('scenario.load', { file });
  if (res && res.ok && res.scenario) {
    S.scenario = res.scenario;
    S.scenarioDirty = false;
    S.selection = -1;
    renderedScenarioHash = 0;
    renderScenarioBar();
    renderTimeline(true);
    toast('已载入「' + res.scenario.name + '」', 'ok');
  }
}

async function newScenario() {
  const res = await API.action('scenario.new', { name: '新剧本' });
  if (res && res.ok && res.scenario) {
    res.scenario.states = [{
      id: 's' + Math.random().toString(16).slice(2, 8),
      name: '整机上电', channels: { power: 'on', acc: 'on', ig: 'on' },
      hold_ms: 3000, wait: { type: 'none', timeout_ms: 5000, value: '', bus: '' },
      checks: [], actions: [], on_fail: 'continue', note: '',
    }];
    S.scenario = res.scenario;
    S.scenarioDirty = true;
    renderedScenarioHash = 0;
    renderScenarioBar();
    renderTimeline(true);
  }
}

/* ---------------------------------------------------------------- 时间线 */
function renderTimeline(force) {
  const wrap = $('#timeline');
  if (!wrap || !S.scenario) return;
  const hash = scenarioHash(S.scenario);
  if (!force && hash === renderedScenarioHash) { updateTimelineDynamic(); return; }
  renderedScenarioHash = hash;

  clear(wrap);
  const states = S.scenario.states || [];
  if (!states.length) {
    wrap.appendChild(el('div', { class: 'empty', text: '还没有状态卡：点下面「添加状态」开始搭剧本' }));
    return;
  }
  states.forEach((state, index) => wrap.appendChild(buildCard(state, index)));
  updateTimelineDynamic();
}

function buildCard(state, index) {
  const card = el('div', {
    class: 'state-card' + (S.selection === index ? ' selected' : ''),
    draggable: 'true',
    dataset: { index: String(index), id: state.id || '' },
  });

  card.appendChild(el('div', { class: 'drag-handle', title: '拖动调整顺序', text: '⠿' }));

  const body = el('div', { class: 'state-body' });
  body.appendChild(el('div', { class: 'state-head' }, [
    el('span', { class: 'state-index', text: String(index + 1) }),
    el('span', { class: 'state-name', text: state.name || '未命名状态' }),
    el('span', { class: 'state-hold', text: fmtMs(state.hold_ms) }),
  ]));

  const tags = el('div', { class: 'state-tags' });
  for (const [cid, value] of Object.entries(state.channels || {})) {
    if (value === 'keep') continue;
    const on = value === 'on';
    tags.appendChild(el('span', {
      class: 'tag ' + (on ? 'on' : 'off'),
      text: channelLabel(cid) + ' ' + (on ? '给电' : '断电'),
    }));
  }
  const wait = state.wait || {};
  if (wait.type && wait.type !== 'none') {
    let text = WAIT_LABEL[wait.type] || wait.type;
    if (wait.type === 'pwr_state') {
      text += ':' + pwrName(parseInt(wait.value, 10)) + '(' + wait.value + ')';
    } else if (wait.value) {
      text += ' ' + wait.value;
    }
    if (wait.timeout_ms) text += ' · ' + fmtMs(wait.timeout_ms);
    tags.appendChild(el('span', { class: 'tag wait', text }));
  }
  for (const chk of (state.checks || [])) {
    let text = CHECK_LABEL[chk.type] || chk.type;
    if (chk.type === 'serial_seen') text += (chk.expect === false ? ' 不出现 ' : ' ') + (chk.value || '');
    else if (chk.type === 'can_seen') text += ' ' + (chk.value || '') + ' ≥' + (chk.min || 1);
    else if (chk.type === 'adb_online' && chk.expect === false) text = 'SOC 离线';
    else if (chk.type === 'pwr_state') text += '=' + pwrName(parseInt(chk.expect, 10)) + '(' + chk.expect + ')';
    else if (chk.type === 'pwr_seen') text = '到过 ' + pwrName(parseInt(chk.expect, 10)) + '(' + chk.expect + ')';
    else if (chk.type === 'voltage') text = '电压 ' + (chk.min !== undefined ? chk.min : 18)
      + '~' + (chk.max !== undefined ? chk.max : 32) + 'V';
    else if (chk.type === 'screen') text = '画面已画出 ≥' + Math.round((chk.min_ratio || 0.15) * 100) + '%';
    else if (chk.type === 'screen_region') text = '电压区域≈基线（容差 ' + (chk.tol || 8) + '）';
    else if (chk.type === 'screen_text') {
      const ml = (typeof METRIC_LABEL !== 'undefined' && METRIC_LABEL[chk.metric || 'voltage'])
        || (chk.metric || 'voltage');
      text = ml + ' ≈ ' + ((chk.expect === undefined || chk.expect === 'auto') ? 'MCU' : chk.expect);
    }
    if (chk.grace_ms !== undefined && (chk.type === 'adb_online' || chk.type === 'pwr_state')) {
      text += '（宽限 ' + (chk.grace_ms / 1000) + 's）';
    }
    tags.appendChild(el('span', { class: 'tag check', text }));
  }
  for (const act of (state.actions || [])) {
    let text = ACTION_LABEL[act.type] || act.type;
    if (act.type === 'can_stim') {
      text += ':' + (STIM_LABEL[act.mode] || act.mode) + (act.period_ms ? ' ' + act.period_ms + 'ms' : '');
    } else if (act.type === 'capture') {
      text += '·' + (CAPTURE_WHEN_LABEL[act.when || 'entry'] || act.when)
        + (act.when === 'interval' ? ' ' + (act.every_ms || 2000) + 'ms' : '');
    } else if (act.value) {
      text += ' ' + act.value;
    }
    tags.appendChild(el('span', { class: 'tag act', text }));
  }
  if (tags.childElementCount) body.appendChild(tags);
  if (state.note) body.appendChild(el('div', { class: 'state-note', text: state.note }));

  const side = el('div', { class: 'state-side' });
  side.appendChild(el('div', { class: 'card-tools' }, [
    el('button', { class: 'icon-btn', title: '上移', text: '↑', onclick: ev => { ev.stopPropagation(); moveState(index, -1); } }),
    el('button', { class: 'icon-btn', title: '下移', text: '↓', onclick: ev => { ev.stopPropagation(); moveState(index, 1); } }),
    el('button', { class: 'icon-btn', title: '复制', text: '⧉', onclick: ev => { ev.stopPropagation(); duplicateState(index); } }),
    el('button', { class: 'icon-btn', title: '删除', text: '✕', onclick: ev => { ev.stopPropagation(); deleteState(index); } }),
  ]));
  side.appendChild(el('div', { class: 'state-result', text: '—' }));
  card.appendChild(body);
  card.appendChild(side);

  card.addEventListener('click', () => {
    S.selection = index;
    $$('.state-card').forEach(node => node.classList.remove('selected'));
    card.classList.add('selected');
    openStateEditor(index);
  });
  card.addEventListener('dragstart', ev => onDragStart(ev, index, card));
  card.addEventListener('dragend', () => onDragEnd(card));
  return card;
}

function updateTimelineDynamic() {
  const run = (S.snapshot && S.snapshot.runner) || {};
  const results = run.results || {};
  const active = (run.phase === 'running' || run.phase === 'paused' || run.phase === 'stopping')
    ? run.state_index : -1;
  $$('#timeline .state-card').forEach(node => {
    const index = Number(node.dataset.index);
    node.classList.toggle('active', index === active);
    const state = (S.scenario && S.scenario.states || [])[index];
    const result = state ? results[state.id] : null;
    const resultEl = node.querySelector('.state-result');
    if (resultEl) {
      if (!result) {
        resultEl.textContent = '—';
        resultEl.className = 'state-result';
      } else {
        const bad = result.fail > 0;
        resultEl.textContent = '✓' + (result.pass || 0) + '  ✕' + (result.fail || 0);
        resultEl.className = 'state-result ' + (bad ? 'bad' : 'ok');
        node.classList.toggle('failed', bad && result.last !== 'ok');
      }
    }
  });
}

/* ---------------------------------------------------------------- 拖动排序 */
function onDragStart(ev, index, card) {
  // 只有从拖动手柄按下才允许拖，避免点卡片/按钮误触
  if (!ev.target.closest || !ev.target.closest('.drag-handle')) {
    ev.preventDefault();
    return;
  }
  draggingIndex = index;
  dropIndex = index;
  card.classList.add('dragging');
  ev.dataTransfer.effectAllowed = 'move';
  try { ev.dataTransfer.setData('text/plain', String(index)); } catch (e) { /* 某些浏览器限制 */ }
}

function onDragEnd(card) {
  card.classList.remove('dragging');
  const line = $('#dropLine');
  if (line) line.remove();
  $$('#timeline .state-card').forEach(node => node.classList.remove('drop-before', 'drop-after'));
  draggingIndex = -1;
  dropIndex = -1;
}

function bindTimelineDrag() {
  const wrap = $('#timeline');
  wrap.addEventListener('dragover', ev => {
    if (draggingIndex < 0) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    const cards = $$('#timeline .state-card');
    if (!cards.length) return;
    let index = cards.length;
    for (let i = 0; i < cards.length; i++) {
      const rect = cards[i].getBoundingClientRect();
      if (ev.clientY < rect.top + rect.height / 2) { index = i; break; }
    }
    dropIndex = index;
    let line = $('#dropLine');
    if (!line) {
      line = el('div', { class: 'drop-line', id: 'dropLine' });
    }
    const reference = cards[index] || null;
    if (reference) wrap.insertBefore(line, reference);
    else wrap.appendChild(line);
  });
  wrap.addEventListener('drop', ev => {
    if (draggingIndex < 0) return;
    ev.preventDefault();
    reorder(draggingIndex, dropIndex);
  });
  wrap.addEventListener('dragleave', ev => {
    if (ev.target !== wrap) return;
    const line = $('#dropLine');
    if (line) line.remove();
  });
}

function reorder(from, to) {
  if (!S.scenario || from < 0) return;
  const states = S.scenario.states;
  if (to < 0) to = 0;
  if (to > states.length) to = states.length;
  const line = $('#dropLine');
  if (line) line.remove();
  const [item] = states.splice(from, 1);
  let target = to;
  if (from < to) target -= 1;
  states.splice(target, 0, item);
  S.selection = target;
  S.scenarioDirty = true;
  renderedScenarioHash = 0;
  renderTimeline(true);
}

function moveState(index, delta) {
  if (!S.scenario) return;
  const target = index + delta;
  if (target < 0 || target >= S.scenario.states.length) return;
  const states = S.scenario.states;
  const [item] = states.splice(index, 1);
  states.splice(target, 0, item);
  S.selection = target;
  S.scenarioDirty = true;
  renderedScenarioHash = 0;
  renderTimeline(true);
}

function duplicateState(index) {
  if (!S.scenario) return;
  const copy = clone(S.scenario.states[index]);
  copy.id = 's' + Math.random().toString(16).slice(2, 8);
  copy.name = (copy.name || '状态') + ' 副本';
  S.scenario.states.splice(index + 1, 0, copy);
  S.selection = index + 1;
  S.scenarioDirty = true;
  renderedScenarioHash = 0;
  renderTimeline(true);
}

function deleteState(index) {
  if (!S.scenario) return;
  const state = S.scenario.states[index];
  if (!state) return;
  if (!confirm('删除状态「' + (state.name || index + 1) + '」？')) return;
  S.scenario.states.splice(index, 1);
  if (S.selection >= S.scenario.states.length) S.selection = S.scenario.states.length - 1;
  S.scenarioDirty = true;
  renderedScenarioHash = 0;
  renderTimeline(true);
}

/* ---------------------------------------------------------------- 状态编辑器 */
function openStateEditor(index) {
  if (!S.scenario) return;
  const state = S.scenario.states[index];
  if (!state) return;
  S.stateEditingIndex = index;
  $('#stateModalTitle').textContent = '编辑状态 · 第 ' + (index + 1) + ' 张';
  $('#stName').value = state.name || '';
  $('#stHold').value = state.hold_ms || 0;
  $('#stOnFail').value = state.on_fail || 'continue';
  $('#stNote').value = state.note || '';

  const wait = state.wait || {};
  $('#stWaitType').value = wait.type || 'none';
  $('#stWaitValue').value = wait.value || '';
  $('#stWaitBus').value = wait.bus || '';
  $('#stWaitTimeout').value = wait.timeout_ms || 5000;

  buildChannelMatrix(state);
  buildCheckRows(state);
  buildActionRows(state);
  updateWaitVisibility();

  $('#stateModal').classList.remove('hidden');
}

function buildChannelMatrix(state) {
  const wrap = $('#stChannels');
  clear(wrap);
  const channels = (S.snapshot && S.snapshot.channels) || [];
  for (const ch of channels) {
    const value = (state.channels || {})[ch.id] || 'keep';
    const seg = el('div', { class: 'seg' });
    for (const [key, label] of [['on', '给电'], ['off', '断电'], ['keep', '不动']]) {
      const btn = el('button', {
        class: key + (value === key ? ' active' : ''),
        text: label,
        dataset: { channel: ch.id, value: key },
        onclick: () => {
          $$('.seg button', seg).forEach(node => node.classList.remove('active'));
          btn.classList.add('active');
        },
      });
      seg.appendChild(btn);
    }
    wrap.appendChild(el('div', { class: 'chan-cell' }, [
      el('b', { text: ch.label }),
      seg,
    ]));
  }
  if (!channels.length) wrap.appendChild(el('div', { class: 'empty', text: '没有可用通道' }));
}

function currentChannelSelection() {
  const result = {};
  $$('#stChannels .chan-cell').forEach(cell => {
    const active = cell.querySelector('.seg button.active');
    if (!active) return;
    const cid = active.dataset.channel;
    if (active.dataset.value !== 'keep') result[cid] = active.dataset.value;
  });
  return result;
}

function buildCheckRows(state) {
  const wrap = $('#stChecks');
  clear(wrap);
  (state.checks || []).forEach(chk => wrap.appendChild(buildCheckRow(chk)));
}

function buildCheckRow(chk) {
  const row = el('div', { class: 'check-row' });
  const type = el('select', {}, [
    el('option', { value: 'no_error', text: '无 ERROR 日志' }),
    el('option', { value: 'serial_seen', text: '串口匹配' }),
    el('option', { value: 'can_seen', text: 'CAN 收到' }),
    el('option', { value: 'adb_online', text: 'SOC 在线' }),
    el('option', { value: 'pwr_state', text: '电源状态 =' }),
    el('option', { value: 'pwr_seen', text: '到过电源状态' }),
    el('option', { value: 'voltage', text: '蓄电池电压（V）' }),
    el('option', { value: 'screen', text: '仪表画面已画出' }),
    el('option', { value: 'screen_region', text: '电压区域与基线一致' }),
    el('option', { value: 'screen_text', text: '仪表显示数字（OCR）' }),
  ]);
  type.value = chk.type || 'no_error';
  const value = el('input', { class: 'grow', placeholder: '正则 / CAN ID', value: chk.value || '' });
  const expect = el('select', {}, [
    el('option', { value: 'true', text: '应出现' }),
    el('option', { value: 'false', text: '不应出现' }),
  ]);
  expect.value = String(chk.expect === false ? 'false' : 'true');
  const bus = el('input', { placeholder: '总线', value: chk.bus || '', style: 'width:76px' });
  // 每种断言类型有自己的默认阈值：帧数=1、电压下限=18、画面占比/区域容差=0.15
  const defaultMin = (t) => (t === 'screen' || t === 'screen_region') ? 0.15
    : (t === 'voltage' ? 18 : 1);
  const initialMin = chk.min_ratio !== undefined ? chk.min_ratio
    : (chk.tol !== undefined ? chk.tol
      : (chk.min !== undefined ? chk.min : defaultMin(chk.type || 'no_error')));
  const min = el('input', { type: 'number', min: '0', step: '0.01', value: initialMin, style: 'width:70px' });
  const max = el('input', { type: 'number', min: '0', step: '0.01', value: chk.max !== undefined ? chk.max : 32, style: 'width:70px' });
  const pwrSel = el('select', {}, [0, 1, 2, 3, 4].map(n =>
    el('option', { value: String(n), text: n + ' ' + pwrName(n) })));
  const metricSel = el('select', {}, ['voltage', 'odo', 'trip', 'soc'].map(k =>
    el('option', { value: k, text: (typeof METRIC_LABEL !== 'undefined' && METRIC_LABEL[k]) || k })));
  metricSel.value = chk.metric || 'voltage';
  const grace = el('input', {
    type: 'number', min: '0', step: '500',
    value: chk.grace_ms !== undefined ? chk.grace_ms : '',
    placeholder: '宽限', style: 'width:78px',
    title: '宽限期(ms)：条件在宽限内成立即算通过；留空用默认（SOC 15s / 电源 5s）',
  });
  pwrSel.value = String(chk.expect !== undefined && chk.expect !== null ? chk.expect : 0);
  const remove = el('button', { class: 'icon-btn', text: '✕', onclick: () => row.remove() });

  const sync = () => {
    const t = type.value;
    const pwr = t === 'pwr_state' || t === 'pwr_seen';
    value.style.display = (t === 'serial_seen' || t === 'can_seen') ? '' : 'none';
    expect.style.display = (t === 'serial_seen' || t === 'adb_online') ? '' : 'none';
    bus.style.display = (t === 'can_seen') ? '' : 'none';
    min.style.display = (t === 'can_seen') ? '' : ((t === 'voltage' || t === 'screen' || t === 'screen_region' || t === 'screen_text') ? '' : 'none');
    max.style.display = (t === 'voltage') ? '' : 'none';
    pwrSel.style.display = pwr ? '' : 'none';
    grace.style.display = (t === 'adb_online' || t === 'pwr_state') ? '' : 'none';
    metricSel.style.display = (t === 'screen_text') ? '' : 'none';
    min.placeholder = t === 'voltage' ? '下限V'
      : (t === 'screen' ? '非黑占比' : (t === 'screen_region' ? '容差' : (t === 'screen_text' ? '期望V(空=MCU)' : '帧数')));
  };
  type.addEventListener('change', () => { min.value = defaultMin(type.value); sync(); });
  row.append(type, value, expect, bus, min, max, pwrSel, metricSel, grace, remove);
  row._read = () => {
    const t = type.value;
    if (t === 'pwr_state' || t === 'pwr_seen') {
      return { type: t, expect: parseInt(pwrSel.value, 10) || 0 };
    }
    if (t === 'voltage') {
      return { type: 'voltage', min: parseFloat(min.value) || 18, max: parseFloat(max.value) || 32 };
    }
    if (t === 'screen') {
      return { type: 'screen', min_ratio: parseFloat(min.value) || 0.15 };
    }
    if (t === 'screen_region') {
      return { type: 'screen_region', tol: parseFloat(min.value) || 0.15 };
    }
    if (t === 'screen_text') {
      const raw = String(min.value).trim();
      const num = parseFloat(raw);
      return {
        type: 'screen_text',
        metric: metricSel.value,
        expect: (raw === '' || isNaN(num)) ? 'auto' : num,
      };
    }
    const item = { type: t };
    if (t === 'serial_seen' || t === 'can_seen') item.value = value.value;
    if (t === 'serial_seen' || t === 'adb_online') item.expect = expect.value === 'true';
    if (t === 'can_seen') { item.bus = bus.value; item.min = parseInt(min.value, 10) || 1; }
    if ((t === 'adb_online' || t === 'pwr_state') && grace.value !== '') {
      item.grace_ms = parseInt(grace.value, 10) || 0;
    }
    return item;
  };
  sync();
  return row;
}

function buildActionRows(state) {
  const wrap = $('#stActions');
  clear(wrap);
  (state.actions || []).forEach(act => wrap.appendChild(buildActionRow(act)));
}

function buildActionRow(act) {
  const row = el('div', { class: 'action-row' });
  const type = el('select', {}, [
    el('option', { value: 'can_tx_start', text: '启动 CAN 发送' }),
    el('option', { value: 'can_tx_stop', text: '停止 CAN 发送' }),
    el('option', { value: 'can_stim', text: 'CAN 激励（电源状态）' }),
    el('option', { value: 'mark', text: '打标记' }),
    el('option', { value: 'capture', text: '抓屏（可选触发时机）' }),
  ]);
  type.value = act.type || 'can_tx_start';

  const value = el('input', { class: 'grow', placeholder: '标记文本', value: act.value || '' });
  const mode = el('select', {}, [
    el('option', { value: 'any', text: '任意帧' }),
    el('option', { value: 'wake', text: '唤醒帧' }),
    el('option', { value: 'frame', text: '指定帧' }),
    el('option', { value: 'silence', text: '静默' }),
  ]);
  mode.value = act.mode || 'any';
  const bus = el('select', {}, [
    el('option', { value: '', text: '双总线' }),
    el('option', { value: 'ican', text: 'ICAN' }),
    el('option', { value: 'pcan', text: 'PCAN' }),
  ]);
  bus.value = act.bus || '';
  const period = el('input', { type: 'number', min: '1', step: '10', value: act.period_ms || 100, style: 'width:70px' });
  const frameId = el('input', { placeholder: '0x18FE4121', value: act.id || '', style: 'width:100px' });
  const frameData = el('input', { placeholder: '00 11 22 …', value: act.data || '', class: 'grow' });
  const capWhen = el('select', {}, [
    el('option', { value: 'entry', text: '进入状态时' }),
    el('option', { value: 'on_pass', text: '断言通过时' }),
    el('option', { value: 'on_fail', text: '断言失败时' }),
    el('option', { value: 'on_timeout', text: '等待超时时' }),
    el('option', { value: 'interval', text: '定时周期' }),
  ]);
  capWhen.value = act.when || 'entry';
  const capEvery = el('input', {
    type: 'number', min: '500', step: '500', value: act.every_ms || 2000,
    style: 'width:80px', title: '定时抓屏间隔(ms)',
  });
  const capLabel = el('input', { placeholder: '抓屏名称（可空）', value: act.label || '', class: 'grow' });
  const remove = el('button', { class: 'icon-btn', text: '✕', onclick: () => row.remove() });

  const sync = () => {
    const stim = type.value === 'can_stim';
    const cap = type.value === 'capture';
    const frameMode = stim && mode.value === 'frame';
    const silent = stim && mode.value === 'silence';
    value.style.display = type.value === 'mark' ? '' : 'none';
    mode.style.display = stim ? '' : 'none';
    bus.style.display = stim && !silent ? '' : 'none';
    period.style.display = stim && !silent ? '' : 'none';
    frameId.style.display = frameMode ? '' : 'none';
    frameData.style.display = frameMode ? '' : 'none';
    capWhen.style.display = cap ? '' : 'none';
    capEvery.style.display = cap && capWhen.value === 'interval' ? '' : 'none';
    capLabel.style.display = cap ? '' : 'none';
  };
  type.addEventListener('change', sync);
  mode.addEventListener('change', sync);
  capWhen.addEventListener('change', sync);

  row.append(type, value, mode, bus, period, frameId, frameData, capWhen, capEvery, capLabel, remove);
  row._read = () => {
    if (type.value === 'capture') {
      const item = { type: 'capture', when: capWhen.value };
      if (capWhen.value === 'interval') item.every_ms = parseInt(capEvery.value, 10) || 2000;
      if (capLabel.value.trim()) item.label = capLabel.value.trim();
      return item;
    }
    if (type.value === 'can_stim') {
      const item = { type: 'can_stim', mode: mode.value };
      if (mode.value === 'silence') return item;
      item.period_ms = parseInt(period.value, 10) || 100;
      if (bus.value) item.bus = bus.value;
      if (mode.value === 'frame') {
        item.id = frameId.value.trim();
        item.data = frameData.value.trim();
      }
      return item;
    }
    const item = { type: type.value };
    if (type.value === 'mark' && value.value) item.value = value.value;
    return item;
  };
  sync();
  return row;
}

function updateWaitVisibility() {
  const type = $('#stWaitType').value;
  const needValue = type === 'serial_match' || type === 'can_frame' || type === 'pwr_state';
  const valueInput = $('#stWaitValue');
  valueInput.style.display = needValue ? '' : 'none';
  valueInput.placeholder = type === 'pwr_state' ? '0..4（0关机 1待机 2监听 3预开机 4开机）'
    : (type === 'serial_match' ? '正则' : 'CAN ID');
  $('#stWaitBusField').style.display = type === 'can_frame' ? '' : 'none';
  $('#stWaitTimeoutField').style.display = type === 'none' ? 'none' : '';
}

async function applyStateEditor() {
  const index = S.stateEditingIndex;
  if (!S.scenario || index < 0) return;
  const state = S.scenario.states[index];
  if (!state) return;
  state.name = $('#stName').value.trim() || state.name;
  state.hold_ms = Math.max(0, parseInt($('#stHold').value, 10) || 0);
  state.on_fail = $('#stOnFail').value;
  state.note = $('#stNote').value;
  state.channels = currentChannelSelection();

  const waitType = $('#stWaitType').value;
  state.wait = { type: waitType };
  if (waitType !== 'none') {
    state.wait.timeout_ms = Math.max(0, parseInt($('#stWaitTimeout').value, 10) || 5000);
    if (waitType === 'serial_match' || waitType === 'can_frame') state.wait.value = $('#stWaitValue').value;
    if (waitType === 'pwr_state') state.wait.value = parseInt($('#stWaitValue').value, 10) || 0;
    if (waitType === 'can_frame') state.wait.bus = $('#stWaitBus').value || 'ican';
  }

  state.checks = $$('#stChecks .check-row').map(row => row._read()).filter(Boolean);
  state.actions = $$('#stActions .action-row').map(row => row._read()).filter(Boolean);

  S.scenarioDirty = true;
  renderedScenarioHash = 0;
  $('#stateModal').classList.add('hidden');
  renderTimeline(true);

  // 「应用」即保存到后端：避免"界面改了、实际还是旧值"
  const running = !!(S.snapshot && S.snapshot.runner
    && ['running', 'paused', 'stopping'].includes(S.snapshot.runner.phase));
  await saveScenario();
  if (running) {
    toast('已保存；正在运行的这轮仍用启动时的剧本，改动从下次运行生效', 'info');
  }
}

function bindStateEditor() {
  $('#stWaitType').addEventListener('change', updateWaitVisibility);
  $('#btnAddCheck').addEventListener('click', () => {
    $('#stChecks').appendChild(buildCheckRow({ type: 'no_error' }));
  });
  $('#btnAddAction').addEventListener('click', () => {
    $('#stActions').appendChild(buildActionRow({ type: 'can_tx_start' }));
  });
  $('#btnStateApply').addEventListener('click', applyStateEditor);
}
