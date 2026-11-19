/* can.js — CAN 设备 / 通道参数 / DBC 弹窗 */

const CAN_BAUD_PRESETS = ['125000', '250000', '500000', '1000000'];
const CAN_DATA_BAUD_PRESETS = ['1000000', '2000000', '4000000', '5000000'];

function canChannelRows() {
  const snap = S.snapshot || {};
  const cfg = (snap.can && snap.can.channel_cfg) || {};
  const buses = Object.keys(cfg);
  if (!buses.length) return [['ican', 0], ['pcan', 1]];
  return buses.map(bus => [bus, (cfg[bus] && cfg[bus].ch) || 0]);
}

function canNumInput(value, width, title, list) {
  const input = el('input', {
    type: 'number', class: 'mini-input',
    value: value === undefined || value === null ? '' : String(value),
    title: title || '', style: 'width:' + (width || 88) + 'px',
  });
  if (list) input.setAttribute('list', list);
  return input;
}

function buildCanRows() {
  const body = $('#canChBody');
  if (!body) return;
  clear(body);
  const snap = S.snapshot || {};
  const cfg = (snap.can && snap.can.channel_cfg) || {};
  const live = (snap.can && snap.can.buses) || {};
  for (const [bus, ch] of canChannelRows()) {
    const item = cfg[bus] || {};
    const enabled = item.enabled !== false;
    const proto = item.proto || 'canfd';
    const fdStandard = item.fd_standard === 1 ? '1' : '0';
    const accel = item.accel === true ? '1' : '0';
    const mode = Number(item.mode || 0);
    const terminator = item.terminator !== false;

    const chk = el('input', { type: 'checkbox' });
    chk.checked = enabled;
    const protoSel = el('select', { class: 'mini-select' }, [
      el('option', { value: 'can', text: '经典 CAN' }),
      el('option', { value: 'canfd', text: 'CAN FD' }),
    ]);
    protoSel.value = proto;
    const stdSel = el('select', { class: 'mini-select' }, [
      el('option', { value: '0', text: 'ISO' }),
      el('option', { value: '1', text: '非 ISO' }),
    ]);
    stdSel.value = fdStandard;
    const accSel = el('select', { class: 'mini-select' }, [
      el('option', { value: '0', text: '否' }),
      el('option', { value: '1', text: '是' }),
    ]);
    accSel.value = accel;
    const arb = canNumInput(item.baud || 500000, 96, '仲裁域波特率');
    const arbSp = canNumInput(item.arb_sp, 64, '仲裁域采样点（空=设备默认）');
    const dataBaud = canNumInput(item.data_baud || 2000000, 96, '数据域波特率');
    const dataSp = canNumInput(item.data_sp, 64, '数据域采样点（空=设备默认）');
    const modeSel = el('select', { class: 'mini-select' }, [
      el('option', { value: '0', text: '正常' }),
      el('option', { value: '1', text: '只听' }),
    ]);
    modeSel.value = String(mode);
    const termSel = el('select', { class: 'mini-select' }, [
      el('option', { value: '1', text: '使能' }),
      el('option', { value: '0', text: '禁用' }),
    ]);
    termSel.value = terminator ? '1' : '0';
    const running = !!(live[bus] && live[bus].frames !== undefined && snap.can.state === 'running');
    const startBtn = el('button', { class: 'btn tiny' + (running ? ' ghost' : ''), text: running ? '停止' : '启动' });
    startBtn.addEventListener('click', async () => {
      const op = running ? 'can.channel_stop' : 'can.channel_start';
      const res = await API.action(op, { bus });
      if (res && res.ok) toast((running ? '已停止 ' : '已启动 ') + bus, 'ok');
      else toast((res && res.message) || '操作失败', 'error');
    });

    const tr = el('tr');
    tr.append(
      el('td', { text: bus + ' (CH' + ch + ')' }),
      el('td', {}, [chk]),
      el('td', {}, [protoSel]),
      el('td', {}, [stdSel]),
      el('td', {}, [accSel]),
      el('td', {}, [arb]),
      el('td', {}, [arbSp]),
      el('td', {}, [dataBaud]),
      el('td', {}, [dataSp]),
      el('td', {}, [modeSel]),
      el('td', {}, [termSel]),
      el('td', {}, [startBtn]),
    );
    tr._read = () => ({
      bus,
      item: {
        ch,
        enabled: chk.checked,
        proto: protoSel.value,
        fd_standard: parseInt(stdSel.value, 10),
        accel: accSel.value === '1',
        baud: parseInt(arb.value, 10) || 500000,
        data_baud: parseInt(dataBaud.value, 10) || 2000000,
        mode: parseInt(modeSel.value, 10) || 0,
        terminator: termSel.value === '1',
        arb_sp: arbSp.value === '' ? null : parseFloat(arbSp.value),
        data_sp: dataSp.value === '' ? null : parseFloat(dataSp.value),
      },
    });
    body.append(tr);
  }
}

