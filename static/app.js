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
    cockpitVideoCallbackId: null, cockpitAnimationFrameId: null,
  };

  const COCKPIT_REFERENCE_LIBRARY = {
    lowBeam: {
      label: "近光灯控制",
      steps: [
        { src: "/static/reference-library/low-beam/step-01-system-ready.png", label: "系统上电与灯光控制入口", source: "上汽大通 V90 使用手册" },
        { src: "/static/reference-library/low-beam/step-02-open-light-settings.jpg", label: "进入车辆灯光设置页面", source: "长安 CS55 PLUS 中控灯光界面" },
        { src: "/static/reference-library/low-beam/step-03-auto-light-off.jpg", label: "自动大灯关闭状态", source: "问界 M5 中控灯光界面" },
        { src: "/static/reference-library/low-beam/step-04-low-beam-on.jpeg", label: "近光灯开启操作", source: "深蓝 S7 中控灯光界面" },
        { src: "/static/reference-library/low-beam/step-05-on-state-check.jpg", label: "近光灯开启状态校验", source: "马自达 EZ-60 中控界面" },
        { src: "/static/reference-library/low-beam/step-06-low-beam-off.jpg", label: "近光灯关闭操作", source: "深蓝 S7 中控灯光界面" },
        { src: "/static/reference-library/low-beam/step-07-off-state-check.jpg", label: "近光灯关闭状态校验", source: "深蓝 S05 中控界面" },
      ],
    },
    climate: {
      label: "空调控制",
      steps: [
        { src: "/static/reference-library/climate/step-01-system-ready.jpg", label: "空调系统初始界面", source: "奇瑞瑞虎 8 中控空调界面" },
        { src: "/static/reference-library/climate/step-02-open-climate.jpg", label: "进入空调控制页面", source: "MG U9 中控空调界面" },
        { src: "/static/reference-library/climate/step-03-temperature-setting.webp", label: "空调温度设置", source: "广汽传祺 GS4 中控空调界面" },
        { src: "/static/reference-library/climate/step-04-fan-setting.jpg", label: "空调风量与模式设置", source: "奇瑞瑞虎 8 PRO 中控空调界面" },
        { src: "/static/reference-library/climate/step-05-state-check.jpg", label: "空调状态校验", source: "雪铁龙 C5 Aircross 中控空调界面" },
      ],
    },
    seatHeating: {
      label: "座椅加热",
      steps: [
        { src: "/static/reference-library/seat-heating/step-01-system-ready.jpg", label: "座椅加热初始界面", source: "CUPRA Born 中控界面" },
        { src: "/static/reference-library/seat-heating/step-02-open-seat-panel.jpg", label: "进入座椅加热页面", source: "CUPRA 中控座椅界面" },
        { src: "/static/reference-library/seat-heating/step-03-heating-level.webp", label: "座椅加热挡位设置", source: "DS 中控座椅界面" },
        { src: "/static/reference-library/seat-heating/step-04-indicator-check.jpg", label: "加热指示状态校验", source: "Chrysler 中控座椅控制界面" },
        { src: "/static/reference-library/seat-heating/step-05-state-check.jpg", label: "座椅加热结果校验", source: "Jeep Compass 中控座椅界面" },
      ],
    },
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

  function renderLegacyCockpitPage() {
    return `
      <div class="cockpit-console">
        <header class="cockpit-masthead">
          <div class="cockpit-brand" aria-label="智能云测">
            <span class="cockpit-cloud-mark"><i></i></span>
            <span><strong>智能云测</strong><small>SMART CLOUD TEST</small></span>
          </div>
          <div class="cockpit-title">
            <p>SMART COCKPIT TEST CLOUD</p>
            <h1>智能网联汽车座舱测试云平台</h1>
            <span>七大高优先级功能组件可视图</span>
          </div>
          <div class="cockpit-head-actions" aria-label="平台快捷操作">
            <button type="button" data-cockpit-action="通知" aria-label="通知">⌁</button>
            <button type="button" data-cockpit-action="帮助" aria-label="帮助">?</button>
            <button type="button" class="cockpit-user" data-cockpit-action="用户中心" aria-label="用户中心">人</button>
          </div>
        </header>

        <section class="cockpit-vehicle-strip" aria-label="测试车辆信息">
          <div class="cockpit-vehicle-meta"><i>VIN</i><span><small>VIN</small><strong>LB8FA24H0R1234567</strong></span></div>
          <div class="cockpit-vehicle-meta"><i>车</i><span><small>车型</small><strong>智行 S7</strong></span></div>
          <div class="cockpit-vehicle-meta"><i>版</i><span><small>座舱版本</small><strong data-domain-field="cockpit.system_version">IVI OS 2.3.1</strong></span></div>
          <div class="cockpit-vehicle-meta"><i>任</i><span><small>测试任务</small><strong>TASK-20240521-003</strong></span></div>
          <div class="cockpit-vehicle-meta online"><i>联</i><span><small>车辆在线</small><strong>在线</strong></span></div>
          <div class="cockpit-vehicle-meta"><i>员</i><span><small>测试人员</small><strong>张伟</strong></span></div>
          <div class="cockpit-vehicle-meta"><i>时</i><span><small>测试时间</small><strong data-cockpit-clock>--:--:--</strong></span></div>
        </section>

        <div class="cockpit-primary-grid">
          <aside class="cockpit-card cockpit-case-panel">
            <div class="cockpit-section-title"><span>02</span><h2>测试用例</h2><button type="button" class="cockpit-add" data-cockpit-action="新建测试用例" aria-label="新建测试用例">+</button></div>
            <div class="cockpit-case-tree">
              <strong>测试用例树</strong>
              <div class="cockpit-case-list">
                <button type="button" data-cockpit-case="TC001"><i class="done">✓</i><b>TC001</b><em class="pass">PASS</em></button>
                <button type="button" data-cockpit-case="TC002"><i class="done">✓</i><b>TC002</b><em class="pass">PASS</em></button>
                <button type="button" class="active" data-cockpit-case="TC003"><i class="running">▶</i><b>TC003</b><em class="run">RUN</em></button>
                <button type="button" data-cockpit-case="TC004"><i></i><b>TC004</b></button>
                <button type="button" data-cockpit-case="TC005"><i></i><b>TC005</b></button>
              </div>
            </div>
          </aside>

          <section class="cockpit-card cockpit-mirror-panel">
            <div class="cockpit-section-title"><span>01</span><h2>中控 / 仪表实时镜像</h2><em><i></i>实时同步</em></div>
            <div class="cockpit-mirror-grid">
              <article class="cockpit-mirror-unit">
                <div class="cockpit-mirror-label"><strong>中控实时镜像</strong><span>[IVI Mirror]</span></div>
                <div class="ivi-screen">
                  <div class="ivi-statusbar"><span>⌂</span><span>●　⌁　4G　14:22</span></div>
                  <div class="ivi-body">
                    <nav class="ivi-rail" aria-label="中控功能">
                      <span class="active"><i>车</i>车辆</span><span><i>光</i>灯光</span><span><i>驾</i>驾驶</span><span><i>A</i>ADAS</span><span><i>设</i>设置</span>
                    </nav>
                    <div class="ivi-settings">
                      <strong>灯光设置</strong>
                      <label><span>自动大灯</span><button type="button" class="cockpit-switch active" data-switch-name="自动大灯" aria-pressed="true"><i></i></button></label>
                      <label><span>近光灯</span><button type="button" class="cockpit-switch active" data-switch-name="近光灯" aria-pressed="true"><i></i></button></label>
                      <label><span>日间行车灯</span><button type="button" class="cockpit-switch active" data-switch-name="日间行车灯" aria-pressed="true"><i></i></button></label>
                      <label><span>转向辅助灯</span><button type="button" class="cockpit-switch" data-switch-name="转向辅助灯" aria-pressed="false"><i></i></button></label>
                    </div>
                    <div class="ivi-car-art" aria-label="车辆正面示意图">
                      <div class="ivi-car-shadow"></div>
                      <div class="ivi-car"><span class="ivi-glass"></span><span class="ivi-light left"></span><span class="ivi-light right"></span><i class="ivi-wheel left"></i><i class="ivi-wheel right"></i><b></b></div>
                    </div>
                  </div>
                  <div class="ivi-dock"><span>⌂</span><span>▦</span><span>车</span><span>‹　24.0°　›</span><span>✣</span><span>24.0°　›</span><span>♩</span></div>
                </div>
              </article>

              <article class="cockpit-mirror-unit">
                <div class="cockpit-mirror-label"><strong>仪表实时镜像</strong><span>[Cluster Mirror]</span></div>
                <div class="cluster-screen">
                  <div class="cluster-frame">
                    <div class="cluster-top"><span>14:22　　25°C</span><strong>P</strong><span class="ready">◉　READY</span></div>
                    <div class="cluster-content">
                      <div class="cluster-charge"><span>100%</span><i><em></em></i><small>0%</small></div>
                      <div class="cluster-speed"><strong>0</strong><span>km/h</span></div>
                      <div class="cluster-car" aria-label="车辆俯视示意图"><span></span><i></i><b></b><em></em></div>
                    </div>
                    <div class="cluster-bottom"><span>续航&nbsp; <b>520</b> km</span><strong>ECO</strong><span>ODO&nbsp; <b>12345</b> km</span></div>
                  </div>
                </div>
              </article>
            </div>
          </section>
        </div>

        <div class="cockpit-workflow-grid">
          <section class="cockpit-card cockpit-steps-card">
            <div class="cockpit-section-title compact"><span>03</span><h2>当前测试步骤</h2></div>
            <ol class="cockpit-step-list">
              <li><span>Step 1</span><b>车辆上电</b><em class="pass">PASS</em></li>
              <li><span>Step 2</span><b>进入车辆设置</b><em class="pass">PASS</em></li>
              <li class="active"><span>Step 3</span><b>打开车灯</b><em class="running">RUNNING</em></li>
              <li><span>Step 4</span><b>检查仪表近光灯图标</b><em>待执行</em></li>
            </ol>
          </section>

          <section class="cockpit-card cockpit-compare-card">
            <div class="cockpit-section-title compact"><span>04</span><h2>Expected / Actual</h2></div>
            <div><p><b>Expected:</b><span>近光灯图标点亮</span></p><p><b>Actual:</b><span>近光灯图标点亮</span></p></div>
          </section>

          <section class="cockpit-card cockpit-result-card">
            <div class="cockpit-section-title compact"><span>06</span><h2>PASS / FAIL</h2></div>
            <div class="cockpit-pass-result"><i>✓</i><strong>PASS</strong></div>
            <p>相似度 <b>99.6%</b></p>
          </section>
        </div>

      </div>`;
  }

  function renderCockpitPage() {
    return `
      <div class="cockpit-domain-shell">
        <div class="page-heading cockpit-domain-heading">
          <div><p class="eyebrow">SMART COCKPIT DOMAIN</p><h1>智能座舱域测试</h1><p>中控摄像头实时画面、中控功能测试用例生成及图像结果比对</p></div>
          <div class="domain-summary"><span class="status-chip active">中控单路监控</span><span class="status-chip">用例智能生成</span></div>
        </div>

        <div class="cockpit-live-layout">
          <aside class="panel cockpit-generator-panel">
            <div class="cockpit-module-head"><span>02</span><div><p class="eyebrow">AI TEST CASE GENERATOR</p><h2>中控用例生成</h2></div><em class="cockpit-prototype-tag">前端原型</em></div>
            <form class="cockpit-generator-form" data-cockpit-generator>
              <label for="cockpitFeatureName">中控大功能名称</label>
              <div class="cockpit-generator-control">
                <input id="cockpitFeatureName" name="featureName" type="text" value="近光灯控制" placeholder="例如：空调控制、座椅加热" maxlength="30" autocomplete="off" />
                <button type="submit"><i>✦</i><span>生成用例</span></button>
              </div>
              <p>输入一个中控功能，生成主流程、状态保持与异常恢复用例。</p>
              <div class="cockpit-generator-presets" aria-label="功能示例">
                <span>快速选择</span>
                <button type="button" data-cockpit-preset="近光灯控制">近光灯控制</button>
                <button type="button" data-cockpit-preset="空调控制">空调控制</button>
                <button type="button" data-cockpit-preset="座椅加热">座椅加热</button>
              </div>
            </form>
            <div class="cockpit-generated-head"><span>生成结果</span><em data-cockpit-case-count>3 条用例</em></div>
            <div class="cockpit-generated-list" data-cockpit-generated-cases>${renderCockpitGeneratedCases("近光灯控制")}</div>
          </aside>

          <section class="panel cockpit-stream-panel">
            <div class="cockpit-module-head cockpit-stream-head"><span>01</span><div><p class="eyebrow">LIVE IVI CAMERA STREAM</p><h2>中控实时镜像</h2></div><em data-cockpit-stream-status><i></i><span data-cockpit-stream-status-label>等待车端视频</span></em></div>
            <div class="cockpit-camera-grid single">
              <article class="cockpit-camera-feed">
                <div class="cockpit-camera-title">
                  <div class="camera-title"><span class="camera-indicator"></span><div><strong>中控采集摄像头 · CAM-IVI-01</strong><small data-cockpit-stream-profile>复用智能驾驶域视频链路 · 等待视频参数</small></div></div>
                  <button type="button" data-cockpit-fullscreen="cockpitIviStage">全屏查看</button>
                </div>
                <div class="cockpit-video-stage" id="cockpitIviStage">
                  <canvas id="cockpitIviCanvas" aria-label="中控摄像头实时视频"></canvas>
                  <div class="cockpit-stream-placeholder" data-cockpit-stream-placeholder><div class="signal-symbol"><span></span><span></span><span></span></div><strong>等待车端摄像头视频流</strong><p>与智能驾驶域共用采集、传输、解码和重连链路</p></div>
                  <div class="cockpit-video-overlay cockpit-overlay-top"><span class="live-tag"><i></i> LIVE</span><span data-cockpit-sequence>SEQ —</span></div>
                  <div class="cockpit-video-overlay cockpit-overlay-bottom"><span data-cockpit-vehicle>CAM-IVI-01 · ${escapeHtml(state.vehicleId)}</span><span data-cockpit-transport>WS / VIDEO</span></div>
                </div>
              </article>
            </div>
          </section>
        </div>

        <div class="cockpit-analysis-grid">
          <section class="panel cockpit-test-steps">
            <div class="cockpit-module-head compact"><span>03</span><div><p class="eyebrow">CURRENT PROCEDURE</p><h2>当前测试步骤</h2></div><em data-cockpit-current-case>近光灯基础功能</em></div>
            <div class="cockpit-step-toolbar">
              <span><small>当前步骤</small><b data-cockpit-current-step>01 · 车辆上电，确认中控屏和车身控制系统正常启动。</b></span>
              <div aria-label="设置当前步骤状态">
                <button type="button" class="running active" data-cockpit-step-status="running">RUNNING</button>
                <button type="button" class="pass" data-cockpit-step-status="pass">PASS</button>
                <button type="button" class="fail" data-cockpit-step-status="fail">FAIL</button>
              </div>
            </div>
            <ol data-cockpit-test-steps>${renderCockpitCaseSteps(buildCockpitTestCases("近光灯控制")[0], 0)}</ol>
          </section>

          <section class="panel cockpit-image-compare-panel">
            <div class="cockpit-module-head compact"><span>04</span><div><p class="eyebrow">IMAGE COMPARISON</p><h2>Expected / Actual</h2></div><em data-cockpit-compare-step>步骤 01 · 测试准备</em></div>
            <div class="cockpit-image-compare">
              <figure>
                <div class="cockpit-shot-frame cockpit-reference-frame expected">
                  <img data-cockpit-expected-image src="/static/reference-library/low-beam/step-01-system-ready.png" alt="近光灯控制步骤 01 标准参考图" />
                  <span>LOCAL REFERENCE</span>
                </div>
                <figcaption><span>EXPECTED</span><b data-cockpit-reference-caption>步骤 01 · 系统上电与灯光控制入口</b></figcaption>
              </figure>
              <figure>
                <div class="cockpit-shot-frame cockpit-reference-frame actual">
                  <img class="hidden" data-cockpit-actual-image alt="摄像头采集到的中控图像" />
                  <div class="cockpit-actual-placeholder" data-cockpit-actual-placeholder><i>CAM</i><strong>等待摄像头采集</strong><small>选择步骤后采集对应的中控画面</small></div>
                  <span>CAMERA CAPTURE</span>
                </div>
                <figcaption>
                  <span>ACTUAL</span><b data-cockpit-actual-caption>步骤 01 · 等待上传或采集</b>
                  <div class="cockpit-image-actions">
                    <input class="hidden" type="file" accept="image/png,image/jpeg,image/webp" data-cockpit-upload-input />
                    <button type="button" data-cockpit-upload>上传 Actual</button>
                    <button type="button" data-cockpit-capture>采集当前画面</button>
                  </div>
                </figcaption>
              </figure>
            </div>
          </section>

          <section class="panel cockpit-verdict-panel" data-cockpit-verdict-panel data-verdict="waiting">
            <div class="cockpit-module-head compact"><span>06</span><div><p class="eyebrow">TEST VERDICT</p><h2>PASS / FAIL</h2></div></div>
            <div class="cockpit-verdict waiting" data-cockpit-verdict>
              <i data-cockpit-verdict-icon>…</i>
              <strong data-cockpit-verdict-status>WAITING</strong>
              <span><span data-cockpit-verdict-label>对比算法启动中</span><b data-cockpit-verdict-score>—</b></span>
              <p data-cockpit-verdict-details>MS-SSIM — · 差异 — · 对齐 —</p>
            </div>
          </section>
        </div>
      </div>`;
  }

  function buildCockpitTestCases(featureName) {
    const feature = String(featureName || "中控功能").trim() || "中控功能";
    if (feature.includes("近光灯")) {
      return [
        {
          id: "TC-AUTO-001", type: "主流程", title: "近光灯基础功能", priority: "P0",
          description: "验证中控近光灯开关的开启、关闭操作及车辆灯光反馈。",
          steps: [
            ["测试准备", "车辆上电，确认中控屏和车身控制系统正常启动。"],
            ["进入页面", "在中控依次打开“车辆设置 > 灯光设置”。"],
            ["设置前置", "关闭“自动大灯”，确认近光灯当前处于熄灭状态。"],
            ["执行开启", "点击“近光灯”开关，将近光灯切换为开启状态。"],
            ["开启校验", "确认中控开关显示已开启，同时车辆近光灯实际点亮。"],
            ["执行关闭", "再次点击“近光灯”开关，将近光灯切换为关闭状态。"],
            ["关闭校验", "确认中控开关显示已关闭，同时车辆近光灯实际熄灭。"],
          ],
        },
        {
          id: "TC-AUTO-002", type: "状态保持", title: "近光灯控制状态保持", priority: "P1",
          description: "验证页面切换和屏幕休眠唤醒后，近光灯状态保持一致。",
          steps: [
            ["测试准备", "车辆上电，关闭自动大灯并进入中控“灯光设置”页面。"],
            ["初始状态", "确认近光灯开关显示关闭，车辆近光灯实际熄灭。"],
            ["设置状态", "点击近光灯开关开启近光灯，并确认灯光已经点亮。"],
            ["切换页面", "离开灯光设置页面，依次进入首页和其他中控功能页面。"],
            ["返回校验", "重新进入灯光设置，确认近光灯开关仍保持开启。"],
            ["休眠唤醒", "关闭中控屏后重新唤醒，再次进入灯光设置页面。"],
            ["一致性校验", "确认中控显示状态与车辆近光灯实际状态始终一致。"],
          ],
        },
        {
          id: "TC-AUTO-003", type: "异常恢复", title: "近光灯控制异常及恢复验证", priority: "P1",
          description: "验证控制链路异常时的提示、状态保护和恢复后重试能力。",
          steps: [
            ["测试准备", "车辆上电并进入灯光设置，确认近光灯初始状态为关闭。"],
            ["注入异常", "模拟中控与车身控制器之间的控制通信短暂中断。"],
            ["异常操作", "在通信中断期间点击近光灯开关，尝试开启近光灯。"],
            ["异常校验", "确认中控显示操作失败或超时提示，且不错误显示为已开启。"],
            ["恢复链路", "恢复中控与车身控制器之间的正常通信。"],
            ["恢复重试", "重新点击近光灯开关，确认控制指令可以正常下发。"],
            ["最终校验", "确认近光灯实际点亮、中控状态同步，且页面无卡死或异常。"],
          ],
        },
      ];
    }

    return [
      {
        id: "TC-AUTO-001", type: "主流程", title: `${feature}基础功能`, priority: "P0",
        description: `验证用户通过中控完成${feature}操作，界面响应与功能状态符合预期。`,
        steps: [
          ["测试准备", "车辆上电，确认中控系统和目标控制模块正常启动。"],
          ["进入功能", `从中控首页进入“${feature}”功能页面。`],
          ["记录初始", `确认并记录${feature}当前显示状态。`],
          ["执行操作", `通过中控完成一次${feature}核心操作。`],
          ["结果校验", `确认界面反馈与${feature}实际执行结果一致。`],
        ],
      },
      {
        id: "TC-AUTO-002", type: "状态保持", title: `${feature}状态保持`, priority: "P1",
        description: `验证${feature}切换后，中控显示、反馈信息与实际状态保持一致。`,
        steps: [
          ["测试准备", `进入${feature}页面并确认系统运行正常。`],
          ["设置状态", `设置一个可识别的${feature}目标状态。`],
          ["切换页面", "离开当前页面并进入其他中控功能。"],
          ["返回功能", `重新进入${feature}页面。`],
          ["状态校验", "确认页面显示、控制模块状态与离开前保持一致。"],
        ],
      },
      {
        id: "TC-AUTO-003", type: "异常恢复", title: `${feature}异常及恢复验证`, priority: "P1",
        description: `验证连续操作或通信短暂异常后，${feature}能够正确提示并恢复。`,
        steps: [
          ["测试准备", `进入${feature}页面并记录初始状态。`],
          ["注入异常", "模拟目标控制模块通信短暂中断。"],
          ["异常操作", `尝试执行一次${feature}操作。`],
          ["异常校验", "确认中控给出失败提示且不会显示错误状态。"],
          ["恢复重试", "恢复通信后重新执行操作，并确认功能恢复正常。"],
        ],
      },
    ];
  }

  function renderCockpitGeneratedCases(featureName) {
    return buildCockpitTestCases(featureName).map((testCase, index) => `
      <button type="button" class="cockpit-generated-case${index === 0 ? " active" : ""}" data-cockpit-case="${escapeHtml(testCase.id)}" data-cockpit-case-title="${escapeHtml(testCase.title)}">
        <span class="cockpit-generated-case-meta"><b>${escapeHtml(testCase.id)}</b><em>${escapeHtml(testCase.type)}</em></span>
        <strong>${escapeHtml(testCase.title)}</strong>
        <p>${escapeHtml(testCase.description)}</p>
        <span class="cockpit-generated-case-foot"><i>中控 UI</i><i>${escapeHtml(testCase.priority)}</i><em>已生成</em></span>
      </button>`).join("");
  }

  function getCockpitReference(featureName) {
    const feature = String(featureName || "");
    if (feature.includes("近光灯") || feature.includes("灯光")) return COCKPIT_REFERENCE_LIBRARY.lowBeam;
    if (feature.includes("座椅") && (feature.includes("加热") || feature.includes("采暖"))) return COCKPIT_REFERENCE_LIBRARY.seatHeating;
    if (feature.includes("空调") || feature.includes("温度") || feature.includes("除霜")) return COCKPIT_REFERENCE_LIBRARY.climate;
    return COCKPIT_REFERENCE_LIBRARY.climate;
  }

  function prepareCockpitCases(featureName) {
    const reference = getCockpitReference(featureName);
    return buildCockpitTestCases(featureName).map((testCase) => ({
      ...testCase,
      stepReferences: testCase.steps.map((_, index) => {
        const stepReference = reference.steps[index % reference.steps.length];
        return {
          ...stepReference,
          featureLabel: reference.label,
          caseTitle: testCase.title,
          stepNumber: index + 1,
        };
      }),
      stepStates: testCase.steps.map((_, index) => index === 0 ? "running" : "waiting"),
      captures: {},
      captureSources: {},
      comparisons: {},
    }));
  }

  function renderCockpitCaseSteps(testCase, selectedIndex = 0) {
    if (!testCase) return "";
    const stateLabels = { waiting: "WAITING", running: "RUNNING", pass: "PASS", fail: "FAIL" };
    return testCase.steps.map(([phase, action], index) => `
      <li class="${index === selectedIndex ? "active " : ""}${escapeHtml(testCase.stepStates?.[index] || (index === 0 ? "running" : "waiting"))}">
        <button type="button" data-cockpit-step-index="${index}">
          <span>${String(index + 1).padStart(2, "0")}</span>
          <b>${escapeHtml(action)}<small>${escapeHtml(phase)}</small></b>
          <em>${stateLabels[testCase.stepStates?.[index] || (index === 0 ? "running" : "waiting")]}</em>
        </button>
      </li>`).join("");
  }

  function renderDomainPages() {
    document.querySelectorAll("[data-domain-page]").forEach((root) => {
      const key = root.dataset.domainPage;
      const config = DOMAIN_CONFIG[key];
      root.classList.toggle("cockpit-page", key === "cockpit");
      if (key === "cockpit") {
        root.innerHTML = renderCockpitPage();
        setupCockpitUI(root);
        return;
      }
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

  function setupCockpitUI(root) {
    const generator = root.querySelector("[data-cockpit-generator]");
    const featureInput = root.querySelector("#cockpitFeatureName");
    const generatedCases = root.querySelector("[data-cockpit-generated-cases]");
    const caseCount = root.querySelector("[data-cockpit-case-count]");
    const stepsPanel = root.querySelector(".cockpit-test-steps");
    const stepsList = root.querySelector("[data-cockpit-test-steps]");
    const currentCaseLabel = root.querySelector("[data-cockpit-current-case]");
    const currentStepLabel = root.querySelector("[data-cockpit-current-step]");
    const statusButtons = [...root.querySelectorAll("[data-cockpit-step-status]")];
    const expectedImage = root.querySelector("[data-cockpit-expected-image]");
    const referenceCaption = root.querySelector("[data-cockpit-reference-caption]");
    const compareStep = root.querySelector("[data-cockpit-compare-step]");
    const actualImage = root.querySelector("[data-cockpit-actual-image]");
    const actualPlaceholder = root.querySelector("[data-cockpit-actual-placeholder]");
    const actualCaption = root.querySelector("[data-cockpit-actual-caption]");
    const captureButton = root.querySelector("[data-cockpit-capture]");
    const uploadButton = root.querySelector("[data-cockpit-upload]");
    const uploadInput = root.querySelector("[data-cockpit-upload-input]");
    const verdictPanel = root.querySelector("[data-cockpit-verdict-panel]");
    const verdict = root.querySelector("[data-cockpit-verdict]");
    const verdictIcon = root.querySelector("[data-cockpit-verdict-icon]");
    const verdictStatus = root.querySelector("[data-cockpit-verdict-status]");
    const verdictLabel = root.querySelector("[data-cockpit-verdict-label]");
    const verdictScore = root.querySelector("[data-cockpit-verdict-score]");
    const verdictDetails = root.querySelector("[data-cockpit-verdict-details]");
    let currentCases = prepareCockpitCases("近光灯控制");
    let currentTestCase = currentCases[0];
    let currentStepIndex = 0;
    let comparisonRequestId = 0;
    let comparisonService = { status: "starting", error: null };

    const metric = (value) => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)}%` : "—";

    const updateVerdict = () => {
      const comparison = currentTestCase.comparisons[currentStepIndex];
      let tone = "waiting";
      let icon = "…";
      let status = "WAITING";
      let label = "采集 Actual 后自动对比";
      let score = "—";
      let details = "MS-SSIM — · 差异 — · 对齐 —";

      if (!comparison && comparisonService.status === "starting") {
        tone = "running";
        icon = "↻";
        status = "STARTING";
        label = "正在预加载图像对比算法";
        details = "服务启动后将常驻等待 Expected / Actual 图像";
      } else if (!comparison && comparisonService.status === "error") {
        tone = "invalid";
        icon = "!";
        status = "OFFLINE";
        label = "图像对比算法启动失败";
        details = comparisonService.error || "请检查 hmi_comparison.py 和 OpenCV 环境";
      } else if (!comparison && comparisonService.status === "ready") {
        label = "算法已就绪，等待上传或采集 Actual 图像";
        details = `${comparisonService.source || "hmi_comparison.py"} · READY · 等待图像`;
      } else if (comparison?.state === "running") {
        tone = "running";
        icon = "↻";
        status = "ANALYZING";
        label = "正在运行图像对比算法";
        details = "正在计算结构、颜色差异与画面对齐";
      } else if (comparison?.state === "error") {
        tone = "invalid";
        icon = "!";
        status = "INVALID";
        label = "对比未完成";
        details = comparison.message || "图像对比服务不可用";
      } else if (comparison?.result) {
        const result = comparison.result;
        status = String(result.status || "INVALID").toUpperCase();
        tone = status === "PASS" ? "pass" : status === "FAIL" ? "fail" : "invalid";
        icon = status === "PASS" ? "✓" : status === "FAIL" ? "×" : "!";
        label = "图像相似度";
        score = metric(result.similarity);
        const alignment = result.alignment || {};
        details = `MS-SSIM ${metric(result.ms_ssim)} · 差异 ${metric(result.difference_ratio)} · 对齐 ${alignment.method || alignment.status || "—"}`;
        if (result.critical_failures?.length) details += ` · ${result.critical_failures[0]}`;
      }

      verdictPanel.dataset.verdict = tone;
      verdict.className = `cockpit-verdict ${tone}`;
      verdictIcon.textContent = icon;
      verdictStatus.textContent = status;
      verdictLabel.textContent = label;
      verdictScore.textContent = score;
      verdictDetails.textContent = details;
    };

    const updateComparison = () => {
      const [phase, action] = currentTestCase.steps[currentStepIndex];
      const stepNumber = String(currentStepIndex + 1).padStart(2, "0");
      const stepReference = currentTestCase.stepReferences[currentStepIndex];
      expectedImage.src = stepReference.src;
      expectedImage.alt = `${stepReference.featureLabel}，${currentTestCase.title}，步骤 ${stepNumber} 标准参考图`;
      referenceCaption.textContent = `步骤 ${stepNumber} · ${stepReference.label}`;
      referenceCaption.title = `${stepReference.source} · ${action}`;
      compareStep.textContent = `步骤 ${stepNumber} · ${phase}`;
      const capturedImage = currentTestCase.captures[currentStepIndex];
      const comparison = currentTestCase.comparisons[currentStepIndex];
      if (capturedImage) {
        actualImage.src = comparison?.result?.annotated_actual || capturedImage;
        actualImage.classList.remove("hidden");
        actualPlaceholder.classList.add("hidden");
        actualCaption.textContent = comparison?.state === "running"
          ? `步骤 ${stepNumber} · 对比中`
          : comparison?.result
            ? `步骤 ${stepNumber} · ${comparison.result.status} 差异标注`
            : comparison?.state === "error"
              ? `步骤 ${stepNumber} · 对比失败`
              : `步骤 ${stepNumber} · 已采集`;
      } else {
        actualImage.removeAttribute("src");
        actualImage.classList.add("hidden");
        actualPlaceholder.classList.remove("hidden");
        actualPlaceholder.querySelector("small").textContent = `步骤 ${stepNumber} · ${phase}`;
        actualCaption.textContent = `步骤 ${stepNumber} · 等待上传或采集`;
      }
      captureButton.disabled = comparison?.state === "running";
      captureButton.textContent = comparison?.state === "running" ? "算法对比中…" : capturedImage ? "重新采集并对比" : "采集当前画面";
      uploadButton.disabled = comparison?.state === "running";
      uploadButton.textContent = comparison?.state === "running" ? "请稍候…" : capturedImage ? "重新上传" : "上传 Actual";
      updateVerdict();
    };

    const renderCurrentStep = () => {
      const [, action] = currentTestCase.steps[currentStepIndex];
      const status = currentTestCase.stepStates[currentStepIndex];
      stepsList.innerHTML = renderCockpitCaseSteps(currentTestCase, currentStepIndex);
      currentStepLabel.textContent = `${String(currentStepIndex + 1).padStart(2, "0")} · ${action}`;
      statusButtons.forEach((button) => button.classList.toggle("active", button.dataset.cockpitStepStatus === status));
      updateComparison();
    };

    const selectStep = (stepIndex) => {
      if (!Number.isInteger(stepIndex) || !currentTestCase.steps[stepIndex]) return;
      currentStepIndex = stepIndex;
      if (currentTestCase.stepStates[stepIndex] === "waiting") {
        currentTestCase.stepStates = currentTestCase.stepStates.map((status) => status === "running" ? "waiting" : status);
        currentTestCase.stepStates[stepIndex] = "running";
      }
      renderCurrentStep();
    };

    const setCurrentStepStatus = (status) => {
      if (!["running", "pass", "fail"].includes(status)) return;
      if (status === "running") {
        currentTestCase.stepStates = currentTestCase.stepStates.map((item) => item === "running" ? "waiting" : item);
      }
      currentTestCase.stepStates[currentStepIndex] = status;
      renderCurrentStep();
      toast(`步骤 ${String(currentStepIndex + 1).padStart(2, "0")} 状态已更新为 ${status.toUpperCase()}`);
    };

    const showCaseSteps = (caseId, revealPanel = false) => {
      const testCase = currentCases.find((item) => item.id === caseId);
      if (!testCase) return;
      currentTestCase = testCase;
      currentStepIndex = 0;
      root.querySelectorAll("[data-cockpit-case]").forEach((item) => item.classList.toggle("active", item.dataset.cockpitCase === caseId));
      currentCaseLabel.textContent = testCase.title;
      currentCaseLabel.title = `${testCase.id} · ${testCase.title}`;
      renderCurrentStep();
      stepsPanel.classList.remove("is-updating");
      void stepsPanel.offsetWidth;
      stepsPanel.classList.add("is-updating");
      if (revealPanel) {
        const panelRect = stepsPanel.getBoundingClientRect();
        if (panelRect.top < 72 || panelRect.bottom > window.innerHeight) {
          stepsPanel.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    };

    const generateCases = (value) => {
      const featureName = String(value || "").trim();
      if (!featureName) {
        featureInput.focus();
        toast("请先输入中控功能名称");
        return;
      }
      featureInput.value = featureName;
      currentCases = prepareCockpitCases(featureName);
      generatedCases.innerHTML = renderCockpitGeneratedCases(featureName);
      caseCount.textContent = "3 条用例";
      showCaseSteps(currentCases[0].id);
      toast(`已生成“${featureName}”测试用例`);
    };

    generator?.addEventListener("submit", (event) => {
      event.preventDefault();
      generateCases(featureInput.value);
    });
    root.querySelectorAll("[data-cockpit-preset]").forEach((button) => button.addEventListener("click", () => {
      generateCases(button.dataset.cockpitPreset);
    }));
    root.addEventListener("click", (event) => {
      const caseButton = event.target.closest("[data-cockpit-case]");
      if (caseButton && root.contains(caseButton)) {
        showCaseSteps(caseButton.dataset.cockpitCase, true);
        toast(`已选择 ${caseButton.dataset.cockpitCase} · ${caseButton.dataset.cockpitCaseTitle || "测试用例"}`);
        return;
      }
      const stepButton = event.target.closest("[data-cockpit-step-index]");
      if (stepButton && root.contains(stepButton)) {
        selectStep(Number(stepButton.dataset.cockpitStepIndex));
        return;
      }
      const statusButton = event.target.closest("[data-cockpit-step-status]");
      if (statusButton && root.contains(statusButton)) {
        setCurrentStepStatus(statusButton.dataset.cockpitStepStatus);
      }
    });
    const runImageComparison = async (actualCapture, sourceLabel) => {
      const targetCase = currentTestCase;
      const targetStepIndex = currentStepIndex;
      const targetReference = targetCase.stepReferences[targetStepIndex];
      const requestId = ++comparisonRequestId;
      targetCase.captures[targetStepIndex] = actualCapture;
      targetCase.captureSources[targetStepIndex] = sourceLabel;
      targetCase.comparisons[targetStepIndex] = { state: "running", requestId };
      targetCase.stepStates[targetStepIndex] = "running";
      renderCurrentStep();
      toast(`步骤 ${String(targetStepIndex + 1).padStart(2, "0")} ${sourceLabel}，正在执行 Expected / Actual 对比`);

      try {
        const response = await fetch(`/api/vehicles/${encodeURIComponent(state.vehicleId)}/cockpit/compare`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_image: targetReference.src,
            actual_image: actualCapture,
            case_id: targetCase.id,
            step_index: targetStepIndex,
          }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || `图像对比请求失败（HTTP ${response.status}）`);
        if (targetCase.comparisons[targetStepIndex]?.requestId !== requestId) return;
        targetCase.comparisons[targetStepIndex] = { state: "complete", requestId, result };
        targetCase.stepStates[targetStepIndex] = result.status === "PASS" ? "pass" : "fail";
        if (currentTestCase === targetCase) renderCurrentStep();
        toast(`步骤 ${String(targetStepIndex + 1).padStart(2, "0")} 对比完成：${result.status} · 相似度 ${metric(result.similarity)}`);
      } catch (error) {
        if (targetCase.comparisons[targetStepIndex]?.requestId !== requestId) return;
        targetCase.comparisons[targetStepIndex] = { state: "error", requestId, message: error.message || "图像对比失败" };
        targetCase.stepStates[targetStepIndex] = "fail";
        if (currentTestCase === targetCase) renderCurrentStep();
        toast(error.message || "图像对比失败");
      }
    };

    captureButton?.addEventListener("click", async () => {
      const cockpitCanvas = root.querySelector("#cockpitIviCanvas");
      if (!cockpitCanvas?.width || !cockpitCanvas.height || !root.querySelector("#cockpitIviStage")?.classList.contains("has-video")) {
        toast("当前还没有可采集的中控摄像头画面");
        return;
      }
      const captureCanvas = document.createElement("canvas");
      captureCanvas.width = cockpitCanvas.width;
      captureCanvas.height = cockpitCanvas.height;
      captureCanvas.getContext("2d").drawImage(cockpitCanvas, 0, 0);
      const actualCapture = captureCanvas.toDataURL("image/jpeg", .92);
      await runImageComparison(actualCapture, "已采集");
    });
    uploadButton?.addEventListener("click", () => uploadInput?.click());
    uploadInput?.addEventListener("change", async () => {
      const file = uploadInput.files?.[0];
      uploadInput.value = "";
      if (!file) return;
      if (!/^image\/(jpeg|png|webp)$/i.test(file.type)) {
        toast("仅支持 JPEG、PNG 或 WebP 图像");
        return;
      }
      if (file.size > 4 * 1024 * 1024) {
        toast("Actual 图像不能超过 4 MB");
        return;
      }
      try {
        const actualImage = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error("无法读取上传图像"));
          reader.readAsDataURL(file);
        });
        await runImageComparison(actualImage, `已上传 ${file.name}`);
      } catch (error) {
        toast(error.message || "Actual 图像上传失败");
      }
    });

    const refreshComparisonService = async () => {
      try {
        const response = await fetch("/api/cockpit/comparison/status", { cache: "no-store" });
        const payload = await response.json().catch(() => ({}));
        comparisonService = response.ok ? payload : { status: "error", error: payload.detail || "无法读取算法状态" };
      } catch (error) {
        comparisonService = { status: "error", error: error.message || "无法连接图像对比服务" };
      }
      renderCurrentStep();
    };
    void refreshComparisonService();
    root.querySelectorAll("[data-cockpit-fullscreen]").forEach((button) => button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.cockpitFullscreen);
      if (!target) return;
      if (document.fullscreenElement) document.exitFullscreen();
      else target.requestFullscreen().catch(() => toast("浏览器未允许全屏显示"));
    }));
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

  function setCockpitStreamConnection(online, label) {
    const status = document.querySelector("[data-cockpit-stream-status]");
    const statusLabel = document.querySelector("[data-cockpit-stream-status-label]");
    if (status) status.classList.toggle("online", online);
    if (statusLabel) statusLabel.textContent = label;
  }

  function updateCockpitStreamDetails({ profile, sequence, capturedAt, transport } = {}) {
    const profileElement = document.querySelector("[data-cockpit-stream-profile]");
    const sequenceElement = document.querySelector("[data-cockpit-sequence]");
    const transportElement = document.querySelector("[data-cockpit-transport]");
    const vehicleElement = document.querySelector("[data-cockpit-vehicle]");
    if (profile && profileElement) profileElement.textContent = profile;
    if (sequence && sequenceElement) sequenceElement.textContent = sequence;
    if (transport && transportElement) transportElement.textContent = transport;
    if (vehicleElement) vehicleElement.textContent = `CAM-IVI-01 · ${state.vehicleId}`;
    const stage = $("cockpitIviStage");
    if (capturedAt && stage) stage.title = `采集时间 ${formatFrameTime(capturedAt)}`;
  }

  function drawCockpitStreamFrame(source, width, height, details = {}) {
    const cockpitCanvas = $("cockpitIviCanvas");
    if (!cockpitCanvas || !width || !height) return false;
    if (cockpitCanvas.width !== width || cockpitCanvas.height !== height) {
      cockpitCanvas.width = width;
      cockpitCanvas.height = height;
    }
    try {
      cockpitCanvas.getContext("2d", { alpha: false, desynchronized: true })
        .drawImage(source, 0, 0, width, height);
    } catch (_) {
      return false;
    }
    $("cockpitIviStage")?.classList.add("has-video");
    document.querySelector("[data-cockpit-stream-placeholder]")?.classList.add("hidden");
    setCockpitStreamConnection(true, "中控画面在线");
    updateCockpitStreamDetails(details);
    return true;
  }

  function stopCockpitVideoMirror() {
    if (state.cockpitVideoCallbackId !== null && typeof video.cancelVideoFrameCallback === "function") {
      video.cancelVideoFrameCallback(state.cockpitVideoCallbackId);
    }
    if (state.cockpitAnimationFrameId !== null) cancelAnimationFrame(state.cockpitAnimationFrameId);
    state.cockpitVideoCallbackId = null;
    state.cockpitAnimationFrameId = null;
  }

  function startCockpitVideoMirror() {
    stopCockpitVideoMirror();
    const mirrorFrame = () => {
      state.cockpitVideoCallbackId = null;
      state.cockpitAnimationFrameId = null;
      if (!state.h264Active || video.paused || video.ended) return;
      if (video.readyState >= 2 && video.videoWidth && video.videoHeight) {
        const metrics = state.latestMetrics;
        drawCockpitStreamFrame(video, video.videoWidth, video.videoHeight, {
          profile: `${video.videoWidth} × ${video.videoHeight} · H.264 · 与智能驾驶域同步`,
          sequence: `SEG ${Number(metrics.sequence || 0).toLocaleString()}`,
          capturedAt: state.latestMediaCreatedAt || Date.now(),
          transport: "WS / H.264",
        });
      }
      if (typeof video.requestVideoFrameCallback === "function") {
        state.cockpitVideoCallbackId = video.requestVideoFrameCallback(mirrorFrame);
      } else {
        state.cockpitAnimationFrameId = requestAnimationFrame(mirrorFrame);
      }
    };
    if (typeof video.requestVideoFrameCallback === "function") {
      state.cockpitVideoCallbackId = video.requestVideoFrameCallback(mirrorFrame);
    } else {
      state.cockpitAnimationFrameId = requestAnimationFrame(mirrorFrame);
    }
  }

  function destroyMediaSource() {
    stopCockpitVideoMirror();
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
    startCockpitVideoMirror();
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
    const bufferedSeconds = Number(state.latestMetrics.send_queue_seconds || 0);
    drawCockpitStreamFrame(source, frame.width, frame.height, {
      profile: `${frame.width} × ${frame.height} · JPEG Q${frame.quality} · 与智能驾驶域同步`,
      sequence: `SEQ ${frame.sequence.toLocaleString()}`,
      capturedAt: frame.capturedAt,
      transport: "WS / JPEG",
    });
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
    updateCockpitStreamDetails({
      profile: h264
        ? `${metrics.width || 1280} × ${metrics.height || 720} · H.264 · ${metrics.encoded_fps || 30} FPS · 与智能驾驶域同步`
        : `${metrics.width || 1280} × ${metrics.height || 720} · JPEG Q${metrics.jpeg_quality || "—"} · 与智能驾驶域同步`,
      sequence: `${h264 ? "SEG" : "SEQ"} ${Number(metrics.sequence || 0).toLocaleString()}`,
      transport: h264 ? "WS / H.264" : "WS / JPEG",
    });
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
    setCockpitStreamConnection(online, online ? "中控画面在线" : label);
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
    document.querySelectorAll("[data-cockpit-clock]").forEach((element) => {
      element.textContent = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")} ${now.toLocaleTimeString("zh-CN", { hour12: false })}`;
    });
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
