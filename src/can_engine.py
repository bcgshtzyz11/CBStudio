# -*- coding: utf-8 -*-
"""can_engine.py — CAN 收发引擎（**32 位 Python 3.8** 子进程，由工作台拉起）

为什么单独一个进程：本机 ZLG SDK 只提供 32 位 ``zlgcan.dll``，而工作台主进程是
64 位 Python（要跑 pyserial / openpyxl）。于是把"碰 CAN 硬件"的部分关进这个子进程，
父子之间用**行分隔 JSON** 通信（stdin 下命令、stdout 上事件），互不阻塞。

进程内三件事：

1. **打开设备与通道**：``open`` 命令按 ``channels`` 逐个 InitCAN/StartCAN；
2. **接收聚合**：每路一个读线程，把收到的帧聚合成"每 ID 计数 / 周期 / 最新数据"，
   每 ``stats_ms`` 上报一次快照（不逐帧上报，避免刷爆管道与浏览器）；可选把逐帧
   记录写 CSV（``rx_log``）；
3. **周期随机发送**：``tx_load`` 载入 ``can_matrix_manifest.json`` 的报文清单，
   ``tx_start`` 后按矩阵周期持续发送、每帧信号在矩阵原始值域内随机；``bus_tx``
   可按总线暂停/恢复，剧本里的"断开发送"就走这条。

协议（父子约定，均为单行 JSON）::

    子 <- {"cmd":"open","device_type":41,"channels":[...],"sdk_dir":"..."}
    子 <- {"cmd":"tx_load","manifest":"...","buses":["ican"],"seed":7,"rand_mode":"per-frame"}
    子 <- {"cmd":"tx_start"} / {"cmd":"tx_stop"} / {"cmd":"bus_tx","bus":"ican","enable":true}
    子 <- {"cmd":"rx_log","path":"...","enable":true}
    子 <- {"cmd":"stats"} / {"cmd":"ping"} / {"cmd":"quit"}

    子 -> {"ev":"hello","py":"3.8.10","sdk":true}
    子 -> {"ev":"ack","cmd":"open","ok":true,"message":"..."}
    子 -> {"ev":"rx","t":..,"buses":{...}}
    子 -> {"ev":"tx","t":..,"fps":..,"sent":..,"errors":..,"buses":{...}}
    子 -> {"ev":"log","level":"info","text":"..."}

本文件只用标准库 + 同级 ``can_pack``，不 import ``studio`` 包，保证 3.8-32 直接可跑。
"""

from __future__ import print_function

import glob
import json
import os
import random
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _path in (HERE, PARENT):          # HERE 用于 import can_pack，PARENT 兼容老结构
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 父子进程统一 UTF-8：Windows 管道默认用 OEM 码页（cp936），中文会变成乱码
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from can_pack import FrameBuilder          # noqa: E402

DEFAULT_SDK_DIR = r"F:/zlgcan_python_260529"
EXT_FLAG = 0x80000000
ID_MASK = 0x1FFFFFFF
MAX_DLC = 8
STATS_MS_DEFAULT = 500
MAX_RX_IDS = 400

# ZLG 设备类型号中属于 USBCANFD 家族的：波特率键名与初始化类型都按 CANFD 走
CANFD_DEVICE_TYPES = {41, 42, 43, 59, 76, 77, 78, 79, 80, 81, 85, 86, 87}

# 自动识别时依次尝试的设备类型：先 CANFD 家族，再经典 CAN 盒子
AUTO_DEVICE_TYPES = (41, 42, 43, 76, 77, 78, 79, 80, 81, 85, 86, 87, 59,
                     4, 3, 1)


def _arg_value(flag):
    """从 argv 里取 ``--flag value``（不引入 argparse，避免干扰主循环）。"""
    argv = list(sys.argv[1:])
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return None


SDK_DIR_HINT = _arg_value("--sdk-dir") or os.environ.get("CBSTUDIO_SDK_DIR") or ""


def _project_sdk_dirs():
    """随包放在项目里的 SDK 目录（``sdk/zlgcan`` 等），换机器时不用另装。"""
    patterns = [
        os.path.join(HERE, "sdk", "zlgcan"),
        os.path.join(HERE, "sdk"),
        os.path.join(HERE, "zlgcan"),
        os.path.join(HERE, "sdk", "*"),
        os.path.join(os.path.dirname(HERE), "sdk", "zlgcan"),
    ]
    found = []
    seen = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not os.path.isdir(path):
                continue
            seen.add(key)
            found.append(path)
    return found


def find_sdk_dir(explicit=None):
    """定位 zlgcan SDK 目录。

    优先级：显式配置 > 环境变量 > 项目内随包的 ``sdk/zlgcan`` > 默认目录 >
    各盘符下的 ``zlgcan*`` 目录。
    """
    candidates = [explicit, os.environ.get("CBSTUDIO_SDK_DIR"), SDK_DIR_HINT]
    candidates += _project_sdk_dirs()
    candidates.append(DEFAULT_SDK_DIR)
    for root in ("C:\\", "D:\\", "E:\\", "F:\\", os.path.expanduser("~")):
        try:
            for name in os.listdir(root):
                if "zlgcan" in name.lower():
                    candidates.append(os.path.join(root, name))
        except OSError:
            continue
    for path in candidates:
        if not path:
            continue
        text = str(path)
        if os.path.isdir(text) and os.path.isfile(os.path.join(text, "zlgcan.dll")):
            return text
    for path in candidates:
        if path and os.path.isdir(str(path)):
            return str(path)
    return ""


