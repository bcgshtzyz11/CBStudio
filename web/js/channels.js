/* ==========================================================================
   channels.js — 左栏：通道开关、采集状态、CAN 总览
   ========================================================================== */
'use strict';

const pendingChannels = new Set();

/* ---------------------------------------------------------------- 通道 */
function renderChannels(snap) {
  const list = $('#channelList');
  if (!list) return;
  const channels = snap.channels || [];
  const existing = new Map();
  $$('.channel', list).forEach(node => existing.set(node.dataset.id, node));

  for (const ch of channels) {
    let node = existing.get(ch.id);
    if (!node) {
      node = el('div', { class: 'channel', dataset: { id: ch.id } }, [
        el('div', { class: 'channel-main' }, [
          el('b', { text: ch.label }),
          el('small', { text: ch.detail || ch.kind }),
        ]),
        el('button', { class: 'switch', title: '点击切换', 'aria-label': ch.label }, [el('span')]),
      ]);
      node.addEventListener('click', () => toggleChannel(ch.id));
      list.appendChild(node);
      existing.set(ch.id, node);
    }
    const isOn = ch.state === 'on';
    node.classList.toggle('on', isOn);
    node.classList.toggle('off', !isOn);
    node.classList.toggle('unavailable', ch.available === false);
    node.classList.toggle('pending', pendingChannels.has(ch.id));
    const detail = node.querySelector('small');
    if (detail) {
      const err = ch.error ? ' · ' + ch.error : '';
      detail.textContent = (ch.detail || ch.kind) + err;
      detail.title = detail.textContent;
    }
  }
  // 删掉已不存在的通道节点
  for (const [id, node] of existing) {
    if (!channels.find(c => c.id === id)) node.remove();
  }
  const hint = $('#channelHint');
  if (hint && snap.relay) {
    hint.textContent = snap.relay.dry_run
      ? '干跑模式：不驱动硬件'
      : (snap.relay.open ? snap.relay.port + ' 已连接' : '继电器未连接');
  }
}

async function toggleChannel(id) {
  const snap = S.snapshot;
  const ch = (snap && snap.channels || []).find(c => c.id === id);
  if (!ch || pendingChannels.has(id)) return;
  const target = ch.state === 'on' ? 'off' : 'on';
  pendingChannels.add(id);
  ch.state = target;                    // 乐观更新，失败再由服务端快照纠正
  renderChannels(snap);
  try {
    const res = await API.action('channel.set', { id, state: target });
    if (res && res.ok === false) {
      ch.state = target === 'on' ? 'off' : 'on';
    } else if (res && res.message) {
      toast(ch.label + '：' + res.message, 'ok');
    }
  } finally {
    pendingChannels.delete(id);
    if (S.snapshot) renderChannels(S.snapshot);
  }
}

