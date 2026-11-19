# -*- coding: utf-8 -*-
"""studio_app.py — 工作台应用层：装配硬件/探针/执行器，提供动作分派与快照

分层关系::

    server.py        HTTP/SSE（只做协议转换，不含业务）
        │
    studio_app.py    业务装配与动作分派（本文件）
        ├─ channels.py   通道（继电器 / CAN 闸门 / 自定义命令）
        ├─ probes.py     抓取与判定（串口 / adb / CAN / 规则）
        ├─ runner.py     剧本执行
        ├─ incidents.py  现场保全
        └─ canctl.py     CAN 子进程主管

动作都以 ``{"op": "...", ...}`` 形式进来，统一在 :meth:`StudioApp.dispatch` 分派；
任何异常都被收敛成 ``{"ok": false, "message": "..."}``，不把栈抛给浏览器。
"""

import json
import os
import threading
import time

from . import config as config_mod
from . import paths, scenario as scenario_mod
from .canctl import CanSupervisor
from .channels import ChannelManager
from .eventlog import EventLog
from .incidents import IncidentManager
from .probes import ProbeHub
from .relay import RelayController
from .runner import Runner
from .sim import SimCanSupervisor

VERSION = "1.0.4"
STUDIO_NAME = "CBStudio · 台架剧本工作台"


