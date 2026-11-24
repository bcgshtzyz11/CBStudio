# -*- coding: utf-8 -*-
"""relay.py — LCUS-4 USB 继电器驱动（POWER / ACC / IG 上下电）

硬件与协议（详见工程 skill ``relay-power-control``）::

    串口 9600 8N1，4 字节定长帧：A0 <通道 1..4> <0x01 吸合=给电 | 0x00 释放=断电> <校验和>
    校验和 = (0xA0 + 通道 + 动作) & 0xFF

通道约定：1 = 主电 POWER（关掉整机断电）、2 = ACC、3 = IG、4 = 备用。

能力边界：

* **没有回读、没有应答**。发出帧后无法从继电器侧确认状态，因此内部状态是
  "最后一次成功写入的值"，界面上标注为推定值；是否真的上电/断电只能看 DUT
  （adb / 串口 / 屏幕）。
* 因此每次动作都发**该通道的目标状态**（``A0 ch 01/00``），不做"切换"。
* 序列动作（整机上电/断电）按通道顺序逐个发，通道之间留 ``STEP_DELAY_S``，
  与压测规范一致。
"""

import os
import time

try:
    import serial
    from serial.tools import list_ports
    HAVE_PYSERIAL = True
except Exception:                                       # 环境没装 pyserial 时降级
    serial = None
    list_ports = None
    HAVE_PYSERIAL = False

FRAME_HEAD = 0xA0
STEP_DELAY_S = 0.5          # 通道之间的间隔（skill 第 3 节：0.5s）
DEFAULT_BAUD = 9600
CHANNEL_NAMES = {1: "主电 POWER", 2: "ACC", 3: "IG", 4: "备用"}


def checksum(channel, on):
    """校验和 = (A0 + 通道 + 动作) & 0xFF。"""
    return (FRAME_HEAD + int(channel) + (1 if on else 0)) & 0xFF


def build_frame(channel, on):
    """组一帧 4 字节继电器指令。"""
    return bytes([FRAME_HEAD, int(channel), 1 if on else 0, checksum(channel, on)])


def list_serial_ports():
    """枚举串口及描述（``[{"port","desc","hwid"}]``），用于界面选口。"""
    if not HAVE_PYSERIAL:
        return []
    result = []
    for info in list_ports.comports():
        result.append({
            "port": info.device,
            "desc": info.description or "",
            "hwid": info.hwid or "",
        })
    result.sort(key=lambda item: item["port"])
    return result


def relay_candidates(preferred=None, exclude=()):
    """按优先级列出继电器串口候选（跳过别的探针占用的口）。

    排序：配置指定的口 > CH340（LCUS-4 常见）> 其它 USB 串口 > 板载串口。
    返回 ``([{"port","desc","hwid","why"}], reason)``。
    """
    ports = list_serial_ports()
    used = {str(p).upper() for p in (exclude or []) if p}
    if preferred:
        for item in ports:
            if item["port"].upper() == str(preferred).upper():
                return [dict(item, why="配置指定")], "配置指定"
        return [{"port": str(preferred).upper(), "desc": "", "hwid": "",
                 "why": "配置指定"}], "配置指定（当前未枚举到）"

    def is_ch340(item):
        text = (item["desc"] + " " + item["hwid"]).upper()
        return "CH340" in text or "1A86" in text

    ranked = []
    for item in ports:
        if item["port"].upper() in used:
            continue
        item = dict(item)
        if is_ch340(item):
            item["why"] = "自动识别 CH340"
            ranked.insert(0, item)
        else:
            item["why"] = "自动识别 USB 串口"
            ranked.append(item)
    if not ranked:
        return [], "未找到可用继电器串口（%d 个串口都被占用或不存在）" % len(ports)
    if len(ranked) > 1:
        reason = "%s（共 %d 个候选，取第一个）" % (ranked[0]["why"], len(ranked))
    else:
        reason = ranked[0]["why"]
    return ranked, reason


def detect_relay_port(preferred=None, exclude=()):
    """挑一个继电器串口，返回 ``(port, reason)``；找不到时 ``port`` 为 ``None``。"""
    candidates, reason = relay_candidates(preferred, exclude)
    if not candidates:
        return None, reason
    return candidates[0]["port"], reason


