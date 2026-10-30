#!/usr/bin/env python3
"""Poll cloud attack commands and trigger the local CANoe CAPL node via COM."""

import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request

try:
    import win32com.client
except ImportError:
    print("缺少 pywin32，请先执行：python -m pip install -r canoe_gateway/requirements.txt")
    raise SystemExit(2)


POLL_SECONDS = 0.5
START_TIMEOUT_SECONDS = 20.0
INJECTION_START_TIMEOUT_SECONDS = 3.0
INJECTION_FINISH_TIMEOUT_SECONDS = 8.0


def required_env(name, default=""):
    value = os.environ.get(name, default).strip()
    if not value:
        raise RuntimeError("请设置环境变量 {}".format(name))
    return value


def post_json(url, token, payload, timeout=5.0):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until(predicate, timeout_seconds, description):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError("等待{}超时".format(description))


def canoe_variable(application, variable_name):
    namespace = application.System.Namespaces("VShield")
    return namespace.Variables(variable_name)


def open_canoe(config_path):
    # CANoe is effectively a single automation server on many installations.
    # Reuse an already-open matching configuration instead of blocking while
    # trying to open the same file through a second DispatchEx instance.
    application = win32com.client.Dispatch("CANoe.Application")
    current_config = ""
    try:
        current_config = str(application.Configuration.FullName or "")
    except Exception:
        pass
    same_config = (
        current_config
        and os.path.normcase(os.path.abspath(current_config))
        == os.path.normcase(os.path.abspath(str(config_path)))
    )
    if not same_config:
        if application.Measurement.Running:
            raise RuntimeError(
                "CANoe 正在运行其他配置，请先停止并关闭该配置：{}".format(
                    current_config or "未知配置"
                )
            )
        application.Open(str(config_path))
    if not application.Measurement.Running:
        application.Measurement.Start()
    wait_until(
        lambda: bool(application.Measurement.Running),
        START_TIMEOUT_SECONDS,
        "CANoe Measurement 启动",
    )
    request_variable = canoe_variable(application, "AttackRequest")
    state_variable = canoe_variable(application, "AttackState")
    return application, request_variable, state_variable


def verify_canoe_connection(application, request_variable, state_variable):
    """Raise when CANoe was closed/restarted and the COM proxies are stale."""
    _ = bool(application.Measurement.Running)
    _ = int(request_variable.Value)
    _ = int(state_variable.Value)


def acknowledge(ack_url, token, sequence, status, message=""):
    return post_json(ack_url, token, {
        "sequence": sequence,
        "status": status,
        "error": message,
    })


def main():
    if os.name != "nt":
        raise RuntimeError("CANoe 代理必须运行在安装 CANoe 的 Windows 电脑上")
    cloud_url = required_env("CLOUD_HTTP_URL").rstrip("/")
    vehicle_id = required_env("VEHICLE_ID", "VHC-001")
    token = required_env("VEHICLE_INGEST_TOKEN")
    config_path = Path(required_env("CANOE_CONFIG_PATH")).expanduser().resolve()
    if not config_path.is_file():
        raise RuntimeError("找不到 CANoe 配置：{}".format(config_path))

    base_url = "{}/api/vehicles/{}/body/canoe-attack".format(cloud_url, vehicle_id)
    next_url = base_url + "/next"
    ack_url = base_url + "/ack"
    print("正在启动 CANoe 并打开：{}".format(config_path))
    application, attack_request, attack_state = open_canoe(config_path)
    print("CANoe Measurement 已运行，开始轮询云端攻击命令")
    last_sequence = 0

    try:
        while True:
            try:
                try:
                    verify_canoe_connection(application, attack_request, attack_state)
                except Exception as exc:
                    print("CANoe 连接已失效，正在自动重连：{}".format(exc))
                    time.sleep(0.5)
                    application, attack_request, attack_state = open_canoe(config_path)
                    print("CANoe 已重新连接，继续轮询云端攻击命令")

                reply = post_json(next_url, token, {"last_sequence": last_sequence})
                command = reply.get("command") if reply.get("pending") else None
                if not command:
                    time.sleep(POLL_SECONDS)
                    continue

                sequence = int(command["sequence"])
                if sequence == last_sequence:
                    time.sleep(POLL_SECONDS)
                    continue
                print("领取攻击命令 SEQ={}，写入 VShield::AttackRequest".format(sequence))
                try:
                    attack_state.Value = 0
                    attack_request.Value = sequence
                    wait_until(
                        lambda: int(attack_state.Value) == 1,
                        INJECTION_START_TIMEOUT_SECONDS,
                        "CAPL 进入注入状态",
                    )
                    acknowledge(ack_url, token, sequence, "injecting")
                    print("SEQ={} 已开始真实 CAN 注入".format(sequence))
                    wait_until(
                        lambda: int(attack_state.Value) in {2, -1},
                        INJECTION_FINISH_TIMEOUT_SECONDS,
                        "CAPL 完成注入并恢复正常报文",
                    )
                    if int(attack_state.Value) == -1:
                        raise RuntimeError("CAPL 返回失败状态")
                    last_sequence = sequence
                    print("SEQ={} 已完成，CAPL 已恢复正常报文".format(sequence))
                except Exception as exc:
                    last_sequence = sequence
                    try:
                        acknowledge(ack_url, token, sequence, "failed", str(exc))
                    except Exception:
                        pass
                    print("SEQ={} 执行失败：{}".format(sequence, exc))
            except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print("云端轮询暂时失败：{}".format(exc))
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nCANoe 代理已停止；CANoe CAPL 会自行完成5秒恢复。")
    finally:
        # Do not stop Measurement here: CAPL owns the five-second safety restore.
        _ = application


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("CANoe 代理启动失败：{}".format(exc))
        sys.exit(1)