def load_sdk(sdk_dir=None):
    """优先从 SDK 目录导入 ``zlgcan``，压过 site-packages 里的同名包。

    本机 32 位环境里另装了一个 ``zlgcan`` 包（只有 8KB，不导出 ``ZCAN``），
    若按默认导入顺序会先命中它。这里把 SDK 目录插到 ``sys.path`` 最前面，并清掉
    已缓存模块后再导入。
    """
    resolved = find_sdk_dir(sdk_dir)
    candidates = [resolved] if resolved and os.path.isdir(resolved) else []
    for path in candidates:
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "zlgcan" or name.startswith("zlgcan."):
            sys.modules.pop(name, None)
    import zlgcan
    return zlgcan


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

_OUT_LOCK = threading.Lock()


def emit(payload):
    """向父进程写一行 JSON（线程安全、立即 flush）。"""
    line = json.dumps(payload, ensure_ascii=False)
    with _OUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(text, level="info"):
    """发一条日志事件。"""
    emit({"ev": "log", "level": level, "text": str(text)})


# ---------------------------------------------------------------------------
# 接收聚合
# ---------------------------------------------------------------------------

class RxAggregator(object):
    """按 ``(bus, can_id)`` 聚合接收统计，快照时一并给出窗口计数与周期估计。"""

    def __init__(self, stats_ms, csv_path=None):
        self._lock = threading.Lock()
        self._stats_ms = int(stats_ms)
        self._last_snapshot = time.time()
        self._buses = {}                 # bus -> id -> 统计
        self._bus_frames = {}            # bus -> 累计帧数
        self._window = {}                # bus -> 窗口帧数
        self._csv = None
        if csv_path:
            try:
                os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                self._csv = open(csv_path, "a", encoding="utf-8", buffering=1)
                self._csv.write("t_ms,bus,can_id,ext,dlc,data\n")
            except OSError as exc:
                log("接收 CSV 打开失败: %s" % exc, "warn")

    def close(self):
        """关闭 CSV 文件。"""
        if self._csv is not None:
            try:
                self._csv.close()
            except OSError:
                pass
            self._csv = None

    def feed(self, bus, can_id, dlc, data, stamp):
        """记录一帧。"""
        ext = bool(can_id & EXT_FLAG)
        cid = can_id & ID_MASK
        with self._lock:
            table = self._buses.setdefault(bus, {})
            slot = table.get(cid)
            if slot is None:
                if len(table) >= MAX_RX_IDS:
                    return
                slot = {"n": 0, "win": 0, "last": 0.0, "period": 0.0, "dlc": dlc,
                        "data": "", "first": stamp}
                table[cid] = slot
            delta = stamp - slot["last"] if slot["last"] else 0.0
            if delta > 0 and slot["period"]:
                slot["period"] = slot["period"] * 0.8 + delta * 0.2
            elif delta > 0:
                slot["period"] = delta
            slot["n"] += 1
            slot["win"] += 1
            slot["last"] = stamp
            slot["dlc"] = dlc
            slot["data"] = data
            self._bus_frames[bus] = self._bus_frames.get(bus, 0) + 1
            self._window[bus] = self._window.get(bus, 0) + 1
            csv = self._csv
        if csv is not None:
            try:
                csv.write("%d,%s,0x%08X,%d,%d,%s\n"
                          % (int(stamp * 1000), bus, cid, 1 if ext else 0, dlc, data))
            except OSError:
                pass

    def snapshot(self, force=False):
        """到期返回快照（``{"buses": {...}, "window_ms": ...}``），否则 ``None``。"""
        now = time.time()
        if not force and (now - self._last_snapshot) * 1000.0 < self._stats_ms:
            return None
        with self._lock:
            window_ms = int((now - self._last_snapshot) * 1000.0) or 1
            self._last_snapshot = now
            buses = {}
            for bus, table in self._buses.items():
                ids = {}
                window_frames = 0
                for cid, slot in table.items():
                    window_frames += slot["win"]
                    ids["0x%08X" % cid] = {
                        "n": slot["n"],
                        "win": slot["win"],
                        "age_ms": int((now - slot["last"]) * 1000.0),
                        "period_ms": round(slot["period"] * 1000.0, 1) if slot["period"] else 0.0,
                        "dlc": slot["dlc"],
                        "data": slot["data"],
                    }
                    slot["win"] = 0
                buses[bus] = {"frames": self._bus_frames.get(bus, 0),
                              "fps": round(window_frames * 1000.0 / window_ms, 1),
                              "ids": ids}
                self._window[bus] = 0
        return {"ev": "rx", "t": int(now * 1000), "window_ms": window_ms, "buses": buses}


# ---------------------------------------------------------------------------
# 周期发送
# ---------------------------------------------------------------------------

