# -*- coding: utf-8 -*-
"""can_pack.py — 报文位打包（``FrameBuilder``）

``can_engine.py`` 的周期随机发送要把 ``can_matrix_manifest.json`` 里的一条条信号
塞进 8 字节载荷，本模块负责这件事。**只依赖标准库**，同时兼容主进程的 64 位
Python 3.10+ 与 CAN 子进程的 32 位 Python 3.8。

信号描述（来自报文清单的 ``signals[i]``）::

    {"name": "AEBS1_WorkSts", "start": 0, "length": 4,
     "byte_order": "intel" | "motorola", "signed": false,
     "scale": 1.0, "offset": 0.0,
     "raw_min": 0, "raw_max": 15, "init_raw": 0}

位序约定与 DBC / CANdb++ 一致：

* ``intel``（小端）：``start`` 是信号的**最低位**在载荷里的线性位号，
  位号 ``n`` 落在第 ``n // 8`` 字节的第 ``n % 8`` 位（bit0 最右）。
* ``motorola``（大端）：``start`` 是信号的**最高位**在"DBC 锯齿位号"里的位置，
  即第 ``start // 8`` 字节的第 ``start % 8`` 位；信号从该位向下延伸到更低的有效位，
  位号 0 之后跳到下一个字节的 bit7。

用法::

    builder = FrameBuilder(8)
    builder.set_raw({"start": 0, "length": 4}, 0x5)
    payload = builder.to_bytes()          # b'\\x05\\x00\\x00\\x00\\x00\\x00\\x00\\x00'
"""

MAX_PAYLOAD = 64          # CANFD 单帧最大字节数


def _signal_field(signal, *names):
    """从信号字典里按候选键名取值（兼容 ``length`` / ``bit_length`` 两种写法）。"""
    for name in names:
        if name in signal:
            return signal[name]
    return None


def _intel_positions(start, length):
    """小端位序：返回**低位在前**的线性位号列表。"""
    return [start + index for index in range(length)]


def _motorola_positions(start, length):
    """大端位序：``start`` 是最高位，返回**低位在前**的线性位号列表。"""
    positions = []
    bit = int(start)
    for _index in range(length):
        positions.append(bit)
        if bit % 8 == 0:
            bit += 15                 # 当前字节 bit0 之后落到下一个字节的 bit7
        else:
            bit -= 1
    positions.reverse()               # 转成低位在前
    return positions


class FrameBuilder(object):
    """把若干信号按位域写进一帧载荷。"""

    __slots__ = ("dlc", "_data", "_touched")

    def __init__(self, dlc=8):
        self.dlc = max(0, min(int(dlc or 0), MAX_PAYLOAD))
        self._data = bytearray(self.dlc)
        self._touched = False

    # -- 内部 -------------------------------------------------------------
    @staticmethod
    def _positions(signal):
        """按字节序算出信号的线性位号（低位在前）。"""
        start = int(_signal_field(signal, "start", "start_bit", "startBit") or 0)
        length = int(_signal_field(signal, "length", "bit_length", "size") or 0)
        order = str(_signal_field(signal, "byte_order", "byteOrder") or "intel").lower()
        if length <= 0:
            return []
        if order in ("motorola", "big_endian", "big", "msb"):
            return _motorola_positions(start, length)
        return _intel_positions(start, length)

    @staticmethod
    def _bit_length(signal):
        """信号位宽（负数按无符号处理：位宽即其绝对范围）。"""
        length = int(_signal_field(signal, "length", "bit_length", "size") or 0)
        return max(0, length)

    # -- 写入 -------------------------------------------------------------
    def set_raw(self, signal, raw):
        """按**原始值**写一个信号（不做 scale/offset 换算）。"""
        positions = self._positions(signal)
        length = self._bit_length(signal)
        if not positions or length <= 0:
            return self
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            value = 0
        mask = (1 << length) - 1
        value &= mask
        for index in range(length):
            if not (value >> index) & 1:
                continue
            position = positions[index]
            byte_index = position >> 3
            if byte_index >= self.dlc:
                continue
            self._data[byte_index] |= 1 << (position & 7)
            self._touched = True
        return self

    def set_phys(self, signal, value):
        """按**物理值**写一个信号（用 ``scale`` / ``offset`` 反算原始值）。"""
        scale = float(_signal_field(signal, "scale") or 1.0) or 1.0
        offset = float(_signal_field(signal, "offset") or 0.0)
        raw = int(round((float(value) - offset) / scale))
        return self.set_raw(signal, raw)

    def set_initials(self, signals):
        """把信号表里带 ``init_raw`` 的项按初值填进去。"""
        for signal in signals or []:
            init = _signal_field(signal, "init_raw")
            if init is None:
                continue
            self.set_raw(signal, init)
        return self

    def set_bytes(self, data):
        """直接整帧写入原始字节（``bytes`` / ``bytearray`` / hex 串）。"""
        if isinstance(data, (bytes, bytearray)):
            payload = bytes(data)
        else:
            text = str(data or "").replace(" ", "").replace("-", "").replace(",", "")
            payload = bytes.fromhex(text) if text else b""
        for index, byte in enumerate(payload[:self.dlc]):
            self._data[index] = byte
            self._touched = True
        return self

    # -- 读出 -------------------------------------------------------------
    def get_raw(self, signal):
        """读回一个信号的原始值（反解用；主要在自检里用）。"""
        positions = self._positions(signal)
        if not positions:
            return 0
        value = 0
        for index, position in enumerate(positions):
            byte_index = position >> 3
            if byte_index >= self.dlc:
                continue
            if (self._data[byte_index] >> (position & 7)) & 1:
                value |= 1 << index
        length = self._bit_length(signal)
        if length and (_signal_field(signal, "signed") or False):
            if value & (1 << (length - 1)):
                value -= 1 << length
        return value

    def to_bytes(self):
        """输出载荷。"""
        return bytes(self._data)

    def to_hex(self, sep=" "):
        """输出 ``"00 50 00 ..."`` 形式。"""
        return sep.join("%02X" % byte for byte in self._data)

    def __len__(self):
        return self.dlc

    def __repr__(self):
        return "FrameBuilder(dlc=%d, hex=%r)" % (self.dlc, self.to_hex())


