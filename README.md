# V-SHIELD 云端服务

电脑 A 运行云端服务和 Web 页面；Ubuntu 蓝牙电脑运行 `ble_s32_sender.py`；Jetson 统一运行视频、导航、蓝牙、WiFi 和车身域边缘端。Jetson 的完整操作见 [`jetson_edge/README.md`](jetson_edge/README.md)。

## 云端文件

- `run_cloud.py`：云端启动入口和运行配置。
- `server.py`：FastAPI、WebSocket、视频中继、车辆遥测、导航和座舱图像比对服务。
- `hmi_comparison.py`：智能座舱 Expected/Actual 图像比对算法。
- `stream_protocol.py`：云端与 Jetson 共用的二进制帧协议。
- `static/`：管理大屏及免口令 VHC-001 只读视频页面。
- `requirements.txt`：云端 Python 依赖。
- `.env.example`：云端环境变量示例。

## 电脑 A：启动云端与 Web

上传 Token、网页访问口令和公网地址已经在 `run_cloud.py` 的“可修改配置”区域中设置好，电脑 A 直接启动即可：

```powershell
D:\Anconda\envs\TM\python.exe run_cloud.py
```

以后配置发生变化时，直接修改 [`run_cloud.py`](run_cloud.py) 顶部的以下变量，不需要在每次启动前设置环境变量：

- `PUBLIC_DASHBOARD_URL`：cpolar 映射后的公网地址；
- `INGEST_TOKEN`：Jetson 上传数据使用的 Token，必须与 `start_edge.py` 中的 `DEFAULT_VEHICLE_INGEST_TOKEN` 一致；
- `DASHBOARD_ACCESS_TOKEN`：管理大屏访问口令；
- `HOST`、`PORT`：云端监听地址和端口。

默认监听：

```text
http://0.0.0.0:8001
```

确认电脑 A 本机健康检查正常：

```powershell
curl.exe http://127.0.0.1:8001/healthz
```

当前已使用 cpolar 将电脑 A 的 `8001` 端口映射到源码中的公网地址。映射域名变化时，同时修改 `run_cloud.py` 的 `PUBLIC_DASHBOARD_URL`，以及 `jetson_edge/start_edge.py` 的 `DEFAULT_CLOUD_HTTP_URL` 和 `DEFAULT_CLOUD_WS_URL`。

- 管理大屏：`PUBLIC_DASHBOARD_URL/`
- VHC-001 免口令只读视频：`PUBLIC_DASHBOARD_URL/public/live/VHC-001`

## Jetson：启动统一边缘端

K3/S32K344 程序烧录到 Flash 后，只需给板子上电，不需要每次打开 S32DS。Jetson 端执行：

```bash
cd ~/Desktop/body_1/ICV_Cybersecurity_System/jetson_edge
python3 start_edge.py
```

Jetson 不启动 `run_cloud.py`，也不在 Jetson 打开 Web 页面；Jetson 只需手动运行 `start_edge.py`。它会统一拉起并监管 `WiFi_Module.py`、`bluetooth_edge_agent.py`、`body_edge_agent.py`、GPS 和两路摄像头上传进程，不需要再单独启动这些子进程。

`start_edge.py` 集中配置的默认云端地址为：

```text
CLOUD_HTTP_URL=http://3f7b7aec.r7.nas.cpolar.cn
CLOUD_WS_URL=ws://3f7b7aec.r7.nas.cpolar.cn
VEHICLE_INGEST_TOKEN=vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0
```

所有子进程统一继承这些源码默认值，不需要逐个修改子进程，也不需要在启动前临时设置。以后公网域名或 Token 变化时，直接修改 [`jetson_edge/start_edge.py`](jetson_edge/start_edge.py) 顶部的 `DEFAULT_CLOUD_HTTP_URL`、`DEFAULT_CLOUD_WS_URL` 和 `DEFAULT_VEHICLE_INGEST_TOKEN`。

## 当前数据链路

