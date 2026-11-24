"""dbc.py — 极简 DBC 解析 + 信号解码（只依赖标准库）。

支持 DBC 里最常用的两行：
    BO_ 256 MSG_NAME: 8 ECU
    SG_ SIG_NAME : 0|8@1+ (1,0) [0|255] "km/h" ECU

解码：Intel（``@1``，低位在前）与 Motorola（``@0``，高位在前）两种字节序，
带 factor/offset 与符号位。够用于看实时数值，不做多路复用/值表。
"""

import json
import os
import re

MSG_RE = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s*(\w*)")
SIG_RE = re.compile(
    r"^SG_\s+(\w+)\s*(?:\w+)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
    r"\(([-\d.eE+]+),([-\d.eE+]+)\)\s*"
    r"\[([^|]*)\|([^\]]*)\]\s*\"([^\"]*)\"\s*(.*)$")


class DbcMessage(object):
    """一条报文及其信号定义。"""

    def __init__(self, can_id, name, dlc, sender):
        self.can_id = int(can_id)
        self.name = name
        self.dlc = int(dlc)
        self.sender = sender
        self.signals = []

    def to_dict(self):
        return {"id": self.can_id, "name": self.name, "dlc": self.dlc,
                "sender": self.sender,
                "signals": [sig.to_dict() for sig in self.signals]}


class DbcSignal(object):
    """一个信号的位布局与换算参数。"""

    def __init__(self, name, start, length, order, signed, factor, offset,
                 minimum, maximum, unit, receivers):
        self.name = name
        self.start = int(start)
        self.length = int(length)
        self.order = int(order)              # 1=Intel(小端) 0=Motorola(大端)
        self.signed = signed == "-"
        self.factor = float(factor)
        self.offset = float(offset)
        self.minimum = minimum
        self.maximum = maximum
        self.unit = unit
        self.receivers = receivers

    def to_dict(self):
        return {"name": self.name, "start": self.start, "len": self.length,
                "order": self.order, "signed": self.signed,
                "factor": self.factor, "offset": self.offset,
                "unit": self.unit}


def parse(text):
    """解析 DBC 文本，返回 ``[DbcMessage]``。"""
    messages = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        match = MSG_RE.match(line)
        if match:
            current = DbcMessage(match.group(1), match.group(2), match.group(3),
                                 match.group(4))
            messages.append(current)
            continue
        match = SIG_RE.match(line)
        if match and current is not None:
            current.signals.append(DbcSignal(
                name=match.group(1), start=match.group(2), length=match.group(3),
                order=match.group(4), signed=match.group(5),
                factor=match.group(6), offset=match.group(7),
                minimum=match.group(8), maximum=match.group(9),
                unit=match.group(10), receivers=match.group(11).strip()))
            continue
        if line.startswith("BO_") or line.startswith("SG_"):
            continue
        if line.startswith("BU_") or line.startswith("CM_") or line.startswith("BA_"):
            continue
    return messages


def _bits_from_payload(data):
    """十六进制字节串 -> 位列表（索引 0 是报文第一个字节的最低位）。"""
    bits = []
    for byte in data:
        for offset in range(8):
            bits.append((byte >> offset) & 1)
    return bits


def _extract_intel(bits, start, length):
    value = 0
    for index in range(length):
        bit = bits[start + index] if start + index < len(bits) else 0
        value |= bit << index
    return value


def _extract_motorola(bits, start, length):
    """Motorola：起始位按"字节序号*8 + 位序号(7..0)"的锯齿顺序向前取。"""
    value = 0
    byte_index = start // 8
    bit_index = start % 8
    for _ in range(length):
        pos = byte_index * 8 + bit_index
        bit = bits[pos] if pos < len(bits) else 0
        value = (value << 1) | bit
        if bit_index == 0:
            byte_index += 1
            bit_index = 7
        else:
            bit_index -= 1
    return value


def decode_signal(signal, payload_bytes):
    """按信号定义从载荷字节里解出物理值；数据不够返回 ``None``。"""
    if len(payload_bytes) * 8 < signal.start + signal.length and signal.order == 1:
        if len(payload_bytes) * 8 < signal.start + 1:
            return None
    bits = _bits_from_payload(payload_bytes)
    if signal.order == 1:
        raw = _extract_intel(bits, signal.start, signal.length)
    else:
        raw = _extract_motorola(bits, signal.start, signal.length)
    if signal.signed and signal.length > 0:
        sign_bit = 1 << (signal.length - 1)
        if raw & sign_bit:
            raw -= (1 << signal.length)
    return raw * signal.factor + signal.offset


def load_payload(text):
    """把 ``"AA BB CC"`` 或 ``"AABBCC"`` 转成字节列表。"""
    cleaned = (text or "").replace(" ", "").replace("-", "")
    if not cleaned:
        return []
    try:
        return [int(cleaned[i:i + 2], 16) for i in range(0, len(cleaned), 2)]
    except ValueError:
        return []


def save(model, path, name=""):
    """把解析结果存成 JSON（带名称，供下次启动直接加载）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"meta": {"name": name or os.path.basename(path)},
               "messages": [msg.to_dict() for msg in model]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def load_model(raw):
    """从 `[{id,name,dlc,signals}]` 结构恢复报文列表。"""
    messages = []
    for item in raw or []:
        msg = DbcMessage(item["id"], item["name"], item["dlc"], item.get("sender", ""))
        for sig in item.get("signals") or []:
            msg.signals.append(DbcSignal(
                sig["name"], sig["start"], sig["len"], sig["order"],
                "-" if sig.get("signed") else "+", sig.get("factor", 1),
                sig.get("offset", 0), "", "", sig.get("unit", ""), ""))
        messages.append(msg)
    return messages


def load(path):
    """从 JSON 恢复解析结果（兼容旧的无 meta 列表格式）。"""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):
        return load_model(raw.get("messages") or [])
    return load_model(raw)
