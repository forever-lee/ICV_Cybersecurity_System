# Ubuntu 蓝牙发送端

本方案的固定链路是：Ubuntu 电脑运行 `ble_s32_sender.py`，经 BLE 写入
nRF52840；nRF52840 经 UART 交给 S32K344；S32K344 再通过以太网 UDP
把报文发送给 Jetson 的 5000 端口。Jetson 只运行 `start_edge.py`，后端只
运行 `run_cloud.py`。

Ubuntu 需要有可被 BlueZ 识别的蓝牙适配器，且 `bluetoothctl list` 必须能
列出本机 Controller。创建环境并安装依赖：

```bash
conda create -n ble_gateway python=3.10 pip -y
conda activate ble_gateway
python -m pip install -r bluetooth_gateway/requirements.txt
```

启动发送端：

```bash
export BLE_ADDRESS='E2:92:FA:3D:0E:40'
python bluetooth_gateway/ble_s32_sender.py
```

`BLE_WRITE_UUID` 默认是 `00001001-0000-1000-8000-00805f9b34fb`。
`BLUETOOTH_SEND_INTERVAL_SECONDS` 和 `BLUETOOTH_ABNORMAL_EVERY` 可选。

该发送端只通过 BLE 与 nRF52840 通信，不配置云端地址、车辆 Token 或网页
控制轮询。遥测数据由 S32K344 经 Jetson 上传。网页端到端延迟与发送启停
按钮目前只保留界面位置，等待完整硬件闭环接入后启用。
