# S32K344 实车报文解析补丁

`main.patch` 只增加 CAN0 收到 `0x1D1` 或 `0x228`（DLC 8）时的独立 UDP 转发标记，不改变原有车身状态解析、事件控制或 CAN1 发送逻辑。

在 `Body_CAN_Monitor_S32K344` 工程根目录执行 `git apply main.patch`（把补丁复制到该目录后），或按补丁内容修改 `src/main.c`，然后 Clean、Build、烧录。若源码已包含 `BODY_INSPECT_1D1_CAN_ID`，无需再次应用。

Jetson 端需同步 `jetson_edge/body_protocol.py` 与 `jetson_edge/body_edge_agent.py`；云端需同步 `server.py`、`body_blf_monitor.py` 和 `static/app.js`。Web 上传 BLF 模式不依赖板端补丁。
