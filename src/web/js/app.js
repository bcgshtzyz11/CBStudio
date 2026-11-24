/* ==========================================================================
   app.js — 启动、SSE 连接、快照落位、顶栏按钮、设置、快捷键
   ========================================================================== */
'use strict';

let clockBase = { elapsed: 0, at: 0, running: false };

/* ---------------------------------------------------------------- 启动 */
document.addEventListener('DOMContentLoaded', async () => {
  bindScenarioBar();
  bindTimelineDrag();
  bindStateEditor();
  bindLogTools();
  bindIncidentTools();
  bindRuleTools();
  bindTopButtons();
  bindSettings();
  bindModals();
  bindRegionPicker();
  bindCanModal();
  bindProbeToggles();
  bindKeys();
  bindTabs();

  try {
    const snap = await API.snapshot();
    applySnapshot(snap, true);
    await backfillLogs();
    connectSSE();
  } catch (err) {
    toast('连不上工作台服务：' + err.message, 'error');
    setConn(false, '连接失败');
  }
  setInterval(tickClock, 100);
  setInterval(updateCountersFromLogs, 1000);
});

/* ---------------------------------------------------------------- SSE */
async function backfillLogs() {
  // 打开页面时补一段历史日志，避免"运行到一半才打开页面"看到空日志
  try {
    const resp = await fetch('/api/events?limit=400', { cache: 'no-store' });
    const data = await resp.json();
    if (data.ok && data.events) {
      pushLogBatch(data.events);
      S.lastSeq = data.seq || S.lastSeq;
    }
  } catch (err) { /* 忽略 */ }
}

function connectSSE() {
  if (!window.EventSource) { pollFallback(); return; }
  const source = new EventSource('/api/stream');
  source.addEventListener('open', () => setConn(true, '已连接'));
  source.addEventListener('snapshot', ev => {
    try { applySnapshot(JSON.parse(ev.data), false); } catch (err) { /* 忽略坏帧 */ }
  });
  source.addEventListener('log', ev => {
    try {
      const data = JSON.parse(ev.data);
      pushLogBatch(data.events || []);
    } catch (err) { /* 忽略 */ }
  });
  source.addEventListener('error', () => {
    setConn(false, '重连中…');
    // EventSource 会自动重连；超过 8 秒仍未恢复则退回轮询
    setTimeout(() => { if (!S.connected) pollFallback(); }, 8000);
  });
}

let polling = false;
function pollFallback() {
  if (polling) return;
  polling = true;
  setInterval(async () => {
    try {
      const snap = await API.snapshot();
      applySnapshot(snap, false);
      setConn(true, '已连接（轮询）');
    } catch (err) {
      setConn(false, '离线');
    }
  }, 2000);
}

function setConn(ok, text) {
  S.connected = ok;
  const dot = $('#connDot');
  if (dot) dot.className = 'dot ' + (ok ? 'ok' : 'err');
  const label = $('#connText');
  if (label) label.textContent = text;
}

/* ---------------------------------------------------------------- 快照 */
function applySnapshot(snap, initial) {
  if (!snap) return;
  S.snapshot = snap;
  S.connected = true;

  if (!S.scenario) {
    S.scenario = snap.scenario ? clone(snap.scenario) : null;
    S.scenarioDirty = false;
    renderScenarioBar();
    renderTimeline(true);
  } else if (initial) {
    renderScenarioBar();
    renderTimeline(true);
  }

  if (!S.rulesInit) {
    S.rules = clone((snap.probe && snap.probe.rules) || []);
    S.rulesInit = true;
  }

  applyHeader(snap);
  renderChannels(snap);
  renderProbes(snap);
  renderCanIds(snap);
  renderIncidents(snap);
  if (typeof renderCanModal === "function") renderCanModal(snap);
  renderRules(snap);
  applyCounters(snap);
  updateTimelineDynamic();
  updateLogFoot();

  const version = $('#statVersion');
  if (version) version.textContent = 'CBStudio v' + (snap.version || '') + ' · ' + (snap.host || '') + ':' + (snap.port || '');
  const simBadge = $('#simBadge');
  if (simBadge) simBadge.classList.toggle('hidden', !snap.simulate);
  const runDir = $('#statRunDir');
  if (runDir) {
    const dir = (snap.runner && snap.runner.run_dir) || snap.run_dir || '';
    runDir.textContent = dir || '（尚无运行目录）';
    runDir.title = dir || '';
  }
  const hint = $('#screenHint');
  if (hint) {
    const last = (snap.screens || [])[0];
    const base = snap.screen_baseline;
    hint.textContent = (last
      ? (last.time + ' · 非黑 ' + Math.round(((last.stats || {}).bright_ratio || 0) * 100) + '%')
      : '') + (base ? (' · 基线 ' + (base.count || 0) + ' 条（' + String(base.last_time || '').slice(11) + '）')
                    : ' · 未学基线');
  }
  // 设置面板只在打开时填一次：以前这里每来一次快照（约 1 秒）就 fillSettings()，
  // 用户正在输入端口，下一秒就被服务端旧值覆盖，表现为"串口改不了"。
}

