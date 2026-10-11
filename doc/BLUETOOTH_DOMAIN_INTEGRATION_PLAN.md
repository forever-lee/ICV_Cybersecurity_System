# 蓝牙域代码融合方案

## 1. 方案目标

本方案用于把当前不完整的蓝牙分支能力融合到最新 `main`，形成可以独立运行、测试和部署的蓝牙域完整链路。

融合后必须满足：

1. `main` 是唯一代码基线，蓝牙分支只作为功能素材来源，不直接整体合并。
2. 不改动智能驾驶域现有功能：
   - 摄像头采集和视频上传；
   - Web 实时视频演示；
   - GPS 采集、上传和地图渲染；
   - 当前白底地图样式。
3. 电脑 A 只运行：

   ```powershell
   python run_cloud.py
   ```

4. Jetson 端代码全部放在 `jetson_edge/`，唯一人工启动入口是：

   ```bash
   python3 start_edge.py
   ```

5. 蓝牙域在 Jetson 上的数据接收与上传程序，必须注册为 `start_edge.py` 管理的独立子进程。
6. Jetson 不运行 `run_cloud.py`，不增加 `.sh` 启动脚本，也不增加 systemd 服务。

## 2. 当前蓝牙分支的定位

当前蓝牙分支不是完整、可直接部署的系统，主要包含以下素材：

| 现有文件 | 当前职责 | 融合处理 |
| --- | --- | --- |
| `my_bluetooth_code/jetson_udp_cloud/Bluetooth_Module_Board.py` | 接收 UDP 蓝牙数据并通过 HTTP 上传 | 作为 Jetson agent 的原型，重构后迁入 `jetson_edge/` |
| `my_bluetooth_code/pc_ble_udp/ble_to_udp_gateway.py` | 电脑连接 BLE 设备，把 Notify 数据转发为 UDP | 保留为外部 BLE 网关工具，不默认放入 Jetson 运行链路 |
| `my_bluetooth_code/s32k344_code/S32K344_main.c` | S32K344 蓝牙侧示例代码 | 作为板端参考代码保留，不参与 Python 进程管理 |

当前代码不能直接视为完整功能，至少还存在以下缺口：

- Jetson 蓝牙上传代码没有正式纳入 `jetson_edge/start_edge.py`。
- 部分公网地址、车辆编号、令牌或 UDP 地址可能为示例值或硬编码值。
- 云端蓝牙上行 API、数据模型、状态缓存和鉴权需要以 `main` 为基线核对并补齐。
- 云端控制命令的获取、确认和超时机制尚未形成可验证的完整闭环。
- 蓝牙页面、云端 API、Jetson agent 和 BLE 网关之间缺少统一协议定义。
- 缺少覆盖协议解析、鉴权、断线恢复和进程管理的测试。

因此，本次工作应采用“提取有效逻辑并补齐缺失模块”的方式，不能把蓝牙分支直接合并后宣称完成。

## 3. 目标链路

### 3.1 推荐部署方式

```text
蓝牙设备 / S32K344
        │ BLE
        ▼
电脑或专用 BLE 网关
ble_to_udp_gateway.py
        │ UDP :5000
        ▼
Jetson
bluetooth_edge_agent.py
        │ HTTPS
        ▼
电脑 A / 公网代理
run_cloud.py -> server.py
        │
        ▼
蓝牙域 Web 页面
```

此模式适用于 BLE 适配器连接在独立电脑上的现状。BLE 网关只是数据源，不是云端后端，电脑 A 仍然只运行 `run_cloud.py`。

### 3.2 可选的 Jetson 直连 BLE 模式

如果以后确认 BLE 适配器直接安装在 Jetson 上，可以将 BLE 连接程序重构为另一个 Jetson 子进程。但这属于后续部署变体，不应与本次基础融合混在一起。

基础方案仍采用：

- BLE 网关负责 BLE 协议；
- Jetson 蓝牙 agent 负责 UDP 接收、数据校验和云端上传；
- 云端负责存储、展示、检测和控制队列。

## 4. 目标代码结构

```text
ICV_Cybersecurity_System/
├── run_cloud.py
├── server.py
├── bluetooth_protocol.py             # 云端可复用的数据模型/校验，可选
├── static/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── jetson_edge/
│   ├── start_edge.py                 # Jetson 唯一入口
│   ├── bluetooth_edge_agent.py       # UDP 接收、解析、上传、控制轮询
│   └── bluetooth_protocol.py         # 纯协议解析和组包
├── tools/
│   └── bluetooth/
│       └── ble_to_udp_gateway.py     # 外部 BLE 网关工具
├── firmware/
│   └── bluetooth_s32k344/
│       └── S32K344_main.c
└── tests/
    ├── test_bluetooth_protocol.py
    ├── test_bluetooth_api.py
    ├── test_bluetooth_control.py
    └── test_start_edge_bluetooth.py
```

