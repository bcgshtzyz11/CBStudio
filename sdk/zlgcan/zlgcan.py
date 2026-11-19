# -*- coding: utf-8 -*-
"""zlgcan.py — ZLG USBCANFD 系列驱动的 ctypes 绑定（32 位）

这个文件补的是原分享包里**缺失**的一环：``can_engine.py`` 会 ``import zlgcan``
并直接使用 ``ZCAN`` / ``ZCAN_Transmit_Data`` 这一套官方 Demo 风格的接口，但分享包里
既没有 ``zlgcan`` 模块，也没有 SDK 目录，CAN 功能因此完全跑不起来。

本实现按 ZLG ``zlgcan.h`` 的结构体布局用 ``ctypes`` 重新描述了一遍，接口名与官方
Python Demo 保持一致，``can_engine.py`` 不用改一行就能用::

    from zlgcan import ZCAN, ZCAN_TYPE_CAN, ZCAN_CHANNEL_INIT_CONFIG, ZCAN_Transmit_Data

    z = ZCAN()
    device = z.OpenDevice(41, 0, 0)                 # USBCANFD-200U
    z.ZCAN_SetValue(device, "0/canfd_abit_baud_rate", b"500000")
    config = ZCAN_CHANNEL_INIT_CONFIG()
    config.can_type = ZCAN_TYPE_CANFD
    config.config.canfd.mode = 0
    channel = z.InitCAN(device, 0, config)
    z.StartCAN(channel)
    msgs, count = z.Receive(channel, 100, 0)

**只在 32 位 Python 上可用**：ZLG 只发 32 位 ``zlgcan.dll``。这里的句柄按
``c_uint32`` 声明，与 32 位指针同宽；64 位解释器里请改用适配层选定的 32 位解释器。

DLL 查找顺序：``ZCAN_DLL_PATH`` 环境变量 -> 本文件所在目录 -> 当前工作目录。
``kerneldlls/``（各设备驱动）跟着 ``zlgcan.dll`` 一起放在同目录即可。
"""

import ctypes
import os
import sys

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ZCAN_TYPE_CAN = 0x01
ZCAN_TYPE_CANFD = 0x02
ZCAN_TYPE_LIN = 0x03
ZCAN_TYPE_DA = 0x04

ZCAN_STATUS_ERR = 0
ZCAN_STATUS_OK = 1
ZCAN_STATUS_ONLINE = 2
ZCAN_STATUS_OFFLINE = 3
ZCAN_STATUS_UNSUPPORTED = 4

INVALID_DEVICE_HANDLE = 0
INVALID_CHANNEL_HANDLE = 0

# 设备类型号（只列常用，完整的见 ZCANPRO / zlgcan.h）
ZCAN_USBCANFD_200U = 41
ZCAN_USBCANFD_100U = 42
ZCAN_USBCANFD_MINI = 43
ZCAN_USBCANFD_800U = 59

TRANSMIT_NORMAL = 0          # 正常发送
TRANSMIT_SINGLE = 1          # 单次发送
TRANSMIT_SELF_SR = 2         # 自发自收
TRANSMIT_SINGLE_SELF_SR = 3  # 单次自发自收


class ZCANException(Exception):
    """SDK 调用失败时抛出。"""


# ---------------------------------------------------------------------------
# 结构体（与 zlgcan.h 一一对应）
# ---------------------------------------------------------------------------

class ZCAN_CAN_FRAME(ctypes.Structure):
    """经典 CAN 帧（8 字节载荷）。"""
    _fields_ = [
        ("can_id", ctypes.c_uint32),        # 报文 ID（bit31 = 扩展帧标志）
        ("can_dlc", ctypes.c_ubyte),        # 数据长度 0~8
        ("pad", ctypes.c_ubyte),            # 对齐填充
        ("res0", ctypes.c_ubyte),           # 保留
        ("res1", ctypes.c_ubyte),           # 保留
        ("data", ctypes.c_ubyte * 8),       # 数据
    ]


