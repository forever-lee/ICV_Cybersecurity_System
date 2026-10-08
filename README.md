# V-SHIELD Body Domain V1

S32K344车身域状态通过100BASE-T1发送到Jetson，并由本地Web页面展示和控制。

本仓库只保留车身域运行所需代码，不包含Wi-Fi、蓝牙、导航、GNSS和摄像头/视频采集程序。

## 主要文件

- `Body_Module_Board.py`：Jetson车身域UDP适配器，接收开发板状态并向Web后台上传。
- `Body_Module_Ethernet.py`：车身域以太网兼容适配程序。
- `run_cloud.py`：启动FastAPI服务和Web页面。
- `server.py`：车身域数据接口、控制接口、历史记录和WebSocket推送。
- `stream_protocol.py`：Web服务需要的共享通信协议实现。
- `static/`：包含车身域页面所需的前端资源。
- `Jetson/`：Jetson安装、一键启动和停止脚本。
- `tests/`：车身域控制和以太网协议测试。

## 默认网络参数

- S32K344：`192.168.4.31/24`
- Jetson eth0：`192.168.4.100/24`
- UDP状态端口：`7100`
- UDP控制和对时端口：`7101`
- Web端口：`8001`

## 安全配置

代码库不保存真实Token或地图密钥。复制`.env.example`中的变量名称，并通过启动环境或服务配置提供实际值：

- `VEHICLE_INGEST_TOKEN`
- `DASHBOARD_ACCESS_TOKEN`
- `AMAP_JS_KEY`
- `AMAP_SECURITY_JS_CODE`
- `AMAP_SERVICE_HOST`

不要提交包含真实值的`.env`文件。

## Jetson启动

首次安装：

```bash
python3 -m pip install -r requirements.txt
cd Jetson
chmod +x *.sh
./install_vshield_launcher.sh
```

安装后可以使用生成的“启动 V-SHIELD”和“停止 V-SHIELD”快捷方式，也可以直接运行：

```bash
./Jetson/start_vshield_body.sh
```

浏览器访问：`http://127.0.0.1:8001`

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q tests
```
