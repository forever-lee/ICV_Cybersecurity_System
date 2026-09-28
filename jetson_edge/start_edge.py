#!/usr/bin/env python3
"""Start and supervise Jetson GPS plus both vehicle camera uplinks.

Run on the Jetson from this directory:
    python3 start_edge.py

All settings can be overridden with environment variables; see README.md.
"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
NAVIGATION_FILE = BASE_DIR / "navigation_live.json"
RESTART_DELAY_SECONDS = 3
MAX_LOG_BYTES = 20 * 1024 * 1024


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
        if process is None or process.poll() is not None:
            self.close_log()
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
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
    common_vehicle_environment = {
        "VIDEO_MODE": os.getenv("VIDEO_MODE", "copy"),
        "RTSP_CODEC": os.getenv("RTSP_CODEC", "h265"),
        "VEHICLE_NAVIGATION_FILE": str(NAVIGATION_FILE),
    }
    for name in ("CLOUD_WS_URL", "VEHICLE_INGEST_TOKEN", "VIDEO_BUFFER_SECONDS"):
        if os.getenv(name):
            common_vehicle_environment[name] = os.environ[name]

    components = []
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
            environment={},
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

    print("Jetson 边缘端统一启动：GPS + {} 路摄像头".format(
        sum(component.name.startswith("camera_") for component in components)
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
