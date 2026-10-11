# Body 分支 Jetson 纯 Python 边缘端整改方案

## 1. 设计约束

整改后的 Jetson 代码必须满足：

- Jetson 目录中不包含任何 `.sh` 文件；
- Jetson 不运行 `run_cloud.py`、`server.py` 或 Uvicorn；
- 电脑 A 负责云端服务、Web 页面和公网映射；
- Jetson 只负责 S32K344 数据接收、协议解析、控制下发、对时和云端上传；
- Jetson 只有一个人工启动入口：`python3 start_edge.py`；
- 子进程监管、日志、重启和优雅停止全部由 Python 实现；
- 后台运行方式与 `video` 分支一致，直接使用 `nohup python3 start_edge.py`；
- 云端地址和 Token 通过启动环境变量提供。

## 2. 目标架构

```text
S32K344 192.168.4.31
  ├─ UDP 7100：车身状态上报
  └─ UDP 7101：控制命令与时钟同步
            │
            ▼
Jetson eth0 192.168.4.100
  └─ jetson_edge/start_edge.py
       └─ body_edge_agent.py
            ├─ 接收并校验 S32K344 二进制报文
            ├─ 轮询电脑 A 的控制命令
            ├─ 向板端发送控制和对时报文
            └─ HTTP/HTTPS 上传车身状态
                         │
                         ▼
电脑 A / cpolar 公网入口
  └─ run_cloud.py:8001
       └─ server.py
            └─ 车身域页面
```

职责边界：

| 设备 | 运行内容 | 不运行内容 |
|---|---|---|
| Jetson | `start_edge.py`、`body_edge_agent.py` | `run_cloud.py`、Uvicorn、浏览器 |
| 电脑 A | `run_cloud.py`、`server.py`、Web 页面、cpolar | Jetson UDP 适配器 |
| S32K344 | 状态发送、命令接收、板端时钟 | HTTP/Web 服务 |

## 3. 目标目录结构

建议将原有大写 `Jetson/` 目录统一为与 `video` 分支一致的 `jetson_edge/`：

```text
jetson_edge/
├─ start_edge.py
├─ body_edge_agent.py
├─ body_protocol.py
├─ requirements_jetson.txt
├─ README.md
└─ .gitignore
```

运行时生成但不提交 Git：

```text
jetson_edge/logs/
jetson_edge/edge_supervisor.pid
jetson_edge/edge_supervisor.lock
jetson_edge/__pycache__/
```

原 `Jetson/*.sh` 不再属于目标架构。实施时应按照仓库删除规则将它们移动到回收站，而不是直接永久删除。

## 4. Python 模块职责

### 4.1 `start_edge.py`

它是 Jetson 唯一入口，只负责配置、检查和进程监管，不处理业务报文。

主要职责：

- 读取并校验环境变量；
- 检查 `body_edge_agent.py` 是否存在；
- 检查 Jetson 是否具有 Python 运行环境；
- 可选检查 `eth0` 是否拥有期望地址；
- 将车身域适配器注册为独立子进程；
- 创建独立进程组；
- 将日志写入 `logs/body.log`；
- 日志达到上限后轮转为 `body.log.1`；
- 业务子进程异常退出后等待 3 秒自动重启；
- 使用文件锁阻止重复启动；
- 响应 `SIGINT` 和 `SIGTERM`；
- 正常退出时终止整个业务子进程组；
- 写入并清理 `edge_supervisor.pid`。

禁止行为：

- 不导入 `run_cloud`；
- 不启动 `run_cloud.py`；
- 不检查本机 `127.0.0.1:8001`；
- 不打开浏览器；
- 不保存网页访问口令；
- 不终止电脑 A 的任何进程。

### 4.2 `body_edge_agent.py`

该文件承接当前 `Body_Module_Board.py` 的 Jetson 业务能力：