如果暂时不调整原型文件目录，也应新建正式的 `jetson_edge/bluetooth_edge_agent.py`，不能让 `start_edge.py` 跨目录启动 `my_bluetooth_code` 下的实验脚本。

## 5. 分支融合方法

### 5.1 创建集成分支

从最新 `main` 创建蓝牙专用集成分支：

```bash
git fetch --all --prune
git switch main
git pull --ff-only
git switch -c integration/bluetooth-domain
```

不要执行：

```bash
git merge bluetooth
```

推荐通过查看远端蓝牙分支文件并手工迁移所需逻辑：

```bash
git diff main..origin/bluetooth --stat
git diff main..origin/bluetooth -- my_bluetooth_code
```

只有在确认某个提交仅新增独立蓝牙文件、不修改主线公共文件时，才考虑 `git cherry-pick`。涉及 `server.py`、`static/app.js`、`static/styles.css` 等公共文件的提交，应按逻辑块手工融合。

### 5.2 可以复用的内容

- `Bluetooth_Module_Board.py` 中 UDP socket 接收流程；
- 已有报文解析字段和 HTTP 上传数据结构；
- `ble_to_udp_gateway.py` 中 BLE 扫描、连接、Notify 订阅和 UDP 转发逻辑；
- S32K344 示例中的板端协议定义。

### 5.3 不应原样迁移的内容

- 硬编码公网域名、车辆编号、令牌和本机 IP；
- 调试模式下的固定测试数据和无限快速重试；
- 在模块导入时立即连接设备或启动循环的代码；
- 未鉴权的控制接口；
- 把 BLE 网关、Jetson agent 和云端服务混在同一个进程中的实现；
- 对智能驾驶域前端文件的整文件覆盖。

## 6. 蓝牙协议先行

在接入云端之前，先冻结 BLE 到 UDP、UDP 到云端的字段定义。建议使用带版本号的 JSON 上行格式：

```json
{
  "schema_version": 1,
  "vehicle_id": "VHC001",
  "device_id": "BLE01",
  "sequence": 1024,
  "timestamp_ms": 1760000000000,
  "message_type": "telemetry",
  "data": {
    "raw_hex": "01020304"
  }
}
```

最低要求：

- `sequence` 用于识别丢包和重复包；
- `timestamp_ms` 明确使用 Unix 毫秒时间；
- `schema_version` 支持后续协议升级；
- `device_id` 区分同一车辆的多个蓝牙节点；
- 原始数据和解析后数据可以并存，便于定位协议问题；
- 明确最大 UDP 报文长度和最大 HTTP 请求大小；
- 非法 UTF-8、非法 JSON、字段缺失和超长报文必须丢弃并计数，不能导致进程退出。

如果 S32K344 当前发送二进制帧，应在 `bluetooth_protocol.py` 中集中定义：

- 帧头和帧尾；
- 协议版本；
- 消息类型；
- 长度字段；
- 序号；
- CRC/校验算法；
- 多字节字段的大小端；
- 每个状态位和控制位含义。

不要把二进制字段偏移散落在 BLE 网关、Jetson agent 和云端三个位置。

## 7. Jetson 蓝牙独立子进程

新增 `jetson_edge/bluetooth_edge_agent.py`，职责限定为：

1. 监听 BLE 网关发送的 UDP 报文；
2. 校验来源、长度、格式、序号和时间戳；
3. 转换为统一蓝牙域上行数据；
4. 使用 HTTPS 上传电脑 A 的云端 API；
5. 按需轮询蓝牙控制命令并向 BLE 网关发送 UDP 控制帧；
6. 输出健康状态、错误计数和结构化日志；
7. 网络异常时限速重试，不阻塞其他领域进程。

建议配置：

```dotenv
BLUETOOTH_ENABLED=1
BLUETOOTH_UDP_HOST=0.0.0.0
BLUETOOTH_UDP_PORT=5000
BLUETOOTH_GATEWAY_HOST=192.168.4.10
BLUETOOTH_GATEWAY_CONTROL_PORT=5001
BLUETOOTH_HTTP_TIMEOUT_SECONDS=5
BLUETOOTH_CONTROL_POLL_SECONDS=1

CLOUD_HTTP_URL=https://15151e1e.r7.nas.cpolar.cn
VEHICLE_ID=VHC001
VEHICLE_TOKEN=<部署时设置>
```

