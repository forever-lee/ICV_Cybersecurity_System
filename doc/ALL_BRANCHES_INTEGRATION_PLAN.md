# 多分支代码融合方案

## 1. 目标与不可变约束

本次融合以 `main` 分支最新代码为唯一基线，将 `wifi`、`bluetooth`、`body`、`cockpit` 等分支中的领域功能选择性迁入集成分支。

必须遵守以下约束：

1. 不改变 `main` 已有智能驾驶域功能和表现：
   - Jetson 摄像头采集与视频上传；
   - 浏览器端实时视频演示；
   - GPS 采集、上传及地图渲染；
   - 保留 `main` 当前白底地图样式。
2. 后端电脑 A 只启动一个入口：

   ```powershell
   python run_cloud.py
   ```

   不在电脑 A 额外手工启动 WiFi、蓝牙、车身或座舱服务进程。
3. Jetson 运行代码统一放在 `jetson_edge/`，唯一人工启动入口为：

   ```bash
   python3 start_edge.py
   ```

4. Jetson 不运行 `run_cloud.py`，不承载网页、云端 API、数据库或 IDS/图像比对服务。
5. 每个领域的“采集并上传后端”代码必须是 `start_edge.py` 管理的独立子进程；一个领域异常不得阻塞摄像头、GPS 或其他领域。
6. Jetson 方案不增加 `.sh` 启动脚本，也不增加 systemd 服务。
7. 不直接合并会删除或覆盖主线功能的分支；尤其禁止直接整体合并 `body` 分支，应按文件和逻辑块选择性迁移。

## 2. 目标运行架构

```text
各域硬件/模拟器
   │
   ├─ 摄像头 ───────────────> camera 子进程 ── WebSocket ─┐
   ├─ GPS 串口 ─────────────> gps 子进程 ────────────────┤
   ├─ WiFi/UDP 报文 ────────> wifi 子进程 ── HTTP(S) ────┤
   ├─ 蓝牙网关/UDP 报文 ────> bluetooth 子进程 ─ HTTP(S) ┤
   ├─ 车身域串口/UDP 报文 ──> body 子进程 ── HTTP(S) ────┤
   └─ 座舱画面源 ───────────> cockpit 子进程 ─ HTTP(S) ──┤
                                                           │
Jetson: jetson_edge/start_edge.py                           │
                                                           ▼
电脑 A: run_cloud.py -> server.py -> Web 页面/API/检测与存储
```

职责边界：

- `start_edge.py` 只负责配置下发、启动、监控、退出和按策略重启子进程，不直接实现任何领域协议。
- 各 Jetson 子进程只负责本领域的数据采集、标准化和上传。
- `run_cloud.py` 是电脑 A 的唯一入口；它启动的 `server.py` 统一提供 API、WebSocket、页面、检测和控制接口。
- BMS 报文产生器、BLE-to-UDP 网关等属于外部数据源或调试工具，不作为电脑 A 后端必需进程，也不由 `run_cloud.py` 管理。

## 3. 分支融合原则

建议从最新 `main` 新建专用集成分支：

```bash
git fetch --all --prune
git switch main
git pull --ff-only
git switch -c integration/all-domains
```

不要依次执行 `git merge wifi bluetooth body cockpit`。这些分支都修改了 `server.py`、前端文件或依赖文件，部分分支还删除主线文件，整体合并容易把视频和 GPS 功能回退。正确方式是：以 `main` 文件为底稿，把各域功能按逻辑块迁入，再针对最终架构重构。

### 3.1 `video` 分支

`video` 的有效功能已经进入 `main`，不再重复合并。以下内容视为受保护基线：

- `jetson_edge/start_edge.py`
- `jetson_edge/navigation_gps.py`
- `jetson_edge/run_vehicle_jetson.py`
- `jetson_edge/h264_vehicle_agent.py`
- `jetson_edge/stream_protocol.py`
- `server.py` 中视频接入、fMP4 分发和 GPS/指标处理逻辑
- `static/index.html`、`static/app.js`、`static/styles.css` 中智能驾驶域的视频、地图和图表代码

融合期间只允许在 `start_edge.py` 的组件注册区域追加新子进程，不修改现有摄像头和 GPS 子进程的命令、参数、环境变量及重启行为。

### 3.2 `wifi` 分支

选择性迁移：

- 云端 WiFi 接口、数据模型、状态存储和 Web 展示；
- `wifi_ids.py` 及其测试；
- `jetson_edge/WiFi_Module.py` 的收包和上传能力；
- WiFi 链路文档。

整改要求：