- 监听 UDP 7100；
- 只接受目标 S32K344 地址的数据；
- 解码 16/25 字节状态包；
- 校验协议头、版本、消息类型和校验和；
- 解析车门、灯光、中控锁及 CAN 状态；
- 每 5 秒向 S32K344 UDP 7101 发送对时报文；
- 轮询电脑 A 的车身域控制接口；
- 将启停命令通过 UDP 7101 下发给 S32K344；
- 将车身状态通过 HTTP POST 上传电脑 A；
- 网络失败时记录错误并继续接收后续报文。

建议将串口兼容模式保留为参数：

```bash
python3 body_edge_agent.py --mode serial
```

默认正式模式为：

```bash
python3 body_edge_agent.py --mode udp
```

### 4.3 `body_protocol.py`

从原 `Body_Module_Board.py` 抽取无副作用的协议代码：

- `decode_serial_packet()`；
- `build_serial_command()`；
- `build_time_sync_packet()`；
- `serial_payload()`；
- `ethernet_payload()`；
- 车身信号归一化函数；
- CAN/PDU 元数据生成函数。

该模块不得创建 Socket、启动循环或读取运行环境，使其能够被单元测试直接导入。

## 5. 配置设计

### 5.1 必需环境变量

```text
CLOUD_HTTP_URL
VEHICLE_INGEST_TOKEN
```

`start_edge.py` 启动时必须校验：

```python
CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "").rstrip("/")
INGEST_TOKEN = os.getenv("VEHICLE_INGEST_TOKEN", "")

if not CLOUD_HTTP_URL:
    raise SystemExit("请设置 CLOUD_HTTP_URL")

if not INGEST_TOKEN:
    raise SystemExit("请设置 VEHICLE_INGEST_TOKEN")
```

不得从 `run_cloud.py` 导入 Token。

### 5.2 可选环境变量

```text
BODY_INPUT_MODE=udp
BODY_UDP_HOST=0.0.0.0
BODY_UDP_PORT=7100
BODY_BOARD_HOST=192.168.4.31
BODY_CONTROL_PORT=7101
VEHICLE_ID=VHC-001
BODY_SERIAL_PORT=/dev/ttyUSB0
BODY_SERIAL_BAUDRATE=115200
BODY_RESTART_DELAY_SECONDS=3
BODY_LOG_MAX_BYTES=20971520
BODY_EXPECTED_ETH0_ADDRESS=192.168.4.100/24
```

### 5.3 云端地址示例

局域网访问电脑 A：

```bash
export CLOUD_HTTP_URL='http://192.168.2.136:8001'
```

公网 HTTP 映射：

```bash
export CLOUD_HTTP_URL='http://15151e1e.r7.nas.cpolar.cn'
```

公网 HTTPS 映射：

```bash
export CLOUD_HTTP_URL='https://15151e1e.r7.nas.cpolar.cn'
```

车身域通过 HTTP POST 通信，不需要 `CLOUD_WS_URL`。

## 6. `start_edge.py` 组件设计

监督器只注册一个业务组件：

```python
Component(
    name="body",
    command=[
        sys.executable,
        "-u",
        str(BASE_DIR / "body_edge_agent.py"),
        "--mode", os.getenv("BODY_INPUT_MODE", "udp"),
        "--host", os.getenv("BODY_UDP_HOST", "0.0.0.0"),
        "--port", os.getenv("BODY_UDP_PORT", "7100"),
        "--board-host", os.getenv("BODY_BOARD_HOST", "192.168.4.31"),
        "--control-port", os.getenv("BODY_CONTROL_PORT", "7101"),
        "--cloud", CLOUD_HTTP_URL,
        "--vehicle-id", os.getenv("VEHICLE_ID", "VHC-001"),
        "--token", INGEST_TOKEN,
    ],
    environment={},
)
```

Token 出现在子进程命令行可能被 `ps` 查看。更安全的实现是通过子进程环境传递 Token，并让 `body_edge_agent.py` 从 `VEHICLE_INGEST_TOKEN` 读取；命令行中不传 `--token`。推荐采用环境变量方案。

