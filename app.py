# -*- coding: utf-8 -*-
"""app.py — 工作台入口：起本地服务、开浏览器、按 Ctrl+C 退出

用法::

    py -3.14 app.py                 # 默认 127.0.0.1:8777，自动开浏览器
    py -3.14 app.py --port 9000     # 换端口（被占用会自动顺延）
    py -3.14 app.py --no-browser    # 不开浏览器（远程/脚本用）
    py -3.14 app.py --dry-run       # 继电器干跑：不碰硬件，只记录动作
    py -3.14 app.py --no-probes     # 不自动开串口/adb 探针
    py -3.14 app.py --connect-can   # 启动时就连接 CAN 设备
"""

import argparse
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from studio import config as config_mod          # noqa: E402
from studio import paths                         # noqa: E402
from studio.server import build_server           # noqa: E402
from studio.studio_app import STUDIO_NAME, VERSION, StudioApp   # noqa: E402


def build_parser():
    """命令行参数。"""
    parser = argparse.ArgumentParser(
        description="台架剧本工作台（本地 Web GUI）：故障注入 / 抓取判定 / 现场保全",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--host", default=None, help="监听地址（默认取配置 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取配置 8777）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-probes", action="store_true", help="不自动启动串口/adb 探针")
    parser.add_argument("--connect-can", action="store_true", help="启动时连接 CAN 设备")
    parser.add_argument("--dry-run", action="store_true", help="继电器干跑（不碰硬件）")
    parser.add_argument("--sim", action="store_true",
                        help="全模拟模式：不碰继电器/CAN/串口/adb，用模拟数据跑通流程")
    return parser


def main(argv=None):
    """入口。"""
    args = build_parser().parse_args(argv)
    cfg = config_mod.load()
    if args.dry_run:
        cfg.setdefault("relay", {})["dry_run"] = True
    if args.sim:
        cfg["simulate"] = True
    server_cfg = cfg.get("server") or {}
    host = args.host or server_cfg.get("host") or "127.0.0.1"
    port = args.port or int(server_cfg.get("port") or 8777)

    app = StudioApp(cfg, host=host, port=port, simulate=bool(cfg.get("simulate")))
    server = build_server(app, host=host, port=port)
    app.host, app.port = server.host, server.port
    app.bootstrap(start_probes=not args.no_probes, connect_can=args.connect_can)

    print("=" * 66)
    print("  %s  v%s%s" % (STUDIO_NAME, VERSION, "   [模拟模式]" if app.simulate else ""))
    print("  地址: %s" % server.url)
    print("  目录: %s" % paths.STUDIO_DIR)
    print("  继电器: %s%s" % (app.relay.port or "(未连接)",
                              "（干跑）" if app.relay.dry_run else ""))
    print("  串口: %s @%d    CAN: %s" % (app.probes.serial.port, app.probes.serial.baud,
                                         "已连接" if app.can.state == "running" else "未连接"))
    print("  Ctrl+C 退出")
    print("=" * 66)

    if not args.no_browser:
        def _open():
            time.sleep(0.6)
            try:
                webbrowser.open(server.url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        app.shutdown()
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
