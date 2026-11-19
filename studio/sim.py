# -*- coding: utf-8 -*-
"""sim.py — 无硬件的"彩排模式"：模拟 CAN 引擎

台架被占用时（或接不到设备时），用它把整条链路跑通：界面、剧本、断言、
现场保全都不变，只是数据来自模拟器而不是真实总线。

接口与 :class:`studio.canctl.CanSupervisor` 保持一致，``StudioApp`` 直接替换即可::

    SimCanSupervisor(...).start() -> (True, "模拟器已就绪")
    .load_manifest() / .start_tx() / .stop_tx() / .set_bus_tx()
    .snapshot() / .rx_ids(bus) / .enable_rx_log(path)

模拟数据来源：

* 有 ``can_matrix_manifest.json`` 时按其中每条报文的周期与 DLC 驱动接收统计；
* 没有清单时用一组内置 ID 兜底，保证界面有东西可看。
"""

import json
import os
import random
import threading
import time

DEFAULT_IDS = [
    ("0x18FEF117", 100, 8), ("0x18FFE117", 100, 8), ("0x18FFDA17", 200, 8),
    ("0x18FEF100", 100, 8), ("0x18FFE017", 500, 8), ("0x0CF00400", 20, 8),
    ("0x18FECA17", 1000, 8), ("0x18FEF11B", 100, 8),
]


