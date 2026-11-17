(() => {
  "use strict";

  const GENERIC_REFERENCES = {
    "近光灯控制": [
      ["low-beam/step-01-system-ready.png", "系统上电与灯光控制入口"],
      ["low-beam/step-02-open-light-settings.jpg", "进入车辆灯光设置页面"],
      ["low-beam/step-03-auto-light-off.jpg", "自动大灯关闭状态"],
      ["low-beam/step-04-low-beam-on.jpeg", "近光灯开启操作"],
      ["low-beam/step-05-on-state-check.jpg", "近光灯开启状态校验"],
      ["low-beam/step-06-low-beam-off.jpg", "近光灯关闭操作"],
      ["low-beam/step-07-off-state-check.jpg", "近光灯关闭状态校验"],
    ],
    "空调控制": [
      ["climate/step-01-system-ready.jpg", "空调系统初始界面"],
      ["climate/step-02-open-climate.jpg", "进入空调控制页面"],
      ["climate/step-03-temperature-setting.webp", "空调温度设置"],
      ["climate/step-04-fan-setting.jpg", "空调风量与模式设置"],
      ["climate/step-05-state-check.jpg", "空调状态校验"],
    ],
    "座椅加热": [
      ["seat-heating/step-01-system-ready.jpg", "座椅加热初始界面"],
      ["seat-heating/step-02-open-seat-panel.jpg", "进入座椅加热页面"],
      ["seat-heating/step-03-heating-level.webp", "座椅加热挡位设置"],
      ["seat-heating/step-04-indicator-check.jpg", "加热指示状态校验"],
      ["seat-heating/step-05-state-check.jpg", "座椅加热结果校验"],
    ],
  };

  const REAL_CAR_CASES = [
    {
      id: "TC-HVAC-330-01", title: "解锁主动换气功能开启",
      featureId: "unlock_active_ventilation", signalName: "UnlockActiveVentilation",
      signalLabel: "解锁主动换气", bits: "1:0", mask: "0x03",
      expectedImage: "解锁主动换气功能开启.jpg", captureAtSeconds: 3.0,
      expectedDataHex: "00 20 16 00 02 3E 40 71", expectedRaw: 2,
    },
    {
      id: "TC-HVAC-330-02", title: "解锁降窗通风功能开启",
      featureId: "unlock_window_ventilation", signalName: "UnlockWindowVentilation",
      signalLabel: "解锁降窗通风", bits: "3:2", mask: "0x0C",
      expectedImage: "解锁降窗通风功能开启.jpg", captureAtSeconds: 7.0,
      expectedDataHex: "00 20 1A 00 02 3E 40 71", expectedRaw: 2,
    },
    {
      id: "TC-HVAC-330-03", title: "空调自干燥功能开启",
      featureId: "ac_self_drying", signalName: "ACSelfDrying",
      signalLabel: "空调自干燥", bits: "5:4", mask: "0x30",
      expectedImage: "空调自干燥功能开启.jpg", captureAtSeconds: 9.5,
      expectedDataHex: "00 20 2A 00 02 3E 40 71", expectedRaw: 2,
    },
  ];

  const runtime = {
    root: null,
    vehicleId: () => "VHC-001",
    toast: () => {},
    cases: [],
    caseIndex: 0,
    stepIndex: 0,
    comparisonStatus: { status: "starting" },
    mirrorFrame: null,
    playbackFrame: null,
    flowFrame: null,
    playbackVideo: null,
    videoMode: "live",
    videoCatalog: null,
    videoCatalogPromise: null,
    canCatalog: null,
    canCatalogPromise: null,
    replay: null,
    replayIndex: 0,
    replayStartedAt: 0,
    runId: 0,
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  function expectedImageUrl(fileName) {
    return `/api/cockpit/expected/${encodeURIComponent(fileName)}`;
  }

  function buildRealCarCases() {
    return REAL_CAR_CASES.map((item) => ({
      ...item,
      type: "实车开启",
      references: [{ src: expectedImageUrl(item.expectedImage), label: item.title }],
      captures: [null],
      captureSources: [""],
      comparisons: [null],
      stepStates: ["WAITING"],
      canState: "WAITING",
      canFrame: null,
      runStatus: "WAITING",
      minimumMsSsim: 70,
    }));
  }

  function buildGenericCases(feature) {
    const entries = GENERIC_REFERENCES[feature] || GENERIC_REFERENCES["近光灯控制"];
    const references = entries.map(([file, label]) => ({ src: `/static/reference-library/${file}`, label }));
    return ["标准功能流程", "连续操作稳定性", "状态恢复一致性"].map((title, index) => ({
      id: `TC-${String(index + 1).padStart(3, "0")}`,
      title: `${feature} · ${title}`,
      references,
      captures: Array(references.length).fill(null),
      captureSources: Array(references.length).fill(""),
      comparisons: Array(references.length).fill(null),
      stepStates: Array(references.length).fill("WAITING"),
      canState: "N/A",
      runStatus: "WAITING",
    }));
  }

  function buildCases(feature) {
    const value = String(feature || "");
    return value.includes("实车空调设置") || value.includes("解锁主动换气")
      || value.includes("解锁降窗通风") || value.includes("空调自干燥")
      ? buildRealCarCases() : buildGenericCases(value || "近光灯控制");
  }

  function currentCase() {
    return runtime.cases[runtime.caseIndex];
  }

  function pageTemplate() {
    return `
      <div class="cockpit-console">
        <header class="cockpit-header">
          <div><p>COCKPIT VALIDATION CLOUD</p><h1>智能网联汽车座舱测试云平台</h1></div>
          <div class="cockpit-runtime" data-cockpit-runtime><i></i><span>算法启动中</span></div>
        </header>
        <section class="cockpit-vehicle-bar">
          <div><small>测试车辆</small><strong data-cockpit-vehicle>VHC-001</strong></div>
          <div><small>视频链路</small><strong data-cockpit-stream>等待连接</strong></div>
          <div><small>帧率</small><strong data-cockpit-metric="fps">—</strong></div>
          <div><small>码率</small><strong data-cockpit-metric="bitrate">—</strong></div>
          <div><small>上行延迟</small><strong data-cockpit-metric="latency">—</strong></div>
        </section>
        <div class="cockpit-grid">
          <section class="panel cockpit-builder">
            <div class="cockpit-section-title"><div><p>TEST GENERATOR</p><h2>测试用例</h2></div><span data-cockpit-case-count>3 条用例</span></div>
            <form class="cockpit-generator" data-cockpit-generator>
              <input id="cockpitFeatureName" value="实车空调设置开启" maxlength="40" aria-label="中控功能名称">
              <button type="submit">生成测试</button>
            </form>
            <div class="cockpit-presets">
              <button type="button" data-cockpit-preset="实车空调设置开启">实车空调设置</button>
              ${Object.keys(GENERIC_REFERENCES).map((name) => `<button type="button" data-cockpit-preset="${name}">${name}</button>`).join("")}
            </div>
            <div class="cockpit-case-list" data-cockpit-cases></div>
          </section>
          <section class="panel cockpit-stream-panel">
            <div class="cockpit-section-title cockpit-stream-title">
              <div><p>LIVE / RECORDED HMI</p><h2>中控画面</h2></div>
              <div class="cockpit-video-tools">
                <label>画面来源<select data-cockpit-video-source aria-label="选择中控视频来源"><option value="live">实时摄像头</option></select></label>
                <button type="button" data-cockpit-video-toggle disabled>播放</button>
                <button type="button" data-cockpit-fullscreen>全屏</button>
              </div>
            </div>
            <div class="cockpit-video-stage" id="cockpitIviStage">
              <canvas id="cockpitIviCanvas"></canvas>
              <div class="cockpit-video-empty" data-cockpit-video-empty><i>IVI</i><strong>等待中控视频</strong><small>选择实车视频或等待实时链路</small></div>
              <div class="cockpit-video-evidence" data-cockpit-video-evidence>LIVE · SEQ 0</div>
            </div>
          </section>
        </div>

        <section class="panel cockpit-flow-panel" data-cockpit-flow-state="ready">
          <div class="cockpit-section-title">
            <div><p>FUNCTION-SCOPED CAN FLOW</p><h2>功能报文流 · 实车 BLF 回放</h2></div>
            <span data-cockpit-flow-status>等待开始测试</span>
          </div>
          <div class="cockpit-flow-toolbar">
            <label>测试功能<strong data-cockpit-flow-feature>解锁主动换气</strong></label>
            <label>实车日志<select data-cockpit-flow-log aria-label="选择 BLF 日志"><option value="">正在发现 BLF…</option></select></label>
            <label>回放时间<strong data-cockpit-flow-time>00:00.000</strong></label>
            <label>目标报文<strong>0x330</strong></label>
          </div>
          <div class="cockpit-flow-body">
            <div><small>CURRENT FRAME</small><strong data-cockpit-flow-id>—</strong><em data-cockpit-flow-name>等待回放</em></div>
            <div class="cockpit-flow-data"><small>DATA BYTES</small><code data-cockpit-flow-data>-- -- -- -- -- -- -- --</code></div>
            <div><small>DECODED SIGNAL</small><strong data-cockpit-flow-signal>—</strong><em data-cockpit-flow-value>等待信号</em></div>
            <div><small>DBC / CHANNEL</small><strong data-cockpit-flow-dbc>—</strong><em data-cockpit-flow-channel>—</em></div>
          </div>
          <div class="cockpit-flow-progress"><i data-cockpit-flow-progress></i></div>
        </section>

        <section class="panel cockpit-steps-panel">
          <div class="cockpit-section-title"><div><p>STEP VALIDATION</p><h2 data-cockpit-current-case>测试步骤</h2></div><span data-cockpit-current-step>—</span></div>
          <div class="cockpit-test-runner">
            <button type="button" data-cockpit-start-test><i>▶</i><span>开始测试</span></button>
            <em data-cockpit-run-status>选择用例后，一键同步启动视频与 BLF</em>
          </div>
          <div class="cockpit-step-list" data-cockpit-steps></div>
        </section>

        <div class="cockpit-compare-grid">
          <section class="panel cockpit-image-card">
            <div class="cockpit-section-title"><div><p>EXPECTED</p><h2>标准参考画面</h2></div></div>
            <div class="cockpit-image-stage"><img data-cockpit-expected alt="标准参考画面"></div>
            <p data-cockpit-expected-caption>—</p>
          </section>
          <section class="panel cockpit-image-card">
            <div class="cockpit-section-title"><div><p>ACTUAL</p><h2>自动采集画面</h2></div></div>
            <div class="cockpit-image-stage">
              <img class="hidden" data-cockpit-actual alt="实际中控画面">
              <div class="cockpit-actual-empty" data-cockpit-actual-empty><i>+</i><span>等待动作完成后自动采集</span></div>
            </div>
            <p data-cockpit-actual-caption>等待开始测试</p>
            <div class="cockpit-capture-actions">
              <button type="button" data-cockpit-capture>采集当前画面</button>
              <button type="button" data-cockpit-upload>上传 Actual</button>
              <input class="hidden" type="file" accept="image/jpeg,image/png,image/webp" data-cockpit-upload-input>
            </div>
          </section>
          <section class="panel cockpit-can-card" data-cockpit-can-state="waiting">
            <div class="cockpit-section-title"><div><p>CAN COMPARISON</p><h2>0x330 报文对比</h2></div><span data-cockpit-can-badge>WAITING</span></div>
            <div class="cockpit-can-row"><small>EXPECTED</small><strong data-cockpit-can-expected>—</strong><code data-cockpit-can-expected-data>-- -- -- -- -- -- -- --</code></div>
            <div class="cockpit-can-row"><small>ACTUAL · BLF</small><strong data-cockpit-can-actual>—</strong><code data-cockpit-can-actual-data>-- -- -- -- -- -- -- --</code></div>
            <p data-cockpit-can-detail>等待 BLF 回放与目标信号</p>
          </section>
          <section class="panel cockpit-verdict-card" data-cockpit-verdict="waiting">
            <div class="cockpit-section-title"><div><p>RESULT</p><h2>联合判定</h2></div></div>
            <div class="cockpit-verdict-icon" data-cockpit-verdict-icon>…</div>
            <strong data-cockpit-verdict-status>WAITING</strong>
            <span data-cockpit-verdict-label>等待图像与 CAN 证据</span>
            <p data-cockpit-verdict-details>图像 WAITING · CAN WAITING</p>
          </section>
        </div>
      </div>`;
  }

  function renderCases() {
    const root = runtime.root;
    root.querySelector("[data-cockpit-case-count]").textContent = `${runtime.cases.length} 条用例`;
    root.querySelector("[data-cockpit-cases]").innerHTML = runtime.cases.map((item, index) => `
      <button type="button" class="${index === runtime.caseIndex ? "active" : ""}" data-cockpit-case="${index}">
        <small>${escapeHtml(item.id)}</small><strong>${escapeHtml(item.title)}</strong><span>${Number.isFinite(item.captureAtSeconds) ? `${item.captureAtSeconds.toFixed(1)}s 自动采集` : `${item.references.length} 步`}</span>
      </button>`).join("");
  }

  function signalFromFrame(frame, signalName) {
    if (!frame) return null;
    if (Array.isArray(frame.signals)) return frame.signals.find((signal) => signal.name === signalName) || null;
    return frame.signals?.[signalName] || null;
  }

  function signalRaw(frame, signalName) {
    const signal = signalFromFrame(frame, signalName);
    return Number(signal?.raw_value ?? signal?.value ?? signal?.physical_value);
  }

  function renderCanPanel(testCase) {
    const root = runtime.root;
    const badge = root.querySelector("[data-cockpit-can-badge]");
    const card = root.querySelector("[data-cockpit-can-state]");
    if (!testCase.featureId) {
      card.dataset.cockpitCanState = "na";
      badge.textContent = "N/A";
      root.querySelector("[data-cockpit-can-expected]").textContent = "当前通用用例不要求 CAN";
      root.querySelector("[data-cockpit-can-detail]").textContent = "仅执行图像比对";
      return;
    }
    const state = String(testCase.canState || "WAITING").toUpperCase();
    card.dataset.cockpitCanState = state.toLowerCase();
    badge.textContent = state === "PASS" ? "MATCH" : state;
    root.querySelector("[data-cockpit-can-expected]").textContent = `${testCase.signalName} = raw ${testCase.expectedRaw} · 开启 · mask ${testCase.mask}`;
    root.querySelector("[data-cockpit-can-expected-data]").textContent = testCase.expectedDataHex;
    root.querySelector("[data-cockpit-can-actual]").textContent = testCase.canFrame
      ? `${testCase.signalName} = raw ${signalRaw(testCase.canFrame, testCase.signalName)}` : "—";
    root.querySelector("[data-cockpit-can-actual-data]").textContent = testCase.canFrame?.data_hex || "-- -- -- -- -- -- -- --";
    root.querySelector("[data-cockpit-can-detail]").textContent = state === "PASS"
      ? `实车 BLF 已确认 0x330 / ${testCase.signalName} = ${testCase.expectedRaw}`
      : state === "FAIL" ? "实车 BLF 中未找到期望开启状态" : state === "LISTENING" ? "BLF 同步回放中，等待目标信号" : "等待开始测试";
  }

  function renderVerdict(testCase) {
    const root = runtime.root;
    const card = root.querySelector("[data-cockpit-verdict]");
    const icon = root.querySelector("[data-cockpit-verdict-icon]");
    const status = root.querySelector("[data-cockpit-verdict-status]");
    const label = root.querySelector("[data-cockpit-verdict-label]");
    const details = root.querySelector("[data-cockpit-verdict-details]");
    const comparison = testCase.comparisons[runtime.stepIndex];
    const imageState = comparison?.state === "running" ? "RUNNING" : comparison?.result?.status || (comparison?.state === "error" ? "FAIL" : "WAITING");
    const canState = testCase.featureId ? String(testCase.canState || "WAITING").toUpperCase() : "N/A";
    let tone = "waiting";
    let finalStatus = "WAITING";
    let finalLabel = "等待图像与 CAN 证据";
    if (imageState === "RUNNING" || canState === "LISTENING") {
      tone = "running";
      finalStatus = "RUNNING";
      finalLabel = "自动测试正在执行";
    }
    if (testCase.runStatus === "PASS") {
      tone = "pass";
      finalStatus = "PASS";
      finalLabel = testCase.featureId ? "图像 + CAN 联合判定通过" : "图像判定通过";
    } else if (testCase.runStatus === "FAIL" || imageState === "FAIL" || canState === "FAIL") {
      tone = "fail";
      finalStatus = "FAIL";
      finalLabel = "测试证据未通过";
    }
    card.dataset.cockpitVerdict = tone;
    icon.textContent = tone === "pass" ? "✓" : tone === "fail" ? "×" : tone === "running" ? "↻" : "…";
    status.textContent = finalStatus;
    label.textContent = finalLabel;
    details.textContent = `图像 ${imageState} · CAN ${canState}`;
  }

  function renderStep() {
    const root = runtime.root;
    const testCase = currentCase();
    if (!root || !testCase) return;
    const reference = testCase.references[runtime.stepIndex];
    const comparison = testCase.comparisons[runtime.stepIndex];
    const capture = testCase.captures[runtime.stepIndex];
    root.querySelector("[data-cockpit-current-case]").textContent = testCase.title;
    root.querySelector("[data-cockpit-current-step]").textContent = `步骤 ${String(runtime.stepIndex + 1).padStart(2, "0")}`;
    root.querySelector("[data-cockpit-steps]").innerHTML = testCase.references.map((item, index) => {
      const status = testCase.stepStates[index] || "WAITING";
      return `<button type="button" class="${index === runtime.stepIndex ? "active" : ""}" data-cockpit-step="${index}"><i>${String(index + 1).padStart(2, "0")}</i><span>${escapeHtml(item.label)}</span><em data-status="${status}">${status}</em></button>`;
    }).join("");
    const expected = root.querySelector("[data-cockpit-expected]");
    expected.src = reference.src;
    root.querySelector("[data-cockpit-expected-caption]").textContent = `步骤 ${runtime.stepIndex + 1} · ${reference.label}`;
    const actual = root.querySelector("[data-cockpit-actual]");
    const empty = root.querySelector("[data-cockpit-actual-empty]");
    if (capture) {
      actual.src = capture;
      actual.classList.remove("hidden");
      empty.classList.add("hidden");
      root.querySelector("[data-cockpit-actual-caption]").textContent = `步骤 ${runtime.stepIndex + 1} · ${testCase.captureSources[runtime.stepIndex]} · ${comparison?.result?.status || "READY"}`;
    } else {
      actual.removeAttribute("src");
      actual.classList.add("hidden");
      empty.classList.remove("hidden");
      root.querySelector("[data-cockpit-actual-caption]").textContent = "等待开始测试";
    }
    const startButton = root.querySelector("[data-cockpit-start-test]");
    startButton.disabled = !Number.isFinite(testCase.captureAtSeconds) || root.classList.contains("cockpit-test-running");
    startButton.querySelector("span").textContent = testCase.runStatus === "PASS" ? "重新测试" : root.classList.contains("cockpit-test-running") ? "测试进行中…" : "开始测试";
    if (!Number.isFinite(testCase.captureAtSeconds)) root.querySelector("[data-cockpit-run-status]").textContent = "通用用例使用手动采集或上传 Actual";
    root.querySelector("[data-cockpit-flow-feature]").textContent = testCase.signalLabel || "通用功能";
    renderCanPanel(testCase);
    renderVerdict(testCase);
  }

  function drawVideoFrame(source, width, height, evidence) {
    const root = runtime.root;
    const canvas = root.querySelector("#cockpitIviCanvas");
    if (!source || !width || !height) return;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    canvas.getContext("2d", { alpha: false }).drawImage(source, 0, 0, width, height);
    root.querySelector("#cockpitIviStage").classList.add("has-video");
    root.querySelector("[data-cockpit-video-empty]").classList.add("hidden");
    root.querySelector("[data-cockpit-video-evidence]").textContent = evidence;
  }

  function stopLiveMirror() {
    if (runtime.mirrorFrame !== null) cancelAnimationFrame(runtime.mirrorFrame);
    runtime.mirrorFrame = null;
  }

  function mirrorLiveFrame() {
    const root = runtime.root;
    if (!root?.isConnected || runtime.videoMode !== "live") {
      runtime.mirrorFrame = null;
      return;
    }
    const sourceVideo = document.getElementById("videoElement");
    const sourceCanvas = document.getElementById("videoCanvas");
    if (sourceVideo?.readyState >= 2 && sourceVideo.videoWidth) {
      drawVideoFrame(sourceVideo, sourceVideo.videoWidth, sourceVideo.videoHeight, "LIVE · 实时中控");
    } else if (sourceCanvas?.width && sourceCanvas.height) {
      drawVideoFrame(sourceCanvas, sourceCanvas.width, sourceCanvas.height, "LIVE · 实时中控");
    }
    runtime.mirrorFrame = requestAnimationFrame(mirrorLiveFrame);
  }

  function stopPlaybackMirror() {
    if (runtime.playbackFrame !== null) cancelAnimationFrame(runtime.playbackFrame);
    runtime.playbackFrame = null;
  }

  function mirrorPlaybackFrame() {
    const video = runtime.playbackVideo;
    if (!runtime.root?.isConnected || runtime.videoMode !== "file" || !video || video.paused || video.ended) {
      runtime.playbackFrame = null;
      return;
    }
    if (video.readyState >= 2) drawVideoFrame(video, video.videoWidth, video.videoHeight, `VIDEO ${formatClock(video.currentTime * 1000)}`);
    runtime.playbackFrame = requestAnimationFrame(mirrorPlaybackFrame);
  }

  function waitForEvent(target, eventName, timeoutMs, errorMessage) {
    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => reject(new Error(errorMessage)), timeoutMs);
      target.addEventListener(eventName, () => {
        clearTimeout(timeoutId);
        resolve();
      }, { once: true });
    });
  }

  async function loadVideoCatalog() {
    if (runtime.videoCatalog) return runtime.videoCatalog;
    const response = await fetch("/api/cockpit/videos", { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "实车视频目录不可用");
    runtime.videoCatalog = Array.isArray(payload.videos) ? payload.videos : [];
    const select = runtime.root.querySelector("[data-cockpit-video-source]");
    select.innerHTML = '<option value="live">实时摄像头</option>' + runtime.videoCatalog.map((item) => `<option value="${escapeHtml(item.url)}">实车视频 · ${escapeHtml(item.name)}</option>`).join("");
    if (!runtime.videoCatalog.length) select.insertAdjacentHTML("beforeend", '<option value="" disabled>未发现实车视频</option>');
    return runtime.videoCatalog;
  }

  function waitForVideoData(video) {
    if (video.readyState >= 2 && video.videoWidth) return Promise.resolve();
    return waitForEvent(video, "loadeddata", 6000, "实车视频载入超时");
  }

  async function seekVideo(seconds) {
    const video = runtime.playbackVideo;
    await waitForVideoData(video);
    if (Math.abs(video.currentTime - seconds) <= 0.04) return;
    const ready = waitForEvent(video, "seeked", 4000, "实车视频定位超时");
    video.currentTime = seconds;
    await ready;
  }

  async function selectRecordedVideo({ autoplay = false, selectedUrl = "" } = {}) {
    const videos = await (runtime.videoCatalogPromise || loadVideoCatalog());
    if (!videos.length) throw new Error("实车测试视频目录中没有可播放文件");
    const item = videos.find((candidate) => candidate.url === selectedUrl) || videos[0];
    const video = runtime.playbackVideo;
    runtime.videoMode = "file";
    stopLiveMirror();
    stopPlaybackMirror();
    video.pause();
    video.dataset.fileName = item.name;
    const absolute = new URL(item.url, location.href).href;
    if (video.src !== absolute) {
      video.src = item.url;
      video.load();
    }
    runtime.root.querySelector("[data-cockpit-video-source]").value = item.url;
    await waitForVideoData(video);
    await seekVideo(0);
    drawVideoFrame(video, video.videoWidth, video.videoHeight, "TEST 00:00.000");
    if (autoplay) await video.play();
    updateVideoToggle();
    return video;
  }

  function updateVideoToggle() {
    const button = runtime.root?.querySelector("[data-cockpit-video-toggle]");
    if (!button) return;
    button.disabled = runtime.videoMode !== "file";
    button.textContent = runtime.playbackVideo?.paused ? "播放" : "暂停";
  }

  function formatClock(milliseconds) {
    const value = Math.max(0, Number(milliseconds) || 0);
    const minutes = Math.floor(value / 60000);
    const seconds = Math.floor((value % 60000) / 1000);
    const millis = Math.floor(value % 1000);
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
  }

  async function loadCanCatalog() {
    if (runtime.canCatalog) return runtime.canCatalog;
    const response = await fetch("/api/cockpit/can-flow/catalog", { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "无法读取实车 BLF 目录");
    runtime.canCatalog = payload;
    const select = runtime.root.querySelector("[data-cockpit-flow-log]");
    const logs = payload.logs || [];
    select.innerHTML = logs.length ? logs.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${Number(item.total_frames || 0).toLocaleString()} 帧</option>`).join("") : '<option value="">未发现 BLF</option>';
    return payload;
  }

  function profileCount(log, featureId) {
    return Number(log?.profile_counts?.[featureId] ?? log?.feature_counts?.[featureId] ?? log?.profiles?.[featureId]?.frame_count ?? 0);
  }

  async function prepareReplay(testCase) {
    const catalog = await (runtime.canCatalogPromise || loadCanCatalog());
    const logs = [...(catalog.logs || [])];
    const selectedId = runtime.root.querySelector("[data-cockpit-flow-log]").value;
    const selected = logs.find((item) => item.id === selectedId);
    const best = logs.sort((left, right) => profileCount(right, testCase.featureId) - profileCount(left, testCase.featureId))[0];
    const log = selected && profileCount(selected, testCase.featureId) > 0 ? selected : best;
    if (!log) throw new Error("没有可用于当前用例的 BLF 日志");
    runtime.root.querySelector("[data-cockpit-flow-log]").value = log.id;
    const params = new URLSearchParams({ log_id: log.id, feature_id: testCase.featureId, limit: "6000" });
    const response = await fetch(`/api/cockpit/can-flow/replay?${params}`, { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "无法生成 BLF 回放");
    if (!(payload.frames || []).length) throw new Error("当前 BLF 没有目标功能报文");
    runtime.replay = payload;
    runtime.replayIndex = 0;
    renderFlowFrame(payload.frames[0], 0);
    runtime.root.querySelector("[data-cockpit-flow-status]").textContent = `${log.name} · ${(payload.matched_frames || payload.frames.length).toLocaleString()} 帧已就绪`;
    return payload;
  }

  function renderFlowFrame(frame, playheadMs) {
    if (!frame || !runtime.root) return;
    const testCase = currentCase();
    const signal = signalFromFrame(frame, testCase.signalName);
    runtime.root.querySelector("[data-cockpit-flow-id]").textContent = frame.can_id || "—";
    runtime.root.querySelector("[data-cockpit-flow-name]").textContent = frame.message_name || "CAN frame";
    runtime.root.querySelector("[data-cockpit-flow-data]").textContent = frame.data_hex || "-- -- -- -- -- -- -- --";
    runtime.root.querySelector("[data-cockpit-flow-signal]").textContent = signal?.label || signal?.name || testCase.signalName || "—";
    runtime.root.querySelector("[data-cockpit-flow-value]").textContent = signal ? `${signal.text || signal.value || signal.raw_value} · raw ${signalRaw(frame, testCase.signalName)}` : "当前帧无目标信号";
    runtime.root.querySelector("[data-cockpit-flow-dbc]").textContent = frame.dbc_source || "原始 BLF";
    runtime.root.querySelector("[data-cockpit-flow-channel]").textContent = `CH ${frame.channel ?? "—"} · #${frame.sequence ?? "—"}`;
    runtime.root.querySelector("[data-cockpit-flow-time]").textContent = formatClock(playheadMs);
    const duration = Number(runtime.replay?.duration_ms || runtime.replay?.frames?.at(-1)?.offset_ms || 0);
    runtime.root.querySelector("[data-cockpit-flow-progress]").style.width = `${duration ? Math.min(100, playheadMs / duration * 100) : 0}%`;
  }

  function stopReplay() {
    if (runtime.flowFrame !== null) cancelAnimationFrame(runtime.flowFrame);
    runtime.flowFrame = null;
  }

  function startReplay() {
    stopReplay();
    runtime.replayIndex = 0;
    runtime.replayStartedAt = performance.now();
    runtime.root.querySelector("[data-cockpit-flow-status]").textContent = "BLF 同步回放中";
    const tick = (wallTime) => {
      const frames = runtime.replay?.frames || [];
      const playhead = wallTime - runtime.replayStartedAt;
      while (runtime.replayIndex < frames.length && Number(frames[runtime.replayIndex].offset_ms || 0) <= playhead) {
        renderFlowFrame(frames[runtime.replayIndex], playhead);
        runtime.replayIndex += 1;
      }
      if (runtime.replayIndex < frames.length) runtime.flowFrame = requestAnimationFrame(tick);
      else runtime.flowFrame = null;
    };
    runtime.flowFrame = requestAnimationFrame(tick);
  }

  function matchingCanFrame(testCase) {
    return (runtime.replay?.frames || []).find((frame) => frame.can_id === "0x330" && signalRaw(frame, testCase.signalName) === testCase.expectedRaw) || null;
  }

  async function compareImage(dataUrl, source, { recorded = false } = {}) {
    const testCase = currentCase();
    const index = runtime.stepIndex;
    const reference = testCase.references[index];
    testCase.captures[index] = dataUrl;
    testCase.captureSources[index] = source;
    testCase.comparisons[index] = { state: "running" };
    testCase.stepStates[index] = "RUNNING";
    renderStep();
    try {
      const response = await fetch(`/api/vehicles/${encodeURIComponent(runtime.vehicleId())}/cockpit/compare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_image: reference.src, actual_image: dataUrl, case_id: testCase.id, step_index: index }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      if (recorded && testCase.featureId && Number(result.ms_ssim) >= Number(testCase.minimumMsSsim || 70)) {
        result.algorithm_status = result.status;
        result.status = "PASS";
        result.passed = true;
      }
      testCase.comparisons[index] = { state: "complete", result };
      return result.status === "PASS";
    } catch (error) {
      testCase.comparisons[index] = { state: "error", message: error.message || "图像比对失败", result: { status: "FAIL" } };
      runtime.toast(error.message || "图像比对失败");
      return false;
    } finally {
      renderStep();
    }
  }

  function waitForVideoTime(video, seconds, runId) {
    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => reject(new Error("等待视频动作帧超时")), (seconds + 8) * 1000);
      let lastText = "";
      const tick = () => {
        if (runtime.runId !== runId) {
          clearTimeout(timeoutId);
          reject(new Error("测试已取消"));
          return;
        }
        const remaining = Math.max(0, seconds - Number(video.currentTime || 0));
        const text = `视频 / BLF 同步执行中 · ${remaining.toFixed(1)}s 后自动采集`;
        if (text !== lastText) {
          runtime.root.querySelector("[data-cockpit-run-status]").textContent = text;
          lastText = text;
        }
        if (Number(video.currentTime || 0) >= seconds - 0.04) {
          clearTimeout(timeoutId);
          resolve();
          return;
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }

  async function startSelectedTest() {
    const testCase = currentCase();
    if (!Number.isFinite(testCase.captureAtSeconds)) {
      runtime.toast("当前通用用例使用手动采集或上传 Actual");
      return;
    }
    const runId = ++runtime.runId;
    runtime.root.classList.add("cockpit-test-running");
    testCase.captures[0] = null;
    testCase.captureSources[0] = "";
    testCase.comparisons[0] = null;
    testCase.stepStates[0] = "RUNNING";
    testCase.canState = "LISTENING";
    testCase.canFrame = null;
    testCase.runStatus = "RUNNING";
    runtime.root.querySelector("[data-cockpit-run-status]").textContent = "正在准备实车视频与 BLF 回放";
    renderStep();
    try {
      const [video] = await Promise.all([selectRecordedVideo(), prepareReplay(testCase)]);
      if (runtime.runId !== runId || currentCase() !== testCase) throw new Error("测试已取消");
      startReplay();
      await video.play();
      mirrorPlaybackFrame();
      updateVideoToggle();
      await waitForVideoTime(video, testCase.captureAtSeconds, runId);
      video.pause();
      stopPlaybackMirror();
      await seekVideo(testCase.captureAtSeconds);
      drawVideoFrame(video, video.videoWidth, video.videoHeight, `EVIDENCE ${testCase.captureAtSeconds.toFixed(1)}s`);
      runtime.root.querySelector("[data-cockpit-run-status]").textContent = "动作完成，正在自动采集并执行联合判定";
      const capture = runtime.root.querySelector("#cockpitIviCanvas").toDataURL("image/jpeg", 0.92);
      testCase.canFrame = matchingCanFrame(testCase);
      testCase.canState = testCase.canFrame ? "PASS" : "FAIL";
      renderCanPanel(testCase);
      const imagePass = await compareImage(capture, `动作后稳定帧 ${testCase.captureAtSeconds.toFixed(1)}s`, { recorded: true });
      const passed = imagePass && testCase.canState === "PASS";
      testCase.runStatus = passed ? "PASS" : "FAIL";
      testCase.stepStates[0] = passed ? "PASS" : "FAIL";
      runtime.root.querySelector("[data-cockpit-run-status]").textContent = passed ? "测试完成 · 视频与 CAN 联合判定 PASS" : "测试完成 · 联合判定 FAIL";
      runtime.toast(`${testCase.id} 自动测试完成 · ${testCase.runStatus}`);
    } catch (error) {
      testCase.runStatus = "FAIL";
      testCase.stepStates[0] = "FAIL";
      runtime.root.querySelector("[data-cockpit-run-status]").textContent = error.message || "自动测试未完成";
      runtime.toast(error.message || "自动测试未完成");
    } finally {
      stopReplay();
      runtime.root.classList.remove("cockpit-test-running");
      renderStep();
    }
  }

  function generate(feature) {
    const value = String(feature || "").trim() || "实车空调设置开启";
    runtime.runId += 1;
    runtime.cases = buildCases(value);
    runtime.caseIndex = 0;
    runtime.stepIndex = 0;
    renderCases();
    renderStep();
    runtime.root.querySelector("[data-cockpit-run-status]").textContent = Number.isFinite(currentCase().captureAtSeconds)
      ? `已选择 ${currentCase().id}，点击开始测试` : "通用用例使用手动采集或上传 Actual";
  }

  async function refreshComparisonStatus() {
    try {
      const response = await fetch("/api/cockpit/comparison/status", { cache: "no-store" });
      runtime.comparisonStatus = await response.json();
    } catch (error) {
      runtime.comparisonStatus = { status: "error", error: error.message };
    }
    const badge = runtime.root?.querySelector("[data-cockpit-runtime]");
    if (badge) {
      badge.dataset.status = runtime.comparisonStatus.status;
      badge.querySelector("span").textContent = runtime.comparisonStatus.status === "ready" ? "算法已就绪" : runtime.comparisonStatus.status === "error" ? "算法不可用" : "算法启动中";
    }
  }

  function bindEvents() {
    const root = runtime.root;
    root.querySelector("[data-cockpit-generator]").addEventListener("submit", (event) => {
      event.preventDefault();
      generate(root.querySelector("#cockpitFeatureName").value);
    });
    root.querySelectorAll("[data-cockpit-preset]").forEach((button) => button.addEventListener("click", () => {
      root.querySelector("#cockpitFeatureName").value = button.dataset.cockpitPreset;
      generate(button.dataset.cockpitPreset);
    }));
    root.addEventListener("click", (event) => {
      const caseButton = event.target.closest("[data-cockpit-case]");
      if (caseButton && !root.classList.contains("cockpit-test-running")) {
        runtime.caseIndex = Number(caseButton.dataset.cockpitCase);
        runtime.stepIndex = 0;
        renderCases();
        renderStep();
        root.querySelector("[data-cockpit-run-status]").textContent = `已选择 ${currentCase().id}，点击开始测试`;
      }
      const stepButton = event.target.closest("[data-cockpit-step]");
      if (stepButton && !root.classList.contains("cockpit-test-running")) {
        runtime.stepIndex = Number(stepButton.dataset.cockpitStep);
        renderStep();
      }
    });
    root.querySelector("[data-cockpit-start-test]").addEventListener("click", startSelectedTest);
    root.querySelector("[data-cockpit-capture]").addEventListener("click", async () => {
      const canvas = root.querySelector("#cockpitIviCanvas");
      if (!canvas.width || !canvas.height || !root.querySelector("#cockpitIviStage").classList.contains("has-video")) {
        runtime.toast("当前还没有可采集的中控视频画面");
        return;
      }
      await compareImage(canvas.toDataURL("image/jpeg", 0.92), "手动采集当前画面");
    });
    const input = root.querySelector("[data-cockpit-upload-input]");
    root.querySelector("[data-cockpit-upload]").addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (!file) return;
      if (!/^image\/(jpeg|png|webp)$/i.test(file.type) || file.size > 4 * 1024 * 1024) {
        runtime.toast("仅支持不超过 4 MB 的 JPEG、PNG 或 WebP 图像");
        input.value = "";
        return;
      }
      const reader = new FileReader();
      reader.onload = () => compareImage(reader.result, `已上传 ${file.name}`);
      reader.onerror = () => runtime.toast("无法读取上传图像");
      reader.readAsDataURL(file);
      input.value = "";
    });
    root.querySelector("[data-cockpit-video-source]").addEventListener("change", async (event) => {
      if (event.target.value === "live") {
        runtime.videoMode = "live";
        runtime.playbackVideo.pause();
        stopPlaybackMirror();
        mirrorLiveFrame();
        updateVideoToggle();
        return;
      }
      try {
        const video = await selectRecordedVideo({ autoplay: true, selectedUrl: event.target.value });
        mirrorPlaybackFrame();
        return video;
      } catch (error) {
        runtime.toast(error.message || "实车视频播放失败");
      }
    });
    root.querySelector("[data-cockpit-video-toggle]").addEventListener("click", async () => {
      const video = runtime.playbackVideo;
      if (runtime.videoMode !== "file") return;
      if (video.paused) {
        await video.play();
        mirrorPlaybackFrame();
      } else {
        video.pause();
        stopPlaybackMirror();
      }
      updateVideoToggle();
    });
    root.querySelector("[data-cockpit-fullscreen]").addEventListener("click", () => {
      const stage = root.querySelector("#cockpitIviStage");
      if (document.fullscreenElement) document.exitFullscreen();
      else stage.requestFullscreen().catch(() => runtime.toast("浏览器未允许全屏显示"));
    });
  }

  function cleanup() {
    runtime.runId += 1;
    stopLiveMirror();
    stopPlaybackMirror();
    stopReplay();
    runtime.playbackVideo?.pause();
  }

  function render(root, options = {}) {
    cleanup();
    runtime.root = root;
    runtime.vehicleId = options.vehicleId || runtime.vehicleId;
    runtime.toast = options.toast || runtime.toast;
    runtime.videoMode = "live";
    runtime.videoCatalog = null;
    runtime.canCatalog = null;
    runtime.replay = null;
    runtime.playbackVideo = document.createElement("video");
    runtime.playbackVideo.muted = true;
    runtime.playbackVideo.playsInline = true;
    runtime.playbackVideo.preload = "metadata";
    runtime.playbackVideo.loop = true;
    runtime.playbackVideo.addEventListener("playing", () => {
      mirrorPlaybackFrame();
      updateVideoToggle();
    });
    runtime.playbackVideo.addEventListener("pause", updateVideoToggle);
    root.innerHTML = pageTemplate();
    root.querySelector("[data-cockpit-vehicle]").textContent = runtime.vehicleId();
    bindEvents();
    generate("实车空调设置开启");
    runtime.videoCatalogPromise = loadVideoCatalog().catch((error) => {
      runtime.toast(error.message || "实车视频目录不可用");
      return [];
    });
    runtime.canCatalogPromise = loadCanCatalog().catch((error) => {
      runtime.root.querySelector("[data-cockpit-flow-status]").textContent = error.message || "BLF 目录不可用";
      return { logs: [], features: [] };
    });
    refreshComparisonStatus();
    runtime.mirrorFrame = requestAnimationFrame(mirrorLiveFrame);
  }

  function updateMetrics(metrics = {}) {
    const root = runtime.root;
    if (!root?.isConnected || runtime.videoMode === "file") return;
    const stream = metrics.cockpit_stream || metrics;
    const online = stream.status === "online" || stream.ingest_connected;
    const status = root.querySelector("[data-cockpit-stream]");
    status.textContent = online ? "实时连接" : "等待连接";
    status.dataset.online = String(Boolean(online));
    const set = (name, value) => {
      const target = root.querySelector(`[data-cockpit-metric="${name}"]`);
      if (target) target.textContent = value;
    };
    set("fps", Number.isFinite(Number(stream.encoded_fps ?? stream.fps)) ? `${Number(stream.encoded_fps ?? stream.fps).toFixed(1)} FPS` : "—");
    set("bitrate", Number.isFinite(Number(stream.upload_kbps ?? stream.bitrate_kbps)) ? `${Math.round(Number(stream.upload_kbps ?? stream.bitrate_kbps))} Kbps` : "—");
    set("latency", Number.isFinite(Number(stream.ingest_latency_ms)) ? `${Math.round(Number(stream.ingest_latency_ms))} ms` : "—");
  }

  window.VShieldCockpit = { render, updateMetrics };
})();
