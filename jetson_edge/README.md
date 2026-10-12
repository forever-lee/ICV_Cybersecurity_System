# Jetson 边缘端代码

该目录是从 Jetson Orin NX Super 实际运行环境整理出的最小可运行包，只包含：

- 两路 H.265 摄像头采流与云端上传；
- Jetson UART GNSS 定位与车速采集；
- fMP4/WebSocket 协议；
- 蓝牙 UDP 接收、可靠排队与云端上传；
- 一个统一的进程管理入口。

## 文件

- `start_edge.py`：唯一推荐入口，分别监管蓝牙、GPS、VHC-001 和 VHC-002；任一模块退出只重启该模块。
- `bluetooth_edge_agent.py`：接收 S32K344 经以太网发送的 UDP 报文并上传云端，失败时自动重试。
- `run_vehicle_jetson.py`：单路摄像头环境检查与参数组装。
- `h264_vehicle_agent.py`：RTSP/fMP4 采流上传实现（同时支持 H.264/H.265）。
- `stream_protocol.py`：边缘端与云端二进制协议。
- `navigation_gps.py`：读取 `/dev/ttyTHS1` NMEA 并更新定位 JSON。
- `requirements_jetson.txt`：Python 依赖。

`logs/`、`*.pid`、`navigation_live.json` 均为运行时自动生成，不属于源码。

## 默认设备配置

| 车辆 | 摄像头地址 |
|---|---|
| VHC-001 | `rtsp://192.168.4.88:8554/main` |
| VHC-002 | `rtsp://192.168.4.89:8554/main` |

GPS 默认使用 `/dev/ttyTHS1`，波特率 `38400`。两路摄像头共享同一块 Jetson 的 GPS 数据。

## 安装和运行

```bash
cd ~/Code/jetson_edge
python3 -m pip install --user -r requirements_jetson.txt
python3 start_edge.py
```

后台运行：

```bash
nohup python3 start_edge.py > edge_supervisor.log 2>&1 &
```

日志保存在 `logs/`。正常停止统一进程时，GPS、两个上传进程和 FFmpeg 子进程会一起退出。

## 可选环境变量

```bash
export CLOUD_WS_URL='ws://your-cloud-host'
export VEHICLE_INGEST_TOKEN='your-ingest-token'
export CAMERA_1_RTSP_URL='rtsp://192.168.4.88:8554/main'
export CAMERA_2_RTSP_URL='rtsp://192.168.4.89:8554/main'
export GPS_DEVICE='/dev/ttyTHS1'
export GPS_BAUD='38400'
export BLUETOOTH_ENABLED='1'
export CLOUD_HTTP_URL='http://192.168.2.189:8000'
export BLUETOOTH_VEHICLE_ID='VHC-001'
export BLUETOOTH_UDP_HOST='0.0.0.0'
export BLUETOOTH_UDP_PORT='5000'
python3 start_edge.py
```

设置 `GPS_ENABLED=0`、`CAMERA_1_ENABLED=0` 或 `CAMERA_2_ENABLED=0` 可单独关闭组件。
蓝牙默认关闭，以免未配置云端时影响现有 Jetson。配置好 `CLOUD_HTTP_URL`、`VEHICLE_INGEST_TOKEN` 后，将 `BLUETOOTH_ENABLED` 设为 `1` 即可。启动后查看 `logs/bluetooth.log`。

当前实验链路中，笔记本运行仓库 `bluetooth_gateway/ble_s32_sender.py`，通过 BLE 向 nRF52840 写入测试报文；nRF52840 经 UART 交给 S32K344，由开发板向 Ubuntu 的有线网口 `192.168.4.100:5000` 发送 UDP。Ubuntu 的 `bluetooth_edge_agent.py` 接收后通过 `CLOUD_HTTP_URL` 上传云端。笔记本无需运行 `ble_to_udp_gateway.py`，Ubuntu 也无需直接连接蓝牙适配器。请根据实际有线网口配置调整上述地址。

## 已排除的旧文件

- `gps_monitor.py`：只打印串口状态，不生成云端使用的导航数据，已由 `navigation_gps.py` 完整替代。
- 旧日志、PID、临时 JSON：运行产物，不进入源码包。
- 电脑测试启动器和云端服务代码：不属于 Jetson 边缘端。