要求：

- `CLOUD_HTTP_URL` 必须使用 `http://` 或 `https://`，不能填写 `ws://`。
- 不在源码中保存真实令牌。
- HTTP 请求携带 `Authorization: Bearer <token>`。
- 连接失败使用有限指数退避并设置最大间隔。
- 上行缓存必须有数量或磁盘空间上限；第一阶段可以只做有限内存队列。
- 单条坏报文只记录和丢弃，不能使进程退出。

## 8. 注册到 `start_edge.py`

蓝牙 agent 以独立组件追加到现有组件列表，不修改摄像头和 GPS 组件：

```python
bluetooth_component = {
    "name": "bluetooth",
    "enabled_env": "BLUETOOTH_ENABLED",
    "script": "bluetooth_edge_agent.py",
    "restart": True,
}
```

具体写法应沿用 `main` 中 `start_edge.py` 已有的组件模型。需要保证：

- 蓝牙禁用时不启动该进程；
- 蓝牙配置缺失时给出清晰错误，但摄像头和 GPS 继续运行；
- 蓝牙进程退出时只重启蓝牙进程；
- 连续失败使用退避，避免硬件未连接时刷屏；
- `start_edge.py` 退出时同时终止蓝牙子进程；
- 日志统一带 `[bluetooth]` 前缀；
- 不通过 Shell 脚本间接启动。

Jetson 最终仍只需要：

```bash
cd jetson_edge
python3 start_edge.py
```

## 9. 云端需要补齐的能力

电脑 A 仍然只启动 `run_cloud.py`。蓝牙代码必须作为现有 `server.py` 的路由和业务模块运行，不能再单独启动第二个后端服务。

### 9.1 数据上行接口

建议保留统一接口：

```text
POST /api/vehicles/{vehicle_id}/bluetooth
```

接口必须完成：

- Bearer Token 校验；
- URL 中 `vehicle_id` 与令牌权限、请求体车辆编号的一致性校验；
- JSON schema、报文大小和时间戳校验；
- 按 `device_id + sequence` 去重；
- 更新车辆蓝牙域最新状态；
- 返回明确的成功或错误结构。

### 9.2 页面查询接口

根据现有前端模式提供蓝牙最新状态和历史记录查询，避免浏览器直接读取 Jetson：

```text
GET /api/vehicles/{vehicle_id}/bluetooth
GET /api/vehicles/{vehicle_id}/bluetooth/history
```

历史记录可以在第一阶段限制条数并保存在现有后端存储机制中，不应为了蓝牙单独再启动数据库服务。

### 9.3 控制闭环

只有补齐以下闭环后，才能声明蓝牙控制功能完成：

```text
页面创建控制命令
  -> 云端队列
  -> Jetson 获取 next
  -> Jetson UDP 发给 BLE 网关
  -> BLE 网关写入 GATT
  -> 设备响应
  -> BLE 网关 UDP 回传
  -> Jetson ACK 云端
  -> 页面显示最终状态
```

建议接口：

```text
POST /api/vehicles/{vehicle_id}/bluetooth/control
GET  /api/vehicles/{vehicle_id}/bluetooth/control/next
POST /api/vehicles/{vehicle_id}/bluetooth/control/{command_id}/ack
```

控制命令至少具有：

- `command_id`；
- 创建时间和过期时间；
- 目标 `device_id`；
- 命令类型和参数；
- `pending`、`delivered`、`succeeded`、`failed`、`expired` 状态；
- 幂等处理规则；
- 失败原因和设备响应。

第一阶段如果只完成数据上行，应明确标注“控制功能未启用”，不要保留会让页面误以为控制成功的模拟 ACK。

## 10. BLE 网关整改

`ble_to_udp_gateway.py` 保留为独立工具时，应完成以下整改：

- BLE 地址、服务 UUID、Characteristic UUID、Jetson IP 和端口全部配置化；
- BLE 断线后自动扫描和重连；
- Notify 回调只负责快速入队，网络发送在独立任务中完成；
- 为 UDP 包增加设备编号、序号和时间戳；
- 如启用下行控制，监听独立 UDP 控制端口并通过 GATT Write 写入设备；
- 仅在设备真实响应后生成成功 ACK；
- 不连接云端，不持有云端车辆令牌；
- 日志中不输出敏感数据。

如果 BLE 网关运行在电脑 A，同一台电脑仍可只把 `run_cloud.py` 视为“后端入口”，但 BLE 网关属于硬件接入工具，需要单独运行。若要求电脑 A 绝对只能存在一个 Python 进程，则 BLE 网关必须迁移到另一台电脑或 Jetson；不能把它静默塞进 `run_cloud.py`。

