# -*- coding: utf-8 -*-
"""canctl.py — CAN 子进程主管（64 位主进程侧）

``can_engine.py`` 必须跑在 32 位 Python（ZLG SDK 只有 32 位 DLL），本模块负责：

* 选题器/解释器（``py -3.8-32`` 或配置的绝对路径）、拉起子进程、握手；
* 用行分隔 JSON 下发命令、等待 ack（带超时），把事件转给日志与探针；
* 维护"当前接收统计 / 发送统计 / 通道可用性"快照，供界面与判定规则读取；
* 子进程意外退出时给出明确状态，不影响工作台其余部分。

对外只暴露状态与动作，不暴露协议细节。
"""

import json
import os
import subprocess
import sys
import threading
import time

STUDIO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_PATH = os.path.join(STUDIO_DIR, "can_engine.py")

DEFAULT_CAN_CONFIG = {
    "enabled": True,
    "python": "",                       # 空 = 自动（优先 py -3.8-32）
    "sdk_dir": r"F:/zlgcan_python_260529",
    "device_type": 41,                  # USBCANFD-200U
    "stats_ms": 500,
    "manifest": os.path.join(os.path.dirname(STUDIO_DIR), "can_random_tx",
                             "can_matrix_manifest.json"),
    "channels": {
        "ican": {"ch": 0, "baud": 500000},
        "pcan": {"ch": 1, "baud": 500000},
    },
}


def _split_command(text):
    """把 ``"py -3.8-32"`` 这类命令行拆成 argv。"""
    return [part for part in str(text or "").split() if part]