/* ---------------------------------------------------------------- 采集 */
function renderProbes(snap) {
  const serial = (snap.probe && snap.probe.serial) || {};
  const adb = (snap.probe && snap.probe.adb) || {};
  const can = snap.can || {};

  const serialRow = $('#probeSerial');
  const serialOff = serial.enabled === false || serial.state === 'disabled';
  const serialOk = serial.state === 'open';
  serialRow.className = 'probe-row '
    + (serialOff ? 'off' : (serialOk ? 'ok' : (serial.state === 'error' ? 'err' : 'warn')));
  $('#probeSerialDetail').textContent = serialOff
    ? '已关闭（串口已释放）'
    : (serialOk
      ? serial.port + ' @' + serial.baud + ' · 已收 ' + (serial.lines || 0) + ' 行'
      : (serial.message || '未打开'));
  $('#probeSerialRate').textContent = serialOff ? '—' : (serial.fps || 0).toFixed(0) + ' 行/s';
  const serialBtn = $('#btnProbeSerial');
  if (serialBtn) serialBtn.textContent = serialOff ? '开启' : '关闭';

  const adbRow = $('#probeAdb');
  const adbOff = adb.enabled === false || adb.state === 'disabled';
  adbRow.className = 'probe-row ' + (adbOff ? 'off' : (adb.online ? 'ok' : (adb.ever_online ? 'err' : 'warn')));
  const adbBtn = $('#btnProbeAdb');
  if (adbBtn) adbBtn.textContent = adbOff ? '开启' : '关闭';
  $('#probeAdbDetail').textContent = adbOff ? '已关闭' : (adb.online
    ? '在线 · uptime ' + fmtMs((adb.uptime_s || 0) * 1000) + ' · 日志 ' + fmtSize(adb.log_size)
    : (adb.ever_online ? '已离线 ' + fmtMs(adb.offline_ms) : '未检测到设备'));
  $('#probeAdbRate').textContent = (adbOff || !adb.online)
    ? '—'
    : (adb.growth > 0 ? '+' + fmtSize(adb.growth) + '/s' : '日志未增长');

  const canRow = $('#probeCan');
  const canRunning = can.state === 'running';
  const canBusy = can.state === 'starting' || can.state === 'stopping';
  canRow.className = 'probe-row '
    + (canRunning ? 'ok' : (canBusy ? 'warn' : (can.state === 'error' ? 'err' : 'off')));
  $('#probeCanDetail').textContent = canRunning
    ? (can.device && can.device.type ? can.device.type : 'ZLG') + ' · Python ' + (can.python || '?')
    : (canBusy ? (can.state === 'starting' ? '连接中…' : '断开中…')
      : (can.message && can.state === 'error' ? can.message : '已关闭（设备已释放）'));
  const btn = $('#btnCanConnect');
  if (btn) {
    btn.textContent = canRunning ? '关闭' : (canBusy ? '处理中' : '开启');
    btn.disabled = canBusy;
  }

  const pwr = (snap.probe && snap.probe.pwr) || {};
  const pwrRow = $('#probePwr');
  if (pwrRow) {
    const known = pwr.state !== null && pwr.state !== undefined;
    pwrRow.className = 'probe-row ' + (!known ? 'warn' : (pwr.state >= 2 ? 'ok' : ''));
    const gate = (pwr.allowed === null || pwr.allowed === undefined)
      ? 'CAN 外发闸门未知'
      : ('CAN 外发' + (pwr.allowed ? '放行' : '关闭'));
    $('#probePwrDetail').textContent = !known
      ? '等待 MCU 日志（I_PWRMNG_IO）'
      : [gate, pwr.vbat ? pwr.vbat + 'V' : ''].filter(Boolean).join(' · ');
    $('#probePwrState').textContent = known ? (pwrName(pwr.state) + '(' + pwr.state + ')') : '—';
  }

  renderCanBuses(snap);
}

function renderCanBuses(snap) {
  const wrap = $('#canBuses');
  if (!wrap) return;
  const can = snap.can || {};
  const buses = can.buses || {};
  const ids = Object.keys(buses);
  if (!ids.length) {
    wrap.innerHTML = '';
    return;
  }
  if (!S.canBus || !buses[S.canBus]) S.canBus = ids[0];
  clear(wrap);
  for (const name of ids) {
    const bus = buses[name] || {};
    wrap.appendChild(el('div', { class: 'can-bus' }, [
      el('b', { text: name.toUpperCase() + ' · CH' + bus.ch }),
      el('span', { class: bus.tx ? 'tx-on' : 'tx-off', text: bus.tx ? 'TX 开' : 'TX 关' }),
      el('span', { text: ' · ' + (bus.rx_fps || 0).toFixed(0) + ' fps' }),
    ]));
  }
  const select = $('#canBusSelect');
  if (select) {
    const current = select.value;
    clear(select);
    for (const name of ids) select.appendChild(el('option', { value: name, text: name.toUpperCase() }));
    select.value = buses[current] ? current : ids[0];
    S.canBus = select.value;
  }
}

/* ---------------------------------------------------------------- CAN 报文表 */
function renderCanIds(snap) {
  const table = $('#canIdTable');
  if (!table) return;
  const canIds = snap.can_ids || {};
  const rows = canIds[S.canBus] || [];
  if (!(snap.can && snap.can.state === 'running')) {
    table.innerHTML = '<div class="empty">连接 CAN 后显示接收统计</div>';
    return;
  }
  if (!rows.length) {
    table.innerHTML = '<div class="empty">' + (S.canBus || '该总线') + ' 暂无接收报文</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const row of rows) {
    const stale = (row.age_ms || 0) > 2000;
    frag.appendChild(el('div', { class: 'id-row' + (stale ? ' stale' : '') }, [
      el('span', { class: 'id', text: row.id }),
      el('span', { class: 'dlc', text: 'D' + (row.dlc || 0) }),
      el('span', { class: 'per', text: row.period_ms ? row.period_ms.toFixed(0) + 'ms' : '—' }),
      el('span', { class: 'dat', text: row.data || '' }),
    ]));
  }
  clear(table);
  table.appendChild(frag);
}