class ZCAN_CANFD_FRAME(ctypes.Structure):
    """CANFD 帧（最多 64 字节载荷）。"""
    _fields_ = [
        ("can_id", ctypes.c_uint32),
        ("len", ctypes.c_ubyte),            # 数据长度 0~64
        ("res0", ctypes.c_ubyte),
        ("res1", ctypes.c_ubyte),
        ("res2", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 64),
    ]


class ZCAN_Transmit_Data(ctypes.Structure):
    """待发送的经典 CAN 帧。"""
    _fields_ = [
        ("frame", ZCAN_CAN_FRAME),
        ("transmit_type", ctypes.c_uint32),
    ]


class ZCAN_Receive_Data(ctypes.Structure):
    """接收到的经典 CAN 帧（带时间戳）。"""
    _fields_ = [
        ("frame", ZCAN_CAN_FRAME),
        ("timestamp", ctypes.c_ulonglong),
    ]


class ZCAN_TransmitFD_Data(ctypes.Structure):
    """待发送的 CANFD 帧。"""
    _fields_ = [
        ("frame", ZCAN_CANFD_FRAME),
        ("transmit_type", ctypes.c_uint32),
    ]


class ZCAN_ReceiveFD_Data(ctypes.Structure):
    """接收到的 CANFD 帧（带时间戳）。"""
    _fields_ = [
        ("frame", ZCAN_CANFD_FRAME),
        ("timestamp", ctypes.c_ulonglong),
    ]


