# -*- coding: utf-8 -*-
"""runner.py — 剧本执行器：按顺序跑状态卡，支持循环、暂停、等待、断言、事故中止

执行一张状态卡的顺序（与界面上的语义一一对应）::

    进入状态
      ├─ 执行 actions（can_tx_start / can_tx_stop / mark）
      ├─ 切通道：先按"配置顺序"关掉要关的，再按"配置顺序"打开要开的
      │            （关：IG→ACC→主电；开：主电→ACC→IG，见 CHANNEL_ORDER 注释）
      ├─ 至少保持 hold_ms
      ├─ 满足 wait 条件（可选，带超时）
      └─ 评估 checks（窗口内必须成立的条件）

任何一步失败按 ``on_fail`` 处理：``continue`` 记一笔继续、``stop`` 停下、
``freeze`` 先现场保全再停下。监看规则（probes.RuleEngine）可以在任意时刻
触发事故，是否中止由规则 ``stop`` 与剧本 ``stop_on_incident`` 共同决定。

**暂停不改变时间轴**：暂停期间累计的时长会从所有截止时间里扣掉，恢复后
``hold_ms`` 不会因为暂停而提前结束。
"""

import json
import os
import threading
import time

from . import paths

PHASE_IDLE = "idle"
PHASE_RUNNING = "running"
PHASE_PAUSED = "paused"
PHASE_STOPPING = "stopping"
PHASE_FINISHED = "finished"
PHASE_ERROR = "error"

TICK_S = 0.05


