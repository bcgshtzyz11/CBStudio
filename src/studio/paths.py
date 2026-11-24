# -*- coding: utf-8 -*-
"""paths.py — 工作台目录布局、运行编号与文件工具

两个锚点，其它一切由它们推导：

``STUDIO_DIR``  工程根（配置 / 剧本 / 数据 / SDK / 双击的 exe 都在这层）::

    CBStudio.exe             双击入口（打包产物）
    _internal/               打包运行时
    run_studio.bat           源码启动
    src/                     源码（app.py / studio / can_engine.py / web）
    sdk/ scenarios/ config.json can_matrix_manifest.json
    data/                    运行时生成

``SRC_DIR``     源码目录（``app.py / studio/ / can_engine.py / can_pack.py / web/``）。
    老版本把源码平铺在根目录，这里做兼容：根下有 ``src/`` 就用它，否则退回根目录。

运行产物（``data/runs/<run_id>/``）::

    events.jsonl             全部结构化日志（可回放）
    serial.log               DUT 调试串口原始文本
    incidents/<n>_<reason>/  现场保全目录（快照 + 说明）
    report.html              运行报告（导出）

``run_id`` 形如 ``20261117-184501-r01``：日期-时间-序号，天然按时间排序。
"""

import datetime
import os
import re
import sys
import time


def app_dir():
    """工程根目录（**唯一**的目录锚点）。

    * 源码运行：``src/studio/`` 往上三层，即工程根；
    * 打包成 exe（PyInstaller）：exe 所在目录 —— exe 放哪，
      ``sdk/ scenarios/ config.json data/`` 就按它旁边找。

    打包后 ``__file__`` 落在 ``_internal/ studio/`` 里，不能再用 ``__file__``
    推工程目录，否则配置、剧本、SDK 全会指到包里。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bundle_dir():
    """打包时随包解出的资源目录（源码运行 = 工程根目录）。"""
    return getattr(sys, "_MEIPASS", None) or app_dir()


def src_dir():
    """源码目录：``<根>/src``，没有就退回根目录（兼容老结构）。"""
    candidate = os.path.join(app_dir(), "src")
    return candidate if os.path.isdir(candidate) else app_dir()


def app_file(name):
    """按「源码目录 → 根目录 → 包内」的顺序找一个随包文件，返回首个存在的。"""
    for base in (src_dir(), app_dir(), bundle_dir()):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    return os.path.join(src_dir(), name)


STUDIO_DIR = app_dir()
SRC_DIR = src_dir()
# 前端：先找外置（src/web），再兼容老结构（根/web），最后用包内副本
WEB_DIR = app_file("web")
SCENARIO_DIR = os.path.join(STUDIO_DIR, "scenarios")
DATA_DIR = os.path.join(STUDIO_DIR, "data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
CONFIG_PATH = os.path.join(STUDIO_DIR, "config.json")
STUDIO_LOG_PATH = os.path.join(DATA_DIR, "studio.log")

_SAFE = re.compile(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+")


def ensure_dirs():
    """建立运行所需的目录（幂等）。"""
    for path in (DATA_DIR, RUNS_DIR, SCENARIO_DIR):
        os.makedirs(path, exist_ok=True)
    if WEB_DIR.startswith(STUDIO_DIR):         # 包内前端（只读）不去创建
        os.makedirs(WEB_DIR, exist_ok=True)


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
