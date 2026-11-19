# -*- coding: utf-8 -*-
"""incidents.py — 现场保全：出事后把"当时的一切"打包留档

一次保全产出 ``runs/<run>/incidents/<序号>_<原因>/``，内含：

    incident.json     结构化现场（规则/状态/通道/探针/CAN 统计/计数）
    README.md         人读摘要（什么规则、在哪个状态、当时通道什么状态）
    serial_tail.log   出事前最后 256KB 串口日志
    events_tail.jsonl 出事前最后 300 条工作台事件（含人工标记）
    can_rx.json       各路 CAN 的每 ID 统计（计数/周期/最新数据）
    can_rx.csv        逐帧接收记录（若本次运行开了接收记录）
    adb/uptime.txt    SOC uptime（判断是否刚重启）
    adb/mcu_log_tail.txt  DUT 侧 mcu.log 尾部
    adb/screenshot.png    DUT 屏幕截图（可选）

**默认不动电源**：出事后保持现场（不下电、不复位），方便回看屏幕与 DUT 日志；
需要恢复由人点"整机上电/断电"。``keep_power`` 只影响 README 里的提示文案。
"""

import json
import os
import re
import subprocess
import time

from . import paths

_SLUG = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


def _slug(text, fallback="incident", limit=40):
    """原因 -> 目录名片段。"""
    clean = _SLUG.sub("_", str(text or "")).strip("_")
    return (clean or fallback)[:limit]


