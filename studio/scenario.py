# -*- coding: utf-8 -*-
"""scenario.py — 剧本模型：状态卡序列、循环、等待、断言

一个 **剧本（scenario）** 就是"状态卡"的有序列表，运行时按顺序逐张执行，
可循环 N 次或无限循环。每张 **状态卡** 描述：

``channels``
    进入该状态时把哪些通道切成什么状态。取值 ``on`` / ``off`` / ``keep``
    （``keep`` 或不写 = 不动）。主电/ACC/IG 的先后顺序由执行器按配置顺序保证。
``hold_ms``
    至少保持多久（ms）。
``wait``
    可选的等待条件（在 ``hold_ms`` 之后继续等，直到满足或超时）::

        {"type":"none"}
        {"type":"adb_online","timeout_ms":8000}
        {"type":"serial_match","value":"\\\\[SpiRx\\\\]","timeout_ms":3000}
        {"type":"can_frame","bus":"ican","value":"0x18FEF117","timeout_ms":3000}
        {"type":"log_growing","timeout_ms":5000}

``checks``
    在该状态窗口内必须成立的条件，不成立即判失败::

        {"type":"no_error"}                       整个窗口内没有 ERROR 级日志
        {"type":"serial_seen","value":"...","expect":true}
        {"type":"can_seen","bus":"ican","value":"0x...","min":1}
        {"type":"adb_online","expect":true}

``on_fail``
    失败/超时后的动作：``continue``（记一笔继续）/ ``stop``（停下剧本）/
    ``freeze``（现场保全后停下）。

``actions``
    进入状态时执行的动作：``can_tx_start`` / ``can_tx_stop`` / ``mark``。
"""

import copy
import json
import os
import re
import uuid

from . import paths

WAIT_TYPES = ("none", "adb_online", "serial_match", "can_frame", "log_growing", "pwr_state")
CHECK_TYPES = ("no_error", "serial_seen", "can_seen", "adb_online", "pwr_state", "pwr_seen",
               "screen", "screen_region", "screen_text", "voltage")
ACTION_TYPES = ("can_tx_start", "can_tx_stop", "mark", "can_stim", "capture")
# 抓屏动作的触发时机：进入状态 / 断言通过 / 断言失败 / 等待超时 / 定时周期
CAPTURE_WHENS = ("entry", "on_pass", "on_fail", "on_timeout", "interval")
STIM_MODES = ("any", "wake", "frame", "silence")
ON_FAIL = ("continue", "stop", "freeze")


def new_id(prefix="s"):
    """生成短 id（拖动排序时用它做前端 key）。"""
    return "%s%s" % (prefix, uuid.uuid4().hex[:6])


def new_state(name="新状态"):
    """一张空白状态卡（只保留"不动通道"）。"""
    return {
        "id": new_id(),
        "name": name,
        "channels": {},
        "hold_ms": 2000,
        "wait": {"type": "none", "timeout_ms": 5000, "value": "", "bus": ""},
        "checks": [],
        "on_fail": "continue",
        "actions": [],
        "note": "",
    }


def new_scenario(name="新剧本"):
    """一个空剧本。"""
    return {
        "name": name,
        "description": "",
        "loops": 1,                 # 0 = 无限循环
        "loop_interval_ms": 1000,
        "stop_on_incident": True,
        "keep_power_on_incident": True,
        # 剧本自己会主动断电时（上下电压测），断电期间的"掉线/静默"类监看
        # 一定命中，属预期行为而非事故；打开本项则在主电断开期间自动静默监看。
        "auto_mute_power_off": True,
        "can": {"mode": "none", "buses": [], "rand_mode": "per-frame",
                "rand_interval_ms": 1000, "cycle_scale": 1.0},
        "states": [],
    }


def normalize(scenario):
    """补齐缺省字段，保证前端与执行器拿到的结构一致。"""
    data = copy.deepcopy(scenario or {})
    base = new_scenario(data.get("name") or "未命名剧本")
    base.update({key: value for key, value in data.items() if value is not None})
    base["states"] = [normalize_state(item) for item in (data.get("states") or [])]
    can = dict(new_scenario()["can"])
    can.update(data.get("can") or {})
    base["can"] = can
    return base


