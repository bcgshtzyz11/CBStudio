# -*- coding: utf-8 -*-
"""config.py — 工作台配置（config.json）的默认值、读写与合并

所有硬件参数都在这里落地，界面"设置"页改的就是它。首次启动写入默认值；
之后按"默认值 <- 文件内容"深合并，缺字段自动补齐（升级不会因缺键报错）。
"""

import copy
import json
import os

from . import paths
from .canctl import DEFAULT_CAN_CONFIG
from .channels import DEFAULT_CHANNELS

DEFAULTS = {
    "version": 1,
    "simulate": False,               # true = 全模拟（不碰继电器/CAN/串口/adb）
    "server": {"host": "127.0.0.1", "port": 8777, "open_browser": True},
    "channels": DEFAULT_CHANNELS,
    # 端口/路径留空 = 自动识别（继电器口、DUT 串口、adb、32 位解释器、SDK、清单）
    "relay": {"port": "", "baud": 9600, "dry_run": False},
    "can": dict(DEFAULT_CAN_CONFIG),
    "probes": {
        # 上电/上 IG 之后 DUT 要启动，这段时间内"没 adb / 没日志"属正常，不判事故
        "boot_grace_ms": 25000,
        "serial": {"enabled": True, "port": "", "baud": 460800, "log_rate": 40},
        "adb": {"enabled": True, "interval_ms": 1000,
                "exe": "",
                "log_path": "/userdata/log/mcu/mcu/mcu.log"},
    },
    "incident": {
        "freeze_ms": 800,
        "serial_tail_kb": 256,
        "events_tail": 300,
        "adb_log_lines": 300,
        "screenshot": True,
        "keep_power": True,
        "open_folder": False,
    },
    "retention": {"max_runs": 60, "max_days": 30},
    "ui": {"log_cap": 2000, "theme": "dark"},
}


def _merge(base, override):
    """递归合并：``override`` 覆盖 ``base``，dict 逐键合并。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load(path=None):
    """读取配置（不存在则写默认值）；返回完整配置 dict。"""
    path = path or paths.CONFIG_PATH
    paths.ensure_dirs()
    if not os.path.isfile(path):
        config = copy.deepcopy(DEFAULTS)
        save(config, path)
        return config
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        stored = {}
    return _merge(DEFAULTS, stored)


def save(config, path=None):
    """原子写配置。"""
    path = path or paths.CONFIG_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def public_view(config):
    """给前端的配置视图（去掉不需要展示的默认值副本，保留全部硬件参数）。"""
    return copy.deepcopy(config)
