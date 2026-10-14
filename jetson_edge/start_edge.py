#!/usr/bin/env python3
"""Start and supervise Jetson body, Bluetooth, GPS, WiFi, and camera uplinks.

Run on the Jetson from this directory:
    python3 start_edge.py

All settings can be overridden with environment variables; see README.md.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
NAVIGATION_FILE = BASE_DIR / "navigation_live.json"
BODY_AGENT_PATH = BASE_DIR / "body_edge_agent.py"
BLUETOOTH_AGENT_PATH = BASE_DIR / "bluetooth_edge_agent.py"
RESTART_DELAY_SECONDS = 3
MAX_LOG_BYTES = 20 * 1024 * 1024
DEFAULT_CLOUD_HTTP_URL = "http://3f7b7aec.r7.nas.cpolar.cn"
DEFAULT_CLOUD_WS_URL = "ws://3f7b7aec.r7.nas.cpolar.cn"
DEFAULT_VEHICLE_INGEST_TOKEN = "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0"

@dataclass
class Component:
    name: str
    command: list
    environment: dict
    process: object = None
    log_handle: object = None
    restart_at: float = 0.0

    @property

    
    def log_path(self):
        return LOG_DIR / "{}.log".format(self.name)

    def rotate_log(self):
        path = self.log_path
        if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
            return
        backup = path.with_suffix(".log.1")
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        path.replace(backup)

    def start(self):
        self.rotate_log()
        self.log_handle = open(self.log_path, "a", encoding="utf-8", buffering=1)
        environment = os.environ.copy()
        environment.update(self.environment)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            self.command,
            cwd=str(BASE_DIR),
            env=environment,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print("[START] {} pid={} log={}".format(self.name, self.process.pid, self.log_path))

    def stop(self):
        process = self.process
        if process is None:
            self.close_log()
            return
        if process.poll() is not None and os.name != "posix":
            self.close_log()
            return
        try:
            if os.name == "posix":
                process_group = process.pid
                os.killpg(process_group, signal.SIGTERM)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    process.poll()
                    try:
                        os.killpg(process_group, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    # The Python agent may exit before its FFmpeg/GStreamer
                    # child. Ensure the complete session is not orphaned.
                    os.killpg(process_group, signal.SIGKILL)
                if process.poll() is None:
                    process.wait(timeout=2)
            else:
                process.terminate()
                process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass
        finally:
            self.close_log()

    def close_log(self):
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None


def enabled(name, default="1"):
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def required_environment():
    configured_http_url = os.getenv("CLOUD_HTTP_URL", "").strip()
    cloud_http_url = (configured_http_url or DEFAULT_CLOUD_HTTP_URL).rstrip("/")
    ingest_token = (
        os.getenv("VEHICLE_INGEST_TOKEN", "").strip()
        or DEFAULT_VEHICLE_INGEST_TOKEN
    )
    configured_ws_url = os.getenv("CLOUD_WS_URL", "").strip()
    if configured_ws_url:
        cloud_ws_url = configured_ws_url.rstrip("/")
    elif configured_http_url:
        cloud_ws_url = (
            ("wss://" + cloud_http_url[8:])
            if cloud_http_url.startswith("https://")
            else ("ws://" + cloud_http_url[7:])
            if cloud_http_url.startswith("http://")
            else cloud_http_url
        )
    else:
        cloud_ws_url = DEFAULT_CLOUD_WS_URL
    return cloud_http_url, cloud_ws_url, ingest_token


def check_expected_body_address():
    if not enabled("BODY_ENABLED"):
        return
    expected = os.getenv("BODY_EXPECTED_ETH0_ADDRESS", "192.168.4.100/24").strip()
    configured_interface = os.getenv("BODY_NETWORK_INTERFACE", "").strip()
    if not expected or os.name != "posix":
        return
    ip_command = shutil.which("ip")
    if not ip_command:
        print("[WARN] 无法检查车身域网口：系统中没有 ip 命令；继续启动其他组件")
        return False
    command = [ip_command, "-4", "-o", "addr", "show"]
    if configured_interface:
        command.extend(["dev", configured_interface])
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if result.returncode != 0 or expected not in result.stdout:
        target = configured_interface or "任一网口"
        print("[WARN] {} 未配置期望地址 {}；车身域可能不可用，继续启动其他组件".format(
            target, expected
        ))
        return False
    matching_line = next(
        (line for line in result.stdout.splitlines() if expected in line),
        "",
    )
    fields = matching_line.split()
    detected_interface = fields[1].rstrip(":") if len(fields) > 1 else configured_interface
    print("[CHECK] 车身域网口 {} 地址正确：{}".format(
        detected_interface or "未知", expected
    ))
    return True


def device_ping_targets():
    """Return the vehicle-side devices checked before component startup."""
    camera_urls = (
        os.getenv("CAMERA_1_RTSP_URL", "rtsp://192.168.4.88:8554/main"),
        os.getenv("CAMERA_2_RTSP_URL", "rtsp://192.168.4.89:8554/main"),
    )
    targets = []
    for index, camera_url in enumerate(camera_urls, start=1):
        host = urlsplit(camera_url).hostname
        if host:
            targets.append(("摄像头{}".format(index), host))
    targets.extend((
        ("车身", os.getenv("BODY_BOARD_HOST", "192.168.4.31").strip()),
        ("WiFi", os.getenv("WIFI_DEVICE_HOST", "192.168.4.201").strip()),
        ("蓝牙", os.getenv("BLUETOOTH_DEVICE_HOST", "192.168.4.20").strip()),
    ))
    return [(name, host) for name, host in targets if host]


def check_device_connectivity():
    """Ping vehicle devices, print every result, and continue on failures."""
    targets = device_ping_targets()
    ping_command = shutil.which("ping")
    print("=" * 64)
    print("Jetson 设备网络连通性检查")
    if not ping_command:
        print("[PING WARN] 系统中没有 ping 命令，跳过 {} 个设备检查".format(len(targets)))
        print("=" * 64)
        return {host: False for _name, host in targets}

    timeout_seconds = max(1, int(float(os.getenv("DEVICE_PING_TIMEOUT_SECONDS", "1"))))
    results = {}
    for name, host in targets:
        command = (
            [ping_command, "-c", "1", "-W", str(timeout_seconds), host]
            if os.name == "posix"
            else [ping_command, "-n", "1", "-w", str(timeout_seconds * 1000), host]
        )
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_seconds + 1,
            )
            reachable = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            reachable = False
        results[host] = reachable
        print("[PING {}] {:<8} {}".format("OK" if reachable else "FAIL", name, host))

    reachable_count = sum(1 for reachable in results.values() if reachable)
    print("[PING SUMMARY] 可达 {}/{}；不可达设备不会阻止其他组件启动".format(
        reachable_count, len(targets)
    ))
    print("=" * 64)
    return results


def acquire_instance_lock():
    if os.name != "posix":
        return None
    import fcntl

    handle = open(BASE_DIR / "edge_supervisor.lock", "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("start_edge.py 已经在运行，不能重复启动")
    return handle


def build_components():
    cloud_http_url, cloud_ws_url, ingest_token = required_environment()
    common_cloud_environment = {
        "CLOUD_HTTP_URL": cloud_http_url,
        "CLOUD_WS_URL": cloud_ws_url,
        "VEHICLE_INGEST_TOKEN": ingest_token,
    }
    common_vehicle_environment = dict(common_cloud_environment)
    common_vehicle_environment.update({
        "VIDEO_MODE": os.getenv("VIDEO_MODE", "copy"),
        "RTSP_CODEC": os.getenv("RTSP_CODEC", "h265"),
        "VEHICLE_NAVIGATION_FILE": str(NAVIGATION_FILE),
    })
    for name in ("VIDEO_BUFFER_SECONDS",):
        if os.getenv(name):
            common_vehicle_environment[name] = os.environ[name]

    components = []
    if enabled("BODY_ENABLED"):
        if not BODY_AGENT_PATH.is_file():
            raise SystemExit("缺少车身域程序：{}".format(BODY_AGENT_PATH))
        body_environment = dict(common_cloud_environment)
        body_environment["VEHICLE_ID"] = os.getenv("BODY_VEHICLE_ID", "VHC-001")
        components.append(Component(
            name="body",
            command=[
                sys.executable, "-u", str(BODY_AGENT_PATH),
                "--mode", os.getenv("BODY_INPUT_MODE", "udp"),
                "--host", os.getenv("BODY_UDP_HOST", "0.0.0.0"),
                "--port", os.getenv("BODY_UDP_PORT", "7100"),
                "--board-host", os.getenv("BODY_BOARD_HOST", "192.168.4.31"),
                "--control-port", os.getenv("BODY_CONTROL_PORT", "7101"),
                "--serial-port", os.getenv("BODY_SERIAL_PORT", "/dev/ttyUSB0"),
                "--baudrate", os.getenv("BODY_SERIAL_BAUDRATE", "115200"),
                "--cloud", cloud_http_url,
                "--vehicle-id", body_environment["VEHICLE_ID"],
            ],
            environment=body_environment,
        ))

    if enabled("GPS_ENABLED"):
        components.append(Component(
            name="gps",
            command=[
                sys.executable, "-u", str(BASE_DIR / "navigation_gps.py"),
                "--mode", "serial",
                "--nmea-device", os.getenv("GPS_DEVICE", "/dev/ttyTHS1"),
                "--nmea-baud", os.getenv("GPS_BAUD", "38400"),
                "--output", str(NAVIGATION_FILE),
            ],
            environment=dict(common_cloud_environment),
        ))

    if enabled("WIFI_ENABLED"):
        wifi_environment = dict(common_cloud_environment)
        wifi_environment.update({
            "WIFI_UDP_HOST": os.getenv("WIFI_UDP_HOST", "0.0.0.0"),
            "WIFI_UDP_PORT": os.getenv("WIFI_UDP_PORT", "6001"),
            "VEHICLE_ID": os.getenv(
                "WIFI_VEHICLE_ID", os.getenv("VEHICLE_1_ID", "VHC-001")
            ),
        })
        components.append(Component(
            name="wifi",
            command=[sys.executable, "-u", str(BASE_DIR / "WiFi_Module.py")],
            environment=wifi_environment,
        ))

    if enabled("BLUETOOTH_ENABLED"):
        if not BLUETOOTH_AGENT_PATH.is_file():
            raise SystemExit("缺少蓝牙域程序：{}".format(BLUETOOTH_AGENT_PATH))
        bluetooth_environment = dict(common_cloud_environment)
        bluetooth_environment.update({
            "VEHICLE_ID": os.getenv(
                "BLUETOOTH_VEHICLE_ID", os.getenv("VEHICLE_1_ID", "VHC-001")
            ),
            "BLUETOOTH_UDP_HOST": os.getenv("BLUETOOTH_UDP_HOST", "0.0.0.0"),
            "BLUETOOTH_UDP_PORT": os.getenv("BLUETOOTH_UDP_PORT", "5000"),
        })
        for name in (
            "BLUETOOTH_QUEUE_SIZE",
            "BLUETOOTH_HTTP_TIMEOUT_SECONDS",
            "BLUETOOTH_SOURCE_PREFIX",
        ):
            if os.getenv(name):
                bluetooth_environment[name] = os.environ[name]
        components.append(Component(
            name="bluetooth",
            command=[sys.executable, "-u", str(BLUETOOTH_AGENT_PATH)],
            environment=bluetooth_environment,
        ))

    cameras = [
        ("camera_vhc001", os.getenv("VEHICLE_1_ID", "VHC-001"),
         os.getenv("CAMERA_1_RTSP_URL", "rtsp://192.168.4.88:8554/main")),
        ("camera_vhc002", os.getenv("VEHICLE_2_ID", "VHC-002"),
         os.getenv("CAMERA_2_RTSP_URL", "rtsp://192.168.4.89:8554/main")),
    ]
    for index, (name, vehicle_id, rtsp_url) in enumerate(cameras, start=1):
        if not enabled("CAMERA_{}_ENABLED".format(index)):
            continue
        environment = dict(common_vehicle_environment)
        environment.update({"VEHICLE_ID": vehicle_id, "RTSP_URL": rtsp_url})
        components.append(Component(
            name=name,
            command=[sys.executable, "-u", str(BASE_DIR / "run_vehicle_jetson.py")],
            environment=environment,
        ))
    return components


def main():
    check_expected_body_address()
    check_device_connectivity()
    LOG_DIR.mkdir(exist_ok=True)
    lock_handle = acquire_instance_lock()
    pid_path = BASE_DIR / "edge_supervisor.pid"
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    components = build_components()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print("Jetson 边缘端统一启动：{}".format(
        " + ".join(component.name for component in components) or "无启用组件"
    ))
    try:
        for component in components:
            component.start()
        while not stopping:
            now = time.monotonic()
            for component in components:
                if component.process is None:
                    if now >= component.restart_at:
                        component.start()
                    continue
                return_code = component.process.poll()
                if return_code is None:
                    continue
                print("[EXIT] {} code={}，{} 秒后重启".format(
                    component.name, return_code, RESTART_DELAY_SECONDS
                ))
                component.close_log()
                component.process = None
                component.restart_at = now + RESTART_DELAY_SECONDS
            time.sleep(1)
    finally:
        print("正在停止 Jetson 边缘端组件...")
        for component in reversed(components):
            component.stop()
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        if lock_handle is not None:
            lock_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
