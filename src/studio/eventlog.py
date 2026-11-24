# -*- coding: utf-8 -*-
"""eventlog.py — 内存环形缓冲 + JSONL 落盘 + 多订阅者广播的结构化日志总线

一条事件（``LogEvent``）的字段::

    seq     全局自增序号（前端断线重连按 seq 续传）
    t       时间戳（毫秒）
    level   debug / info / warn / error / incident / action
    source  来源（serial / can / adb / relay / runner / probe / studio ...）
    text    人读文本
    run     运行编号（不属于任何运行时为空）
    st      状态卡名字（运行器写入，便于按状态回溯）

设计要点：

* 串口每秒可达数百行，**不能**逐行推给浏览器：上游写环形缓冲并落盘，
  下游（SSE）按 10Hz 批量取增量，超出批量上限时给 ``dropped`` 计数。
* 落盘用一把锁顺序追加，文件按运行目录切换；切换时 flush + close 旧文件。
* 计数器分级累计，用于界面上的 ERROR/WARN 徽标与报告统计。
"""

import collections
import json
import os
import threading
import time

from . import paths

LEVEL_ORDER = {"debug": 10, "info": 20, "warn": 30, "error": 40, "incident": 50, "action": 60}
LEVEL_ALIASES = {
    "trace": "debug", "verbose": "debug", "dbg": "debug",
    "inf": "info", "notice": "info",
    "warning": "warn", "wrn": "warn",
    "err": "error", "fatal": "error", "panic": "error",
    "crit": "incident",
}

_RING_CAPACITY = 30000


def normalize_level(level):
    """把各种写法的等级归一化成 6 档之一。"""
    key = str(level or "info").strip().lower()
    key = LEVEL_ALIASES.get(key, key)
    return key if key in LEVEL_ORDER else "info"


class EventLog(object):
    """线程安全的事件总线：环形缓冲 + JSONL 文件 + 订阅队列。"""

    def __init__(self, capacity=_RING_CAPACITY):
        self._lock = threading.Lock()
        self._ring = collections.deque(maxlen=capacity)
        self._seq = 0
        self._queues = []
        self._file = None
        self._file_path = None
        self._counters = {name: 0 for name in LEVEL_ORDER}
        self._counters["total"] = 0
        self._started = paths.now_ms()

    # -- 生命周期 ---------------------------------------------------------
    def attach_file(self, path):
        """切换 JSONL 落盘文件（每次运行一个）；传 ``None`` 关闭落盘。"""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            self._file_path = path
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self._file = open(path, "a", encoding="utf-8", buffering=1)
        if path:
            self.emit("info", "studio", "日志落盘 %s" % path)

    def close(self):
        """关闭落盘文件（进程退出时调用）。"""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                except OSError:
                    pass
                self._file = None

    # -- 写入 -------------------------------------------------------------
    def emit(self, level, source, text, run=None, state=None, **fields):
        """写一条事件并广播；返回该事件 dict。"""
        level = normalize_level(level)
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "t": paths.now_ms(),
                "level": level,
                "source": str(source or "studio"),
                "text": str(text if text is not None else ""),
                "run": run or "",
                "st": state or "",
            }
            if fields:
                event.update({k: v for k, v in fields.items() if v is not None})
            self._ring.append(event)
            self._counters[level] = self._counters.get(level, 0) + 1
            self._counters["total"] += 1
            if self._file is not None:
                try:
                    self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
                except (OSError, ValueError):
                    pass
            queues = list(self._queues)
        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:              # 订阅者断开 / 队列满：丢给该订阅者
                pass
        return event

    # 便捷入口
    def debug(self, source, text, **kw):
        return self.emit("debug", source, text, **kw)

    def info(self, source, text, **kw):
        return self.emit("info", source, text, **kw)

    def warn(self, source, text, **kw):
        return self.emit("warn", source, text, **kw)

    def error(self, source, text, **kw):
        return self.emit("error", source, text, **kw)

    def action(self, source, text, **kw):
        return self.emit("action", source, text, **kw)

    # -- 读取 -------------------------------------------------------------
    def subscribe(self, maxsize=4000):
        """新建一个订阅队列（SSE 连接用），返回 ``queue.Queue``。"""
        import queue as queue_mod
        item = queue_mod.Queue(maxsize=maxsize)
        with self._lock:
            self._queues.append(item)
        return item

    def unsubscribe(self, item):
        """移除订阅队列。"""
        with self._lock:
            if item in self._queues:
                self._queues.remove(item)

    @property
    def seq(self):
        """最新事件序号。"""
        return self._seq

    def since(self, seq, limit=2000):
        """取 ``seq`` 之后的事件（环形缓冲里还在的部分）。"""
        with self._lock:
            if not self._ring:
                return []
            latest = self._ring[-1]["seq"]
            if seq >= latest:
                return []
            start = max(0, len(self._ring) - (latest - seq))
            items = list(self._ring)[start:]
        return items[-limit:]

    def tail(self, count=200):
        """最近 ``count`` 条事件。"""
        with self._lock:
            return list(self._ring)[-count:]

    def counters(self):
        """分级计数快照。"""
        with self._lock:
            data = dict(self._counters)
        data["uptime_s"] = int((paths.now_ms() - self._started) / 1000)
        return data

    def reset_counters(self):
        """清零分级计数（开始新运行时调用）。"""
        with self._lock:
            self._counters = {name: 0 for name in LEVEL_ORDER}
            self._counters["total"] = 0


class RateMeter(object):
    """滑动窗口速率统计（行/秒、帧/秒），供探针与界面显示。"""

    def __init__(self, window_s=5.0):
        self.window_s = float(window_s)
        self._marks = collections.deque()
        self._lock = threading.Lock()
        self._total = 0

    def hit(self, count=1):
        """记一次（或 ``count`` 次）事件。"""
        now = time.time()
        with self._lock:
            self._marks.append((now, count))
            self._total += count
            self._trim(now)

    def _trim(self, now):
        edge = now - self.window_s
        while self._marks and self._marks[0][0] < edge:
            self._marks.popleft()

    def rate(self):
        """窗口内平均速率（每秒）。"""
        now = time.time()
        with self._lock:
            self._trim(now)
            if not self._marks:
                return 0.0
            span = max(1e-6, now - self._marks[0][0])
            total = sum(count for _t, count in self._marks)
        return total / min(span, self.window_s)

    @property
    def total(self):
        """累计次数。"""
        with self._lock:
            return self._total