class RelayController(object):
    """继电器控制器：打开串口、按通道写状态、记录推定状态。

    ``dry_run=True`` 时不碰硬件，只把帧记录下来（界面/日志仍显示动作），
    用于无硬件联调与排练剧本。
    """

    def __init__(self, port=None, baud=DEFAULT_BAUD, dry_run=False, log=None,
                 states_path=None, exclude=None):
        self.port = port
        self.exclude_ports = list(exclude or [])
        self.exclude_provider = None
        self.baud = int(baud or DEFAULT_BAUD)
        self.dry_run = bool(dry_run)
        self._serial = None
        self._log = log
        self._states = {}                 # 通道 -> True/False（推定值）
        self._states_path = states_path
        self._load_states()
        self.last_error = None
        self.port_reason = ""
        self._lock = __import__("threading").RLock()
        self.frames_sent = 0

    def _excluded(self):
        """当前被其它用途占用的串口（例如 DUT 调试口），自动识别时跳过。"""
        ports = set(self.exclude_ports)
        provider = self.exclude_provider
        if provider:
            try:
                ports.update(provider() or [])
            except Exception:
                pass
        return ports

    # -- 推定状态持久化 ---------------------------------------------------
    def _load_states(self):
        """开机时恢复上次的推定状态（继电器没有回读，只能沿用"最后命令值"）。"""
        if not self._states_path or not os.path.isfile(self._states_path):
            return
        try:
            import json
            with open(self._states_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self._states = {int(key): bool(value) for key, value in raw.items()}
        except Exception:
            self._states = {}

    def _save_states(self):
        """把推定状态落盘，供下次启动恢复。"""
        if not self._states_path:
            return
        try:
            import json
            os.makedirs(os.path.dirname(self._states_path) or ".", exist_ok=True)
            tmp = self._states_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({str(key): value for key, value in self._states.items()}, handle)
            os.replace(tmp, self._states_path)
        except OSError:
            pass

    # -- 生命周期 ---------------------------------------------------------
    def open(self):
        """打开串口；返回 ``(ok, message)``。"""
        with self._lock:
            if self._serial is not None:
                return True, "已打开 %s" % self.port
            if self.dry_run:
                self.port = self.port or "DRY-RUN"
                self.port_reason = "干跑模式（不碰硬件）"
                return True, "干跑模式，继电器动作只记录不发送"
            if not HAVE_PYSERIAL:
                self.last_error = "未安装 pyserial"
                return False, self.last_error
            if not self.port:
                exclude = self._excluded()
                candidates, reason = relay_candidates(None, exclude)
                self.port_reason = reason
                if not candidates:
                    self.last_error = reason
                    return False, reason
                last_error = None
                for item in candidates:
                    try:
                        self._serial = serial.Serial(item["port"], self.baud,
                                                     timeout=0.3, write_timeout=1.0)
                        self.port = item["port"]
                        self.port_reason = item["why"]
                        break
                    except Exception as exc:
                        last_error = "%s 打不开: %s" % (item["port"], exc)
                if self._serial is None:
                    self.last_error = last_error or reason
                    return False, self.last_error
            if self._serial is None:
                try:
                    self._serial = serial.Serial(self.port, self.baud, timeout=0.3,
                                                 write_timeout=1.0)
                except Exception as exc:
                    self.last_error = "打开 %s 失败: %s" % (self.port, exc)
                    return False, self.last_error
            time.sleep(0.15)              # CH340 打开后首次写需要一点稳定时间
            if self._log:
                self._log.info("relay", "继电器串口已打开 %s @%d" % (self.port, self.baud),
                               port=self.port)
            return True, "已打开 %s" % self.port

    def close(self):
        """关闭串口（不改动继电器状态）。"""
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

    def reconfigure(self, port=None, baud=None, dry_run=None):
        """改串口/波特率/干跑模式，并立即按新参数重连。

        以前保存设置只改 ``self.port`` 属性，``Serial`` 句柄还绑在旧 COM 上，
        写指令照样发到老端口；这里统一「关旧口 -> 改参数 -> 重开」，保存即生效。
        切到干跑时只关串口、不再占 COM。返回 ``(ok, message)``。
        """
        with self._lock:
            new_port = self.port if port is None else (str(port).strip() or None)
            new_baud = self.baud if not baud else int(baud)
            new_dry = self.dry_run if dry_run is None else bool(dry_run)
            unchanged = (new_port == self.port and new_baud == self.baud
                         and new_dry == self.dry_run)
            # 参数没变且串口已就绪：不折腾（改别的设置项不该把继电器重连一遍）；
            # 参数没变但串口没开：当成"再试一次连接"。
            if unchanged and (new_dry or self._serial is not None):
                if new_dry:
                    return True, "参数未变（干跑，串口未占用）"
                return True, "参数未变，保持 %s @%d" % (self.port, self.baud)
            self.close()
            self.port = new_port
            self.baud = new_baud
            self.dry_run = new_dry
            self.last_error = None
            if new_dry:
                self.port_reason = "干跑模式（不碰硬件）"
                return True, "已切到干跑，串口已释放"
            ok, message = self.open()
            if not ok:
                return False, message
            return True, "已重连 %s @%d" % (self.port, self.baud)

    @property
    def is_open(self):
        """串口是否已打开。"""
        return self._serial is not None or self.dry_run

    @property
    def states(self):
        """推定状态快照（通道 -> bool）。"""
        with self._lock:
            return dict(self._states)

    # -- 动作 -------------------------------------------------------------
    def set_channel(self, channel, on, settle=True):
        """把单个通道写为目标状态；返回 ``(ok, message)``。

        ``settle=True`` 时写完后等待 :data:`STEP_DELAY_S`，让电源轨稳定
        （序列动作里逐通道调用时用）。
        """
        with self._lock:
            channel = int(channel)
            if channel not in (1, 2, 3, 4):
                return False, "通道号必须是 1..4"
            frame = build_frame(channel, on)
            if self.dry_run:
                self._states[channel] = bool(on)
                self._save_states()
                self.frames_sent += 1
                if settle:
                    time.sleep(STEP_DELAY_S)
                return True, "干跑: CH%d -> %s 帧 %s" % (
                    channel, "给电" if on else "断电", frame.hex(" ").upper())
            if self._serial is None:
                ok, message = self.open()
                if not ok:
                    return False, message
            try:
                self._serial.write(frame)
                self._serial.flush()
            except Exception as exc:
                self.last_error = "写 %s 失败: %s" % (self.port, exc)
                return False, self.last_error
            self._states[channel] = bool(on)
            self.frames_sent += 1
            self._save_states()
            if settle:
                time.sleep(STEP_DELAY_S)
            return True, "CH%d(%s) %s 帧 %s" % (
                channel, CHANNEL_NAMES.get(channel, "?"),
                "给电" if on else "断电", frame.hex(" ").upper())

    def set_sequence(self, steps):
        """按给定顺序执行 ``[(channel, on), ...]``，返回逐步结果列表。"""
        results = []
        for channel, on in steps:
            ok, message = self.set_channel(channel, on, settle=True)
            results.append({"channel": channel, "on": bool(on), "ok": ok, "message": message})
            if not ok:
                break
        return results

    def preset(self, name):
        """预设动作：``power_on`` / ``power_off`` / ``soc_off`` / ``all_off``。"""
        if name == "power_on":
            return self.set_sequence([(1, True), (2, True), (3, True)])
        if name == "power_off":
            return self.set_sequence([(3, False), (2, False), (1, False)])
        if name == "soc_off":
            return self.set_sequence([(3, False), (2, False)])
        if name == "all_off":
            return self.set_sequence([(4, False), (3, False), (2, False), (1, False)])
        return [{"channel": 0, "on": False, "ok": False, "message": "未知预设 %r" % (name,)}]

    def describe(self):
        """供界面显示的端口信息。"""
        return {
            "port": self.port or "",
            "baud": self.baud,
            "dry_run": self.dry_run,
            "open": self.is_open,
            "reason": self.port_reason,
            "frames": self.frames_sent,
            "error": self.last_error,
            "states": {str(k): v for k, v in self.states.items()},
        }
