const elements = {
  serviceState: document.querySelector("#serviceState"),
  scanRange: document.querySelector("#scanRange"),
  metrics: document.querySelector("#metrics"),
  maxArticles: document.querySelector("#maxArticles"),
  currentConfigPreview: document.querySelector("#currentConfigPreview"),
  progressState: document.querySelector("#progressState"),
  accountPosition: document.querySelector("#accountPosition"),
  currentAccount: document.querySelector("#currentAccount"),
  currentAccountArticles: document.querySelector("#currentAccountArticles"),
  totalArticles: document.querySelector("#totalArticles"),
  progressPhase: document.querySelector("#progressPhase"),
  overallProgressBar: document.querySelector("#overallProgressBar"),
  startManual: document.querySelector("#startManual"),
  stopTask: document.querySelector("#stopTask"),
  currentState: document.querySelector("#currentState"),
  currentDetail: document.querySelector("#currentDetail"),
  progressBar: document.querySelector("#progressBar"),
  scheduleEnabled: document.querySelector("#scheduleEnabled"),
  scheduleEnabledText: document.querySelector("#scheduleEnabledText"),
  scheduleTimes: document.querySelector("#scheduleTimes"),
  scheduleRangeList: document.querySelector("#scheduleRangeList"),
  saveSchedule: document.querySelector("#saveSchedule"),
  testSchedule: document.querySelector("#testSchedule"),
  savedSchedulePreview: document.querySelector("#savedSchedulePreview"),
  nextRun: document.querySelector("#nextRun"),
  outputPath: document.querySelector("#outputPath"),
  logConsole: document.querySelector("#logConsole"),
  clearLogs: document.querySelector("#clearLogs"),
  refreshPreflight: document.querySelector("#refreshPreflight"),
  preflightSummary: document.querySelector("#preflightSummary"),
  wechatCheck: document.querySelector("#wechatCheck"),
  searchCheck: document.querySelector("#searchCheck"),
  desktopCheck: document.querySelector("#desktopCheck"),
  runSummaryText: document.querySelector("#runSummaryText"),
  activeRunId: document.querySelector("#activeRunId"),
  runHistoryBody: document.querySelector("#runHistoryBody"),
  runDiagnostic: document.querySelector("#runDiagnostic"),
  runDiagnosticTitle: document.querySelector("#runDiagnosticTitle"),
  runDiagnosticBody: document.querySelector("#runDiagnosticBody"),
  runConfirmDialog: document.querySelector("#runConfirmDialog"),
  confirmRunDescription: document.querySelector("#confirmRunDescription"),
  confirmRunFacts: document.querySelector("#confirmRunFacts"),
  cancelRunConfirm: document.querySelector("#cancelRunConfirm"),
  confirmRunStart: document.querySelector("#confirmRunStart"),
  toast: document.querySelector("#toast"),
};

let lastLogId = 0;
let configLoaded = false;
let preflightReady = false;
let taskRunning = false;
let pendingRunSource = "";
let pendingRunOptions = null;
let savedScheduleOptions = null;
let savedScheduleRanges = {};
const logCounts = { info: 0, success: 0, warning: 0, error: 0 };

const rangeLabels = { today: "今天", yesterday: "昨天", today_yesterday: "今天和昨天" };
const metricsLabels = { share: "仅转发数（速度更快）", all: "全部互动数" };
const sourceLabels = {
  manual: "手动 · 页面当前选择",
  scheduled: "定时 · 已保存计划",
  "scheduled-test": "测试 · 下一次定时配置",
};

function toast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => elements.toast.classList.remove("show"), 2400);
}

function request(path, options = {}) {
  return fetch(path, { headers: { "Content-Type": "application/json" }, ...options })
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || "请求失败");
      return payload;
    });
}

function currentOptions() {
  return {
    max_articles: Number(elements.maxArticles.value),
    scan_range: elements.scanRange.value,
    metrics: elements.metrics.value,
  };
}

