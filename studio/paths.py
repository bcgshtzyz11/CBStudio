# -*- coding: utf-8 -*-
"""paths.py — 工作台目录布局、运行编号与文件工具

目录约定（全部落在 ``_tools/can_bench_studio/`` 下，已被工程 .gitignore 忽略）::

    config.json              用户配置（通道 / CAN / 探针 / 保留策略）
    scenarios/*.json         剧本（可拖动排序的状态序列）
    data/runs/<run_id>/      一次运行的落盘目录
        events.jsonl             全部结构化日志（可回放）
        serial.log               DUT 调试串口原始文本
        incidents/<n>_<reason>/ 现场保全目录（快照 + 说明）
        report.html             运行报告（导出）
    data/studio.log          服务端自身日志

``run_id`` 形如 ``20261117-184501-r01``：日期-时间-序号，天然按时间排序。
"""

import datetime
import os
import re
import time

STUDIO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(STUDIO_DIR, "web")
SCENARIO_DIR = os.path.join(STUDIO_DIR, "scenarios")
DATA_DIR = os.path.join(STUDIO_DIR, "data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
CONFIG_PATH = os.path.join(STUDIO_DIR, "config.json")
STUDIO_LOG_PATH = os.path.join(DATA_DIR, "studio.log")

_SAFE = re.compile(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+")


def ensure_dirs():
    """建立运行所需的目录（幂等）。"""
    for path in (DATA_DIR, RUNS_DIR, SCENARIO_DIR, WEB_DIR):
        os.makedirs(path, exist_ok=True)


def safe_name(text, fallback="item", limit=48):
    """把任意文本压成可安全落盘的文件名片段。"""
    cleaned = _SAFE.sub("_", str(text or "").strip()).strip("_")
    return (cleaned or fallback)[:limit]


def now_ms():
    """当前 wall clock 毫秒（日志时间戳用）。"""
    return int(time.time() * 1000)


def iso_now():
    """本地时间 ISO 串（秒精度，落盘与界面显示用）。"""
    return datetime.datetime.now().isoformat(timespec="seconds")


def fmt_ts(ms=None, fmt="%Y-%m-%d %H:%M:%S"):
    """把毫秒时间戳格式化成 ``YYYY-mm-dd HH:MM:SS``；``None`` 表示当前。"""
    stamp = time.time() if ms is None else float(ms) / 1000.0
    return time.strftime(fmt, time.localtime(stamp))


def fmt_clock(ms=None):
    """``HH:MM:SS.mmm``，日志行前缀用。"""
    stamp = time.time() if ms is None else float(ms) / 1000.0
    return time.strftime("%H:%M:%S", time.localtime(stamp)) + ".%03d" % int((stamp % 1) * 1000)


def new_run_id(prefix="r"):
    """生成按时间可排序的运行编号 ``20261117-184501-r01``（同日同秒顺延序号）。"""
    base = time.strftime("%Y%m%d-%H%M%S")
    for index in range(1, 100):
        candidate = "%s-%s%02d" % (base, prefix, index)
        if not os.path.exists(os.path.join(RUNS_DIR, candidate)):
            return candidate
    return "%s-%s%03d" % (base, prefix, int(time.time() * 1000) % 1000)


def run_dir(run_id):
    """运行编号 -> 落盘目录（不检查存在性）。"""
    return os.path.join(RUNS_DIR, safe_name(run_id, "run", 80))


def human_size(size):
    """字节数 -> ``1.2 KB`` / ``3.4 MB`` 文本。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return ("%d B" % value) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f GB" % value


def dir_size(path):
    """递归统计目录占用字节数；路径不存在返回 0。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def read_tail(path, max_bytes=256 * 1024):
    """读取文件尾部 ``max_bytes`` 字节并按行切成文本（大日志快照用）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()          # 丢掉被截断的半行
            data = handle.read()
        return data.decode("utf-8", "replace")
    except OSError:
        return ""