- 建议统一命名为 `jetson_edge/wifi_edge_agent.py`；如果暂时保留 `WiFi_Module.py`，也只能由 `start_edge.py` 启动。
- UDP 默认监听端口为 `6001`，ESP32 时间同步目标由环境变量配置。
- 云端地址、车辆编号和令牌不得硬编码。
- `bms_wifi_sender.py` 是 BMS/链路模拟工具，放在 `tools/` 或测试目录，不注册为 Jetson 常驻子进程。
- WiFi 页面应增量加入车联网域，不能用 WiFi 分支页面整体覆盖蓝牙或主线页面。

### 3.3 `bluetooth` 分支

选择性迁移：

- `Bluetooth_Module_Board.py` 中 UDP 接收、蓝牙数据标准化和云端上传逻辑；
- S32K344 端示例代码和协议说明；
- 需要的云端蓝牙数据接口和页面逻辑。

整改要求：

- 形成 `jetson_edge/bluetooth_edge_agent.py`，默认监听 UDP `5000`。
- 删除代码中的旧公网地址、固定车辆编号和固定令牌，全部改读统一环境变量。
- `ble_to_udp_gateway.py` 是连接 BLE 设备并向 Jetson 转发 UDP 的网关工具；若它运行在独立电脑上，应放在 `tools/bluetooth/`，不注册到 Jetson。只有确认 BLE 适配器就在 Jetson 上时，才能另行改造成 Jetson 子进程。
- 若要实现云端到蓝牙设备的控制下发，必须先补齐并测试 `control/next` 与 ACK API；没有这些接口时，第一阶段只融合上行数据，不能假设控制链路已经可用。

### 3.4 `body` 分支

禁止整体合并该分支。该分支包含删除视频、GPS、WiFi、蓝牙文件以及让 Jetson 启动云端服务的改动，与目标架构冲突。

只迁移以下业务能力：

- `Body_Module_Board.py`、`Body_Module_Ethernet.py` 中车身报文解析、状态上传、控制轮询和时间同步逻辑；
- `server.py` 中车身域 API、数据模型和控制队列逻辑；
- 车身域前端面板；
- `tests/test_body_control.py`、`tests/test_body_ethernet.py` 等有效测试。

整改要求：

- 将协议解析提取为 `jetson_edge/body_protocol.py`，将运行入口整理为 `jetson_edge/body_edge_agent.py`。
- 数据口默认 UDP `7100`，控制/校时目标默认 UDP `7101`，均可由环境变量覆盖。
- `body_edge_agent.py` 不导入也不启动 `run_cloud.py` 或 `server.py`。
- 不迁移任何 Jetson `.sh` 脚本，不增加 systemd 配置。
- 车身域后端代码并入现有 `server.py`，由电脑 A 上的 `run_cloud.py` 一次性启动。

### 3.5 `cockpit` 分支

选择性迁移：

- `hmi_comparison.py`；
- 座舱比对 API、状态接口和页面逻辑；
- `static/reference-library/` 中确实需要的基准图像。

该分支目前主要是云端和浏览器端功能，没有完整的 Jetson 上传进程。需要新增 `jetson_edge/cockpit_edge_agent.py`：

- 从独立配置的 RTSP、V4L2 或文件源抓取座舱/HMI 画面；
- 按配置频率上传到座舱接口；
- 不复用、不修改智能驾驶域摄像头的进程、WebSocket、DOM 元素或缓存；
- 图像比对仍在电脑 A 的 `hmi_comparison.py` 中执行，Jetson 只负责采集和上传；
- 未配置座舱画面源时保持禁用，避免无限重启。

如果继续使用 `/api/vehicles/{vehicle_id}/cockpit/compare` 作为上传接口，必须为其补齐与其他上行接口一致的 Bearer Token 校验和请求大小限制。

## 4. Jetson 目录目标结构

```text
jetson_edge/
├── start_edge.py                 # 唯一启动入口/进程管理器
├── navigation_gps.py             # 保持 main 行为
├── run_vehicle_jetson.py         # 保持 main 行为
├── h264_vehicle_agent.py         # 保持 main 行为
├── stream_protocol.py            # 保持 main 行为
├── wifi_edge_agent.py            # WiFi UDP 接收与上传
├── bluetooth_edge_agent.py       # 蓝牙 UDP 接收与上传
├── body_edge_agent.py            # 车身采集、上传及控制轮询
├── body_protocol.py              # 纯协议解析/组包
└── cockpit_edge_agent.py         # 座舱画面采集与上传
```

领域协议文件不得反向依赖 `start_edge.py`；每个 agent 都应可以单独运行以便调试，但生产运行统一由 `start_edge.py` 拉起。

## 5. `start_edge.py` 统一子进程设计

建议把每个进程声明为组件配置，不在主线程中直接调用模块函数：

```python
components = [
    # main 中已有定义保持原样
    gps_component,
    camera_vhc001_component,
    camera_vhc002_component,

    # 新增领域
    wifi_component,
    bluetooth_component,
    body_component,
    cockpit_component,
]
```

