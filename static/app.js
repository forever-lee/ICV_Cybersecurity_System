(() => {
  "use strict";

  const HEADER_SIZE = 29;
  const FMP4_HEADER_SIZE = 17;
  const H264_MIME = 'video/mp4; codecs="avc1.640029"';
  const H264_TARGET_BUFFER_SECONDS = 10;
  const H264_RESUME_BUFFER_SECONDS = 6;
  const VIEW_NAMES = {
    overview: "全局安全态势", telematics: "车联网域", adas: "智能驾驶域",
    chassis: "底盘域", powertrain: "动力域", body: "车身域", cockpit: "智能座舱域",
    attack: "攻击验证", defense: "检测防御", events: "安全事件",
  };
  const DOMAIN_CONFIG = {
    chassis: {
      title: "底盘域安全", en: "CHASSIS DOMAIN", icon: "底",
      subtitle: "制动、转向与车身稳定系统运行数据及车载网络安全监测",
      assets: "EPS / ESC / EBS", surface: "CAN 报文注入、信号重放、诊断越权",
      fields: [["车辆速度", "speed_kph", "— km/h"], ["方向盘转角", "steering_deg", "— °"], ["制动状态", "brake_status", "—"], ["横摆角速度", "yaw_rate", "— °/s"]],
      capabilities: [["CAN", "CAN 异常报文检测", "识别频率突变、越界与伪造帧"], ["UDS", "诊断会话防护", "监测未授权会话与敏感服务"], ["SAFE", "安全状态联动", "异常条件下进入受控降级状态"]],
    },
    powertrain: {
      title: "动力域安全", en: "POWERTRAIN DOMAIN", icon: "动",
      subtitle: "整车控制、电池管理与电驱系统数据及关键控制指令安全监测",
      assets: "VCU / BMS / MCU", surface: "扭矩指令篡改、BMS 数据欺骗、诊断滥用",
      fields: [["电池 SOC", "soc_percent", "— %"], ["输出扭矩", "torque_nm", "— N·m"], ["母线电压", "voltage_v", "— V"], ["电驱温度", "motor_temp_c", "— °C"]],
      capabilities: [["CMD", "关键指令完整性", "校验扭矩与能量控制指令"], ["BMS", "电池数据一致性", "识别电压、温度与 SOC 异常"], ["UDS", "刷写与诊断防护", "限制高风险诊断服务调用"]],
    },
    body: {
      title: "车身域安全", en: "BODY DOMAIN", icon: "身",
      subtitle: "门锁、灯光、无钥匙进入与车身舒适控制系统安全监测",
      assets: "BCM / PEPS / TPMS", surface: "无线重放、非法解锁、车身控制报文伪造",
      fields: [["车门状态", "door_status", "—"], ["锁车状态", "lock_status", "—"], ["胎压状态", "tpms_status", "—"], ["灯光状态", "light_status", "—"]],
      capabilities: [["PEPS", "无钥匙进入防护", "监测中继与重放攻击特征"], ["CAN", "车身控制白名单", "限制异常控制报文与频率"], ["AUD", "敏感操作审计", "记录解锁与远程控制行为"]],
    },
    cockpit: {
      title: "智能座舱域安全", en: "COCKPIT DOMAIN", icon: "舱",
      subtitle: "车机应用、蓝牙互联、信息娱乐与用户数据安全监测",
      assets: "IVI / HU / APP", surface: "应用漏洞、组件暴露、权限滥用、隐私泄露",
      fields: [["系统版本", "system_version", "—"], ["应用数量", "app_count", "—"], ["用户会话", "user_session", "—"], ["存储占用", "storage_percent", "— %"]],
      capabilities: [["APP", "应用运行时检测", "识别高危权限与异常调用"], ["DATA", "敏感数据防护", "监测隐私数据访问与外发"], ["BOOT", "启动与系统完整性", "验证系统镜像及关键组件"]],
    },
  };

  const state = {
    vehicleId: new URLSearchParams(location.search).get("vehicle") || "VHC-001",
    liveSocket: null, metricsSocket: null, reconnectTimer: null,
    frameTimes: [], latencyHistory: [], rateHistory: [], lastFrameAt: 0,
    latestMetrics: {}, currentView: "overview",
    frameQueue: [], decoderBusy: false, playbackTimer: null,
    lastPresentedCaptureAt: 0, lastPlaybackWallAt: 0, playbackLatency: 0,
    playbackStarted: false, bufferingStartedAt: 0,
    amap: null, vehicleMap: null, vehicleMarker: null, routeLine: null,
    routePoints: [], pendingNavigation: null, navigationUpdateId: 0,
    browserNavigation: null, browserLocationStarted: false,
    browserLocationWatchId: null, lastBrowserPosition: null,
    navigationSourceKind: null,
    mediaSource: null, sourceBuffer: null, mediaQueue: [], mediaUrl: null,
    h264Active: false, h264Started: false, latestMediaCreatedAt: 0,
    h264Rebuffering: false,
  };

  const $ = (id) => document.getElementById(id);
  const canvas = $("videoCanvas");
  const video = document.createElement("video");
  video.id = "videoElement";
  video.className = "video-element hidden";
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  $("videoStage").insertBefore(video, canvas);
  const context = canvas.getContext("2d", { alpha: false, desynchronized: true });
  const chart = $("trendChart");
  const chartContext = chart.getContext("2d");

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[char]);
  }

  function renderDomainPages() {
    document.querySelectorAll("[data-domain-page]").forEach((root) => {
      const key = root.dataset.domainPage;
      const config = DOMAIN_CONFIG[key];
      root.innerHTML = `
        <div class="page-heading">
          <div><p class="eyebrow">${config.en}</p><h1>${config.title}</h1><p>${config.subtitle}</p></div>
          <div class="domain-summary"><span class="status-chip" data-domain-chip="${key}">数据接口待接入</span><span class="status-chip">安全能力规划中</span></div>
        </div>
        <div class="domain-hero">
          <article class="panel domain-asset-panel">
            <div class="panel-title"><div><p class="eyebrow">DOMAIN ASSETS</p><h2>域控资产与实时数据</h2></div><span class="tag" data-domain-label="${key}">待接入</span></div>
            <div class="asset-summary">
              <div class="asset-status"><i>${config.icon}</i><span><b>${config.assets}</b><small>${config.subtitle}</small></span><em data-domain-state="${key}">OFFLINE</em></div>
              <div class="domain-fields">${config.fields.map(([label, field, fallback]) => `<div><span>${label}</span><b data-domain-field="${key}.${field}">${fallback}</b></div>`).join("")}</div>
            </div>
          </article>
          <article class="panel domain-security-panel">
            <div class="panel-title"><div><p class="eyebrow">SECURITY CAPABILITY</p><h2>域安全能力规划</h2></div><span class="tag plan">待建设</span></div>
            <div class="security-cap-list">${config.capabilities.map(([icon, title, desc]) => `<div><i>${icon}</i><span><b>${title}</b><small>${desc}</small></span><em>未接入</em></div>`).join("")}</div>
          </article>
        </div>
        <article class="panel domain-data-empty">
          <div><i>↗</i><strong>等待 ${config.title.replace("安全", "")}数据源</strong><p>后端接口已经预留。数据接入后，这里将实时显示资产遥测、通信状态与安全告警；当前攻击面重点为：${config.surface}。</p><code class="endpoint-code">POST /api/vehicles/${escapeHtml(state.vehicleId)}/domains/${key}</code></div>
        </article>`;
    });
  }

  function navigate(view, updateHash = true) {
    if (!VIEW_NAMES[view]) view = "overview";
    state.currentView = view;
    document.querySelectorAll("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === view));
    document.querySelectorAll(".side-link[data-view]").forEach((link) => link.classList.toggle("active", link.dataset.view === view));
    $("currentViewName").textContent = VIEW_NAMES[view];
    document.title = `${VIEW_NAMES[view]} · V-SHIELD`;
    if (updateHash) history.replaceState(null, "", `${location.pathname}?vehicle=${encodeURIComponent(state.vehicleId)}#${view}`);
    closeMobileNav();
    if (view === "adas") requestAnimationFrame(() => {
      resizeChart();
      if (state.vehicleMap) state.vehicleMap.resize();
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function closeMobileNav() {
    $("sidebar").classList.remove("open");
    $("sidebarBackdrop").classList.remove("show");
  }

  function setupNavigation() {
    document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
    document.querySelectorAll("[data-open-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.openView)));
    $("mobileMenu").addEventListener("click", () => { $("sidebar").classList.toggle("open"); $("sidebarBackdrop").classList.toggle("show"); });
    $("sidebarBackdrop").addEventListener("click", closeMobileNav);
    const initial = location.hash.replace("#", "");
    navigate(VIEW_NAMES[initial] ? initial : "overview", false);
  }

  function wsUrl(path) {
    return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`;
  }

  function setMapPlaceholder(title, detail, isError = false) {
    const placeholder = $("mapPlaceholder");
    placeholder.classList.remove("hidden");
    placeholder.classList.toggle("error", isError);
    placeholder.querySelector("strong").textContent = title;
    placeholder.querySelector("p").textContent = detail;
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = resolve;
      script.onerror = () => reject(new Error("script load failed"));
      document.head.appendChild(script);
    });
  }

  async function initializeVehicleMap() {
    try {
      const response = await fetch("/api/config/map");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const config = await response.json();
      if (!config.enabled || !config.key) {
        setMapPlaceholder(
          "高德地图 Key 未配置",
          "请在云端设置 AMAP_JS_KEY，并同时设置 AMAP_SECURITY_JS_CODE 或安全代理地址后重启服务。",
          true,
        );
        return;
      }
      window._AMapSecurityConfig = config.service_host
        ? { serviceHost: config.service_host }
        : { securityJsCode: config.security_js_code || "" };
      const source = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(config.key)}&plugin=AMap.Scale`;
      await loadScript(source);
      if (!window.AMap) throw new Error("AMap unavailable");
      state.amap = window.AMap;
      state.vehicleMap = new window.AMap.Map("vehicleMap", {
        viewMode: "2D",
        zoom: 4,
        center: [104.1954, 35.8617],
        mapStyle: "amap://styles/darkblue",
        resizeEnable: true,
      });
      state.vehicleMap.addControl(new window.AMap.Scale());
      $("mapPlaceholder").classList.add("hidden");
      if (state.pendingNavigation) renderNavigationOnMap(state.pendingNavigation);
    } catch (_) {
      setMapPlaceholder(
        "高德地图加载失败",
        "请检查 Web 端 Key、安全密钥、绑定域名和云端网络，然后刷新页面重试。",
        true,
      );
    }
  }

  function navigationPosition(navigation, callback) {
    const raw = [Number(navigation.longitude), Number(navigation.latitude)];
    if (navigation.coordinate_system === "GCJ02") {
      callback(raw);
      return;
    }
    state.amap.convertFrom(raw, "gps", (status, result) => {
      if (status === "complete" && result && result.info === "ok" && result.locations && result.locations[0]) {
        callback(result.locations[0]);
      } else {
        callback(raw);
      }
    });
  }

  function renderNavigationOnMap(navigation) {
    state.pendingNavigation = navigation;
    if (!state.vehicleMap || !state.amap) return;
    const updateId = ++state.navigationUpdateId;
    navigationPosition(navigation, (position) => {
      if (updateId !== state.navigationUpdateId) return;
      if (!state.vehicleMarker) {
        const markerContent = document.createElement("div");
        markerContent.className = "vehicle-map-marker";
        markerContent.innerHTML = '<i class="vehicle-heading"></i>';
        state.vehicleMarker = new state.amap.Marker({
          position,
          content: markerContent,
          offset: new state.amap.Pixel(-13, -13),
          zIndex: 120,
        });
        state.routeLine = new state.amap.Polyline({
          path: [position],
          strokeColor: "#20d6e7",
          strokeWeight: 4,
          strokeOpacity: 0.72,
          lineJoin: "round",
          showDir: true,
        });
        state.vehicleMap.add([state.routeLine, state.vehicleMarker]);
        state.vehicleMap.setZoomAndCenter(17, position);
      } else {
        state.vehicleMarker.setPosition(position);
        state.vehicleMap.panTo(position);
      }
      const heading = state.vehicleMarker.getContent().querySelector(".vehicle-heading");
      heading.style.transform = `rotate(${Number(navigation.heading_deg || 0)}deg)`;
      state.routePoints.push(position);
      state.routePoints = state.routePoints.slice(-80);
      state.routeLine.setPath(state.routePoints);
    });
  }

  function formatNavigationAge(epoch) {
    if (!epoch) return "—";
    const ageSeconds = Math.max(0, Math.round((Date.now() - Number(epoch)) / 1000));
    return ageSeconds < 2 ? "刚刚更新" : `${ageSeconds} 秒前`;
  }

  function navigationIsFresh(navigation) {
    if (!navigation) return false;
    const capturedAt = Number(navigation.captured_at_ms || 0);
    return navigation.status === "online"
      && capturedAt > 0
      && Date.now() - capturedAt < 15000;
  }

  function distanceMeters(from, to) {
    const radius = 6371000;
    const toRadians = (value) => value * Math.PI / 180;
    const latitude1 = toRadians(from.latitude);
    const latitude2 = toRadians(to.latitude);
    const deltaLatitude = latitude2 - latitude1;
    const deltaLongitude = toRadians(to.longitude - from.longitude);
    const value = Math.sin(deltaLatitude / 2) ** 2
      + Math.cos(latitude1) * Math.cos(latitude2) * Math.sin(deltaLongitude / 2) ** 2;
    return radius * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
  }

  function bearingDegrees(from, to) {
    const toRadians = (value) => value * Math.PI / 180;
    const latitude1 = toRadians(from.latitude);
    const latitude2 = toRadians(to.latitude);
    const deltaLongitude = toRadians(to.longitude - from.longitude);
    const y = Math.sin(deltaLongitude) * Math.cos(latitude2);
    const x = Math.cos(latitude1) * Math.sin(latitude2)
      - Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(deltaLongitude);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }

  function browserNavigationFromPosition(position) {
    const coords = position.coords;
    const current = {
      latitude: Number(coords.latitude),
      longitude: Number(coords.longitude),
      accuracy: Number(coords.accuracy || 0),
      timestamp: Number(position.timestamp || Date.now()),
    };
    let speed = coords.speed != null && Number.isFinite(Number(coords.speed)) && Number(coords.speed) >= 0
      ? Number(coords.speed) * 3.6 : NaN;
    let heading = coords.heading != null && Number.isFinite(Number(coords.heading)) && Number(coords.heading) >= 0
      ? Number(coords.heading) : NaN;
    const previous = state.lastBrowserPosition;
    if (previous && current.timestamp > previous.timestamp) {
      const distance = distanceMeters(previous, current);
      const elapsedSeconds = (current.timestamp - previous.timestamp) / 1000;
      const jitterFloor = Math.max(
        2,
        Math.min(20, Math.max(previous.accuracy, current.accuracy) * 0.35),
      );
      if (!Number.isFinite(speed)) {
        speed = distance <= jitterFloor ? 0 : Math.min(300, distance / elapsedSeconds * 3.6);
      }
      if (!Number.isFinite(heading) && distance > jitterFloor) {
        heading = bearingDegrees(previous, current);
      }
    }
    state.lastBrowserPosition = current;
    return {
      latitude: current.latitude,
      longitude: current.longitude,
      speed_kph: Number.isFinite(speed) ? speed : null,
      heading_deg: Number.isFinite(heading) ? heading : null,
      accuracy_m: current.accuracy,
      coordinate_system: "WGS84",
      source: "PHONE-GEOLOCATION",
      captured_at_ms: current.timestamp,
      status: "online",
    };
  }

  function startBrowserLocationFallback() {
    if (state.browserLocationStarted) return;
    state.browserLocationStarted = true;
    if (!window.isSecureContext) {
      $("navigationStatus").className = "tag stale";
      $("navigationStatus").textContent = "手机定位需要 HTTPS";
      return;
    }
    if (!("geolocation" in navigator)) {
      $("navigationStatus").className = "tag stale";
      $("navigationStatus").textContent = "当前浏览器不支持定位";
      return;
    }
    $("navigationDataSourceTag").textContent = "临时来源：访问设备定位";
    $("navigationStatus").textContent = "等待手机定位授权";
    state.browserLocationWatchId = navigator.geolocation.watchPosition(
      (position) => {
        state.browserNavigation = browserNavigationFromPosition(position);
        if (!navigationIsFresh(state.latestMetrics.navigation)) applyPreferredNavigation();
      },
      (error) => {
        if (navigationIsFresh(state.latestMetrics.navigation)) return;
        const labels = {
          1: "手机定位未授权",
          2: "手机定位暂不可用",
          3: "手机定位超时",
        };
        $("navigationStatus").className = "tag stale";
        $("navigationStatus").textContent = labels[error.code] || "手机定位失败";
      },
      { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 },
    );
  }

  function updateNavigation(navigation, domains) {
    const chassisSpeed = domains && domains.chassis && domains.chassis.speed_kph != null
      ? Number(domains.chassis.speed_kph) : NaN;
    const hasPosition = navigation
      && Number.isFinite(Number(navigation.latitude))
      && Number.isFinite(Number(navigation.longitude));
    const navigationSpeed = hasPosition && navigation.speed_kph != null
      ? Number(navigation.speed_kph) : NaN;
    const speed = Number.isFinite(navigationSpeed) ? navigationSpeed : chassisSpeed;
    const phoneFallback = hasPosition && navigation.source === "PHONE-GEOLOCATION";
    $("vehicleSpeed").textContent = Number.isFinite(speed) ? Math.max(0, speed).toFixed(1) : "—";
    $("speedSource").textContent = Number.isFinite(navigationSpeed)
      ? (phoneFallback ? "访问设备定位" : "边缘端 GNSS / CAN")
      : Number.isFinite(chassisSpeed) ? "底盘域 CAN" : "等待边缘端";
    if (!hasPosition) return;

    const sourceKind = phoneFallback ? "phone" : "edge";
    if (state.navigationSourceKind && state.navigationSourceKind !== sourceKind) {
      state.routePoints = [];
      if (state.vehicleMap) {
        if (state.vehicleMarker) state.vehicleMap.remove(state.vehicleMarker);
        if (state.routeLine) state.vehicleMap.remove(state.routeLine);
      }
      state.vehicleMarker = null;
      state.routeLine = null;
    }
    state.navigationSourceKind = sourceKind;

    const online = navigationIsFresh(navigation);
    $("navigationDataSourceTag").textContent = phoneFallback
      ? "临时来源：访问设备定位"
      : "数据源：边缘端 GNSS / CAN";
    $("vehicleLongitude").textContent = Number(navigation.longitude).toFixed(6);
    $("vehicleLatitude").textContent = Number(navigation.latitude).toFixed(6);
    $("vehicleHeading").textContent = navigation.heading_deg == null ? "—" : `${Number(navigation.heading_deg).toFixed(1)}°`;
    $("vehicleAccuracy").textContent = navigation.accuracy_m == null ? "—" : `± ${Number(navigation.accuracy_m).toFixed(1)} m`;
    $("coordinateSystem").textContent = navigation.coordinate_system === "GCJ02" ? "GCJ-02" : "WGS-84 → GCJ-02";
    $("navigationSource").textContent = `${navigation.source || "GNSS"} · ${navigation.coordinate_system || "WGS84"}`;
    $("navigationTime").textContent = formatFrameTime(navigation.captured_at_ms);
    $("navigationAge").textContent = formatNavigationAge(navigation.captured_at_ms);
    $("navigationPulse").classList.toggle("online", online);
    $("navigationStatus").className = `tag ${online ? "online" : "stale"}`;
    $("navigationStatus").textContent = online
      ? (phoneFallback ? "手机定位在线" : "边缘端定位在线")
      : "定位数据已过期";
    $("navigationHeadingStatus").classList.toggle("active", online);
    $("navigationHeadingStatus").textContent = online ? "实时定位" : "定位已过期";
    renderNavigationOnMap(navigation);
  }

  function preferredNavigation() {
    const edgeNavigation = state.latestMetrics.navigation || null;
    if (navigationIsFresh(edgeNavigation)) return edgeNavigation;
    return state.browserNavigation || edgeNavigation;
  }

  function applyPreferredNavigation() {
    if (!navigationIsFresh(state.latestMetrics.navigation)) startBrowserLocationFallback();
    updateNavigation(preferredNavigation(), state.latestMetrics.domains || {});
  }

  function resetNavigationView() {
    state.pendingNavigation = null;
    state.navigationUpdateId += 1;
    state.routePoints = [];
    if (state.vehicleMap) {
      if (state.vehicleMarker) state.vehicleMap.remove(state.vehicleMarker);
      if (state.routeLine) state.vehicleMap.remove(state.routeLine);
      state.vehicleMap.setZoomAndCenter(4, [104.1954, 35.8617]);
    }
    state.vehicleMarker = null;
    state.routeLine = null;
    state.navigationSourceKind = null;
    $("vehicleSpeed").textContent = "—";
    $("speedSource").textContent = "等待边缘端";
    $("vehicleLongitude").textContent = "—";
    $("vehicleLatitude").textContent = "—";
    $("vehicleHeading").textContent = "—";
    $("vehicleAccuracy").textContent = "—";
    $("coordinateSystem").textContent = "—";
    $("navigationSource").textContent = "GNSS / CAN 数据待接入";
    $("navigationDataSourceTag").textContent = "数据源：边缘端 GNSS / CAN";
    $("navigationTime").textContent = "--:--:--";
    $("navigationAge").textContent = "—";
    $("navigationPulse").classList.remove("online");
    $("navigationStatus").className = "tag";
    $("navigationStatus").textContent = "等待定位数据";
    $("navigationHeadingStatus").classList.remove("active");
    $("navigationHeadingStatus").textContent = "定位待接入";
  }

  function destroyMediaSource() {
    state.mediaQueue = [];
    state.sourceBuffer = null;
    state.mediaSource = null;
    state.h264Active = false;
    state.h264Started = false;
    state.latestMediaCreatedAt = 0;
    state.h264Rebuffering = false;
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.classList.add("hidden");
    canvas.style.display = "block";
    if (state.mediaUrl) URL.revokeObjectURL(state.mediaUrl);
    state.mediaUrl = null;
  }

  function prepareMediaSource() {
    destroyMediaSource();
    state.mediaSource = new MediaSource();
    state.mediaUrl = URL.createObjectURL(state.mediaSource);
    video.src = state.mediaUrl;
    state.mediaSource.addEventListener("sourceopen", () => {
      if (!state.mediaSource || state.mediaSource.readyState !== "open") return;
      try {
        state.sourceBuffer = state.mediaSource.addSourceBuffer(H264_MIME);
        state.sourceBuffer.mode = "segments";
        state.sourceBuffer.addEventListener("updateend", onMediaUpdateEnd);
        appendNextMediaSegment();
      } catch (_) {
        setConnection(false, "H.264 decoder unavailable");
      }
    }, { once: true });
  }

  function enqueueFmp4(event) {
    const buffer = event.data;
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength <= FMP4_HEADER_SIZE) return;
    const view = new DataView(buffer);
    const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
    if (magic !== "FMP4") return;
    const item = {
      kind: view.getUint8(4),
      sequence: view.getUint32(5, false),
      createdAt: Number(view.getBigUint64(9, false)),
      payload: buffer.slice(FMP4_HEADER_SIZE),
    };
    if (item.kind === 0) {
      // FFmpeg or the edge uplink restarted. Rebuild MSE so an old timestamp
      // range cannot leave playback stalled in a discontinuity.
      if (state.h264Started || state.h264Active) prepareMediaSource();
      state.mediaQueue = [];
    }
    state.mediaQueue.push(item);
    // Do not discard an intermediate media segment here. Dropping one creates
    // a timestamp hole in MSE and causes exactly the stop-at-each-segment issue.
    appendNextMediaSegment();
  }

  function appendNextMediaSegment() {
    const sourceBuffer = state.sourceBuffer;
    if (!sourceBuffer || sourceBuffer.updating || !state.mediaQueue.length) return;
    const item = state.mediaQueue.shift();
    state.appendingMedia = item;
    try {
      sourceBuffer.appendBuffer(item.payload);
    } catch (_) {
      state.appendingMedia = null;
      if (state.mediaQueue.length) setTimeout(appendNextMediaSegment, 100);
    }
  }

  function onMediaUpdateEnd() {
    const item = state.appendingMedia;
    state.appendingMedia = null;
    if (item && item.kind === 1) {
      state.latestMediaCreatedAt = item.createdAt;
      state.lastFrameAt = Date.now();
      $("sequenceTag").textContent = `SEG ${item.sequence.toLocaleString()}`;
      $("overlayTimestamp").textContent = formatFrameTime(item.createdAt);
    }
    if (state.sourceBuffer && state.sourceBuffer.buffered.length) {
      const ranges = state.sourceBuffer.buffered;
      const start = ranges.start(0);
      const end = ranges.end(ranges.length - 1);
      const ahead = Math.max(0, end - (video.currentTime || start));
      const bufferedDuration = end - start;
      if (!state.h264Started && bufferedDuration >= H264_TARGET_BUFFER_SECONDS) {
        video.currentTime = Math.max(start, end - Math.min(H264_TARGET_BUFFER_SECONDS, bufferedDuration - 0.2));
        state.h264Started = true;
        state.h264Rebuffering = false;
        video.play().catch(() => {});
      } else if (state.h264Started && state.h264Rebuffering && ahead >= H264_RESUME_BUFFER_SECONDS) {
        state.h264Rebuffering = false;
        video.play().catch(() => {});
      }
      if (state.h264Started && start < video.currentTime - 10 && !state.sourceBuffer.updating) {
        state.sourceBuffer.remove(start, video.currentTime - 8);
        return;
      }
    }
    appendNextMediaSegment();
  }

  video.addEventListener("playing", () => {
    state.h264Active = true;
    state.h264Rebuffering = false;
    video.classList.remove("hidden");
    canvas.style.display = "none";
    $("videoPlaceholder").classList.add("hidden");
    $("videoStage").classList.add("has-video");
    setConnection(true, "H.264 连续播放");
  });

  video.addEventListener("waiting", () => {
    if (!state.h264Started) return;
    state.h264Rebuffering = true;
    setConnection(true, "网络抖动，正在补充连续缓冲");
  });

  video.addEventListener("stalled", () => {
    if (state.h264Started) state.h264Rebuffering = true;
  });

  video.addEventListener("timeupdate", () => {
    if (!state.h264Active || !state.latestMediaCreatedAt) return;
    let ahead = 0;
    if (video.buffered.length) ahead = Math.max(0, video.buffered.end(video.buffered.length - 1) - video.currentTime);
    const latency = Math.max(0, Date.now() - state.latestMediaCreatedAt + ahead * 1000);
    state.playbackLatency = latency;
    $("latencyValue").textContent = Math.round(latency);
    updateLatencyState(latency);
  });

  function connect() {
    clearTimeout(state.reconnectTimer);
    closeSockets();
    resetPlayback();
    setConnection(false, "正在连接");
    const h264Supported = "MediaSource" in window && MediaSource.isTypeSupported(H264_MIME);
    if (h264Supported) prepareMediaSource();
    const livePath = h264Supported ? "/ws/live-fmp4/" : "/ws/live/";
    state.liveSocket = new WebSocket(wsUrl(`${livePath}${encodeURIComponent(state.vehicleId)}`));
    state.liveSocket.binaryType = "arraybuffer";
    state.liveSocket.onmessage = h264Supported ? enqueueFmp4 : enqueueFrame;
    state.liveSocket.onopen = () => setConnection(false, "等待车端");
    state.liveSocket.onclose = scheduleReconnect;
    state.liveSocket.onerror = () => state.liveSocket.close();
    state.metricsSocket = new WebSocket(wsUrl(`/ws/metrics/${encodeURIComponent(state.vehicleId)}`));
    state.metricsSocket.onmessage = (event) => {
      try { updateMetrics(JSON.parse(event.data)); } catch (_) { /* Ignore malformed telemetry. */ }
    };
    state.metricsSocket.onclose = scheduleReconnect;
    state.metricsSocket.onerror = () => state.metricsSocket.close();
  }

  function closeSockets() {
    for (const socket of [state.liveSocket, state.metricsSocket]) {
      if (socket) { socket.onclose = null; socket.close(); }
    }
  }

  function scheduleReconnect() {
    setConnection(false, "连接中断");
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(connect, 1800);
  }

  function enqueueFrame(event) {
    const buffer = event.data;
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength <= HEADER_SIZE) return;
    const view = new DataView(buffer);
    const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
    if (magic !== "VCS1") return;
    const frame = {
      jpeg: buffer.slice(HEADER_SIZE),
      sequence: view.getUint32(4, false),
      capturedAt: Number(view.getBigUint64(8, false)),
      width: view.getUint16(24, false),
      height: view.getUint16(26, false),
      quality: view.getUint8(28),
    };
    state.frameQueue.push(frame);
    if (!state.bufferingStartedAt) state.bufferingStartedAt = performance.now();
    // 浏览器只承担短抖动整形，不再囤积长时间旧画面。
    while (
      state.frameQueue.length > 1
      && frame.capturedAt - state.frameQueue[0].capturedAt > 6000
    ) {
      state.frameQueue.shift();
    }
    schedulePlayback();
  }

  function resetPlayback() {
    destroyMediaSource();
    state.frameQueue = [];
    state.decoderBusy = false;
    state.lastPresentedCaptureAt = 0;
    state.lastPlaybackWallAt = 0;
    state.playbackLatency = 0;
    state.playbackStarted = false;
    state.bufferingStartedAt = 0;
    clearTimeout(state.playbackTimer);
    state.playbackTimer = null;
  }

  function schedulePlayback() {
    if (state.decoderBusy || state.playbackTimer != null || !state.frameQueue.length) return;
    if (!state.playbackStarted) {
      const bufferedDuration = state.frameQueue.length > 1
        ? state.frameQueue[state.frameQueue.length - 1].capturedAt - state.frameQueue[0].capturedAt
        : 0;
      const bufferingTime = performance.now() - state.bufferingStartedAt;
      if (bufferedDuration < 1000 && bufferingTime < 1500) {
        state.playbackTimer = setTimeout(() => {
          state.playbackTimer = null;
          schedulePlayback();
        }, 80);
        return;
      }
      state.playbackStarted = true;
    }
    const nextFrame = state.frameQueue[0];
    let delay = 0;
    if (state.lastPresentedCaptureAt && state.lastPlaybackWallAt) {
      const captureInterval = Math.max(10, Math.min(200, nextFrame.capturedAt - state.lastPresentedCaptureAt));
      const elapsed = performance.now() - state.lastPlaybackWallAt;
      delay = Math.max(0, captureInterval - elapsed);
    }
    state.playbackTimer = setTimeout(playbackTick, delay);
  }

  function playbackTick() {
    state.playbackTimer = null;
    if (state.decoderBusy) return;
    const frame = state.frameQueue.shift();
    if (!frame) return;
    decodeAndDraw(frame);
  }

  async function decodeAndDraw(frame) {
    state.decoderBusy = true;
    const blob = new Blob([frame.jpeg], { type: "image/jpeg" });
    try {
      if ("createImageBitmap" in window) {
        const bitmap = await createImageBitmap(blob);
        drawFrame(bitmap, frame);
        bitmap.close();
      } else {
        await drawFrameWithImage(blob, frame);
      }
    } catch (_) {
      // 损坏帧直接跳过，下一播放节拍会继续使用最新帧。
    } finally {
      state.decoderBusy = false;
      schedulePlayback();
    }
  }

  function drawFrame(source, frame) {
    if (canvas.width !== frame.width || canvas.height !== frame.height) {
      canvas.width = frame.width;
      canvas.height = frame.height;
    }
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    const now = Date.now();
    const latency = Math.max(0, now - frame.capturedAt);
    state.playbackLatency = latency;
    state.lastFrameAt = now;
    state.lastPresentedCaptureAt = frame.capturedAt;
    state.lastPlaybackWallAt = performance.now();
    state.frameTimes.push(now);
    state.frameTimes = state.frameTimes.filter((value) => now - value < 1000);
    $("videoPlaceholder").classList.add("hidden");
    $("videoStage").classList.add("has-video");
    $("sequenceTag").textContent = `SEQ ${frame.sequence.toLocaleString()}`;
    $("overlayTimestamp").textContent = formatFrameTime(frame.capturedAt);
    const bufferedSeconds = Number(state.latestMetrics.send_queue_seconds || 0);
    $("streamProfile").textContent = `${frame.width} × ${frame.height} · JPEG Q${frame.quality} · ${bufferedSeconds.toFixed(1)}s 缓冲`;
    $("latencyValue").textContent = Math.round(latency);
    updateLatencyState(latency);
    setConnection(true, "车端在线");
  }

  function drawFrameWithImage(blob, frame) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      const objectUrl = URL.createObjectURL(blob);
      image.onload = () => { drawFrame(image, frame); URL.revokeObjectURL(objectUrl); resolve(); };
      image.onerror = () => { URL.revokeObjectURL(objectUrl); reject(new Error("jpeg decode failed")); };
      image.src = objectUrl;
    });
  }

  function updateMetrics(metrics) {
    state.latestMetrics = metrics;
    const online = metrics.status === "online";
    if (online) setConnection(true, "车端在线");
    else if (Date.now() - state.lastFrameAt > 3500) setConnection(false, "等待车端");
    const fps = Number(metrics.fps || metrics.encoded_fps || state.frameTimes.length || 0);
    const kbps = Number(metrics.upload_kbps || 0);
    const latency = Math.max(Number(metrics.ingest_latency_ms || 0), state.playbackLatency || 0);
    $("fpsValue").textContent = fps ? fps.toFixed(1) : "—";
    $("bitrateValue").textContent = kbps ? (kbps / 1000).toFixed(2) : "—";
    const h264 = metrics.stream_mode === "h264-fmp4";
    $("qualityLabel").textContent = h264 ? `H.264 ${metrics.bitrate_kbps || "—"} Kbps` : `JPEG Q${metrics.jpeg_quality || "—"}`;
    $("frameSizeLabel").textContent = h264 && metrics.segment_bytes
      ? `${(metrics.segment_bytes / 1024).toFixed(0)} KB/片段`
      : metrics.frame_bytes ? `${(metrics.frame_bytes / 1024).toFixed(0)} KB/帧` : "— KB/帧";
    if (h264) {
      $("streamProfile").textContent = `${metrics.width || 1280} × ${metrics.height || 720} · H.264 High · ${metrics.encoded_fps || 30} FPS · ${H264_TARGET_BUFFER_SECONDS}s 连续缓冲 · 流畅优先`;
    }
    const droppedFrames = Number(metrics.cloud_dropped_frames || 0)
      + Number(metrics.queue_dropped_frames || 0)
      + Number(metrics.stale_dropped_frames || 0);
    $("dropValue").textContent = droppedFrames.toLocaleString();
    $("viewerValue").textContent = metrics.viewer_count || 0;
    $("reconnectValue").textContent = Number(metrics.agent_reconnects || 0) + Number(metrics.capture_reconnects || 0);
    $("overlayTransport").textContent = (metrics.transport || "WS / JPEG").toUpperCase();
    const total = Math.max(1, Number(metrics.received_frames || 0) + droppedFrames);
    const dropRate = droppedFrames / total;
    const health = online ? Math.max(0, Math.round(100 - dropRate * 100 - Math.min(latency / 50, 8))) : 0;
    $("healthValue").textContent = online ? `${health}%` : "离线";
    $("healthBar").style.width = `${health}%`;
    state.latencyHistory.push(latency);
    state.rateHistory.push(kbps / 1000);
    state.latencyHistory = state.latencyHistory.slice(-40);
    state.rateHistory = state.rateHistory.slice(-40);
    updateFpsBars(fps);
    updateDomains(metrics.domains || {});
    applyPreferredNavigation();
    updateBluetooth(metrics.bluetooth || {});
    updateWifi(metrics.wifi || {});
    $("lastRefresh").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    drawChart();
  }

  function updateDomains(domains) {
    let activeCount = 1;
    Object.keys(DOMAIN_CONFIG).forEach((key) => {
      const payload = domains[key] || {};
      const active = payload.status === "active" || payload.status === "online";
      if (active) activeCount += 1;
      document.querySelectorAll(`[data-domain-indicator="${key}"]`).forEach((element) => element.classList.toggle("online", active));
      document.querySelectorAll(`[data-domain-label="${key}"]`).forEach((element) => { element.textContent = active ? "已接入" : "待接入"; });
      document.querySelectorAll(`[data-domain-state="${key}"]`).forEach((element) => { element.textContent = active ? "ONLINE" : "OFFLINE"; element.classList.toggle("online", active); });
      document.querySelectorAll(`[data-domain-chip="${key}"]`).forEach((element) => { element.textContent = active ? "实时数据已接入" : "数据接口待接入"; element.classList.toggle("active", active); });
      for (const [, field, fallback] of DOMAIN_CONFIG[key].fields) {
        const element = document.querySelector(`[data-domain-field="${key}.${field}"]`);
        if (element) element.textContent = payload[field] ?? fallback;
      }
    });
    $("activeDomainCount").textContent = activeCount;
  }

  function updateBluetooth(bluetooth) {
    const latest = bluetooth.latest;
    const online = bluetooth.status === "online" && latest;
    const status = $("bluetoothStatus");
    status.classList.toggle("online", Boolean(online));
    status.classList.toggle("offline", !online);
    status.querySelector("span").textContent = online ? "数据在线" : "等待数据";
    $("bluetoothPacketCount").textContent = Number(bluetooth.packet_count || 0).toLocaleString();
    updateWirelessTotal(bluetooth, state.latestMetrics.wifi || {});
    if (!latest) {
      $("bluetoothText").textContent = "尚未收到蓝牙数据"; $("bluetoothHex").textContent = "—"; $("bluetoothRssi").textContent = "—"; $("bluetoothTime").textContent = "—";
      $("bluetoothHistory").innerHTML = '<div class="history-empty">等待蓝牙模块发送数据…</div>'; return;
    }
    $("bluetoothText").textContent = latest.text || "（空报文）";
    $("bluetoothHex").textContent = latest.hex || "—";
    $("bluetoothRssi").textContent = latest.rssi == null ? "—" : `${latest.rssi} dBm`;
    $("bluetoothTime").textContent = formatFrameTime(latest.received_at_ms);
    $("bluetoothHistory").innerHTML = renderHistory(bluetooth.history, "BLE-UDP", "等待蓝牙模块发送数据…");
  }

  function updateWifi(wifi) {
    const latest = wifi.latest;
    const online = wifi.status === "online" && latest;
    const status = $("wifiStatus");
    status.classList.toggle("online", Boolean(online));
    status.classList.toggle("offline", !online);
    status.querySelector("span").textContent = online ? "数据在线" : "等待数据";
    $("wifiPacketCount").textContent = Number(wifi.packet_count || 0).toLocaleString();
    updateWirelessTotal(state.latestMetrics.bluetooth || {}, wifi);
    if (!latest) {
      $("wifiText").textContent = "尚未收到 WiFi 数据"; $("wifiHex").textContent = "—"; $("wifiRssi").textContent = "—"; $("wifiTime").textContent = "—";
      $("wifiHistory").innerHTML = '<div class="history-empty">等待 WiFi 模块发送数据…</div>'; return;
    }
    $("wifiText").textContent = latest.text || "（空报文）";
    $("wifiHex").textContent = latest.hex || "—";
    $("wifiRssi").textContent = latest.rssi == null ? "—" : `${latest.rssi} dBm`;
    $("wifiTime").textContent = formatFrameTime(latest.received_at_ms);
    $("wifiHistory").innerHTML = renderHistory(wifi.history, "WIFI-UDP", "等待 WiFi 模块发送数据…");
  }

  function updateWirelessTotal(bluetooth, wifi) {
    $("wirelessPacketTotal").textContent = (Number(bluetooth.packet_count || 0) + Number(wifi.packet_count || 0)).toLocaleString();
  }

  function renderHistory(history, source, emptyText) {
    const records = Array.isArray(history) ? history.slice(0, 8) : [];
    return records.map((record) => `
      <div class="history-row ${record.test ? "test" : ""}">
        <span>#${escapeHtml(record.sequence)}</span><span>${escapeHtml(formatFrameTime(record.received_at_ms))}</span>
        <span class="history-source">${escapeHtml(record.source || source)}</span>
        <code title="${escapeHtml(record.hex || "")}">${escapeHtml(record.text || "（空报文）")}</code>
        <span class="history-size">${escapeHtml(record.byte_count || 0)} B</span>
      </div>`).join("") || `<div class="history-empty">${emptyText}</div>`;
  }

  function updateLatencyState(latency) {
    const element = $("latencyState");
    element.className = latency < 15000 ? "good" : "warn";
    element.textContent = latency < 15000 ? "连续播放稳定" : "质量优先缓冲中";
  }

  function setConnection(online, label) {
    $("connectionPill").classList.toggle("online", online);
    $("connectionPill").classList.toggle("offline", !online);
    $("connectionText").textContent = label;
  }

  function updateFpsBars(fps) {
    const values = Array.from({ length: 30 }, (_, index) => Math.max(12, Math.min(100, (fps / 30) * 75 + Math.sin(index * .9 + Date.now() / 500) * 12)));
    $("fpsBars").innerHTML = values.map((height) => `<i style="height:${height.toFixed(0)}%"></i>`).join("");
  }

  function resizeChart() {
    const rect = chart.getBoundingClientRect();
    const ratio = Math.min(devicePixelRatio || 1, 2);
    chart.width = Math.max(1, Math.round(rect.width * ratio));
    chart.height = Math.max(1, Math.round(rect.height * ratio));
    drawChart();
  }

  function drawChart() {
    const width = chart.width; const height = chart.height;
    if (!width || !height) return;
    chartContext.clearRect(0, 0, width, height);
    chartContext.strokeStyle = "rgba(111, 171, 219, .09)"; chartContext.lineWidth = 1;
    for (let i = 1; i < 5; i += 1) { const y = (height / 5) * i; chartContext.beginPath(); chartContext.moveTo(0, y); chartContext.lineTo(width, y); chartContext.stroke(); }
    drawSeries(state.latencyHistory, 20000, "#20d6e7", width, height);
    drawSeries(state.rateHistory, Math.max(20, ...state.rateHistory), "#f2b84b", width, height);
  }

  function drawSeries(values, max, color, width, height) {
    if (values.length < 2) return;
    chartContext.beginPath();
    values.forEach((value, index) => { const x = (index / 39) * width; const y = height - Math.min(1, value / Math.max(1, max)) * height * .82 - height * .08; if (index === 0) chartContext.moveTo(x, y); else chartContext.lineTo(x, y); });
    chartContext.strokeStyle = color; chartContext.lineWidth = Math.max(1.3, devicePixelRatio || 1); chartContext.shadowColor = color; chartContext.shadowBlur = 6; chartContext.stroke(); chartContext.shadowBlur = 0;
  }

  function updateClock() {
    const now = new Date();
    $("clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    $("date").textContent = `${now.getFullYear()}/${String(now.getMonth() + 1).padStart(2, "0")}/${String(now.getDate()).padStart(2, "0")} · CST`;
  }

  function formatFrameTime(epoch) {
    if (!epoch) return "—";
    const date = new Date(epoch);
    return `${date.toLocaleTimeString("zh-CN", { hour12: false })}.${String(date.getMilliseconds()).padStart(3, "0")}`;
  }

  function toast(message) {
    const element = $("toast"); element.textContent = message; element.classList.add("show");
    setTimeout(() => element.classList.remove("show"), 1800);
  }

  $("snapshotButton").addEventListener("click", () => {
    if (!state.lastFrameAt) { toast("当前还没有可保存的画面"); return; }
    if (state.h264Active && video.videoWidth) {
      canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
    }
    const link = document.createElement("a"); link.download = `${state.vehicleId}-${Date.now()}.jpg`; link.href = canvas.toDataURL("image/jpeg", .94); link.click(); toast("当前画面已保存");
  });
  $("fullscreenButton").addEventListener("click", () => { const target = $("videoStage"); if (document.fullscreenElement) document.exitFullscreen(); else target.requestFullscreen().catch(() => toast("浏览器未允许全屏显示")); });
  $("vehicleSelect").addEventListener("change", (event) => {
    state.vehicleId = event.target.value; $("overlayVehicle").textContent = state.vehicleId;
    resetNavigationView();
    history.replaceState(null, "", `${location.pathname}?vehicle=${encodeURIComponent(state.vehicleId)}#${state.currentView}`);
    renderDomainPages(); setupDomainOpenButtons(); connect();
  });

  function setupDomainOpenButtons() {
    document.querySelectorAll(".domain-page [data-open-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.openView)));
  }

  async function createTestData(kind) {
    const button = $(`${kind}TestButton`);
    button.disabled = true; button.textContent = "生成中…";
    try {
      const response = await fetch(`/api/vehicles/${encodeURIComponent(state.vehicleId)}/${kind}/test`, { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json(); const record = data[kind]; const current = state.latestMetrics[kind] || {};
      const next = { ...current, status: "online", packet_count: record.sequence, latest: record, history: [record, ...(current.history || []).filter((item) => item.sequence !== record.sequence)] };
      state.latestMetrics[kind] = next;
      if (kind === "bluetooth") updateBluetooth(next); else updateWifi(next);
      toast(`已生成一条随机${kind === "bluetooth" ? "蓝牙" : " WiFi"}测试数据`);
    } catch (_) { toast("测试数据发送失败，请检查云端服务"); }
    finally { button.disabled = false; button.textContent = "生成测试数据"; }
  }

  $("bluetoothTestButton").addEventListener("click", () => createTestData("bluetooth"));
  $("wifiTestButton").addEventListener("click", () => createTestData("wifi"));
  async function loadVehicles() {
    try {
      const response = await fetch("/api/vehicles"); const data = await response.json(); const select = $("vehicleSelect");
      select.innerHTML = data.vehicles.map((vehicle) => `<option value="${escapeHtml(vehicle.vehicle_id)}">${escapeHtml(vehicle.vehicle_id)}</option>`).join("");
      if (![...select.options].some((option) => option.value === state.vehicleId)) select.add(new Option(state.vehicleId, state.vehicleId));
      select.value = state.vehicleId;
    } catch (_) { /* Default vehicle remains usable. */ }
  }

  renderDomainPages();
  initializeVehicleMap();
  setupNavigation();
  setupDomainOpenButtons();
  setTimeout(() => {
    if (!navigationIsFresh(state.latestMetrics.navigation)) startBrowserLocationFallback();
  }, 1500);
  window.addEventListener("resize", () => { if (state.currentView === "adas") resizeChart(); });
  window.addEventListener("beforeunload", () => {
    closeSockets();
    if (state.browserLocationWatchId !== null && "geolocation" in navigator) {
      navigator.geolocation.clearWatch(state.browserLocationWatchId);
    }
  });
  setInterval(updateClock, 1000);
  setInterval(() => { if (state.lastFrameAt && Date.now() - state.lastFrameAt > 4000) setConnection(false, "画面中断"); }, 1000);
  updateClock();
  $("overlayVehicle").textContent = state.vehicleId;
  loadVehicles().finally(connect);
})();
