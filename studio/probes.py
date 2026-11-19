# -*- coding: utf-8 -*-
"""probes.py — 抓取与判定：DUT 串口、adb 状态、CAN 接收、监看规则

三段结构：

1. :class:`SerialProbe` —— 读 DUT 调试串口（默认 COM4 @460800），按行切分并解析
   ``[时间][模块][级别]evt=CODE key=val`` 结构；原始行全量落盘 ``serial.log``，
   只有 WARN/ERROR 与命中关键字的行会即时进日志总线（其余按速率抽样并计丢弃数），
   避免每秒几百行把界面刷爆。
2. :class:`AdbProbe` —— 每秒采一次 SOC 在线状态 / uptime / mcu.log 体积，算出
   日志增长速度，用于"DUT 掉线""DUT 活着但不刷日志"两类判定。
3. :class:`RuleEngine` + :class:`ProbeHub` —— 用户可编辑的监看规则（正则命中、
   静默、CAN 报文超时、adb 掉线、日志停更），命中即触发 **incident**（现场保全）。

规则类型（``rules[].type``）::

    regex        串口行命中正则；window_ms 内命中 min_hits 次即触发
    silence      串口在 after_ms 内没有任何行 / 没有匹配 pattern 的行
    can_timeout  某总线某 CAN ID 在 after_ms 内没有收到
    adb_offline  SOC 掉线超过 after_ms
    log_stall    SOC 在线但 mcu.log after_ms 内没有增长

每条规则还有 ``severity``（warn/error）、``freeze``（是否现场保全）、
``stop``（是否停下剧本）、``cooldown_ms``（同一规则最短触发间隔）。
"""

import collections
import re
import threading
import time

try:
    import serial
    HAVE_PYSERIAL = True
except Exception:
    serial = None
    HAVE_PYSERIAL = False

from . import adb as adb_mod
from .eventlog import RateMeter

LINE_RE = re.compile(r"^\[(?P<ts>[^\]]*)\]\[(?P<module>[^\]]*)\]\[(?P<level>[A-Za-z]+)\](?P<rest>.*)$")
EVT_RE = re.compile(r"evt=([A-Za-z0-9_]+)")
LEVEL_MAP = {"debug": "debug", "info": "info", "warn": "warn", "warning": "warn",
             "error": "error", "err": "error", "fatal": "error"}

DEFAULT_INTEREST = ("SpiRx", "SpiDma", "BOOT", "FW_VERSION", "PROTO_INIT", "MAPENG_INIT",
                    "PWRMNG", "PROTO_LINK", "SOUND", "ALARM", "BUSOFF", "ERROR", "NACK")

DEFAULT_RULES = [
    {"id": "r_err", "name": "出现 ERROR 级日志", "probe": "serial", "type": "regex",
     "pattern": r"\[ERROR\]", "window_ms": 0, "min_hits": 1, "severity": "error",
     "freeze": True, "stop": True, "cooldown_ms": 5000, "enabled": True},
    {"id": "r_spirx_stall", "name": "SpiRx 停更 (SPI 链路断)", "probe": "spi",
     "type": "silence", "pattern": r"\[SpiRx\]", "after_ms": 3000, "severity": "error",
     "freeze": True, "stop": False, "cooldown_ms": 10000, "enabled": True},
    {"id": "r_adb_off", "name": "SOC 掉线", "probe": "adb", "type": "adb_offline",
     "after_ms": 3000, "severity": "error", "freeze": True, "stop": False,
     "cooldown_ms": 10000, "enabled": True},
    {"id": "r_log_stall", "name": "SOC 在线但 mcu.log 不增长", "probe": "adb",
     "type": "log_stall", "after_ms": 5000, "severity": "warn", "freeze": True,
     "stop": False, "cooldown_ms": 15000, "enabled": False},
]


