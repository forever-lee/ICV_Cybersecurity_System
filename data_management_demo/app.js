(() => {
  "use strict";

  const records = [
    { id: 1, video_name: "normal_001", vehicle_id: "VHC-001", camera_id: "CAM-FRONT-01", start_time: "2026-10-20 10:00:00.000", end_time: "2026-10-20 10:00:30.000", duration_seconds: 30, video_type: "normal", attack_type: null, pcap_path: "/data/iov/video/pcap/normal/normal_001.pcap", file_size_bytes: 8450112, save_status: "completed", playback_status: "available", playback_error: null, created_at: "2026-10-20 10:00:00.000", updated_at: "2026-10-20 10:00:35.000" },
    { id: 2, video_name: "replay_001", vehicle_id: "VHC-001", camera_id: "CAM-FRONT-01", start_time: "2026-10-20 10:05:00.000", end_time: "2026-10-20 10:05:15.000", duration_seconds: 15, video_type: "attack", attack_type: "replay", pcap_path: "/data/iov/video/pcap/replay/replay_001.pcap", file_size_bytes: 4283392, save_status: "completed", playback_status: "available", playback_error: null, created_at: "2026-10-20 10:05:00.000", updated_at: "2026-10-20 10:05:20.000" },
    { id: 3, video_name: "dos_001", vehicle_id: "VHC-002", camera_id: "CAM-FRONT-02", start_time: "2026-10-20 10:10:00.000", end_time: "2026-10-20 10:10:20.000", duration_seconds: 20, video_type: "attack", attack_type: "dos", pcap_path: "/data/iov/video/pcap/dos/dos_001.pcap", file_size_bytes: 5890048, save_status: "completed", playback_status: "pending", playback_error: null, created_at: "2026-10-20 10:10:00.000", updated_at: "2026-10-20 10:10:21.000" },
    { id: 4, video_name: "normal_002", vehicle_id: "VHC-002", camera_id: "CAM-FRONT-02", start_time: "2026-10-20 10:20:00.000", end_time: "2026-10-20 10:21:00.000", duration_seconds: 60, video_type: "normal", attack_type: null, pcap_path: "/data/iov/video/pcap/normal/normal_002.pcap", file_size_bytes: 16908288, save_status: "completed", playback_status: "parsing", playback_error: null, created_at: "2026-10-20 10:20:00.000", updated_at: "2026-10-20 10:21:02.000" },
    { id: 5, video_name: "normal_003", vehicle_id: "VHC-001", camera_id: "CAM-FRONT-01", start_time: "2026-10-20 10:30:00.000", end_time: null, duration_seconds: null, video_type: "normal", attack_type: null, pcap_path: "/data/iov/video/pcap/normal/normal_003.pcap.part", file_size_bytes: 2359296, save_status: "saving", playback_status: "pending", playback_error: null, created_at: "2026-10-20 10:30:00.000", updated_at: "2026-10-20 10:30:08.000" },
    { id: 6, video_name: "replay_002", vehicle_id: "VHC-002", camera_id: "CAM-FRONT-02", start_time: "2026-10-20 10:40:00.000", end_time: "2026-10-20 10:40:12.000", duration_seconds: 12, video_type: "attack", attack_type: "replay", pcap_path: "/data/iov/video/pcap/replay/replay_002.pcap.part", file_size_bytes: 0, save_status: "failed", playback_status: "failed", playback_error: "PCAP 保存中断，无法解析回放", created_at: "2026-10-20 10:40:00.000", updated_at: "2026-10-20 10:40:13.000" },
  ];

  const labels = {
    normal: "正常数据", replay: "Replay 攻击", dos: "DoS 攻击",
    completed: "已完成", saving: "保存中", failed: "失败",
    available: "可回放", pending: "待处理", parsing: "解析中",
  };
  const pageNames = { overview: "管理总览", videos: "视频数据管理", jobs: "存储与回放任务", body: "车身域", wifi: "Wi-Fi 域", bluetooth: "蓝牙域" };

  const $ = (id) => document.getElementById(id);
  const typeKey = (record) => record.video_type === "normal" ? "normal" : record.attack_type;
  const formatSize = (bytes) => bytes ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : "0 MB";
  const formatDuration = (seconds) => seconds == null ? "进行中" : `${seconds.toFixed(3)}s`;
  const typePill = (record) => `<span class="type-pill ${typeKey(record)}">${labels[typeKey(record)]}</span>`;
  const statusPill = (key) => `<span class="status-pill ${key}">${labels[key]}</span>`;

  function rowMarkup(record, compact = false) {
    if (compact) {
      return `<tr><td><span class="record-name">${record.video_name}</span><span class="subline">#${String(record.id).padStart(4, "0")}</span></td><td>${record.vehicle_id}<span class="subline">${record.camera_id}</span></td><td>${typePill(record)}</td><td>${formatDuration(record.duration_seconds)}</td><td>${statusPill(record.save_status)}</td><td>${statusPill(record.playback_status)}</td><td><button class="row-action" data-record="${record.id}">详情</button></td></tr>`;
    }
    return `<tr><td><span class="record-name">#${String(record.id).padStart(3, "0")}</span><span class="subline">${record.video_name}</span></td><td>${record.vehicle_id}<span class="subline">${record.camera_id}</span></td><td><span class="date-cell">${record.start_time}</span><span class="subline">至 ${record.end_time || "采集中"}</span></td><td>${formatDuration(record.duration_seconds)}</td><td>${typePill(record)}<span class="subline">attack_type: ${record.attack_type || "NULL"}</span></td><td><code class="path-cell">${record.pcap_path}</code></td><td>${formatSize(record.file_size_bytes)}<span class="subline">${record.file_size_bytes.toLocaleString()} bytes</span></td><td>${statusPill(record.save_status)}</td><td>${statusPill(record.playback_status)}</td><td><span class="date-cell">${record.updated_at}</span></td><td><button class="row-action detail-action" data-record="${record.id}">全部字段</button></td></tr>`;
  }

  function bindRecordActions(root = document) {
    root.querySelectorAll("[data-record]").forEach((button) => button.addEventListener("click", () => {
      const record = records.find((item) => item.id === Number(button.dataset.record));
      if (record) openPlayback(record);
    }));
  }

  function renderRecent() {
    $("recentRecords").innerHTML = records.slice(0, 4).map((record) => rowMarkup(record, true)).join("");
    bindRecordActions($("recentRecords"));
  }

  function filteredRecords() {
    const query = $("recordSearch").value.trim().toLowerCase();
    const type = $("typeFilter").value;
    const status = $("statusFilter").value;
    const vehicle = $("vehicleScope").value;
    return records.filter((record) => {
      const haystack = `${record.video_name} ${record.vehicle_id} ${record.camera_id} ${record.pcap_path}`.toLowerCase();
      return (!query || haystack.includes(query))
        && (type === "all" || typeKey(record) === type)
        && (status === "all" || record.playback_status === status)
        && (vehicle === "全部车辆" || record.vehicle_id === vehicle);
    });
  }

  function renderAll() {
    const result = filteredRecords();
    $("recordCount").textContent = result.length;
    $("allRecords").innerHTML = result.length
      ? result.map((record) => rowMarkup(record)).join("")
      : `<tr><td colspan="11" style="height:180px;text-align:center;color:#5d7688">没有符合条件的视频记录</td></tr>`;
    bindRecordActions($("allRecords"));
  }

  function gotoPage(page) {
    document.querySelectorAll("[data-page-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.pagePanel === page));
    document.querySelectorAll("[data-page]").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
    $("pageCrumb").textContent = pageNames[page] || "数据管理";
    $("adminSidebar").classList.remove("open");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  let toastTimer;
  function toast(message) {
    const node = $("adminToast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 2200);
  }

  const playback = { record: null, playing: true, progress: 12, timer: null, animation: null, started: performance.now() };
  function openPlayback(record) {
    playback.record = record;
    playback.progress = 12;
    playback.playing = true;
    $("playbackTitle").textContent = `${record.video_name} · PCAP 按需流式回放`;
    $("videoRecordLabel").textContent = record.video_name;
    $("videoVehicleLabel").textContent = `${record.vehicle_id} · ${record.camera_id}`;
    $("videoTimeLabel").textContent = record.start_time.split(" ")[1];
    const fieldLabels = {
      id: "主键 ID", video_name: "记录名称", vehicle_id: "车辆编号", camera_id: "摄像头编号",
      start_time: "开始时间", end_time: "结束时间", duration_seconds: "持续时间（秒）",
      video_type: "视频类型", attack_type: "攻击类型", pcap_path: "PCAP 文件路径",
      file_size_bytes: "PCAP 文件大小（字节）", save_status: "保存状态",
      playback_status: "回放状态", playback_error: "回放错误", created_at: "创建时间", updated_at: "更新时间",
    };
    $("recordFieldList").innerHTML = Object.entries(fieldLabels).map(([key, title]) => {
      let value = record[key];
      if (value === null) value = "NULL";
      if (key === "duration_seconds" && value !== "NULL") value = Number(value).toFixed(3);
      return `<div class="${key.includes("path") || key === "playback_error" ? "wide-field" : ""}"><dt><code>${key}</code><span>${title}</span></dt><dd>${value}</dd></div>`;
    }).join("");
    $("totalTime").textContent = timeText(record.duration_seconds);
    $("playbackProgress").value = playback.progress;
    $("playToggle").textContent = "Ⅱ";
    $("playbackModal").classList.add("open");
    $("playbackModal").setAttribute("aria-hidden", "false");
    playback.playing = record.playback_status === "available";
    $("playToggle").disabled = record.playback_status !== "available";
    $("playToggle").textContent = record.playback_status === "available" ? "Ⅱ" : "等待解析";
    startPlaybackClock();
    drawFrame();
  }

  function closePlayback() {
    $("playbackModal").classList.remove("open");
    $("playbackModal").setAttribute("aria-hidden", "true");
    clearInterval(playback.timer);
    cancelAnimationFrame(playback.animation);
  }

  function timeText(seconds) {
    const value = Math.max(0, Math.floor(seconds || 0));
    return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  }

  function startPlaybackClock() {
    clearInterval(playback.timer);
    playback.timer = setInterval(() => {
      if (!playback.playing || !playback.record) return;
      playback.progress = Math.min(100, playback.progress + 100 / playback.record.duration_seconds / 4);
      if (playback.progress >= 100) { playback.progress = 0; }
      $("playbackProgress").value = playback.progress;
      $("elapsedTime").textContent = timeText(playback.record.duration_seconds * playback.progress / 100);
    }, 250);
  }

  function drawFrame() {
    if (!$("playbackModal").classList.contains("open")) return;
    const canvas = $("playbackCanvas");
    const bounds = canvas.getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    if (canvas.width !== Math.floor(bounds.width * scale) || canvas.height !== Math.floor(bounds.height * scale)) {
      canvas.width = Math.floor(bounds.width * scale); canvas.height = Math.floor(bounds.height * scale);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    const width = bounds.width, height = bounds.height, t = performance.now() / 1000;
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, "#153349"); gradient.addColorStop(.45, "#132a38"); gradient.addColorStop(.46, "#18292f"); gradient.addColorStop(1, "#060d13");
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = "rgba(128,187,199,.12)"; ctx.fillRect(0, height * .44, width, 2);
    const horizon = height * .45, center = width * .5;
    ctx.fillStyle = "#101b20"; ctx.beginPath(); ctx.moveTo(center - 65, horizon); ctx.lineTo(center + 65, horizon); ctx.lineTo(width * .91, height); ctx.lineTo(width * .09, height); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "rgba(214,236,222,.7)"; ctx.lineWidth = 2;
    [[-65, -.41], [65, .41]].forEach(([top, bottom]) => { ctx.beginPath(); ctx.moveTo(center + top, horizon); ctx.lineTo(center + width * bottom, height); ctx.stroke(); });
    const offset = (t * 80) % 70;
    ctx.strokeStyle = "rgba(238,226,146,.72)"; ctx.lineWidth = 3;
    for (let y = horizon + offset; y < height; y += 70) { const ratio = (y - horizon) / (height - horizon); ctx.beginPath(); ctx.moveTo(center, y); ctx.lineTo(center, Math.min(height, y + 16 + ratio * 18)); ctx.stroke(); }
    ctx.fillStyle = "rgba(27,40,45,.95)";
    for (let x = 0; x < width; x += 55) { const h = 18 + ((x * 7) % 42); ctx.fillRect(x, horizon - h, 37, h); }
    ctx.strokeStyle = "rgba(42,215,224,.22)"; ctx.lineWidth = 1; ctx.strokeRect(18, 48, width - 36, height - 82);
    ctx.fillStyle = "rgba(42,215,224,.75)"; ctx.font = "10px Consolas"; ctx.fillText("ADAS FRONT CAMERA · PCAP ON-DEMAND STREAM", 27, 67);
    playback.animation = requestAnimationFrame(drawFrame);
  }

  function updateClock() {
    const now = new Date();
    $("adminClock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    $("adminDate").textContent = now.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  }

  const jobPlayback = { playing: true, current: 12, duration: 60 };
  function renderJobPlayback() {
    $("jobPlaybackProgress").value = jobPlayback.current;
    $("jobElapsedTime").textContent = timeText(jobPlayback.current);
    $("jobPlaybackToggle").textContent = jobPlayback.playing ? "Ⅱ" : "▶";
    $("jobPlaybackToggle").setAttribute("aria-label", jobPlayback.playing ? "暂停播放" : "继续播放");
    $("jobStreamState").textContent = jobPlayback.playing ? "STREAMING" : "PAUSED";
  }

  $("jobPlaybackToggle").addEventListener("click", () => { jobPlayback.playing = !jobPlayback.playing; renderJobPlayback(); });
  $("jobPlaybackProgress").addEventListener("input", (event) => { jobPlayback.current = Number(event.target.value); renderJobPlayback(); });
  $("jobVolumeButton").addEventListener("click", () => toast("演示回放音轨未启用"));
  $("jobFullscreenButton").addEventListener("click", () => document.querySelector(".job-video-player").requestFullscreen?.());
  setInterval(() => {
    if (!jobPlayback.playing) return;
    jobPlayback.current = jobPlayback.current >= jobPlayback.duration ? 0 : Math.min(jobPlayback.duration, jobPlayback.current + 0.25);
    renderJobPlayback();
  }, 250);

  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => gotoPage(button.dataset.page)));
  document.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => gotoPage(button.dataset.goto)));
  document.querySelectorAll("[data-placeholder]").forEach((button) => button.addEventListener("click", () => gotoPage(button.dataset.placeholder === "车身域" ? "body" : button.dataset.placeholder === "Wi-Fi 域" ? "wifi" : "bluetooth")));
  document.querySelectorAll("[data-toast]").forEach((button) => button.addEventListener("click", () => toast(button.dataset.toast)));
  $("newArchiveButton").addEventListener("click", () => toast("保存任务创建功能将在实施阶段接入"));
  $("menuButton").addEventListener("click", () => $("adminSidebar").classList.toggle("open"));
  ["recordSearch", "typeFilter", "statusFilter"].forEach((id) => $(id).addEventListener(id === "recordSearch" ? "input" : "change", renderAll));
  $("vehicleScope").addEventListener("change", renderAll);
  $("resetFilters").addEventListener("click", () => { $("recordSearch").value = ""; $("typeFilter").value = "all"; $("statusFilter").value = "all"; $("vehicleScope").value = "全部车辆"; renderAll(); });
  $("closePlayback").addEventListener("click", closePlayback);
  $("playbackModal").addEventListener("click", (event) => { if (event.target === $("playbackModal")) closePlayback(); });
  $("playToggle").addEventListener("click", () => { playback.playing = !playback.playing; $("playToggle").textContent = playback.playing ? "Ⅱ" : "▶"; });
  $("playbackProgress").addEventListener("input", (event) => { playback.progress = Number(event.target.value); if (playback.record) $("elapsedTime").textContent = timeText(playback.record.duration_seconds * playback.progress / 100); });
  $("volumeToggle").addEventListener("click", () => toast("演示回放音轨未启用"));
  $("fullscreenMock").addEventListener("click", () => $("playbackModal").requestFullscreen?.());
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closePlayback(); });

  renderRecent(); renderAll(); renderJobPlayback(); updateClock(); setInterval(updateClock, 1000);
})();
