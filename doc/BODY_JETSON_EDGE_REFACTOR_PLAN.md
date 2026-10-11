# Body 分支 Jetson 边缘端改造方案

> **已废弃：** 本版方案仍包含 Shell 启停脚本，不再作为实施依据。请使用新版纯 Python 方案：[`BODY_JETSON_PYTHON_ONLY_REFACTOR_PLAN.md`](BODY_JETSON_PYTHON_ONLY_REFACTOR_PLAN.md)。

## 1. 改造目标

参考 `video` 分支的技术架构，将云端服务和 Jetson 边缘端彻底分离：

- 电脑 A 运行 `run_cloud.py`、`server.py` 和 Web 页面；
- Jetson 只负责接收 S32K344 车身域数据、下发控制与对时命令，并把数据上传到电脑 A；
- Jetson 不启动 `run_cloud.py`，不监听 TCP 8001，也不管理浏览器；
- Jetson 使用独立入口监管车身域适配器，支持日志、异常重启和统一停止。

目标数据链路：

```text
S32K344 192.168.4.31
  │
  │ UDP 7100：状态上报
  │ UDP 7101：控制与对时
  ▼
Jetson eth0 192.168.4.100
  │
  │ Body_Module_Board.py
  │ HTTP / HTTPS
  ▼
电脑 A 或 cpolar 公网入口
  │
  │ run_cloud.py:8001
  ▼
车身域监控与控制页面
```

## 2. 当前架构存在的问题

`body` 分支当前的 `Jetson/start_vshield_body.sh` 同时承担了边缘端和云端职责：

- 在 Jetson 启动 `run_cloud.py`；
- 检查 Jetson 本机 `127.0.0.1:8001/healthz`；
- 从 `run_cloud.py` 导入 `INGEST_TOKEN`；
- 将车身域数据上传到 `http://127.0.0.1:8001`；
- 在 Jetson 打开管理页面；
- 停止脚本会同时终止 `run_cloud.py`。

其他入口也存在相同耦合：

- `Jetson/start_body_ethernet.sh` 将云端地址固定为 `http://127.0.0.1:8001`；
- `Jetson/start_body_demo.sh` 会尝试在 Jetson 启动云端，并且项目根目录定位不正确；
- `Jetson/install_vshield_launcher.sh` 创建的快捷方式仍描述为“启动云端和车身域适配器”。

上述设计与 `video` 分支“电脑 A 运行云端、Jetson 只运行边缘上传程序”的架构不一致。

## 3. 目标目录与职责

建议继续保留现有业务文件，并新增一个与 `video` 分支监督器职责相同的入口：

```text
Body_Module_Board.py            # 车身域协议解析、UDP/串口接收、控制和云端上传
Body_Module_Ethernet.py         # 以太网兼容适配器
run_cloud.py                    # 仅在电脑 A 运行
server.py                       # 仅在电脑 A 运行
Jetson/
  start_edge.py                 # Jetson 唯一推荐入口
  start_vshield_body.sh         # start_edge.py 的 Shell 包装器
  stop_vshield_body.sh          # 只停止 Jetson 边缘端进程
  start_body_ethernet.sh        # 独立调试入口
  start_body_demo.sh            # 串口兼容入口
  install_vshield_launcher.sh   # 网络与桌面快捷方式安装
  logs/                         # 运行时日志，不提交 Git
```

为便于以后与 `video`、`wifi` 等分支集成，也可以将 `Jetson/` 统一命名为 `jetson_edge/`。本次改造不要求立即迁移目录，重点是先消除 Jetson 对云端服务的依赖。

## 4. 新增 Jetson 监督入口

### 4.1 新建 `Jetson/start_edge.py`

参考 `video` 分支的 `jetson_edge/start_edge.py`，实现以下能力：

- 将 `Body_Module_Board.py --mode udp` 注册为独立子进程；
- 使用独立进程组启动；
- 输出写入 `Jetson/logs/body.log`；
- 日志达到上限后轮转为 `body.log.1`；
- 子进程异常退出后等待 3 秒自动重启；
- 使用锁文件防止重复启动；
- 响应 `SIGINT` 和 `SIGTERM`；
- 正常退出时停止全部受管子进程；
- 不启动 `run_cloud.py`，不导入 `run_cloud`。

监督器启动的业务命令等价于：

```bash
python3 -u Body_Module_Board.py \
  --mode udp \
  --host 0.0.0.0 \
  --port 7100 \
  --board-host 192.168.4.31 \
  --control-port 7101 \
  --cloud "$CLOUD_HTTP_URL" \
  --vehicle-id "$VEHICLE_ID" \
  --token "$VEHICLE_INGEST_TOKEN"
```

### 4.2 环境变量校验

监督器必须从环境变量读取云端地址和令牌：