## 7. 启动与停止方式

### 7.1 前台运行

```bash
cd jetson_edge
export CLOUD_HTTP_URL='http://15151e1e.r7.nas.cpolar.cn'
export VEHICLE_INGEST_TOKEN='与电脑A一致的Token'
python3 start_edge.py
```

停止：

```text
按 Ctrl+C
```

监督器收到信号后停止 `body_edge_agent.py` 并清理 PID 文件。

### 7.2 后台运行

与 `video` 分支保持一致，直接使用 `nohup` 启动 Python 入口：

```bash
cd jetson_edge
nohup python3 start_edge.py > edge_supervisor.log 2>&1 &
echo $! > edge_supervisor.pid
```

查看监督器输出：

```bash
tail -f edge_supervisor.log
```

停止后台进程时向监督器发送 `SIGTERM`，由监督器统一停止业务子进程：

```bash
kill -TERM "$(cat edge_supervisor.pid)"
```

该方式只使用命令行，不需要仓库中存在 Shell 脚本或系统服务文件。

## 8. Jetson 网络配置

应用程序不应自行修改系统网络。`start_edge.py` 只检查网络，不执行需要 root 权限的配置。

可使用 NetworkManager 一次性配置 `eth0`：

```bash
sudo nmcli connection add type ethernet ifname eth0 con-name vshield-body \
  ipv4.method manual ipv4.addresses 192.168.4.100/24 \
  ipv4.never-default yes ipv6.method disabled
sudo nmcli connection up vshield-body
```

关键要求：

- Jetson `eth0`：`192.168.4.100/24`；
- S32K344：`192.168.4.31/24`；
- `eth0` 不设置默认网关；
- Jetson 的 WiFi 或其他上网接口保留默认路由；
- Jetson 能访问 `CLOUD_HTTP_URL`。

## 9. 原有 Shell 文件处理

以下文件不再保留在 Jetson 目标目录：

```text
Jetson/start_body_demo.sh
Jetson/start_body_ethernet.sh
Jetson/start_vshield_body.sh
Jetson/stop_body_demo.sh
Jetson/stop_vshield_body.sh
Jetson/install_vshield_launcher.sh
```

实施时必须遵循仓库规则：将这些文件移动到系统回收站；如果移动失败，立即停止并询问用户，不得永久删除。

删除前应确认以下替代关系：

| 原 Shell 脚本 | Python 替代方案 |
|---|---|
| `start_vshield_body.sh` | `python3 jetson_edge/start_edge.py` |
| `stop_vshield_body.sh` | `Ctrl+C` 或向监督器发送 `SIGTERM` |
| `start_body_ethernet.sh` | `python3 body_edge_agent.py --mode udp` |
| `start_body_demo.sh` | `python3 body_edge_agent.py --mode serial` |
| `stop_body_demo.sh` | `Ctrl+C` 或向监督器发送 `SIGTERM` |
| `install_vshield_launcher.sh` | README 中的 `nmcli` 配置命令 |

## 10. 电脑 A 部署

电脑 A 保持云端职责：

```bash
python run_cloud.py
```

电脑 A 提供：

- `/healthz`；
- `/api/vehicles/{vehicle_id}/domains/body`；
- `/api/vehicles/{vehicle_id}/domains/body/control`；
- 车身域监控与控制页面；
- 可选 cpolar 公网映射。

Jetson 启动前可以验证：

```bash
curl -i "$CLOUD_HTTP_URL/healthz"
```

## 11. README 重写要求

新的 `jetson_edge/README.md` 应只描述 Jetson 边缘端：