## 11. 前端融合规则

蓝牙页面应增量并入车联网域，不能用蓝牙分支的公共前端文件覆盖 `main`：

- 保持智能驾驶域视频、地图、图表相关 HTML 和 JavaScript 不变；
- 蓝牙与 WiFi 使用不同 DOM ID，例如统一增加 `bluetooth-` 前缀；
- 蓝牙 CSS 选择器使用 `.bluetooth-*` 命名空间；
- 不复用或覆盖视频 WebSocket 全局变量；
- 页面切换时停止蓝牙轮询或定时器，返回页面后再恢复；
- 控制按钮必须显示 `pending`、成功、失败和超时，不能点击后立即假定成功；
- 后端没有实现控制闭环前，控制区保持禁用并说明原因。

## 12. 分阶段实施

### 阶段一：上行最小闭环

- 冻结蓝牙报文格式；
- 新建 `bluetooth_protocol.py`；
- 新建 `jetson_edge/bluetooth_edge_agent.py`；
- 补齐云端 `POST /bluetooth`；
- 注册 `start_edge.py` 独立子进程；
- 完成最新状态页面。

完成标志：BLE 数据能够经 UDP 到达 Jetson，再上传电脑 A 并在页面显示；中断蓝牙链路不影响视频和 GPS。

### 阶段二：可靠性和安全

- 增加鉴权、去重、输入大小限制和错误计数；
- 完成 BLE、UDP、HTTP 断线恢复；
- 增加有限缓存和超时；
- 补齐单元测试和接口测试。

### 阶段三：控制闭环

- 新增控制队列接口；
- Jetson 实现命令领取、UDP 下发和 ACK；
- BLE 网关实现 GATT Write 与设备响应关联；
- 页面展示真实命令状态。

未完成第三阶段时，只能称为“蓝牙数据采集与展示已融合”，不能称为“蓝牙双向控制已完成”。

## 13. 测试与验收

### 13.1 自动化测试

- 协议测试：正常帧、截断帧、超长帧、错误校验、重复序号和未知版本。
- API 测试：正确令牌、错误令牌、车辆不匹配、非法载荷和重复数据。
- 控制测试：命令领取、重复领取、ACK、失败、超时和过期。
- 进程测试：校验 `start_edge.py` 能正确生成蓝牙组件命令和环境变量，测试时不连接真实硬件。
- 回归测试：智能驾驶域视频 WebSocket、视频播放、GPS 更新和白底地图均保持原状。

### 13.2 真机验收

1. 电脑 A 只启动 `python run_cloud.py`，蓝牙 API 和页面可访问。
2. Jetson 只启动 `python3 start_edge.py`，能看到独立蓝牙子进程。
3. BLE 设备数据经网关、UDP、Jetson、HTTPS 到达云端页面。
4. 停止 BLE 网关后，蓝牙状态显示离线，视频和 GPS 持续正常。
5. 恢复 BLE 网关后，链路自动恢复，不需要重启 `start_edge.py`。
6. 杀掉蓝牙 agent 后，`start_edge.py` 只重启蓝牙进程。
7. 更换公网映射地址后，只修改 `CLOUD_HTTP_URL` 即可恢复蓝牙上行。
8. 若启用控制，页面只有在设备真实 ACK 后才显示成功。

## 14. 建议提交顺序

```text
feat(bluetooth): add shared bluetooth protocol parser
feat(bluetooth): add authenticated cloud telemetry API
feat(edge): add bluetooth UDP upload agent
feat(edge): supervise bluetooth agent from start_edge
feat(bluetooth-ui): add namespaced bluetooth status panel
test(bluetooth): cover protocol api and process lifecycle
feat(bluetooth-control): add command queue and device ack loop
docs(bluetooth): document gateway edge and cloud deployment
```

## 15. 完成判定

蓝牙域只有同时满足以下条件才算融合完成：

- 代码基于最新 `main`，没有整体合并蓝牙分支导致公共文件回退；
- 视频 Web 演示、GPS 地图和白底地图样式没有变化；
- 电脑 A 后端只由 `run_cloud.py` 启动；
- Jetson 只由 `jetson_edge/start_edge.py` 启动；
- 蓝牙上行程序是受 `start_edge.py` 管理的独立子进程；
- 公网地址、车辆编号、设备地址和令牌全部配置化；
- 蓝牙链路故障不会影响视频、GPS 或其他领域；
- 上行链路通过自动化和真机测试；
- 只有控制下发及真实设备 ACK 全部通过后，才标记双向控制完成。