function applyHeader(snap) {
  const run = snap.runner || {};
  const phase = run.phase || 'idle';
  const pill = $('#phasePill');
  if (pill) {
    pill.textContent = phaseText(phase);
    pill.className = 'pill phase-' + phase;
  }
  const scenario = $('#runScenario');
  if (scenario) scenario.textContent = run.scenario || (S.scenario && S.scenario.name) || '未载入剧本';

  const iter = $('#runIteration');
  if (iter) {
    const total = run.loops ? '/' + run.loops : '/∞';
    iter.textContent = '第 ' + (run.iteration || 0) + ' 轮' + (run.iteration ? total : '');
  }
  const stateName = $('#runStateName');
  if (stateName) {
    stateName.textContent = run.state_name
      ? (run.state_index + 1) + '. ' + run.state_name
      : (run.error && phase === 'error' ? run.error : '—');
  }
  clockBase = {
    elapsed: run.elapsed_ms || 0,
    at: performance.now(),
    running: phase === 'running' || phase === 'paused' || phase === 'stopping',
  };
  tickClock();

  const btnPause = $('#btnPause');
  if (btnPause) {
    const paused = phase === 'paused';
    $('#pauseIco').textContent = paused ? '▶' : '⏸';
    $('#pauseLabel').textContent = paused ? '恢复' : '暂停';
    btnPause.disabled = !['running', 'paused'].includes(phase);
  }
  const btnStart = $('#btnStart');
  if (btnStart) btnStart.disabled = ['running', 'paused', 'stopping'].includes(phase);
  const btnStop = $('#btnStop');
  if (btnStop) btnStop.disabled = !['running', 'paused', 'stopping'].includes(phase);
  markDirtyHint();
}

function tickClock() {
  const node = $('#runClock');
  if (!node) return;
  const extra = clockBase.running ? (performance.now() - clockBase.at) : 0;
  node.textContent = fmtMs(clockBase.elapsed + extra);
}

function applyCounters(snap) {
  const counters = snap.counters || {};
  const node = $('#statCounters');
  if (node) node.textContent = '错误 ' + (counters.error || 0) + ' · 告警 ' + (counters.warn || 0)
    + ' · 事故 ' + (counters.incident || 0);
}

function updateCountersFromLogs() {
  const drop = $('#logDrop');
  if (!drop || !S.snapshot) return;
  const serial = (S.snapshot.probe && S.snapshot.probe.serial) || {};
  drop.textContent = '串口抽样丢弃 ' + (serial.dropped || 0) + ' 行';
}