def normalize_state(state):
    """补齐一张状态卡的缺省字段。"""
    item = copy.deepcopy(state or {})
    base = new_state(item.get("name") or "状态")
    base.update({key: value for key, value in item.items() if value is not None})
    wait = dict(new_state()["wait"])
    wait.update(item.get("wait") or {})
    base["wait"] = wait
    base["channels"] = {str(k): v for k, v in (item.get("channels") or {}).items()}
    base["checks"] = [dict(check) for check in (item.get("checks") or [])]
    base["actions"] = [dict(action) for action in (item.get("actions") or [])]
    if not base.get("id"):
        base["id"] = new_id()
    return base


def validate(scenario, channel_ids=None):
    """返回 ``(errors, warnings)`` 文本列表。"""
    errors = []
    warnings = []
    data = normalize(scenario)
    if not data["states"]:
        errors.append("剧本里至少需要一张状态卡")
    if int(data.get("loops", 1)) < 0:
        errors.append("循环次数不能为负（0 表示无限循环）")
    known = set(channel_ids or [])
    seen_names = set()
    for index, state in enumerate(data["states"], 1):
        label = "第 %d 张「%s」" % (index, state.get("name") or "?")
        if not state.get("name"):
            warnings.append("%s 没有名字" % label)
        if state.get("name") in seen_names:
            warnings.append("%s 名字重复" % label)
        seen_names.add(state.get("name"))
        for channel, value in (state.get("channels") or {}).items():
            if known and channel not in known:
                errors.append("%s 引用了不存在的通道 %s" % (label, channel))
            if value not in ("on", "off", "keep"):
                errors.append("%s 通道 %s 的取值 %r 非法（on/off/keep）" % (label, channel, value))
        if int(state.get("hold_ms") or 0) < 0:
            errors.append("%s 的保持时长不能为负" % label)
        wait = state.get("wait") or {}
        if wait.get("type") not in WAIT_TYPES:
            errors.append("%s 的等待类型 %r 非法" % (label, wait.get("type")))
        if wait.get("type") == "serial_match":
            try:
                re.compile(wait.get("value") or "")
            except re.error as exc:
                errors.append("%s 的等待正则无效: %s" % (label, exc))
        for check in state.get("checks") or []:
            if check.get("type") not in CHECK_TYPES:
                errors.append("%s 的断言类型 %r 非法" % (label, check.get("type")))
            if check.get("type") == "serial_seen":
                try:
                    re.compile(check.get("value") or "")
                except re.error as exc:
                    errors.append("%s 的断言正则无效: %s" % (label, exc))
        if state.get("on_fail") not in ON_FAIL:
            errors.append("%s 的失败动作 %r 非法" % (label, state.get("on_fail")))
        for action in state.get("actions") or []:
            if action.get("type") not in ACTION_TYPES:
                errors.append("%s 的动作类型 %r 非法" % (label, action.get("type")))
            if action.get("type") == "can_stim" and action.get("mode") not in STIM_MODES:
                errors.append("%s 的激励模式 %r 非法（any/wake/frame/silence）"
                              % (label, action.get("mode")))
            if action.get("type") == "capture":
                when = action.get("when") or "entry"
                if when not in CAPTURE_WHENS:
                    errors.append("%s 的抓屏时机 %r 非法（entry/on_pass/on_fail/on_timeout/interval）"
                                  % (label, when))
                if when == "interval":
                    try:
                        every = int(action.get("every_ms") or 2000)
                        if every < 500:
                            raise ValueError
                    except (TypeError, ValueError):
                        errors.append("%s 的定时抓屏间隔必须 ≥500ms" % label)
        if wait.get("type") == "pwr_state":
            try:
                number = int(wait.get("value") or 0)
                if number < 0 or number > 4:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("%s 的电源状态等待值必须是 0..4" % label)
        for check in state.get("checks") or []:
            if check.get("type") in ("pwr_state", "pwr_seen"):
                try:
                    number = int(check.get("expect") if check.get("expect") is not None else 0)
                    if number < 0 or number > 4:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("%s 的电源状态断言值必须是 0..4" % label)
            if check.get("type") == "voltage":
                try:
                    lower = float(check.get("min") if check.get("min") is not None else 18)
                    upper = float(check.get("max") if check.get("max") is not None else 32)
                    if lower >= upper:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("%s 的电压范围不合法（min < max）" % label)
            if check.get("grace_ms") is not None:
                try:
                    if float(check.get("grace_ms")) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("%s 的宽限期必须是非负数（毫秒）" % label)
            if check.get("type") == "screen":
                try:
                    ratio = float(check.get("min_ratio")
                                  if check.get("min_ratio") is not None else 0.15)
                    if not (0 <= ratio <= 1):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("%s 的画面占比阈值必须是 0..1" % label)
                else:
                    if ratio > 0.98:
                        warnings.append("%s 的画面占比阈值 %.2f 近乎不可达（正常仪表约 0.99，"
                                        "建议 0.9 以下）" % (label, ratio))
            if check.get("type") == "screen_region":
                try:
                    tol = float(check.get("tol")
                                if check.get("tol") is not None else 0.15)
                    if tol <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("%s 的区域容差必须是正数" % label)
                else:
                    if tol > 0.6:
                        warnings.append("%s 的区域容差 %.2f 过大（归一化距离，建议 0.1~0.2）"
                                        % (label, tol))
    return errors, warnings