```text
摄像头 1（192.168.4.88）/ 摄像头 2（192.168.4.89）RTSP/H.265
  → FFmpeg 零转码 fMP4
  → WebSocket 上传
  → 云端短缓冲
  → 浏览器 MediaSource 播放

智能座舱测试页面
  → 复用当前车辆浏览器视频画面或上传 Actual 图片
  → POST /api/vehicles/{vehicle_id}/cockpit/compare
  → 电脑 A 执行 Expected/Actual 图像比对
  → 返回相似度、MS-SSIM、差异区域和 PASS/FAIL

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
  → Jetson eno1（192.168.4.100）监听 UDP 6001
  → WiFi_Module.py（由 start_edge.py 自动启动和监管）
  → HTTP POST /api/vehicles/{vehicle_id}/wifi
  → cpolar 公网映射 / 电脑 A 云端 8001
  → WiFi 充电状态、报文历史与 IDS 告警页面

Ubuntu 蓝牙电脑运行 ble_s32_sender.py
  → BLE 写入 nRF52840
  → UART 转交蓝牙板（192.168.4.20）
  → 以太网 UDP 5000
  → Jetson bluetooth_edge_agent.py（由 start_edge.py 监管）
  → HTTP POST /api/vehicles/{vehicle_id}/bluetooth
  → 蓝牙正常/异常报文页面

Ubuntu 蓝牙电脑只配置 BLE 地址，不配置云端地址或上传 Token，也不与云端
直接通信。蓝牙页面暂时保留端到端延迟和发送启停位置，当前统一显示“待接入”；
两个启停按钮保持禁用，不会向后端创建控制命令。

车身板（192.168.4.31）UDP 7100
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

`192.168.4.x` 是板端设备、摄像头、交换机和 Jetson `eno1` 所在的固定有线网段：

| 设备 | 固定地址 | 用途 |
| --- | --- | --- |
| Jetson `eno1` | `192.168.4.100/24` | 接收摄像头 RTSP、车身/蓝牙/WiFi UDP 数据 |
| 车身板 | `192.168.4.31` | 车身状态 UDP 7100、控制与对时 UDP 7101 |
| WiFi 板 | `192.168.4.201` | 向 Jetson UDP 6001 转发充电报文 |
| 蓝牙板 | `192.168.4.20` | 向 Jetson UDP 5000 转发蓝牙报文 |
| 摄像头 1 | `192.168.4.88` | VHC-001 RTSP 视频流 |
| 摄像头 2 | `192.168.4.89` | VHC-002 RTSP 视频流 |

WiFi 链路中的 `192.168.3.x` 仅用于 BMS 报文产生端与 ESP32 热点之间的无线连接。Jetson 收到有线侧 UDP 报文后，再通过可访问电脑 A 或互联网的网络接口上传到云端。

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
- `POST /api/vehicles/{vehicle_id}/bluetooth/timing`：预留接口，当前发送端不调用。
- `POST /api/vehicles/{vehicle_id}/bluetooth/control`：预留接口，当前网页不调用。
- `GET /api/vehicles/{vehicle_id}/bluetooth/control/next`：预留控制接口，当前发送端不轮询。
- `POST /api/vehicles/{vehicle_id}/bluetooth/control/ack`：预留确认接口，当前发送端不调用。
- `POST /api/vehicles/{vehicle_id}/wifi`：WiFi 遥测接收接口。
- `POST /api/vehicles/{vehicle_id}/navigation`：独立导航数据接收接口。
- `POST /api/vehicles/{vehicle_id}/domains/body`：车身域状态上传接口。
- `POST /api/vehicles/{vehicle_id}/body/event`：提交真实车身控制命令；等待 K3 CAN0 回读成功后更新页面。
- `POST /api/vehicles/{vehicle_id}/domains/body/control`：Jetson 轮询车身域启停控制。
- `POST /api/vehicles/{vehicle_id}/body/transmission`：网页设置车身域上传状态。
- `GET /api/vehicles/{vehicle_id}/body/history`：车身域历史报文。
- `GET /api/cockpit/comparison/status`：座舱图像比对算法状态。
- `POST /api/vehicles/{vehicle_id}/cockpit/compare`：座舱 Expected/Actual 图像比对。

## 智能座舱图像比对

座舱页面复用现有 Jetson 摄像头视频链路，不需要额外启动座舱边缘进程。浏览器可以采集当前中控画面，也可以上传不超过 4 MB 的 JPEG、PNG 或 WebP 图片，然后由电脑 A 上的 `hmi_comparison.py` 与本地参考图库进行比对。

云端需要安装 OpenCV 与 NumPy：

```powershell
python -m pip install -r requirements.txt
```

如需从其他位置加载算法文件，可以设置 `HMI_COMPARISON_PATH`；最大图片大小由 `MAX_COMPARISON_IMAGE_BYTES` 控制。图像算法加载失败时 `/readyz` 返回 `degraded`，其他车辆域仍继续服务。

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
| Jetson | `jetson_edge/start_edge.py` | 唯一手动启动入口；自动以子进程运行 `WiFi_Module.py` 等全部边缘组件 |
| 交换机 | 无需运行程序 | 连接板端设备、摄像头和 Jetson `eno1`；电脑 A 可选接入 |

### 推荐网络拓扑

```text
电脑 B（连接 S32K344_WIFI 热点）
  → ESP32 192.168.3.1:8080
  → S32K344
  → 交换机 / 192.168.4.x
  → Jetson eno1（192.168.4.100，UDP 6001）