/* ---------------------------------------------------------------- 顶栏按钮 */
function bindTopButtons() {
  $('#btnStart').addEventListener('click', async () => {
    if (!S.scenario) { toast('先载入或新建剧本', 'warn'); return; }
    const check = validateScenarioLocal(S.scenario);
    if (check.errors.length) { toast(check.errors[0], 'error'); return; }
    if (S.scenarioDirty) await saveScenario();
    const res = await API.action('run.start', { scenario: S.scenario });
    if (res && res.ok) toast(res.message || '已开始', 'ok');
  });

  $('#btnPause').addEventListener('click', async () => {
    const phase = S.snapshot && S.snapshot.runner && S.snapshot.runner.phase;
    const res = await API.action(phase === 'paused' ? 'run.resume' : 'run.pause', {});
    if (res && res.ok) toast(res.message, 'ok');
  });

  $('#btnStop').addEventListener('click', async () => {
    const res = await API.action('run.stop', {});
    if (res && res.ok) toast(res.message, 'ok');
  });

  $('#btnFreeze').addEventListener('click', async () => {
    const res = await API.action('run.freeze', { reason: '手动保留现场' });
    if (res && res.ok) toast('现场已保全，DUT 保持不动电', 'ok');
  });

  $('#btnCanConnect').addEventListener('click', async () => {
    const can = (S.snapshot && S.snapshot.can) || {};
    const running = can.state === 'running';
    const res = await API.action(running ? 'can.disconnect' : 'can.connect', {});
    if (res) {
      toast(res.message || (res.ok ? (running ? 'CAN 已关闭' : 'CAN 已开启') : '操作失败'),
            res.ok ? 'ok' : 'error');
    }
  });

  $$('.preset-row .btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.preset;
      btn.disabled = true;
      try {
        const res = await API.action('preset', { name });
        if (res && res.ok === false) toast(res.message || '执行失败', 'error');
        else toast('已完成', 'ok');
      } finally {
        btn.disabled = false;
      }
    });
  });

  $('#canBusSelect').addEventListener('change', ev => {
    S.canBus = ev.target.value;
    renderCanIds(S.snapshot);
  });

  $('#btnSendFrame').addEventListener('click', async () => {
    const id = $('#mtxId').value.trim();
    const data = $('#mtxData').value.trim();
    const count = parseInt($('#mtxCount').value, 10) || 1;
    if (!id) { toast('先填报文 ID，例如 0x18FE4121', 'warn'); return; }
    const res = await API.action('can.send_frame', {
      bus: S.canBus || 'ican', id, data, period_ms: 100, count,
    });
    if (res && res.ok) toast('已开始发帧：' + id + ' ×' + count, 'ok');
  });

  $$('.stim-row .btn[data-stim]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const mode = btn.dataset.stim;
      const silent = mode === 'silence';
      const res = await API.action('can.stim', {
        action: silent ? 'stop' : 'start',
        mode,
        period_ms: mode === 'wake' ? 50 : 200,
      });
      if (res && res.ok) {
        toast(silent ? '激励已停止（网络静默）'
          : ('激励已启动：' + ({ any: '任意帧', wake: '唤醒帧' }[mode] || mode)), 'ok');
      }
    });
  });

  $('#btnScreen').addEventListener('click', () => captureAndShow());
  $('#btnScreenLast').addEventListener('click', () => {
    const last = (S.snapshot && S.snapshot.screens || [])[0];
    if (last) openScreen(last);
    else captureAndShow();
  });
  $('#btnScreenRefresh').addEventListener('click', () => captureAndShow());
  $('#btnScreenBase').addEventListener('click', async () => {
    const value = window.prompt(
      '这张画面显示的电压是多少伏？\n（填数字才会被“仪表显示数字”断言使用；留空只作图像比对）', '');
    if (value === null) return;
    const res = await API.action('screen.learn_baseline', { label: '电压基线', value: value.trim() });
    if (res && res.ok) toast('已学习基线：' + (res.baseline && res.baseline.shot || ''), 'ok');
  });
}

/* ---------------------------------------------------------------- tabs */
function bindTabs() {
  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      $$('.tab').forEach(node => node.classList.remove('active'));
      tab.classList.add('active');
      $$('.tab-body').forEach(body => body.classList.add('hidden'));
      const body = $('.tab-body[data-body="' + tab.dataset.tab + '"]');
      if (body) body.classList.remove('hidden');
    });
  });
}

/* ---------------------------------------------------------------- 采集开关 */
function bindProbeToggles() {
  const bind = (selector, which) => {
    const btn = document.querySelector(selector);
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const snap = S.snapshot || {};
      const probe = (snap.probe && snap.probe[which]) || {};
      const off = probe.enabled === false || probe.state === 'disabled';
      const res = await API.action('probe.toggle', { which, enabled: off });
      if (res && res.ok) toast(off ? '已开启采集' : '已关闭采集（资源已释放）', 'ok');
      else toast((res && res.message) || '操作失败', 'error');
    });
  };
  bind('#btnProbeSerial', 'serial');
  bind('#btnProbeAdb', 'adb');
}

