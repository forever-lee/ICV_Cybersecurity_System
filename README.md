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

## WiFi 充电报文演示

这一套演示把电脑端模拟的 BMS 充电报文，经 ESP32、S32K344 和 Jetson 转发到云端页面。WiFi 接收器位于 [`jetson_edge/WiFi_Module.py`](jetson_edge/WiFi_Module.py)，报文产生器位于根目录的 [`bms_wifi_sender.py`](bms_wifi_sender.py)。

### 设备职责

| 设备 | 需要运行的文件 | 作用 |
| --- | --- | --- |
| 电脑 A（后端） | `run_cloud.py` | 接收 Jetson 转发的 HTTP 报文并显示网页 |
| 电脑 B（BMS 模拟端） | `bms_wifi_sender.py` | 连接 ESP32 热点，持续发送正常/异常充电报文 |
| ESP32 + S32K344 | 已烧录的板端固件 | ESP32 接收 WiFi UDP；S32K344 通过以太网转发 UDP |
| Jetson | `jetson_edge/WiFi_Module.py` | 监听板端 UDP 6001，并转发到电脑 A 后端 |
| 交换机 | 无需运行程序 | 连接 S32K344、Jetson `eth0`；电脑 A 可选接入 |

### 推荐网络拓扑

```text
电脑 B（连接 S32K344_WIFI 热点）
  → ESP32 192.168.3.1:8080
  → S32K344
  → 交换机 / 192.168.4.x
  → Jetson eth0（例：192.168.4.100，UDP 6001）

Jetson WiFi（例：192.168.2.207）
  → 电脑 A WiFi（例：192.168.2.136，HTTP 8001）
  → 浏览器页面
```

Jetson 必须同时具备两条网络：`eth0` 用于接收板子的 UDP；WiFi 用于把数据上传给电脑 A。示例 IP 只用于当前实验环境，可替换为自己的地址；关键是 Jetson 必须能访问电脑 A 的 `8001` 端口。

### 启动顺序

1. **电脑 A：启动后端。** 进入仓库根目录，使用已经安装依赖的 Python 环境运行：

   ```powershell
   python run_cloud.py
   ```

   后端默认监听 `0.0.0.0:8001`。浏览器可打开 `http://127.0.0.1:8001`。若 Jetson 通过 WiFi 上传，Windows 防火墙需要允许 Jetson 访问 TCP 8001，例如：

   ```powershell
   New-NetFirewallRule -DisplayName "Jetson WiFi Forward 8001" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8001 -RemoteAddress 192.168.2.207
   ```

2. **Jetson：启动 UDP 接收与转发。** 先确认 Jetson 可到达板端以太网 IP、以及电脑 A 的后端 IP。将 `192.168.2.136` 替换为电脑 A 当前 WiFi IP：

   ```bash
   ping -c 4 192.168.4.201
   curl -i http://192.168.2.136:8001/healthz
   CLOUD_HTTP_URL=http://192.168.2.136:8001 python3 jetson_edge/WiFi_Module.py
   ```

   正常启动后会显示 `UDP receiver started, listening on 0.0.0.0:6001`。每次成功转发会显示 `CLOUD : accepted`。

3. **电脑 B：启动 BMS 报文产生器。** 电脑 B 先连接 `S32K344_WIFI` 热点，再进入仓库根目录运行：

   ```powershell
   python bms_wifi_sender.py
   ```

   程序会先检查热点 `192.168.3.1`，倒数 3 秒后开始发送。每秒一条充电报文；每轮 120 秒，随机插入 1～3 条 SOC 突变报文用于 IDS 演示。`Ctrl+C` 可停止。

### 排查顺序

1. Jetson 窗口没有报文：检查 S32K344 到 Jetson 的以太网连接与 `192.168.4.x` 地址。
2. Jetson 显示 `Cloud forwarding failed`：先在 Jetson 执行 `curl -i http://电脑A_IP:8001/healthz`，再检查电脑 A 后端和防火墙。
3. Jetson 显示 `CLOUD : accepted`、页面仍无变化：刷新电脑 A 的网页，确认浏览器打开的是本机 `8001` 服务。
