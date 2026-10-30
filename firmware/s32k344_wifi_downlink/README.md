# S32K344 WiFi 下行源码补丁

此补丁保存本次已经完成实物验证的板端改动，与平台 WiFi 暂停/恢复控制配套。原工程为 `lwip_baremetal_s32k344_wifi_uart_backup`，只修改 `src/wifi_at_uart.c`、`src/wifi_udp_forward.c`、`include/wifi_at_uart.h`。完整 NXP SDK、生成文件及编译产物不重复纳入本仓库。

功能：板端监听有线 UDP 6002，接收来自 192.168.4.100 的 CTRL 命令，校验后由 PTC7/LPUART1_TX 送至 ESP32 GPIO16。命令使用换行分帧，UART 发送由主循环轮询推进。脚本 ACK 沿原来的串口接收及有线上行返回。ESP32 GPIO17 接 PTC6，双方共地。

在原工程根目录，对尚未包含下行功能的源码先检查再应用（补丁路径替换为实际路径）：

```sh
git apply --check /path/to/downlink.patch
git apply /path/to/downlink.patch
```

已烧录并验证过下行功能的工程不需要再应用。回退本次修改可用 `git apply -R`；操作前先备份工程。

S32DS 使用 NXP GCC 10.2、Debug_FLASH 配置编译，生成 `Debug_FLASH/lwip_baremetal_s32k344_wifi_uart_backup.elf`，烧录后 Resume。平台接入无需再次烧录。实际时钟和引脚配置沿用原工程；停止在断点会使整个转发链路停止。

原工程还保留示例的 `tests_timeout` 退出逻辑，本补丁不修改它；长时间运行时应单独核对 `test.c` 中时间单位和退出条件。
