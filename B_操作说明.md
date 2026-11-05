# WiFi A / B 版：烧录和本地验证

## 1. 版本位置（不要混用）

- A 版原平台：`D:\S32kds\WKspace\wifi_downlink_integration`，未修改。
- A 版原板端：`D:\NXP\S32DS\WKspace\lwip_baremetal_s32k344_wifi_uart_backup`，未修改。
- A 版完整备份：`D:\S32kds\WKspace\wifi_versions\A_20261104`，包括 platform、board 和原 ELF。
- B 版平台：`D:\S32kds\WKspace\wifi_versions\B_platform`。
- B 版板端：`D:\S32kds\WKspace\wifi_versions\B_board`，S32DS 工程名为 `wifi_gateway_B`。

以上绝对路径对应开发电脑。其他电脑可从仓库获取平台代码，并按板端补丁说明在自己的工程上编译固件。

## 2. B 版实现内容

- 云端视角 TX / RX 记录、分别累计收发数量。TX 是 Jetson 实际执行 UDP sendto 后上报的下发次数，包括重试；不代表板子收到或动作完成。RX 是上传到后端的充电报文、状态回复、确认。
- 每 5 秒查询一次真实发送状态，暂停时也回复；15 秒没有新回复后显示状态未知。
- 用户命令优先于查询。相同编号重试最多 5 次，约 1 秒间隔；每个操作有效期 12 秒。HTTP 延迟也会影响实际重试间隔。
- 重复命令不会重复执行，顺序号拒绝迟到的旧命令；过期命令不执行。回复和后续查询都可以确认实际状态。
- 默认正常充电，不再每轮自动随机攻击。SOC 突变与流量模拟由页面选择触发。
- SOC：收到命令约 2 秒后，注入一次 15～25 个百分点的突变，下一条恢复真实曲线。
- DDoS 流量模拟：约 2 秒后，以目标 10 条/秒发送，持续 10 秒，再回到 1 条/秒。仅是有界流量演示，不是压力测试。
- 充电进度按真实运行时间推进，增加报文不会加速充满。暂停会冻结充电时间并取消当前或尚未触发的攻击；恢复后正常发送。
- 后端对 SOC 突变、2 秒窗口内至少 8 条/秒的充电流量分别告警。查询/确认不参与流量规则。告警在本轮保持红色，新 START 清除上一轮告警；清空按钮仅清理当前浏览器显示，不控制板子或后端。
- UDP 收包和 HTTP 上传使用不同线程，网络请求不再阻塞确认接收。上传队列最多 256 条，队列满会明确打印丢包；普通遥测 HTTP 失败会报告失败，不无限重放，避免伪造接收速率。控制确认会保留并重试上传。
- 板子保留 MAC `02:11:22:04:02:01`、IP `192.168.4.201`；扩展受限下行命令；关闭原例程演示计时结束后自动停网的行为。

## 3. 先烧录 B 版

**只选择这个文件：**

`D:\S32kds\WKspace\wifi_versions\B_board\Debug_FLASH\wifi_gateway_B.elf`

生成时间：2026-11-05 12:29:39；SHA256：

`6D95400405685BA9E1BA4EF625DA340C9126C48DBE8BE3F48BE039F01441F11F`

### 复制原来成功的调试配置并切换 ELF

1. 关闭 A 版发送、接收、后端窗口。S32DS 若正在调试，点红色 Terminate 终止旧会话。
2. 打开 `Run → Debug Configurations…`。
3. 选中以前烧录成功的 SEGGER J-Link / Debug_FLASH 配置，复制并重命名为 `WiFi_B`。
4. Main 页的 `C/C++ Application` 用 Browse 选择上面的完整 **wifi_gateway_B.elf** 路径，核对包含 `wifi_versions\B_board`。
5. 本次使用已经编译好的 ELF，将“启动前构建 / Build before launch”设为禁用，避免又构建了原 A 工程。调试器、芯片型号、下载方式沿用原来成功配置。
6. 点击 Apply，再点击 Debug，等待 Flash 下载结束。
7. 若停在 main 或断点，按 F8 / Resume。测试时必须处于 Running 状态。
8. 本次 ESP32 的固件、热点名、密码和透传配置都不用改。

若以后需要在 S32DS 编辑、编译 B 源码：File → Import → Existing Projects into Workspace，选择 `B_board`，不勾选复制工程。导入后的名称为 `wifi_gateway_B`。它与 A 工程并存；调试应用仍选 B 的 `Debug_FLASH\wifi_gateway_B.elf`。

## 4. 单电脑运行 B 版

接线：电脑 WiFi → ESP32 热点；ESP32 串口 ↔ K3；K3 网线 → 已上电交换机 → 电脑有线网口。

| 设备 / 网卡 | IP / 端口 |
|---|---|
| ESP32 热点 | 192.168.3.1，UDP 8080 |
| 电脑 WiFi | **192.168.3.2**，UDP 6001 |
| K3 有线 | 192.168.4.201，命令监听 UDP 6002 |
| 电脑有线 | **192.168.4.100/24**，接收 UDP 6001 |
| 本机后端 | http://127.0.0.1:8001 |

WiFi 接口与有线接口同时使用，IP 不要填反。其他热点客户端不能占用 192.168.3.2；其他设备不能占用有线 192.168.4.100。

**发送电脑、Jetson、后端电脑的系统时间应同步。**B 使用命令有效期拒绝迟到命令，时间差过大会导致 EXPIRED。单电脑测试共用系统时间，无此问题。

