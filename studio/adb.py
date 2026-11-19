# -*- coding: utf-8 -*-
"""adb.py — DUT（SOC）侧取证：在线判定、uptime、日志体积、截图、日志尾部

继电器没有回读、SOC 是否真的起来/掉下去只能从 DUT 侧取证，本模块把常用的几条
adb 命令包起来，供探针（在线/日志增长判定）与现场保全（截图 + 日志尾部）使用。

约定（本机实测）：

* ``adb devices`` 有 ``device`` 行 = SOC 在线；
* ``cat /proc/uptime`` 秒级累加 = 系统在跑，数值很小 = 刚重启；
* ``wc -c < /userdata/log/mcu/mcu/mcu.log`` 持续增长 = MCU 在刷日志（间接证明 SPI 通）；
* 设备日志时间 = 编译日零点 UTC + uptime，与 PC 时间差 7~8 小时属正常。
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import zlib

DEFAULT_ADB = ""            # 空 = 自动探测（PATH / 随包 platform-tools / 常见安装位置）
DEFAULT_LOG_PATH = "/userdata/log/mcu/mcu/mcu.log"
STUDIO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 仪表主页面"蓄电池电压"数字所在的区域（按整帧比例），用于和基线做像素比对。
# 960x360 缩略图上大致是 x 607..660 / y 330..348。
VOLTAGE_REGION = {"x0": 0.625, "x1": 0.700, "y0": 0.900, "y1": 0.985}
GRID_COLS = 16
GRID_ROWS = 8


def _region_grid(data, width, height, region=None, cols=GRID_COLS, rows=GRID_ROWS):
    """取一块区域，压成 ``cols x rows`` 的平均亮度网格（用于和基线比对）。

    返回 ``(grid, rect)``；``grid`` 是 128 个 0..255 的整数，``rect`` 是像素坐标。
    """
    region = region or VOLTAGE_REGION
    x0 = max(0, int(round(region["x0"] * width)))
    x1 = min(width, int(round(region["x1"] * width)))
    y0 = max(0, int(round(region["y0"] * height)))
    y1 = min(height, int(round(region["y1"] * height)))
    if x1 <= x0 or y1 <= y0:
        return [], {"x0": x0, "x1": x1, "y0": y0, "y1": y1}
    cell_w = max(1, (x1 - x0) // cols)
    cell_h = max(1, (y1 - y0) // rows)
    grid = []
    for row in range(rows):
        cy0 = y0 + row * cell_h
        cy1 = min(y1, cy0 + cell_h)
        for col in range(cols):
            cx0 = x0 + col * cell_w
            cx1 = min(x1, cx0 + cell_w)
            total = 0
            count = 0
            for y in range(cy0, cy1):
                base = y * width * 4
                for x in range(cx0, cx1):
                    pos = base + x * 4
                    total += (data[pos + 2] * 299 + data[pos + 1] * 587 + data[pos] * 114) // 1000
                    count += 1
            grid.append(int(total / count) if count else 0)
    return grid, {"x0": x0, "x1": x1, "y0": y0, "y1": y1}


def grid_distance(left, right):
    """两个亮度网格的平均绝对差（0 越大越不像）。"""
    if not left or not right or len(left) != len(right):
        return 999.0
    return sum(abs(a - b) for a, b in zip(left, right)) / float(len(left))


def normalize_grid(grid):
    """把亮度网格归一化成"墨迹分布"：|灰度-区域均值| 再除以标准差。

    日/夜主题会把同一块区域整体变亮或变暗（白底黑字 ↔ 黑底白字），
    取绝对值后极性被抹掉，剩下"哪里有笔画、哪里是空白"的形状，
    于是同一数值跨主题仍然接近，不同数值仍然明显不同。
    """
    if not grid:
        return []
    mean = sum(grid) / float(len(grid))
    dev = [abs(value - mean) for value in grid]
    energy = (sum(value * value for value in dev) / float(len(dev))) ** 0.5
    if energy < 1e-6:
        energy = 1.0
    return [value / energy for value in dev]


def grid_distance_norm(left, right):
    """归一化后的网格距离（主题无关）；0.08≈同数值，0.26+≈不同数值。"""
    a = normalize_grid(left)
    b = normalize_grid(right)
    if not a or not b or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a))


def _png_chunk(tag, data):
    """拼一个 PNG chunk（长度 + tag + 数据 + CRC）。"""
    return (len(data).to_bytes(4, "big") + tag + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big"))


def _write_png(path, width, height, rows):
    """自带 PNG 编码器（RGB24，不依赖 Pillow）。"""
    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", ihdr)
           + _png_chunk(b"IDAT", zlib.compress(raw, 6))
           + _png_chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(png)


def _bgra_to_png(raw_path, width, height, out_png, out_width=960, region=None):
    """DRM framebuffer 是 BGRA，按步长抽样转成 RGB PNG；返回 (宽, 高, 统计)。

    统计用于"画面是否真的画出来了"：``bright_ratio`` 是亮度 > 30 的像素占比
    （纯黑/没渲染时接近 0），``text_ratio`` 是亮度 > 180 的像素占比（文字/高亮）。
    """
    with open(raw_path, "rb") as handle:
        data = handle.read()
    step = 1
    if out_width and out_width < width:
        step = max(1, width // out_width)
    out_h = (height + step - 1) // step
    out_w = (width + step - 1) // step
    rows = []
    bright = 0
    textish = 0
    total = 0
    luma_sum = 0
    for y in range(0, height, step):
        base = y * width * 4
        row = bytearray(out_w * 3)
        offset = 0
        for x in range(0, width, step):
            pos = base + x * 4
            blue = data[pos]
            green = data[pos + 1]
            red = data[pos + 2]
            row[offset] = red
            row[offset + 1] = green
            row[offset + 2] = blue
            offset += 3
            peak = red if red > green else green
            if blue > peak:
                peak = blue
            if peak > 30:
                bright += 1
            if peak > 180:
                textish += 1
            luma_sum += (red * 299 + green * 587 + blue * 114) // 1000
            total += 1
        rows.append(bytes(row))
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    _write_png(out_png, out_w, out_h, rows)
    stats = {
        "width": width, "height": height,
        "out_width": out_w, "out_height": out_h,
        "bright_ratio": round(bright / float(total or 1), 4),
        "text_ratio": round(textish / float(total or 1), 4),
        "mean_luma": round(luma_sum / float(total or 1), 1),
        "bytes": os.path.getsize(out_png) if os.path.isfile(out_png) else 0,
    }
    try:
        # 用与输出 PNG 同一套降采样像素算电压区域网格，保证与"从截图学基线"的口径一致
        small = bytearray(out_w * out_h * 4)
        for y in range(out_h):
            row = rows[y]
            base = y * out_w * 4
            for x in range(out_w):
                src = x * 3
                dst = base + x * 4
                small[dst] = row[src + 2]
                small[dst + 1] = row[src + 1]
                small[dst + 2] = row[src]
                small[dst + 3] = 255
        grid, rect = _region_grid(bytes(small), out_w, out_h, region)
        stats["voltage_grid"] = grid
        stats["voltage_rect"] = rect
    except Exception:
        pass
    return out_w, out_h, stats


def find_adb(exe_path=None):
    """定位 adb：用户配置 > PATH > 随包 platform-tools > Android SDK / 常见安装位置。"""
    candidates = []
    if exe_path:
        candidates.append(exe_path)
    found = shutil.which("adb")
    if found:
        candidates.append(found)
    candidates.extend([
        os.path.join(STUDIO_DIR, "platform-tools", "adb.exe"),
        os.path.join(os.path.dirname(STUDIO_DIR), "platform-tools", "adb.exe"),
        os.path.join(STUDIO_DIR, "tools", "adb.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expandvars(r"%ANDROID_HOME%\platform-tools\adb.exe"),
        os.path.expandvars(r"%ANDROID_SDK_ROOT%\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        r"C:\Android\platform-tools\adb.exe",
    ])
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return exe_path or DEFAULT_ADB


class AdbRunner(object):
    """adb 命令封装（同步、带超时；失败时返回空结果而不是抛异常）。"""

    def __init__(self, exe_path=None, serial=None, log=None):
        self.exe_path = find_adb(exe_path)
        self.serial = serial or ""
        self._log = log
        self.last_error = None
        self.calls = 0
        details = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._flags = details if os.name == "nt" else 0

    @property
    def available(self):
        """adb 可执行文件是否存在。"""
        return os.path.isfile(self.exe_path)

    def _argv(self, args, with_serial=True):
        base = [self.exe_path]
        if with_serial and self.serial:
            base += ["-s", self.serial]
        return base + list(args)

    def run(self, args, timeout=6.0, with_serial=True):
        """执行一条 adb 子命令，返回 ``(returncode, stdout)``。"""
        if not self.available:
            return 127, ""
        self.calls += 1
        try:
            proc = subprocess.run(self._argv(args, with_serial), capture_output=True,
                                  timeout=timeout, text=True, encoding="utf-8",
                                  errors="replace", creationflags=self._flags)
        except subprocess.TimeoutExpired:
            self.last_error = "adb 超时: %s" % " ".join(args)
            return 124, ""
        except Exception as exc:
            self.last_error = "adb 失败: %s" % exc
            return 1, ""
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 and not output:
            output = (proc.stderr or "").strip()
            self.last_error = output[:200] or ("adb 退出码 %d" % proc.returncode)
        # 设备重启/重连时 "-s <序列号>" 会短暂报 not found：丢掉钉住的序列号，
        # 下次重新枚举，否则探针会一直以为设备不在
        if proc.returncode != 0 and "not found" in (proc.stderr or "").lower():
            self.serial = ""
        return proc.returncode, output

    def devices(self):
        """在线设备序列号列表（**不带 -s**，否则 DUT 重启期间永远查不到自己）。"""
        code, output = self.run(["devices"], timeout=8.0, with_serial=False)
        if code != 0:
            return []
        found = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                found.append(parts[0])
        return found

    def online(self):
        """DUT 是否在线（有 device 行）。"""
        return bool(self.devices())

    def shell(self, command, timeout=6.0):
        """执行 shell 命令，返回 ``(rc, stdout)``。"""
        return self.run(["shell", command], timeout=timeout)

    def uptime(self):
        """``/proc/uptime`` 的第一个数（秒）；取不到返回 ``None``。"""
        code, output = self.shell("cat /proc/uptime", timeout=5.0)
        if code != 0 or not output:
            return None
        try:
            return float(output.split()[0])
        except (ValueError, IndexError):
            return None

    def file_size(self, path=DEFAULT_LOG_PATH):
        """远端文件字节数；取不到返回 ``None``。"""
        code, output = self.shell("wc -c < %s" % path, timeout=5.0)
        if code != 0 or not output:
            return None
        try:
            return int(output.strip().split()[0])
        except (ValueError, IndexError):
            return None

    def log_tail(self, path=DEFAULT_LOG_PATH, lines=200):
        """远端日志尾部文本。"""
        code, output = self.shell("tail -n %d %s" % (int(lines), path), timeout=8.0)
        return output if code == 0 else ""

    def log_grep(self, pattern, path=DEFAULT_LOG_PATH, lines=50):
        """远端日志里 grep 一段（无输出返回空串）。"""
        code, output = self.shell("grep -a -n -m %d %s %s"
                                  % (int(lines), _quote(pattern), path), timeout=8.0)
        return output if code == 0 else ""

    def screenshot(self, local_path, width=960, region=None):
        """抓 DUT 屏幕。

        两条路，优先用 VOP dump（内核把 VOP 输入端那一帧原样落盘，几何信息写在
        文件名里，最贴近"屏幕实际在扫的图"）：

        1. ``vop_dump``：``echo dump > .../vop_dump/dump`` → 内核写 ``/data/*.bin``
           → 按文件名里的 ``WxH_stride-N_fmt`` 解析 → 拉回转 PNG → 删掉 .bin；
        2. ``kmsgrab``：读 DRM framebuffer（``vop_dump`` 不可用时兜底）。

        返回 ``(ok, 说明, 统计)``；统计含 ``bright_ratio``/``text_ratio``/``mean_luma``。
        """
        errors = []
        for backend in (self._screenshot_vopdump, self._screenshot_drm):
            try:
                return backend(local_path, width, region)
            except Exception as exc:
                errors.append("%s: %s" % (backend.__name__.replace("_screenshot_", ""), exc))
                self.last_error = errors[-1]
        ok, detail = self._screenshot_screencap(local_path)
        stats = {"bytes": os.path.getsize(local_path) if ok else 0}
        return ok, "%s（%s）" % (detail, "；".join(errors)), stats

    # -- VOP dump 抓帧 ---------------------------------------------------
    def _vopdump_node(self):
        """选一个 ACTIVE 视频口的 ``vop_dump/dump`` 节点。"""
        nodes = []
        out = self._dev("find /sys/kernel/debug/dri -path '*vop_dump/dump' 2>/dev/null", timeout=15)
        for line in (out or "").splitlines():
            node = line.strip()
            if node:
                nodes.append(node)
        if not nodes:
            raise RuntimeError("没有 vop_dump 节点（debugfs 未挂或内核未开）")
        summary = self._dev("cat /sys/kernel/debug/dri/0/summary 2>/dev/null", timeout=10) or ""
        for node in nodes:
            match = re.search(r"/video_port(\d+)/", node)
            if match and ("Video Port%s: ACTIVE" % match.group(1)) in summary:
                return node
        return nodes[0]

    def _screenshot_vopdump(self, local_path, width, region=None):
        """触发一次 VOP dump，拉回并转 PNG；用完删掉设备上的 .bin。"""
        node = self._vopdump_node()
        self._dev("mkdir -p /data/vop_buf 2>/dev/null; rm -f /data/*.bin 2>/dev/null", timeout=15)
        self._dev("echo dump > %s" % node, timeout=20)

        name = ""
        last_size = -1
        for _ in range(40):
            time.sleep(0.2)
            listing = self._dev(
                "ls -l /data/*.bin 2>/dev/null | awk '{print $5\" \"$9}' | head -1", timeout=10) or ""
            parts = listing.split()
            if len(parts) < 2:
                continue
            try:
                size = int(parts[0])
            except ValueError:
                continue
            if size > 0 and size == last_size:
                name = parts[1]
                break
            last_size = size
        if not name:
            raise RuntimeError("VOP dump 未产出文件（看 dmesg / dump 节点）")

        match = re.search(r"_fb-(\d+)x(\d+)_stride-(\d+)", name)
        if not match:
            raise RuntimeError("文件名解析不出几何信息: %s" % os.path.basename(name))
        src_w, src_h, stride = int(match.group(1)), int(match.group(2)), int(match.group(3))
        fourcc = re.search(r"_([A-Z0-9]{4})_\d+\.bin$", name)
        fourcc = fourcc.group(1) if fourcc else "XR24"
        if fourcc not in ("XR24", "AR24", "AB24", "RG24"):
            raise RuntimeError("暂不支持的像素格式 %s" % fourcc)

        handle, local_raw = tempfile.mkstemp(suffix=".bin", prefix="cbstudio_vop_")
        os.close(handle)
        code, output = self.run(["pull", name, local_raw], timeout=60.0)
        self._dev("rm -f %s" % name, timeout=10)
        if code != 0:
            os.remove(local_raw)
            raise RuntimeError("adb pull 失败: %s" % output[:120])
        try:
            if stride != src_w * 4:
                # 行宽有对齐填充：先压成紧凑 BGRA 再转
                with open(local_raw, "rb") as handle_in:
                    raw = handle_in.read()
                packed = bytearray(src_w * src_h * 4)
                for row in range(src_h):
                    src_off = row * stride
                    packed[row * src_w * 4:(row + 1) * src_w * 4] = raw[src_off:src_off + src_w * 4]
                with open(local_raw, "wb") as handle_out:
                    handle_out.write(packed)
            out_w, out_h, stats = _bgra_to_png(local_raw, src_w, src_h, local_path, width, region)
        finally:
            try:
                os.remove(local_raw)
            except OSError:
                pass
        return True, "vopdump %dx%d(%s) -> %dx%d" % (src_w, src_h, fourcc, out_w, out_h), stats

    # -- DRM framebuffer 抓帧 -------------------------------------------
    def _dev(self, command, timeout=60.0):
        """执行设备侧 shell 命令，返回 stdout。"""
        code, output = self.run(["shell", command], timeout=timeout)
        if code != 0:
            raise RuntimeError("设备命令失败: %s (%s)" % (command, output[:120]))
        return output

    def _find_crtc(self):
        """从 ``kmsgrab -h`` 里读 ``Valid crtcs:``（换板子会变，不写死）。"""
        output = self._dev("kmsgrab -h 2>&1 | head -8", timeout=20.0)
        match = re.search(r"Valid crtcs:\s*(\d+)", output or "")
        if not match:
            raise RuntimeError("kmsgrab -h 里没有 Valid crtcs")
        return int(match.group(1))

    def _screenshot_drm(self, local_path, width, region=None):
        """kmsgrab 一帧 -> 拉回 -> 转 PNG。"""
        import tempfile
        import zlib
        dev_raw = "/tmp/cbstudio_shot.raw"
        dev_err = "/tmp/cbstudio_shot.err"
        crtc = self._find_crtc()
        self._dev("rm -f %s %s" % (dev_raw, dev_err), timeout=10.0)
        self._dev("kmsgrab --crtc %d > %s 2>%s" % (crtc, dev_raw, dev_err), timeout=40.0)
        err = self._dev("cat %s" % dev_err, timeout=10.0)
        size_text = (self._dev("stat -c %%s %s" % dev_raw, timeout=10.0) or "").strip()
        if not size_text.isdigit():
            raise RuntimeError("设备上没有生成 raw 文件: %s" % err[:120])
        size = int(size_text)
        match = re.search(r"size=(\d+)x(\d+)", err or "")
        if not match:
            raise RuntimeError("读不到分辨率: %s" % err[:120])
        src_w, src_h = int(match.group(1)), int(match.group(2))
        if size != src_w * src_h * 4:
            raise RuntimeError("raw 大小 %d != %dx%dx4" % (size, src_w, src_h))

        handle, local_raw = tempfile.mkstemp(suffix=".raw", prefix="cbstudio_")
        os.close(handle)
        code, output = self.run(["pull", dev_raw, local_raw], timeout=60.0)
        self._dev("rm -f %s %s" % (dev_raw, dev_err), timeout=10.0)
        if code != 0:
            os.remove(local_raw)
            raise RuntimeError("adb pull 失败: %s" % output[:120])
        try:
            out_w, out_h, stats = _bgra_to_png(local_raw, src_w, src_h, local_path, width, region)
        finally:
            try:
                os.remove(local_raw)
            except OSError:
                pass
        return True, "kmsgrab %dx%d -> %dx%d" % (src_w, src_h, out_w, out_h), stats

    def _screenshot_screencap(self, local_path):
        """Android 设备兜底：``exec-out screencap -p``。"""
        if not self.available:
            return False, "adb 不可用"
        try:
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            proc = subprocess.run(self._argv(["exec-out", "screencap -p"]),
                                  capture_output=True, timeout=15.0,
                                  creationflags=self._flags)
            if proc.returncode != 0 or len(proc.stdout) < 1000:
                return False, "screencap 不可用（设备不是 Android？）"
            with open(local_path, "wb") as handle:
                handle.write(proc.stdout)
            return True, "screencap %d 字节" % len(proc.stdout)
        except Exception as exc:
            return False, "截图异常: %s" % exc

    def snapshot(self, log_path=DEFAULT_LOG_PATH):
        """探针周期采样：在线/uptime/日志体积。"""
        devices = self.devices()
        online = bool(devices)
        if devices and self.serial not in devices:
            self.serial = devices[0]          # 换设备或重启后重新钉住
        return {
            "available": self.available,
            "online": online,
            "devices": devices,
            "uptime": self.uptime() if online else None,
            "log_size": self.file_size(log_path) if online else None,
            "error": self.last_error,
            "t": time.time(),
        }


def _quote(text):
    """把参数包一层单引号（远端 shell 用）。"""
    return "'" + str(text).replace("'", "'\\''") + "'"