1. Jetson 不运行云端；
2. 唯一入口是 `python3 start_edge.py`；
3. 配置来自环境变量；
4. 前台使用 `Ctrl+C` 停止；
5. 后台使用 `nohup python3 start_edge.py`；
6. `eth0` 网络通过 NetworkManager 配置；
7. 日志位于 `logs/body.log`；
8. 说明 UDP 7100/7101 和 HTTP 上传链路；
9. 不出现任何 `.sh` 文件操作说明；
10. 浏览器访问电脑 A，而不是 Jetson。

根目录 README 应分别列出电脑 A 和 Jetson 的启动步骤，避免再次混淆两端职责。

## 12. 测试方案

### 12.1 静态检查

```bash
python3 -m py_compile \
  jetson_edge/start_edge.py \
  jetson_edge/body_edge_agent.py \
  jetson_edge/body_protocol.py
python3 -m pytest -q tests
```

### 12.2 文件约束检查

```bash
find jetson_edge -type f -name '*.sh'
```

预期无输出。

检查 Jetson 代码是否引用云端启动文件：

```bash
grep -R "run_cloud\|uvicorn" jetson_edge
```

除 README 中的禁止说明外，业务代码不应出现相关引用。

### 12.3 进程检查

启动后执行：

```bash
ps -ef | grep -E 'start_edge|body_edge_agent|run_cloud|uvicorn'
ss -lntup
```

预期：

- 存在 `start_edge.py`；
- 存在 `body_edge_agent.py`；
- 不存在 `run_cloud.py`；
- 不存在 Uvicorn；
- 不监听 TCP 8001；
- 监听 UDP 7100。

### 12.4 监督器检查

1. 杀掉 `body_edge_agent.py`；
2. 确认 `start_edge.py` 在约 3 秒后重新启动它；
3. 按 `Ctrl+C` 或向监督器发送 `SIGTERM`；
4. 确认监督器及业务子进程均退出；
5. 确认电脑 A 的云端服务不受影响。

### 12.5 数据链路检查

验证以下链路：

```text
S32K344 UDP 7100
  → Jetson
  → HTTP/HTTPS
  → 电脑 A
  → 车身域页面更新
```

验证控制反向链路：

```text
电脑 A 页面控制命令
  → Jetson HTTP 轮询
  → UDP 7101
  → S32K344
```

## 13. 验收标准

整改完成后必须全部满足：

1. `jetson_edge/` 中不存在 `.sh` 文件；
2. Jetson 唯一人工入口为 `python3 start_edge.py`；
3. Jetson 不运行 `run_cloud.py`、`server.py` 或 Uvicorn；
4. Jetson 不监听 TCP 8001；
5. Jetson 监听 UDP 7100；
6. Jetson 可向 UDP 7101 下发控制和对时报文；
7. 车身状态能够上传电脑 A；
8. 电脑 A 的控制命令能够到达 S32K344；
9. 业务子进程异常退出后自动重启；
10. `Ctrl+C` 和 `SIGTERM` 均能完整停止进程组；
11. Token 不出现在 Git 或公开日志中；
12. 局域网地址和公网地址均可通过环境变量切换；
13. 停止 Jetson 不影响电脑 A 的云端服务；
14. 所有现有单元测试通过。

## 14. 推荐实施顺序

1. 新建 `jetson_edge/body_protocol.py`；
2. 将协议函数从 `Body_Module_Board.py` 抽取到该模块；
3. 新建 `jetson_edge/body_edge_agent.py`；
4. 新建纯 Python 监督器 `jetson_edge/start_edge.py`；
5. 增加 `requirements_jetson.txt`、`.gitignore` 和 `README.md`；
6. 调整现有测试的导入路径并补充监督器测试；
7. 验证 Python 入口完整替代全部 Shell 脚本；
8. 将原 `Jetson/*.sh` 移动到回收站；
9. 执行静态检查、单元测试和真实硬件联调。

## 15. 最终原则

> 电脑 A 运行云端；Jetson 只运行 Python 边缘端；仓库中的 Jetson 部署目录不包含任何 Shell 脚本。
