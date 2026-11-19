/* ==========================================================================
   core.js — 全局状态、API 封装、通用工具
   ========================================================================== */
'use strict';

const S = {
  snapshot: null,        // 服务端最新快照（SSE 每秒推）
  scenario: null,        // 客户端编辑中的剧本（权威副本）
  scenarioDirty: false,
  scenarioFile: '',
  logs: [],              // 环形日志（最多 ui.log_cap）
  logLevel: 'all',
  logFilter: '',
  logPaused: false,
  incidents: [],
  activeIncident: '',
  rules: [],
  rulesInit: false,
  ruleEditingId: '',
  stateEditingIndex: -1,
  canBus: '',
  sseOk: false,
  lastSeq: 0,
  dropped: 0,
  connected: false,
  selection: -1,
};

const API = {
  async snapshot() {
    const resp = await fetch('/api/snapshot', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.message || '快照失败');
    return data.state;
  },
  async action(op, payload) {
    const body = Object.assign({ op }, payload || {});
    const resp = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data;
    try { data = await resp.json(); } catch (e) { data = { ok: false, message: '响应不是 JSON' }; }
    if (data && data.ok === false) {
      toast(data.message || '操作失败', 'error');
    }
    return data;
  },
};

/* ---------------------------------------------------------------- DOM 工具 */
function $(sel, root) { return (root || document).querySelector(sel); }
function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else node.setAttribute(key, value === true ? '' : value);
    }
  }
  for (const child of (children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

/* ---------------------------------------------------------------- 格式化 */
function fmtClock(ms) {
  const d = new Date(ms || Date.now());
  const p = (n, w) => String(n).padStart(w || 2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return Math.round(ms) + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return m + 'm' + (s ? s + 's' : '');
}

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function escapeHtml(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toast(message, kind) {
  const wrap = $('#toasts');
  if (!wrap) return;
  const node = el('div', { class: 'toast ' + (kind || ''), text: message });
  wrap.appendChild(node);
  setTimeout(() => { node.style.opacity = '0'; node.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => node.remove(), 3600);
}

function hashText(text) {
  let h = 5381;
  const str = String(text || '');
  for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
  return h;
}

/* ---------------------------------------------------------------- 通道语义 */
const CHANNEL_GLYPH = { relay: '', can: '', custom: '' };
const WAIT_LABEL = {
  none: '', adb_online: '等 SOC 在线', serial_match: '等串口匹配',
  can_frame: '等 CAN 报文', log_growing: '等日志增长', pwr_state: '等电源',
};
const CHECK_LABEL = {
  no_error: '无 ERROR', serial_seen: '串口匹配', can_seen: 'CAN 收到',
  adb_online: 'SOC 在线', pwr_state: '电源状态', pwr_seen: '到过电源状态',
};
const ACTION_LABEL = {
  can_tx_start: '启动 CAN 发送', can_tx_stop: '停止 CAN 发送',
  mark: '打标记', can_stim: 'CAN 激励', capture: '抓屏',
};
const METRIC_LABEL = {
  voltage: '蓄电池电压(V)', odo: '总里程(km)', trip: '小计里程(km)', soc: 'SOC(%)',
};
const CAPTURE_WHEN_LABEL = {
  entry: '进入状态时', on_pass: '断言通过时', on_fail: '断言失败时',
  on_timeout: '等待超时时', interval: '定时周期',
};
const STIM_LABEL = {
  any: '任意帧', wake: '唤醒帧', frame: '指定帧', silence: '静默',
};
const PWR_NAMES = { 0: '关机', 1: '待机', 2: '监听', 3: '预开机', 4: '开机' };
function pwrName(value) {
  return PWR_NAMES[value] || ('#' + value);
}
const RULE_LABEL = {
  regex: '串口正则', silence: '串口静默', can_timeout: 'CAN 超时',
  adb_offline: 'SOC 掉线', log_stall: '日志停更',
};

function channelLabel(id) {
  const snap = S.snapshot;
  if (!snap) return id;
  const found = (snap.channels || []).find(c => c.id === id);
  return found ? found.label : id;
}

function phaseText(phase) {
  return {
    idle: '空闲', running: '运行中', paused: '已暂停', stopping: '停止中',
    finished: '已完成', error: '异常',
  }[phase] || phase;
}

/* ---------------------------------------------------------------- 剧本操作 */
function scenarioHash(sc) {
  if (!sc) return 0;
  return hashText(JSON.stringify({
    name: sc.name, loops: sc.loops, interval: sc.loop_interval_ms,
    can: sc.can, states: sc.states,
  }));
}

function estimateScenario(sc) {
  if (!sc || !sc.states) return 0;
  let total = 0;
  for (const st of sc.states) {
    total += Number(st.hold_ms) || 0;
    const w = st.wait || {};
    if (w.type && w.type !== 'none') total += Math.round((Number(w.timeout_ms) || 0) / 2);
  }
  total += Number(sc.loop_interval_ms) || 0;
  return total;
}

function validateScenarioLocal(sc) {
  const errors = [];
  const warnings = [];
  if (!sc) return { errors: ['没有剧本'], warnings: [] };
  if (!sc.states || !sc.states.length) errors.push('剧本里至少需要一张状态卡');
  if (Number(sc.loops) < 0) errors.push('循环次数不能为负（0 = 无限）');
  const known = new Set(((S.snapshot && S.snapshot.channels) || []).map(c => c.id));
  (sc.states || []).forEach((st, i) => {
    const label = '第 ' + (i + 1) + ' 张「' + (st.name || '?') + '」';
    for (const [cid, value] of Object.entries(st.channels || {})) {
      if (known.size && !known.has(cid)) errors.push(label + ' 引用了不存在的通道 ' + cid);
      if (!['on', 'off', 'keep'].includes(value)) errors.push(label + ' 通道取值非法: ' + cid + '=' + value);
    }
    if (Number(st.hold_ms) < 0) errors.push(label + ' 保持时长不能为负');
    const w = st.wait || {};
    if (w.type === 'serial_match') {
      try { new RegExp(w.value || ''); } catch (e) { errors.push(label + ' 等待正则无效: ' + e.message); }
    }
    (st.checks || []).forEach(chk => {
      if (chk.type === 'serial_seen') {
        try { new RegExp(chk.value || ''); } catch (e) { errors.push(label + ' 断言正则无效: ' + e.message); }
      }
    });
  });
  return { errors, warnings };
}

function clone(obj) { return JSON.parse(JSON.stringify(obj)); }
