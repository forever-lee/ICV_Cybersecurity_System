# V-SHIELD 云端服务

电脑 A 运行云端服务和 Web 页面；Jetson 统一运行视频、导航、WiFi 和车身域边缘端。Jetson 的完整操作见 [`jetson_edge/README.md`](jetson_edge/README.md)。

## 云端文件

- `run_cloud.py`：云端启动入口和运行配置。
- `server.py`：FastAPI、WebSocket、视频中继、车辆遥测与导航数据服务。
- `stream_protocol.py`：云端与 Jetson 共用的二进制帧协议。
- `static/`：管理大屏及免口令 VHC-001 只读视频页面。
- `requirements.txt`：云端 Python 依赖。
- `.env.example`：云端环境变量示例。

## 电脑 A：启动云端与 Web

先设置上传 Token 和网页访问口令，再启动：

```powershell
$env:VEHICLE_INGEST_TOKEN = "请换成长随机字符串"
$env:DASHBOARD_ACCESS_TOKEN = "请换成另一个访问口令"
$env:PUBLIC_DASHBOARD_URL = "http://你的cpolar地址"
D:\Anconda\envs\TM\python.exe run_cloud.py
```

默认监听：

```text
http://0.0.0.0:8001
```

确认电脑 A 本机健康检查正常：

```powershell
curl.exe http://127.0.0.1:8001/healthz
```

如使用 cpolar，把 `8001` 映射到公网；Jetson 的 `CLOUD_HTTP_URL` 填电脑 A 的局域网地址或该公网地址。

- 管理大屏：`PUBLIC_DASHBOARD_URL/`
- VHC-001 免口令只读视频：`PUBLIC_DASHBOARD_URL/public/live/VHC-001`

## Jetson：启动统一边缘端

K3/S32K344 程序烧录到 Flash 后，只需给板子上电，不需要每次打开 S32DS。Jetson 端执行：

```bash
cd ~/Desktop/body_1/ICV_Cybersecurity_System/jetson_edge
export CLOUD_HTTP_URL='http://电脑A的IP:8001'
export VEHICLE_INGEST_TOKEN='与电脑A完全一致的Token'
python3 start_edge.py
```

Jetson 不启动 `run_cloud.py`，也不在 Jetson 打开 Web 页面；`start_edge.py` 统一监管视频、导航、WiFi 和车身域组件。

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

BMS 报文产生端（电脑 A 或电脑 B，连接 S32K344_WIFI）
  → WiFi / UDP 报文
  → ESP32 热点 192.168.3.1
  → UART
  → S32K344 以太网 192.168.4.201
  → 网线 / UDP
  → 交换机
  → Jetson eth0（图示地址 192.168.4.11）监听 UDP 6001
  → WiFi_Module.py
  → HTTP POST /api/vehicles/{vehicle_id}/wifi
  → cpolar 公网映射 / 电脑 A 云端 8001
  → WiFi 充电状态、报文历史与 IDS 告警页面

S32K344 UDP 7100
  → Jetson body_edge_agent.py
  → HTTP/HTTPS 上传
  → 电脑 A server.py
  → 车身域 Web 页面

车身操作联调按钮
  → 云端命令队列（pending / executing）
  → Jetson HTTP 轮询并通过 UDP 7101 下发
  → S32K344 发送 0x351 / 0x352 / 0x353 真实 CAN 报文
  → CAN0 回读后经 UDP 7100、Jetson、云端确认
  → Web 仅在真实回读成功后改变画面
  → 事件期间屏蔽其他周期门锁/灯光动作，只呈现本次事件
  → 事件保持约 5 秒后恢复原周期演示

电脑 A 页面控制
  → Jetson HTTP 轮询
  → UDP 7101
  → S32K344
```

网页不再读取访问者手机定位，仅显示 Jetson 上传的 GNSS/CAN 数据。

WiFi 链路中，`192.168.3.x` 是 BMS 报文产生端与 ESP32 热点之间的无线网段，`192.168.4.x` 是 S32K344、交换机与 Jetson `eth0` 之间的有线网段。图中的 `192.168.4.11` 是示例地址，部署时应以 Jetson 实际的 `eth0` 地址为准。Jetson 收到 UDP 6001 报文后，再通过自身可访问互联网的网络接口将数据上传到云端。

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
- `POST /api/vehicles/{vehicle_id}/domains/body`：车身域状态上传接口。
- `POST /api/vehicles/{vehicle_id}/body/event`：提交真实车身控制命令；等待 K3 CAN0 回读成功后更新页面。
- `POST /api/vehicles/{vehicle_id}/domains/body/control`：Jetson 轮询车身域启停控制。
- `POST /api/vehicles/{vehicle_id}/body/transmission`：网页设置车身域上传状态。
- `GET /api/vehicles/{vehicle_id}/body/history`：车身域历史报文。

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

#### 使用 cpolar 公网映射（可选）

上面的局域网直连方式继续保留。若电脑 A 已将本地 `8001` 端口通过 cpolar 映射到公网，Jetson 也可以通过公网域名上传视频、GPS 和 WiFi 数据，不再要求 Jetson WiFi 与电脑 A 位于同一局域网。

当前公网域名为：

```text
15151e1e.r7.nas.cpolar.cn
```

公网和本地服务的对应关系为：

```text
http://15151e1e.r7.nas.cpolar.cn
  → cpolar
  → 电脑 A 的 http://127.0.0.1:8001
```

Jetson 上的不同上传协议使用同一个公网域名，但 URL 协议不同：

```text
视频及 GPS 遥测：ws://15151e1e.r7.nas.cpolar.cn
WiFi 报文上传：http://15151e1e.r7.nas.cpolar.cn
```

`jetson_edge/start_edge.py` 已内置以上两个地址。直接运行它会同时监管 GPS、WiFi 和两路视频上传进程：

```bash
cd jetson_edge
python3 start_edge.py
```

启动前可在 Jetson 上检查公网入口：

```bash
curl -i http://15151e1e.r7.nas.cpolar.cn/healthz
```

公网方式下，Jetson 的 WiFi 只需要能够访问互联网；Jetson `eth0` 与 S32K344 仍需保持在可互通的 `192.168.4.x` 网络中。公网映射只替代“Jetson WiFi → 电脑 A TCP 8001”这一段，不改变“S32K344 → Jetson UDP 6001”链路。

### 排查顺序

1. Jetson 窗口没有报文：检查 S32K344 到 Jetson 的以太网连接与 `192.168.4.x` 地址。
2. Jetson 显示 `Cloud forwarding failed`：先在 Jetson 执行 `curl -i http://电脑A_IP:8001/healthz`，再检查电脑 A 后端和防火墙。
3. Jetson 显示 `CLOUD : accepted`、页面仍无变化：刷新电脑 A 的网页，确认浏览器打开的是本机 `8001` 服务。
