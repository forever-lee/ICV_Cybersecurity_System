# 笔记本蓝牙发送端

当前演示由同一台 Windows 笔记本运行 `run_cloud.py` 和
`bluetooth_gateway/ble_s32_sender.py`。笔记本经 BLE 写入 nRF52840，随后由
S32K344 经以太网 UDP 5000 发给 Jetson；Jetson 只运行 `start_edge.py`，
由它启动 `bluetooth_edge_agent.py` 并上传遥测。蓝牙模块继续使用已有的透传模式。

在仓库根目录的 PowerShell 中安装依赖并启动两个进程（分别占用一个窗口）：

```powershell
python -m pip install -r bluetooth_gateway/requirements.txt
python run_cloud.py
```

```powershell
$env:BLE_ADDRESS = 'E2:92:FA:3D:0E:40'  # 换成现场实际 BLE 地址
python bluetooth_gateway/ble_s32_sender.py
```

发送端只连接 BLE，不向云端发 HTTP，也不保存车端上传 Token。启动后同时订阅
`BLE_NOTIFY_UUID`（默认 `00001002-0000-1000-8000-00805f9b34fb`）。

模拟异常报文默认采用 `BLUETOOTH_ATTACK_MODE=replace`：每 10 个发送位置中，
随机选第 5 或第 6 个位置，用一条 `S32K344_ABNORMAL` 替换对应的 `HELLO`，
不额外增加一条报文。因此该轮缺少 `HELLO_05` 或 `HELLO_06` 是预期现象，
不能算作链路丢帧。异常报文仍带合法 `CRC`，它是业务层攻击标记，不是故意
制造损坏的传输帧。若要恢复旧的“每 5 条正常报文后额外插入 1 条异常报文”，
启动前设置 `BLUETOOTH_ATTACK_MODE=extra`。做纯正常报文丢帧基线测试时，
启动前设置 `BLUETOOTH_ABNORMAL_EVERY=0`；测试后取消此环境变量即可恢复模拟攻击。
更改环境变量后需重启发送端进程。

在发送端命令行输入 `off`、`on` 或 `status` 并按回车，可暂停、恢复业务报文或查看状态。
网页蓝牙域的“关闭发送”“打开发送”按钮经云端、Jetson UDP 5001、K344 UART、
nRF52840 BLE Notify 送到这个进程；进程修改发送状态后，沿 BLE Write、K344
UDP 5000、Jetson 返回带 CRC 的 `BTACK`。网页收到与命令 ID 和 nonce 匹配的
真实 ACK 才显示成功；未收到会显示超时，不能仅凭 UDP 下发成功显示成功。
`off` 不会断开 BLE、停止 Notify 或结束进程。停止整个发送端请按 `Ctrl+C`。

业务报文默认在 BLE 写入完成后继续下一帧。若 K344 的 `BTDATA` 回执超时，
发送端会在后台重发同一 `SEQ`/`CRC`（默认最多发送 3 次），不暂停新报文、
也不因回执缺失强制重连。收到回执会打印 `[K3 异步确认]`；重试和最终未确认
分别打印 `[K3 后台重发]`、`[K3 未确认]`。`[BLE 已发送]`
只代表写入蓝牙，不能单独证明 Jetson 或云端收到。云端累计报文与 Jetson
`[蓝牙上传成功]` 才能判断实际上传情况。若需要恢复旧的逐帧阻塞确认行为，
可设置 `BLUETOOTH_REQUIRE_DATA_ACK=1`，但回执不稳定时吞吐量会明显下降。

CMD 中的 `on`/`off` 仅改变本机发送，不直接访问云端，也不会伪装成网页命令 ACK。

## 报文完整性与重试

业务报文现在带 `CRC`。K344 对识别为业务类型的报文校验 CRC；同一
`SEQ`/`CRC` 的重复帧只转发一次、但会再次确认，并通过 UART → nRF52840 BLE Notify
返回 `BTDATA` 硬件接收确认。默认发送端没有收到确认时会在后台重发**同一条**报文，
但后续新报文仍照常发送；`off` 会暂停重试，`on` 后继续。
`[K3 异步确认]` 表示 K344 收到了完整报文，`[K3 未确认]` 表示达到重试上限仍无回执。
这个确认只覆盖笔记本到 K344；后续 UDP 和云端上传仍需查看 Jetson 日志。

新版发送端应与支持 `BTDATA` 的新版 K344 固件一起使用；否则默认模式的后台重发
会在每条报文上持续到重试上限。更新后还须把新的 `jetson_edge/bluetooth_time_sync.py`
和 `jetson_edge/bluetooth_edge_agent.py` 复制到 Jetson 的
`/home/nvidia/all_code1/` 并重启 `start_edge.py`，否则带
`CRC` 的业务报文不会被旧版 Jetson 解析。不要仅通过网页计数判断是否无丢包；
对比发送端 `[K3 已确认]`、Jetson `[蓝牙上传成功]` 和网页报文编号。

可选：`BLUETOOTH_DATA_ACK_TIMEOUT_SECONDS`（默认 2.5 秒）、
`BLUETOOTH_DATA_ACK_ATTEMPTS`（默认总共发送 3 次）。默认快速模式到达上限后
只记录未确认，不会为此断开 BLE；设置 `BLUETOOTH_REQUIRE_DATA_ACK=1`
可恢复旧的逐帧阻塞与重连行为。

## 蓝牙端到端延迟

