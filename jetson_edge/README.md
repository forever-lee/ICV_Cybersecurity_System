# Jetson 边缘端代码

该目录是从 Jetson Orin NX Super 实际运行环境整理出的最小可运行包，只包含：

- 两路 H.265 摄像头采流与云端上传；
- Jetson UART GNSS 定位与车速采集；
- WiFi 充电遥测转发；
- S32K344 蓝牙域 UDP 遥测转发；
- S32K344 车身域 UDP 状态、控制与时间同步；
- fMP4/WebSocket 协议；
- 一个统一的进程管理入口。

## 文件

- `start_edge.py`：唯一推荐入口，同时监管车身域、蓝牙域、WiFi、GPS、VHC-001 和 VHC-002。
- `body_edge_agent.py`：接收 S32K344 UDP/串口数据、对时、控制和云端上传。
- `body_protocol.py`：车身域二进制协议编解码。
- `WiFi_Module.py`：WiFi 充电遥测接收和云端转发。
- `bluetooth_edge_agent.py`：监听 S32K344 的 UDP 5000 报文，校验、去重并可靠上传云端。
- `bluetooth_control_protocol.py`：蓝牙网页启停帧与 ACK 的 CRC 协议。
- `bluetooth_time_sync.py`：蓝牙四时间戳对时协议和短时单调时钟映射。
- `run_vehicle_jetson.py`：单路摄像头环境检查与参数组装。
- `h264_vehicle_agent.py`：RTSP/fMP4 采流上传实现（同时支持 H.264/H.265）。
- `stream_protocol.py`：边缘端与云端二进制协议。
- `navigation_gps.py`：读取 `/dev/ttyTHS1` NMEA 并更新定位 JSON。
- `requirements_jetson.txt`：Python 依赖。

`logs/`、`*.pid`、`navigation_live.json` 均为运行时自动生成，不属于源码。

## 默认设备配置

| 设备 | 固定地址 | 端口/用途 |
|---|---|---|
| Jetson `eno1` | `192.168.4.100/24` | 板端和摄像头有线网络 |
| 车身板 | `192.168.4.31` | UDP 7100；控制/对时 UDP 7101 |
| WiFi 板 | `192.168.4.201` | 向 Jetson UDP 6001 转发报文 |
| 蓝牙板 | `192.168.4.20` | 向 Jetson UDP 5000 转发报文 |
| 摄像头 1 / VHC-001 | `192.168.4.88` | `rtsp://192.168.4.88:8554/main` |
| 摄像头 2 / VHC-002 | `192.168.4.89` | `rtsp://192.168.4.89:8554/main` |

GPS 默认使用 `/dev/ttyTHS1`，波特率 `38400`。两路摄像头共享同一块 Jetson 的 GPS 数据。

Jetson 的有线网口 `eno1` 固定为 `192.168.4.100/24`。`start_edge.py` 启动时会先 Ping 上表中的两路摄像头、车身板、WiFi 板和蓝牙板，并逐项打印 `[PING OK]` 或 `[PING FAIL]`。设备暂时不可达不会阻止其他组件启动。可设置 `BODY_NETWORK_INTERFACE=eno1` 明确限定车身域网口。
车身域状态报文使用 UDP 7100，控制和对时使用 UDP 7101。
网页车身联调命令也经 UDP 7101 下发；K3 在 CAN0 回读后，通过 UDP 7100
返回带命令序号的确认状态。更新该功能时，需将新版 `body_protocol.py` 和
`body_edge_agent.py` 一并复制到 Jetson，并重新启动 `start_edge.py`。

## 安装和运行

```bash
cd ~/Code/jetson_edge
python3 -m pip install --user -r requirements_jetson.txt
python3 start_edge.py
```

Jetson 只需手动运行上述 `start_edge.py`。它会自动以子进程启动并监管 `WiFi_Module.py`、`bluetooth_edge_agent.py`、`body_edge_agent.py`、`navigation_gps.py` 和两路摄像头上传进程；不要再分别启动这些脚本。

后台运行：

```bash
nohup python3 start_edge.py > edge_supervisor.log 2>&1 &
```

日志保存在 `logs/`。正常停止统一进程时，车身、蓝牙、WiFi、GPS、两个摄像头上传进程和 FFmpeg 子进程会一起退出。

## 源码默认配置与修改位置

正常启动不需要设置任何环境变量，直接运行 `python3 start_edge.py`。云端地址和上传 Token 统一在 `start_edge.py` 顶部配置，当前源码默认值为：

```text
CLOUD_HTTP_URL=http://3f7b7aec.r7.nas.cpolar.cn
CLOUD_WS_URL=ws://3f7b7aec.r7.nas.cpolar.cn
VEHICLE_INGEST_TOKEN=vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0
```

车身、蓝牙、WiFi、GPS 和摄像头子进程都会继承这些源码值。以后 cpolar 域名或 Token 变化时，直接修改 `start_edge.py` 顶部的 `DEFAULT_CLOUD_HTTP_URL`、`DEFAULT_CLOUD_WS_URL` 和 `DEFAULT_VEHICLE_INGEST_TOKEN`；不要在每次启动前临时设置。

板端与摄像头地址的源码默认值位于 `start_edge.py` 的 `device_ping_targets()` 和 `build_components()`：

- `CAMERA_1_RTSP_URL` 默认指向 `rtsp://192.168.4.88:8554/main`；
- `CAMERA_2_RTSP_URL` 默认指向 `rtsp://192.168.4.89:8554/main`；
- `BODY_BOARD_HOST` 默认为 `192.168.4.31`；
- 启动/停止发送采用 K3 真实确认闭环：默认每 `2` 秒重发一次、最多 `3` 次、`10` 秒超时。可用 `BODY_CONTROL_RETRY_INTERVAL`、`BODY_CONTROL_MAX_ATTEMPTS`、`BODY_CONTROL_TIMEOUT` 覆盖；
- `WIFI_DEVICE_HOST` 默认为 `192.168.4.201`；
- `BLUETOOTH_DEVICE_HOST` 默认为 `192.168.4.20`；
- GPS 默认设备和波特率分别为 `/dev/ttyTHS1`、`38400`。