class IncidentManager(object):
    """把一次事故的现场落盘，并向界面提供事故列表。"""

    def __init__(self, config, bus, probes, channels, can):
        self._config = config or {}
        self._log = bus
        self._probes = probes
        self._channels = channels
        self._can = can
        self.run_dir = ""
        self.run_id = ""
        self._seq = 0
        self._items = []

    def bind_run(self, run_dir, run_id=""):
        """绑定当前运行的落盘目录并重置计数。"""
        self.run_dir = run_dir or ""
        self.run_id = run_id or os.path.basename(run_dir or "")
        self._seq = 0
        self._items = []
        if self.run_dir:
            os.makedirs(os.path.join(self.run_dir, "incidents"), exist_ok=True)

    def configure(self, config):
        """更新保全配置（设置页保存后调用）。"""
        self._config = config or {}

    # -- 采集 -------------------------------------------------------------
    def capture(self, reason, rule=None, severity="warn", detail="", state="",
                freeze=True):
        """抓一次现场，返回 incident dict（同时写盘 + 进日志）。"""
        if not self.run_dir:
            self.bind_run(paths.run_dir(self._ensure_run()))
        self._seq += 1
        stamp = time.strftime("%H%M%S")
        folder = "%03d_%s_%s" % (self._seq, stamp, _slug(reason))
        target = os.path.join(self.run_dir, "incidents", folder)
        os.makedirs(target, exist_ok=True)
        cfg = self._config.get("incident") or {}

        snapshot = {
            "id": "%03d" % self._seq,
            "folder": folder,
            "time": paths.iso_now(),
            "t_ms": paths.now_ms(),
            "reason": reason,
            "rule": rule or {},
            "severity": severity,
            "detail": detail,
            "state": state,
            "run": self.run_id,
            "run_dir": self.run_dir,
            "freeze": bool(freeze),
            "keep_power": bool(cfg.get("keep_power", True)),
            "channels": self._channels.snapshot() if self._channels else [],
            "probe": self._probes.snapshot() if self._probes else {},
            "can": self._can.snapshot() if self._can else {},
        }
        if self._can is not None:
            snapshot["can_rx"] = {bus: self._can.rx_ids(bus)
                                  for bus in (self._can.channels or {})}
        self._write(target, "incident.json", json.dumps(snapshot, ensure_ascii=False, indent=1))

        # 串口尾部：优先用本次运行的 serial.log；没有运行文件时退回探针内存尾部
        tail_kb = int(cfg.get("serial_tail_kb", 256))
        serial_path = os.path.join(self.run_dir, "serial.log")
        if os.path.isfile(serial_path):
            self._write(target, "serial_tail.log",
                        paths.read_tail(serial_path, tail_kb * 1024))
        elif self._probes is not None:
            tail_lines = self._probes.serial.tail(2000)
            if tail_lines:
                self._write(target, "serial_tail.log", "\n".join(tail_lines) + "\n")

        # 事件尾部
        if self._log is not None:
            events = self._log.tail(int(cfg.get("events_tail", 300)))
            self._write(target, "events_tail.jsonl",
                        "\n".join(json.dumps(item, ensure_ascii=False) for item in events))

        # CAN 统计 + 逐帧记录
        if self._can is not None:
            self._write(target, "can_rx.json", json.dumps(snapshot.get("can_rx") or {},
                                                          ensure_ascii=False, indent=1))
            if self._can.rx_log_path and os.path.isfile(self._can.rx_log_path):
                self._write(target, "can_rx.csv", paths.read_tail(self._can.rx_log_path,
                                                                  4 * 1024 * 1024))

        # DUT 侧取证
        files = self._capture_adb(target, cfg)

        self._write(target, "README.md", self._readme(snapshot, files))

        incident = {
            "id": snapshot["id"],
            "folder": folder,
            "dir": target,
            "time": snapshot["time"],
            "t_ms": snapshot["t_ms"],
            "reason": reason,
            "rule": (rule or {}).get("name") or (rule or {}).get("rule") or "",
            "severity": severity,
            "detail": detail,
            "state": state,
            "run": self.run_id,
            "files": files,
        }
        self._items.append(incident)
        if self._log is not None:
            self._log.emit("incident", "scene",
                           "现场已保全: %s（%s）" % (reason, snapshot["folder"]),
                           state=state, dir=target, severity=severity)
        return incident

    def _capture_adb(self, target, cfg):
        """采集 DUT 侧证据（在线才有），返回落盘文件清单。"""
        files = {}
        if self._probes is None:
            return files
        if getattr(self._probes, "simulate", False):
            return files                     # 模拟模式：不碰真机 adb
        adb_probe = getattr(self._probes, "adb", None)
        if adb_probe is None:
            return files
        runner = adb_probe.runner
        if not runner.available:
            return files
        adb_dir = os.path.join(target, "adb")
        os.makedirs(adb_dir, exist_ok=True)
        devices = runner.devices()
        online = bool(devices)
        info = ["# adb 现场取证", "时间: %s" % paths.iso_now(),
                "设备: %s" % (", ".join(devices) or "(无)"), ""]
        uptime = runner.uptime() if online else None
        if online:
            info.append("uptime: %s s%s" % (
                ("%.2f" % uptime) if uptime is not None else "?",
                "（数值很小 = 刚重启）" if uptime is not None and uptime < 120 else ""))
            self._write_text(os.path.join(adb_dir, "uptime.txt"),
                             "%.3f\n" % uptime if uptime is not None else "")
            tail = runner.log_tail(adb_probe.log_path, int(cfg.get("adb_log_lines", 300)))
            if tail:
                files["adb/mcu_log_tail.txt"] = self._write(adb_dir, "mcu_log_tail.txt", tail)
            if cfg.get("screenshot", True):
                shot = os.path.join(adb_dir, "screenshot.png")
                ok, detail, stats = runner.screenshot(shot)
                if ok:
                    files["adb/screenshot.png"] = os.path.getsize(shot)
                    info.append("screenshot: %s（非黑 %.0f%%）"
                                % (detail, (stats.get("bright_ratio") or 0) * 100))
                else:
                    info.append("screenshot: 失败（%s）" % detail)
        else:
            info.append("uptime: (设备不在线)")
        files["adb/uptime.txt"] = self._write(os.path.join(adb_dir), "uptime.txt",
                                              "\n".join(info) + "\n")
        return files

    def _readme(self, snapshot, files):
        """生成人读摘要。"""
        rule = snapshot.get("rule") or {}
        lines = [
            "# 现场保全 — %s" % snapshot["reason"],
            "",
            "- 时间: %s" % snapshot["time"],
            "- 运行: %s" % snapshot["run"],
            "- 状态卡: %s" % (snapshot.get("state") or "(无)"),
            "- 规则: %s" % (rule.get("name") or rule.get("rule") or "(手动触发)"),
            "- 级别: %s" % snapshot["severity"],
            "- 详情: %s" % (snapshot.get("detail") or ""),
            "",
            "## 当时的通道状态（推定值，继电器无回读）",
            "",
        ]
        for channel in snapshot.get("channels") or []:
            lines.append("- %-12s %-5s %s" % (channel.get("label"), channel.get("state"),
                                              channel.get("detail") or ""))
        probe = snapshot.get("probe") or {}
        serial_state = probe.get("serial") or {}
        adb_state = probe.get("adb") or {}
        lines += [
            "",
            "## 探针",
            "",
            "- 串口: %s %s，已收 %s 行 / %.0f 行每秒" % (
                serial_state.get("state"), serial_state.get("port"),
                serial_state.get("lines"), serial_state.get("fps") or 0),
            "- SOC: %s%s，uptime=%s，日志增长 %.0f B/s" % (
                adb_state.get("state"),
                "" if adb_state.get("online") else "（离线）",
                adb_state.get("uptime_s"), adb_state.get("growth") or 0),
            "",
            "## 文件",
            "",
            "- `incident.json` 结构化现场",
            "- `serial_tail.log` 出事前串口日志",
            "- `events_tail.jsonl` 工作台事件",
            "- `can_rx.json` CAN 每 ID 统计",
        ]
        for name in sorted(files):
            lines.append("- `%s`" % name)
        if snapshot.get("keep_power"):
            lines += ["", "> 已保持现场：未改动电源，DUT 维持出事时的状态。", ""]
        return "\n".join(lines) + "\n"

    def _write(self, folder, name, content):
        """写文本文件，返回字节数。"""
        path = os.path.join(folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content or "")
        return os.path.getsize(path)

    def _write_text(self, path, content):
        """写文本文件（给定完整路径）。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content or "")
        return os.path.getsize(path)

    def _ensure_run(self):
        """没有活动运行时，临时开一个目录承载事故。"""
        run_id = paths.new_run_id("x")
        os.makedirs(paths.run_dir(run_id), exist_ok=True)
        return run_id

    # -- 列表 -------------------------------------------------------------
    def items(self):
        """本次运行的事故列表。"""
        return list(self._items)

    def scan(self, run_dir=None):
        """扫描某运行目录下的全部事故（页面刷新/切换历史时用）。"""
        base = os.path.join(run_dir or self.run_dir, "incidents")
        items = []
        if not base or not os.path.isdir(base):
            return items
        for entry in sorted(os.listdir(base)):
            path = os.path.join(base, entry, "incident.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            rule = data.get("rule") or {}
            items.append({
                "id": data.get("id"),
                "folder": entry,
                "dir": os.path.join(base, entry),
                "time": data.get("time"),
                "t_ms": data.get("t_ms"),
                "reason": data.get("reason"),
                "rule": rule.get("name") or rule.get("rule") or "",
                "severity": data.get("severity"),
                "detail": data.get("detail"),
                "state": data.get("state"),
                "run": data.get("run"),
            })
        items.sort(key=lambda item: item.get("t_ms") or 0)
        return items

    def open_folder(self, path=None):
        """在资源管理器里打开事故目录（或当前运行目录）。"""
        target = path or self.run_dir
        if not target or not os.path.isdir(target):
            return False
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", os.path.normpath(target)])
            else:
                subprocess.Popen(["xdg-open", target])
            return True
        except Exception:
            return False

    def write_run_summary(self, extra):
        """运行结束写 ``run.json`` 摘要。"""
        if not self.run_dir:
            return None
        payload = {
            "run": self.run_id,
            "finished_at": paths.iso_now(),
            "incidents": len(self._items),
        }
        payload.update(extra or {})
        path = os.path.join(self.run_dir, "run.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        return path