/* ---------------------------------------------------------------- 读数区域圈选 */
let regionPicking = false;
let regionStart = null;

function regionBoxIn(wrap, region) {
  const img = document.querySelector('#screenImg');
  const w = (img && img.clientWidth) || wrap.clientWidth;
  const h = (img && img.clientHeight) || wrap.clientHeight;
  return {
    left: Math.round(region.x0 * w),
    top: Math.round(region.y0 * h),
    width: Math.round((region.x1 - region.x0) * w),
    height: Math.round((region.y1 - region.y0) * h),
  };
}

function currentRegionMetric() {
  const sel = document.querySelector('#regionMetric');
  return (sel && sel.value) || 'voltage';
}

function currentRegion() {
  const snap = S.snapshot || {};
  const metric = currentRegionMetric();
  const regions = snap.read_regions || {};
  return regions[metric] || snap.read_region || null;
}

function paintRegion(region) {
  const wrap = $('#screenWrap');
  const box = $('#screenSel');
  if (!wrap || !box) return;
  if (!region) { box.classList.add('hidden'); return; }
  const r = regionBoxIn(wrap, region);
  box.style.left = r.left + 'px';
  box.style.top = r.top + 'px';
  box.style.width = r.width + 'px';
  box.style.height = r.height + 'px';
  box.classList.remove('hidden');
  const hint = $('#regionHint');
  if (hint) {
    const label = (typeof METRIC_LABEL !== 'undefined' && METRIC_LABEL[currentRegionMetric()])
      || currentRegionMetric();
    hint.textContent = '读数区域（' + label + '）：x ' + (region.x0 * 100).toFixed(1) + '~' + (region.x1 * 100).toFixed(1)
      + '% , y ' + (region.y0 * 100).toFixed(1) + '~' + (region.y1 * 100).toFixed(1) + '%';
  }
}

function bindRegionPicker() {
  const wrap = $('#screenWrap');
  const img = $('#screenImg');
  const box = $('#screenSel');
  const pick = $('#btnRegionPick');
  if (!wrap || !img || !box || !pick) return;

  pick.addEventListener('click', () => {
    regionPicking = !regionPicking;
    pick.classList.toggle('primary', regionPicking);
    toast(regionPicking ? '在画面上拖一个框，圈住要读取的数字' : '已退出圈选', 'info');
  });

  const toFraction = (ev) => {
    const target = document.querySelector('#screenImg') || wrap;
    const rect = target.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
    };
  };

  img.addEventListener('mousedown', ev => {
    if (!regionPicking) return;
    ev.preventDefault();
    regionStart = toFraction(ev);
    box.style.left = (regionStart.x * img.clientWidth) + 'px';
    box.style.top = (regionStart.y * img.clientHeight) + 'px';
    box.style.width = '0px';
    box.style.height = '0px';
    box.classList.remove('hidden');
  });

  img.addEventListener('mousemove', ev => {
    if (!regionPicking || !regionStart) return;
    const cur = toFraction(ev);
    const x0 = Math.min(regionStart.x, cur.x);
    const y0 = Math.min(regionStart.y, cur.y);
    box.style.left = (x0 * img.clientWidth) + 'px';
    box.style.top = (y0 * img.clientHeight) + 'px';
    box.style.width = (Math.abs(cur.x - regionStart.x) * img.clientWidth) + 'px';
    box.style.height = (Math.abs(cur.y - regionStart.y) * img.clientHeight) + 'px';
  });

  img.addEventListener('mouseup', async ev => {
    if (!regionPicking || !regionStart) return;
    const cur = toFraction(ev);
    const region = {
      x0: Math.min(regionStart.x, cur.x), x1: Math.max(regionStart.x, cur.x),
      y0: Math.min(regionStart.y, cur.y), y1: Math.max(regionStart.y, cur.y),
    };
    regionStart = null;
    if ((region.x1 - region.x0) < 0.01 || (region.y1 - region.y0) < 0.01) {
      toast('框太小了，重画一个', 'error');
      return;
    }
    const res = await API.action('screen.set_region', { metric: currentRegionMetric(), region });
    if (res && res.ok) {
      paintRegion(res.region);
      regionPicking = false;
      pick.classList.remove('primary');
      toast('读数区域已保存；旧基线已清空，请重新「学基线」', 'ok');
    }
  });

  $('#btnScreenZoom')?.addEventListener('click', () => {
    const wrap = document.querySelector('#screenWrap');
    const img = $('#screenImg');
    if (!wrap || !img) return;
    const zoomed = wrap.classList.toggle('zoom');
    img.style.width = zoomed ? 'auto' : '100%';
    const btn = $('#btnScreenZoom');
    if (btn) btn.textContent = zoomed ? '适应窗口' : '放大 1:1';
    paintRegion(currentRegion());
  });

  $('#btnRegionReset')?.addEventListener('click', async () => {
    const res = await API.action('screen.set_region', {});
    if (res && res.ok) {
      paintRegion(res.region);
      toast('已恢复默认读数区域', 'ok');
    }
  });
}

