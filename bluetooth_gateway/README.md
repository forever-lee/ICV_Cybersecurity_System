# 蓝牙适配器网关

当前 S32K344 实验链路中，笔记本通过 BLE 写入 nRF52840；S32K344 收到 UART 报文后，通过网线向 Ubuntu 的 UDP 5000 端口发报文。因此笔记本运行 `ble_s32_sender.py`，Ubuntu 运行 `jetson_edge/bluetooth_edge_agent.py`。网页开关控制笔记本发送循环。`ble_to_udp_gateway.py` 仅供蓝牙设备主动 Notify、需要笔记本代为转发 UDP 的另一种链路使用。

```powershell
$env:BLE_ADDRESS="E2:92:FA:3D:0E:40"
$env:CLOUD_HTTP_URL="http://127.0.0.1:8001"
$env:VEHICLE_ID="VHC-001"
$env:VEHICLE_INGEST_TOKEN="与 run_cloud.py 的 INGEST_TOKEN 一致"
python -m pip install -r .\bluetooth_gateway\requirements.txt
python .\bluetooth_gateway\ble_s32_sender.py
```

在仓库根目录执行上面的命令。`BLE_WRITE_UUID`、`BLUETOOTH_SEND_INTERVAL_SECONDS`、`BLUETOOTH_ABNORMAL_EVERY` 和 `CLOUD_CONTROL_POLL_SECONDS` 可以用环境变量覆盖。程序断线后会自动重连。Ubuntu 有线网口需要保持 `192.168.4.100/24`，开发板的以太网 UDP 目标应指向该地址的 5000 端口。