class TxJob(object):
    """清单里的一条周期报文 + 调度状态。"""

    __slots__ = ("name", "bus", "can_id", "dlc", "signals", "period", "next_due",
                 "sent", "payload", "refresh_at")

    def __init__(self, message, period, start_at):
        self.name = message["name"]
        self.bus = message["bus"]
        self.can_id = int(message["id"])
        self.dlc = int(message["dlc"])
        self.signals = message.get("signals") or []
        self.period = period
        self.next_due = start_at + period
        self.sent = 0
        self.payload = None
        self.refresh_at = 0.0

    def build(self, rng, pins):
        """按随机策略生成一帧载荷。"""
        builder = FrameBuilder(self.dlc)
        for signal in self.signals:
            raw = pins.get(signal["name"])
            if raw is None:
                raw = rng.randint(signal["raw_min"], signal["raw_max"])
            builder.set_raw(signal, raw)
        return builder.to_bytes()


class Engine(object):
    """设备/通道/发送/接收的总装。"""

    def __init__(self):
        self.z = None
        self.device = None
        self.sdk_dir = DEFAULT_SDK_DIR
        self.channels = {}               # bus -> {"ch", "baud", "handle", "tx": bool}
        self._watch = set()              # 要留存原始载荷的 CAN ID（DBC 解码用）
        self._watch_all = False
        self._last_payload = {}          # bus -> {id: {"d": "..", "t": ms, "ext": bool}}
        self.stats_ms = STATS_MS_DEFAULT
        self.rx = None
        self._rx_thread = None
        self._stop = threading.Event()
        self._tx_thread = None
        self._tx_lock = threading.Lock()
        self._jobs = []
        self._pins = {}
        self._rng = random.Random(1)
        self._rand_mode = "per-frame"
        self._rand_interval = 1.0
        self._tx_running = False
        self._tx_sent = 0
        self._tx_errors = 0
        self._tx_window = 0
        self._tx_last_snapshot = time.time()
        self._zlg = None
        self.device_info = {}
        # 电源状态机激励：按名字登记的周期帧流（与矩阵随机发送相互独立）
        self._streams = {}
        self._stream_lock = threading.Lock()
        self._stim_thread = None

    # -- 打开 -------------------------------------------------------------
    def open(self, device_type, channels, sdk_dir, stats_ms=STATS_MS_DEFAULT):
        """打开设备并初始化各通道。"""
        self.close()
        self.sdk_dir = find_sdk_dir(sdk_dir) or DEFAULT_SDK_DIR
        self.stats_ms = int(stats_ms or STATS_MS_DEFAULT)
        if not os.path.isdir(self.sdk_dir):
            raise RuntimeError("zlgcan SDK 目录不存在: %s" % self.sdk_dir)
        os.chdir(self.sdk_dir)
        module = load_sdk(self.sdk_dir)
        ZCAN = module.ZCAN
        ZCAN_TYPE_CAN = module.ZCAN_TYPE_CAN
        ZCAN_CHANNEL_INIT_CONFIG = module.ZCAN_CHANNEL_INIT_CONFIG
        ZCAN_STATUS_OK = module.ZCAN_STATUS_OK
        INVALID_DEVICE_HANDLE = module.INVALID_DEVICE_HANDLE
        self._zlg = (ZCAN, ZCAN_TYPE_CAN, ZCAN_CHANNEL_INIT_CONFIG,
                     ZCAN_STATUS_OK, INVALID_DEVICE_HANDLE)
        self._zlg_fd = module.ZCAN_TYPE_CANFD
        self._transmit_type = module.ZCAN_Transmit_Data
        self.z = ZCAN()
        try:
            wanted = int(device_type or 0)
        except (TypeError, ValueError):
            wanted = 0
        self.detected_device_type = wanted
        if wanted <= 0:
            # 自动识别：逐个型号试探，第一个能打开的就是当前设备
            self.device = INVALID_DEVICE_HANDLE
            for candidate in AUTO_DEVICE_TYPES:
                handle = self.z.OpenDevice(candidate, 0, 0)
                if handle != INVALID_DEVICE_HANDLE:
                    self.device = handle
                    self.detected_device_type = candidate
                    break
            if self.device == INVALID_DEVICE_HANDLE:
                raise RuntimeError("自动识别失败：未找到 ZLG 设备（试过 %d 种型号）"
                                   "：未插设备 / 被 ZCANPRO 占用" % len(AUTO_DEVICE_TYPES))
        else:
            self.device = self.z.OpenDevice(wanted, 0, 0)
            if self.device == INVALID_DEVICE_HANDLE:
                raise RuntimeError("打开 ZLG 设备失败 (device_type=%s)：未插设备 / 被 ZCANPRO 占用 / "
                                   "设备型号不符" % device_type)
        device_type = self.detected_device_type
        try:
            info = self.z.GetDeviceInf(self.device)
            self.device_info = {"hw": getattr(info, "hw_version", ""),
                                "fw": getattr(info, "fw_version", ""),
                                "serial": str(getattr(info, "serial", "")).strip("\x00"),
                                "type": str(getattr(info, "hw_type", "")).strip("\x00"),
                                "can_num": int(getattr(info, "can_num", 0))}
        except Exception:
            self.device_info = {}
        try:
            self.z.ZCAN_SetValue(self.device, "0/set_device_tx_echo", b"0")
        except Exception:
            pass

        self.channels = {}
        self._device_family = "canfd" if int(device_type) in CANFD_DEVICE_TYPES else "classic"
        for spec in channels or []:
            bus = str(spec.get("bus") or "")
            chn = int(spec.get("ch") or 0)
            baud = int(spec.get("baud") or 500000)
            data_baud = int(spec.get("data_baud") or 2000000)
            mode = int(spec.get("mode") or 0)          # 0 正常 / 1 只听
            proto = str(spec.get("proto") or "").strip().lower()
            usage = proto if proto in ("can", "canfd") else self._device_family
            if usage == "canfd":
                self._apply_canfd_options(chn, spec, baud, data_baud)
            self._set_baud(chn, baud, data_baud)
            try:
                self.z.ZCAN_SetValue(self.device, "%d/initenal_resistance" % chn,
                                     b"1" if spec.get("terminator", True) else b"0")
            except Exception:
                pass
            config = ZCAN_CHANNEL_INIT_CONFIG()
            if usage == "canfd":
                config.can_type = module.ZCAN_TYPE_CANFD
                config.config.canfd.mode = mode
            else:
                config.can_type = ZCAN_TYPE_CAN
                config.config.can.mode = mode
            handle = self.z.InitCAN(self.device, chn, config)
            if not handle:
                raise RuntimeError("InitCAN CH%d 失败" % chn)
            if self.z.StartCAN(handle) != ZCAN_STATUS_OK:
                raise RuntimeError("StartCAN CH%d 失败" % chn)
            self.channels[bus] = {"ch": chn, "baud": baud, "handle": handle,
                                  "tx": False, "mode": mode, "proto": usage,
                                  "running": True, "spec": dict(spec)}
            log("CH%d <- %s (%d bps, mode=%d, %s)"
                % (chn, bus, baud, mode, usage))
        self._stop.clear()
        self.rx = RxAggregator(self.stats_ms)
        self._rx_thread = threading.Thread(target=self._rx_loop, name="rx")
        self._rx_thread.daemon = True
        self._rx_thread.start()
        self._stim_thread = threading.Thread(target=self._stim_loop, name="stim")
        self._stim_thread.daemon = True
        self._stim_thread.start()
        return {"device": self.device_info,
                "device_type": self.detected_device_type,
                "channels": [{"bus": bus, "ch": item["ch"], "baud": item["baud"],
                              "mode": item["mode"]} for bus, item in self.channels.items()]}

    def _set_value(self, key, value):
        """写一个设备/通道参数键，成功返回 True（失败静默，由调用方决定是否告警）。"""
        try:
            return self.z.ZCAN_SetValue(self.device, key, str(value).encode("utf-8")) == 1
        except Exception:
            return False

    def _apply_canfd_options(self, chn, spec, baud, data_baud):
        """CANFD 专属选项：标准、加速、采样点（采样点走 baud_rate_custom）。"""
        standard = spec.get("fd_standard")
        if standard is not None:
            self._set_value("%d/canfd_standard" % chn, int(standard))
        accel = spec.get("accel")
        if accel is not None:
            self._set_value("%d/canfd_exp" % chn, 1 if accel else 0)
        arb_sp = spec.get("arb_sp")
        data_sp = spec.get("data_sp")
        if arb_sp or data_sp:
            text = "%gKbps(%g%%),%gMbps(%g%%)" % (
                baud / 1000.0, float(arb_sp or 80), data_baud / 1000000.0, float(data_sp or 80))
            if not self._set_value("%d/baud_rate_custom" % chn, text):
                log("CH%d 采样点配置不支持（%s），退回固定波特率" % (chn, text), "warn")

    def channel_start(self, bus):
        """启动一路通道。"""
        item = self.channels.get(bus)
        if not item:
            raise RuntimeError("通道 %s 未初始化" % bus)
        if item.get("running"):
            return {"bus": bus, "running": True}
        if not self.z.StartCAN(item["handle"]):
            raise RuntimeError("StartCAN %s 失败" % bus)
        item["running"] = True
        return {"bus": bus, "running": True}

    def channel_stop(self, bus):
        """停止一路通道。"""
        item = self.channels.get(bus)
        if not item:
            raise RuntimeError("通道 %s 未初始化" % bus)
        self.z.ResetCAN(item["handle"])
        item["running"] = False
        return {"bus": bus, "running": False}

    def set_rx_watch(self, ids=None, watch_all=False):
        """设定要留存原始载荷的 CAN ID（DBC 解码用）。"""
        self._watch_all = bool(watch_all)
        self._watch = {int(i) & ID_MASK for i in (ids or [])}
        return len(self._watch)

    def _set_baud(self, chn, baud, data_baud):
        """按设备族设置波特率；USBCANFD 只认 canfd_* 两个键，经典设备认 can_baud_rate。"""
        keys = []
        if self._device_family == "canfd":
            keys.append(("%d/canfd_abit_baud_rate" % chn, baud))
            keys.append(("%d/canfd_dbit_baud_rate" % chn, data_baud))
        keys.append(("%d/can_baud_rate" % chn, baud))
        errors = []
        for key, value in keys:
            try:
                if self.z.ZCAN_SetValue(self.device, key, str(value).encode("utf-8")) == 1:
                    return
                errors.append(key)
            except Exception as exc:
                errors.append("%s(%s)" % (key, exc))
        if len(errors) == len(keys):
            raise RuntimeError("设置 CH%d 波特率失败: %s" % (chn, ", ".join(errors)))

    def close(self):
        """停止线程、复位通道、关闭设备。"""
        self._stop.set()
        self._tx_running = False
        with self._stream_lock:
            self._streams = {}
        for thread in (self._rx_thread, self._tx_thread, self._stim_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.5)
        self._rx_thread = None
        self._tx_thread = None
        self._stim_thread = None
        if self.rx is not None:
            self.rx.close()
            self.rx = None
        if self.z is not None:
            for item in (self.channels or {}).values():
                try:
                    self.z.ResetCAN(item["handle"])
                except Exception:
                    pass
            if self.device:
                try:
                    self.z.CloseDevice(self.device)
                except Exception:
                    pass
        self.channels = {}
        self.device = None

    # -- 接收 -------------------------------------------------------------
    def _rx_loop(self):
        """轮询各路接收缓冲，喂给聚合器；到期发快照。"""
        while not self._stop.is_set():
            try:
                for bus, item in list(self.channels.items()):
                    self._drain(bus, item)
                snap = self.rx.snapshot() if self.rx else None
                if snap is not None:
                    if self._last_payload:
                        snap["last"] = self._last_payload
                    emit(snap)
            except Exception as exc:
                log("接收线程异常: %s" % exc, "error")
            self._stop.wait(0.01)

    def _remember(self, bus, can_id, data, stamp, ext=False):
        """按观察列表留存最近一帧载荷。"""
        masked = can_id & ID_MASK
        if not self._watch_all and masked not in self._watch:
            return
        slot = self._last_payload.setdefault(bus, {})
        slot[masked] = {"d": data, "t": int(stamp * 1000), "ext": bool(ext)}

    def _drain(self, bus, item):
        """排空一路通道的接收缓冲（经典 CAN 队列 + CANFD 队列）。"""
        self._drain_classic(bus, item)
        if self._device_family == "canfd":
            self._drain_fd(bus, item)

    def _drain_classic(self, bus, item):
        """经典 CAN 接收队列。"""
        z = self.z
        handle = item["handle"]
        pending = int(z.GetReceiveNum(handle, self._zlg[1]))
        if pending <= 0:
            return
        msgs, ret = z.Receive(handle, min(pending, 2000), 0)
        if not ret:
            return
        stamp = time.time()
        for index in range(int(ret)):
            frame = msgs[index].frame
            can_id = int(frame.can_id)
            if can_id & (1 << 30):                    # 远程帧无数据
                dlc = 0
                data = ""
            else:
                dlc = min(int(frame.can_dlc), MAX_DLC)
                data = " ".join("%02X" % frame.data[offset] for offset in range(dlc))
            self._remember(bus, can_id, data, stamp, bool(can_id & EXT_FLAG))
            self.rx.feed(bus, can_id, dlc, data, stamp)

    def _drain_fd(self, bus, item):
        """CANFD 接收队列（总线上若有 FD 帧，也一并统计）。"""
        z = self.z
        handle = item["handle"]
        pending = int(z.GetReceiveNum(handle, self._zlg_fd))
        if pending <= 0:
            return
        msgs, ret = z.ReceiveFD(handle, min(pending, 1000), 0)
        if not ret:
            return
        stamp = time.time()
        for index in range(int(ret)):
            frame = msgs[index].frame
            can_id = int(frame.can_id)
            length = min(int(frame.len), 64)
            data = " ".join("%02X" % frame.data[offset] for offset in range(length))
            self._remember(bus, can_id, data, stamp, bool(can_id & EXT_FLAG))
            self.rx.feed(bus, can_id, length, data, stamp)

    # -- 发送 -------------------------------------------------------------
    def tx_load(self, manifest, buses=None, seed=1, rand_mode="per-frame",
                rand_interval_ms=1000.0, pins=None, cycle_scale=1.0,
                only_ids=None, exclude_ids=None):
        """载入报文清单，构建发送任务（不动发送开关）。"""
        if not os.path.isfile(manifest):
            raise RuntimeError("报文清单不存在: %s" % manifest)
        with open(manifest, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        messages = document.get("messages") or []
        if not messages:
            raise RuntimeError("清单里没有报文: %s" % manifest)
        only = {int(value, 0) for value in (only_ids or [])}
        exclude = {int(value, 0) for value in (exclude_ids or [])}
        wanted = {str(bus).lower() for bus in (buses or []) if bus}
        scale = float(cycle_scale or 1.0)
        if scale <= 0:
            scale = 1.0
        start_at = time.perf_counter()
        jobs = []
        skipped = 0
        for message in messages:
            bus = message["bus"]
            if wanted and bus not in wanted:
                continue
            if only and int(message["id"]) not in only:
                continue
            if exclude and int(message["id"]) in exclude:
                continue
            if int(message["dlc"]) > MAX_DLC:
                skipped += 1
                continue
            period = max(0.001, float(message["cycle_ms"]) / 1000.0 * scale)
            jobs.append(TxJob(message, period, start_at))
        with self._tx_lock:
            self._jobs = jobs
            self._pins = dict(pins or {})
            self._rng = random.Random(int(seed) if seed is not None else 1)
            self._rand_mode = rand_mode
            self._rand_interval = max(0.001, float(rand_interval_ms) / 1000.0)
            self._tx_sent = 0
            self._tx_errors = 0
        return {"jobs": len(jobs), "skipped": skipped}

    def tx_start(self):
        """启动发送线程（同时打开全部总线的发送闸门）。"""
        if not self.channels:
            raise RuntimeError("尚未打开任何 CAN 通道")
        with self._tx_lock:
            if not self._jobs:
                raise RuntimeError("尚未载入报文清单")
            if self._tx_thread is not None and self._tx_thread.is_alive():
                self._tx_running = True
                return {"already": True}
            start_at = time.perf_counter()
            for job in self._jobs:
                job.next_due = start_at + job.period
                job.payload = None
                job.refresh_at = 0.0
            for item in self.channels.values():
                item["tx"] = True
            self._tx_running = True
            self._tx_last_snapshot = time.time()
            self._tx_thread = threading.Thread(target=self._tx_loop, name="tx")
            self._tx_thread.daemon = True
            self._tx_thread.start()
        return {"running": True, "jobs": len(self._jobs)}

    def tx_stop(self):
        """停止发送线程（保留任务，可再次启动）。"""
        self._tx_running = False
        thread = self._tx_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._tx_thread = None
        return {"running": False}

    def set_bus_tx(self, bus, enable):
        """按总线打开/关闭发送闸门。"""
        item = self.channels.get(bus)
        if item is None:
            return {"ok": False, "message": "通道 %r 未打开" % bus}
        item["tx"] = bool(enable)
        return {"ok": True, "bus": bus, "tx": item["tx"]}

    def send_manual(self, bus, can_id, data, period_ms, count):
        """手动按周期发一帧（不受发送闸门/清单影响，用于唤醒帧、单帧模拟）。"""
        item = self.channels.get(bus)
        if item is None:
            raise RuntimeError("通道 %r 未打开" % bus)
        payload = bytes.fromhex(str(data or "").replace(" ", "").replace("-", ""))
        if len(payload) > MAX_DLC:
            raise RuntimeError("数据超过 8 字节")
        count = max(1, int(count or 1))
        period = max(1, int(period_ms or 0)) / 1000.0

        def worker():
            sent = 0
            for _index in range(count):
                if self._stop.is_set():
                    break
                try:
                    message = self._transmit_type()
                    frame = message.frame
                    frame.can_id = ((int(can_id) | EXT_FLAG) if int(can_id) > 0x7FF
                                    else int(can_id))
                    frame.can_dlc = len(payload)
                    for offset, byte in enumerate(payload):
                        frame.data[offset] = byte
                    message.transmit_type = 0
                    if int(self.z.Transmit(item["handle"], message, 1)) == 1:
                        sent += 1
                except Exception as exc:
                    log("手动发送异常: %s" % exc, "error")
                    break
                time.sleep(period)
            log("手动发送完成 %s 0x%08X 共 %d/%d 帧"
                % (bus, int(can_id), sent, count))

        thread = threading.Thread(target=worker, name="manual-tx")
        thread.daemon = True
        thread.start()
        return {"bus": bus, "can_id": int(can_id), "period_ms": int(period_ms),
                "count": count, "bytes": len(payload)}

    # -- 电源状态机激励 ---------------------------------------------------
    def _send_one(self, bus, can_id, data):
        """发一帧（不经过发送闸门）；返回是否成功入队。"""
        item = self.channels.get(bus)
        if item is None:
            return False
        try:
            payload = bytes.fromhex(str(data or "").replace(" ", ""))
        except ValueError:
            return False
        if len(payload) > MAX_DLC:
            return False
        try:
            message = self._transmit_type()
            frame = message.frame
            frame.can_id = ((int(can_id) | EXT_FLAG) if int(can_id) > 0x7FF else int(can_id))
            frame.can_dlc = len(payload)
            for offset, byte in enumerate(payload):
                frame.data[offset] = byte
            message.transmit_type = 0
            return int(self.z.Transmit(item["handle"], message, 1)) == 1
        except Exception as exc:
            log("激励发送异常: %s" % exc, "error")
            return False

    def stim_start(self, name, bus, frames, period_ms):
        """登记/替换一路激励流（按周期轮转发 ``frames`` 里的帧）。"""
        if bus not in self.channels:
            raise RuntimeError("通道 %r 未打开" % bus)
        parsed = []
        for item in frames or []:
            try:
                can_id = int(item[0])
                data = item[1] if len(item) > 1 else ""
            except (TypeError, IndexError, ValueError):
                continue
            parsed.append((can_id, str(data)))
        if not parsed:
            raise RuntimeError("激励帧为空")
        period = max(1, int(period_ms or 100)) / 1000.0
        with self._stream_lock:
            self._streams[str(name)] = {
                "bus": bus, "frames": parsed, "period": period,
                "idx": 0, "next_due": time.perf_counter(), "sent": 0,
            }
        return {"name": str(name), "bus": bus, "frames": len(parsed),
                "period_ms": int(period * 1000)}

    def stim_stop(self, name=None):
        """停止某一路（``name=None`` 停全部）。"""
        with self._stream_lock:
            if name is None:
                removed = list(self._streams.keys())
                self._streams = {}
            else:
                removed = [name] if name in self._streams else []
                self._streams.pop(name, None)
        return {"stopped": removed}

    def stim_status(self):
        """当前激励流快照（供界面显示）。"""
        with self._stream_lock:
            return {name: {"bus": item["bus"], "period_ms": int(item["period"] * 1000),
                           "frames": len(item["frames"]), "sent": item["sent"]}
                    for name, item in self._streams.items()}

    def _stim_loop(self):
        """激励调度：5ms 一拍，按各流周期轮转发帧；每 500ms 上报一次统计。"""
        last_report = 0.0
        while not self._stop.is_set():
            now = time.perf_counter()
            with self._stream_lock:
                streams = list(self._streams.items())
            for name, stream in streams:
                if now < stream["next_due"]:
                    continue
                can_id, data = stream["frames"][stream["idx"] % len(stream["frames"])]
                if self._send_one(stream["bus"], can_id, data):
                    stream["sent"] += 1
                stream["idx"] += 1
                stream["next_due"] += stream["period"]
                if stream["next_due"] <= now:
                    stream["next_due"] = now + stream["period"]
            wall = time.time()
            if wall - last_report >= 0.5:
                last_report = wall
                emit({"ev": "stim", "t": int(wall * 1000), "streams": self.stim_status()})
            self._stop.wait(0.005)

    def _tx_loop(self):
        """绝对时刻调度 + 到期批发送。"""
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        try:
            while self._tx_running and not self._stop.is_set():
                now = time.perf_counter()
                due = {}
                with self._tx_lock:
                    jobs = list(self._jobs)
                    pins = dict(self._pins)
                    rng = self._rng
                    mode = self._rand_mode
                    interval = self._rand_interval
                for job in jobs:
                    if job.next_due > now:
                        continue
                    item = self.channels.get(job.bus)
                    if item is None or not item.get("tx"):
                        job.next_due = now + job.period     # 闸门关闭：只顺延不打点
                        continue
                    job.next_due += job.period
                    if job.next_due <= now:
                        job.next_due = now + job.period
                    if mode == "interval":
                        if job.payload is None or now >= job.refresh_at:
                            job.payload = job.build(rng, pins)
                            job.refresh_at = now + interval
                        payload = job.payload
                    else:
                        payload = job.build(rng, pins)
                    due.setdefault(job.bus, []).append((job, payload))

                for bus, items in due.items():
                    sent = self._transmit(bus, items)
                    self._tx_sent += sent
                    self._tx_errors += len(items) - sent
                    self._tx_window += sent
                self._emit_tx_snapshot()
                time.sleep(0.001)
        finally:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    def _transmit(self, bus, items):
        """把一批 ``[(job, payload)]`` 发出去，返回成功帧数。"""
        item = self.channels.get(bus)
        if item is None:
            return 0
        handle = item["handle"]
        Transmit = self.z.Transmit
        count = len(items)
        try:
            if count > 1:
                array = (self._transmit_type * count)()
                for index, (job, payload) in enumerate(items):
                    frame = array[index].frame
                    frame.can_id = (job.can_id | EXT_FLAG) if job.can_id > 0x7FF else job.can_id
                    frame.can_dlc = len(payload)
                    for offset, byte in enumerate(payload):
                        frame.data[offset] = byte
                    array[index].transmit_type = 0
                ret = int(Transmit(handle, array, count))
                if ret == count:
                    for job, _payload in items:
                        job.sent += 1
                    return count
                if ret == 0:
                    # 批量接口不可用：退回逐帧
                    self._batch_ok = False
                else:
                    return max(0, ret)
            sent = 0
            for job, payload in items:
                message = self._transmit_type()
                frame = message.frame
                frame.can_id = (job.can_id | EXT_FLAG) if job.can_id > 0x7FF else job.can_id
                frame.can_dlc = len(payload)
                for offset, byte in enumerate(payload):
                    frame.data[offset] = byte
                message.transmit_type = 0
                if int(Transmit(handle, message, 1)) == 1:
                    sent += 1
                    job.sent += 1
            return sent
        except Exception as exc:
            log("发送异常: %s" % exc, "error")
            return 0

    def _emit_tx_snapshot(self):
        """每 ``stats_ms`` 上报一次发送统计。"""
        now = time.time()
        if (now - self._tx_last_snapshot) * 1000.0 < self.stats_ms:
            return
        window_s = max(1e-3, now - self._tx_last_snapshot)
        self._tx_last_snapshot = now
        per_bus = {}
        for bus, item in self.channels.items():
            per_bus[bus] = {"tx": bool(item.get("tx"))}
        emit({"ev": "tx", "t": int(now * 1000), "fps": round(self._tx_window / window_s, 1),
              "sent": self._tx_sent, "errors": self._tx_errors,
              "jobs": len(self._jobs), "buses": per_bus, "stim": self.stim_status()})
        self._tx_window = 0

    # -- 统计 -------------------------------------------------------------
    def stats(self):
        """立即回一份接收快照 + 发送统计。"""
        if self.rx is not None:
            snap = self.rx.snapshot(force=True)
            if snap is not None:
                emit(snap)
        emit({"ev": "tx", "t": int(time.time() * 1000), "now": True,
              "sent": self._tx_sent, "errors": self._tx_errors,
              "jobs": len(self._jobs), "running": bool(self._tx_running),
              "stim": self.stim_status(),
              "buses": {bus: {"tx": bool(self._tx_running and item.get("tx"))}
                        for bus, item in self.channels.items()}})


# ---------------------------------------------------------------------------
# 命令循环
# ---------------------------------------------------------------------------

def main():
    """入口：握手 -> 读 stdin 命令 -> 逐条执行并回 ack。"""
    resolved_sdk = find_sdk_dir(SDK_DIR_HINT) or ""
    try:
        load_sdk(resolved_sdk)
        sdk_ok = True
    except Exception as exc:
        sdk_ok = False
        sdk_error = str(exc)
    else:
        sdk_error = ""
    emit({"ev": "hello", "py": sys.version.split()[0],
          "bits": 64 if sys.maxsize > 2 ** 32 else 32, "sdk": sdk_ok,
          "sdk_error": sdk_error, "sdk_dir": resolved_sdk, "pid": os.getpid()})

    engine = Engine()
    engine._transmit_type = None
    engine._batch_ok = True

    for raw in iter(sys.stdin.readline, ""):
        line = raw.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except ValueError:
            log("无法解析命令: %s" % line[:200], "warn")
            continue
        name = command.get("cmd")
        try:
            if name == "open":
                result = engine.open(command.get("device_type", 41),
                                     command.get("channels") or [],
                                     command.get("sdk_dir"),
                                     command.get("stats_ms", STATS_MS_DEFAULT))
                emit({"ev": "ack", "cmd": "open", "ok": True, "message": "设备已打开",
                      "data": result})
            elif name == "close":
                engine.close()
                emit({"ev": "ack", "cmd": "close", "ok": True})
            elif name == "ch_start":
                emit({"ev": "ack", "cmd": "ch_start", "ok": True,
                      "data": engine.channel_start(command.get("bus"))})
            elif name == "ch_stop":
                emit({"ev": "ack", "cmd": "ch_stop", "ok": True,
                      "data": engine.channel_stop(command.get("bus"))})
            elif name == "rx_watch":
                count = engine.set_rx_watch(command.get("ids"), command.get("all", False))
                emit({"ev": "ack", "cmd": "rx_watch", "ok": True,
                      "data": {"count": count}})
            elif name == "tx_load":
                result = engine.tx_load(command.get("manifest"),
                                        command.get("buses"),
                                        command.get("seed", 1),
                                        command.get("rand_mode", "per-frame"),
                                        command.get("rand_interval_ms", 1000.0),
                                        command.get("pins"),
                                        command.get("cycle_scale", 1.0),
                                        command.get("only_ids"),
                                        command.get("exclude_ids"))
                emit({"ev": "ack", "cmd": "tx_load", "ok": True,
                      "message": "已载入 %d 条报文" % result["jobs"], "data": result})
            elif name == "tx_start":
                result = engine.tx_start()
                emit({"ev": "ack", "cmd": "tx_start", "ok": True, "data": result})
            elif name == "tx_stop":
                engine.tx_stop()
                emit({"ev": "ack", "cmd": "tx_stop", "ok": True})
            elif name == "bus_tx":
                result = engine.set_bus_tx(command.get("bus"), command.get("enable"))
                emit({"ev": "ack", "cmd": "bus_tx", "ok": bool(result.get("ok")),
                      "data": result})
            elif name == "send_frame":
                result = engine.send_manual(command.get("bus"), command.get("id"),
                                            command.get("data"), command.get("period_ms"),
                                            command.get("count"))
                emit({"ev": "ack", "cmd": "send_frame", "ok": True,
                      "message": "已开始手动发送", "data": result})
            elif name == "stim_start":
                result = engine.stim_start(command.get("name") or "stim",
                                           command.get("bus") or "ican",
                                           command.get("frames") or [],
                                           command.get("period_ms") or 100)
                emit({"ev": "ack", "cmd": "stim_start", "ok": True,
                      "message": "激励已启动", "data": result})
            elif name == "stim_stop":
                result = engine.stim_stop(command.get("name"))
                emit({"ev": "ack", "cmd": "stim_stop", "ok": True,
                      "message": "激励已停止", "data": result})
            elif name == "stim_status":
                emit({"ev": "ack", "cmd": "stim_status", "ok": True,
                      "data": engine.stim_status()})
            elif name == "rx_log":
                path = command.get("path")
                if command.get("enable") and path:
                    engine.rx.close()
                    engine.rx = RxAggregator(engine.stats_ms, path)
                elif engine.rx is not None:
                    engine.rx.close()
                    engine.rx = RxAggregator(engine.stats_ms)
                emit({"ev": "ack", "cmd": "rx_log", "ok": True})
            elif name == "stats":
                engine.stats()
                emit({"ev": "ack", "cmd": "stats", "ok": True})
            elif name == "ping":
                emit({"ev": "ack", "cmd": "ping", "ok": True})
            elif name == "quit":
                emit({"ev": "ack", "cmd": "quit", "ok": True})
                break
            else:
                emit({"ev": "ack", "cmd": name, "ok": False, "message": "未知命令"})
        except Exception as exc:
            emit({"ev": "ack", "cmd": name, "ok": False,
                  "message": "%s: %s" % (type(exc).__name__, exc)})
            log("命令 %s 失败: %s" % (name, exc), "error")
    engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