Jetson WiFi（例：192.168.2.207）
  → 电脑 A WiFi（例：192.168.2.136，HTTP 8001）
  → 浏览器页面
```

Jetson 必须同时具备两条网络：`eno1` 固定为 `192.168.4.100/24`，用于接收板端 UDP 和摄像头 RTSP；另一条可联网接口用于把数据上传给电脑 A。Jetson 必须能访问电脑 A 的 `8001` 端口。

### 启动顺序

1. **电脑 A：启动后端。** 进入仓库根目录，使用已经安装依赖的 Python 环境运行：

   ```powershell
   python run_cloud.py
   ```

   后端默认监听 `0.0.0.0:8001`。浏览器可打开 `http://127.0.0.1:8001`。若 Jetson 通过 WiFi 上传，Windows 防火墙需要允许 Jetson 访问 TCP 8001，例如：

   ```powershell
   New-NetFirewallRule -DisplayName "Jetson WiFi Forward 8001" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8001 -RemoteAddress 192.168.2.207
   ```

2. **Jetson：启动统一边缘端。** 确认 `eno1` 已配置为 `192.168.4.100/24`，然后只运行 `start_edge.py`。映射域名、WebSocket 地址和上传 Token 已写入源码，不需要在启动命令前设置 `CLOUD_HTTP_URL` 或其他环境变量。程序会先 Ping 两路摄像头、车身板、WiFi 板和蓝牙板并打印结果，随后自动启动 `WiFi_Module.py` 等所有子进程：

   ```bash
   cd jetson_edge
   python3 start_edge.py
   ```

   `start_edge.py` 会打印每个设备的 `[PING OK]` 或 `[PING FAIL]`，再显示统一启动的组件列表。WiFi 子进程日志位于 `jetson_edge/logs/wifi.log`；成功转发时该日志会记录 `CLOUD : accepted`。

3. **电脑 B：启动 BMS 报文产生器。** 电脑 B 先连接 `S32K344_WIFI` 热点，再进入仓库根目录运行：

   ```powershell
   python bms_wifi_sender.py
   ```

   程序会先检查热点 `192.168.3.1`，倒数 3 秒后开始发送。每秒一条充电报文；每轮 120 秒，随机插入 1～3 条 SOC 突变报文用于 IDS 演示。`Ctrl+C` 可停止。

#### 使用 cpolar 公网映射（可选）

电脑 A 已将本地 `8001` 端口通过 cpolar 映射到公网；Jetson 使用 `start_edge.py` 源码中的默认域名上传视频、GPS、车身、蓝牙和 WiFi 数据，不要求 Jetson 与电脑 A 位于同一局域网。

当前公网域名为：

```text
3f7b7aec.r7.nas.cpolar.cn
```

公网和本地服务的对应关系为：

```text
http://3f7b7aec.r7.nas.cpolar.cn
  → cpolar
  → 电脑 A 的 http://127.0.0.1:8001
```

Jetson 上的不同上传协议使用同一个公网域名，但 URL 协议不同：

```text
视频及 GPS 遥测：ws://3f7b7aec.r7.nas.cpolar.cn
车身、蓝牙和 WiFi 报文上传：http://3f7b7aec.r7.nas.cpolar.cn
```

`jetson_edge/start_edge.py` 已内置以上两个地址。直接运行它会同时监管车身、蓝牙、WiFi、GPS 和两路视频上传进程：

```bash
cd jetson_edge
python3 start_edge.py
```

公网方式下，Jetson 的联网接口只需要能够访问互联网；Jetson `eno1` 仍固定为 `192.168.4.100/24`，并与板端设备、摄像头保持在可互通的 `192.168.4.x` 网络中。公网映射只替代“Jetson → 电脑 A TCP 8001”这一段，不改变板端到 Jetson 的 UDP 链路。

### 排查顺序

1. Jetson 窗口没有报文：先查看 `start_edge.py` 启动时的 Ping 结果，再检查 `eno1=192.168.4.100/24` 和对应板端地址。
2. Jetson 显示 `Cloud forwarding failed`：先在 Jetson 执行 `curl -i http://电脑A_IP:8001/healthz`，再检查电脑 A 后端和防火墙。
3. Jetson 显示 `CLOUD : accepted`、页面仍无变化：刷新电脑 A 的网页，确认浏览器打开的是本机 `8001` 服务。
