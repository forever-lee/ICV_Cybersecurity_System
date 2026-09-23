/*==================================================================================================
*   Project              : RTD AUTOSAR 4.7
*   Platform             : CORTEXM
*   Peripheral           : S32K3XX
*   Dependencies         : none
*
*   Autosar Version      : 4.7.0
*   Autosar Revision     : ASR_REL_4_7_REV_0000
*   Autosar Conf.Variant :
*   SW Version           : 3.0.0
*   Build Version        : S32K3_RTD_3_0_0_D2303_ASR_REL_4_7_REV_0000_20230331
*
*   Copyright 2020 - 2023 NXP Semiconductors
*
*   NXP Confidential. This software is owned or controlled by NXP and may only be
*   used strictly in accordance with the applicable license terms. By expressly
*   accepting such terms or by downloading, installing, activating and/or otherwise
*   using the software, you are agreeing that you have read, and that you agree to
*   comply with and are bound by, such license terms. If you do not agree to be
*   bound by the applicable license terms, then you may not retain, install,
*   activate or otherwise use the software.
==================================================================================================*/

/**
*   @file main.c
*
*   @addtogroup main_module main module documentation
*   @{
*/

/* Including necessary configuration files. */
#include "Mcal.h"

#include "Clock_Ip.h"
#include "Clock_Ip_Cfg.h"

#include "Siul2_Port_Ip.h"
#include "Siul2_Port_Ip_Cfg.h"

#include "Lpuart_Uart_Ip.h"
#include "Lpuart_Uart_Ip_Sa_PBcfg.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static volatile uint8_t g_ble_send_enable = 1U;
static char g_cmd_buf[256];

static uint32_t g_cmd_len = 0U;
static volatile uint32_t g_rx_byte_count = 0U;
static volatile uint8_t g_rx_debug_pending = 0U;



static void delay_ms_simple(uint32_t ms)
{
    volatile uint32_t i;
    volatile uint32_t j;

    for (i = 0U; i < ms; i++)
    {
        for (j = 0U; j < 40000U; j++)
        {
            __asm volatile ("nop");
        }
    }
}

static void uart_send_text(const char *text)
{
    (void)Lpuart_Uart_Ip_SyncSend(
        LPUART_UART_IP_INSTANCE_USING_1,
        (const uint8_t *)text,
        (uint32_t)strlen(text),
        1000000U
    );
}

static void handle_command(const char *cmd)
{
    uart_send_text("RX_CMD:");
    uart_send_text(cmd);
    uart_send_text("\r\n");

    if ((strstr(cmd, "BLE_TX_OFF") != NULL) ||
        (strstr(cmd, "424C455F54585F4F4646") != NULL))
    {
        g_ble_send_enable = 0U;
        uart_send_text("ACK:BLE_TX_OFF\r\n");
    }
    else if ((strstr(cmd, "BLE_TX_ON") != NULL) ||
             (strstr(cmd, "424C455F54585F4F4E") != NULL))
    {
        g_ble_send_enable = 1U;
        uart_send_text("ACK:BLE_TX_ON\r\n");
    }
    else
    {
        uart_send_text("ACK:UNKNOWN_CMD\r\n");
    }
}

static void clear_cmd_buffer(void)
{
    memset(g_cmd_buf, 0, sizeof(g_cmd_buf));
    g_cmd_len = 0U;
}

static uint8_t handle_single_command_char(uint8_t ch)
{
    if ((ch == 'F') || (ch == 'f'))
    {
        g_ble_send_enable = 0U;
        uart_send_text("ACK:BLE_TX_OFF\r\n");
        clear_cmd_buffer();
        return 1U;
    }
    else if ((ch == 'O') || (ch == 'o') || (ch == 'N') || (ch == 'n'))
    {
        g_ble_send_enable = 1U;
        uart_send_text("ACK:BLE_TX_ON\r\n");
        clear_cmd_buffer();
        return 1U;
    }
    else if (ch == '?')
    {
        if (g_ble_send_enable == 1U)
        {
            uart_send_text("STATUS:BLE_TX_ON\r\n");
        }
        else
        {
            uart_send_text("STATUS:BLE_TX_OFF\r\n");
        }

        clear_cmd_buffer();
        return 1U;
    }

    return 0U;
}




