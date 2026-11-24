# -*- coding: utf-8 -*-
"""channels.py — 通道抽象：继电器通道 / CAN 通道 / 自定义命令通道

界面与剧本只跟"通道"打交道，不关心底层是继电器、ZLG 通道还是别的外部命令：

===================  =========================================================
kind                 语义
===================  =========================================================
``relay``            LCUS-4 的某个通道（1=主电 2=ACC 3=IG 4=备用），on=给电
``can``              某路 CAN 的发送闸门（off = 暂停该总线发送，接收仍在）
``custom``           自定义 shell 命令（on/off 各一条），用于扩展台架上别的设备
===================  =========================================================

通道状态是**推定值**：继电器没有回读，CAN 闸门以引擎回报为准，自定义命令按退出码。
"""

import subprocess
import threading

from . import relay as relay_mod


class Channel(object):
    """一路可控通道的静态定义 + 动态状态。"""

    def __init__(self, spec):
        self.id = str(spec.get("id") or "").strip()
        self.label = str(spec.get("label") or self.id)
        self.kind = str(spec.get("kind") or "relay").strip().lower()
        self.relay = int(spec.get("relay") or 0)
        self.bus = str(spec.get("bus") or "")          # CAN 通道对应的引擎总线名
        self.on_cmd = str(spec.get("on_cmd") or "")
        self.off_cmd = str(spec.get("off_cmd") or "")
        self.verify_cmd = str(spec.get("verify_cmd") or "")
        self.default_on = bool(spec.get("default_on"))
        self.state = "on" if self.default_on else "off"
        self.available = True
        self.detail = ""
        self.error = None

    def as_dict(self):
        """界面用序列化（不含命令全文，避免泄露过长文本）。"""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "relay": self.relay,
            "bus": self.bus,
            "state": self.state,
            "available": self.available,
            "detail": self.detail,
            "error": self.error,
            "has_command": bool(self.on_cmd or self.off_cmd),
        }


DEFAULT_CHANNELS = [
    {"id": "power", "label": "主电 POWER", "kind": "relay", "relay": 1, "default_on": False},
    {"id": "acc", "label": "ACC", "kind": "relay", "relay": 2, "default_on": False},
    {"id": "ig", "label": "IG", "kind": "relay", "relay": 3, "default_on": False},
    {"id": "relay4", "label": "继电器 4", "kind": "relay", "relay": 4, "default_on": False},
    {"id": "ican", "label": "ICAN 发送", "kind": "can", "bus": "ican", "default_on": False},
    {"id": "pcan", "label": "PCAN 发送", "kind": "can", "bus": "pcan", "default_on": False},
]