function optionsForRun(source) {
  // 手动执行允许使用页面上尚未保存的选择；定时测试必须复用已保存计划，
  // 这样“测试”才能验证下一次定时任务实际会使用的参数。
  if (source === "scheduled-test" && savedScheduleOptions) {
    return { ...savedScheduleOptions };
  }
  return currentOptions();
}

function optionText(options) {
  return `范围=${rangeLabels[options.scan_range] || options.scan_range}，指标=${metricsLabels[options.metrics] || options.metrics}，每账号上限=${options.max_articles} 篇`;
}

function defaultScheduleRange(time, fallback = "today_yesterday") {
  // 早上补采前一天，晚上只关注当天新增；其他自定义时间使用手动范围作为默认值。
  return { "08:00": "today_yesterday", "22:00": "today" }[time] || fallback;
}

function scheduleRangeSummary(ranges = {}) {
  return Object.entries(ranges).sort(([left], [right]) => left.localeCompare(right, "zh-CN"))
    .map(([time, range]) => `${time} ${rangeLabels[range] || range}`).join(" · ") || "未设置";
}

function nextScheduledTime(times) {
  const now = new Date();
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  const sorted = [...times].sort();
  return sorted.find((time) => {
    const [hour, minute] = time.split(":").map(Number);
    return Number.isFinite(hour) && Number.isFinite(minute) && hour * 60 + minute > nowMinutes;
  }) || sorted[0] || "";
}

function renderScheduleRanges(times, ranges = {}, fallback = "today_yesterday") {
  elements.scheduleRangeList.replaceChildren();
  const validTimes = times.filter((time) => /^([01]\d|2[0-3]):[0-5]\d$/.test(time));
  if (!validTimes.length) {
    const hint = document.createElement("p");
    hint.textContent = "请先填写有效的执行时间，才能配置各时段的扫描范围。";
    elements.scheduleRangeList.appendChild(hint);
    return;
  }
  validTimes.forEach((time) => {
    const label = document.createElement("label");
    label.className = "schedule-range-field";
    const title = document.createElement("span");
    title.textContent = `${time} 扫描范围`;
    const select = document.createElement("select");
    select.dataset.scheduleTime = time;
    [["today_yesterday", "今天和昨天"], ["today", "今天"], ["yesterday", "昨天"]].forEach(([value, text]) => {
      select.add(new Option(text, value));
    });
    select.value = ranges[time] || defaultScheduleRange(time, fallback);
    label.append(title, select);
    elements.scheduleRangeList.appendChild(label);
  });
}

function scheduleRangesForSave() {
  return Object.fromEntries([...elements.scheduleRangeList.querySelectorAll("select[data-schedule-time]")]
    .map((select) => [select.dataset.scheduleTime, select.value]));
}

function renderCurrentConfig() {
  elements.currentConfigPreview.textContent = `本次运行将使用：${optionText(currentOptions())}。`;
}

function renderSavedSchedule(config) {
  savedScheduleRanges = config.schedule_ranges || {};
  const scheduledTime = nextScheduledTime(config.times || []);
  savedScheduleOptions = {
    max_articles: Number(config.max_articles),
    scan_range: savedScheduleRanges[scheduledTime] || config.scan_range,
    metrics: config.metrics,
    scheduled_time: scheduledTime,
  };
  elements.savedSchedulePreview.textContent = `已保存计划：${scheduleRangeSummary(savedScheduleRanges)}；指标=${metricsLabels[config.metrics]}，每账号上限=${config.max_articles} 篇。保存计划前的修改只影响本次手动执行。`;
}

function renderCollectionProgress(progress = {}, running = false) {
  const total = Number(progress.total_accounts || 0);
  const started = Number(progress.current_account_index || progress.accounts_started || 0);
  const finished = Number(progress.accounts_finished || 0);
  const currentArticles = Number(progress.current_account_articles || 0);
  const collected = Number(progress.articles_collected || 0);
  const phase = progress.phase || (running ? "正在启动采集任务" : "等待任务");

  elements.progressState.textContent = running ? "正在采集" : phase;
  elements.progressState.classList.toggle("running", running);
  elements.accountPosition.textContent = total
    ? `第 ${Math.min(started || 1, total)} / ${total} 个（已完成 ${finished}）`
    : (running ? "正在读取账号列表" : "等待读取");
  elements.currentAccount.textContent = progress.current_account || "—";
  elements.currentAccountArticles.textContent = `${currentArticles} 篇`;
  elements.totalArticles.textContent = `${collected} 篇`;
  elements.progressPhase.textContent = phase;
  const percent = total ? Math.min(100, finished / total * 100) : 0;
  elements.overallProgressBar.style.width = `${percent}%`;
  elements.overallProgressBar.parentElement.setAttribute("aria-valuenow", String(Math.round(percent)));
}

