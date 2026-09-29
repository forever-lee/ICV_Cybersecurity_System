# Jetson 边缘端代码

该目录是从 Jetson Orin NX Super 实际运行环境整理出的最小可运行包，只包含：

- 两路 H.265 摄像头采流与云端上传；
- Jetson UART GNSS 定位与车速采集；
- fMP4/WebSocket 协议；
- 一个统一的进程管理入口。

## 文件

- `start_edge.py`：唯一推荐入口，同时监管 GPS、VHC-001 和 VHC-002。
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
python3 start_edge.py
```

设置 `GPS_ENABLED=0`、`CAMERA_1_ENABLED=0` 或 `CAMERA_2_ENABLED=0` 可单独关闭组件。

## 已排除的旧文件

- `gps_monitor.py`：只打印串口状态，不生成云端使用的导航数据，已由 `navigation_gps.py` 完整替代。
- 旧日志、PID、临时 JSON：运行产物，不进入源码包。
- 电脑测试启动器和云端服务代码：不属于 Jetson 边缘端。