Jetson 每 5 秒沿 UDP 5001 → S32K344 UART → nRF52840 Notify 发送带 CRC 的
`BTSYNC` 请求。发送电脑记录 Notify 到达的单调时钟 U2、BLE Write 前的 U3，
再沿 BLE Write → S32K344 UDP 5000 回传响应。Jetson 用 T1/T4/U2/U3 建立
最多 30 秒有效的时钟映射。业务报文附 `SEQ`、`TICK_US`、`SYNC_ID`；云端显示
“发送电脑写入 BLE 前 → 云端收到”的估算延迟（毫秒）。同步失败、RTT 超过
500 ms、映射过期或云端与 Jetson 时钟未对齐时显示“待时间同步”。这不是
S32K344 或 nRF52840 内部处理时间的单独测量值。

对时请求和响应现使用带 CRC 的紧凑版 `BTSYNC|2|Q/R|...#`，各不超过 40 字节，
以减少 BLE 分包和串口混帧；旧版 `BTSYNC|1|...#` 仍能解析。RTT 500 ms
门槛保持不变。更新时发送电脑和 Jetson 的对时代码须一起替换并重启，
现有支持 `BTSYNC|` 透传的 K344 固件无需为此紧凑版再次烧录。
如果个别对时响应丢失，但近 30 秒内已有成功对时，Jetson 可用同一发送电脑的
有效时钟映射估算报文时间；仍要求报文时间戳与样本接近、延迟非负且不超过 60 秒。
Jetson 日志的 `[蓝牙报文已对时]` 会显示报文 ID、实际采用的映射 ID 和到
Jetson 的延迟，方便区分“对时已成功”与“最新报文已对时”。
Jetson 蓝牙代理另每 10 秒读取一次云端 `/healthz` 时间，选择最近 30 秒内
往返不超过 100 ms 的样本，估计“云端时钟－Jetson 时钟”的偏差。云端只在
蓝牙硬件映射和这份云端校时样本均有效、校正后的各段耗时非负时显示
“端到端”；否则只显示“至 Jetson”，不影响报文上传、计数或重传。
这个数值是发送电脑 BLE Write 前至云端接收的估算，误差仍受两次校时
的往返时延与网络不对称影响，不是射频单向时延的精密测量。

关闭业务发送不会关闭 BLE Notify 或时间同步。发送端仍不直接访问云端。
需要更新发送端代码、Jetson 的三个文件（`bluetooth_edge_agent.py`、
`bluetooth_time_sync.py`、`start_edge.py`）、云端代码，并重新烧录已加入
`BTSYNC` 转发的 S32K344 固件；仅替换 Python 文件不能让旧固件转发对时报文。
更新后分别重启发送端、Jetson `start_edge.py` 和云端 `run_cloud.py`。

`BLE_WRITE_UUID` 默认是 `00001001-0000-1000-8000-00805f9b34fb`。
`BLUETOOTH_SEND_INTERVAL_SECONDS`、`BLUETOOTH_ABNORMAL_EVERY` 可选。
`BLUETOOTH_INITIAL_ENABLED=0` 可让进程启动时先暂停业务发送；默认启动后发送。

## 上板验证

先确认原有网页 ON/OFF 仍得到真实硬件 ACK。再查看发送端是否打印
`[蓝牙对时响应已发出]`、Jetson `logs/bluetooth.log` 是否打印
`[蓝牙对时成功]`。两者都有且云端时钟准确、持续收到业务报文时，网页
“端到端延迟（估算）”才会出现数值。当前修改仅通过本地模拟测试和 S32DS
编译，尚未在实际板卡上完成烧录与往返验证。

## 旧 K3 模拟链路与当前链路的区别

`E:\S32DS代码\main.c.txt` 中的旧程序只向 UART 发报文：一次
`Lpuart_Uart_Ip_SyncSend` 写出一条短的 `S32K344_HELLO_0001\r\n`。
`E:\S32DS代码\ble_to_udp_gateway.txt` 在笔记本按 `\r\n` 缓存 BLE
Notify 的碎片，凑齐完整行后才发出一个 UDP 包。该链路方向是
**K3 → nRF Notify → 笔记本 → Jetson**。

当前链路方向是 **笔记本 BLE Write → nRF UART → K3 UART 接收 →
K3 UDP → Jetson**。带 `SEQ/TICK_US/SYNC_ID/CRC` 的一条报文长于
单次 20 字节 BLE 写入，笔记本需分多次写入。旧程序没有测试这条
反向通道，也没有 K3 的 UART 接收、`#` 组帧或 UDP 转发代码；
因此旧链路不丢帧不能证明当前链路的接收路径正确。

当前烧录工程已定位到
`D:\S32DS\workspace\lwip_baremetal_s32k344_gai.zip_expanded\lwip_baremetal_s32k344\src\test.c`。
它已经按 `#` 缓存完整报文并做 CRC 检查，但旧版 UART 接收每满
128 字节就结束一次 `AsyncReceive`；主循环重新启动时，RTD 驱动
会清空硬件 RX FIFO，可能吞掉紧跟着的字节。现已改为在 RX_FULL
回调里连续续接单字节缓冲，并用 4096 字节环形队列交给主循环，
避免正常收满时停止/清空串口。修改已通过本地 S32DS 工程编译，
尚未烧录和上板验证。若上板后仍有 CRC 错误，下一步需核对
nRF 模块 BLE Write → UART 的实际字节流。不能在 Jetson/网页
重新拼凑已经夹杂乱码或相互覆盖的报文，否则会把坏帧伪装成正常数据。

排查时先设 `BLUETOOTH_ABNORMAL_EVERY=0`，只发送正常报文，按
`SEQ`（不是循环的 `HELLO_XX` 后缀、也不是网页的云端 `#`）
逐级核对发送端、K3 ACK、Jetson 接收和云端记录。发送端的
后台限次重发可能令旧 `SEQ` 晚于新 `SEQ` 抵达；这属于乱序，
需与真正未到达或 CRC 被拒绝分开统计。网页现显示发送 `SEQ`，
即使该帧尚未完成时间同步也能用于核对。