function renderCanModal(snap) {
  if (!$('#canModal') || $('#canModal').classList.contains('hidden')) return;
  const can = (snap && snap.can) || {};
  const device = can.device || {};
  const info = $('#canDevInfo');
  if (info) {
    info.textContent = can.state === 'running'
      ? '已连接 ' + (device.type || '') + '  hw=' + (device.hw || '?') + '  fw=' + (device.fw || '?')
        + '  序列号=' + (device.serial || '?')
      : ('未连接' + (can.message ? '：' + can.message : ''));
  }
  const typeSel = $('#canDeviceType');
  if (typeSel) {
    const cfgType = (snap.config && snap.config.can && snap.config.can.device_type) || 0;
    typeSel.value = String(cfgType);
    if (typeSel.value === '' ) typeSel.value = '0';
  }
  buildCanRows();
  renderDbcRows(snap);
}

function renderDbcRows(snap) {
  const dbc = (snap && snap.dbc) || null;
  const info = document.querySelector('#dbcInfo');
  const body = document.querySelector('#dbcBody');
  if (!info || !body) return;
  const busSel = document.querySelector('#dbcBus');
  const buses = (dbc && dbc.buses) || canChannelRows().map(row => row[0]);
  if (busSel && busSel.options.length !== buses.length) {
    clear(busSel);
    for (const bus of buses) busSel.append(el('option', { value: bus, text: bus }));
  }
  if (!dbc || !(dbc.items || []).length) {
    info.textContent = '未导入';
    clear(body);
    return;
  }
  const items = dbc.items;
  info.textContent = items.map(it => it.bus + '：' + it.name + '（' + it.messages + ' 报文 / '
    + it.signals + ' 信号，实时 ' + (it.rows || []).length + '）').join('；');
  clear(body);
  for (const it of items) {
    body.append(el('tr', {}, [el('td', { text: it.bus + ' · ' + it.name, colspan: 5 })]));
    for (const row of (it.rows || []).slice(0, 40)) {
      body.append(el('tr', {}, [
        el('td', { text: it.bus }),
        el('td', { text: row.msg + ' (0x' + row.id.toString(16).toUpperCase() + ')' }),
        el('td', { text: row.signal }),
        el('td', { text: row.value }),
        el('td', { text: row.unit || '' }),
      ]));
    }
  }
}

async function applyCanConfig() {
  const channels = {};
  for (const tr of $$('#canChBody tr')) {
    if (!tr._read) continue;
    const { bus, item } = tr._read();
    channels[bus] = item;
  }
  const typeSel = $('#canDeviceType');
  const deviceType = typeSel ? parseInt(typeSel.value, 10) : 0;
  const res = await API.action('can.apply', { can: { device_type: deviceType, channels } });
  if (res && res.ok) toast('CAN 参数已应用：' + (res.message || ''), 'ok');
  else toast((res && res.message) || '应用失败', 'error');
}

function bindCanModal() {
  const openBtn = $('#btnCanCfg');
  if (openBtn) {
    openBtn.addEventListener('click', () => {
      $('#canModal').classList.remove('hidden');
      renderCanModal(S.snapshot || {});
    });
  }
  const apply = $('#btnCanApply');
  if (apply) apply.addEventListener('click', applyCanConfig);

  const openDev = $('#btnCanOpen');
  if (openDev) {
    openDev.addEventListener('click', async () => {
      const res = await API.action('can.connect', {});
      if (res && res.ok) toast('CAN 已连接', 'ok'); else toast((res && res.message) || '连接失败', 'error');
    });
  }
  const closeDev = $('#btnCanClose');
  if (closeDev) {
    closeDev.addEventListener('click', async () => {
      const res = await API.action('can.disconnect', {});
      if (res && res.ok) toast('CAN 已断开', 'ok');
    });
  }

  const file = $('#dbcFile');
  if (file) {
    file.addEventListener('change', async () => {
      const picked = file.files && file.files[0];
      if (!picked) return;
      const text = await picked.text();
      const busSel = document.querySelector('#dbcBus');
      const bus = (busSel && busSel.value) || '';
      const res = await API.action('can.dbc_import', { bus, name: picked.name, text });
      if (res && res.ok) {
        toast('DBC 已绑到 ' + res.bus + '：' + res.messages + ' 报文 / ' + res.signals + ' 信号', 'ok');
        renderDbcRows(S.snapshot || {});
      } else {
        toast((res && res.message) || '导入失败', 'error');
      }
      file.value = '';
    });
  }
  const clearDbc = $('#btnDbcClear');
  if (clearDbc) {
    clearDbc.addEventListener('click', async () => {
      const busSel = document.querySelector('#dbcBus');
      await API.action('can.dbc_clear', { bus: (busSel && busSel.value) || '' });
      toast('已清除 DBC', 'ok');
      renderDbcRows(S.snapshot || {});
    });
  }
}