class Runner(object):
    """剧本执行线程 + 状态快照。"""

    def __init__(self, app):
        self._app = app                    # StudioApp：提供 bus/channels/probes/can/incidents
        self._lock = threading.RLock()
        self._thread = None
        self._stop_event = threading.Event()
        self._paused_at = 0.0
        self._pause_shift = 0.0
        self.phase = PHASE_IDLE
        self.scenario = None
        self.iteration = 0
        self.loops = 1
        self.state_index = -1
        self.state_name = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.run_id = ""
        self.run_dir = ""
        self.last_error = ""
        self.results = {}
        self.state_started_at = 0.0
        self.fail_streak = 0
        self.incidents = 0
        self._changed = threading.Event()
        self._state_token = None           # 当前状态卡的令牌，定时抓屏据此自动退出

    # -- 时钟（扣除暂停） -------------------------------------------------
    def _now(self):
        """返回扣除暂停时长后的单调时间。"""
        if self._paused_at:
            return self._paused_at - self._pause_shift
        return time.monotonic() - self._pause_shift

    def _sleep_until(self, target, step=TICK_S):
        """睡到 ``target``（按 :meth:`_now` 的时间轴）；被停止则返回 False。"""
        while True:
            if self._stop_event.is_set():
                return False
            if self._paused_at:
                time.sleep(step)
                continue
            remain = target - self._now()
            if remain <= 0:
                return True
            time.sleep(min(step, remain))

    def _wait_check(self, condition, grace_ms):
        """在宽限期内轮询条件（ms）；``grace_ms <= 0`` 表示只判一次。"""
        if grace_ms <= 0:
            return bool(condition())
        deadline = time.time() + grace_ms / 1000.0
        while True:
            if condition():
                return True
            if self._stop_event.is_set() or time.time() >= deadline:
                return False
            self._sleep_ms(200)

    def _sleep_ms(self, ms):
        """相对睡眠（ms）。"""
        return self._sleep_until(self._now() + max(0.0, ms / 1000.0))

    # -- 生命周期 ---------------------------------------------------------
    def start(self, scenario):
        """启动剧本执行；返回 ``(ok, message)``。"""
        with self._lock:
            if self.phase in (PHASE_RUNNING, PHASE_PAUSED):
                return False, "剧本正在运行，先停止"
            self.scenario = scenario
            self.phase = PHASE_RUNNING
            self.iteration = 0
            self.loops = int(scenario.get("loops") or 0)
            self.state_index = -1
            self.state_name = ""
            self.results = {}
            self.fail_streak = 0
            self.incidents = 0
            self.last_error = ""
            self.started_at = time.monotonic()
            self.finished_at = 0.0
            self._pause_shift = 0.0
            self._paused_at = 0.0
            self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="runner")
        self._thread.daemon = True
        self._thread.start()
        return True, "开始执行「%s」" % (scenario.get("name") or "剧本")

    def stop(self, reason="手动停止"):
        """请求停止（异步；线程在下一个 tick 收尾）。"""
        with self._lock:
            if self.phase not in (PHASE_RUNNING, PHASE_PAUSED, PHASE_STOPPING):
                return False
            self.phase = PHASE_STOPPING
            self.last_error = reason
        self._stop_event.set()
        self._app.bus.action("runner", "停止剧本：%s" % reason)
        return True

    def pause(self):
        """暂停（时间轴冻结）。"""
        with self._lock:
            if self.phase != PHASE_RUNNING:
                return False
            self.phase = PHASE_PAUSED
            self._paused_at = time.monotonic() - self._pause_shift
        self._app.bus.action("runner", "剧本已暂停")
        return True

    def resume(self):
        """恢复。"""
        with self._lock:
            if self.phase != PHASE_PAUSED:
                return False
            self._pause_shift += time.monotonic() - (self._paused_at + self._pause_shift)
            self._paused_at = 0.0
            self.phase = PHASE_RUNNING
        self._app.bus.action("runner", "剧本已恢复")
        return True

    # -- 事故入口（监看规则 / 手动） --------------------------------------
    def notify_incident(self, payload):
        """规则命中：记录并按策略中止。"""
        with self._lock:
            self.incidents += 1
            running = self.phase in (PHASE_RUNNING, PHASE_PAUSED)
            scenario = self.scenario or {}
        self._app.bus.emit("incident", "rule", "事故命中：%s（%s）"
                           % (payload.get("rule"), payload.get("detail", "")),
                           state=payload.get("state", self.state_name))
        if not running:
            return
        should_stop = bool(payload.get("stop")) or bool(scenario.get("stop_on_incident", True))
        if should_stop:
            self.stop("事故触发：%s" % (payload.get("rule") or "监看规则"))

    # -- 主循环 -----------------------------------------------------------
    def _run(self):
        """执行线程主体。"""
        app = self._app
        scenario = self.scenario or {}
        states = scenario.get("states") or []
        try:
            self._prepare_run()
            ok, message = app.start_can_for(scenario)
            if not ok:
                app.bus.warn("runner", "CAN 未就绪：%s（剧本继续，CAN 相关断言会失败）" % message)
            loops = int(scenario.get("loops") or 0)
            iteration = 0
            while True:
                iteration += 1
                with self._lock:
                    self.iteration = iteration
                app.bus.action("runner", "第 %d 轮开始%s" % (
                    iteration, "" if loops else "（无限循环）"))
                for index, state in enumerate(states):
                    if self._stop_event.is_set():
                        raise _Stopped()
                    with self._lock:
                        self.state_index = index
                        self.state_name = state.get("name") or "状态 %d" % (index + 1)
                        self.state_started_at = self._now()
                    if not self._execute_state(state):
                        raise _Stopped()
                if loops and iteration >= loops:
                    break
                interval = int(scenario.get("loop_interval_ms") or 0)
                if interval > 0:
                    app.bus.info("runner", "本轮结束，等待 %dms 进入下一轮" % interval)
                    if not self._sleep_ms(interval):
                        raise _Stopped()
            self._finish("完成：共 %d 轮" % iteration)
        except _Stopped:
            self._finish(self.last_error or "已停止")
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self._finish("异常中止：%s" % self.last_error, error=True)
            app.bus.error("runner", "剧本异常: %s" % exc)
        finally:
            self._teardown()

    def _prepare_run(self):
        """建运行目录、挂日志文件、起探针。"""
        app = self._app
        run_id = paths.new_run_id()
        run_dir = paths.run_dir(run_id)
        os.makedirs(os.path.join(run_dir, "incidents"), exist_ok=True)
        with self._lock:
            self.run_id = run_id
            self.run_dir = run_dir
        app.incidents.bind_run(run_dir, run_id)
        app.bus.attach_file(os.path.join(run_dir, "events.jsonl"))
        app.probes.start(run_dir)
        app.bus.action("runner", "运行目录 %s" % run_dir, run=run_id)
        self._sync_rule_context()          # 先按现实对齐，再开始跑状态卡
        app.bus.info("runner", "剧本「%s」开始，循环 %s" % (
            (self.scenario or {}).get("name"),
            "无限" if not (self.scenario or {}).get("loops") else self.scenario["loops"]))

    def _teardown(self):
        """收尾：停发送、写摘要、脱开日志文件。"""
        app = self._app
        try:
            self._sync_rule_context()
        except Exception:
            pass
        try:
            app.stop_can_tx()
        except Exception:
            pass
        try:
            app.stop_stimuli()
        except Exception:
            pass
        summary = {
            "scenario": (self.scenario or {}).get("name"),
            "iterations": self.iteration,
            "loops": self.loops,
            "phase": self.phase,
            "error": self.last_error,
            "incidents": len(app.incidents.items()),
            "states": self.results,
            "elapsed_ms": int((self.finished_at or time.monotonic()) - self.started_at),
        }
        try:
            app.incidents.write_run_summary(summary)
        except Exception:
            pass
        app.bus.attach_file(None)
        app.probes.serial.attach_file(None)
        # 结束时**不关探针**：串口/adb 继续盯住（跑完还能立刻看到现场），
        # 只把本次运行的落盘文件摘掉。探针线程的启动/停止由工作台生命周期管理。
        with self._lock:
            if self.phase not in (PHASE_ERROR,):
                self.phase = PHASE_FINISHED
            self.finished_at = time.monotonic()
        self._changed.set()

    def _finish(self, message, error=False):
        """记录结束状态与日志。"""
        with self._lock:
            self.phase = PHASE_ERROR if error else PHASE_FINISHED
            self.finished_at = time.monotonic()
            if not error:
                self.last_error = message
        self._app.bus.action("runner", message)
        self._changed.set()

    # -- 单张状态卡 -------------------------------------------------------
    def _execute_state(self, state):
        """执行一张状态卡；返回 False 表示被停止。"""
        app = self._app
        name = state.get("name") or "状态"
        app.bus.action("runner", "进入状态「%s」" % name, state=name)
        entered = self._now()
        captures = [a for a in (state.get("actions") or []) if a.get("type") == "capture"]
        token = object()
        self._state_token = token

        # 1) 动作
        for action in state.get("actions") or []:
            self._run_action(state, action)
        self._start_interval_capture(state, captures, token)

        # 2) 切通道（先关后开；关按逆序，开按正序）
        mapping = {cid: value for cid, value in (state.get("channels") or {}).items()
                   if value in ("on", "off")}
        if mapping:
            offs = {cid: value for cid, value in mapping.items() if value == "off"}
            ons = {cid: value for cid, value in mapping.items() if value == "on"}
            if offs:
                app.channels.apply_map(offs, order="reverse", source="剧本")
            if ons:
                app.channels.apply_map(ons, order="config", source="剧本")

        # 3) 断言基线（CAN 计数从这里开始算）；保持时长也从这里起算
        baseline = self._can_baseline()
        window_start = time.time()
        started = self._now()
        self._update_mute()

        # 4) 保持
        hold_ms = int(state.get("hold_ms") or 0)
        if hold_ms > 0 and not self._sleep_until(started + hold_ms / 1000.0):
            return False
        if self._paused_at:
            app.bus.debug("runner", "状态「%s」暂停中" % name)

        # 5) 等待条件
        wait = state.get("wait") or {}
        wait_ok, wait_detail = self._wait_condition(state, wait, window_start)
        if not wait_ok and self._stop_event.is_set():
            return False                       # 被事故/手动停止打断，不再记一条"超时"
        if not wait_ok:
            self._fire_captures(captures, "on_timeout", state, "等待超时")
            failed = self._record_failure(state, "等待超时", wait_detail)
            self._handle_failure(state, failed, "等待条件超时: %s" % wait_detail)
            if self._stop_event.is_set():
                return False
            if state.get("on_fail") == "stop":
                self.stop("等待超时: %s" % wait_detail)
                return False

        # 6) 断言
        failures = self._run_checks(state, baseline, window_start)
        if failures:
            self._fire_captures(captures, "on_fail", state, "断言失败")
            detail = "；".join(failures)
            failed = self._record_failure(state, "断言失败", detail)
            self._handle_failure(state, failed, "断言失败: %s" % detail)
            if self._stop_event.is_set():
                return False
            if state.get("on_fail") == "stop":
                self.stop("断言失败: %s" % detail)
                return False
        else:
            self._fire_captures(captures, "on_pass", state, "断言通过")
            self._record_pass(state)

        app.bus.info("runner", "状态「%s」完成（%.1fs）" % (
            name, self._now() - entered), state=name)
        return not self._stop_event.is_set()

    def _run_action(self, state, action):
        """执行状态卡上的动作。"""
        app = self._app
        kind = action.get("type")
        value = action.get("value") or ""
        if kind == "mark":
            app.probes.mark(value or "剧本标记")
        elif kind == "can_tx_start":
            ok, message = app.start_can_tx()
            app.bus.action("runner", "CAN 发送启动%s" % ("" if ok else "失败: " + message))
        elif kind == "can_tx_stop":
            ok, message = app.stop_can_tx()
            app.bus.action("runner", "CAN 发送停止%s" % ("" if ok else "失败: " + message))
        elif kind == "can_stim":
            mode = action.get("mode") or "any"
            if mode == "silence":
                ok, message = app.stop_stimuli()
            else:
                ok, message = app.start_stimulus(
                    mode, bus=action.get("bus") or None,
                    period_ms=int(action.get("period_ms") or 100),
                    can_id=action.get("id") or None, data=action.get("data") or None)
            app.bus.action("runner", "CAN 激励 %s %s" % (
                mode, "已启动" if ok else "失败: " + message))
        elif kind == "capture":
            when = action.get("when") or "entry"
            if when == "entry":
                self._capture(state, action, "进入状态")
            # 其余时机（on_pass/on_fail/on_timeout/interval）由 _execute_state 的钩子触发
        else:
            app.bus.warn("runner", "未知动作 %r" % (kind,))

    # -- 抓屏动作（触发时机可选） -----------------------------------------
    def _capture(self, state, action, reason):
        """按动作配置抓一帧并记一条日志；失败只告警不打断剧本。"""
        app = self._app
        name = state.get("name") or "状态"
        label = action.get("label") or "剧本_%s_%s" % (name, reason)
        ok, shot = app.capture_screen(label)
        if ok:
            app.bus.action("runner", "抓屏（%s）→ %s" % (reason, shot.get("name") or ""),
                           state=name)
        else:
            app.bus.warn("runner", "抓屏失败（%s）：%s" % (reason, shot), state=name)

    def _fire_captures(self, captures, when, state, reason=None):
        """触发所有指定时机的抓屏动作。"""
        for action in captures:
            if (action.get("when") or "entry") == when:
                self._capture(state, action, reason or when)

    def _start_interval_capture(self, state, captures, token):
        """为 interval 时机的抓屏起一个定时线程；状态切换或运行结束时自动退出。"""
        items = [a for a in captures if (a.get("when") or "") == "interval"]
        if not items:
            return None
        stop = threading.Event()

        def loop():
            while not stop.is_set() and not self._stop_event.is_set() \
                    and self._state_token is token:
                every = max(0.5, float(items[0].get("every_ms") or 2000) / 1000.0)
                if stop.wait(every):
                    return
                for action in items:
                    if stop.is_set() or self._stop_event.is_set() \
                            or self._state_token is not token:
                        return
                    self._capture(state, action, "定时")

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return stop

    def _update_mute(self):
        """按"当前应该有什么"降级监看：断电/断 IG 属剧本主动行为，不是事故。"""
        scenario = self.scenario or {}
        rules = self._app.probes.rules
        if not scenario.get("auto_mute_power_off", True):
            rules.set_context(True, True)
            return

        def is_on(channel_id):
            channel = self._app.channels.get(channel_id)
            return True if channel is None else (channel.state == "on")

        power_on = is_on("power")
        soc_on = power_on and is_on("ig")
        if not power_on:
            rules.set_context(False, False, "剧本主动断电")
        elif not soc_on:
            rules.set_context(True, False, "剧本主动断 IG")
        else:
            rules.set_context(True, True)

    def _sync_rule_context(self):
        """按**实际**通道状态同步一次监看上下文（开头/结尾各做一次）。"""
        def is_on(channel_id):
            channel = self._app.channels.get(channel_id)
            return True if channel is None else (channel.state == "on")

        power_on = is_on("power")
        self._app.probes.rules.set_context(power_on, power_on and is_on("ig"))

    def _can_baseline(self):
        """记录各路 CAN 当前的每 ID 计数，作为该状态的起点。"""
        can = self._app.can
        baseline = {}
        if can is None or can.state != "running":
            return baseline
        for bus in (can.channels or {}):
            baseline[bus] = {can_id: int(item.get("n") or 0)
                             for can_id, item in can.rx_ids(bus).items()}
        return baseline

    def _wait_condition(self, state, wait, window_start):
        """按 ``wait`` 等待条件成立；返回 ``(ok, detail)``。"""
        kind = (wait or {}).get("type") or "none"
        if kind == "none":
            return True, ""
        timeout_ms = int(wait.get("timeout_ms") or 5000)
        deadline = self._now() + timeout_ms / 1000.0
        probes = self._app.probes
        can = self._app.can
        while True:
            if self._stop_event.is_set():
                return False, "已停止"
            if self._paused_at:
                time.sleep(TICK_S)
                continue
            if kind == "adb_online" and probes.adb.online:
                return True, "SOC 在线"
            if kind == "serial_match":
                pattern = wait.get("value") or ""
                if self._match_serial(pattern, window_start):
                    return True, "串口出现 %s" % pattern
            if kind == "can_frame" and can is not None:
                bus = wait.get("bus") or "ican"
                can_id = str(wait.get("value") or "").lower()
                ids = can.rx_ids(bus)
                for key, item in ids.items():
                    if key.lower() == can_id and (item.get("age_ms") or 99999) < 1000:
                        return True, "%s 收到 %s" % (bus, can_id)
            if kind == "log_growing" and probes.adb.online and probes.adb.growth > 0:
                return True, "日志增长 %.0f B/s" % probes.adb.growth
            if kind == "pwr_state":
                try:
                    wanted = int(wait.get("value") or 0)
                except (TypeError, ValueError):
                    wanted = 0
                if probes.pwr_state() == wanted:
                    return True, "电源状态=%d" % wanted
            if self._now() >= deadline:
                return False, "%s 在 %dms 内未满足" % (kind, timeout_ms)
            if not self._sleep_until(min(deadline, self._now() + TICK_S), step=TICK_S):
                return False, "已停止"

    def _match_serial(self, pattern, since):
        """在状态窗口内的串口行里找正则。"""
        import re
        if not pattern:
            return False
        try:
            regex = re.compile(pattern)
        except re.error:
            return False
        for parsed in self._app.probes.recent_since(since):
            if regex.search(parsed.get("raw") or parsed.get("text") or ""):
                return True
        return False

    def _run_checks(self, state, baseline, window_start):
        """评估状态卡上的断言，返回失败描述列表。"""
        failures = []
        app = self._app
        recent = app.probes.recent_since(window_start)
        for check in state.get("checks") or []:
            kind = check.get("type")
            if kind == "no_error":
                hits = [parsed for parsed in recent
                        if (parsed.get("level") == "error"
                            or "[ERROR]" in (parsed.get("raw") or ""))]
                if hits:
                    failures.append("出现 %d 条 ERROR 日志，例如: %s"
                                    % (len(hits), (hits[0].get("raw") or "")[:120]))
            elif kind == "serial_seen":
                expect = check.get("expect", True)
                found = self._match_serial(check.get("value") or "", window_start)
                if bool(found) != bool(expect):
                    failures.append("串口%s出现 %r" % ("" if expect else "不应",
                                                      check.get("value")))
            elif kind == "can_seen":
                bus = check.get("bus") or "ican"
                can_id = str(check.get("value") or "").lower()
                need = int(check.get("min") or 1)
                before = (baseline.get(bus) or {}).get(can_id, 0)
                now = 0
                for key, item in (app.can.rx_ids(bus) if app.can else {}).items():
                    if key.lower() == can_id:
                        now = int(item.get("n") or 0)
                        break
                if now - before < need:
                    failures.append("%s 的 %s 本状态只收到 %d 帧（需 %d）"
                                    % (bus, can_id, now - before, need))
            elif kind == "adb_online":
                expect = bool(check.get("expect", True))
                grace = float(check.get("grace_ms")
                              if check.get("grace_ms") is not None
                              else (15000 if expect else 5000))
                if not self._wait_check(lambda: app.probes.adb.online == expect, grace):
                    failures.append("SOC %s在线（宽限 %.0fs 内未达成）"
                                    % ("" if expect else "不应", grace / 1000.0))
            elif kind == "pwr_state":
                try:
                    expect = int(check.get("expect") if check.get("expect") is not None else 0)
                except (TypeError, ValueError):
                    expect = 0
                grace = float(check.get("grace_ms")
                              if check.get("grace_ms") is not None else 5000)
                if not self._wait_check(lambda: app.probes.pwr_state() == expect, grace):
                    current = app.probes.pwr_state()
                    from . import stimulus
                    failures.append("电源状态是 %s(%s)，应为 %s(%d)（宽限 %.0fs 内未达成）"
                                    % (stimulus.state_name(current), current,
                                       stimulus.state_name(expect), expect, grace / 1000.0))
            elif kind == "pwr_seen":
                try:
                    expect = int(check.get("expect") or 0)
                except (TypeError, ValueError):
                    expect = 0
                if not app.probes.pwr_seen(window_start, expect):
                    from . import stimulus
                    failures.append("本状态窗口内从未进入 %s(%d)"
                                    % (stimulus.state_name(expect), expect))
            elif kind == "voltage":
                raw = app.probes.pwr.get("vbat")
                lower = float(check.get("min") if check.get("min") is not None else 18.0)
                upper = float(check.get("max") if check.get("max") is not None else 32.0)
                if raw is None:
                    failures.append("没有采到蓄电池电压（MCU 未上报 I_PWRMNG_IO）")
                else:
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        value = None
                    if value is None:
                        failures.append("电压值无法解析: %r" % (raw,))
                    elif not (lower <= value <= upper):
                        failures.append("电压 %.1fV 超出 %.1f~%.1fV" % (value, lower, upper))
                    else:
                        self._app.bus.info("runner", "电压 %.1fV 在 %.1f~%.1fV 内"
                                           % (value, lower, upper), state=state.get("name"))
            elif kind == "screen":
                if app.simulate:
                    continue
                min_ratio = float(check.get("min_ratio")
                                  if check.get("min_ratio") is not None else 0.15)
                attempts = max(1, int(check.get("attempts") or 3))
                gap_ms = int(check.get("gap_ms") or 200)
                best_ratio = 0.0
                last_error = ""
                passed = False
                for attempt in range(attempts):
                    ok, shot = app.capture_screen("断言_%s" % (state.get("name") or ""))
                    if not ok:
                        last_error = "抓屏失败: %s" % shot
                        break
                    ratio = float((shot.get("stats") or {}).get("bright_ratio") or 0.0)
                    best_ratio = max(best_ratio, ratio)
                    if ratio >= min_ratio:
                        passed = True
                        break
                    if attempt + 1 < attempts:
                        self._sleep_ms(gap_ms)
                if not passed:
                    if last_error:
                        failures.append(last_error)
                    else:
                        failures.append("画面像没画出来（非黑像素最好 %.1f%% < %.1f%%，共抓 %d 帧）"
                                        % (best_ratio * 100, min_ratio * 100, attempts))
            elif kind == "screen_region":
                if app.simulate:
                    continue
                if not app.screen_baseline:
                    app.bus.warn("runner", "没有画面基线，跳过 screen_region（先点「学基线」）")
                    continue
                tol = float(check.get("tol") if check.get("tol") is not None else 0.15)
                attempts = max(1, int(check.get("attempts") or 3))
                gap_ms = int(check.get("gap_ms") or 200)
                best_diff = None
                last_error = ""
                passed = False
                for attempt in range(attempts):
                    ok, shot = app.capture_screen("区域断言_%s" % (state.get("name") or ""))
                    if not ok:
                        last_error = "抓屏失败: %s" % shot
                        break
                    diff = app.screen_baseline_diff_any(shot)
                    if diff is None:
                        passed = True
                        break
                    best_diff = diff if best_diff is None else min(best_diff, diff)
                    if diff <= tol:
                        passed = True
                        break
                    if attempt + 1 < attempts:
                        self._sleep_ms(gap_ms)
                if not passed:
                    if last_error:
                        failures.append(last_error)
                    elif best_diff is not None:
                        failures.append("电压区域与基线差 %.3f（阈值 %.3f，共抓 %d 帧最优值）"
                                        % (best_diff, tol, attempts))
                elif best_diff is not None:
                    app.bus.info("runner", "电压区域与基线一致（最优差 %.3f）" % best_diff,
                                 state=state.get("name"))
            elif kind == "screen_text":
                if app.simulate:
                    continue
                tol_v = float(check.get("tol_v") if check.get("tol_v") is not None else 0.3)
                expect_raw = check.get("expect")
                expect = None
                try:
                    if str(expect_raw).lower() not in ("auto", "", "none"):
                        expect = float(expect_raw)
                except (TypeError, ValueError):
                    expect = None
                if expect is None:
                    mcu = app.probes.pwr.get("vbat")
                    try:
                        expect = float(mcu) if mcu is not None else None
                    except (TypeError, ValueError):
                        expect = None
                attempts = max(1, int(check.get("attempts") or 3))
                gap_ms = int(check.get("gap_ms") or 200)
                metric = (check.get("metric") or "voltage").strip()
                defs = getattr(app, "METRIC_DEFS", {}) or {}
                metric_label = (defs.get(metric) or {}).get("label", metric)
                metric_unit = (defs.get(metric) or {}).get("unit", "")
                region = (getattr(app, "read_regions", None) or {}).get(metric) or app.read_region
                from . import ocr
                shown = None
                used = ""
                last_error = ""
                for attempt in range(attempts):
                    ok, shot = app.capture_screen("文字断言_%s" % (state.get("name") or ""))
                    if not ok:
                        last_error = "抓屏失败: %s" % shot
                        break
                    # 1) 真 OCR：直接读出画面上的数字（任意数值都行）
                    try:
                        parsed = ocr.read_number(shot.get("path") or "", region)
                    except Exception as exc:
                        parsed = None
                        app.bus.debug("runner", "OCR 失败: %s" % exc)
                    if parsed and parsed.get("value") is not None:
                        shown = float(parsed["value"])
                        used = "OCR"
                        if expect is not None and abs(shown - expect) <= tol_v:
                            break
                        if attempt + 1 < attempts:
                            self._sleep_ms(gap_ms)
                        continue
                    # 2) OCR 读不出：只有电压有"带标注模板"可回退（其它指标没有样本）
                    if metric == "voltage" and app.screen_baseline:
                        found = app.screen_baseline_best(shot, labeled=True)
                        limit = float(check.get("tol") if check.get("tol") is not None else 0.15)
                        if found and found[0] <= limit and found[1].get("value") is not None:
                            shown = float(found[1]["value"])
                            used = "模板"
                            if expect is not None and abs(shown - expect) <= tol_v:
                                break
                    if attempt + 1 < attempts:
                        self._sleep_ms(gap_ms)
                if last_error:
                    failures.append(last_error)
                elif shown is None:
                    failures.append("%s 读数读不出来（OCR 与样本比对均失败，共抓 %d 帧）"
                                    % (metric_label, attempts))
                elif expect is not None and abs(shown - expect) > tol_v:
                    failures.append("%s 显示 %.1f%s，期望 %.1f%s（容差 %.1f，%s）"
                                    % (metric_label, shown, metric_unit, expect,
                                       metric_unit, tol_v, used or "?"))
                else:
                    app.bus.info("runner", "%s 显示 %.2f%s（%s）"
                                 % (metric_label, shown, metric_unit, used or "?"),
                                 state=state.get("name"))
        return failures

    def _record_pass(self, state):
        """记一次通过。"""
        with self._lock:
            slot = self.results.setdefault(state.get("id"), {"pass": 0, "fail": 0,
                                                             "last": ""})
            slot["pass"] += 1
            slot["last"] = "ok"
            self.fail_streak = 0

    def _record_failure(self, state, kind, detail):
        """记一次失败，返回失败描述 dict。"""
        with self._lock:
            slot = self.results.setdefault(state.get("id"), {"pass": 0, "fail": 0,
                                                             "last": ""})
            slot["fail"] += 1
            slot["last"] = kind
            self.fail_streak += 1
        return {"kind": kind, "detail": detail, "state": state.get("name"),
                "state_id": state.get("id")}

    def _handle_failure(self, state, failure, reason):
        """按 ``on_fail`` 处理断言/等待失败。"""
        app = self._app
        mode = state.get("on_fail") or "continue"
        app.bus.warn("runner", "状态「%s」%s：%s" % (state.get("name"), failure["kind"],
                                                    failure["detail"]),
                     state=state.get("name"))
        if mode == "continue":
            return
        incident = app.incidents.capture(reason, severity="error",
                                         detail=failure["detail"],
                                         state=state.get("name"), freeze=True)
        app.runner_incident(incident)
        if mode == "freeze":
            self.stop("现场保全后停止：%s" % failure["kind"])

    # -- 快照 -------------------------------------------------------------
    def elapsed_ms(self):
        """运行时长（扣除暂停，ms）。"""
        if not self.started_at:
            return 0
        end = self.finished_at or self._now()
        return int(max(0.0, end - self.started_at) * 1000)

    def snapshot(self):
        """界面用运行状态。"""
        with self._lock:
            scenario = self.scenario or {}
            states = scenario.get("states") or []
            state_name = ""
            if 0 <= self.state_index < len(states):
                state_name = states[self.state_index].get("name") or ""
            return {
                "phase": self.phase,
                "scenario": scenario.get("name") or "",
                "iteration": self.iteration,
                "loops": self.loops,
                "state_index": self.state_index,
                "state_name": state_name,
                "elapsed_ms": self.elapsed_ms(),
                "run_id": self.run_id,
                "run_dir": self.run_dir,
                "error": self.last_error,
                "results": dict(self.results),
                "fail_streak": self.fail_streak,
                "incidents": self.incidents,
                "total_states": len(states),
            }

    def state_of(self):
        """当前状态名（探针打事件用）。"""
        with self._lock:
            return self.state_name if self.phase in (PHASE_RUNNING, PHASE_PAUSED) else ""


class _Stopped(Exception):
    """内部信号：剧本被停止。"""