/* ---------------------------------------------------------------- 抓屏 */
async function captureAndShow() {
  const res = await API.action('screen.capture', { label: '手动抓屏' });
  if (res && res.ok && res.screen) openScreen(res.screen);
}

function openScreen(shot) {
  if (!shot) return;
  $('#screenTitle').textContent = '仪表画面 · ' + (shot.time || '')
    + (shot.label ? ' · ' + shot.label : '');
  const img = $('#screenImg');
  img.onload = () => paintRegion(currentRegion());
  img.src = '/api/screen/' + shot.id + '.png?t=' + Date.now();
  const st = shot.stats || {};
  $('#screenStats').textContent = '非黑像素 ' + ((st.bright_ratio || 0) * 100).toFixed(1)
    + '% · 高亮 ' + ((st.text_ratio || 0) * 100).toFixed(1)
    + '% · 平均亮度 ' + (st.mean_luma || 0)
    + ' · 源 ' + (st.width || 0) + 'x' + (st.height || 0)
    + (st.width ? '（缩放到 ' + (st.out_width || 0) + 'x' + (st.out_height || 0) + '）' : '');
  $('#screenModal').classList.remove('hidden');
}

/* ---------------------------------------------------------------- 弹窗通用 */
function bindModals() {
  $$('[data-close]').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.close;
      const node = document.getElementById(id);
      if (node) node.classList.add('hidden');
    });
  });
  $$('.overlay').forEach(overlay => {
    overlay.addEventListener('mousedown', ev => {
      if (ev.target === overlay) overlay.classList.add('hidden');
    });
  });
}

/* ---------------------------------------------------------------- 设置 */
function bindSettings() {
  $('#btnSettings').addEventListener('click', () => {
    if (!S.snapshot) return;
    fillSettings(S.snapshot);
    fillPortOptions(S.snapshot);
    $('#settingsModal').classList.remove('hidden');
  });
  $('#btnSettingsApply').addEventListener('click', applySettings);
}

function fillSettings(snap) {
  const settings = snap.settings || {};
  const relay = settings.relay || {};
  const probes = settings.probes || {};
  const serial = probes.serial || {};
  const adb = probes.adb || {};
  const can = settings.can || {};
  const incident = settings.incident || {};
  const retention = settings.retention || {};

  $('#setRelayPort').value = relay.port || '';
  $('#setRelayBaud').value = relay.baud || 9600;
  $('#setRelayDry').checked = !!relay.dry_run;
  $('#setSerialPort').value = serial.port || '';
  $('#setSerialBaud').value = serial.baud || 460800;
  $('#setSerialRate').value = serial.log_rate || 40;
  $('#setBootGrace').value = probes.boot_grace_ms || 25000;
  $('#setCanPython').value = can.python || '';
  $('#setCanSdk').value = can.sdk_dir || '';
  $('#setCanType').value = can.device_type || 41;
  $('#setIncKb').value = incident.serial_tail_kb || 256;
  $('#setIncEvents').value = incident.events_tail || 300;
  $('#setIncShot').checked = incident.screenshot !== false;
  $('#setIncKeep').checked = incident.keep_power !== false;
  $('#setRetRuns').value = retention.max_runs || 60;
  $('#setRetDays').value = retention.max_days || 30;

  const wrap = $('#setCanChannels');
  clear(wrap);
  const channels = can.channels || {};
  for (const [bus, item] of Object.entries(channels)) {
    wrap.appendChild(el('label', { class: 'field' }, [
      el('span', { text: bus.toUpperCase() + ' 通道' }),
      el('input', { type: 'number', value: item.ch, dataset: { bus, field: 'ch' }, style: 'width:56px' }),
    ]));
    wrap.appendChild(el('label', { class: 'field' }, [
      el('span', { text: '波特率' }),
      el('input', { type: 'number', value: item.baud, dataset: { bus, field: 'baud' }, style: 'width:92px' }),
    ]));
  }
}

