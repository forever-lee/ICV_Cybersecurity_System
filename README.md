# V-SHIELD 云端服务

当前根目录只保留云端服务。Jetson 边缘端代码已经独立整理到 [`jetson_edge/`](jetson_edge/README.md)。

## 云端文件

- `run_cloud.py`：云端启动入口和运行配置。
- `server.py`：FastAPI、WebSocket、视频中继、车辆遥测与导航数据服务。
- `stream_protocol.py`：云端与 Jetson 共用的二进制帧协议。
- `static/`：管理大屏及免口令 VHC-001 只读视频页面。
- `requirements.txt`：云端 Python 依赖。
- `.env.example`：云端环境变量示例。

## 启动

当前验证环境：

```powershell
D:\Anconda\envs\TM\python.exe run_cloud.py
```

默认监听：

```text
http://0.0.0.0:8001
```

公网隧道地址在 `run_cloud.py` 中配置。当前入口：

- 管理大屏：`http://719b8bb7.r7.nas.cpolar.cn/`
- VHC-001 免口令只读视频：`http://719b8bb7.r7.nas.cpolar.cn/public/live/VHC-001`

## 当前数据链路

```text
Jetson 摄像头 RTSP/H.265
  → FFmpeg 零转码 fMP4
  → WebSocket 上传
  → 云端短缓冲
  → 浏览器 MediaSource 播放

Jetson GNSS /dev/ttyTHS1
  → navigation_live.json
  → 视频遥测 WebSocket
  → 云端车辆状态
  → 网页地图、速度、轨迹
```

网页不再读取访问者手机定位，仅显示 Jetson 上传的 GNSS/CAN 数据。

## 核心接口

- `GET /healthz`：进程健康状态。
- `GET /readyz`：服务就绪状态。
- `GET /api/vehicles`：车辆列表。
- `GET /api/vehicles/{vehicle_id}/metrics`：车辆指标与导航数据。
- `WS /ws/ingest-fmp4/{vehicle_id}`：Jetson fMP4 上传入口。
- `WS /ws/live-fmp4/{vehicle_id}`：鉴权浏览器视频入口。
- `WS /ws/public/live-fmp4/VHC-001`：VHC-001 公开只读视频入口。
- `WS /ws/metrics/{vehicle_id}`：网页指标入口。
- `POST /api/vehicles/{vehicle_id}/bluetooth`：BLE 遥测接收接口。
- `POST /api/vehicles/{vehicle_id}/wifi`：WiFi 遥测接收接口。
- `POST /api/vehicles/{vehicle_id}/navigation`：独立导航数据接收接口。

## 安全说明

管理大屏继续使用独立访问口令；车端上传使用 `VEHICLE_INGEST_TOKEN`。公开视频页面只开放 VHC-001 视频，不开放车辆定位、指标或控制接口。