class ChannelManager(object):
    """通道集合的持有者：解析配置、转发动作、给出界面快照。"""

    def __init__(self, config, bus, relay=None, can=None):
        self._log = bus
        self._lock = threading.RLock()
        self._relay = relay or relay_mod.RelayController(log=bus)
        self._can = can
        self._channels = []
        self._by_id = {}
        self.reload(config)

    # -- 配置 -------------------------------------------------------------
    def reload(self, config):
        """按配置重建通道表（保留已知状态）。"""
        specs = (config or {}).get("channels") or DEFAULT_CHANNELS
        with self._lock:
            previous = {cid: ch.state for cid, ch in self._by_id.items()}
            self._channels = []
            self._by_id = {}
            for spec in specs:
                channel = Channel(spec)
                if channel.id in previous:
                    channel.state = previous[channel.id]
                if channel.kind == "relay":
                    channel.detail = "LCUS-4 CH%d" % channel.relay
                elif channel.kind == "can":
                    channel.detail = "ZLG %s" % (channel.bus or "?")
                else:
                    channel.detail = "自定义命令"
                self._channels.append(channel)
                self._by_id[channel.id] = channel
        self._sync_relay_states()

    def _sync_relay_states(self):
        """把继电器推定状态同步到通道状态（界面刷新用）。"""
        states = self._relay.states if self._relay else {}
        for channel in self._channels:
            if channel.kind != "relay" or not channel.relay:
                continue
            if channel.relay in states:
                channel.state = "on" if states[channel.relay] else "off"

    def set_controller(self, relay=None, can=None):
        """注入/替换底层控制器（服务启动时装配）。"""
        if relay is not None:
            self._relay = relay
        if can is not None:
            self._can = can

    @property
    def relay(self):
        """当前继电器控制器。"""
        return self._relay

    def get(self, channel_id):
        """按 id 取通道对象。"""
        return self._by_id.get(str(channel_id))

    def ids(self):
        """全部通道 id（配置顺序）。"""
        return [channel.id for channel in self._channels]

    # -- 快照 -------------------------------------------------------------
    def snapshot(self):
        """界面用通道列表。"""
        with self._lock:
            self._sync_relay_states()
            items = []
            for channel in self._channels:
                item = channel.as_dict()
                if channel.kind == "can" and self._can is not None:
                    info = self._can.bus_info(channel.bus) or {}
                    item["available"] = bool(info.get("available", False))
                    detail = info.get("detail")
                    if info.get("state") != "running" and not detail:
                        detail = "未连接"
                    item["detail"] = detail or item["detail"]
                elif channel.kind == "relay":
                    item["available"] = bool(self._relay and self._relay.is_open)
                items.append(item)
            return items

    # -- 动作 -------------------------------------------------------------
    def set(self, channel_id, on, source="manual"):
        """把通道切到目标状态；返回结果 dict（``ok``/``message``/``channel``）。"""
        channel = self.get(channel_id)
        if channel is None:
            return {"ok": False, "message": "未知通道 %r" % (channel_id,), "id": channel_id}
        on = bool(on)
        if channel.kind == "relay":
            ok, message = self._relay.set_channel(channel.relay, on) if self._relay else (
                False, "继电器未初始化")
            with self._lock:
                channel.state = "on" if on else "off"
                channel.error = None if ok else message
            self._log.action("channel", "%s -> %s（%s）" % (
                channel.label, "给电" if on else "断电", source),
                channel=channel.id, state=channel.state, ok=ok)
            return {"ok": ok, "message": message, "id": channel.id, "state": channel.state}

        if channel.kind == "can":
            if self._can is None:
                return {"ok": False, "message": "CAN 引擎未初始化", "id": channel.id}
            result = self._can.set_bus_tx(channel.bus, on)
            ok = bool(result.get("ok"))
            with self._lock:
                if ok:
                    channel.state = "on" if on else "off"
                channel.error = None if ok else result.get("message")
            self._log.action("channel", "%s -> %s（%s）" % (
                channel.label, "发送开" if on else "发送暂停", source),
                channel=channel.id, state=channel.state, ok=ok)
            return {"ok": ok, "message": result.get("message", ""),
                    "id": channel.id, "state": channel.state}

        # custom：执行用户命令
        command = channel.on_cmd if on else channel.off_cmd
        if not command:
            return {"ok": False, "message": "通道 %s 未配置命令" % channel.id, "id": channel.id}
        ok, message = self._run_command(command)
        with self._lock:
            channel.state = "on" if on else "off"
            channel.error = None if ok else message
        self._log.action("channel", "%s -> %s（%s）%s" % (
            channel.label, "ON" if on else "OFF", source, "" if ok else " 失败"),
            channel=channel.id, state=channel.state, ok=ok)
        return {"ok": ok, "message": message, "id": channel.id, "state": channel.state}

    def _run_command(self, command):
        """执行自定义通道命令（15s 超时，输出进日志）。"""
        try:
            proc = subprocess.run(command, shell=True, capture_output=True,
                                  timeout=15.0, text=True, errors="replace")
        except subprocess.TimeoutExpired:
            return False, "命令超时: %s" % command
        except Exception as exc:
            return False, "命令异常: %s" % exc
        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        if proc.returncode != 0:
            return False, "命令退出码 %d: %s" % (proc.returncode, output[:200])
        if output:
            self._log.info("channel", "命令输出: %s" % output[:400])
        return True, "命令完成"

    def apply_map(self, mapping, order=None, source="scenario"):
        """按映射批量设置通道，返回逐步结果。

        ``mapping`` 形如 ``{"power":"on","acc":"off","ig":"keep"}``；``keep``/缺省
        表示不动。``order`` 为 ``"config"``（默认，主电先上）或 ``"reverse"``
        （下电时先断 IG/ACC 再断主电）。
        """
        wanted = []
        for cid, target in (mapping or {}).items():
            if target in (None, "", "keep"):
                continue
            channel = self.get(cid)
            if channel is None:
                self._log.warn("channel", "剧本引用了不存在的通道 %r（已忽略）" % (cid,))
                continue
            wanted.append((channel, target in ("on", True, 1, "1", "true")))

        if order == "reverse":
            wanted = list(reversed(wanted))
        elif order == "config":
            order_index = {cid: index for index, cid in enumerate(self.ids())}
            wanted.sort(key=lambda item: order_index.get(item[0].id, 999))

        results = []
        for channel, on in wanted:
            results.append(self.set(channel.id, on, source=source))
        return results

    def preset(self, name, source="manual"):
        """预设上下电序列（继电器通道专用）。"""
        results = []
        if self._relay is None:
            return [{"ok": False, "message": "继电器未初始化"}]
        for step in self._relay.preset(name):
            channel = None
            for item in self._channels:
                if item.kind == "relay" and item.relay == step["channel"]:
                    channel = item
                    break
            if channel is not None:
                channel.state = "on" if step["on"] else "off"
                channel.error = None if step["ok"] else step["message"]
            results.append({"id": channel.id if channel else "", "label":
                            channel.label if channel else "CH%d" % step["channel"],
                            "ok": step["ok"], "message": step["message"]})
        label = {"power_on": "整机上电", "power_off": "整机断电",
                 "soc_off": "SOC 掉电（主电保留）", "all_off": "全断"}.get(name, name)
        failed = [item for item in results if not item["ok"]]
        self._log.action("relay", "%s %s" % (label, "完成" if not failed else "有失败"),
                         preset=name, ok=not failed)
        return results
