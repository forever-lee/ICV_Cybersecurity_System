import asyncio
import json
import os
import socket
from datetime import datetime
from urllib import error, request
from bleak import BleakClient

BLE_ADDRESS = "E2:92:FA:3D:0E:40"

# S32K344 -> 蓝牙模块 -> Python，接收 Notify
NOTIFY_UUID = "00001002-0000-1000-8000-00805f9b34fb"

# Python -> 蓝牙模块 -> S32K344，下发 Write
# 先用 1001，如果 off/on 无效，后面再改成 1003
WRITE_UUID = "00001001-0000-1000-8000-00805f9b34fb"

UDP_IP = "192.168.4.10"
UDP_PORT = 5000

# 云端控制接口。默认连接本机运行的 run_cloud.py，也可用环境变量覆盖。
CLOUD_HTTP_URL = os.getenv("CLOUD_HTTP_URL", "http://127.0.0.1:8000").rstrip("/")
VEHICLE_ID = os.getenv("VEHICLE_ID", "VHC-001")
INGEST_TOKEN = os.getenv(
    "VEHICLE_INGEST_TOKEN",
    "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0",
)
CLOUD_CONTROL_POLL_SECONDS = float(os.getenv("CLOUD_CONTROL_POLL_SECONDS", "1"))

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

buffer = b""
last_line = None
last_ack_text = ""


def on_notify(sender, data: bytearray):
    global buffer
    global last_line
    global last_ack_text

    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    buffer += bytes(data)

    while b"\r\n" in buffer:
        one_line, buffer = buffer.split(b"\r\n", 1)

        if len(one_line) == 0:
            continue

        text = one_line.decode("utf-8", errors="ignore")

        if text.startswith("ACK:") or text.startswith("STATUS:"):
            last_ack_text = text

        if one_line == last_line:
            print(f"[{now}] DUP SKIP: {text}")
            continue

        last_line = one_line

        print(f"[{now}] BLE LINE: {text}")

        udp_payload = one_line + b"\r\n"
        sock.sendto(udp_payload, (UDP_IP, UDP_PORT))

        print(f"UDP SENT -> {UDP_IP}:{UDP_PORT}  {text}")

async def write_payload_chars(client: BleakClient, payload: str):
    for ch in payload:
        await client.write_gatt_char(
            WRITE_UUID,
            ch.encode("utf-8"),
            response=True
        )
        await asyncio.sleep(0.03)


async def send_until_ack(client: BleakClient, payload: str, expected_ack: str, max_retry: int = 10):
    global last_ack_text

    for attempt in range(1, max_retry + 1):
        last_ack_text = ""

        print(f"BLE WRITE TRY {attempt} -> {payload}")

        try:
            await write_payload_chars(client, payload)
        except Exception as e:
            print("BLE WRITE FAILED:", e)
            continue

        # 缩短等待时间：最多等 0.35 秒
        for _ in range(60):
            if expected_ack in last_ack_text:
                print(f"CMD ACK OK: {last_ack_text}")
                return True
            await asyncio.sleep(0.01)

        print(f"NO ACK, retry... expected={expected_ack}")

    print(f"CMD FAILED: 多次重发后仍未收到 {expected_ack}")
    return False


async def execute_command(client: BleakClient, command: str, command_lock: asyncio.Lock):
    """键盘与云端共用的唯一控制入口，避免并发写 BLE。"""
    command = command.strip().lower()
    async with command_lock:
        if command == "off":
            return await send_until_ack(client, "FFFFFF", "ACK:BLE_TX_OFF")
        if command == "on":
            return await send_until_ack(client, "OOOOOO", "ACK:BLE_TX_ON")
        if command == "st":
            return await send_until_ack(client, "???", "STATUS:")
    return False


def cloud_control_request(path: str, payload=None):
    url = f"{CLOUD_HTTP_URL}/api/vehicles/{VEHICLE_ID}/bluetooth/control/{path}"
    headers = {
        "Authorization": f"Bearer {INGEST_TOKEN}",
        "Accept": "application/json",
        "User-Agent": "BLE-UDP-Gateway/1.0",
    }
    body = None
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    http_request = request.Request(url, data=body, headers=headers, method=method)
    with request.urlopen(http_request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def cloud_control_loop(client: BleakClient, command_lock: asyncio.Lock):
    """持续获取云端开关指令，执行后把 BLE ACK 结果回传给页面。"""
    last_result = None
    print(f"Cloud control ready: {CLOUD_HTTP_URL}  vehicle={VEHICLE_ID}")
    while True:
        try:
            response = await asyncio.to_thread(cloud_control_request, "next")
            cloud_command = response.get("command")
            if cloud_command:
                command_id = int(cloud_command["id"])
                command_name = str(cloud_command["command"]).strip().lower()

                # ACK 上传失败时云端会再次返回同一 ID；只重传 ACK，不重复操作蓝牙。
                if not last_result or last_result["id"] != command_id:
                    print(f"CLOUD CMD #{command_id}: {command_name}")
                    success = await execute_command(client, command_name, command_lock)
                    last_result = {
                        "id": command_id,
                        "status": "applied" if success else "error",
                        "message": "BLE ACK received" if success else "BLE ACK timeout",
                    }

                ack_payload = {
                    **last_result,
                    "serial_port": "BLE:" + BLE_ADDRESS,
                }
                await asyncio.to_thread(cloud_control_request, "ack", ack_payload)
                print(
                    f"CLOUD ACK #{command_id}: "
                    f"{'SUCCESS' if last_result['status'] == 'applied' else 'FAILED'}"
                )
        except error.HTTPError as exc:
            print(f"CLOUD CONTROL HTTP ERROR: {exc.code} {exc.reason}")
        except (error.URLError, socket.timeout) as exc:
            print(f"CLOUD CONTROL OFFLINE: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"CLOUD CONTROL ERROR: {exc}")

        await asyncio.sleep(max(0.2, CLOUD_CONTROL_POLL_SECONDS))


async def command_loop(client: BleakClient, command_lock: asyncio.Lock):
    print("")
    print("Command input ready.")
    print("输入 off  -> 下发 BLE_TX_OFF，停止 S32K344 周期发送")
    print("输入 on   -> 下发 BLE_TX_ON，恢复 S32K344 周期发送")
    print("输入 st   -> 下发 STATUS?，查询当前状态")
    print("输入 q    -> 退出程序")
    print("")

    while True:
        user_cmd = await asyncio.to_thread(input, "CMD> ")
        user_cmd = user_cmd.strip().lower()

        if user_cmd == "off":
            await execute_command(client, "off", command_lock)
        elif user_cmd == "on":
            await execute_command(client, "on", command_lock)
        elif user_cmd == "st":
            await execute_command(client, "st", command_lock)
        elif user_cmd == "q":
            print("退出程序")
            break
        else:
            print("未知输入，请输入 off / on / st / q")
            continue



async def main():
    print("Connecting BLE device...")
    print("BLE address:", BLE_ADDRESS)

    async with BleakClient(BLE_ADDRESS, timeout=30.0, pair=False) as client:
        print("Connected:", client.is_connected)

        print("Start notify:", NOTIFY_UUID)
        await client.start_notify(NOTIFY_UUID, on_notify)

        print("BLE -> UDP gateway running...")
        print(f"UDP target: {UDP_IP}:{UDP_PORT}")
        print("Press Ctrl+C to stop.")

        command_lock = asyncio.Lock()
        cloud_task = asyncio.create_task(cloud_control_loop(client, command_lock))
        try:
            await command_loop(client, command_lock)
        finally:
            cloud_task.cancel()
            await asyncio.gather(cloud_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