每个组件至少包含：

- 唯一名称；
- Python 脚本绝对路径；
- 是否启用；
- 独立环境变量；
- 日志标识；
- 退出码、连续失败次数和重启退避时间。

推荐启用变量：

```dotenv
GPS_ENABLED=1
CAMERA_VHC001_ENABLED=1
CAMERA_VHC002_ENABLED=1
WIFI_ENABLED=1
BLUETOOTH_ENABLED=0
BODY_ENABLED=0
COCKPIT_ENABLED=0
```

蓝牙、车身和座舱在对应硬件尚未接入时默认禁用；部署完成后只需改为 `1`，不用修改代码。

进程管理要求：

1. 使用当前 Python 解释器启动子进程，工作目录固定为 `jetson_edge/`。
2. 一个子进程退出后只重启该进程，不重启其他领域。
3. 使用有限指数退避，防止硬件缺失时产生重启风暴。
4. 收到 `SIGINT`/`SIGTERM` 时先通知全部子进程，超时后再强制结束。
5. 子进程日志带组件名前缀，便于定位故障。
6. 不把访问令牌放入命令行参数，统一通过环境变量传递。
7. 增加配置校验：启用组件缺少必要地址、端口或设备路径时，明确报错并跳过该组件。

## 6. 统一配置

`start_edge.py` 负责读取公共配置，并传给各子进程：

```dotenv
CLOUD_WS_URL=wss://<公网域名>
CLOUD_HTTP_URL=https://<公网域名>
VEHICLE_ID=VHC001
VEHICLE_TOKEN=<仅保存在部署环境中的令牌>
```

规则如下：

- 视频 WebSocket 使用 `CLOUD_WS_URL`。
- WiFi、蓝牙、车身和座舱 HTTP API 使用 `CLOUD_HTTP_URL`。
- 公网代理地址变化时只改环境配置，不修改每个 agent 源码。
- `CLOUD_HTTP_URL` 使用 `http://` 或 `https://`，不能写成 `ws://`；`CLOUD_WS_URL` 使用 `ws://` 或 `wss://`。
- 若当前公网域名为 `15151e1e.r7.nas.cpolar.cn`，则生产配置应根据代理是否启用 TLS 设置为：

  ```dotenv
  CLOUD_HTTP_URL=https://15151e1e.r7.nas.cpolar.cn
  CLOUD_WS_URL=wss://15151e1e.r7.nas.cpolar.cn
  ```

- 本地联调仍可分别使用局域网 HTTP/WS 地址，不删除 README 中原有同一局域网说明。

各领域专属配置示例：

```dotenv
WIFI_UDP_HOST=0.0.0.0
WIFI_UDP_PORT=6001
ESP32_BRIDGE_HOST=192.168.3.1
ESP32_BRIDGE_PORT=8080

BLUETOOTH_UDP_HOST=0.0.0.0
BLUETOOTH_UDP_PORT=5000

BODY_TRANSPORT=udp
BODY_UDP_HOST=0.0.0.0
BODY_STATUS_PORT=7100
BODY_BOARD_HOST=192.168.4.31
BODY_CONTROL_PORT=7101

COCKPIT_SOURCE=<rtsp/v4l2/file source>
COCKPIT_UPLOAD_INTERVAL_SECONDS=5
```

## 7. 云端 API 和页面融合

电脑 A 上只执行 `run_cloud.py`，因此所有云端领域能力必须最终汇总到 `server.py` 或由它导入的无副作用模块中。被导入模块不能自行启动 Uvicorn，也不能在导入阶段创建常驻线程。

建议的上行接口如下：

| 领域 | Jetson 上行接口 | 数据来源 |
| --- | --- | --- |
| 视频/GPS | 保持 `main` 现有 fMP4 WebSocket 和遥测协议 | 摄像头、GPS |
| WiFi | `POST /api/vehicles/{vehicle_id}/wifi` | UDP 6001 |
| 蓝牙 | `POST /api/vehicles/{vehicle_id}/bluetooth` | UDP 5000 |
| 车身 | `POST /api/vehicles/{vehicle_id}/domains/body` | 串口或 UDP 7100 |
| 座舱 | `POST /api/vehicles/{vehicle_id}/cockpit/compare` 或专用帧上传接口 | 独立座舱画面源 |

所有设备上行接口使用统一认证、错误格式、时间戳规则和车辆编号校验。控制类接口还应具有命令 ID、幂等处理、超时和 ACK，避免重复执行。

前端融合规则：

