# V-SHIELD 智能网联汽车攻防平台

平台按照智能网联汽车域控架构组织车辆资产、实时采集与攻防能力。当前版本已接入视频、蓝牙和 WiFi 信息采集，并预留底盘、动力、车身、座舱等域数据接口；攻击验证、检测防御和安全事件模块目前为规划界面，后续可接入实际引擎。

底层工程把原来的“本机打开 RTSP 窗口”拆成两个可独立部署的组件：

- `h264_vehicle_agent.py`：车端 H.264/fMP4 上传核心，Windows 使用 FFmpeg/NVENC，Jetson 使用 NVIDIA GStreamer 硬件管线。
- `vehicle_agent.py`：旧版 JPEG 兼容链路，新部署不再使用。
- `server.py`：部署在云端，鉴权接收车辆数据，将最新画面分发给网页，同时提供车辆、指标、历史与控制域扩展 API。
- `Bluetooth_Module.py`：接收 BLE 转 UDP 报文，记录文本/HEX，并通过鉴权接口转发到云端蓝牙数据模块。
- `WiFi_Module.py`：接收 WiFi 转 UDP 报文，记录文本/HEX，并通过鉴权接口转发到云端 WiFi 数据模块。
- `static/`：实时监控大屏，显示端到端延迟、帧率、上行速率、丢帧、重连、观看端数量和各控制域状态。

## 1. 本机快速演示

使用指定环境：`D:\Anconda\envs\TM\python.exe`。

### PyCharm 直接运行（推荐）

1. 确认 PyCharm 项目解释器为 `D:\Anconda\envs\TM\python.exe`。
2. 右键运行 `run_cloud.py`。
3. 再右键运行 `run_vehicle.py`。
4. 浏览器打开 `http://127.0.0.1:8000`。

摄像头地址、云端地址、车辆编号、视频质量和 Token 都集中在这两个启动文件顶部，可直接修改。

浏览器打开 `http://127.0.0.1:8000`。如需在没有车辆的情况下调试，可暂时将 `run_vehicle.py` 中的 `RTSP_URL` 改为 `lavfi:testsrc2=size=960x540:rate=15`。

蓝牙验证：先运行 `run_cloud.py`，再运行 `Bluetooth_Module.py`。网页底部的“蓝牙数据模块”会实时显示报文。没有板子时可直接点击网页里的“生成测试数据”，也可以另开终端执行：

```powershell
python Bluetooth_Module.py --send-test --count 3
```

真机/公网使用时，把 `CLOUD_HTTP_URL`、`VEHICLE_ID`、`VEHICLE_INGEST_TOKEN` 配成与云端一致；其中公网地址使用 `https://...`，不要使用 `wss://...`。

WiFi 验证：运行 `WiFi_Module.py` 后，网页底部的“WiFi 数据模块”会显示 UDP 6000 收到的报文。没有板子时可点击网页里的“生成测试数据”，或另开终端执行 `python WiFi_Module.py --send-test --count 3`。

当前网页入口为 `http://61bf8db4.vip.cpolar.top`。当 `run_vehicle.py` 与 `run_cloud.py` 在同一台电脑运行时，车端固定使用 `ws://127.0.0.1:8000` 本机直连；Jetson 真车端使用 `ws://61bf8db4.vip.cpolar.top` 上传。cpolar 地址变化后同步修改 Jetson 的 `CLOUD_WS_URL`。

公网页面启用了独立访问口令，打开网址后输入 `run_cloud.py` 启动窗口中显示的口令。Windows 电脑端默认使用 1280×720、30 FPS、H.264 High 2.8 Mbps 严格限速，以适配当前实测约 3.5 Mbps 的 cpolar 公网链路。车端优先使用 NVDEC 解码、CUDA 缩放与 NVENC 编码的全 GPU 链路，并自动回退到软件解码加 NVENC、最后回退 libx264。NVENC 使用 P7 高质量 CBR、双遍、前向分析、时空 AQ 与 B 帧；每 1 秒产生一个独立可解码的 fMP4 片段。浏览器通过 Media Source Extensions 原生解码，并在累计约 10 秒连续内容后开始播放，不会因为延迟增加主动跳帧。

### Jetson TX2 车端

`run_vehicle.py` 和 `run_vehicle_jetson.py` 都使用 H.264/fMP4 主链路。Windows 调试机优先走 FFmpeg NVENC；TX2 使用 `nvv4l2decoder + nvvidconv + nvv4l2h264enc + mp4mux`，视频解码、缩放和 H.264 编码由 Jetson 硬件完成，不再逐帧上传 JPEG。

板端启动前运行 `python3 run_vehicle_jetson.py --check-only`；它会检查 `nvv4l2decoder`、`nvvidconv`、`nvv4l2h264enc`、`h264parse`、`mp4mux`、摄像头对应的 H.264/H.265 解包插件和 Python 依赖。该链路直接调用 `gst-launch-1.0`，不依赖 OpenCV 是否启用 GStreamer。

项目已提供 TX2 专用启动文件。把整个项目复制到板子后执行：