class SimCanSupervisor(object):
    """模拟 CAN 设备：按报文周期产出接收统计，按发送闸门累计发送量。"""

    def __init__(self, config=None, bus=None):
        self._log = bus
        self._config = dict(config or {})
        self._lock = threading.RLock()
        self.state = "off"
        self.message = ""
        self.device = {}
        self.channels = {}
        self.tx_enabled = {}
        self.rx = {}
        self.tx = {"fps": 0.0, "sent": 0, "errors": 0, "jobs": 0}
        self.stimuli = {}
        self.rx_log_path = ""
        self.last_rx_at = 0
        self.on_rx = None
        self.on_tx = None
        self._messages = []          # [(bus, id, period_ms, dlc, next_due)]
        self._slots = {}             # bus -> id -> 统计
        self._thread = None
        self._stop = threading.Event()
        self._tx_running = False
        self._tx_sent = 0
        self._tx_window = 0
        self._tx_last = 0.0
        self._last_snapshot = 0.0
        self._csv = None
        self._rng = random.Random(20261117)

    # -- 生命周期 ---------------------------------------------------------
    def configure(self, config):
        """更新配置（模拟器只看通道表）。"""
        self._config.update(config or {})

    def start(self):
        """启动模拟器（始终成功）。"""
        with self._lock:
            if self.state == "running":
                return True, self.message
            buses = (self._config.get("channels") or {"ican": {"ch": 0}, "pcan": {"ch": 1}})
            self.channels = {name: {"ch": int(item.get("ch", index)),
                                    "baud": int(item.get("baud", 500000)),
                                    "handle": index + 1, "tx": True, "mode": 0}
                             for index, (name, item) in enumerate(buses.items())}
            self.device = {"type": "USBCANFD-200U（模拟）", "serial": "SIM-0000",
                           "hw": "V0.00", "fw": "V0.00", "can_num": len(self.channels)}
            for name in self.channels:
                self.tx_enabled[name] = False
            self.state = "running"
            self.message = "模拟模式：不连接真实设备"
            self._stop.clear()
            self._seed_messages()
            self._thread = threading.Thread(target=self._loop, name="sim-can")
            self._thread.daemon = True
            self._thread.start()
        self._emit("info", "CAN 模拟器已启动（%d 路，%d 条报文）"
                   % (len(self.channels), len(self._messages)))
        return True, self.message

    def stop(self):
        """停止模拟器。"""
        self._stop.set()
        self._tx_running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        with self._lock:
            self.state = "off"
            self.channels = {}
            self.rx = {}
            self.stimuli = {}
            self.tx = {"fps": 0.0, "sent": 0, "errors": 0, "jobs": 0}
        self._close_csv()

    def kill(self):
        """与真实主管同名接口。"""
        self.stop()

    def bus_info(self, bus):
        """通道可用性。"""
        item = self.channels.get(bus)
        if item is None:
            return {"available": False, "detail": "未连接", "state": self.state}
        return {"available": self.state == "running",
                "detail": "CH%s %dkbps（模拟）" % (item["ch"], item["baud"] // 1000),
                "state": self.state}

    # -- 报文清单 ---------------------------------------------------------
    def load_manifest(self, path=None, opts=None):
        """载入清单并重建模拟报文表。"""
        options = opts or {}
        manifest = (path or (self._config or {}).get("manifest")
                    or os.path.join(os.path.dirname(os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__)))),
                        "can_random_tx", "can_matrix_manifest.json"))
        messages = []
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                document = json.load(handle)
            for item in document.get("messages") or []:
                messages.append((item.get("bus") or "ican", int(item["id"]),
                                 float(item.get("cycle_ms") or 100),
                                 int(item.get("dlc") or 8)))
        except (OSError, ValueError, KeyError):
            messages = []
        if not messages:
            messages = [("ican", int(cid, 16), period, dlc) for cid, period, dlc in DEFAULT_IDS]
        buses = set(self.channels.keys()) or {"ican", "pcan"}
        messages = [item for item in messages if item[0] in buses] or [
            (sorted(buses)[0], int(cid, 16), period, dlc) for cid, period, dlc in DEFAULT_IDS]
        with self._lock:
            now = time.time()
            self._messages = [(bus, can_id, period, dlc, now + self._rng.random() * 0.01)
                              for bus, can_id, period, dlc in messages]
            self.tx["jobs"] = len(self._messages)
        self._emit("info", "模拟报文 %d 条（%s）" % (
            len(self._messages), ", ".join(sorted({item[0] for item in self._messages}))))
        return {"jobs": len(self._messages), "skipped": 0}

    def start_tx(self):
        """打开全部发送闸门并开始计数。"""
        with self._lock:
            self._tx_running = True
            for name in self.channels:
                self.tx_enabled[name] = True
        self._emit("action", "模拟 CAN 随机发送已启动")
        return {"running": True, "jobs": len(self._messages)}

    def stop_tx(self):
        """停止发送计数。"""
        with self._lock:
            self._tx_running = False
            for name in self.channels:
                self.tx_enabled[name] = False
        self._emit("action", "模拟 CAN 随机发送已停止")
        return {"running": False}

    def set_bus_tx(self, bus, enable):
        """按总线开关发送闸门。"""
        if bus not in self.channels:
            return {"ok": False, "message": "通道 %r 不存在" % bus}
        with self._lock:
            self.tx_enabled[bus] = bool(enable)
        return {"ok": True, "bus": bus, "tx": bool(enable)}

    def send_frame(self, bus, can_id, data, period_ms=100, count=1):
        """模拟器：只认账，不进总线。"""
        if bus not in self.channels:
            return {"ok": False, "message": "通道 %r 不存在" % bus}
        with self._lock:
            self._tx_sent += int(count)
        self._emit("action", "模拟手动发送 %s 0x%08X ×%d @%dms"
                   % (bus, int(can_id), int(count), int(period_ms)))
        return {"ok": True, "message": "模拟发送已记账",
                "data": {"bytes": len(str(data or "").split())}}

    def start_stimulus(self, mode, bus=None, period_ms=100, can_id=None, data=None):
        """模拟器：登记激励（不动真总线）。"""
        from . import stimulus
        frames = stimulus.build_frames(mode, bus=bus, can_id=can_id, data=data)
        if not frames:
            return {"ok": False, "message": "该模式没有可用帧"}
        buses = sorted({item[0] for item in frames})
        with self._lock:
            for name in buses:
                self.stimuli[name] = {"mode": mode, "period_ms": int(period_ms), "sent": 0}
        self._emit("action", "模拟激励启动：%s（%s）"
                   % (stimulus.MODE_LABELS.get(mode, mode), " + ".join(buses)))
        return {"ok": True, "message": "模拟激励已启动", "buses": buses,
                "mode": mode, "period_ms": int(period_ms)}

    def stop_stimulus(self):
        """模拟器：清空激励。"""
        with self._lock:
            self.stimuli = {}
        self._emit("action", "模拟激励已停止")
        return {"ok": True, "message": "模拟激励已停止"}

    def enable_rx_log(self, path=None):
        """模拟器同样落一份 CSV，保证现场保全里有东西。"""
        self._close_csv()
        self.rx_log_path = path or ""
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._csv = open(path, "a", encoding="utf-8", buffering=1)
            self._csv.write("t_ms,bus,can_id,ext,dlc,data\n")
        except OSError:
            self._csv = None

    def _close_csv(self):
        if self._csv is not None:
            try:
                self._csv.close()
            except OSError:
                pass
            self._csv = None

    def request_stats(self):
        """实时统计由线程自己推，这里无需动作。"""

    # -- 模拟循环 ---------------------------------------------------------
    def _seed_messages(self):
        """第一次启动时按默认清单填一批报文。"""
        if self._messages:
            return
        buses = list(self.channels.keys()) or ["ican"]
        now = time.time()
        self._messages = [
            (buses[index % len(buses)], int(cid, 16), period, dlc, now)
            for index, (cid, period, dlc) in enumerate(DEFAULT_IDS)
        ]
        self.tx["jobs"] = len(self._messages)

    def _loop(self):
        """10ms 一拍：按周期产出接收帧、累计发送量、每 500ms 推快照。"""
        while not self._stop.is_set():
            now = time.time()
            produced = 0
            with self._lock:
                messages = list(self._messages)
            updated = []
            for index, (bus, can_id, period, dlc, due) in enumerate(messages):
                if now < due:
                    continue
                next_due = due + period / 1000.0
                while next_due <= now:
                    next_due += period / 1000.0
                messages[index] = (bus, can_id, period, dlc, next_due)
                data = " ".join("%02X" % self._rng.randrange(256) for _ in range(dlc))
                self._feed(bus, can_id, dlc, data, now, period)
                produced += 1
            if produced:
                with self._lock:
                    self._messages = messages

            if self._tx_running:
                with self._lock:
                    fps = sum(1000.0 / item[2] for item in self._messages
                              if self.tx_enabled.get(item[0]))
                    self._tx_sent += int(round(fps * 0.01))
            if now - self._last_snapshot >= 0.5:
                self._last_snapshot = now
                self._emit_snapshot()
                if self._tx_running:
                    self._emit_tx()
            time.sleep(0.01)

    def _feed(self, bus, can_id, dlc, data, now, period):
        """累计一个 ID 的接收统计。"""
        key = "0x%08X" % can_id
        with self._lock:
            table = self._slots.setdefault(bus, {})
            slot = table.get(key)
            if slot is None:
                slot = {"n": 0, "win": 0, "last": now, "period": period, "dlc": dlc, "data": data}
                table[key] = slot
            slot["n"] += 1
            slot["win"] += 1
            slot["last"] = now
            slot["period"] = period
            slot["dlc"] = dlc
            slot["data"] = data
            csv = self._csv
        if csv is not None:
            try:
                csv.write("%d,%s,%s,1,%d,%s\n" % (int(now * 1000), bus, key, dlc, data))
            except OSError:
                pass

    def _emit_snapshot(self):
        """构造与真实引擎同构的接收快照。"""
        now = time.time()
        buses = {}
        with self._lock:
            for bus, table in self._slots.items():
                ids = {}
                window = 0
                for key, slot in table.items():
                    window += slot["win"]
                    ids[key] = {
                        "n": slot["n"], "win": slot["win"],
                        "age_ms": int((now - slot["last"]) * 1000),
                        "period_ms": round(slot["period"], 1),
                        "dlc": slot["dlc"], "data": slot["data"],
                    }
                    slot["win"] = 0
                buses[bus] = {"frames": sum(item["n"] for item in table.values()),
                              "fps": round(window * 2.0, 1), "ids": ids}
            self.rx = buses
            self.last_rx_at = int(now * 1000)
        if self.on_rx is not None:
            try:
                self.on_rx(buses)
            except Exception:
                pass

    def _emit_tx(self):
        """构造发送统计。"""
        now = time.time()
        span = max(1e-3, now - (self._tx_last or now - 0.5))
        self._tx_last = now
        with self._lock:
            fps = sum(1000.0 / item[2] for item in self._messages
                      if self.tx_enabled.get(item[0]))
            self.tx = {"fps": round(fps, 1), "sent": self._tx_sent, "errors": 0,
                       "jobs": len(self._messages)}
            buses = {name: {"tx": bool(self.tx_enabled.get(name))} for name in self.channels}
        if self.on_tx is not None:
            try:
                self.on_tx(self.tx)
            except Exception:
                pass
        return buses

    def _emit(self, level, text):
        if self._log is not None:
            self._log.emit(level, "can", "[SIM] %s" % text)

    # -- 快照 -------------------------------------------------------------
    def snapshot(self):
        """界面用 CAN 状态（与真实主管同构）。"""
        with self._lock:
            buses = {}
            for name, item in self.channels.items():
                rx = self.rx.get(name) or {}
                buses[name] = {
                    "ch": item.get("ch"), "baud": item.get("baud"),
                    "tx": bool(self.tx_enabled.get(name)),
                    "rx_fps": rx.get("fps", 0.0), "ids": len(rx.get("ids") or {}),
                    "frames": rx.get("frames", 0),
                }
            return {"state": self.state, "message": self.message, "device": self.device,
                    "tx": dict(self.tx), "buses": buses, "stimuli": dict(self.stimuli),
                    "rx_log": self.rx_log_path, "python": "模拟", "simulate": True}

    def rx_ids(self, bus):
        """某总线接收统计。"""
        with self._lock:
            return dict((self.rx.get(bus) or {}).get("ids") or {})