class SerialProbe(object):
    """DUT 调试串口读取 + 解析 + 落盘。"""

    # 常见 USB-UART 桥的 VID：Prolific / FTDI / CP210x 多用于 MCU 调试口
    DUT_VIDS = ("067B", "0403", "10C4")

    def __init__(self, config, bus, on_line=None):
        self.port = str(config.get("port") or "").strip()
        self.port_reason = ""
        self.exclude_provider = None
        self.baud = int(config.get("baud") or 460800)
        self.enabled = bool(config.get("enabled", True))
        self.sample_rate = float(config.get("log_rate", 40.0))
        self.simulate = bool(config.get("simulate"))
        self.sim_online = None             # 模拟模式：返回 False 表示"整机断电"
        self._bus = bus
        self._on_line = on_line
        self._serial = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._file = None
        self._file_path = ""
        self.state = "closed"              # closed / opening / open / error
        self.message = ""
        self.last_error = ""
        self.lines = 0
        self.bytes = 0
        self.dropped = 0
        self.last_line_at = 0.0
        self.last_raw = ""
        self.rate = RateMeter(5.0)
        self.drop_rate = RateMeter(5.0)
        self._tail = collections.deque(maxlen=2000)
        self._retry_at = 0.0
        self._sample_budget = 0.0
        self._last_sample_at = time.time()
        self._saw_line = False

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        """启动读线程（串口打不开会后台重试）。"""
        if not self.enabled:
            self.state = "disabled"
            return
        if not self.simulate and not HAVE_PYSERIAL:
            self.state = "error"
            self.message = "未安装 pyserial"
            return
        if self.simulate:
            self.state = "open"
            self.message = "模拟数据源"
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="serial-probe")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        """停止读线程并关闭串口。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
        self._close_port()
        self.state = "closed"

    def attach_file(self, path):
        """切换原始行落盘文件。"""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            self._file_path = path or ""
            if path:
                self._file = open(path, "a", encoding="utf-8", buffering=1)

    def reconfigure(self, config):
        """更新端口/波特率（下一次重连生效）。"""
        port = str(config.get("port") or self.port)
        baud = int(config.get("baud") or self.baud)
        if port != self.port or baud != self.baud:
            self.port, self.baud = port, baud
            self._close_port()

    # -- 内部 -------------------------------------------------------------
    def _excluded_ports(self):
        """被其它用途占用的串口（继电器、adb 适配器等），自动识别时跳过。"""
        ports = set()
        provider = self.exclude_provider
        if provider:
            try:
                ports.update(str(p).upper() for p in (provider() or []) if p)
            except Exception:
                pass
        return ports

    def _detect_port(self):
        """自动挑 DUT 调试口：优先 Prolific/FTDI/CP210x，其次其它 USB 串口，最后 CH340。"""
        from .relay import list_serial_ports
        used = self._excluded_ports()
        ranked = []
        for item in list_serial_ports():
            if item["port"].upper() in used:
                continue
            hwid = (item["hwid"] + " " + item["desc"]).upper()
            if any(vid in hwid for vid in self.DUT_VIDS):
                score, why = 0, "自动识别 USB-UART(%s)" % item["hwid"].split("VID:PID=")[-1][:9]
            elif "BTHENUM" in hwid or "蓝牙" in item["desc"]:
                score, why = 9, "自动识别（蓝牙串口，最后才选）"
            elif "1A86" in hwid or "CH340" in hwid:
                score, why = 5, "自动识别 CH340（可能是继电器口）"
            else:
                score, why = 2, "自动识别 USB 串口"
            ranked.append((score, item["port"], why, item["desc"]))
        ranked.sort(key=lambda row: (row[0], row[1]))
        for _score, port, why, desc in ranked:
            try:
                handle = serial.Serial(port, self.baud, timeout=0.2)
                handle.close()
                return port, "%s %s" % (why, desc or "")
            except Exception:
                continue
        return None, "没有可用的调试串口（%d 个候选都打不开）" % len(ranked)

    def _open_port(self):
        """尝试打开串口；失败留给调用方重试。"""
        self.state = "opening"
        if not self.port:
            detected, reason = self._detect_port()
            if not detected:
                self.state = "error"
                self.last_error = reason
                self.message = reason
                return False
            self.port = detected
            self.port_reason = reason
            self._bus.info("serial", "自动识别 DUT 串口: %s（%s）" % (detected, reason))
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=0.2)
            self._serial.reset_input_buffer()
            self.state = "open"
            self.message = "%s @%d" % (self.port, self.baud)
            self.last_error = ""
            self._bus.info("serial", "串口已打开 %s @%d" % (self.port, self.baud))
            return True
        except Exception as exc:
            self._serial = None
            self.state = "error"
            self.last_error = str(exc)
            self.message = "打开 %s 失败: %s" % (self.port, exc)
            return False

    def _close_port(self):
        """关闭串口句柄。"""
        handle = self._serial
        self._serial = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if self.state == "open":
            self.state = "closed"

    def _loop(self):
        """读循环：打不开就退避重试，读到数据就切行解析。"""
        if self.simulate:
            self._sim_loop()
            return
        buffer = b""
        while not self._stop.is_set():
            if self._serial is None:
                now = time.time()
                if now < self._retry_at:
                    self._stop.wait(0.2)
                    continue
                if not self._open_port():
                    self._retry_at = now + 2.0
                    continue
            try:
                chunk = self._serial.read(4096)
            except Exception as exc:
                self.last_error = str(exc)
                self._close_port()
                self.state = "error"
                self.message = "串口读取异常，2s 后重连"
                self._bus.warn("serial", "串口读取异常: %s" % exc)
                self._retry_at = time.time() + 2.0
                continue
            if not chunk:
                continue
            buffer += chunk
            while True:
                cut = buffer.find(b"\n")
                if cut < 0:
                    break
                raw, buffer = buffer[:cut], buffer[cut + 1:]
                self._handle_raw(raw)
            if len(buffer) > 65536:        # 长时间没有换行：丢掉半截，避免吃内存
                buffer = b""

    def _sim_loop(self):
        """模拟数据源：按 ~100 行/秒产出结构化日志；"断电"时静默。

        产出的行与真实 DUT 的格式一致（``[时间][模块][级别]evt=...``），
        因此解析、规则、断言、日志落盘走的都是同一套路径。
        """
        tick = 0
        while not self._stop.is_set():
            online = True
            if self.sim_online is not None:
                try:
                    online = bool(self.sim_online())
                except Exception:
                    online = True
            if not online:
                self.state = "open"
                self.message = "模拟：整机断电，串口静默"
                self._stop.wait(0.2)
                continue
            self.state = "open"
            self.message = "模拟数据源"
            tick += 1
            stamp = time.strftime("%Y-%m-%d %H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000)
            lines = []
            if tick % 25 == 0:
                lines.append("[%s][SocCom][INFO][SpiRx] seq=%d ftype=0x01 ack=%d tlv=3"
                             % (stamp, tick, max(0, tick - 2)))
            if tick % 60 == 0:
                lines.append("[%s][SocCom][WARN]evt=W_SPICANSIG_UNREG_SID sid=0x0E "
                             "mid=0x9001 val=00 len=8" % stamp)
            if tick % 17 == 0:
                lines.append("[%s][MapEng][INFO]evt=I_MAPENG_TX sid=0x08 mid=0x9001 "
                             "len=2 val=01 00" % stamp)
            lines.append("[%s][SpiCanSig][INFO]evt=D_SPICANSIG_RX_TLV sid=0x05 "
                         "mid=0x1003 len=12" % stamp)
            lines.append("[%s][PowerMng][INFO]evt=I_PWR_STATE state=3 vin=27.1" % stamp)
            for line in lines:
                self._handle_raw(line.encode("utf-8"))
            self._stop.wait(0.02)

    def _handle_raw(self, raw):
        """一行原始字节 -> 解析 -> 落盘 -> 进总线/规则。"""
        text = raw.decode("utf-8", "replace").strip("\r\n\x00 ")
        if not text:
            return
        now = time.time()
        with self._lock:
            self.lines += 1
            self.bytes += len(raw) + 1
            self.last_line_at = now
            self.last_raw = text
        self.rate.hit()
        self._tail.append(text)
        handle = self._file
        if handle is not None:
            try:
                handle.write(text + "\n")
            except OSError:
                pass

        parsed = parse_line(text)
        parsed["t"] = now
        if self._on_line is not None:
            try:
                self._on_line(parsed)
            except Exception:
                pass
        self._emit_line(parsed)

    def _emit_line(self, parsed):
        """按级别与关键词决定是否进日志总线（其余抽样，超出计数）。"""
        level = parsed.get("level") or "info"
        evt = parsed.get("evt") or ""
        interesting = (level in ("warn", "error", "incident")
                       or any(key in evt for key in DEFAULT_INTEREST))
        if not interesting:
            now = time.time()
            elapsed = max(0.001, now - self._last_sample_at)
            self._last_sample_at = now
            self._sample_budget = min(self.sample_rate,
                                      self._sample_budget + elapsed * self.sample_rate)
            if self._sample_budget < 1.0:
                with self._lock:
                    self.dropped += 1
                self.drop_rate.hit()
                return
            self._sample_budget -= 1.0
        module = parsed.get("module") or "DUT"
        self._bus.emit(level, "serial", parsed.get("text", ""),
                       module=module, evt=evt, raw=parsed.get("raw", ""))

    def tail(self, count=200):
        """最近若干原始行。"""
        return list(self._tail)[-count:]

    def write_text(self, text):
        """往原始日志里追加一行非串口来源的文本（人工标记用）。"""
        handle = self._file
        if handle is None:
            return False
        try:
            handle.write(text + "\n")
            return True
        except OSError:
            return False

    def snapshot(self):
        """界面用串口状态。"""
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "port": self.port,
                "port_reason": self.port_reason,
                "baud": self.baud,
                "lines": self.lines,
                "bytes": self.bytes,
                "fps": round(self.rate.rate(), 1),
                "dropped": self.dropped,
                "drop_fps": round(self.drop_rate.rate(), 1),
                "last_line_ms": int(self.last_line_at * 1000) if self.last_line_at else 0,
                "last_raw": self.last_raw,
                "error": self.last_error,
            }


def parse_line(text):
    """解析一行 MCU 日志；解析不出结构化字段时整行当 text。"""
    result = {"raw": text, "level": "info", "module": "", "evt": "", "text": text}
    match = LINE_RE.match(text)
    rest = text
    if match:
        rest = match.group("rest").strip()
        result["module"] = match.group("module").strip()
        result["level"] = LEVEL_MAP.get(match.group("level").strip().lower(), "info")
        result["text"] = rest or text
    evt = EVT_RE.search(rest)
    if evt:
        result["evt"] = evt.group(1)
    return result


class AdbProbe(object):
    """周期性采样 SOC 状态（1s 一次）；维护在线/日志增长状态机。"""

    def __init__(self, config, bus):
        self.exe = adb_mod.find_adb(config.get("exe"))
        self.log_path = config.get("log_path") or adb_mod.DEFAULT_LOG_PATH
        self.interval = max(0.3, float(config.get("interval_ms", 1000)) / 1000.0)
        self.enabled = bool(config.get("enabled", True))
        self.simulate = bool(config.get("simulate"))
        self.sim_online = None             # 模拟模式：None 视为在线
        self.runner = adb_mod.AdbRunner(self.exe, log=bus)
        self._bus = bus
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.online = False
        self.ever_online = False
        self.uptime = None
        self.log_size = None
        self.growth = 0.0                  # 字节/秒
        self.last_sample = 0.0
        self.last_online_at = 0.0
        self.offline_since = 0.0
        self.last_size_at = 0.0
        self.last_size_value = None
        self.samples = 0
        self.error = ""
        self._sim_uptime = 12.0
        self._sim_size = 180000.0

    def start(self):
        """启动采样线程。"""
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="adb-probe")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        """停止采样线程。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _loop(self):
        """采样循环。"""
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as exc:
                self.error = str(exc)
            self._stop.wait(self.interval)

    def sample(self):
        """采一次并更新状态机。"""
        if self.simulate:
            data = self._sim_data()
        else:
            data = self.runner.snapshot(self.log_path)
        now = time.time()
        with self._lock:
            was_online = self.online
            self.samples += 1
            self.last_sample = now
            self.online = bool(data.get("online"))
            self.uptime = data.get("uptime")
            self.log_size = data.get("log_size")
            self.error = data.get("error") or ""
            if self.online:
                self.ever_online = True
                self.last_online_at = now
                self.offline_since = 0.0
                if self.log_size is not None:
                    if self.last_size_value is not None and self.last_size_at:
                        span = max(0.2, now - self.last_size_at)
                        delta = self.log_size - self.last_size_value
                        if delta >= 0:
                            instant = delta / span
                            self.growth = instant if self.growth == 0 else (
                                self.growth * 0.6 + instant * 0.4)
                        else:
                            self.growth = 0.0           # 日志轮转
                    self.last_size_value = self.log_size
                    self.last_size_at = now
            else:
                if was_online:
                    self.offline_since = now
                elif not self.offline_since and not self.ever_online:
                    self.offline_since = now
                self.growth = 0.0
        if self.online and not was_online:
            self._bus.info("adb", "SOC 在线%s" % (
                "，uptime=%.0fs" % self.uptime if self.uptime is not None else ""))
        elif was_online and not self.online:
            self._bus.warn("adb", "SOC 掉线")
        return data

    def snapshot(self):
        """界面用 adb 状态。"""
        with self._lock:
            return {
                "state": ("online" if self.online
                          else ("offline" if self.ever_online else "unknown")),
                "exe": self.runner.exe_path,
                "online": self.online,
                "ever_online": self.ever_online,
                "uptime_s": round(self.uptime, 1) if self.uptime is not None else None,
                "log_size": self.log_size,
                "growth": round(self.growth, 1),
                "last_online_ms": int(self.last_online_at * 1000) if self.last_online_at else 0,
                "offline_ms": int((time.time() - self.offline_since) * 1000)
                if (self.offline_since and not self.online) else 0,
                "error": self.error,
                "samples": self.samples,
            }

    def _sim_data(self):
        """模拟 DUT：整机断电则离线；在线时 uptime 与日志体积按真实速率增长。"""
        online = True
        if self.sim_online is not None:
            try:
                online = bool(self.sim_online())
            except Exception:
                online = True
        if online:
            self._sim_uptime += self.interval
            self._sim_size += 160000.0 * self.interval
        return {
            "available": True,
            "online": online,
            "devices": ["SIM0000000"] if online else [],
            "uptime": round(self._sim_uptime, 2) if online else None,
            "log_size": int(self._sim_size) if online else None,
            "error": "",
        }