class StudioApp(object):
    """工作台单例：持有全部子系统的引用与动作入口。"""

    def __init__(self, cfg=None, host="127.0.0.1", port=8777, simulate=False):
        paths.ensure_dirs()
        scenario_mod.seed_builtin()
        self.config = cfg or config_mod.load()
        self.host = host
        self.port = port
        self.simulate = bool(simulate or self.config.get("simulate"))
        self.bus = EventLog()
        self.started_at = time.time()
        self._dirty = threading.Event()

        relay_cfg = self.config.get("relay") or {}
        self.relay = RelayController(port=relay_cfg.get("port") or None,
                                     baud=int(relay_cfg.get("baud") or 9600),
                                     dry_run=bool(relay_cfg.get("dry_run")) or self.simulate,
                                     log=self.bus,
                                     states_path=os.path.join(paths.DATA_DIR,
                                                              "relay_state.json"))
        if self.simulate:
            self.can = SimCanSupervisor(self.config.get("can") or {}, bus=self.bus)
        else:
            self.can = CanSupervisor(self.config.get("can") or {}, bus=self.bus)
        self.channels = ChannelManager(self.config, self.bus, relay=self.relay, can=self.can)
        self.probes = ProbeHub(self.config.get("probes") or {}, self.bus, can=self.can,
                               simulate=self.simulate,
                               serial_online=self._sim_power_on,
                               adb_online=self._sim_soc_on)
        # 继电器 / DUT 串口互相避开对方已占用的口
        self.relay.exclude_provider = self.ports_in_use
        self.probes.serial.exclude_provider = self.relay_ports
        self.incidents = IncidentManager(self.config, self.bus, self.probes,
                                         self.channels, self.can)
        self.runner = Runner(self)

        # 用户保存过的监看规则优先于内置默认值
        saved_rules = (self.config.get("probes") or {}).get("rules")
        if saved_rules:
            self.probes.set_rules(saved_rules)

        # 探针 -> 运行器
        self.probes.set_state_getter(self.runner.state_of)
        self.probes.on_incident = self._on_rule_incident
        # CAN 接收 -> 界面刷新
        self.can.on_rx = self._on_can_update

        self.current_scenario = None
        self._load_default_scenario()
        # 抓屏记录：界面可预览最近抓的仪表画面
        self.screens = __import__("collections").deque(maxlen=20)
        self._screen_seq = 0
        self.BASELINE_MAX = 8
        self.screen_baseline = self._load_screen_baseline()
        self.read_regions = self._load_read_regions()

    # -- 仪表画面 ---------------------------------------------------------
    def relay_ports(self):
        """继电器占用的串口（DUT 串口自动识别时跳过）。"""
        port = getattr(self.relay, "port", None)
        return [port] if port else []

    def ports_in_use(self):
        """被其它用途占用的串口（继电器自动识别时避开）。"""
        used = []
        try:
            port = getattr(self.probes.serial, "port", None)
            if port:
                used.append(port)
        except Exception:
            pass
        return used

    @property
    def read_region(self):
        """电压读数区域（图像基线与旧接口用）。"""
        return self.read_regions.get("voltage") or dict(self.METRIC_DEFS["voltage"]["region"])

    def capture_screen(self, label="manual"):
        """抓一帧仪表画面，落盘到 data/screens 并登记，返回 ``(ok, 信息)``。"""
        if self.simulate:
            return False, "模拟模式没有仪表画面"
        adb_probe = getattr(self.probes, "adb", None)
        if adb_probe is None or not adb_probe.online:
            return False, "SOC 不在线，抓不到画面"
        screen_dir = os.path.join(paths.DATA_DIR, "screens")
        os.makedirs(screen_dir, exist_ok=True)
        self._screen_seq += 1
        name = "%s_%02d_%s.png" % (time.strftime("%Y%m%d-%H%M%S"), self._screen_seq,
                                   paths.safe_name(label, "shot", 24))
        path = os.path.join(screen_dir, name)
        ok, detail, stats = adb_probe.runner.screenshot(path, region=self.read_region)
        if not ok:
            return False, detail
        shot = {
            "id": name[:-4],
            "name": name,
            "path": path,
            "t": int(time.time() * 1000),
            "time": time.strftime("%H:%M:%S"),
            "label": label,
            "stats": stats,
            "detail": detail,
        }
        self.screens.appendleft(shot)
        # 运行中同时留一份到运行目录，事后可追溯
        run_dir = self.runner.run_dir
        if run_dir:
            try:
                import shutil
                target = os.path.join(run_dir, "screens")
                os.makedirs(target, exist_ok=True)
                shutil.copy2(path, os.path.join(target, name))
            except OSError:
                pass
        self._dirty.set()
        return True, shot

    def screen_by_id(self, screen_id):
        """按 id 找一张抓屏记录。"""
        for shot in self.screens:
            if shot["id"] == screen_id:
                return shot
        return None

    # -- 各指标的读数区域（可分别圈选） -----------------------------------
    METRIC_DEFS = {
        "voltage": {"label": "蓄电池电压", "unit": "V", "decimals": 1,
                    "region": {"x0": 0.625, "x1": 0.700, "y0": 0.900, "y1": 0.985}},
        "odo": {"label": "总里程", "unit": "km", "decimals": 0,
                "region": {"x0": 0.720, "x1": 0.820, "y0": 0.900, "y1": 0.985}},
        "trip": {"label": "小计里程", "unit": "km", "decimals": 1,
                 "region": {"x0": 0.888, "x1": 0.985, "y0": 0.900, "y1": 0.985}},
        "soc": {"label": "SOC 百分比", "unit": "%", "decimals": 0,
                "region": {"x0": 0.090, "x1": 0.155, "y0": 0.895, "y1": 0.990}},
    }

    def _metric_region_path(self):
        """多指标读数区域的文件路径。"""
        return os.path.join(paths.DATA_DIR, "screen_regions.json")

    def _load_read_regions(self):
        """读取各指标读数区域；缺的用默认值（兼容旧的单区域文件）。"""
        regions = {key: dict(defn["region"]) for key, defn in self.METRIC_DEFS.items()}
        old_path = os.path.join(paths.DATA_DIR, "screen_region.json")
        try:
            with open(self._metric_region_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for key, region in (data or {}).items():
                if key not in regions or not isinstance(region, dict):
                    continue
                clean = {k: float(region[k]) for k in ("x0", "x1", "y0", "y1")}
                if 0 <= clean["x0"] < clean["x1"] <= 1 and 0 <= clean["y0"] < clean["y1"] <= 1:
                    regions[key] = clean
        except (OSError, ValueError, KeyError, TypeError):
            try:
                with open(old_path, "r", encoding="utf-8") as handle:
                    old = json.load(handle)
                clean = {k: float(old[k]) for k in ("x0", "x1", "y0", "y1")}
                regions["voltage"] = clean
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return regions

    def set_read_region(self, metric, region):
        """保存某个指标的手动圈选区域；电压区域变化会作废图像基线。"""
        if metric not in self.METRIC_DEFS:
            return False, "未知指标 %r" % (metric,)
        clean = {}
        for key in ("x0", "x1", "y0", "y1"):
            clean[key] = float((region or {}).get(key))
        if not (0 <= clean["x0"] < clean["x1"] <= 1 and 0 <= clean["y0"] < clean["y1"] <= 1):
            return False, "区域必须在 0~1 之间且 x0<x1、y0<y1"
        if (clean["x1"] - clean["x0"]) < 0.01 or (clean["y1"] - clean["y0"]) < 0.01:
            return False, "区域太小（宽高至少 1% 画面）"
        regions = dict(self.read_regions)
        changed = regions.get(metric) != clean
        regions[metric] = clean
        self.read_regions = regions
        try:
            with open(self._metric_region_path(), "w", encoding="utf-8") as handle:
                json.dump(regions, handle, ensure_ascii=False)
        except OSError as exc:
            return False, "区域写盘失败: %s" % exc
        if changed and metric == "voltage":
            self.screen_baseline = None
            try:
                if os.path.isfile(self._baseline_path()):
                    os.remove(self._baseline_path())
            except OSError:
                pass
            self.bus.warn("screen", "电压读数区域已改，旧图像基线已清空，请重新「学基线」")
        self._dirty.set()
        return True, clean

    def reset_read_region(self, metric=None):
        """恢复默认读数区域（不传指标则全部恢复）。"""
        if metric is None:
            for key, defn in self.METRIC_DEFS.items():
                self.set_read_region(key, dict(defn["region"]))
            return True, dict(self.read_regions)
        return self.set_read_region(metric, dict(self.METRIC_DEFS[metric]["region"]))

    # -- 画面基线（电压区域比对） -----------------------------------------
    def _baseline_path(self):
        """基线文件路径。"""
        return os.path.join(paths.DATA_DIR, "screen_baseline.json")

    def _load_screen_baseline(self):
        """读取画面基线（v2：多条；v1：单条自动升级）。"""
        try:
            with open(self._baseline_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if data.get("items"):
            items = [item for item in data["items"] if item.get("grid")]
            return {"version": 2, "items": items} if items else None
        if data.get("grid"):
            return {"version": 2, "items": [{
                "grid": data["grid"], "rect": data.get("rect"),
                "time": data.get("time"), "shot": data.get("shot"),
                "note": data.get("note") or "旧版单条基线",
            }]}
        return None

    def _save_screen_baseline(self):
        """把当前基线写盘。"""
        try:
            with open(self._baseline_path(), "w", encoding="utf-8") as handle:
                json.dump(self.screen_baseline or {"items": []}, handle, ensure_ascii=False)
            return True
        except OSError as exc:
            self.bus.warn("studio", "基线写盘失败: %s" % exc)
            return False

    def learn_screen_baseline(self, label="电压基线", value=None):
        """抓一帧当基线（电压数字区域），供 ``screen_region`` / ``screen_text`` 断言比对。

        ``value`` 是这张画面上显示的电压数字（伏）。带了数值的基线才能被
        ``screen_text`` 用来判断"画面显示的是不是 X.X V"。
        支持多条：同一数值在不同主题/不同负载下各学一条；距离很近的会替换，
        最多保留 ``BASELINE_MAX`` 条。
        """
        from .adb import grid_distance_norm
        ok, shot = self.capture_screen(label)
        if not ok:
            return False, shot
        stats = shot.get("stats") or {}
        grid = stats.get("voltage_grid")
        if not grid:
            return False, "这一帧没取到电压区域网格"
        try:
            number = None if value is None or str(value).strip() == "" else float(value)
        except (TypeError, ValueError):
            return False, "基线数值必须是数字（伏）"
        item = {
            "grid": grid,
            "rect": stats.get("voltage_rect"),
            "value": number,
            "time": paths.iso_now(),
            "t_ms": paths.now_ms(),
            "shot": shot["name"],
            "note": label,
        }
        baseline = self.screen_baseline or {"version": 2, "items": []}
        items = baseline.setdefault("items", [])
        replaced = False
        for index, old in enumerate(items):
            if grid_distance_norm(grid, old.get("grid") or []) < 0.05:
                items[index] = item
                replaced = True
                break
        if not replaced:
            items.append(item)
            if len(items) > self.BASELINE_MAX:
                items.pop(0)
        self.screen_baseline = baseline
        self._save_screen_baseline()
        self._dirty.set()
        self.bus.action("screen", "已学习画面基线（%s，共 %d 条）：%s"
                        % ("替换" if replaced else "新增", len(items), shot["name"]))
        return True, {"items": len(items), "shot": shot["name"], "rect": item["rect"]}

    def screen_baseline_diff(self, shot):
        """当前抓屏与**最近**的一条基线的差异（没基线返回 ``None``）。"""
        if not self.screen_baseline or not shot:
            return None
        from .adb import grid_distance_norm
        grid = ((shot.get("stats") or {}).get("voltage_grid")) or []
        items = self.screen_baseline.get("items") or []
        if not grid or not items:
            return None
        return grid_distance_norm(grid, items[-1].get("grid") or [])

    def screen_baseline_best(self, shot, labeled=None):
        """当前抓屏最匹配的一条基线：``(距离, 基线项)``；没基线返回 ``None``。

        ``labeled=True`` 只比"带数值标注"的样本（数值识别用），
        ``labeled=False`` 只比"无标注"的样本（纯图像一致性用），``None`` 不限。
        """
        if not self.screen_baseline or not shot:
            return None
        from .adb import grid_distance_norm
        grid = ((shot.get("stats") or {}).get("voltage_grid")) or []
        items = self.screen_baseline.get("items") or []
        if not grid or not items:
            return None
        best = None
        for item in items:
            has_value = item.get("value") is not None
            if labeled is True and not has_value:
                continue
            if labeled is False and has_value:
                continue
            dist = grid_distance_norm(grid, item.get("grid") or [])
            if best is None or dist < best[0]:
                best = (dist, item)
        return best

    def screen_baseline_diff_any(self, shot):
        """当前抓屏与**无标注基线**的最近差异（没基线返回 ``None``）。"""
        best = self.screen_baseline_best(shot, labeled=False)
        return None if best is None else best[0]

    # -- 模拟模式下的"电源 -> 在线"映射 ----------------------------------
    def _channel_on(self, channel_id):
        """通道当前是否给电（模拟模式用）。"""
        item = self.channels.get(channel_id)
        return bool(item and item.state == "on")

    def _sim_power_on(self):
        """MCU 由主电供电：主电在 = 串口有日志。"""
        return self._channel_on("power")

    def _sim_soc_on(self):
        """SOC 需要主电 + IG：两者都在才算在线。"""
        return self._channel_on("power") and self._channel_on("ig")

    # -- 启动/停止 ---------------------------------------------------------
    def bootstrap(self, start_probes=True, connect_can=False):
        """启动时的初始化：继电器、探针、（可选）CAN。"""
        ok, message = self.relay.open()
        self.bus.info("studio", "%s %s 启动于 %s" % (STUDIO_NAME, VERSION, self.host + ":" + str(self.port)))
        if ok:
            self.bus.info("relay", "继电器就绪: %s" % message)
        else:
            self.bus.warn("relay", "继电器未就绪: %s（上下电动作会失败，可先干跑）" % message)
        if start_probes:
            self.probes.start(None)
            self.bus.info("studio", "探针已启动（串口 %s / adb %s）"
                          % (self.probes.serial.port,
                             "启用" if self.probes.adb.enabled else "停用"))
        if connect_can and (self.config.get("can") or {}).get("enabled", True):
            self.connect_can()
        if self.simulate:
            self._sim_assume_powered()
        self.cleanup_runs()

    def _sim_assume_powered(self):
        """模拟模式：假定台架处于"整机已上电"，让界面一打开就有数据流。"""
        for channel_id in ("power", "acc", "ig"):
            channel = self.channels.get(channel_id)
            if channel is not None:
                channel.state = "on"
        self.bus.info("studio", "模拟模式：假设整机已上电（power/acc/ig = on）")

    def shutdown(self):
        """进程退出前的收尾。"""
        try:
            self.runner.stop("工作台退出")
        except Exception:
            pass
        try:
            self.can.stop()
        except Exception:
            pass
        try:
            self.probes.stop()
        except Exception:
            pass
        try:
            self.relay.close()
        except Exception:
            pass
        self.bus.close()

    # -- 场景 -------------------------------------------------------------
    def _load_default_scenario(self):
        """默认载入：优先上下电压测，其次冒烟，最后按修改时间取第一个。"""
        files = scenario_mod.list_files()
        if files:
            preferred = ("整机上下电压测", "冒烟_上电自检")
            chosen = None
            for name in preferred:
                chosen = next((item for item in files if item.get("name") == name), None)
                if chosen:
                    break
            if chosen is None:
                chosen = files[0]
            try:
                self.current_scenario = scenario_mod.load_file(
                    os.path.join(paths.SCENARIO_DIR, chosen["file"]))
                return
            except Exception:
                pass
        self.current_scenario = scenario_mod.normalize(
            scenario_mod.builtin_scenarios()[0])

    # -- 事件回调 ---------------------------------------------------------
    def _on_rule_incident(self, payload):
        """监看规则命中 -> 保全现场 -> 通知执行器。"""
        state = payload.get("state") or ""
        incident = self.incidents.capture(
            payload.get("rule") or "监看规则命中",
            rule=payload,
            severity=payload.get("severity") or "error",
            detail=payload.get("detail") or "",
            state=state,
            freeze=payload.get("freeze", True))
        self.runner.notify_incident(dict(payload, incident=incident))
        self._dirty.set()

    def runner_incident(self, incident):
        """执行器自己抓的事故（断言失败）：只做界面提示。"""
        self._dirty.set()
        return incident

    def _on_can_update(self, _rx):
        """CAN 接收快照到达：标记界面刷新（限流到 1Hz）。"""
        self._dirty.set()

    # -- CAN 会话 ---------------------------------------------------------
    def connect_can(self):
        """连接 CAN 设备；返回 ``(ok, message)``。"""
        ok, message = self.can.start()
        self._dirty.set()
        return ok, message

    def ensure_can(self, need_tx=False):
        """确保 CAN 已连接（需要发送时再载入清单）。"""
        if self.can.state != "running":
            ok, message = self.connect_can()
            if not ok:
                return False, message
        return True, "CAN 已连接"

    def start_can_for(self, scenario):
        """按剧本的 CAN 配置准备发送/接收。"""
        can_cfg = (scenario or {}).get("can") or {}
        mode = can_cfg.get("mode") or "none"
        if mode == "none":
            return True, "剧本未使用 CAN"
        ok, message = self.ensure_can()
        if not ok:
            return False, message
        run_dir = self.runner.run_dir
        if run_dir:
            self.can.enable_rx_log(os.path.join(run_dir, "can_rx.csv"))
        if mode == "tx":
            buses = can_cfg.get("buses") or list(self.can.channels.keys())
            try:
                self.can.load_manifest(None, {
                    "buses": buses,
                    "rand_mode": can_cfg.get("rand_mode", "per-frame"),
                    "rand_interval_ms": can_cfg.get("rand_interval_ms", 1000),
                    "cycle_scale": can_cfg.get("cycle_scale", 1.0),
                })
                self.can.start_tx()
            except Exception as exc:
                return False, str(exc)
        return True, "CAN %s 就绪" % mode

    def start_can_tx(self):
        """启动随机发送（剧本动作 / 界面按钮都用它）。"""
        ok, message = self.ensure_can()
        if not ok:
            return False, message
        try:
            if not (self.can.tx.get("jobs") or 0):
                self.can.load_manifest(None, {})
            self.can.start_tx()
        except Exception as exc:
            return False, str(exc)
        for channel in self.channels.snapshot():
            if channel["kind"] == "can":
                item = self.channels.get(channel["id"])
                if item is not None:
                    item.state = "on"
        self._dirty.set()
        return True, "CAN 随机发送已启动"

    def stop_can_tx(self):
        """停止随机发送。"""
        try:
            self.can.stop_tx()
        except Exception as exc:
            return False, str(exc)
        for channel in self.channels.snapshot():
            if channel["kind"] == "can":
                item = self.channels.get(channel["id"])
                if item is not None:
                    item.state = "off"
        self._dirty.set()
        return True, "CAN 随机发送已停止"

    # -- 电源状态机激励 ---------------------------------------------------
    def start_stimulus(self, mode, bus=None, period_ms=100, can_id=None, data=None):
        """启动激励（any/wake/frame）。返回 ``(ok, message)``。"""
        ok, message = self.ensure_can()
        if not ok:
            return False, message
        result = self.can.start_stimulus(mode, bus=bus, period_ms=period_ms,
                                         can_id=can_id, data=data)
        self._dirty.set()
        return bool(result.get("ok")), result.get("message", "")

    def stop_stimuli(self):
        """停止全部激励（网络转静默）。"""
        if self.can.state != "running":
            return False, "CAN 引擎未运行"
        result = self.can.stop_stimulus()
        self._dirty.set()
        return bool(result.get("ok")), result.get("message", "")

    # -- 快照 -------------------------------------------------------------
    def snapshot(self, compact=False):
        """给界面的完整状态；``compact`` 时裁掉大表（SSE 每秒用）。"""
        runner = self.runner.snapshot()
        channels = self.channels.snapshot()
        probe = self.probes.snapshot()
        can_snap = self.can.snapshot()
        if compact:
            can_snap = dict(can_snap)
            buses = {}
            for bus, item in (can_snap.get("buses") or {}).items():
                buses[bus] = dict(item)
            can_snap["buses"] = buses
            can_snap.pop("rx", None)
        incidents = self.incidents.items()
        return {
            "version": VERSION,
            "name": STUDIO_NAME,
            "host": self.host,
            "port": self.port,
            "uptime_s": int(time.time() - self.started_at),
            "simulate": self.simulate,
            "runner": runner,
            "channels": channels,
            "probe": probe,
            "can": can_snap,
            "relay": self.relay.describe(),
            "counters": self.bus.counters(),
            "incidents": incidents,
            "can_ids": self._can_ids_preview(limit=60 if compact else 400),
            "screens": list(self.screens)[:6],
            "read_region": self.read_region,
            "read_regions": {k: dict(v) for k, v in self.read_regions.items()},
            "metric_defs": {k: {"label": d["label"], "unit": d["unit"], "decimals": d["decimals"]} for k, d in self.METRIC_DEFS.items()},
            "screen_baseline": ({"count": len(self.screen_baseline.get("items") or []),
                                 "last_time": (self.screen_baseline.get("items") or [{}])[-1].get("time"),
                                 "last_shot": (self.screen_baseline.get("items") or [{}])[-1].get("shot")}
                                if self.screen_baseline else None),
            "scenario": self.current_scenario,
            "scenario_files": scenario_mod.list_files(),
            "channels_config": self.config.get("channels"),
            "settings": {
                "relay": self.config.get("relay"),
                "can": self.config.get("can"),
                "probes": self.config.get("probes"),
                "incident": self.config.get("incident"),
                "retention": self.config.get("retention"),
            },
            "paths": {"root": paths.STUDIO_DIR, "runs": paths.RUNS_DIR,
                      "scenarios": paths.SCENARIO_DIR},
            "run_dir": runner.get("run_dir") or self.incidents.run_dir,
        }

    def _can_ids_preview(self, limit=200):
        """把每路 CAN 的 ID 统计压成列表（按窗口帧数排序）。"""
        result = {}
        for bus in (self.can.channels or {}):
            ids = self.can.rx_ids(bus)
            rows = []
            for can_id, item in ids.items():
                rows.append({
                    "id": can_id,
                    "n": item.get("n", 0),
                    "win": item.get("win", 0),
                    "age_ms": item.get("age_ms", 0),
                    "period_ms": item.get("period_ms", 0),
                    "dlc": item.get("dlc", 0),
                    "data": item.get("data", ""),
                })
            rows.sort(key=lambda row: (row["win"], row["n"]), reverse=True)
            result[bus] = rows[:limit]
        return result

    def mark_dirty(self):
        """标记"有变化"，SSE 下一拍立刻推快照。"""
        self._dirty.set()

    def take_dirty(self):
        """取走脏标记（SSE 用）。"""
        if not self._dirty.is_set():
            return False
        self._dirty.clear()
        return True

    # -- 动作分派 ---------------------------------------------------------
    def dispatch(self, op, payload=None):
        """执行一条界面动作；返回 ``{"ok":bool,"message":str,...}``。"""
        payload = dict(payload or {})
        handler = getattr(self, "_op_" + str(op).replace(".", "_"), None)
        if handler is None:
            return {"ok": False, "message": "未知动作 %r" % (op,)}
        try:
            result = handler(payload)
            self._dirty.set()
            if result is None:
                result = {"ok": True}
            result.setdefault("ok", True)
            return result
        except Exception as exc:
            self.bus.error("studio", "动作 %s 失败: %s" % (op, exc))
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    # ---- 通道 ----
    def _op_channel_set(self, payload):
        state = payload.get("state")
        on = str(state).lower() in ("on", "true", "1", "give")
        return self.channels.set(payload.get("id"), on, source="手动")

    def _op_preset(self, payload):
        results = self.channels.preset(payload.get("name"), source="手动")
        failed = [item for item in results if not item.get("ok")]
        return {"ok": not failed, "message": "已完成" if not failed else failed[0].get("message"),
                "results": results}

    # ---- 运行控制 ----
    def _op_run_start(self, payload):
        scenario = payload.get("scenario") or self.current_scenario
        errors, _warnings = scenario_mod.validate(scenario, self.channels.ids())
        if errors:
            return {"ok": False, "message": "剧本校验失败: " + "；".join(errors)}
        scenario = scenario_mod.normalize(scenario)
        self.current_scenario = scenario
        ok, message = self.runner.start(scenario)
        return {"ok": ok, "message": message}

    def _op_run_stop(self, payload):
        ok = self.runner.stop(payload.get("reason") or "手动停止")
        return {"ok": ok, "message": "已请求停止" if ok else "当前没有运行中的剧本"}

    def _op_run_pause(self, payload):
        ok = self.runner.pause()
        return {"ok": ok, "message": "已暂停" if ok else "当前不能暂停"}

    def _op_run_resume(self, payload):
        ok = self.runner.resume()
        return {"ok": ok, "message": "已恢复" if ok else "当前不能恢复"}

    def _op_run_freeze(self, payload):
        reason = payload.get("reason") or "手动保留现场"
        incident = self.incidents.capture(reason, severity="warn",
                                          detail=payload.get("detail") or "",
                                          state=self.runner.state_of(), freeze=True)
        return {"ok": True, "message": "现场已保全", "incident": incident}

    def _op_screen_set_region(self, payload):
        """手动圈选某指标的读数区域（按整帧比例）；region 为空表示恢复默认。"""
        metric = (payload.get("metric") or "voltage").strip()
        region = payload.get("region")
        if not region:
            ok, data = self.reset_read_region(metric)
        else:
            ok, data = self.set_read_region(metric, region)
        if not ok:
            return {"ok": False, "message": data}
        label = self.METRIC_DEFS.get(metric, {}).get("label", metric)
        return {"ok": True, "message": "%s 读数区域已更新" % label,
                "region": data, "metric": metric,
                "regions": {k: dict(v) for k, v in self.read_regions.items()}}

    def _op_screen_capture(self, payload):
        """抓一帧仪表画面（界面按钮 / 检测用）。"""
        ok, shot = self.capture_screen(payload.get("label") or "手动抓屏")
        if not ok:
            return {"ok": False, "message": shot}
        return {"ok": True, "message": "已抓屏", "screen": shot}

    def _op_screen_learn_baseline(self, payload):
        """把当前画面学成基线（电压区域比对用）；可带 value=画面上的电压数字。"""
        ok, data = self.learn_screen_baseline(payload.get("label") or "电压基线",
                                              payload.get("value"))
        if not ok:
            return {"ok": False, "message": data}
        return {"ok": True, "message": "已学习基线（共 %d 条）" % data["items"],
                "baseline": {"count": data["items"], "shot": data["shot"],
                             "rect": data["rect"]}}

    # ---- 剧本 ----
    def _op_scenario_save(self, payload):
        scenario = payload.get("scenario") or self.current_scenario
        errors, warnings = scenario_mod.validate(scenario, self.channels.ids())
        if errors:
            return {"ok": False, "message": "；".join(errors)}
        path = scenario_mod.save_file(scenario, payload.get("name"))
        self.current_scenario = scenario_mod.load_file(path)
        self.bus.action("studio", "剧本已保存: %s" % self.current_scenario["name"])
        return {"ok": True, "message": "已保存", "scenario": self.current_scenario,
                "warnings": warnings}

    def _op_scenario_load(self, payload):
        name = payload.get("file") or payload.get("name")
        path = os.path.join(paths.SCENARIO_DIR, name if str(name).endswith(".json")
                            else "%s.json" % name)
        if not os.path.isfile(path):
            return {"ok": False, "message": "剧本不存在: %s" % name}
        self.current_scenario = scenario_mod.load_file(path)
        self.bus.info("studio", "载入剧本「%s」" % self.current_scenario.get("name"))
        return {"ok": True, "scenario": self.current_scenario}

    def _op_scenario_new(self, payload):
        self.current_scenario = scenario_mod.new_scenario(payload.get("name") or "新剧本")
        return {"ok": True, "scenario": self.current_scenario}

    def _op_scenario_delete(self, payload):
        ok = scenario_mod.delete_file(payload.get("name") or payload.get("file"))
        return {"ok": ok, "message": "已删除" if ok else "未找到该剧本"}

    def _op_scenario_validate(self, payload):
        errors, warnings = scenario_mod.validate(payload.get("scenario") or self.current_scenario,
                                                 self.channels.ids())
        return {"ok": not errors, "errors": errors, "warnings": warnings,
                "estimate_ms": scenario_mod.estimate_ms(payload.get("scenario")
                                                        or self.current_scenario)}

    def _op_scenario_select(self, payload):
        """界面编辑中的剧本（不落盘，只改当前）。"""
        self.current_scenario = scenario_mod.normalize(payload.get("scenario") or {})
        return {"ok": True}

    # ---- 规则 ----
    def _op_rules_save(self, payload):
        rules = payload.get("rules") or []
        self.probes.set_rules(rules)
        self.config.setdefault("probes", {})["rules"] = rules
        config_mod.save(self.config)
        self.bus.action("studio", "监看规则已更新（%d 条）" % len(rules))
        return {"ok": True, "rules": [dict(item) for item in self.probes.rules.rules]}

    # ---- 设置 ----
    def _op_settings_save(self, payload):
        settings = payload.get("settings") or {}
        merged = config_mod._merge(self.config, settings)
        self.config = merged
        config_mod.save(self.config)
        self.relay.dry_run = bool((self.config.get("relay") or {}).get("dry_run"))
        self.relay.port = (self.config.get("relay") or {}).get("port") or self.relay.port
        self.channels.reload(self.config)
        self.probes.adb.interval = max(0.3, float(
            (self.config.get("probes") or {}).get("adb", {}).get("interval_ms", 1000)) / 1000.0)
        serial_cfg = (self.config.get("probes") or {}).get("serial") or {}
        self.probes.serial.reconfigure(serial_cfg)
        self.can.configure(self.config.get("can") or {})
        self.incidents.configure(self.config)
        self.bus.action("studio", "设置已保存")
        return {"ok": True, "settings": self.config}

    # ---- 探针 ----
    def _op_probe_restart(self, payload):
        if payload.get("serial"):
            self.probes.serial.reconfigure(payload["serial"])
        self.probes.serial.stop()
        self.probes.serial.state = "closed"
        self.probes.serial.start()
        return {"ok": True, "message": "串口探针已重启"}

    def _op_probe_mark(self, payload):
        self.probes.mark(payload.get("text") or "人工标记")
        return {"ok": True}

    def _op_log_export(self, payload):
        """把当前环形日志导出成 CSV（落在运行目录或 data/）。"""
        import csv
        target_dir = self.incidents.run_dir or paths.DATA_DIR
        name = "log_%s.csv" % time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(target_dir, name)
        events = self.bus.tail(60000)
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["seq", "time", "level", "source", "state", "evt", "text"])
            for item in events:
                writer.writerow([item.get("seq"), paths.fmt_ts(item.get("t")),
                                 item.get("level"), item.get("source"),
                                 item.get("st"), item.get("evt") or "",
                                 item.get("text")])
        self.bus.action("studio", "日志已导出: %s" % path)
        return {"ok": True, "path": path, "count": len(events)}

    # ---- CAN ----
    def _op_can_connect(self, payload):
        ok, message = self.connect_can()
        return {"ok": ok, "message": message}

    def _op_can_disconnect(self, payload):
        self.can.stop()
        return {"ok": True, "message": "CAN 已断开"}

    def _op_can_tx_start(self, payload):
        ok, message = self.start_can_tx()
        return {"ok": ok, "message": message}

    def _op_can_tx_stop(self, payload):
        ok, message = self.stop_can_tx()
        return {"ok": ok, "message": message}

    def _op_can_reload_manifest(self, payload):
        ok, message = self.ensure_can()
        if not ok:
            return {"ok": False, "message": message}
        data = self.can.load_manifest(None, {})
        return {"ok": True, "message": "已载入 %d 条报文" % data.get("jobs", 0)}

    def _op_can_send_frame(self, payload):
        """手动发一帧（唤醒帧 / 单帧模拟）；参数: bus,id,data,period_ms,count。"""
        bus = payload.get("bus") or "ican"
        raw_id = str(payload.get("id") or "").strip()
        try:
            can_id = int(raw_id, 0)
        except ValueError:
            return {"ok": False, "message": "报文 ID 格式不对: %r" % raw_id}
        data = str(payload.get("data") or "").replace(",", " ")
        clean = data.replace(" ", "")
        if len(clean) % 2 != 0 or (clean and any(c not in "0123456789abcdefABCDEF" for c in clean)):
            return {"ok": False, "message": "数据应为偶数个十六进制字符，如 00 50 00 00 00 00 00 00"}
        if len(clean) // 2 > 8:
            return {"ok": False, "message": "经典 CAN 最多 8 字节"}
        return self.can.send_frame(bus, can_id, data,
                                   int(payload.get("period_ms") or 100),
                                   int(payload.get("count") or 1))

    def _op_can_stim(self, payload):
        """电源状态机激励：{action: start|stop, mode, bus, period_ms, id, data}。"""
        action = str(payload.get("action") or "start").lower()
        if action == "stop":
            ok, message = self.stop_stimuli()
            return {"ok": ok, "message": message}
        mode = str(payload.get("mode") or "any").lower()
        raw_id = str(payload.get("id") or "").strip()
        can_id = None
        if raw_id:
            try:
                can_id = int(raw_id, 0)
            except ValueError:
                return {"ok": False, "message": "报文 ID 格式不对: %r" % raw_id}
        ok, message = self.start_stimulus(
            mode, bus=payload.get("bus") or None,
            period_ms=int(payload.get("period_ms") or 100),
            can_id=can_id, data=payload.get("data") or None)
        return {"ok": ok, "message": message, "mode": mode}

    # ---- 历史 / 目录 ----
    def _op_incident_open(self, payload):
        ok = self.incidents.open_folder(payload.get("dir") or payload.get("folder"))
        return {"ok": ok, "message": "" if ok else "目录不存在"}

    def _op_run_open(self, payload):
        target = payload.get("dir") or self.incidents.run_dir
        return {"ok": self.incidents.open_folder(target)}

    def _op_runs_list(self, payload):
        return {"ok": True, "runs": self.list_runs()}

    def _op_runs_delete(self, payload):
        run_id = payload.get("id")
        target = paths.run_dir(run_id)
        if not os.path.isdir(target):
            return {"ok": False, "message": "没有这个运行目录"}
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        self.bus.action("studio", "已删除运行目录 %s" % run_id)
        return {"ok": True}

    def list_runs(self, limit=200):
        """运行历史（按时间倒序）。"""
        items = []
        if not os.path.isdir(paths.RUNS_DIR):
            return items
        for entry in sorted(os.listdir(paths.RUNS_DIR), reverse=True)[:limit]:
            path = os.path.join(paths.RUNS_DIR, entry)
            if not os.path.isdir(path):
                continue
            incidents_dir = os.path.join(path, "incidents")
            count = 0
            if os.path.isdir(incidents_dir):
                count = len([name for name in os.listdir(incidents_dir)
                             if os.path.isdir(os.path.join(incidents_dir, name))])
            summary = {}
            summary_path = os.path.join(path, "run.json")
            if os.path.isfile(summary_path):
                try:
                    import json
                    with open(summary_path, "r", encoding="utf-8") as handle:
                        summary = json.load(handle)
                except (OSError, ValueError):
                    summary = {}
            items.append({
                "id": entry,
                "dir": path,
                "mtime": int(os.path.getmtime(path)),
                "size": paths.dir_size(path),
                "incidents": count,
                "scenario": summary.get("scenario"),
                "iterations": summary.get("iterations"),
                "elapsed_ms": summary.get("elapsed_ms"),
            })
        return items

    def cleanup_runs(self):
        """按保留策略清理旧运行目录。"""
        retention = self.config.get("retention") or {}
        max_runs = int(retention.get("max_runs") or 0)
        max_days = int(retention.get("max_days") or 0)
        if not max_runs and not max_days:
            return
        entries = []
        for entry in sorted(os.listdir(paths.RUNS_DIR)) if os.path.isdir(paths.RUNS_DIR) else []:
            path = os.path.join(paths.RUNS_DIR, entry)
            if os.path.isdir(path):
                entries.append((os.path.getmtime(path), path))
        entries.sort(reverse=True)
        import shutil
        removed = 0
        now = time.time()
        for index, (mtime, path) in enumerate(entries):
            too_many = max_runs and index >= max_runs
            too_old = max_days and (now - mtime) > max_days * 86400
            if too_many or too_old:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        if removed:
            self.bus.info("studio", "按保留策略清理了 %d 个旧运行目录" % removed)


