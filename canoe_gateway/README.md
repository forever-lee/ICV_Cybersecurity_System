# CANoe 车身域篡改攻击代理

本目录实现 Web 到真实 CAN 总线的实验台架闭环。请勿连接实车或道路运行网络。

## 一次性配置 CANoe

1. 创建并保存 `canoe_attack.cfg`。向导中可选择本目录的
   `BodyAttack.dbc`，让 `BodyCAN` 总线被明确识别为 CAN。
2. 将 CAN1 映射到连接 K3 CAN0 的 Vector 硬件，并把波特率设为与 K3 一致。
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

`run_cloud.py` 中的 `CANOE_CONFIG_PATH` 必须指向本机已经完成一次性硬件映射的
`.cfg` 文件。

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