```python
CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "").rstrip("/")
INGEST_TOKEN = os.getenv("VEHICLE_INGEST_TOKEN", "")
```

缺少关键配置时直接退出，并给出明确错误：

```python
if not CLOUD_HTTP_URL:
    raise SystemExit("请设置 CLOUD_HTTP_URL")

if not INGEST_TOKEN:
    raise SystemExit("请设置 VEHICLE_INGEST_TOKEN")
```

禁止继续通过下面的方式从云端源码读取令牌：

```bash
python -c 'import run_cloud; print(run_cloud.INGEST_TOKEN)'
```

## 5. 现有脚本修改清单

### 5.1 `Jetson/start_vshield_body.sh`

将它缩减为 `start_edge.py` 的包装器：

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_DIR}"
exec python3 "${SCRIPT_DIR}/start_edge.py"
```

需要移除：

- `CLOUD_PID_FILE`；
- `CLOUD_LOG`；
- `cloud_is_ready()`；
- `run_cloud.py` 启动逻辑；
- 从 `run_cloud.py` 读取 Token 的逻辑；
- `DASHBOARD_ACCESS_TOKEN`；
- Jetson 浏览器启动逻辑；
- Jetson 本机 8001 健康检查。

### 5.2 `Jetson/stop_vshield_body.sh`

停止脚本只允许停止：

- `Jetson/start_edge.py`；
- 由监督器启动的 `Body_Module_Board.py`。

停止脚本不得查找或终止：

```text
run_cloud.py
uvicorn
server.py
```

电脑 A 的服务生命周期不能受到 Jetson 停止操作影响。

### 5.3 `Jetson/start_body_ethernet.sh`

把固定地址：

```bash
--cloud http://127.0.0.1:8001
```

改为强制读取环境变量：

```bash
: "${CLOUD_HTTP_URL:?请设置 CLOUD_HTTP_URL}"
: "${VEHICLE_INGEST_TOKEN:?请设置 VEHICLE_INGEST_TOKEN}"
```

启动参数改为：

```bash
--cloud "${CLOUD_HTTP_URL}" \
--token "${VEHICLE_INGEST_TOKEN}"
```

该脚本只作为单独调试 `Body_Module_Ethernet.py` 的入口，不再启动或依赖本机云端服务。

### 5.4 `Jetson/start_body_demo.sh`

该脚本保留为串口兼容入口，但需要：

- 删除 `run_cloud.py` 启动逻辑；
- 删除 Jetson 本机 8001 检查；
- 从环境变量读取远程 `CLOUD_HTTP_URL`；
- 从环境变量读取 `VEHICLE_INGEST_TOKEN`；
- 修正项目根目录路径；
- 只运行 `Body_Module_Board.py --mode serial`。

如果串口方案已不再使用，则在 README 中标记为“旧串口兼容入口”，不再作为推荐启动方法。

### 5.5 `Jetson/stop_body_demo.sh`

只停止串口模式的 `Body_Module_Board.py`，不得停止 `run_cloud.py`。

### 5.6 `Jetson/install_vshield_launcher.sh`

保留以下能力：

- 将 `eth0` 配置为 `192.168.4.100/24`；
- 创建并启用网络配置服务；
- 创建启动和停止快捷方式。

快捷方式说明修改为：

```text
启动 Jetson 车身域边缘上传
停止 Jetson 车身域边缘上传
```

不得再描述为“启动云端”，也不再负责打开云端网页。

## 6. Python 业务代码调整

### 6.1 `Body_Module_Board.py`

核心协议实现可以保留，包括：

- UDP 7100 状态接收；
- UDP 7101 控制下发；
- 板端时钟同步；
- 16/25 字节二进制报文解析；
- 车门、灯光和中控锁信号转换；
- HTTP 上传；
- 控制命令轮询。

建议保留模块中的开发默认值，但正式 Jetson 入口必须显式传入远程 `--cloud` 和 `--token`，避免误用 `127.0.0.1:8001`。

也可以进一步将默认地址改为空字符串，并在 `main()` 中强制校验，从源头杜绝 Jetson 误连本机云端。

### 6.2 `Body_Module_Ethernet.py`

该文件可以继续复用 `Body_Module_Board.py` 的协议函数。需要确保正式启动时显式传入：

```text
--cloud $CLOUD_HTTP_URL
--token $VEHICLE_INGEST_TOKEN
```

## 7. 配置方案

### 7.1 电脑 A

电脑 A 运行云端服务：

```bash
python run_cloud.py
```

局域网方式：

```text
http://电脑A_IP:8001
```

公网方式：

```text
电脑 A 的 8001
  → cpolar
  → 公网 HTTP/HTTPS 地址