function statusLabel(status) {
  return {
    running: "执行中",
    completed: "已完成",
    partial: "部分失败",
    cancelled: "已停止",
    blocked: "前置条件不足",
    interrupted: "已中断",
    failed: "异常退出",
  }[status] || status || "未知";
}

function runSummary(record = {}) {
  const summary = record.summary || {};
  if (record.status === "running") {
    return `已完成 ${summary.accounts_finished || 0}/${summary.total_accounts || "?"} 个账号，已采集 ${summary.articles_collected || 0} 篇`;
  }
  if (record.status === "blocked") return record.result_message || "启动前检查未通过";
  const parts = [];
  if (summary.articles_inserted) parts.push(`新增 ${summary.articles_inserted}`);
  if (summary.articles_updated) parts.push(`更新 ${summary.articles_updated}`);
  if (summary.articles_partial_metrics) parts.push(`转发已采集、其他互动待补 ${summary.articles_partial_metrics}`);
  if (summary.articles_title_evidence_warnings) parts.push(`标题辅助校验提示 ${summary.articles_title_evidence_warnings}`);
  if (summary.article_tab_cleanup_warnings) parts.push(`标签清理待确认 ${summary.article_tab_cleanup_warnings}`);
  if (summary.accounts_no_updates) parts.push(`无更新 ${summary.accounts_no_updates} 个`);
  if (summary.accounts_failed) parts.push(`失败 ${summary.accounts_failed} 个`);
  return parts.join("，") || record.result_message || "暂无文章写入";
}

function renderRunDiagnostic(record = null) {
  elements.runDiagnosticBody.replaceChildren();
  if (!record) {
    elements.runDiagnostic.hidden = true;
    return;
  }

  const summary = record.summary || {};
  const failures = summary.failure_samples || [];
  const noUpdates = summary.no_update_samples || [];
  const tabCleanupWarnings = summary.tab_cleanup_samples || [];
  const preflight = record.preflight || {};
  const lines = [];
  if (record.status === "blocked") {
    [preflight.wechat, preflight.search]
      .filter((item) => item && !item.ok)
      .forEach((item) => lines.push(`启动前检查：${item.message}`));
  }
  if (Number(summary.accounts_failed || 0)) {
    lines.push(`本次有 ${summary.accounts_failed} 个公众号采集失败；以下展示最近 ${failures.length} 条原因。`);
  }
  failures.forEach((failure) => {
    // 老任务没有 recovery_hint 时仍按分类补全建议，确保历史诊断也具备可操作性。
    const recoveryHint = failure.recovery_hint || failureRecoveryHint(failure.category);
    lines.push(`公众号「${failure.account || "未知"}」：${failure.error || "未提供错误详情"}（${failure.category || "unknown"}）。建议：${recoveryHint}`);
  });
  if (Number(summary.article_tab_cleanup_warnings || 0)) {
    lines.push(`本次有 ${summary.article_tab_cleanup_warnings} 次浏览器标签未安全清理；任务结束后请确认微信已回到“搜一搜”页，再重试受影响账号。`);
  }
  tabCleanupWarnings.forEach((warning) => {
    const title = warning.title ? `；文章「${warning.title}」` : "";
    lines.push(`标签清理告警：公众号「${warning.account || "未知"}」${title}；${warning.error || "未提供错误详情"}`);
  });
  if (noUpdates.length) {
    lines.push(`以下账号正常完成但在所选范围内没有可采文章（不是采集失败）：`);
    noUpdates.forEach((item) => {
      const range = rangeLabels[item.range] || item.range || "所选范围";
      const promotionDetail = item.promotion_cards ? `推广内容跳过=${item.promotion_cards}` : "";
      const details = [
        `范围=${range}`,
        promotionDetail,
        `本屏识别卡片=${item.observed_cards || 0}`,
        item.outside_range_cards ? `范围外跳过=${item.outside_range_cards}` : "",
        item.ungrouped_cards ? `缺少时间分组=${item.ungrouped_cards}` : "",
      ].filter(Boolean).join("，");
      lines.push(`公众号「${item.account || "未知"}」：${item.stop_reason || "未提供原因"}（${details}）`);
    });
  }
  if (!lines.length) {
    elements.runDiagnostic.hidden = true;
    return;
  }
  elements.runDiagnosticTitle.textContent = `任务诊断 · ${statusLabel(record.status)}`;
  lines.forEach((line) => {
    const item = document.createElement("p");
    item.textContent = line;
    elements.runDiagnosticBody.appendChild(item);
  });
  elements.runDiagnostic.hidden = false;
}

