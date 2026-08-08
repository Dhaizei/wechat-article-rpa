// 默认优先打开最近一份已生成的日报，避免当天早报尚未产出时用户落到空白页。
const requestedIssue = new URLSearchParams(window.location.search).get("date");
const state = {
  date: /^\d{4}-\d{2}-\d{2}$/.test(requestedIssue || "") ? requestedIssue : "",
  data: null,
  hasExplicitIssue: Boolean(requestedIssue),
  hasAutoSelectedLatest: false,
};
const elements = {
  date: document.querySelector("#issueDate"), previous: document.querySelector("#previousIssue"), next: document.querySelector("#nextIssue"),
  archive: document.querySelector("#issueArchive"), archiveCount: document.querySelector("#archiveCount"), title: document.querySelector("#briefingTitle"), meta: document.querySelector("#briefingMeta"),
  content: document.querySelector("#briefingContent"), coverage: document.querySelector("#coverageList"), source: document.querySelector("#openCoverageArticles"),
  highlights: document.querySelector("#briefingHighlights"), toast: document.querySelector("#briefingToast"),
};

function beijingDate(dayOffset = 0) {
  const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(Date.now() + dayOffset * 86400000));
  const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function toast(message) { elements.toast.textContent = message; elements.toast.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3400); }
function setText(node, value) { node.textContent = String(value || "—"); }

function renderArchive(items) {
  elements.archive.replaceChildren();
  // 档案按月归组：用户先找日期，不需要在每一行重复阅读较长的汇总范围。
  elements.archiveCount.textContent = items.length ? `${number(items.length)} 期` : "—";
  if (!items.length) { elements.archive.textContent = "暂无已归档期次"; return; }
  const grouped = new Map();
  items.forEach((item) => {
    const month = String(item.issue_date || "").slice(0, 7);
    if (!grouped.has(month)) grouped.set(month, []);
    grouped.get(month).push(item);
  });
  grouped.forEach((monthItems, month) => {
    const group = document.createElement("section"); group.className = "archive-month";
    const heading = document.createElement("h2"); heading.textContent = month ? `${month.replace("-", " 年 ")} 月` : "未标记月份";
    const amount = document.createElement("span"); amount.textContent = `${monthItems.length} 期`; heading.append(amount);
    const list = document.createElement("div"); list.className = "archive-month-list";
    monthItems.forEach((item) => {
      const button = document.createElement("button"); button.type = "button"; button.className = `archive-item${item.issue_date === state.date ? " active" : ""}`;
      const title = document.createElement("strong"); title.textContent = String(item.issue_date || "").slice(5).replace("-", " 月 ") + " 日";
      const status = document.createElement("small"); status.textContent = item.issue_date === state.date ? "正在阅读" : "查看日报";
      button.append(title, status); button.addEventListener("click", () => { if (item.issue_date !== state.date) { state.date = item.issue_date; elements.date.value = item.issue_date; loadBriefing(); } }); list.append(button);
    });
    group.append(heading, list); elements.archive.append(group);
  });
}

function renderCoverage(data) {
  elements.coverage.replaceChildren();
  const coverage = data.coverage || {};
  const rows = [["汇总范围", coverage.label || "暂未记录"], ["入选文章", `${number(data.article_count)} 篇`], ["来源公众号", `${number(data.account_count)} 个`], ["生成时间", data.generated_at || "暂未记录"]];
  rows.forEach(([label, value]) => { const row = document.createElement("div"); const term = document.createElement("dt"); const detail = document.createElement("dd"); term.textContent = label; detail.textContent = value; row.append(term, detail); elements.coverage.append(row); });
  const coverageDay = String(coverage.start || "").slice(0, 10);
  elements.source.href = coverageDay ? `/daily.html?date=${encodeURIComponent(coverageDay)}` : "/daily.html";
  elements.source.textContent = coverageDay ? `查看 ${coverageDay} 原始文章` : "查看原始文章";
}