```

### 7.2 Jetson

局域网直连电脑 A：

```bash
export CLOUD_HTTP_URL='http://192.168.2.136:8001'
export VEHICLE_INGEST_TOKEN='与电脑A一致的Token'
python3 Jetson/start_edge.py
```

通过公网映射上传：

```bash
export CLOUD_HTTP_URL='http://15151e1e.r7.nas.cpolar.cn'
export VEHICLE_INGEST_TOKEN='与电脑A一致的Token'
python3 Jetson/start_edge.py
```

如果 cpolar 使用 HTTPS：

```bash
export CLOUD_HTTP_URL='https://15151e1e.r7.nas.cpolar.cn'
```

车身域当前使用 HTTP POST，不需要 `CLOUD_WS_URL`。

建议支持以下可选变量：

```bash
export BODY_UDP_HOST='0.0.0.0'
export BODY_UDP_PORT='7100'
export BODY_BOARD_HOST='192.168.4.31'
export BODY_CONTROL_PORT='7101'
export VEHICLE_ID='VHC-001'
```

## 8. README 修改方案

README 应明确区分两个部署端。

电脑 A：

```text
run_cloud.py
server.py
static/
TCP 8001
```

Jetson：

```text
Jetson/start_edge.py
Body_Module_Board.py
UDP 7100/7101
HTTP/HTTPS 上传到电脑 A
```

浏览器访问电脑 A，而不是 Jetson：

```text
http://电脑A_IP:8001
```

README 中应删除或更正以下表述：

- 在 Jetson 启动 `run_cloud.py`；
- 浏览器访问 Jetson 本机 8001；
- Jetson 停止脚本同时停止云端；
- Jetson 从 `run_cloud.py` 读取上传令牌。

## 9. 测试方案

### 9.1 静态检查

```bash
python3 -m py_compile Jetson/start_edge.py Body_Module_Board.py Body_Module_Ethernet.py
shellcheck Jetson/*.sh
python3 -m pytest -q tests
```

### 9.2 电脑 A 检查

```bash
python run_cloud.py
curl -i http://127.0.0.1:8001/healthz
```

### 9.3 Jetson 到电脑 A 检查

局域网：

```bash
curl -i http://电脑A_IP:8001/healthz
```

公网：

```bash
curl -i http://15151e1e.r7.nas.cpolar.cn/healthz
```

### 9.4 进程隔离检查

启动 Jetson 入口后执行：

```bash
ps -ef | grep -E 'start_edge|Body_Module_Board|run_cloud|uvicorn'
ss -lntup
```

预期结果：

- 存在 `start_edge.py`；
- 存在 `Body_Module_Board.py`；
- 不存在 `run_cloud.py`；
- 不存在 Uvicorn；
- Jetson 不监听 TCP 8001；
- Jetson 监听 UDP 7100。

### 9.5 生命周期检查

1. 杀掉 `Body_Module_Board.py`，确认监督器约 3 秒后自动重启；
2. 按 `Ctrl+C` 停止 `start_edge.py`，确认车身域适配器退出；
3. 确认电脑 A 的 `run_cloud.py` 不受影响；
4. 断开电脑 A 网络，确认 Jetson 适配器继续运行并记录上传失败；
5. 网络恢复后，确认新收到的板端报文可以继续上传。

## 10. 验收标准

改造完成后必须满足：

1. Jetson 只需运行 `python3 Jetson/start_edge.py`；
2. Jetson 不运行 `run_cloud.py`、`server.py` 或 Uvicorn；
3. Jetson 不监听 TCP 8001；
4. Jetson 监听 UDP 7100；
5. Jetson 定期向 S32K344 UDP 7101 发送对时报文；
6. 网页控制命令可以从电脑 A 到达 Jetson，再通过 UDP 7101 下发板端；
7. 板端状态可以通过 UDP 7100 到达 Jetson，再上传电脑 A；
8. `Body_Module_Board.py` 异常退出后可以自动重启；
9. 停止 Jetson 入口不会影响电脑 A 的云端服务；
10. 局域网地址和 cpolar 公网地址均可通过环境变量切换。

## 11. 推荐实施顺序

1. 新建 `Jetson/start_edge.py`；
2. 将 `Body_Module_Board.py --mode udp` 注册为受管子进程；
3. 修改 `start_vshield_body.sh` 和 `stop_vshield_body.sh`；
4. 修改 `start_body_ethernet.sh`；
5. 修改或弃用旧串口演示脚本；
6. 更新安装脚本和桌面快捷方式；
7. 更新 README；
8. 执行单元测试和静态检查；
9. 在 Jetson 与真实 S32K344 上完成端到端验证。

## 12. 最终原则

改造后的 `body` 分支应遵循与 `video` 分支相同的职责边界：

> 云端代码只在电脑 A 运行；Jetson 只保留边缘采集、协议转换、设备控制、数据上传和进程监管能力。