class RuleEngine(object):
    """监看规则：串口正则/静默、CAN 超时、adb 掉线、日志停更。"""

    def __init__(self, bus, boot_grace_ms=25000):
        self._bus = bus
        self._lock = threading.RLock()
        self.rules = [dict(rule) for rule in DEFAULT_RULES]
        self._hits = {}                    # rule_id -> deque[时间戳]
        self._last_fire = {}               # rule_id -> 时间戳
        self._last_match = {}              # rule_id -> 最后一次命中时间
        self._compiled = {}
        self.on_incident = None
        self.muted = False
        # 运行上下文：主电是否在、SOC 是否"应该在线"。
        # 剧本主动断电/断 IG 期间，对应的探针类规则不该判成事故。
        self.context = {"power": True, "soc": True}
        self._armed_at = time.time()
        # 上电/上 IG 之后 DUT 要启动：这段时间内"没 adb/没日志"是正常的，不判事故。
        self.boot_grace_ms = int(boot_grace_ms or 0)
        self._grace_until = 0.0
        # 持续性故障（静默/掉线/超时/停更）只在"刚坏"时报一次，
        # 恢复正常后才重新武装；避免同一个持续故障每过一个冷却就报一条。
        self._latched = {}
        self._compile()

    def set_context(self, power_on, soc_on, reason=""):
        """更新"当前应该有什么"的上下文（剧本切通道后调用）。"""
        power_on, soc_on = bool(power_on), bool(soc_on)
        previous = dict(self.context)
        changed = (power_on != previous["power"]) or (soc_on != previous["soc"])
        self.context = {"power": power_on, "soc": soc_on}
        if not changed:
            return
        now = time.time()
        # 从"没有电"到"有电"、或从"SOC 该离线"到"该在线"：给启动留窗口
        powered_up = power_on and not previous["power"]
        soc_woke = soc_on and not previous["soc"]
        if self.boot_grace_ms > 0 and (powered_up or soc_woke):
            self._grace_until = now + self.boot_grace_ms / 1000.0
            self._bus.info("rule", "启动窗口：%ds 内不判掉线/静默" % (self.boot_grace_ms // 1000))
        if not power_on:
            self._grace_until = 0.0
        if power_on or soc_on:
            # 重新武装：给探针一个采样/启动窗口，别拿"刚才断着"的旧状态立刻判死
            self._armed_at = now
        if not power_on or not soc_on:
            detail = "主电断" if not power_on else "IG 断（SOC 预期离线）"
            self._bus.info("rule", "监看降级：%s%s" % (detail, "（%s）" % reason if reason else ""))
        else:
            self._bus.info("rule", "监看恢复：主电与 SOC 均在线")
        # 恢复时给静默类规则一个观察窗口
        if power_on:
            for rule in self.rules:
                if rule.get("type") == "silence":
                    self._last_match[rule.get("id")] = now

    def _active(self, rule):
        """按探针类型判断该规则此刻是否有意义。

        ``spi`` 表示"串口里的 SPI/SOC 链路信号"（如 ``[SpiRx]``）：它只在 SOC
        上电后才该出现，所以按 SOC 上下文门控，而不是按主电。
        """
        if not rule.get("enabled", True):
            return False
        probe = rule.get("probe") or "serial"
        if probe == "spi":
            return self.context["soc"]
        if probe == "serial":
            return self.context["power"]
        if probe == "adb":
            return self.context["soc"]
        return True

    def set_muted(self, muted, reason=""):
        """整体静默/恢复监看（手动用）。"""
        muted = bool(muted)
        if muted == self.muted:
            return
        self.muted = muted
        self._bus.info("rule", "监看已%s%s" % (
            "静默" if muted else "恢复", "（%s）" % reason if reason else ""))

    def set_rules(self, rules):
        """整体替换规则集并重新编译正则。"""
        with self._lock:
            self.rules = [dict(rule) for rule in (rules or [])]
            self._hits.clear()
            self._last_fire.clear()
            self._last_match.clear()
            self._compile()

    def _compile(self):
        """预编译各规则的正则。"""
        self._compiled = {}
        for rule in self.rules:
            if rule.get("type") not in ("regex", "silence"):
                continue
            pattern = rule.get("pattern") or ""
            try:
                self._compiled[rule.get("id")] = re.compile(pattern) if pattern else None
            except re.error as exc:
                self._bus.error("rule", "规则 %s 正则无效: %s" % (rule.get("name"), exc))
                self._compiled[rule.get("id")] = None

    # -- 事件入口 ---------------------------------------------------------
    def feed_line(self, parsed):
        """串口每行调用一次（正则命中计数 + 静默规则的时间戳维护）。"""
        now = parsed.get("t") or time.time()
        text = parsed.get("raw") or parsed.get("text") or ""
        if self.muted:
            return
        with self._lock:
            rules = list(self.rules)
            compiled = dict(self._compiled)
        for rule in rules:
            if not self._active(rule):
                continue
            kind = rule.get("type")
            if kind not in ("regex", "silence"):
                continue
            regex = compiled.get(rule.get("id"))
            if regex is None:
                continue
            if not regex.search(text):
                continue
            self._last_match[rule.get("id")] = now
            if kind == "regex":
                self._note_hit(rule, now)

    def _note_hit(self, rule, now):
        """累计命中次数，达到阈值就触发。"""
        rule_id = rule.get("id")
        bucket = self._hits.setdefault(rule_id, collections.deque(maxlen=200))
        bucket.append(now)
        window = float(rule.get("window_ms") or 0) / 1000.0
        if window > 0:
            while bucket and now - bucket[0] > window:
                bucket.popleft()
        need = max(1, int(rule.get("min_hits") or 1))
        if len(bucket) < need:
            return
        self._fire(rule, now, "命中 %d 次" % len(bucket))

    def _fire_condition(self, rule, now, detail):
        """持续性故障：首次命中报一次，恢复前不重复报。"""
        rule_id = rule.get("id")
        if self._latched.get(rule_id):
            return
        self._latched[rule_id] = True
        self._fire(rule, now, detail)

    def _recovered(self, rule):
        """条件恢复正常：解除锁存，下次再坏可以再报。"""
        rule_id = rule.get("id")
        if rule_id in self._latched:
            self._latched.pop(rule_id, None)
            self._bus.info("rule", "监看恢复：%s" % (rule.get("name") or rule_id))

    def _fire(self, rule, now, detail):
        """按冷却时间触发 incident。"""
        rule_id = rule.get("id")
        cooldown = float(rule.get("cooldown_ms") or 5000) / 1000.0
        if now - self._last_fire.get(rule_id, 0.0) < cooldown:
            return
        self._last_fire[rule_id] = now
        self._hits[rule_id] = collections.deque()
        if self.on_incident is None:
            return
        self.on_incident({
            "rule_id": rule_id,
            "rule": rule.get("name") or rule_id,
            "probe": rule.get("probe", ""),
            "severity": rule.get("severity", "warn"),
            "freeze": bool(rule.get("freeze", True)),
            "stop": bool(rule.get("stop", False)),
            "detail": detail,
            "pattern": rule.get("pattern", ""),
        })

    # -- 周期判定 ---------------------------------------------------------
    def tick(self, serial_probe, adb_probe, can, state_name=""):
        """静默 / CAN 超时 / adb 掉线 / 日志停更 四类周期判定。"""
        if self.muted:
            return
        now = time.time()
        grace = now < self._grace_until
        with self._lock:
            rules = list(self.rules)
        for rule in rules:
            if not self._active(rule):
                continue
            kind = rule.get("type")
            after_ms = float(rule.get("after_ms") or 3000)
            if grace and kind in ("silence", "adb_offline", "log_stall"):
                continue                      # DUT 正在启动，这几种"没东西"是预期的
            if kind == "silence" and serial_probe is not None:
                if serial_probe.state != "open" or not serial_probe.last_line_at:
                    continue
                pattern = rule.get("pattern") or ""
                rule_id = rule.get("id")
                last = self._last_match.get(rule_id)
                if last is None:
                    # 规则刚生效：从当前时刻起算，给一个观察窗口再判静默
                    self._last_match[rule_id] = now
                    continue
                if not pattern:
                    last = serial_probe.last_line_at
                if last and (now - last) * 1000.0 >= after_ms:
                    self._fire_condition(rule, now, "%.1fs 无匹配" % (now - last))
                else:
                    self._recovered(rule)
            elif kind == "can_timeout" and can is not None:
                bus_name = rule.get("bus") or ""
                can_id = str(rule.get("can_id") or "").lower()
                ids = can.rx_ids(bus_name)
                entry = None
                for key, value in ids.items():
                    if key.lower() == can_id:
                        entry = value
                        break
                if entry is None:
                    continue                       # 还没见过这个 ID：不做超时判定
                age = entry.get("age_ms") or 0
                if age >= after_ms:
                    self._fire_condition(rule, now, "%s 已 %.0fms 未收到" % (can_id, age))
                else:
                    self._recovered(rule)
            elif kind == "adb_offline" and adb_probe is not None:
                if not adb_probe.ever_online or adb_probe.online:
                    self._recovered(rule)
                    continue
                if (now - self._armed_at) * 1000.0 < after_ms:
                    continue
                offline_ms = (now - adb_probe.offline_since) * 1000.0 if adb_probe.offline_since else 0
                if adb_probe.offline_since and offline_ms >= after_ms:
                    self._fire_condition(rule, now, "SOC 已离线 %.1fs" % (offline_ms / 1000.0))
            elif kind == "log_stall" and adb_probe is not None:
                if not adb_probe.online or not adb_probe.last_size_at:
                    self._recovered(rule)
                    continue
                if (now - self._armed_at) * 1000.0 < after_ms:
                    continue
                idle = (now - adb_probe.last_size_at) * 1000.0
                if idle >= after_ms:
                    self._fire_condition(rule, now, "mcu.log 已 %.1fs 无增长" % (idle / 1000.0))
                else:
                    self._recovered(rule)


class ProbeHub(object):
    """串口 + adb + 规则的总装，向界面/运行器提供统一快照。"""

    def __init__(self, config, bus, can=None, simulate=False,
                 serial_online=None, adb_online=None):
        self._bus = bus
        self._can = can
        self.config = dict(config or {})
        self.simulate = bool(simulate)
        serial_cfg = dict(self.config.get("serial") or {})
        adb_cfg = dict(self.config.get("adb") or {})
        if self.simulate:
            serial_cfg["simulate"] = True
            adb_cfg["simulate"] = True
        self.serial = SerialProbe(serial_cfg, bus, on_line=self._on_serial_line)
        self.adb = AdbProbe(adb_cfg, bus)
        if self.simulate:
            self.serial.sim_online = serial_online
            self.adb.sim_online = adb_online
        self.rules = RuleEngine(bus, self.config.get("boot_grace_ms", 25000))
        self._thread = None
        self._stop = threading.Event()
        self.on_incident = None
        self._state_getter = None
        self.recent = collections.deque(maxlen=6000)
        # 电源状态机（从 MCU 日志解析）：state 0=关机 1=待机 2=监听 3=预开机 4=开机
        self.pwr = {"state": None, "allowed": None, "acc": None, "ig": None,
                    "vbat": None, "at": 0.0, "history": collections.deque(maxlen=500)}
        self.rules.on_incident = self._incident

    # -- 装配 -------------------------------------------------------------
    def attach_can(self, can):
        """挂上 CAN 主管（规则要用它读接收统计）。"""
        self._can = can

    def set_state_getter(self, getter):
        """运行器注入"当前状态名"取值函数，日志/事件带上状态。"""
        self._state_getter = getter

    def set_rules(self, rules):
        """替换规则集。"""
        self.rules.set_rules(rules)

    def start(self, run_dir=None):
        """启动串口/adb 探针与规则 tick 线程；``run_dir`` 给出则落盘。"""
        if run_dir:
            import os
            self.serial.attach_file(os.path.join(run_dir, "serial.log"))
        self.serial.start()
        self.adb.start()
        self._stop.clear()
        if self._thread is None:
            self._thread = threading.Thread(target=self._tick_loop, name="probe-tick")
            self._thread.daemon = True
            self._thread.start()

    def stop(self, keep_serial=False):
        """停止探针；``keep_serial=True`` 时只切落盘文件不关串口。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        if not keep_serial:
            self.serial.stop()
        self.adb.stop()

    def restart_serial(self):
        """重开串口（改端口/波特率后调用）。"""
        self.serial.stop()
        self.serial.state = "closed"
        self.serial.start()

    def _tick_loop(self):
        """每 250ms 跑一次周期判定。"""
        counter = 0
        while not self._stop.is_set():
            try:
                self.rules.tick(self.serial, self.adb, self._can)
                counter += 1
                # 引擎自己 500ms 推一次接收快照，这里只每 2s 补一次强制刷新，
                # 避免把速率统计的窗口切碎（切碎后 fps 会显示成 0）
                if self._can is not None and counter % 8 == 0:
                    self._can.request_stats()
            except Exception as exc:
                self._bus.debug("probe", "规则 tick 异常: %s" % exc)
            self._stop.wait(0.25)

    def _on_serial_line(self, parsed):
        """串口行 -> 环形缓存（断言窗口用）+ 电源状态机跟踪 + 规则。"""
        self.recent.append(parsed)
        self._track_power(parsed)
        self.rules.feed_line(parsed)

    def _track_power(self, parsed):
        """从 ``I_PWRMNG_IO`` / ``I_PWRMNG_CAN_TX`` 解析电源状态。"""
        evt = parsed.get("evt") or ""
        if evt not in ("I_PWRMNG_IO", "I_PWRMNG_CAN_TX"):
            return
        raw = parsed.get("raw") or ""
        now = parsed.get("t") or time.time()
        if evt == "I_PWRMNG_IO":
            match = re.search(r"state=(\d+)", raw)
            if not match:
                return
            state = int(match.group(1))
            self.pwr["at"] = now
            for key in ("acc", "ig1", "vbat"):
                found = re.search(r"\b%s=([\d.]+)" % re.escape(key), raw)
                if found:
                    self.pwr["acc" if key == "acc" else ("ig" if key == "ig1" else "vbat")] = found.group(1)
            if state == self.pwr["state"]:
                return
            self.pwr["state"] = state
            self.pwr["history"].append({"t": now, "state": state})
            from . import stimulus
            self._bus.info("pwr", "电源状态 → %s(%d)" % (stimulus.state_name(state), state))
        else:
            match = re.search(r"allowed=(\d+)", raw)
            if match:
                allowed = int(match.group(1))
                if allowed != self.pwr["allowed"]:
                    self.pwr["allowed"] = allowed
                    self._bus.info("pwr", "CAN 外发闸门 %s" % ("放行" if allowed else "关闭"))

    def pwr_state(self):
        """当前 PowerMng 状态号（还没解析到时返回 ``None``）。"""
        return self.pwr.get("state")

    def pwr_seen(self, since, state):
        """``since`` 之后是否到达过指定状态。"""
        try:
            wanted = int(state)
        except (TypeError, ValueError):
            return False
        return any(item["state"] == wanted and item["t"] >= since
                   for item in list(self.pwr["history"]))

    def recent_since(self, since):
        """取 ``since``（time.time 秒）之后的解析行，供状态卡断言使用。"""
        return [item for item in self.recent if (item.get("t") or 0) >= since]

    def _incident(self, payload):
        """规则触发 -> 交给上层（运行器）。"""
        payload["state"] = self._state_getter() if self._state_getter else ""
        if self.on_incident is not None:
            self.on_incident(payload)

    # -- 快照 -------------------------------------------------------------
    def snapshot(self):
        """界面用探针快照。"""
        from . import stimulus
        pwr = {key: value for key, value in self.pwr.items() if key != "history"}
        pwr["name"] = stimulus.state_name(pwr.get("state"))
        pwr["history"] = list(self.pwr["history"])[-40:]
        return {
            "serial": self.serial.snapshot(),
            "adb": self.adb.snapshot(),
            "rules": [dict(rule) for rule in self.rules.rules],
            "pwr": pwr,
        }

    def mark(self, text):
        """在日志/串口留一个人工标记（现场复现时对时间轴）。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._bus.action("mark", text)
        self.serial.write_text("===== MARK %s %s =====" % (stamp, text))
        return True