function failureRecoveryHint(category) {
  const hints = {
    account_filter: "确认“搜一搜”已选中“账号”和二级“公众号”，保持窗口可见后重试。",
    account_not_found: "确认公众号当前名称；如名称已变更，请更新账号别名后重试。",
    profile_validation: "关闭残留的公众号资料页，确认搜索结果名称后重试。",
    interaction_ocr: "确认微信缩放和页面完整显示，再重试补采互动数据。",
    copy_link: "确认文章页已完全加载且微信可访问剪贴板后重试。",
    window: "将微信“搜一搜”窗口置前并保持可见后重试。",
    network: "检查网络连接和微信页面加载状态后重试。",
    mongo: "检查入库服务连接与唯一索引状态后重试。",
  };
  return hints[category] || "请保留本次输出目录的 run.log，并确认微信页面状态后重试。";
}

function renderRunHistory(records = [], activeRunId = "") {
  elements.activeRunId.textContent = activeRunId ? `本次任务：${activeRunId.slice(0, 12)}` : "当前未运行";
  const latest = records[0];
  elements.runSummaryText.textContent = latest
    ? `最近一次：${statusLabel(latest.status)} · ${runSummary(latest)}`
    : "尚无已保存的采集任务。";
  renderRunDiagnostic(latest);
  elements.runHistoryBody.replaceChildren();
  if (!records.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="5" class="empty-cell">尚无任务记录</td>';
    elements.runHistoryBody.appendChild(row);
    return;
  }
  records.forEach((record) => {
    const row = document.createElement("tr");
    const options = record.parameters || {};
    const source = sourceLabels[record.source] || record.source || "未知";
    const cells = [
      record.started_at || record.requested_at || "—",
      source,
      optionText(options),
      runSummary(record),
      statusLabel(record.status),
    ];
    cells.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 4) {
        const badge = document.createElement("span");
        badge.className = `run-status ${record.status || ""}`;
        badge.textContent = value;
        cell.appendChild(badge);
      } else {
        cell.textContent = value;
      }
      row.appendChild(cell);
    });
    elements.runHistoryBody.appendChild(row);
  });
}

function updateTaskButtons() {
  const unavailable = taskRunning || !preflightReady;
  elements.startManual.disabled = unavailable;
  elements.testSchedule.disabled = unavailable;
  elements.stopTask.disabled = !taskRunning;
}

function renderPreflightItem(element, item) {
  element.classList.toggle("ok", item.ok);
  element.classList.toggle("missing", !item.ok);
  element.querySelector(".check-icon").textContent = item.ok ? "✓" : "!";
  element.querySelector("small").textContent = item.message;
}