# ---------------------------------------------------------------------------
# 自检：python can_pack.py
# ---------------------------------------------------------------------------

def _selftest():
    """跑一遍已知的位域用例，返回失败条数。"""
    failures = []
    total = [0]

    def check(name, got, want):
        total[0] += 1
        if got != want:
            failures.append("%s: got %r want %r" % (name, got, want))

    # 1) 小端 4 bit @0
    builder = FrameBuilder(8)
    builder.set_raw({"start": 0, "length": 4, "byte_order": "intel"}, 0xF)
    check("intel nibble", builder.to_hex(), "0F 00 00 00 00 00 00 00")

    # 2) 小端 4 bit @4 -> byte0 高半字节
    builder = FrameBuilder(8)
    builder.set_raw({"start": 4, "length": 4, "byte_order": "intel"}, 0xA)
    check("intel high nibble", builder.to_hex(), "A0 00 00 00 00 00 00 00")

    # 3) 转向灯：0x18FE4121 左/右转向 = start 14/12，2 bit -> byte1 = 0x50
    builder = FrameBuilder(8)
    builder.set_raw({"start": 14, "length": 2, "byte_order": "intel"}, 1)
    builder.set_raw({"start": 12, "length": 2, "byte_order": "intel"}, 1)
    check("turn lamp 0x50", builder.to_hex(), "00 50 00 00 00 00 00 00")

    # 4) 双闪：start 2，2 bit -> byte0 = 0x04
    builder = FrameBuilder(8)
    builder.set_raw({"start": 2, "length": 2, "byte_order": "intel"}, 1)
    check("hazard 0x04", builder.to_hex(), "04 00 00 00 00 00 00 00")

    # 5) BMS1 stMode：start 0，4 bit -> 5
    builder = FrameBuilder(8)
    builder.set_raw({"start": 0, "length": 4, "byte_order": "intel"}, 5)
    check("bms1 stMode 0x05", builder.to_hex(), "05 00 00 00 00 00 00 00")

    # 6) 跨字节小端 12 bit @4
    builder = FrameBuilder(8)
    builder.set_raw({"start": 4, "length": 12, "byte_order": "intel"}, 0xABC)
    check("intel cross-byte", builder.to_hex(), "C0 AB 00 00 00 00 00 00")

    # 7) 大端 8 bit @7 -> byte0
    builder = FrameBuilder(8)
    builder.set_raw({"start": 7, "length": 8, "byte_order": "motorola"}, 0x12)
    check("motorola 1 byte", builder.to_hex(), "12 00 00 00 00 00 00 00")

    # 8) 大端 16 bit @7 -> byte0/byte1 大端序
    builder = FrameBuilder(8)
    builder.set_raw({"start": 7, "length": 16, "byte_order": "motorola"}, 0x1234)
    check("motorola 2 byte", builder.to_hex(), "12 34 00 00 00 00 00 00")

    # 9) 大端 4 bit @3 -> byte0 低半字节
    builder = FrameBuilder(8)
    builder.set_raw({"start": 3, "length": 4, "byte_order": "motorola"}, 0x5)
    check("motorola low nibble", builder.to_hex(), "05 00 00 00 00 00 00 00")

    # 10) 读回一致（小端 56 bit 全宽）
    signal = {"start": 0, "length": 56, "byte_order": "intel"}
    builder = FrameBuilder(8)
    builder.set_raw(signal, 0x0123456789ABCD)
    check("round trip raw", builder.get_raw(signal), 0x0123456789ABCD)

    # 11) 有符号读回
    signal = {"start": 0, "length": 8, "byte_order": "intel", "signed": True}
    builder = FrameBuilder(8)
    builder.set_raw(signal, -2)
    check("signed pack", builder.to_hex(), "FE 00 00 00 00 00 00 00")
    check("signed unpack", builder.get_raw(signal), -2)

    # 12) 物理值换算
    signal = {"start": 0, "length": 8, "byte_order": "intel", "scale": 0.5, "offset": -40.0}
    builder = FrameBuilder(8)
    builder.set_phys(signal, 25.0)                 # (25 + 40) / 0.5 = 130
    check("phys pack", builder.to_hex(), "82 00 00 00 00 00 00 00")

    # 13) dlc=5 只输出 5 字节
    builder = FrameBuilder(5)
    builder.set_bytes("11 22 33 44 55 66")
    check("dlc truncation", builder.to_bytes(), bytes([0x11, 0x22, 0x33, 0x44, 0x55]))

    for line in failures:
        print("FAIL %s" % line)
    print("%d/%d passed" % (total[0] - len(failures), total[0]))
    return len(failures)


if __name__ == "__main__":
    import sys
    sys.exit(1 if _selftest() else 0)