1. 确认有线 IP 是 `192.168.4.100`，`ping 192.168.4.201` 成功。
2. WiFi 连接 `S32K344_WIFI`，确认分配/设置为 `192.168.3.2`。
3. 打开 `B_platform` 文件夹，依次双击：
   - `B_1_backend.bat`：后端。
   - `B_2_receiver.bat`：有线收包、上传、下行控制。
   - `B_3_sender.bat`：模拟 BMS 发送。
4. 三个窗口都保持打开。发送程序确认热点后倒数 3 秒开始。不要同时运行 A 版程序。
5. 浏览器访问 `http://127.0.0.1:8001`，使用原来的访问口令，进入“车联网域”，按 Ctrl+F5 强制刷新。
6. 等约 5～10 秒，应出现“正常发送”，TX 查询和 RX 回复持续增加，SOC 随充电上升。

脚本优先使用这台电脑已有 Python 环境 `%USERPROFILE%\Desktop\testS32K\TM\TM\python.exe`。找不到时才使用 PATH 中的 python。

## 5. 逐项验收

1. **正常充电**：先不点攻击。约 120 秒从 20% 到 100%，正常模式无告警。轮间倒数 3 秒后继续下一轮。
2. **暂停**：点击暂停，先显示处理中，真实回复后显示“已暂停发送”。SOC 和充电报文停止推进，但每 5 秒仍有状态查询与回复。连接应保持在线。
3. **恢复**：点击恢复，继续原进度，不重新开始一轮。
4. **SOC 突变**：正常发送时选择 SOC，点开始。约 2 秒后产生一次突变，底部变红；随后 SOC 恢复正常曲线。告警保留到下一轮或当前浏览器清空。
5. **流量模拟**：等待攻击状态显示未启用，选 DDoS，点开始。约 2 秒后 RX 增长加快，随后底部出现流量告警，异常记录标红；10 秒后流量恢复正常。
6. **中止攻击**：执行期间可点停止攻击；点暂停也会取消攻击。恢复不应再次触发已经取消的攻击。
7. **清空显示**：点击清空，本页面 RX/TX 计数和记录清零，后端及设备保持工作，新报文继续出现。
8. **断链**：短暂断开链路，状态应转为未知。恢复后周期查询自动重建真实状态；不能凭旧状态一直显示正常。网络断开期间不保证遥测零丢包。
9. **三个板子并联**：完成单板验证后接入另外两块板子，再验证 PING、充电、暂停、恢复。保持每块板子的 MAC 唯一。

## 6. Jetson 上使用 B 接收端

不要只复制一个 WiFi_Module.py。将 B 版的 **整个 jetson_edge 文件夹**复制过去，至少需要：

- `WiFi_Module.py`
- `wifi_control_agent.py`
- `wifi_control_protocol.py`

Jetson 有线仍配置为 `192.168.4.100/24` 并接交换机。进入该文件夹后运行：

```bash
CLOUD_HTTP_URL=http://后端电脑的实际IP:8001 python3 WiFi_Module.py
```

若后端仍走之前公网 HTTP 映射，将上述 URL 换成有效映射域名，不在映射域名后强加 8001。实际后端内部端口与外部映射地址分别配置。接收端的 VEHICLE_INGEST_TOKEN 必须与后端一致。原来的访问令牌设置保持有效，不应把测试端口 18081 或 B-local-test 用于实物集成。

暂停时 Jetson 接收程序和发送电脑程序都必须保持运行，否则无法恢复。

## 7. A 版恢复

1. 关闭 B 三个窗口，停止 B 调试会话。
2. 使用保留的 A 调试配置，烧录原 A ELF，或者以下备份：
   `D:\S32kds\WKspace\wifi_versions\A_20261104\board\Debug_FLASH\lwip_baremetal_s32k344_wifi_uart_backup.elf`
3. 按 F8 运行，再打开原 A 平台目录中的三个启动文件。

A ELF SHA256：`AD61C4F4E3E67E4DECE900B0D4A6C71D0862EEAA218A24B8CA84F50B18F22097`。

## 8. 已完成验证与限制

- WiFi 自动测试 18 项通过；GB/T 演示解析 3 项通过；接收载荷与时间戳 2 项通过，共 23 项。
- 覆盖真实本机 UDP + HTTP 接口、命令丢失、确认丢失、最多 5 次重发、过期/旧命令、攻击去重、慢 HTTP、队列上限、权限校验、下一轮基线重建、两种检测规则。
- 已验证浏览器中的暂停、恢复、SOC 突变、流量模拟、TX/RX 和红色告警。随后在开发电脑连接实物板子的环境完成了本地联调。
- 前端 JS 语法检查通过。板端使用 NXP GCC 10.2 完整重编译、链接成功；日志为 B_board/build_B.log。完整编译有例程原有的未使用变量/函数警告，本次新命令解析无新增编译警告。
- 实物烧录、串口、电源、交换机和公网链路验收仍需按第 5 节验证。

### 自行复跑软件验证

在 B_platform 目录运行：

```powershell
& "$env:USERPROFILE\Desktop\testS32K\TM\TM\python.exe" -m unittest discover -s tests -p "test_wifi*.py" -v
& "$env:USERPROFILE\Desktop\testS32K\TM\TM\python.exe" -m unittest discover -s tests -p "test_gbt74_demo.py" -v
node --check static/app.js
```