class CanSupervisor(object):
    """子进程生命周期 + 命令/事件泵。"""

    def __init__(self, config, bus=None):
        self._log = bus
        self._config = dict(DEFAULT_CAN_CONFIG)
        self._config.update(config or {})
        self._proc = None
        self._reader = None
        self._err_reader = None
        self._lock = threading.RLock()
        self._ack_cond = threading.Condition(self._lock)
        self._acks = []
        self._stop = threading.Event()
        self.state = "off"                 # off / starting / running / error
        self.message = ""
        self.hello = {}
        self.device = {}
        self.channels = {}                 # bus -> {"ch","baud","mode"}
        self.tx_enabled = {}               # bus -> bool
        self.rx = {}                       # bus -> {"ids": {...}, "fps":..., "frames":...}
        self.tx = {"fps": 0.0, "sent": 0, "errors": 0, "jobs": 0}
        self.stimuli = {}                  # bus -> {mode, period_ms, sent}
        self.rx_log_path = ""
        self.last_rx_at = 0
        self.on_rx = None                  # 回调: (bus, ids_dict) -> None
        self.on_tx = None

    # -- 配置 -------------------------------------------------------------
    def configure(self, config):
        """更新配置（下次 start 生效；已打开时只更新非硬件项）。"""
        self._config.update(config or {})

    def _candidates(self):
        """可尝试的解释器列表（配置指定时只有它）。"""
        configured = str(self._config.get("python") or "").strip()
        if configured:
            return [_split_command(configured)]
        return [["py", "-3.8-32"], ["py", "-3"], ["python"]]

    def _channel_specs(self):
        """配置里的通道表 -> 引擎期望的 ``[{bus,ch,baud,mode}]``。"""
        specs = []
        for bus, item in (self._config.get("channels") or {}).items():
            specs.append({
                "bus": str(bus),
                "ch": int(item.get("ch", 0)),
                "baud": int(item.get("baud", 500000)),
                "mode": int(item.get("mode", 0)),
                "terminator": bool(item.get("terminator", True)),
            })
        return specs

    def bus_info(self, bus):
        """某路 CAN 的可用性（界面通道卡片用）。"""
        if not self._config.get("enabled", True):
            return {"available": False, "detail": "已禁用", "state": self.state}
        item = self.channels.get(bus)
        if item is None:
            return {"available": self.state == "running",
                    "detail": "CH%s" % (self._config.get("channels", {})
                                        .get(bus, {}).get("ch", "?")),
                    "state": self.state}
        return {"available": self.state == "running",
                "detail": "CH%s %dkbps" % (item.get("ch"), int(item.get("baud", 0)) // 1000),
                "state": self.state}

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        """拉起子进程并打开设备；返回 ``(ok, message)``。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self.state == "running", self.message or "引擎已在运行"
            self.state = "starting"
            self.message = "正在启动 CAN 引擎"
        errors = []
        for candidate in self._candidates():
            ok, message = self._spawn(candidate)
            if ok:
                break
            errors.append("%s: %s" % (" ".join(candidate), message))
        else:
            with self._lock:
                self.state = "error"
                self.message = "; ".join(errors[-2:]) or "无法启动 CAN 引擎"
            self._emit("error", "CAN 引擎启动失败: %s" % self.message)
            return False, self.message

        try:
            result = self.send("open", {
                "device_type": int(self._config.get("device_type", 41)),
                "channels": self._channel_specs(),
                "sdk_dir": self._config.get("sdk_dir"),
                "stats_ms": int(self._config.get("stats_ms", 500)),
            }, timeout=12.0)
        except Exception as exc:
            with self._lock:
                self.state = "error"
                self.message = "打开设备失败: %s" % exc
            self._emit("error", "CAN 设备打开失败: %s" % exc)
            self.kill()
            return False, self.message
        if not result.get("ok"):
            with self._lock:
                self.state = "error"
                self.message = result.get("message", "打开设备失败")
            self._emit("error", "CAN 设备打开失败: %s" % self.message)
            self.kill()
            return False, self.message

        data = result.get("data") or {}
        with self._lock:
            self.device = data.get("device") or {}
            self.channels = {item["bus"]: item for item in (data.get("channels") or [])}
            for bus in self.channels:
                self.tx_enabled.setdefault(bus, False)
            self.state = "running"
            self.message = "已连接 %s" % (self.device.get("type") or "ZLG 设备")
        self._emit("info", "CAN 引擎就绪: %s，通道 %s" % (
            self.device.get("type", "?"),
            ", ".join("%s=CH%s" % (name, item.get("ch")) for name, item in self.channels.items())))
        return True, self.message

    def _spawn(self, argv):
        """启动子进程并完成 hello 握手。"""
        command = list(argv) + [ENGINE_PATH, "--sdk-dir", str(self._config.get("sdk_dir") or "")]
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, encoding="utf-8",
                      errors="replace", bufsize=1)
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(command, **kwargs)
        except Exception as exc:
            return False, str(exc)
        reader = threading.Thread(target=self._read_stdout, args=(proc,), name="can-out")
        reader.daemon = True
        reader.start()
        err_reader = threading.Thread(target=self._read_stderr, args=(proc,), name="can-err")
        err_reader.daemon = True
        err_reader.start()
        with self._lock:
            self._proc = proc
            self._reader = reader
            self._err_reader = err_reader
            self.hello = {}
        deadline = time.time() + 8.0
        while time.time() < deadline:
            with self._lock:
                if self.hello:
                    break
                if proc.poll() is not None:
                    break
            time.sleep(0.05)
        with self._lock:
            hello = dict(self.hello)
        if not hello:
            self.kill()
            return False, "子进程无响应（解释器或 SDK 路径不对）"
        if not hello.get("sdk"):
            self.kill()
            return False, "子进程无法导入 zlgcan（需 32 位 Python + SDK 目录）"
        if hello.get("bits") != 32:
            self.kill()
            return False, "解释器是 %s 位，zlgcan.dll 需要 32 位" % hello.get("bits")
        self._emit("info", "CAN 引擎进程 pid=%s Python %s (%s 位)" % (
            hello.get("pid"), hello.get("py"), hello.get("bits")))
        return True, "ok"

    def stop(self):
        """停止发送、关闭设备、结束子进程。"""
        proc = self._proc
        if proc is None:
            with self._lock:
                self.state = "off"
            return
        try:
            self.send("tx_stop", timeout=2.0)
        except Exception:
            pass
        try:
            self.send("close", timeout=3.0)
            self.send("quit", timeout=2.0)
        except Exception:
            pass
        self.kill()
        with self._lock:
            self.state = "off"
            self.message = ""
            self.tx_enabled = {}
            self.channels = {}
            self.rx = {}
            self.stimuli = {}
            self.tx = {"fps": 0.0, "sent": 0, "errors": 0, "jobs": 0}

    def kill(self):
        """强制结束子进程。"""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass
        with self._lock:
            self._proc = None

    # -- 命令 -------------------------------------------------------------
    def send(self, cmd, payload=None, timeout=8.0):
        """下发一条命令并等待同名 ack；超时抛 ``RuntimeError``。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("CAN 引擎进程未运行")
        message = {"cmd": cmd}
        message.update(payload or {})
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except Exception as exc:
            raise RuntimeError("写入引擎失败: %s" % exc)
        deadline = time.time() + timeout
        with self._ack_cond:
            while True:
                for index, ack in enumerate(self._acks):
                    if ack.get("cmd") == cmd:
                        del self._acks[index]
                        return ack
                remain = deadline - time.time()
                if remain <= 0:
                    raise RuntimeError("引擎无响应: %s" % cmd)
                self._ack_cond.wait(min(remain, 0.5))

    def load_manifest(self, path=None, opts=None):
        """载入报文清单（发送任务）。"""
        options = dict(opts or {})
        payload = {"manifest": os.path.abspath(path or self._config.get("manifest")),
                   "buses": options.get("buses") or list(self.channels.keys()),
                   "seed": options.get("seed"),
                   "rand_mode": options.get("rand_mode", "per-frame"),
                   "rand_interval_ms": options.get("rand_interval_ms", 1000.0),
                   "pins": options.get("pins") or {},
                   "cycle_scale": options.get("cycle_scale", 1.0)}
        if payload["seed"] is None:
            import random as random_mod
            payload["seed"] = random_mod.SystemRandom().randrange(1 << 31)
        result = self.send("tx_load", payload, timeout=15.0)
        if not result.get("ok"):
            raise RuntimeError(result.get("message", "载入清单失败"))
        self._emit("info", "CAN 清单已载入: %d 条报文 (seed=%s)"
                   % ((result.get("data") or {}).get("jobs", 0), payload["seed"]))
        return result.get("data") or {}

    def start_tx(self):
        """开始周期发送。"""
        result = self.send("tx_start", timeout=5.0)
        if not result.get("ok"):
            raise RuntimeError(result.get("message", "启动发送失败"))
        for bus in self.channels:
            self.tx_enabled[bus] = True
        self._emit("action", "CAN 随机发送已启动（%d 条报文）"
                   % (result.get("data") or {}).get("jobs", 0))
        return result.get("data") or {}

    def stop_tx(self):
        """停止周期发送。"""
        result = self.send("tx_stop", timeout=5.0)
        with self._lock:
            for bus in self.channels:
                self.tx_enabled[bus] = False
            self.tx["fps"] = 0.0              # 引擎停发后不会再有 tx 事件，这里清掉显示残留
        self._emit("action", "CAN 随机发送已停止")
        return result

    def set_bus_tx(self, bus, enable):
        """按总线打开/关闭发送闸门。"""
        if self.state != "running":
            return {"ok": False, "message": "CAN 引擎未运行"}
        result = self.send("bus_tx", {"bus": bus, "enable": bool(enable)}, timeout=5.0)
        if result.get("ok"):
            self.tx_enabled[bus] = bool(enable)
        return {"ok": bool(result.get("ok")),
                "message": result.get("message", "已切换 %s 发送" % bus)}

    def send_frame(self, bus, can_id, data, period_ms=100, count=1):
        """手动发一帧（按周期重复 ``count`` 次），用于唤醒帧 / 单帧模拟。"""
        if self.state != "running":
            return {"ok": False, "message": "CAN 引擎未运行"}
        result = self.send("send_frame", {
            "bus": bus, "id": int(can_id), "data": str(data or ""),
            "period_ms": int(period_ms), "count": int(count),
        }, timeout=8.0)
        if result.get("ok"):
            data_info = result.get("data") or {}
            self._emit("action", "手动发送 %s 0x%08X ×%d @%dms（%d 字节）" % (
                bus, int(can_id), int(count), int(period_ms), data_info.get("bytes", 0)))
        return {"ok": bool(result.get("ok")),
                "message": result.get("message", ""), "data": result.get("data")}

    def start_stimulus(self, mode, bus=None, period_ms=100, can_id=None, data=None):
        """启动电源状态机激励（任意帧 / 唤醒帧 / 指定帧），按总线各起一路。"""
        from . import stimulus
        if self.state != "running":
            return {"ok": False, "message": "CAN 引擎未运行"}
        frames = stimulus.build_frames(mode, bus=bus, can_id=can_id, data=data)
        if not frames:
            return {"ok": False, "message": "该模式没有可用帧"}
        grouped = stimulus.group_by_bus(frames)
        started = []
        for frame_bus, items in grouped.items():
            name = "stim:%s" % frame_bus
            result = self.send("stim_start", {
                "name": name, "bus": frame_bus,
                "frames": [[int(cid), item_data] for cid, item_data in items],
                "period_ms": int(period_ms),
            }, timeout=8.0)
            if not result.get("ok"):
                return {"ok": False, "message": result.get("message", "激励启动失败")}
            started.append(frame_bus)
        self.stimuli = {frame_bus: {"mode": mode, "period_ms": int(period_ms), "sent": 0}
                        for frame_bus in started}
        self._emit("action", "CAN 激励启动：%s（%s，%dms，%s）" % (
            stimulus.MODE_LABELS.get(mode, mode), " + ".join(started),
            int(period_ms), "%d 帧" % len(frames)))
        return {"ok": True, "message": "激励已启动", "buses": started,
                "mode": mode, "period_ms": int(period_ms)}

    def stop_stimulus(self):
        """停止全部激励流（网络转静默）。"""
        if self.state != "running":
            return {"ok": False, "message": "CAN 引擎未运行"}
        try:
            self.send("stim_stop", {}, timeout=5.0)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        self.stimuli = {}
        self._emit("action", "CAN 激励已停止（网络静默）")
        return {"ok": True, "message": "激励已停止"}

    def enable_rx_log(self, path=None):
        """开启/关闭逐帧接收 CSV 记录。"""
        if self.state != "running":
            return
        payload = {"enable": bool(path), "path": path or ""}
        try:
            self.send("rx_log", payload, timeout=5.0)
            self.rx_log_path = path or ""
        except Exception as exc:
            self._emit("warn", "接收记录开关失败: %s" % exc)

    def request_stats(self):
        """主动要一份统计快照。"""
        if self.state == "running":
            try:
                self.send("stats", timeout=3.0)
            except Exception:
                pass

    # -- 管道读取 ---------------------------------------------------------
    def _read_stdout(self, proc):
        """逐行读引擎事件。"""
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    self._emit("debug", "引擎输出: %s" % line[:300])
                    continue
                self._handle_event(event)
        except Exception:
            pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    if self.state == "running":
                        self.state = "error"
                        self.message = "CAN 引擎进程已退出"
            self._emit("error", "CAN 引擎进程已退出")

    def _apply_stim(self, streams):
        """用引擎上报的激励流**重建**本地快照（消失的流会被清掉）。"""
        rebuilt = {}
        for key, item in (streams or {}).items():
            bus = key.split(":", 1)[1] if ":" in key else key
            rebuilt[bus] = {
                "mode": (self.stimuli.get(bus) or {}).get("mode", ""),
                "period_ms": item.get("period_ms", 0),
                "sent": item.get("sent", 0),
            }
        self.stimuli = rebuilt

    def _read_stderr(self, proc):
        """子进程 stderr 转日志。"""
        try:
            for raw in proc.stderr:
                text = raw.strip()
                if text:
                    self._emit("debug", "引擎 stderr: %s" % text[:300])
        except Exception:
            pass

    def _handle_event(self, event):
        """分派引擎事件。"""
        name = event.get("ev")
        if name == "hello":
            with self._lock:
                self.hello = event
            return
        if name == "ack":
            with self._ack_cond:
                self._acks.append(event)
                self._ack_cond.notify_all()
            return
        if name == "log":
            self._emit(event.get("level", "info"), "[CAN] %s" % event.get("text", ""))
            return
        if name == "rx":
            with self._lock:
                self.rx = event.get("buses") or {}
                self.last_rx_at = paths_now()
            if self.on_rx is not None:
                try:
                    self.on_rx(self.rx)
                except Exception:
                    pass
            return
        if name == "stim":
            self._apply_stim(event.get("streams") or {})
            return
        if name == "tx":
            with self._lock:
                self.tx = {"fps": event.get("fps", 0.0), "sent": event.get("sent", 0),
                           "errors": event.get("errors", 0), "jobs": event.get("jobs", 0)}
                for bus, item in (event.get("buses") or {}).items():
                    self.tx_enabled[bus] = bool(item.get("tx"))
                if event.get("stim") is not None:
                    self._apply_stim(event.get("stim") or {})
            if self.on_tx is not None:
                try:
                    self.on_tx(self.tx)
                except Exception:
                    pass
            return
        if name == "error":
            self._emit("error", "[CAN] %s" % event.get("message", ""))

    def _emit(self, level, text):
        """转日志（没有日志总线时静默）。"""
        if self._log is None:
            return
        self._log.emit(level, "can", text)

    # -- 快照 -------------------------------------------------------------
    def snapshot(self):
        """界面用 CAN 状态。"""
        with self._lock:
            buses = {}
            for bus, item in self.channels.items():
                rx = self.rx.get(bus) or {}
                buses[bus] = {
                    "ch": item.get("ch"),
                    "baud": item.get("baud"),
                    "tx": bool(self.tx_enabled.get(bus)),
                    "rx_fps": rx.get("fps", 0.0),
                    "ids": len(rx.get("ids") or {}),
                    "frames": rx.get("frames", 0),
                }
            return {"state": self.state,
                    "message": self.message,
                    "device": self.device,
                    "tx": dict(self.tx),
                    "buses": buses,
                    "stimuli": dict(self.stimuli),
                    "rx_log": self.rx_log_path,
                    "python": self.hello.get("py", "")}

    def rx_ids(self, bus):
        """某总线的接收 ID 统计（判定规则用）。"""
        with self._lock:
            item = (self.rx.get(bus) or {})
            return dict(item.get("ids") or {})


def paths_now():
    """当前毫秒时间戳（避免 import 循环）。"""
    return int(time.time() * 1000)
