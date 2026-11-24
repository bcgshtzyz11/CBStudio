# -*- coding: utf-8 -*-
"""stimulus.py — 电源状态机激励：任意帧 / 唤醒帧 / 指定帧 / 静默

PowerMng 的状态不是靠"命令"切的，而是靠**总线上有什么帧**推出来的。所以剧本要
测电源状态，必须能按状态控制 CAN 激励。本模块定义各种激励用到的帧集合：

===================  =========================================================
mode                 含义
===================  =========================================================
``any``              普通车身帧（**非**唤醒源），任一路持续发 → OFF 唤醒到待机；
                     持续发则"网络不静默"，停在待机不会掉关机
``wake``             唤醒源帧（转向/双闪/BMS1 充电/TBOX OTA）→ 待机升监听、SOC 上电；
                     持续发则监听态一直续期（每帧一次脉冲）
``frame``            单条指定帧（id + data）
``silence``          停掉全部激励 → 5s 无唤醒帧落待机、10/15s 网络静默落关机
===================  =========================================================

唤醒源与取值来自固件 ``power_can.c``（``g_wakeTrigger[]``）：

- ICAN ``0x18FE4121`` BCM1：左/右转向灯 = 1（``start`` 14/12，2 bit）→ 载荷 byte1 = 0x50
- ICAN ``0x18FE4021`` BCM2：双闪 = 1（``start`` 2，2 bit）→ 载荷 byte0 = 0x04
- PCAN ``0x18FF21B1`` BMS1：``stMode`` = 5(充电中) / 8(完成)（``start`` 0，4 bit）
- PCAN ``0x18FF11FC`` TBOX OTA：**帧到达**即唤醒（与载荷无关）

``any`` 用的普通帧刻意避开上面四类，否则会把状态推到监听。
"""

PWR_STATE_NAMES = {
    0: "关机",
    1: "待机",
    2: "监听",
    3: "预开机",
    4: "开机",
}

# 普通车身帧（非唤醒源）：足以让"网络不静默"，但不会把待机推到监听
ANY_FRAMES = [
    ("ican", 0x18FEF117, "00 00 00 00 00 00 00 00"),
    ("ican", 0x18FFE117, "00 00 00 00 00 00 00 00"),
    ("ican", 0x18FFDA17, "00 00 00 00 00 00 00 00"),
    ("pcan", 0x18FEF100, "00 00 00 00 00 00 00 00"),
    ("pcan", 0x18FEC1EE, "00 00 00 00 00 00 00 00"),
]

# 唤醒源帧：任意一条即够；四条一起发覆盖转向/双闪/充电/OTA 四类唤醒路径
WAKE_FRAMES = [
    ("ican", 0x18FE4121, "00 50 00 00 00 00 00 00"),   # 左+右转向 = 1
    ("ican", 0x18FE4021, "04 00 00 00 00 00 00 00"),   # 双闪 = 1
    ("pcan", 0x18FF21B1, "05 00 00 00 00 00 00 00"),   # BMS1 stMode = 5 充电中
    ("pcan", 0x18FF11FC, "00 00 00 00 00 00 00 00"),   # TBOX OTA 帧到达
]

MODE_LABELS = {
    "any": "任意帧（维持待机）",
    "wake": "唤醒帧（升监听）",
    "frame": "指定帧",
    "silence": "静默（停全部激励）",
}


def build_frames(mode, bus=None, can_id=None, data=None):
    """按模式生成 ``[(bus, can_id, "hex data"), ...]``。

    ``bus`` 指定时只保留该总线的帧（``frame`` 模式则整组都用该总线）。
    """
    mode = str(mode or "any").lower()
    if mode == "any":
        frames = list(ANY_FRAMES)
    elif mode == "wake":
        frames = list(WAKE_FRAMES)
    elif mode == "frame":
        if can_id is None:
            return []
        frames = [(str(bus or "ican").lower(), int(can_id), str(data or ""))]
    else:
        return []
    if bus and mode != "frame":
        wanted = str(bus).lower()
        picked = [item for item in frames if item[0] == wanted]
        if picked:
            frames = picked
    return frames


def group_by_bus(frames):
    """``[(bus,id,data)]`` -> ``{bus: [(id,data)]}``。"""
    grouped = {}
    for item_bus, can_id, data in frames:
        grouped.setdefault(item_bus, []).append((int(can_id), str(data)))
    return grouped


def state_name(value):
    """状态号 -> 中文名（未知返回 ``#N``）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "?"
    return PWR_STATE_NAMES.get(number, "#%d" % number)


def describe_pwr(pwr):
    """把探针里的电源状态压成一行给界面/日志用。"""
    if not pwr or pwr.get("state") is None:
        return "电源状态未知"
    allowed = pwr.get("allowed")
    return "电源: %s(%s)%s" % (
        state_name(pwr.get("state")), pwr.get("state"),
        "" if allowed is None else (" · CAN 外发%s" % ("放行" if allowed else "关闭")))