- 智能驾驶域的 HTML、JavaScript 和 CSS 保持 `main` 行为，不整体替换。
- WiFi 与蓝牙作为车联网域内彼此独立的卡片/面板共存。
- 车身和座舱分别使用独立页面区域。
- 新增 DOM ID、JavaScript 函数、CSS 类均加领域前缀，避免覆盖现有全局名称。
- 不重复创建全局 WebSocket 或页面定时器；页面离开时清理本领域资源。
- 合并 `static/app.js` 和 `static/styles.css` 时按函数、选择器逐段处理，不接受冲突解决后的整文件覆盖。

## 8. 依赖管理

电脑 A 的 `requirements.txt` 取所有云端能力的并集。座舱图像比对优先使用服务器版本依赖（例如 `opencv-python-headless`、`numpy`），避免引入桌面 GUI 依赖。

Jetson 依赖单独维护，例如 `jetson_edge/requirements.txt`：

- 视频/GPS 依赖保持 `main` 版本；
- WiFi UDP、HTTP 上传优先使用标准库；
- 车身串口需要 `pyserial`；
- BLE 网关不在 Jetson 时，不给 Jetson 安装 `bleak`；
- 座舱抓图优先使用设备已有的 GStreamer/FFmpeg，避免仅为上传一帧而引入完整桌面 OpenCV。

## 9. 推荐实施顺序

1. 从最新 `main` 创建集成分支并记录基线提交号。
2. 建立智能驾驶域保护清单和回归测试，先确认视频与 GPS 基线通过。
3. 重构 `start_edge.py` 的组件注册机制，但保持原 GPS/摄像头组件定义不变。
4. 按 WiFi、蓝牙、车身、座舱顺序逐个迁移；每次只融合一个领域并单独提交。
5. 每个领域先完成云端接口和单元测试，再完成 Jetson agent，最后接入页面。
6. 合并公共 `requirements.txt`、`.env.example` 和 README，删除重复配置但保留局域网与公网两种说明。
7. 执行全量自动化测试和真机链路测试。

建议提交粒度：

```text
chore(edge): add component registry without changing video and gps
feat(wifi): integrate wifi cloud and edge agent
feat(bluetooth): integrate bluetooth cloud and edge agent
feat(body): port body protocol without jetson cloud runtime
feat(cockpit): add cockpit cloud comparison and edge uploader
test(integration): protect adas video and gps behavior
docs: document single-entry cloud and edge deployment
```

## 10. 验收清单

### 10.1 静态检查

- `git diff main -- jetson_edge/navigation_gps.py jetson_edge/run_vehicle_jetson.py jetson_edge/h264_vehicle_agent.py jetson_edge/stream_protocol.py` 无非预期修改。
- `main` 中智能驾驶域页面片段、视频播放器、地图初始化和 GPS 更新逻辑没有被删除或替换。
- 不存在 Jetson 调用 `run_cloud.py`、`server.py` 或 Uvicorn 的代码。
- `jetson_edge/` 不新增 `.sh` 和 systemd 文件。
- 代码中不存在硬编码公网域名、令牌和车辆私密配置。

### 10.2 自动化测试

- Python 文件编译检查通过，前端 JavaScript 语法检查通过。
- `wifi_ids.py`、车身协议/控制、蓝牙报文转换、座舱图像比对测试通过。
- 为 `start_edge.py` 增加组件构建测试：校验名称、启用开关、命令和环境变量，但测试时不真正启动硬件进程。
- 使用后端测试客户端验证每个上行 API 的成功、鉴权失败、非法载荷和重复请求。
- 验证单个领域进程崩溃时，摄像头、GPS 和其他进程仍持续运行。

### 10.3 真机验收

1. 电脑 A 只运行 `python run_cloud.py`，页面及所有云端 API 可用。
2. Jetson 只运行 `python3 start_edge.py`，进程列表中出现已启用的独立领域子进程。
3. 两路摄像头视频持续播放，原有延迟/码率指标正常。
4. GPS 位置持续更新，白底地图样式和交互不变。
5. WiFi、蓝牙、车身、座舱任一链路断开后，只影响本领域；恢复后能按重试策略自动恢复。
6. 公网代理更换后，只修改 `CLOUD_HTTP_URL` 和 `CLOUD_WS_URL` 即可恢复全部公网链路。

## 11. 最终判定标准

只有同时满足以下条件，才能认为融合完成：

- 集成分支以最新 `main` 为祖先，没有通过其他分支回退或删除主线功能；
- 智能驾驶域摄像头 Web 演示和 GPS 地图在功能及样式上保持不变；
- 电脑 A 的唯一入口是 `run_cloud.py`；
- Jetson 的唯一入口是 `jetson_edge/start_edge.py`；
- WiFi、蓝牙、车身、座舱分别作为独立、可启停、可监控、可重启的 Jetson 子进程运行；
- Jetson 不运行云端服务，不依赖 `.sh` 或 systemd 才能启动整套功能；
- 所有自动化与真机验收项通过。
