# CANoe 车身域篡改攻击代理

## 0x211 单次台架篡改

当前左侧攻击模块每次点击只让 CANoe 输出两帧：先输出 BLF 中一帧原始 0x211
作为基线，再在约 20 ms 后输出一帧随机改变 Byte 1～3 中有明确信号定义的某一位。
网页根据实际变化的位，把对应的中文功能名称和改变的报文字节一起标红。
两帧都必须由 S32K344 CAN0 接收，经 Jetson 上传云端，网页才会标红真实变化的字节。
这是隔离台架演示；该示例未重算 0x211 的校验字节，不用于实车控制或 ECU 接受性判断。
`BodyAttack.can` 更新后须在 CANoe 中重新编译 CAPL 并保存配置；旧的 `BodyAttack.cbf`
不会自动包含新逻辑。板端还需应用 `firmware/body_can_inspector/attack_211.patch`
并重新烧录，Jetson 需更新 `body_edge_agent.py`，云端需更新 `server.py` 与 `static/app.js`。

## 实车报文解析面板

车身域页面的“实车报文解析”与攻击面板并排展示。上传 BLF 可统计
`0x1D1`、`0x228` 的报文数量、实测周期、DLC、CAN 类型、方向、最新数据，
并按 VCU-ESP 通信矩阵显示各信号的原始值、物理值和状态含义。
通信矩阵未明确字节序，目前按 Intel 小端、LSB0 位序解析，接实车后需对照 CANoe 数据库验证。
实时模式从 S32K344 CAN0 接收的两种报文获取数据，经 Jetson 独立上传到
`/body/edge-monitor`；不会读 CANoe Trace，也不会写入原有车身状态或攻击记录。
台架测试时 CANoe 代替域控发送两种报文；上车后由域控发送，Web/Jetson
链路无需切换。板端须烧录包含专用报文转发标记的新版固件，Jetson 须更新
`body_protocol.py`、`body_edge_agent.py`，云端须更新 `server.py` 和
`body_blf_monitor.py`。BLF 上传功能仍需 `python-can`。

本目录实现 Web 到真实 CAN 总线的实验台架闭环。请勿连接实车或道路运行网络。

## 一次性配置 CANoe

1. 直接打开本目录的 `canoe_attack.cfg`。该配置只引用同目录内的
   `BodyAttack.dbc`、`BodyAttack.can` 和 `BodyAttack.cbf`，移动整个项目目录后
   不需要修改文件路径。
2. 在新电脑上首次运行时，将 **CAN1** 映射到连接 K3 CAN0 的 Vector VN5640
   实际通道，并把波特率设置为 **500 kbit/s**。只启用 CAN1；CAN2、LIN、
   FlexRay、Ethernet 和 Ath 通道均不启用。
3. 在 Simulation Setup 中加入 `BodyAttack.can`。
4. 创建命名空间 `VShield` 及两个整数系统变量：
   - `AttackRequest`：默认值 `0`。
   - `AttackState`：默认值 `0`。
5. 确认实验台架由 CANoe 单独发送 `0x211`，避免其他节点同时发送同 ID。

CAPL 平时保持静默。收到攻击序号后只发送一帧基线和一帧随机单字节篡改帧，随后结束本次请求。

## 网页按需自动启动（推荐）

使用 `run_cloud.py` 启动后端后，点击网页“CANoe发起篡改攻击”即可。后端会在
代理离线时自动后台启动 `canoe_attack_agent.py`；代理再打开 CANoe 配置、启动
Measurement 并执行本次攻击。无需单独打开代理终端，也无需提前手动启动 CANoe。

默认情况下，`run_cloud.py` 自动使用本目录的 `canoe_attack.cfg`，不再依赖固定
盘符或旧电脑路径。如需使用其他配置，才设置环境变量 `CANOE_CONFIG_PATH`。

## 换电脑后的首次检查

1. 安装 CANoe、Vector 驱动和 Python 依赖：
   `python -m pip install -r canoe_gateway\requirements.txt`。
2. USB 连接 VN5640，CAN-H、CAN-L、GND 接入 K3 的 CAN0 总线。
3. 打开 `canoe_attack.cfg`，在 Channel Mapping 中仅启用 CAN1，并映射到实际
   VN5640 通道；确认 CAN1 为 500 kbit/s。
4. 编译 ECU1/CAPL，必须显示 0 errors；启动 Measurement 后，Trace 中应先能
   看到 K3 发出的 `0x180 Rx`。
5. 关闭手动 Measurement，再运行 `run_cloud.py`。网页点击攻击按钮后会自动
   启动 CANoe 与代理。若失败，查看 `logs\canoe_attack_agent.log`。

如果没有 `0x180 Rx`，属于硬件通道、波特率、接线或 K3 未运行问题；如果有
`0x180 Rx` 但点击网页后没有两帧 `0x211 Tx`，再检查 CAPL 编译结果、系统变量
`VShield::AttackRequest/AttackState` 和代理日志。

## 手动启动代理（仅排障）

在安装 CANoe 的 Windows 电脑中执行：

```powershell
python -m pip install -r canoe_gateway\requirements.txt
$env:CLOUD_HTTP_URL="https://你的云端地址"
$env:VEHICLE_ID="VHC-001"
$env:VEHICLE_INGEST_TOKEN="与云端一致的Token"
$env:CANOE_CONFIG_PATH="C:\完整路径\canoe_attack.cfg"
python canoe_gateway\canoe_attack_agent.py
```

手动方式仅用于查看代理输出或排障。CANoe 与 `run_cloud.py` 分别运行在两台电脑时，
代理必须在接有 VN5640 的 CANoe 电脑上启动。只有板端/Jetson 回传两帧 0x211 后，
网页才会显示对比结果。后台日志位于
`logs/canoe_attack_agent.log`。