WiFi 子进程默认按以下链路监听数据：

```text
S32K344 192.168.4.201
  → 网线 / UDP
  → 交换机
  → Jetson eno1 192.168.4.100:6001
```

`logs/wifi.log` 会输出 `[WiFi LINK]`、`[WiFi RX WAIT]`、`[WiFi RX OK]`
和 `[WiFi RX WARN]`，用于判断 UDP 6001 是否收到报文以及来源 IP 是否为
`192.168.4.201`。默认每 5 秒输出一次无报文提示，可通过
`WIFI_DIAGNOSTIC_INTERVAL_SECONDS` 调整；网卡和 Jetson 地址可通过
`WIFI_NETWORK_INTERFACE`、`WIFI_JETSON_ADDRESS` 覆盖。查看实时日志：

```bash
tail -f logs/wifi.log
```

车身域和蓝牙域也会输出同类链路诊断。`start_edge.py` 启动时直接在终端打印
`[BODY LINK]`、`[BODY CTRL]`、`[BT LINK]`、`[BT CTRL]` 以及对应日志查看命令；
子进程在 `logs/body.log`、`logs/bluetooth.log` 中周期输出：

- `[BODY RX WAIT]`、`[BT RX WAIT]`：指定时间内没有收到 UDP 报文；
- `[BODY RX OK]`、`[BT RX OK]`：已收到报文，并显示来源、长度、时间和累计序号；
- `[BODY RX WARN]`、`[BT RX WARN]`：报文来源不是配置的 K3 地址。

默认每 5 秒输出一次等待提示，可分别通过 `BODY_DIAGNOSTIC_INTERVAL_SECONDS`、
`BLUETOOTH_DIAGNOSTIC_INTERVAL_SECONDS` 调整。默认链路为：

```text
车身：192.168.4.31 -> Jetson eno1 192.168.4.100:7100
控制：Jetson eno1 192.168.4.100 -> 192.168.4.31:7101

蓝牙：192.168.4.20 -> Jetson eno1 192.168.4.100:5000
控制/对时：Jetson eno1 192.168.4.100 -> 192.168.4.20:5001
```

蓝牙域默认监听 `0.0.0.0:5000`，车辆默认为 `VHC-001`。可通过
`BLUETOOTH_UDP_HOST`、`BLUETOOTH_UDP_PORT` 和 `BLUETOOTH_VEHICLE_ID`
覆盖。日志位于 `logs/bluetooth.log`。

蓝牙网页启停由云端保存命令；Jetson 蓝牙代理轮询后通过 UDP 5001 下发至
`192.168.4.20`，并在 UDP 5000 等待发送笔记本沿 BLE Write、K344 回传的
`BTACK`。只有真实设备 ACK 经 Jetson 上传云端，网页才显示执行成功。
`sendto()` 成功不等于执行成功。其他域仍由 `start_edge.py` 正常监管。

蓝牙对时也使用 UDP 5001 下行、UDP 5000 上行，但独立于网页启停命令；
即使业务发送处于 OFF，也每 5 秒下发 `BTSYNC`。`logs/bluetooth.log` 出现
`[蓝牙对时成功]` 后，带匹配 `SYNC_ID` 的业务报文才能换算成发送电脑的发送
时间。RTT 最大 500 ms，映射最多保留 30 秒；不满足条件时云端不显示延迟。
当前对时使用不超过 40 字节的 CRC 校验紧凑帧，降低 BLE 分包导致的往返耗时；
旧版帧仍能解析。更换此版本时，发送电脑的 `ble_s32_sender.py` 也要同步更新并
重启；已支持 `BTSYNC|` 透传的 K344 固件无需仅为紧凑帧重新烧录。
Jetson 蓝牙代理会独立使用云端 `/healthz` 测量两机时钟偏差，避免系统时钟
有小幅偏差时把“至 Jetson”误认为“端到端”；校时失败只影响端到端显示，
不阻断报文转发。两台主机仍应保持各自的 NTP 同步。可选环境变量：
`BLUETOOTH_TIME_SYNC_INTERVAL_SECONDS`、`BLUETOOTH_TIME_SYNC_MAX_AGE_SECONDS`、
`BLUETOOTH_TIME_SYNC_MAX_RTT_MS`、`BLUETOOTH_TIME_SYNC_REQUEST_TIMEOUT_SECONDS`、
`BLUETOOTH_TIME_SYNC_MAX_OFFSET_JUMP_MS`。复制代码到 Ubuntu 时，务必同时更新
`bluetooth_edge_agent.py`、`bluetooth_time_sync.py` 和 `start_edge.py`，再重启
统一边缘进程；S32K344 也须烧录支持 `BTSYNC` 转发的新固件。

设置 `BODY_ENABLED=0`、`BLUETOOTH_ENABLED=0`、`WIFI_ENABLED=0`、`GPS_ENABLED=0`、
`CAMERA_1_ENABLED=0` 或 `CAMERA_2_ENABLED=0` 可单独关闭组件。

## 已排除的旧文件

- `gps_monitor.py`：只打印串口状态，不生成云端使用的导航数据，已由 `navigation_gps.py` 完整替代。
- 旧日志、PID、临时 JSON：运行产物，不进入源码包。
- 电脑测试启动器和云端服务代码：不属于 Jetson 边缘端。