async function loadPreflight({ notify = false, recover = false } = {}) {
  const button = elements.refreshPreflight;
  const originalText = button.textContent;
  try {
    if (recover) {
      button.disabled = true;
      button.textContent = "正在尝试打开…";
    }
    const preflight = await request(
      recover ? "/api/preflight/recover" : "/api/preflight",
      recover ? { method: "POST", body: "{}" } : {},
    );
    preflightReady = preflight.ready;
    renderPreflightItem(elements.wechatCheck, preflight.wechat);
    renderPreflightItem(elements.searchCheck, preflight.search);
    elements.desktopCheck.innerHTML = `<strong>屏幕与缩放适配</strong>：${preflight.desktop.message}`;
    elements.preflightSummary.textContent = preflight.ready
      ? "采集环境已就绪，可以开始采集。"
      : "请先完成红色提示中的前置条件，再启动任务。";
    updateTaskButtons();
    if (notify) {
      const recoveryMessage = preflight.recovery?.message;
      toast(recoveryMessage || (preflight.ready ? "启动前检查通过" : "仍有前置条件未满足"));
    }
  } catch (error) {
    preflightReady = false;
    elements.preflightSummary.textContent = "无法检测采集环境，请确认控制台服务正常。";
    updateTaskButtons();
    if (notify) toast(error.message || "重新检测失败");
  } finally {
    if (recover) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function parseTimes() {
  return elements.scheduleTimes.value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
}

function updateEnabledText() {
  elements.scheduleEnabledText.textContent = elements.scheduleEnabled.checked ? "已启用" : "未启用";
}

async function loadStatus() {
  try {
    const status = await request("/api/status");
    elements.serviceState.classList.add("ready");
    elements.serviceState.lastElementChild.textContent = "控制台在线";
    taskRunning = status.running;
    updateTaskButtons();
    renderCollectionProgress(status.progress, status.running);
    renderRunHistory(status.recent_runs || [], status.run_id);
    elements.currentState.textContent = status.running ? "采集执行中" : "空闲";
    const lastRunOptions = status.last_run_options || {};
    elements.currentDetail.textContent = status.running
      ? `当前任务：${optionText(lastRunOptions)}。采集期间请勿操作鼠标和键盘。`
      : (lastRunOptions.scan_range ? `上次任务：${optionText(lastRunOptions)}。` : "确认微信已登录，并保持“搜一搜”窗口可见。");
    elements.progressBar.classList.toggle("running", status.running);
    elements.outputPath.textContent = status.output_dir || "尚未创建输出目录";
    elements.nextRun.textContent = status.next_run;
    if (!configLoaded) {
      const config = status.config;
      elements.scanRange.value = config.scan_range || "today_yesterday";
      elements.metrics.value = config.metrics || "share";
      elements.maxArticles.value = config.max_articles || 20;
      elements.scheduleEnabled.checked = config.enabled;
      elements.scheduleTimes.value = config.times.join(", ");
      renderScheduleRanges(config.times || [], config.schedule_ranges || {}, config.scan_range);
      updateEnabledText();
      renderCurrentConfig();
      renderSavedSchedule(config);
      configLoaded = true;
    } else {
      renderSavedSchedule(status.config);
    }
  } catch (_) {
    elements.serviceState.classList.remove("ready");
    elements.serviceState.lastElementChild.textContent = "控制台未连接";
  }
}

function appendLog(item) {
  const line = document.createElement("div");
  const labels = { info: "信息", success: "成功", warning: "警告", error: "错误" };
  line.className = `log-line ${item.level}`;
  line.innerHTML = "<span class=\"time\"></span><span class=\"level\"></span><span class=\"message\"></span>";
  line.querySelector(".time").textContent = item.time;
  line.querySelector(".level").textContent = `[${labels[item.level] || "信息"}]`;
  line.querySelector(".message").textContent = item.message;
  elements.logConsole.appendChild(line);
  logCounts[item.level] = (logCounts[item.level] || 0) + 1;
  document.querySelector(`#${item.level}Count`).textContent = logCounts[item.level];
}

async function loadLogs() {
  try {
    const payload = await request(`/api/logs?since=${lastLogId}`);
    if (!payload.items.length) return;
    const nearBottom = elements.logConsole.scrollHeight - elements.logConsole.scrollTop - elements.logConsole.clientHeight < 80;
    payload.items.forEach((item) => {
      appendLog(item);
      lastLogId = Math.max(lastLogId, item.id);
    });
    if (nearBottom) elements.logConsole.scrollTop = elements.logConsole.scrollHeight;
  } catch (_) {
    // 日志轮询失败时不弹窗，连接状态会在上方统一显示。
  }
}

function openRunConfirm(source) {
  const options = optionsForRun(source);
  pendingRunSource = source;
  pendingRunOptions = options;
  elements.confirmRunDescription.textContent = source === "manual"
    ? "将按页面当前选择启动；不会使用下方定时计划的已保存参数。"
    : `将按下一次定时时段（${options.scheduled_time || "未设置"}）的已保存参数执行一次测试，不使用页面尚未保存的修改。`;
  elements.confirmRunFacts.replaceChildren();
  [
    `参数来源：${sourceLabels[source] || source}`,
    `账号来源：MongoDB 账号列表`,
    `扫描范围：${rangeLabels[options.scan_range]}`,
    `采集指标：${metricsLabels[options.metrics]}`,
    `每账号上限：${options.max_articles} 篇`,
  ].forEach((text) => {
    const item = document.createElement("div");
    item.textContent = text;
    elements.confirmRunFacts.appendChild(item);
  });
  elements.runConfirmDialog.hidden = false;
  elements.confirmRunStart.focus();
}

function closeRunConfirm() {
  pendingRunSource = "";
  pendingRunOptions = null;
  elements.runConfirmDialog.hidden = true;
}

async function runTask(source, selectedOptions = null) {
  try {
    const options = selectedOptions || optionsForRun(source);
    const payload = await request("/api/run", { method: "POST", body: JSON.stringify({ source, ...options }) });
    closeRunConfirm();
    toast(`${payload.message}：${optionText(payload.run_options || options)}`);
    await loadStatus();
  } catch (error) {
    toast(error.message);
    // 即使启动被前置检查拦截，也要刷新任务结果，让用户看到可追溯的原因。
    await loadStatus();
  }
}

async function saveSchedule() {
  try {
    const options = currentOptions();
    const times = parseTimes();
    const payload = await request("/api/config", {
      method: "POST",
      body: JSON.stringify({ enabled: elements.scheduleEnabled.checked, times, schedule_ranges: scheduleRangesForSave(), ...options }),
    });
    renderSavedSchedule(payload.config);
    toast("定时计划已保存并立即生效");
    await loadStatus();
  } catch (error) {
    toast(error.message);
  }
}

[elements.scanRange, elements.metrics, elements.maxArticles].forEach((element) => {
  element.addEventListener("change", renderCurrentConfig);
});
elements.scheduleEnabled.addEventListener("change", updateEnabledText);
elements.scheduleTimes.addEventListener("change", () => {
  renderScheduleRanges(parseTimes(), scheduleRangesForSave(), elements.scanRange.value);
});
elements.startManual.addEventListener("click", () => openRunConfirm("manual"));
elements.testSchedule.addEventListener("click", () => openRunConfirm("scheduled-test"));
elements.cancelRunConfirm.addEventListener("click", closeRunConfirm);
elements.confirmRunStart.addEventListener("click", () => {
  if (pendingRunSource) runTask(pendingRunSource, pendingRunOptions);
});
elements.runConfirmDialog.addEventListener("click", (event) => {
  if (event.target === elements.runConfirmDialog) closeRunConfirm();
});
elements.saveSchedule.addEventListener("click", saveSchedule);
elements.stopTask.addEventListener("click", async () => {
  try { toast((await request("/api/stop", { method: "POST", body: "{}" })).message); await loadStatus(); }
  catch (error) { toast(error.message); }
});
elements.clearLogs.addEventListener("click", () => {
  elements.logConsole.replaceChildren();
  Object.keys(logCounts).forEach((level) => { logCounts[level] = 0; document.querySelector(`#${level}Count`).textContent = "0"; });
});
elements.refreshPreflight.addEventListener("click", () => loadPreflight({ notify: true, recover: true }));

loadStatus();
loadPreflight();
loadLogs();
setInterval(loadStatus, 2000);
setInterval(loadPreflight, 5000);
setInterval(loadLogs, 1000);