def estimate_ms(scenario):
    """估算单个循环的时长（ms），用于界面显示"预计总时长"。"""
    data = normalize(scenario)
    total = 0
    for state in data["states"]:
        total += int(state.get("hold_ms") or 0)
        wait = state.get("wait") or {}
        if wait.get("type") and wait.get("type") != "none":
            total += int(wait.get("timeout_ms") or 0) // 2      # 按"等一半就满足"粗估
    total += int(data.get("loop_interval_ms") or 0)
    return total


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------

def _file_path(name):
    """剧本名 -> ``scenarios/<name>.json``。"""
    clean = paths.safe_name(name, "scenario", 60)
    return os.path.join(paths.SCENARIO_DIR, clean + ".json")


def load_file(path):
    """从文件读剧本；失败抛 ``ValueError``。"""
    with open(path, "r", encoding="utf-8") as handle:
        return normalize(json.load(handle))


def save_file(scenario, name=None):
    """保存剧本，返回文件路径。"""
    data = normalize(scenario)
    if name:
        data["name"] = name
    path = _file_path(data.get("name") or "scenario")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def delete_file(name):
    """按名字删除剧本文件，返回是否删掉。"""
    path = _file_path(name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def list_files():
    """列出剧本目录下的文件（按修改时间倒序）。"""
    paths.ensure_dirs()
    items = []
    for entry in os.listdir(paths.SCENARIO_DIR):
        if not entry.lower().endswith(".json"):
            continue
        path = os.path.join(paths.SCENARIO_DIR, entry)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            items.append({
                "file": entry,
                "name": data.get("name") or entry[:-5],
                "states": len(data.get("states") or []),
                "loops": data.get("loops", 1),
                "mtime": int(os.path.getmtime(path)),
                "size": os.path.getsize(path),
            })
        except (OSError, ValueError):
            items.append({"file": entry, "name": entry[:-5], "states": 0,
                          "loops": 0, "mtime": 0, "size": 0, "broken": True})
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# 内置剧本（首次启动时落盘，可随意修改/删除）
# ---------------------------------------------------------------------------

def builtin_scenarios():
    """返回内置剧本列表（覆盖上下电、闪断、CAN 中断、冒烟四类场景）。"""

    def state(name, channels, hold, wait=None, checks=None, on_fail="continue",
              actions=None, note=""):
        item = new_state(name)
        item["channels"] = channels
        item["hold_ms"] = hold
        if wait:
            item["wait"].update(wait)
        item["checks"] = checks or []
        item["actions"] = actions or []
        item["on_fail"] = on_fail
        item["note"] = note
        return item

    power_cycle = new_scenario("整机上下电压测")
    power_cycle["description"] = "主电→ACC→IG 依次上电，等待 SOC 起来并检查无 ERROR，再按相反顺序下电；循环压测。"
    power_cycle["loops"] = 0
    power_cycle["loop_interval_ms"] = 5000
    power_cycle["states"] = [
        state("整机断电（基准）", {"power": "off", "acc": "off", "ig": "off"}, 1500,
              note="从全断开始，保证每轮起点一致"),
        state("主电上", {"power": "on"}, 800,
              checks=[{"type": "no_error"}]),
        state("ACC 上", {"acc": "on"}, 800),
        state("IG 上（SOC 启动）", {"ig": "on"}, 1000,
              wait={"type": "adb_online", "timeout_ms": 30000},
              checks=[{"type": "no_error"}, {"type": "adb_online", "expect": True}],
              on_fail="freeze",
              note="SOC 冷启动约 15~20s：等 adb 枚举到再判，超时才算没起来"),
        state("上电稳定观察", {}, 3000,
              wait={"type": "log_growing", "timeout_ms": 5000},
              checks=[{"type": "no_error"}, {"type": "voltage", "min": 18, "max": 32},
                      {"type": "screen", "min_ratio": 0.15},
                      {"type": "screen_region", "tol": 0.15}], on_fail="freeze",
              note="SOC 在线且 mcu.log 持续增长 = MCU 在刷日志；查电压与仪表画面（画面基线需先「学基线」），失败即冻结现场"),
        state("IG 断（SOC 走掉电流程）", {"ig": "off"}, 2000,
              note="撤 IG 后 SOC 约 10s 才真正断电，属正常流程"),
        state("ACC 断", {"acc": "off"}, 1500),
        state("主电断", {"power": "off"}, 3000,
              checks=[{"type": "adb_online", "expect": False}],
              note="主电断后 SOC 应枚举不到"),
    ]

    ig_blink = new_scenario("故障注入_IG 闪断")
    ig_blink["description"] = "整机上电稳定后反复闪断 IG（200ms 断 / 800ms 通），验证掉电保持与重新起机。"
    ig_blink["loops"] = 20
    ig_blink["loop_interval_ms"] = 800
    ig_blink["states"] = [
        state("整机上电", {"power": "on", "acc": "on", "ig": "on"}, 1200,
              wait={"type": "adb_online", "timeout_ms": 10000}),
        state("稳定观察", {}, 2000,
              checks=[{"type": "no_error"}]),
        state("IG 闪断", {"ig": "off"}, 200,
              note="工程要求：掉电保持时间内的状态必须保留"),
        state("IG 恢复", {"ig": "on"}, 800,
              wait={"type": "serial_match", "value": "SpiRx", "timeout_ms": 3000},
              checks=[{"type": "no_error"}], on_fail="freeze"),
    ]

    can_drop = new_scenario("CAN 发送中断注入")
    can_drop["description"] = "CAN 随机发送启动后，分别断开发送 5s 再恢复，验证 DUT 对丢帧/恢复的容错（不碰电源）。"
    can_drop["loops"] = 3
    can_drop["loop_interval_ms"] = 3000
    can_drop["can"] = {"mode": "tx", "buses": ["ican", "pcan"], "rand_mode": "per-frame",
                       "rand_interval_ms": 1000, "cycle_scale": 1.0}
    can_drop["states"] = [
        state("CAN 发送启动", {}, 1000,
              note="剧本 can.mode=tx 时进入即自动开始发送"),
        state("ICAN 断开发送", {"ican": "off"}, 5000,
              note="只暂停发送，接收与 DUT 其余逻辑不受影响"),
        state("ICAN 恢复发送", {"ican": "on"}, 3000),
        state("PCAN 断开发送", {"pcan": "off"}, 5000),
        state("PCAN 恢复发送", {"pcan": "on"}, 3000),
        state("结束前停止发送", {}, 200, actions=[{"type": "can_tx_stop"}]),
    ]

    smoke = new_scenario("冒烟_上电自检")
    smoke["description"] = "最小流程：整机上电 → 等 SOC 在线 → 检查 3s 无 ERROR → 结束（不断电，留着现场）。"
    smoke["loops"] = 1
    smoke["states"] = [
        state("整机上电", {"power": "on", "acc": "on", "ig": "on"}, 1000,
              wait={"type": "adb_online", "timeout_ms": 10000}, on_fail="freeze"),
        state("自检观察", {}, 3000,
              wait={"type": "log_growing", "timeout_ms": 4000},
              checks=[{"type": "no_error"}], on_fail="freeze"),
        state("保留现场（不下电）", {}, 500,
              note="结束时保持上电，方便回看屏幕与日志"),
    ]

    # 电源状态机：靠"总线上有什么帧"驱动 OFF→STANDBY→LISTEN，再用 ACC/IG 进 PRE_ON/ON
    pwr_sm = new_scenario("电源状态机_待机与唤醒")
    pwr_sm["description"] = (
        "按 PowerMng 的状态机走一遍：仅主电（OFF）→ 任意帧（STANDBY）→ 停帧（回 OFF）"
        "→ 唤醒帧（LISTEN，SOC 上电）→ 停唤醒帧（5s 回 STANDBY）→ ACC（PRE_ON）→ IG（ON）。")
    pwr_sm["loops"] = 1
    pwr_sm["loop_interval_ms"] = 3000
    pwr_sm["auto_mute_power_off"] = True
    pwr_sm["can"] = {"mode": "none", "buses": [], "rand_mode": "per-frame",
                     "rand_interval_ms": 1000, "cycle_scale": 1.0}
    pwr_sm["states"] = [
        state("整机断电（基准）", {"power": "off", "acc": "off", "ig": "off"}, 2000,
              note="起点全断，MCU 也掉电"),
        state("仅主电（MCU 起，SOC 不上电）", {"power": "on"}, 3000,
              wait={"type": "pwr_state", "value": 0, "timeout_ms": 12000},
              checks=[{"type": "pwr_state", "expect": 0}], on_fail="freeze",
              note="只有主电时 PowerMng 停在 OFF(0)，SOC 无电"),
        state("任意帧 → 待机", {}, 1000,
              actions=[{"type": "can_stim", "mode": "any", "period_ms": 200}],
              wait={"type": "pwr_state", "value": 1, "timeout_ms": 8000},
              checks=[{"type": "pwr_state", "expect": 1}], on_fail="freeze",
              note="任意一帧即可把 OFF 唤醒到 STANDBY(1)"),
        state("待机保持（普通帧续命 6s）", {}, 6000,
              checks=[{"type": "pwr_state", "expect": 1}], on_fail="freeze",
              note="持续发普通帧 ⇒ 网络不静默，不会落回关机"),
        state("停帧 → 回关机", {}, 1000,
              actions=[{"type": "can_stim", "mode": "silence"}],
              wait={"type": "pwr_state", "value": 0, "timeout_ms": 35000},
              checks=[{"type": "pwr_state", "expect": 0}], on_fail="freeze",
              note="网络静默 10s 发关机状态、再 15s 断电（实测约 25s）"),
        state("唤醒帧 → 监听（SOC 上电）", {}, 1000,
              actions=[{"type": "can_stim", "mode": "wake", "period_ms": 50}],
              wait={"type": "pwr_state", "value": 2, "timeout_ms": 12000},
              checks=[{"type": "pwr_state", "expect": 2},
                      {"type": "pwr_seen", "expect": 2}], on_fail="freeze",
              note="转向/双闪/BMS1 充电/OTA 四类唤醒源，进 LISTEN(2) 且 SOC 上电"),
        state("监听续期（唤醒帧不停 6s）", {}, 6000,
              checks=[{"type": "pwr_state", "expect": 2}], on_fail="freeze",
              note="每 50ms 一帧唤醒源 ⇒ 监听窗口持续续期"),
        state("停唤醒帧 → 5s 落待机", {}, 1000,
              actions=[{"type": "can_stim", "mode": "silence"}],
              wait={"type": "pwr_state", "value": 1, "timeout_ms": 12000},
              checks=[{"type": "pwr_state", "expect": 1}], on_fail="freeze",
              note="5s 无唤醒帧 → STANDBY(1)，SOC 仍带电"),
        state("ACC 上 → 预开机", {"acc": "on"}, 1000,
              wait={"type": "pwr_state", "value": 3, "timeout_ms": 10000},
              checks=[{"type": "pwr_state", "expect": 3}], on_fail="freeze"),
        state("IG 上 → 开机", {"ig": "on"}, 1000,
              wait={"type": "pwr_state", "value": 4, "timeout_ms": 10000},
              checks=[{"type": "pwr_state", "expect": 4},
                      {"type": "adb_online", "expect": True},
                      {"type": "voltage", "min": 18, "max": 32},
                      {"type": "screen", "min_ratio": 0.15}], on_fail="freeze"),
        state("收尾：整机断电", {"power": "off"}, 2000,
              checks=[{"type": "adb_online", "expect": False}],
              note="结束时全断，方便下一轮从一致状态开始"),
    ]
    return [power_cycle, ig_blink, can_drop, smoke, pwr_sm]


def seed_builtin():
    """首次启动把内置剧本写入 scenarios/（已存在则跳过）。"""
    paths.ensure_dirs()
    created = []
    for scenario in builtin_scenarios():
        path = _file_path(scenario["name"])
        if os.path.isfile(path):
            continue
        save_file(scenario)
        created.append(path)
    return created

