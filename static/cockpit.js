(() => {
  "use strict";

  const REFERENCES = {
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

  const runtime = {
    root: null,
    vehicleId: () => "VHC-001",
    toast: () => {},
    cases: [],
    caseIndex: 0,
    stepIndex: 0,
    comparisonStatus: { status: "starting" },
    mirrorFrame: null,
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  function selectedReference(feature) {
    const entries = REFERENCES[feature] || REFERENCES["近光灯控制"];
    return entries.map(([file, label]) => ({
      src: `/static/reference-library/${file}`,
      label,
    }));
  }

  function buildCases(feature) {
    const references = selectedReference(feature);
    const variants = ["标准功能流程", "连续操作稳定性", "状态恢复一致性"];
    return variants.map((title, index) => ({
      id: `TC-${String(index + 1).padStart(3, "0")}`,
      title: `${feature} · ${title}`,
      references,
      captures: Array(references.length).fill(null),
      captureSources: Array(references.length).fill(""),
      comparisons: Array(references.length).fill(null),
    }));
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
              <input id="cockpitFeatureName" value="近光灯控制" maxlength="40" aria-label="中控功能名称">
              <button type="submit">生成测试</button>
            </form>
            <div class="cockpit-presets">
              ${Object.keys(REFERENCES).map((name) => `<button type="button" data-cockpit-preset="${name}">${name}</button>`).join("")}
            </div>
            <div class="cockpit-case-list" data-cockpit-cases></div>
          </section>
          <section class="panel cockpit-stream-panel">
            <div class="cockpit-section-title"><div><p>LIVE HMI MIRROR</p><h2>中控实时画面</h2></div><button type="button" data-cockpit-fullscreen>全屏</button></div>
            <div class="cockpit-video-stage" id="cockpitIviStage">
              <canvas id="cockpitIviCanvas"></canvas>
              <div class="cockpit-video-empty" data-cockpit-video-empty><i>IVI</i><strong>等待中控视频</strong><small>复用当前车辆 fMP4 视频链路</small></div>
            </div>
          </section>
        </div>
        <section class="panel cockpit-steps-panel">
          <div class="cockpit-section-title"><div><p>STEP VALIDATION</p><h2 data-cockpit-current-case>测试步骤</h2></div><span data-cockpit-current-step>—</span></div>
          <div class="cockpit-step-list" data-cockpit-steps></div>
        </section>
        <div class="cockpit-compare-grid">
          <section class="panel cockpit-image-card">
            <div class="cockpit-section-title"><div><p>EXPECTED</p><h2>标准参考画面</h2></div></div>
            <div class="cockpit-image-stage"><img data-cockpit-expected alt="标准参考画面"></div>
            <p data-cockpit-expected-caption>—</p>
          </section>
          <section class="panel cockpit-image-card">
            <div class="cockpit-section-title"><div><p>ACTUAL</p><h2>实际中控画面</h2></div></div>
            <div class="cockpit-image-stage">
              <img class="hidden" data-cockpit-actual alt="实际中控画面">
              <div class="cockpit-actual-empty" data-cockpit-actual-empty><i>+</i><span>采集实时画面或上传图片</span></div>
            </div>
            <div class="cockpit-capture-actions">
              <button type="button" data-cockpit-capture>采集当前画面</button>
              <button type="button" data-cockpit-upload>上传 Actual</button>
              <input class="hidden" type="file" accept="image/jpeg,image/png,image/webp" data-cockpit-upload-input>
            </div>
          </section>
          <section class="panel cockpit-verdict-card" data-cockpit-verdict="waiting">
            <div class="cockpit-section-title"><div><p>RESULT</p><h2>图像比对结论</h2></div></div>
            <div class="cockpit-verdict-icon" data-cockpit-verdict-icon>…</div>
            <strong data-cockpit-verdict-status>WAITING</strong>
            <span data-cockpit-verdict-label>等待 Actual 图像</span>
            <b data-cockpit-verdict-score>—</b>
            <p data-cockpit-verdict-details>MS-SSIM — · 差异 — · 对齐 —</p>
          </section>
        </div>
      </div>`;
  }

  function renderCases() {
    const root = runtime.root;
    root.querySelector("[data-cockpit-cases]").innerHTML = runtime.cases.map((item, index) => `
      <button type="button" class="${index === runtime.caseIndex ? "active" : ""}" data-cockpit-case="${index}">
        <small>${item.id}</small><strong>${escapeHtml(item.title)}</strong><span>${item.references.length} 步</span>
      </button>`).join("");
  }

  function resultMetric(value) {
    return Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)}%` : "—";
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
      const result = testCase.comparisons[index];
      const status = result?.state === "running" ? "RUNNING" : result?.result?.status || (testCase.captures[index] ? "READY" : "WAITING");
      return `<button type="button" class="${index === runtime.stepIndex ? "active" : ""}" data-cockpit-step="${index}"><i>${String(index + 1).padStart(2, "0")}</i><span>${escapeHtml(item.label)}</span><em data-status="${status}">${status}</em></button>`;
    }).join("");
    const expected = root.querySelector("[data-cockpit-expected]");
    expected.src = reference.src;
    root.querySelector("[data-cockpit-expected-caption]").textContent = `步骤 ${runtime.stepIndex + 1} · ${reference.label}`;
    const actual = root.querySelector("[data-cockpit-actual]");
    const empty = root.querySelector("[data-cockpit-actual-empty]");
    if (capture) {
      actual.src = comparison?.result?.annotated_actual || capture;
      actual.classList.remove("hidden");
      empty.classList.add("hidden");
    } else {
      actual.removeAttribute("src");
      actual.classList.add("hidden");
      empty.classList.remove("hidden");
    }
    renderVerdict(comparison);
  }

  function renderVerdict(comparison) {
    const root = runtime.root;
    const card = root.querySelector("[data-cockpit-verdict]");
    const icon = root.querySelector("[data-cockpit-verdict-icon]");
    const status = root.querySelector("[data-cockpit-verdict-status]");
    const label = root.querySelector("[data-cockpit-verdict-label]");
    const score = root.querySelector("[data-cockpit-verdict-score]");
    const details = root.querySelector("[data-cockpit-verdict-details]");
    let tone = "waiting";
    icon.textContent = "…";
    status.textContent = runtime.comparisonStatus.status === "error" ? "OFFLINE" : "WAITING";
    label.textContent = runtime.comparisonStatus.status === "error" ? "图像比对算法不可用" : "等待 Actual 图像";
    score.textContent = "—";
    details.textContent = runtime.comparisonStatus.error || "MS-SSIM — · 差异 — · 对齐 —";
    if (comparison?.state === "running") {
      tone = "running";
      icon.textContent = "↻";
      status.textContent = "ANALYZING";
      label.textContent = "正在执行 Expected / Actual 对比";
    } else if (comparison?.state === "error") {
      tone = "invalid";
      icon.textContent = "!";
      status.textContent = "INVALID";
      label.textContent = "图像比对未完成";
      details.textContent = comparison.message;
    } else if (comparison?.result) {
      const result = comparison.result;
      tone = String(result.status).toLowerCase();
      icon.textContent = result.status === "PASS" ? "✓" : "×";
      status.textContent = result.status;
      label.textContent = "图像相似度";
      score.textContent = resultMetric(result.similarity);
      details.textContent = `MS-SSIM ${resultMetric(result.ms_ssim)} · 差异 ${resultMetric(result.difference_ratio)} · 对齐 ${result.alignment?.method || result.alignment?.status || "—"}`;
    }
    card.dataset.cockpitVerdict = tone;
  }

  async function compareImage(dataUrl, source) {
    const testCase = currentCase();
    const index = runtime.stepIndex;
    const reference = testCase.references[index];
    testCase.captures[index] = dataUrl;
    testCase.captureSources[index] = source;
    testCase.comparisons[index] = { state: "running" };
    renderStep();
    try {
      const response = await fetch(`/api/vehicles/${encodeURIComponent(runtime.vehicleId())}/cockpit/compare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_image: reference.src,
          actual_image: dataUrl,
          case_id: testCase.id,
          step_index: index,
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      testCase.comparisons[index] = { state: "complete", result };
      runtime.toast(`座舱图像比对完成：${result.status} · 相似度 ${resultMetric(result.similarity)}`);
    } catch (error) {
      testCase.comparisons[index] = { state: "error", message: error.message || "图像比对失败" };
      runtime.toast(error.message || "图像比对失败");
    }
    renderStep();
  }

  function generate(feature) {
    const value = String(feature || "").trim() || "近光灯控制";
    runtime.cases = buildCases(value);
    runtime.caseIndex = 0;
    runtime.stepIndex = 0;
    renderCases();
    renderStep();
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
    renderStep();
  }

  function mirrorVideoFrame() {
    const root = runtime.root;
    if (!root?.isConnected) {
      runtime.mirrorFrame = null;
      return;
    }
    const target = root.querySelector("#cockpitIviCanvas");
    const stage = root.querySelector("#cockpitIviStage");
    const sourceVideo = document.getElementById("videoElement");
    const sourceCanvas = document.getElementById("videoCanvas");
    let source = null;
    let width = 0;
    let height = 0;
    if (sourceVideo?.readyState >= 2 && sourceVideo.videoWidth) {
      source = sourceVideo;
      width = sourceVideo.videoWidth;
      height = sourceVideo.videoHeight;
    } else if (sourceCanvas?.width && sourceCanvas.height) {
      source = sourceCanvas;
      width = sourceCanvas.width;
      height = sourceCanvas.height;
    }
    if (source && width && height) {
      if (target.width !== width || target.height !== height) {
        target.width = width;
        target.height = height;
      }
      target.getContext("2d", { alpha: false }).drawImage(source, 0, 0, width, height);
      stage.classList.add("has-video");
      root.querySelector("[data-cockpit-video-empty]").classList.add("hidden");
    }
    runtime.mirrorFrame = requestAnimationFrame(mirrorVideoFrame);
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
      if (caseButton) {
        runtime.caseIndex = Number(caseButton.dataset.cockpitCase);
        runtime.stepIndex = 0;
        renderCases();
        renderStep();
      }
      const stepButton = event.target.closest("[data-cockpit-step]");
      if (stepButton) {
        runtime.stepIndex = Number(stepButton.dataset.cockpitStep);
        renderStep();
      }
    });
    root.querySelector("[data-cockpit-capture]").addEventListener("click", () => {
      const canvas = root.querySelector("#cockpitIviCanvas");
      if (!canvas.width || !canvas.height || !root.querySelector("#cockpitIviStage").classList.contains("has-video")) {
        runtime.toast("当前还没有可采集的中控视频画面");
        return;
      }
      compareImage(canvas.toDataURL("image/jpeg", 0.92), "实时画面");
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
      reader.onload = () => compareImage(reader.result, file.name);
      reader.onerror = () => runtime.toast("无法读取上传图像");
      reader.readAsDataURL(file);
      input.value = "";
    });
    root.querySelector("[data-cockpit-fullscreen]").addEventListener("click", () => {
      const stage = root.querySelector("#cockpitIviStage");
      if (document.fullscreenElement) document.exitFullscreen();
      else stage.requestFullscreen().catch(() => runtime.toast("浏览器未允许全屏显示"));
    });
  }

  function render(root, options = {}) {
    runtime.root = root;
    runtime.vehicleId = options.vehicleId || runtime.vehicleId;
    runtime.toast = options.toast || runtime.toast;
    root.innerHTML = pageTemplate();
    root.querySelector("[data-cockpit-vehicle]").textContent = runtime.vehicleId();
    generate("近光灯控制");
    bindEvents();
    refreshComparisonStatus();
    if (runtime.mirrorFrame !== null) cancelAnimationFrame(runtime.mirrorFrame);
    runtime.mirrorFrame = requestAnimationFrame(mirrorVideoFrame);
  }

  function updateMetrics(metrics = {}) {
    const root = runtime.root;
    if (!root?.isConnected) return;
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