class ZCAN_CHANNEL_CAN_INIT_CONFIG(ctypes.Structure):
    """经典 CAN 通道初始化参数。"""
    _fields_ = [
        ("acc_code", ctypes.c_uint32),
        ("acc_mask", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("filter", ctypes.c_ubyte),
        ("timing0", ctypes.c_ubyte),
        ("timing1", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),           # 0 正常 / 1 只听
    ]


class ZCAN_CHANNEL_CANFD_INIT_CONFIG(ctypes.Structure):
    """CANFD 通道初始化参数。"""
    _fields_ = [
        ("acc_code", ctypes.c_uint32),
        ("acc_mask", ctypes.c_uint32),
        ("abit_timing", ctypes.c_uint32),
        ("dbit_timing", ctypes.c_uint32),
        ("brp", ctypes.c_uint32),
        ("filter", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),           # 0 正常 / 1 只听
        ("pad", ctypes.c_ushort),
        ("reserved", ctypes.c_uint32),
    ]


class ZCAN_CHANNEL_LIN_INIT_CONFIG(ctypes.Structure):
    """LIN 通道初始化参数（占位，保证联合体大小与头文件一致）。"""
    _fields_ = [
        ("acc_code", ctypes.c_uint32),
        ("acc_mask", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("filter", ctypes.c_ubyte),
        ("mode", ctypes.c_ubyte),
        ("pad", ctypes.c_ushort),
        ("timing0", ctypes.c_ubyte),
        ("timing1", ctypes.c_ubyte),
        ("pad2", ctypes.c_ushort),
        ("reserved2", ctypes.c_uint32),
    ]


class ZCAN_CHANNEL_CONFIG(ctypes.Union):
    """通道配置联合体。"""
    _fields_ = [
        ("can", ZCAN_CHANNEL_CAN_INIT_CONFIG),
        ("canfd", ZCAN_CHANNEL_CANFD_INIT_CONFIG),
        ("lin", ZCAN_CHANNEL_LIN_INIT_CONFIG),
    ]


class ZCAN_CHANNEL_INIT_CONFIG(ctypes.Structure):
    """``ZCAN_InitCAN`` 的参数：先给 ``can_type``，再按类型填 ``config.can`` / ``config.canfd``。"""
    _fields_ = [
        ("can_type", ctypes.c_uint32),
        ("config", ZCAN_CHANNEL_CONFIG),
    ]


class ZCAN_DEV_INFORMATION(ctypes.Structure):
    """``ZCAN_GetDeviceInf`` 填的设备信息（原样布局）。"""
    _fields_ = [
        ("hw_version", ctypes.c_ushort),
        ("fw_version", ctypes.c_ushort),
        ("dr_version", ctypes.c_ushort),
        ("in_version", ctypes.c_ushort),
        ("irq_num", ctypes.c_ushort),
        ("can_num", ctypes.c_ubyte),
        ("serial", ctypes.c_char * 20),
        ("hw_type", ctypes.c_char * 40),
        ("reserved", ctypes.c_ushort * 4),
    ]


class DeviceInfo(object):
    """设备信息的人读视图（已把版本号拆成 ``V1.02`` 这种形式）。"""

    __slots__ = ("hw_version", "fw_version", "dr_version", "in_version",
                 "can_num", "serial", "hw_type")

    def __init__(self, raw=None):
        self.hw_version = "V0.00"
        self.fw_version = "V0.00"
        self.dr_version = "V0.00"
        self.in_version = "V0.00"
        self.can_num = 0
        self.serial = ""
        self.hw_type = ""
        if raw is None:
            return
        self.hw_version = _fmt_version(raw.hw_version)
        self.fw_version = _fmt_version(raw.fw_version)
        self.dr_version = _fmt_version(raw.dr_version)
        self.in_version = _fmt_version(raw.in_version)
        self.can_num = int(raw.can_num)
        self.serial = _decode(raw.serial)
        self.hw_type = _decode(raw.hw_type)

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self):
        return ("DeviceInfo(type=%r, serial=%r, hw=%s, fw=%s, can_num=%d)"
                % (self.hw_type, self.serial, self.hw_version,
                   self.fw_version, self.can_num))


def _fmt_version(value):
    """``0x0102`` -> ``V1.02``（ZLG 版本号是按十进制字节拆的）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "V0.00"
    return "V%d.%02d" % ((number >> 8) & 0xFF, number & 0xFF)


def _decode(raw):
    """``c_char`` 数组 -> 去掉尾零的可读文本。"""
    try:
        data = bytes(bytearray(raw))
    except (TypeError, ValueError):
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.split(b"\x00", 1)[0].decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return data.split(b"\x00", 1)[0].decode("latin-1").strip()


# ---------------------------------------------------------------------------
# DLL 定位与加载
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DLL_NAME = "zlgcan.dll"
DLL_PATH = ""
DLL_ERROR = ""


def find_dll(explicit=None):
    """按 ``显式路径 -> 环境变量 -> 本目录 -> 当前目录`` 找 ``zlgcan.dll``。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("ZCAN_DLL_PATH")
    if env:
        candidates.append(env)
    candidates.append(os.path.join(_HERE, _DLL_NAME))
    candidates.append(os.path.join(os.getcwd(), _DLL_NAME))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ""


def _preload(explicit=None):
    """加载 DLL；顺带把同目录和 ``kerneldlls`` 加进依赖搜索路径。"""
    global DLL_PATH, DLL_ERROR
    path = find_dll(explicit)
    if not path:
        DLL_ERROR = ("找不到 %s（应在 SDK 目录里：%s）" % (_DLL_NAME, _HERE))
        return None
    directory = os.path.dirname(path)
    # Python 3.8+ 默认不再用 PATH 找依赖，必须显式登记目录
    add_dir = getattr(os, "add_dll_directory", None)
    if add_dir is not None:
        for folder in (directory, os.path.join(directory, "kerneldlls")):
            if os.path.isdir(folder):
                try:
                    add_dir(folder)
                except OSError:
                    pass
    try:
        # 绝对路径加载，避免命中别的目录里的同名 DLL
        dll = ctypes.WinDLL(path)
    except OSError as exc:
        DLL_ERROR = "加载 %s 失败: %s" % (path, exc)
        return None
    DLL_PATH = path
    DLL_ERROR = ""
    return dll


# ---------------------------------------------------------------------------
# ZCAN 门面
# ---------------------------------------------------------------------------

class ZCAN(object):
    """ZLG 设备/通道操作的薄封装（接口名对齐官方 Python Demo）。"""

    def __init__(self, dll_path=None):
        self._dll = _preload(dll_path)
        if self._dll is None:
            raise ZCANException(DLL_ERROR or "zlgcan.dll 不可用")
        self._bind()

    # -- 绑定 -------------------------------------------------------------
    def _bind(self):
        """声明各导出函数的签名（句柄按 32 位整数）。"""
        dll = self._dll
        handle = ctypes.c_uint32
        status = ctypes.c_uint32

        dll.ZCAN_OpenDevice.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_uint32]
        dll.ZCAN_OpenDevice.restype = handle
        dll.ZCAN_CloseDevice.argtypes = [handle]
        dll.ZCAN_CloseDevice.restype = status

        dll.ZCAN_GetDeviceInf.argtypes = [handle,
                                          ctypes.POINTER(ZCAN_DEV_INFORMATION)]
        dll.ZCAN_GetDeviceInf.restype = status

        dll.ZCAN_InitCAN.argtypes = [handle, ctypes.c_uint32,
                                     ctypes.POINTER(ZCAN_CHANNEL_INIT_CONFIG)]
        dll.ZCAN_InitCAN.restype = handle
        dll.ZCAN_StartCAN.argtypes = [handle]
        dll.ZCAN_StartCAN.restype = status
        dll.ZCAN_ResetCAN.argtypes = [handle]
        dll.ZCAN_ResetCAN.restype = status
        dll.ZCAN_ClearBuffer.argtypes = [handle]
        dll.ZCAN_ClearBuffer.restype = status

        dll.ZCAN_Transmit.argtypes = [handle,
                                      ctypes.POINTER(ZCAN_Transmit_Data),
                                      ctypes.c_uint32]
        dll.ZCAN_Transmit.restype = ctypes.c_uint32
        dll.ZCAN_TransmitFD.argtypes = [handle,
                                        ctypes.POINTER(ZCAN_TransmitFD_Data),
                                        ctypes.c_uint32]
        dll.ZCAN_TransmitFD.restype = ctypes.c_uint32

        dll.ZCAN_Receive.argtypes = [handle,
                                     ctypes.POINTER(ZCAN_Receive_Data),
                                     ctypes.c_uint32, ctypes.c_uint32]
        dll.ZCAN_Receive.restype = ctypes.c_uint32
        dll.ZCAN_ReceiveFD.argtypes = [handle,
                                       ctypes.POINTER(ZCAN_ReceiveFD_Data),
                                       ctypes.c_uint32, ctypes.c_uint32]
        dll.ZCAN_ReceiveFD.restype = ctypes.c_uint32

        dll.ZCAN_GetReceiveNum.argtypes = [handle, ctypes.c_uint32]
        dll.ZCAN_GetReceiveNum.restype = ctypes.c_uint32

        dll.ZCAN_SetValue.argtypes = [handle, ctypes.c_char_p, ctypes.c_char_p]
        dll.ZCAN_SetValue.restype = status
        dll.ZCAN_GetValue.argtypes = [handle, ctypes.c_char_p,
                                      ctypes.c_char_p, ctypes.c_uint32]
        dll.ZCAN_GetValue.restype = status

    # -- 设备 -------------------------------------------------------------
    def OpenDevice(self, device_type, device_index=0, reserved=0):
        """打开设备，返回设备句柄（``0`` = 失败）。"""
        handle = self._dll.ZCAN_OpenDevice(int(device_type), int(device_index),
                                           int(reserved))
        return int(handle or 0)

    def CloseDevice(self, device_handle):
        """关闭设备。"""
        return int(self._dll.ZCAN_CloseDevice(int(device_handle)))

    def GetDeviceInf(self, device_handle):
        """读设备信息，返回 :class:`DeviceInfo`。"""
        raw = ZCAN_DEV_INFORMATION()
        status = self._dll.ZCAN_GetDeviceInf(int(device_handle), ctypes.byref(raw))
        if not status:
            raise ZCANException("ZCAN_GetDeviceInf 失败 (handle=%s)" % device_handle)
        return DeviceInfo(raw)

    # -- 通道 -------------------------------------------------------------
    def InitCAN(self, device_handle, can_index, config):
        """初始化一路通道，返回通道句柄（``0`` = 失败）。"""
        handle = self._dll.ZCAN_InitCAN(int(device_handle), int(can_index),
                                        ctypes.byref(config))
        return int(handle or 0)

    def StartCAN(self, can_handle):
        """启动通道。"""
        return int(self._dll.ZCAN_StartCAN(int(can_handle)))

    def ResetCAN(self, can_handle):
        """复位通道（停止收发）。"""
        return int(self._dll.ZCAN_ResetCAN(int(can_handle)))

    def ClearBuffer(self, can_handle):
        """清空接收缓冲。"""
        return int(self._dll.ZCAN_ClearBuffer(int(can_handle)))

    # -- 收发 -------------------------------------------------------------
    def Transmit(self, can_handle, messages, count):
        """发送 ``count`` 帧；``messages`` 可以是单条结构体或结构体数组。"""
        return int(self._dll.ZCAN_Transmit(int(can_handle), messages, int(count)))

    def TransmitFD(self, can_handle, messages, count):
        """发送 CANFD 帧。"""
        return int(self._dll.ZCAN_TransmitFD(int(can_handle), messages, int(count)))

    def Receive(self, can_handle, count, reserved=0):
        """收帧，返回 ``(结构体数组, 实际帧数)``。"""
        count = max(1, int(count))
        buffer = (ZCAN_Receive_Data * count)()
        got = self._dll.ZCAN_Receive(int(can_handle), buffer, count, int(reserved))
        return buffer, int(got)

    def ReceiveFD(self, can_handle, count, reserved=0):
        """收 CANFD 帧，返回 ``(结构体数组, 实际帧数)``。"""
        count = max(1, int(count))
        buffer = (ZCAN_ReceiveFD_Data * count)()
        got = self._dll.ZCAN_ReceiveFD(int(can_handle), buffer, count,
                                       int(reserved))
        return buffer, int(got)

    def GetReceiveNum(self, can_handle, can_type=ZCAN_TYPE_CAN):
        """查询接收缓冲里待取帧数。"""
        return int(self._dll.ZCAN_GetReceiveNum(int(can_handle), int(can_type)))

    # -- 设备属性 ---------------------------------------------------------
    def ZCAN_SetValue(self, device_handle, path, value):
        """写设备/通道属性，如 ``"0/canfd_abit_baud_rate"`` = ``b"500000"``。"""
        if isinstance(path, str):
            path = path.encode("utf-8")
        if isinstance(value, str):
            value = value.encode("utf-8")
        return int(self._dll.ZCAN_SetValue(int(device_handle), path, value))

    def ZCAN_GetValue(self, device_handle, path, size=64):
        """读设备/通道属性，返回文本（失败返回 ``""``）。"""
        if isinstance(path, str):
            path = path.encode("utf-8")
        buffer = ctypes.create_string_buffer(int(size))
        status = self._dll.ZCAN_GetValue(int(device_handle), path, buffer, int(size))
        if not status:
            return ""
        return buffer.value.decode("utf-8", "replace")

    # 官方 Demo 里的别名，can_engine 走的是 ZCAN_SetValue 全名
    SetValue = ZCAN_SetValue
    GetValue = ZCAN_GetValue


def dll_status():
    """自检用：返回 ``(是否可加载, DLL 路径或错误原因)``。"""
    if DLL_PATH:
        return True, DLL_PATH
    dll = _preload()
    if dll is None:
        return False, DLL_ERROR
    return True, DLL_PATH


if __name__ == "__main__":
    """直接运行：报告 DLL 与设备情况。"""
    ok, info = dll_status()
    print("dll_ok = %s" % ok)
    print("dll    = %s" % info)
    if not ok:
        sys.exit(1)
    zcan = ZCAN()
    dtype = int(sys.argv[1]) if len(sys.argv) > 1 else ZCAN_USBCANFD_200U
    device = zcan.OpenDevice(dtype, 0, 0)
    print("device = %s  (type=%d)" % (device, dtype))
    if not device:
        print("打开失败：未插设备 / 被 ZCANPRO 占用 / 型号不符")
        sys.exit(2)
    try:
        print("info   = %r" % (zcan.GetDeviceInf(device),))
    finally:
        zcan.CloseDevice(device)