function fillPortOptions(snap) {
  const list = $('#portOptions');
  if (!list) return;
  clear(list);
  for (const item of (snap && snap.serial_ports) || []) {
    const desc = item.desc ? (' · ' + item.desc) : '';
    list.appendChild(el('option', { value: item.port, label: item.port + desc }));
  }
}

async function applySettings() {
  const canChannels = {};
  $$('#setCanChannels input').forEach(input => {
    const bus = input.dataset.bus;
    const field = input.dataset.field;
    canChannels[bus] = canChannels[bus] || {};
    canChannels[bus][field] = parseInt(input.value, 10) || 0;
  });
  const snapshotBefore = S.snapshot || {};
  const settings = {
    relay: {
      port: $('#setRelayPort').value.trim(),
      baud: parseInt($('#setRelayBaud').value, 10) || 9600,
      dry_run: $('#setRelayDry').checked,
    },
    probes: {
      boot_grace_ms: parseInt($('#setBootGrace').value, 10) || 0,
      serial: {
        port: $('#setSerialPort').value.trim(),
        baud: parseInt($('#setSerialBaud').value, 10) || 460800,
        log_rate: parseFloat($('#setSerialRate').value) || 40,
      },
      adb: ((snapshotBefore.settings || {}).probes || {}).adb || {},
    },
    can: {
      python: $('#setCanPython').value.trim(),
      sdk_dir: $('#setCanSdk').value.trim(),
      device_type: parseInt($('#setCanType').value, 10) || 41,
      channels: Object.keys(canChannels).length
        ? canChannels
        : ((((snapshotBefore.settings || {}).can) || {}).channels || {}),
    },
    incident: {
      serial_tail_kb: parseInt($('#setIncKb').value, 10) || 256,
      events_tail: parseInt($('#setIncEvents').value, 10) || 300,
      screenshot: $('#setIncShot').checked,
      keep_power: $('#setIncKeep').checked,
    },
    retention: {
      max_runs: parseInt($('#setRetRuns').value, 10) || 0,
      max_days: parseInt($('#setRetDays').value, 10) || 0,
    },
  };
  const res = await API.action('settings.save', { settings });
  if (res && res.ok) {
    $('#settingsModal').classList.add('hidden');
    const relay = res.relay || {};
    toast(relay.message ? ('设置已保存；继电器: ' + relay.message) : '设置已保存',
          relay.ok === false ? 'warn' : 'ok');
  }
}

/* ---------------------------------------------------------------- 快捷键 */
function bindKeys() {
  document.addEventListener('keydown', ev => {
    const tag = (ev.target.tagName || '').toLowerCase();
    const editing = tag === 'input' || tag === 'select' || tag === 'textarea';
    if (ev.key === 'Escape') {
      $$('.overlay').forEach(node => node.classList.add('hidden'));
      if (!editing) $('#btnStop').click();
      return;
    }
    if (editing) return;
    if (ev.code === 'Space') {
      ev.preventDefault();
      const phase = S.snapshot && S.snapshot.runner && S.snapshot.runner.phase;
      if (['running', 'paused'].includes(phase)) $('#btnPause').click();
      else $('#btnStart').click();
    } else if (ev.key === 'f' || ev.key === 'F') {
      $('#btnFreeze').click();
    } else if (ev.key === 's' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      saveScenario();
    } else if (ev.altKey && (ev.key === 'ArrowUp' || ev.key === 'ArrowDown')) {
      ev.preventDefault();
      if (S.selection >= 0) moveState(S.selection, ev.key === 'ArrowUp' ? -1 : 1);
    }
  });
}
