# S32K344 实车报文解析补丁

`main.patch` 只增加 CAN0 收到 `0x1D1` 或 `0x228`（DLC 8）时的独立 UDP 转发标记，不改变原有车身状态解析、事件控制或 CAN1 发送逻辑。

在 `Body_CAN_Monitor_S32K344` 工程根目录执行 `git apply main.patch`（把补丁复制到该目录后），或按补丁内容修改 `src/main.c`，然后 Clean、Build、烧录。若源码已包含 `BODY_INSPECT_1D1_CAN_ID`，无需再次应用。

Jetson 端需同步 `jetson_edge/body_protocol.py` 与 `jetson_edge/body_edge_agent.py`；云端需同步 `server.py`、`body_blf_monitor.py` 和 `static/app.js`。Web 上传 BLF 模式不依赖板端补丁。

0x211 单次 CANoe 台架篡改演示还需应用 `attack_211.patch`，让 CAN0 将该 ID 的两帧回传。若板端源码已经包含 `BODY_ATTACK_211_CAN_ID`，不要重复应用。修改后重新编译并烧录。该补丁不改变右侧 0x1D1/0x228 解析流程。

当 CANoe CAN1 改为 500 kbit/s 时，还需在板端工程应用 `can_500kbps.patch`。补丁同时修改 CAN0/CAN1 的普通 CAN 位时序配置及其 `.mex` 源设置；24 MHz 时钟、16 TQ、分频 3 对应 500 kbit/s。CAN FD 数据位时序和串口波特率不受影响。应用后重新生成配置（若需要）、Clean、Build 并烧录。
