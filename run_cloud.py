"""PyCharm 直接运行：启动云端服务与监控网页。"""

import os


# ======================== 可修改配置 ========================
HOST = "0.0.0.0"
PORT = 8001

# cpolar 公网监控页面。随机域名在 cpolar 重启后可能变化。
PUBLIC_DASHBOARD_URL = "http://7e1f03fc.r7.nas.cpolar.cn"

# 必须与 run_vehicle.py 中的 INGEST_TOKEN 完全一致。
# 部署到公网前请替换为足够长的随机字符串。
INGEST_TOKEN = "vcl_687Nfse29GsoYlX0j8hPaK4ctMv_5g4nXBeYpy1Obu0"

# 公网页面的独立访问口令，不要与车端上传令牌混用。
DASHBOARD_ACCESS_TOKEN = "view_GJEhQFY45cXFdZ2NA2MVex8m"

# 超过多少秒没有收到车端数据，判定车辆离线。
OFFLINE_AFTER_SECONDS = 20

# 每个车辆保留 5 分钟 1 秒 fMP4 片段，观看端短时变慢时继续按顺序补发，
# 不因延迟增加而跳到最新画面。可用环境变量覆盖。
FMP4_BUFFER_SEGMENTS = int(os.getenv("FMP4_BUFFER_SEGMENTS", "300"))

# 高德开放平台申请的“Web 端（JS API）”Key 与安全密钥。
# 本机/PyCharm 调试可直接填在引号内；环境变量存在时会覆盖这里的值。
# 生产环境建议把安全密钥放在反向代理中，并只设置 AMAP_SERVICE_HOST。
AMAP_JS_KEY = os.getenv("AMAP_JS_KEY", "e42382dff12a247360729d11fdfc39c0")  # 例如："你的 Web 端 Key"
AMAP_SECURITY_JS_CODE = os.getenv("AMAP_SECURITY_JS_CODE", "8cdd0c19bda74e5c369bfce4283fa11a")  # 例如："你的安全密钥"
AMAP_SERVICE_HOST = os.getenv("AMAP_SERVICE_HOST", "")
# ===========================================================


# server.py 在导入时读取这些配置，因此必须先设置环境变量。
os.environ["VEHICLE_INGEST_TOKEN"] = INGEST_TOKEN
os.environ["DASHBOARD_ACCESS_TOKEN"] = DASHBOARD_ACCESS_TOKEN
os.environ["OFFLINE_AFTER_SECONDS"] = str(OFFLINE_AFTER_SECONDS)
os.environ["FMP4_BUFFER_SEGMENTS"] = str(FMP4_BUFFER_SEGMENTS)
os.environ["AMAP_JS_KEY"] = AMAP_JS_KEY
os.environ["AMAP_SECURITY_JS_CODE"] = AMAP_SECURITY_JS_CODE
os.environ["AMAP_SERVICE_HOST"] = AMAP_SERVICE_HOST

import uvicorn


if __name__ == "__main__":
    print("=" * 64)
    print("V-SHIELD 智能网联汽车攻防平台正在启动")
    print("本机访问：http://127.0.0.1:{}".format(PORT))
    print("局域网访问：http://本机IP:{}".format(PORT))
    print("公网访问：{}".format(PUBLIC_DASHBOARD_URL))
    print("公网访问口令：{}".format(DASHBOARD_ACCESS_TOKEN))
    print("请保持此窗口运行；需要实时视频时再启动 run_vehicle.py")
    print("=" * 64)
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
