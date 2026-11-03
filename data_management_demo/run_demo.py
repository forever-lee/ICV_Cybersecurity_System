#!/usr/bin/env python3
"""Run the standalone data-management presentation site."""

import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
HOST = os.getenv("DATA_ADMIN_DEMO_HOST", "0.0.0.0")
PORT = int(os.getenv("DATA_ADMIN_DEMO_PORT", "8012"))
PUBLIC_DEMO_URL = "https://71c0a301.r7.nas.cpolar.cn"


def main():
    handler = partial(SimpleHTTPRequestHandler, directory=str(BASE_DIR))
    server = ThreadingHTTPServer((HOST, PORT), handler)
    print("=" * 64)
    print("数据管理后台演示页面正在启动")
    print("本机访问：http://127.0.0.1:{}".format(PORT))
    print("局域网访问：http://本机IP:{}".format(PORT))
    print("公网访问：{}".format(PUBLIC_DEMO_URL))
    print("请确保 cpolar 隧道目标端口为 {}".format(PORT))
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
