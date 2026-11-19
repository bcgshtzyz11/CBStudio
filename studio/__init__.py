# -*- coding: utf-8 -*-
"""can_bench_studio — 台架剧本工作台（图形化故障注入 / 抓取判定 / 现场保全）

包内模块：

    config.py      配置默认值与读写
    paths.py       目录布局、运行编号
    eventlog.py    结构化日志总线（环形缓冲 + JSONL + SSE 订阅）
    relay.py       LCUS-4 继电器驱动
    channels.py    通道抽象（继电器 / CAN 闸门 / 自定义命令）
    adb.py         DUT 侧取证（在线 / uptime / 日志 / 截图）
    canctl.py      CAN 子进程主管（32 位引擎）
    probes.py      串口/adb/CAN 抓取 + 监看规则
    scenario.py    剧本模型与内置剧本
    runner.py      剧本执行器
    incidents.py   现场保全
    studio_app.py  应用装配与动作分派
    server.py      HTTP/SSE 服务
"""

__version__ = "1.0.0"