static void check_command_buffer(void)
{
    if ((strstr(g_cmd_buf, "BLE_TX_OFF") != NULL) ||
        (strstr(g_cmd_buf, "424C455F54585F4F4646") != NULL) ||
        (strstr(g_cmd_buf, "4F4646") != NULL) ||
        (strstr(g_cmd_buf, "F") != NULL))
    {
        g_ble_send_enable = 0U;
        uart_send_text("ACK:BLE_TX_OFF\r\n");
        clear_cmd_buffer();
    }
    else if ((strstr(g_cmd_buf, "BLE_TX_ON") != NULL) ||
             (strstr(g_cmd_buf, "424C455F54585F4F4E") != NULL) ||
             (strstr(g_cmd_buf, "4F4E") != NULL) ||
             (strstr(g_cmd_buf, "N") != NULL))
    {
        g_ble_send_enable = 1U;
        uart_send_text("ACK:BLE_TX_ON\r\n");
        clear_cmd_buffer();
    }
    else if ((strstr(g_cmd_buf, "STATUS?") != NULL) ||
             (strstr(g_cmd_buf, "5354415455533F") != NULL) ||
             (strstr(g_cmd_buf, "?") != NULL))
    {
        if (g_ble_send_enable == 1U)
        {
            uart_send_text("STATUS:BLE_TX_ON\r\n");
        }
        else
        {
            uart_send_text("STATUS:BLE_TX_OFF\r\n");
        }

        clear_cmd_buffer();
    }
}

static void poll_uart_command(void)
{
    uint8_t ch;
    Lpuart_Uart_Ip_StatusType status;
    uint32_t rx_count = 0U;

    while (rx_count < 64U)
    {
        status = Lpuart_Uart_Ip_SyncReceive(
            LPUART_UART_IP_INSTANCE_USING_1,
            &ch,
            1U,
            1000U
        );

        if (status != LPUART_UART_IP_STATUS_SUCCESS)
        {
            break;
        }

        rx_count++;

        /*
         * 先按单字符命令处理。
         * F/0 = 关闭发送
         * O/N/1 = 恢复发送
         * ? = 查询状态
         */
        if (handle_single_command_char(ch) == 1U)
        {
            continue;
        }

        /*
         * 如果还想兼容 BLE_TX_OFF / BLE_TX_ON 这种长命令，
         * 再把可显示字符放进缓冲区判断。
         */
        if ((ch < 0x20U) || (ch > 0x7EU))
        {
            continue;
        }

        if (g_cmd_len < (sizeof(g_cmd_buf) - 1U))
        {
            g_cmd_buf[g_cmd_len] = (char)ch;
            g_cmd_len++;
            g_cmd_buf[g_cmd_len] = '\0';

            check_command_buffer();
        }
        else
        {
            clear_cmd_buffer();
        }
    }
}

int main(void)
{
    const uint8_t at_mode_cmd[] = "AT+MODE=1\r\n";
    char msg[256];
    uint32_t tx_count = 1U;
    uint32_t loop_count = 0U;
    uint32_t abnormal_count = 1U;
    uint32_t abnormal_tick = 0U;

    Clock_Ip_Init(&Clock_Ip_aClockConfig[0U]);

    Siul2_Port_Ip_Init(NUM_OF_CONFIGURED_PINS0, g_pin_mux_InitConfigArr0);

    Lpuart_Uart_Ip_Init(
        LPUART_UART_IP_INSTANCE_USING_1,
        &Lpuart_Uart_Ip_xHwConfigPB_1
    );

    delay_ms_simple(1000U);

    /* 上电后先把蓝牙模块从 AT 模式切到透传模式 */
    (void)Lpuart_Uart_Ip_SyncSend(
        LPUART_UART_IP_INSTANCE_USING_1,
        at_mode_cmd,
        sizeof(at_mode_cmd) - 1U,
        1000000U
    );

    delay_ms_simple(1000U);

    uart_send_text("BOOT:READY\r\n");

    while (1)
    {
        poll_uart_command();

        /*
         * 这里大约每 1000 个短延时周期发送一次正常报文。
         * 实际周期受 poll_uart_command() 和 UART 发送耗时影响。
         */
        if (loop_count >= 1000U)
        {
            loop_count = 0U;

            if (g_ble_send_enable == 1U)
            {
                int len = 0;

                /* 1. 正常蓝牙数据报文 */
                len = snprintf(
                    msg,
                    sizeof(msg),
                    "S32K344_HELLO_%04lu\r\n",
                    (unsigned long)tx_count
                );

                if (len > 0)
                {
                    uart_send_text(msg);
                }

                tx_count++;

                /* 2. 每 10 次循环发送 1 条异常报文 */
                abnormal_tick++;

                if (abnormal_tick >= 5U)
                {
                    abnormal_tick = 0U;

                    len = snprintf(
                        msg,
                        sizeof(msg),
                        "S32K344_ABNORMAL_%04lu;TYPE=BLE_ATTACK;LEVEL=HIGH\r\n",
                        (unsigned long)abnormal_count
                    );

                    if (len > 0)
                    {
                        uart_send_text(msg);
                    }

                    abnormal_count++;
                }
            }
        }

        delay_ms_simple(1U);
        loop_count++;
    }
}
/** @} */
