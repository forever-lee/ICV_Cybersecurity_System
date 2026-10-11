# 蓝牙域融合要求

## 1. 融合基线

- 以最新 `main` 分支为基线融合蓝牙功能。
- 当前蓝牙分支不是完整代码，只提取其中可用的蓝牙采集、UDP 转发和上传逻辑，不直接整体合并。
- 不修改智能驾驶域现有功能，包括摄像头采集、视频 Web 演示、GPS 上传和地图渲染。

## 2. 后端电脑 A

后端电脑 A 只启动一个入口：

```powershell
python run_cloud.py
```

蓝牙域后端接口必须融合到现有 `server.py` 中，由 `run_cloud.py` 一起启动。

不在电脑 A 额外手工启动蓝牙后端服务进程。

## 3. Jetson

Jetson 运行代码统一放在 `jetson_edge/`，唯一人工启动入口为：

```bash
python3 start_edge.py
```

新增：

```text
jetson_edge/bluetooth_edge_agent.py
```

该程序负责：

- 接收蓝牙网关转发的 UDP 数据；
- 将蓝牙数据上传到电脑 A 后端；
- 网络异常后自动重试。

`bluetooth_edge_agent.py` 必须注册成 `start_edge.py` 管理的独立子进程。蓝牙进程异常退出时，只重启蓝牙进程，不能影响摄像头、GPS和其他子进程。

Jetson 不运行 `run_cloud.py`，不增加 Shell 启动脚本或 systemd 服务。

## 4. 蓝牙网关程序

蓝牙分支中的 `ble_to_udp_gateway.py` 负责连接 BLE 设备，并把数据通过 UDP 发送给 Jetson。

它属于蓝牙设备接入工具，不属于电脑 A 的云端后端。根据 BLE 适配器实际连接位置，可运行在独立电脑或 Jetson 上，但不能作为额外的云端服务运行。

## 5. 配置要求

公网地址、车辆编号、令牌、Jetson 地址和 UDP 端口全部通过环境变量配置，不能硬编码在蓝牙程序中。

Jetson 蓝牙上传程序统一使用：

```dotenv
CLOUD_HTTP_URL=https://15151e1e.r7.nas.cpolar.cn
VEHICLE_ID=VHC001
VEHICLE_TOKEN=<实际令牌>
BLUETOOTH_UDP_HOST=0.0.0.0
BLUETOOTH_UDP_PORT=5000
BLUETOOTH_ENABLED=1
```

## 6. 最终数据链路

```text
蓝牙设备
  -> BLE 网关
  -> UDP
  -> Jetson bluetooth_edge_agent.py
  -> HTTPS
  -> 电脑 A run_cloud.py/server.py
  -> 蓝牙域页面
```

完成融合后：

- 电脑 A 只需运行 `python run_cloud.py`；
- Jetson 只需运行 `python3 start_edge.py`；
- 蓝牙数据可以上传并在页面显示；
- 蓝牙链路故障不会影响视频和 GPS 功能。