function renderHighlights(items) {
  elements.highlights.replaceChildren();
  if (!items.length) { const empty = document.createElement("li"); empty.textContent = "本期暂无可展示热点。"; elements.highlights.append(empty); return; }
  items.forEach((item) => { const row = document.createElement("li"); const link = document.createElement(item.url ? "a" : "span"); link.textContent = item.title; if (item.url) { link.href = item.url; link.target = "_blank"; link.rel = "noreferrer"; } const meta = document.createElement("small"); meta.textContent = `${item.account_name} · 转发 ${number(item.share_count)}`; row.append(link, meta); elements.highlights.append(row); });
}

function cleanBriefingText(value) {
  // 日报来自受控任务，但仍以纯文本节点渲染，避免把 Markdown 当作 HTML 注入页面。
  return String(value || "").replaceAll("**", "").replace(/^#{1,3}\s*/gm, "").replace(/\[([^\]]+)\]\([^\)]+\)/g, "$1").replace(/\s+/g, " ").trim();
}

function briefingLines(content) {
  return String(content || "").replace(/\r/g, "").split("\n").map((line) => line.trim()).filter(Boolean);
}

function markerIndex(lines, marker) {
  return lines.findIndex((line) => line.includes(marker));
}

function bulletItems(lines, start, end) {
  return lines.slice(Math.max(start, 0), end < 0 ? lines.length : end)
    .filter((line) => /^[-•]\s*/.test(line))
    .map((line) => cleanBriefingText(line.replace(/^[-•]\s*/, "")))
    .filter(Boolean);
}

function extractExecutiveBriefing(content) {
  // 历史日报有的保留换行，有的入库时已压成单行；因此优先按稳定的栏目标记切分。
  const raw = String(content || "").replaceAll("**", " ");
  const headlineMatch = raw.match(/(?:✨\s*)?今日头条[：:]\s*([\s\S]*?)(?=\s*(?:🔹\s*)?重点关注[：:]|\s*(?:❓\s*)?(?:今日)?战略议题[：:]|$)/);
  const focusMatch = raw.match(/(?:🔹\s*)?重点关注[：:]\s*([\s\S]*?)(?=\s*(?:❓\s*)?(?:今日)?战略议题[：:]|$)/);
  const strategyMatch = raw.match(/(?:❓\s*)?(?:今日)?战略议题[：:]\s*([\s\S]*?)(?=\s*-{3,}\s*|$)/);
  const inlineBullets = (value) => String(value || "").split(/\s+-\s+/).map((item) => cleanBriefingText(item.replace(/^-\s*/, ""))).filter(Boolean);
  if (headlineMatch || focusMatch || strategyMatch) {
    return {
      headline: cleanBriefingText(headlineMatch?.[1]),
      focus: inlineBullets(focusMatch?.[1]),
      strategy: inlineBullets(strategyMatch?.[1]).filter((item) => /议题\s*\d*|我们|如何/.test(item)),
    };
  }
  const lines = briefingLines(content);
  const headlineAt = markerIndex(lines, "今日头条");
  const focusAt = markerIndex(lines, "重点关注");
  const strategyAt = markerIndex(lines, "战略议题");
  const dividerAt = lines.findIndex((line) => /^-{3,}$/.test(line));
  const headlineLine = headlineAt >= 0 ? lines[headlineAt] : "";
  const headline = cleanBriefingText(headlineLine.replace(/^.*?今日头条[：:]?\s*/, ""));
  const focus = bulletItems(lines, focusAt + 1, strategyAt >= 0 ? strategyAt : dividerAt);
  const strategy = bulletItems(lines, strategyAt + 1, dividerAt).filter((item) => /议题\s*\d*|我们|如何/.test(item));
  const fallback = cleanBriefingText(lines.slice(0, dividerAt >= 0 ? dividerAt : lines.length).join(" ")).slice(0, 260);
  return { headline: headline || fallback, focus, strategy };
}

function appendMarkdownLink(container, value) {
  const raw = String(value || "");
  const match = raw.match(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/);
  if (!match) { container.textContent = cleanBriefingText(raw); return; }
  const prefix = cleanBriefingText(raw.slice(0, match.index));
  const suffix = cleanBriefingText(raw.slice((match.index || 0) + match[0].length));
  if (prefix) container.append(document.createTextNode(`${prefix} `));
  const link = document.createElement("a"); link.href = match[2]; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = match[1]; container.append(link);
  if (suffix) container.append(document.createTextNode(` ${suffix}`));
}

function buildExecutiveSources(items) {
  // “重点关注”来自编辑汇总，当前归档没有逐条保存摘要与原文的一一映射；
  // 因此明确标注为本期延伸阅读，避免把热点文章误说成某一条观点的唯一出处。
  const sources = (items || []).filter((item) => item?.url && item?.title).slice(0, 3);
  if (!sources.length) return null;
  const section = document.createElement("section"); section.className = "briefing-source-strip"; section.setAttribute("aria-label", "本期延伸阅读");
  const head = document.createElement("div"); head.className = "briefing-source-head";
  const title = document.createElement("h3"); title.textContent = "本期延伸阅读";
  const note = document.createElement("p"); note.textContent = "来自本期入选文章，按转发热度排序";
  head.append(title, note);
  const list = document.createElement("div"); list.className = "briefing-source-list";
  sources.forEach((item) => {
    const link = document.createElement("a"); link.href = item.url; link.target = "_blank"; link.rel = "noreferrer";
    const name = document.createElement("strong"); name.textContent = item.title;
    const meta = document.createElement("small"); meta.textContent = `${item.account_name} · 转发 ${number(item.share_count)}`;
    link.append(name, meta); list.append(link);
  });
  const reportLink = document.createElement("a"); reportLink.className = "briefing-source-more"; reportLink.href = "#briefingFullReport"; reportLink.textContent = "查看完整分类报告与参考链接 ↓";
  section.append(head, list, reportLink); return section;
}

function buildExecutiveSection(content, highlights) {
  const briefing = extractExecutiveBriefing(content);
  const section = document.createElement("section"); section.className = "briefing-executive";
  const label = document.createElement("p"); label.className = "briefing-section-label"; label.textContent = "编辑速览";
  const headline = document.createElement("article"); headline.className = "briefing-headline-card";
  const headlineTitle = document.createElement("h2"); headlineTitle.textContent = "今日头条";
  const headlineText = document.createElement("p"); headlineText.textContent = briefing.headline || "本期暂无可展示摘要。";
  headline.append(headlineTitle, headlineText); section.append(label, headline);

  const insightGroups = [];
  if (briefing.focus.length) insightGroups.push(["重点关注", briefing.focus, "focus"]);
  if (briefing.strategy.length) insightGroups.push(["战略议题", briefing.strategy, "strategy"]);
  if (insightGroups.length) {
    const grid = document.createElement("div"); grid.className = "briefing-insight-grid";
    insightGroups.forEach(([title, items, kind]) => {
      const card = document.createElement("section"); card.className = `briefing-insight-card ${kind}`;
      const heading = document.createElement("h3"); heading.textContent = title;
      const list = document.createElement("ul");
      items.forEach((item) => { const row = document.createElement("li"); row.textContent = item; list.append(row); });
      card.append(heading, list); grid.append(card);
    });
    section.append(grid);
  }
  const sources = buildExecutiveSources(highlights);
  if (sources) section.append(sources);
  return section;
}

function buildFullReport(content, categories) {
  const section = document.createElement("section"); section.id = "briefingFullReport"; section.className = "briefing-report";
  const reportHead = document.createElement("div"); reportHead.className = "briefing-report-head";
  const reportTitle = document.createElement("h2"); reportTitle.textContent = "分类解读";
  const categoryMeta = document.createElement("span"); categoryMeta.textContent = (categories || []).map((item) => `${item.name} ${item.count} 篇`).join(" · "); reportHead.append(reportTitle, categoryMeta);
  const details = document.createElement("details"); details.className = "briefing-full-report"; details.open = true;
  const summary = document.createElement("summary"); summary.textContent = "收起完整编辑报告";
  const contentWrap = document.createElement("div"); contentWrap.className = "briefing-report-sections";
  const blocks = String(content || "").split(/\n\s*-{3,}\s*\n/).slice(1);

  blocks.forEach((block) => {
    const lines = briefingLines(block);
    if (!lines.length) return;
    const category = document.createElement("section"); category.className = "briefing-category";
    const heading = document.createElement("h3"); heading.textContent = cleanBriefingText(lines.shift()); category.append(heading);
    let article = null;
    lines.forEach((line) => {
      if (/^\d+[.、]\s+/.test(line)) {
        article = document.createElement("article"); article.className = "briefing-report-article";
        const title = document.createElement("h4"); title.textContent = cleanBriefingText(line.replace(/^\d+[.、]\s+/, "")); article.append(title); category.append(article); return;
      }
      const target = article || category;
      if (/参考链接/.test(line) || /\[[^\]]+\]\(https?:\/\//.test(line)) {
        const reference = document.createElement("p"); reference.className = "briefing-reference"; appendMarkdownLink(reference, line); target.append(reference); return;
      }
      const paragraph = document.createElement("p"); paragraph.textContent = cleanBriefingText(line.replace(/^[-•]\s*/, "")); if (paragraph.textContent) target.append(paragraph);
    });
    contentWrap.append(category);
  });
  if (!contentWrap.childElementCount) {
    const fallback = document.createElement("p"); fallback.textContent = cleanBriefingText(content); contentWrap.append(fallback);
  }
  // `toggle` 在原生 details 状态更新后触发，保证说明文字始终与实际展开状态一致。
  details.addEventListener("toggle", () => { summary.textContent = details.open ? "收起完整编辑报告" : "展开完整编辑报告"; });
  details.append(summary, contentWrap); section.append(reportHead, details); return section;
}

function render(data) {
  state.data = data; setText(elements.title, data.available ? data.issue_label : `${state.date} 每日新闻`);
  setText(elements.meta, data.available ? `编辑日报 · ${data.coverage?.label || "汇总范围暂未记录"}` : "该期尚未生成");
  renderArchive(data.archive || []); renderCoverage(data); renderHighlights(data.highlights || []); elements.content.replaceChildren();
  if (!data.available) { const empty = document.createElement("p"); empty.className = "briefing-empty"; empty.textContent = data.message || "该期日报尚未生成。"; const articleLink = document.createElement("a"); articleLink.href = "/daily.html"; articleLink.textContent = "查看文章动态"; empty.append(document.createElement("br"), articleLink); elements.content.append(empty); return; }
  elements.content.append(buildExecutiveSection(data.content, data.highlights), buildFullReport(data.content, data.categories));
}

function shiftIssue(days) {
  const [year, month, day] = state.date.split("-").map(Number); const current = new Date(Date.UTC(year, month - 1, day + days));
  state.date = [current.getUTCFullYear(), String(current.getUTCMonth() + 1).padStart(2, "0"), String(current.getUTCDate()).padStart(2, "0")].join("-"); elements.date.value = state.date; loadBriefing();
}
async function loadBriefing() {
  try {
    const response = await fetch(`/api/daily-briefing?date=${encodeURIComponent(state.date)}`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "读取每日新闻失败");

    // 仅在首次默认打开时回退到最新归档；手动选定日期时仍保留“该期未生成”的真实状态。
    const latest = (data.archive || [])[0];
    if (!data.available && !state.hasExplicitIssue && !state.hasAutoSelectedLatest && latest?.issue_date) {
      state.hasAutoSelectedLatest = true;
      state.date = latest.issue_date;
      elements.date.value = state.date;
      await loadBriefing();
      return;
    }
    render(data);
  } catch (error) {
    elements.content.innerHTML = '<p class="briefing-empty">每日新闻暂时无法读取，请稍后重试。</p>';
    toast(error.message || "读取每日新闻失败");
  }
}
elements.previous.addEventListener("click", () => shiftIssue(-1)); elements.next.addEventListener("click", () => shiftIssue(1)); elements.date.addEventListener("change", () => { state.hasExplicitIssue = true; state.date = elements.date.value; loadBriefing(); }); state.date = state.date || beijingDate(); elements.date.value = state.date; loadBriefing();