```bash
python3 -m pip install -r requirements_jetson.txt
python3 run_vehicle_jetson.py --check-only
python3 run_vehicle_jetson.py
```

不要在 TX2 上安装项目原来的 `requirements.txt`。板端只安装 `requirements_jetson.txt`，保留 JetPack 自带的 GStreamer/NVIDIA 多媒体组件。默认档位为 1280×720、20 FPS、H.264 High、2.2 Mbps、1 秒 fMP4 片段和 5 分钟 FIFO；摄像头输入默认 H.265，所有配置均可用环境变量覆盖。

### 需要复制到 Jetson 的文件

视频与边缘导航上传文件放在 Jetson 同一个目录：

- `run_vehicle_jetson.py`
- `h264_vehicle_agent.py`
- `stream_protocol.py`
- `requirements_jetson.txt`
- `Navigation_Module_Board.py`（需要地图、轨迹和车速时使用）

如果 Jetson 还负责蓝牙和 WiFi UDP 数据转发，再额外复制 `Bluetooth_Module_Board.py` 和 `WiFi_Module_Board.py`。网页、`server.py`、`run_cloud.py` 和 `static/` 只放云端，不需要放到 Jetson。

## 2. 云端部署方式

云服务器开放 HTTPS 入口，例如 `https://vehicle.example.com`。使用 Nginx、云负载均衡或 API 网关终止 TLS，并确保 WebSocket Upgrade 可透传。车端参数改为：

```powershell
D:\Anconda\envs\TM\python.exe h264_vehicle_agent.py `
  --source rtsp://192.168.4.88:8554/main `
  --cloud wss://vehicle.example.com `
  --vehicle-id VHC-001 `
  --token "每车独立的高强度令牌"
```

云端与车端必须使用同一个 `VEHICLE_INGEST_TOKEN`。当前令牌机制适合样机和内网验证；正式部署应升级为每车独立证书的双向 TLS，并将凭据放入 TPM/HSM，不写入脚本或镜像。

## 3. 画质优先与深度 FIFO 缓冲策略

- OpenCV/FFmpeg 使用 TCP RTSP 并持续排空摄像头源缓冲，避免读取端自身越积越慢。
- Windows 电脑端默认使用 1280×720、30 FPS、H.264 High 2.8 Mbps 严格限速；保留 P7、双遍、前向分析、AQ 与 B 帧，在当前公网带宽内优先保证连续播放并尽量保留细节。
- 车端和云端各保留最多 300 个 1 秒 fMP4 片段；链路恢复后按 FIFO 顺序补传，观看期间不主动追到最新画面。浏览器预缓冲约 10 秒，卡顿后积累 6 秒连续内容再恢复播放。
- Jetson 默认使用 1280×720、20 FPS、2.2 Mbps 严格 CBR，并保留最长 5 分钟编码片段。持续带宽低于 2.2 Mbps 时仍需降码率，缓冲不能长期弥补带宽缺口。
- 每帧携带采集、发送时间与序号。页面端到端延迟包含车端接收 RTSP 后到浏览器完成绘制的时间。
- WebSocket 单次发送允许等待 60 秒；重连后继续补传车端 FIFO 中仍保留的片段。
- OpenCV RTSP 打开和读取默认 5 秒超时，超时后释放连接并重新拉流，避免坏连接阻塞约 30 秒。
- 延迟数值依赖车端和云端正确对时。量产环境建议 PTP（车内）与 chrony/NTP（车云）并监控时钟偏差。

## 4. 高德地图、实时位置与车速接入

先在高德开放平台申请“Web 端（JS API）”Key。启动云端前设置：

```powershell
$env:AMAP_JS_KEY="你的 Web 端 Key"
$env:AMAP_SECURITY_JS_CODE="对应的安全密钥"
python run_cloud.py
```

生产环境不要把安全密钥明文下发到浏览器，建议按高德文档配置安全代理，并使用 `AMAP_SERVICE_HOST` 传入代理的 `/_AMapService` 地址。Key、安全密钥或绑定域名不正确时，智能驾驶域会直接显示地图加载错误。

车端导航数据统一使用以下格式。GPS/GNSS 通常输出 WGS-84，页面会自动转换为高德 GCJ-02 坐标；如果板端已经完成坐标转换，则将 `coordinate_system` 改成 `GCJ02`。

```json
{
  "latitude": 31.2304,
  "longitude": 121.4737,
  "speed_kph": 42.6,
  "heading_deg": 88.0,
  "accuracy_m": 2.1,
  "coordinate_system": "WGS84",
  "source": "GNSS+CAN",
  "captured_at_ms": 1784116800000
}
```

有两种车端接入方式：

