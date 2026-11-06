# CANoe 车身域篡改攻击代理

本目录实现 Web 到真实 CAN 总线的实验台架闭环。请勿连接实车或道路运行网络。

## 一次性配置 CANoe

1. 直接打开本目录的 `canoe_attack.cfg`。该配置只引用同目录内的
   `BodyAttack.dbc`、`BodyAttack.can` 和 `BodyAttack.cbf`，移动整个项目目录后
   不需要修改文件路径。
2. 在新电脑上首次运行时，将 **CAN1** 映射到连接 K3 CAN0 的 Vector VN5640
   实际通道，并把波特率设置为 **250 kbit/s**。只启用 CAN1；CAN2、LIN、
   FlexRay、Ethernet 和 Ath 通道均不启用。
3. 在 Simulation Setup 中加入 `BodyAttack.can`。
4. 创建命名空间 `VShield` 及两个整数系统变量：
   - `AttackRequest`：默认值 `0`。
   - `AttackState`：默认值 `0`。
5. 确认实验期间只有 CANoe 发送 `0x351`/`0x352`，避免与其他节点重复发送。

CAPL 平时保持静默，不覆盖 K3 原有周期演示。收到攻击序号后持续 5 秒进入攻击窗口；每连续 10 个 `0x351` 中随机选择第 5 或第 6 帧，将 `Door_FL` 替换为开启，同时继续递增 Alive Counter。5 秒后由 CAPL 自行停止攻击发送，页面恢复 K3 周期状态。

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
   VN5640 通道；确认 CAN1 为 250 kbit/s。
4. 编译 ECU1/CAPL，必须显示 0 errors；启动 Measurement 后，Trace 中应先能
   看到 K3 发出的 `0x180 Rx`。
5. 关闭手动 Measurement，再运行 `run_cloud.py`。网页点击攻击按钮后会自动
   启动 CANoe 与代理。若失败，查看 `logs\canoe_attack_agent.log`。

如果没有 `0x180 Rx`，属于硬件通道、波特率、接线或 K3 未运行问题；如果有
`0x180 Rx` 但点击网页后没有 `0x351/0x352 Tx`，再检查 CAPL 编译结果、系统变量
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

手动方式仅用于查看代理输出或排障。正常演示由网页按钮自动启动。只有 K3/Jetson
回传真实篡改帧后，网页才会显示检测成功。后台日志位于
`logs/canoe_attack_agent.log`。