1. Jetson 运行 `Navigation_Module_Board.py`，它把边缘端GNSS/CAN数据原子写入同目录的 `navigation_live.json`。`run_vehicle_jetson.py` 默认每秒读取并随现有WebSocket遥测上传，无需额外配置文件路径。
2. GNSS串口输出标准NMEA时运行：`python3 Navigation_Module_Board.py --mode serial --serial-device /dev/ttyUSB0 --baud 9600`。
3. GNSS/CAN适配程序输出JSON时运行：`python3 Navigation_Module_Board.py --mode udp`，然后向Jetson UDP 7000发送上面的JSON。允许GNSS先发送经纬度、CAN再单独发送 `{"speed_kph":42.6,"source":"EDGE-CAN"}`，模块会自动合并最新值。
4. 没有真实设备时运行 `python3 Navigation_Module_Board.py --mode test`，可在地图上验证边缘端测试轨迹。测试模式使用Jetson生成的坐标，不读取手机或浏览器定位。
5. 独立导航进程也可以鉴权调用 `POST /api/vehicles/{vehicle_id}/navigation`。请求体就是上面的JSON，`Authorization` 使用与车端一致的Bearer Token。

实际车速优先使用导航数据中的 `speed_kph`；未提供时，页面会回退显示底盘域 `domains.chassis.speed_kph`。位置超过 10 秒未更新会明确标记为过期。

网页优先使用云端保存的边缘端车辆导航遥测。如果边缘端定位缺失或超过 15 秒未更新，页面会请求当前访问设备的浏览器定位，临时显示手机的位置、速度与移动轨迹；边缘端定位恢复后会自动切回边缘端。页面会用“数据源：边缘端 GNSS / CAN”或“临时来源：访问设备定位”明确标识当前来源，切换来源时清空旧轨迹，避免把手机与车辆坐标连成一条线。

手机定位只在当前浏览器页面本地展示，不上传为车辆遥测，也不会覆盖云端保存的边缘端数据。浏览器定位通常要求 HTTPS；手机首次打开页面时需要允许位置权限，拒绝权限后页面会继续等待边缘端定位。

## 5. 其他控制域接入

接口已预留，底盘、动力、车身、座舱和智驾数据可独立上报：

```http
POST /api/vehicles/VHC-001/domains/chassis
Authorization: Bearer <token>
Content-Type: application/json

{"status":"active","speed_kph":62.4,"steering_deg":-1.8}
```

视频数据与结构化数据使用相同 `vehicle_id` 聚合，前端不需要修改链路。量产时建议所有数据增加 `schema_version`、源时间戳、序列号、质量位和数据有效期。

## 6. “车规级”边界与量产清单

当前版本是可运行的工程样机，具备边界缓冲、断线重连、看门狗友好的前后台拆分、数据校验、鉴权入口、健康检查和可观测指标，但不能仅凭一套 Python 程序宣称通过车规认证。量产前至少还需要：

1. 视频承载换成 H.265 + SRT/RIST（车到云）或 WebRTC（浏览器最后一跳），配置拥塞控制、FEC、关键帧请求与 TURN。
2. 按 ISO 21434 做威胁分析、密钥生命周期、安全启动、签名 OTA、最小权限和审计；云端启用 WAF、限流、OIDC/RBAC 与租户隔离。
3. 若功能进入安全链路，按 ISO 26262 做 HARA、ASIL 分解、故障注入、独立监控和安全状态设计；监控画面本身不应作为闭环安全控制依据。
4. 车端服务交给 systemd/Windows Service 管理，增加硬件看门狗、磁盘/内存上限、掉电恢复、离线缓存上限与灰度升级回滚。
5. 完成弱网（时延、抖动、丢包、乱序、带宽突降）、72 小时稳定性、温度、电源扰动、多车并发和故障恢复测试。

## 7. 运维接口

- `GET /healthz`：进程健康状态。
- `GET /readyz`：服务就绪状态。
- `GET /api/vehicles`：车辆列表与在线状态。
- `GET /api/vehicles/{vehicle_id}/metrics`：当前链路指标。
- `GET /api/vehicles/{vehicle_id}/history`：最近 180 个遥测采样。
- `GET /api/config/map`：高德地图前端加载配置。
- `POST /api/vehicles/{vehicle_id}/navigation`：边缘端位置、车速与航向上传入口，需要 Bearer Token。
- `WS /ws/ingest/{vehicle_id}`：车端上传入口，需要 Bearer Token。
- `WS /ws/ingest-fmp4/{vehicle_id}`：车端 H.264/fMP4 压缩片段入口。
- `WS /ws/live-fmp4/{vehicle_id}`：浏览器 H.264/fMP4 连续视频入口。
- `WS /ws/live/{vehicle_id}`：旧 JPEG 浏览器入口，仅作兼容回退。
- `WS /ws/metrics/{vehicle_id}`：浏览器指标入口。
- `POST /api/vehicles/{vehicle_id}/bluetooth`：蓝牙数据上传入口，需要 Bearer Token。
- `POST /api/vehicles/{vehicle_id}/bluetooth/test`：网页随机蓝牙测试数据入口。
- `POST /api/vehicles/{vehicle_id}/wifi`：WiFi 数据上传入口，需要 Bearer Token。
- `POST /api/vehicles/{vehicle_id}/wifi/test`：网页随机 WiFi 测试数据入口。
