const pageSize = 30;
let offset = 0;
let total = 0;

const elements = {
  date: document.querySelector("#dateFilter"), clearDate: document.querySelector("#clearDateFilter"), account: document.querySelector("#accountFilter"), query: document.querySelector("#queryFilter"), minimumShare: document.querySelector("#minimumShareFilter"), sort: document.querySelector("#sortFilter"),
  todayPreset: document.querySelector("#todayPreset"), yesterdayPreset: document.querySelector("#yesterdayPreset"),
  search: document.querySelector("#searchArticles"), list: document.querySelector("#articleList"), summary: document.querySelector("#articleResultSummary"), previous: document.querySelector("#previousPage"), next: document.querySelector("#nextPage"), page: document.querySelector("#pageText"),
  drawer: document.querySelector("#articleDrawer"), backdrop: document.querySelector("#drawerBackdrop"), close: document.querySelector("#closeDrawer"), drawerAccount: document.querySelector("#drawerAccount"), drawerTitle: document.querySelector("#drawerTitle"), drawerMeta: document.querySelector("#drawerMeta"), drawerUrl: document.querySelector("#drawerUrl"), drawerContent: document.querySelector("#drawerContent"), toast: document.querySelector("#toast"),
  exporter: document.querySelector("#articleExporter"), exportButton: document.querySelector("#exportArticles"), exportForm: document.querySelector("#articleExportForm"), cancelExport: document.querySelector("#cancelArticleExport"),
};

function toast(message) { elements.toast.textContent = message; elements.toast.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => elements.toast.classList.remove("show"), 2600); }
function beijingDateValue(dayOffset = 0) {
  // 采集端和 MongoDB 均按北京时间存储日期，浏览器端也必须使用同一日历口径。
  const date = new Date(Date.now() + dayOffset * 24 * 60 * 60 * 1000);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function applyLinkedAccountFilter() {
  const linked = new URLSearchParams(window.location.search);
  const account = linked.get("account");
  if (account) elements.account.value = account;
  // 从账号管理跳转时默认查看全部日期，避免只显示今天造成“没有文章”的误解。
  if (linked.get("date") === "all") elements.date.value = "";
}
function params() { return new URLSearchParams({ date: elements.date.value || "all", account: elements.account.value.trim(), q: elements.query.value.trim(), min_share: elements.minimumShare.value.trim(), sort: elements.sort.value, limit: String(pageSize), offset: String(offset) }); }
function number(value) { return typeof value === "number" ? value.toLocaleString("zh-CN") : "—"; }
function safeText(value) { return value || "—"; }

function render(items) {
  elements.list.replaceChildren();
  if (!items.length) { const empty = document.createElement("p"); empty.className = "article-empty"; empty.textContent = "当前条件下没有文章。可以改为“全部”或调整公众号、标题筛选。"; elements.list.appendChild(empty); return; }
  items.forEach((item) => {
    const card = document.createElement("button"); card.className = "article-card"; card.type = "button";
    const heading = document.createElement("strong"); heading.textContent = item.title;
    const account = document.createElement("span"); account.className = "article-account"; account.textContent = item.account_name;
    const meta = document.createElement("span"); meta.className = "article-card-meta"; meta.textContent = `${safeText(item.publish_time)} · 转发 ${number(item.share_count)} · ${item.content_available ? "正文已保存" : "正文待补齐"}`;
    card.append(heading, account, meta); card.addEventListener("click", () => openArticle(item.id)); elements.list.appendChild(card);
  });
}

async function loadArticles() {
  elements.search.disabled = true; elements.search.textContent = "查询中…";
  try {
    const response = await fetch(`/api/articles?${params()}`); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || "读取文章失败");
    total = Number(data.total || 0); render(data.items || []); const start = total ? offset + 1 : 0; const end = Math.min(offset + pageSize, total);
    elements.summary.textContent = `共 ${total.toLocaleString("zh-CN")} 篇，当前显示 ${start}-${end} 篇。`;
    elements.page.textContent = `第 ${Math.floor(offset / pageSize) + 1} 页`;
    elements.previous.disabled = offset === 0; elements.next.disabled = offset + pageSize >= total;
  } catch (error) { elements.summary.textContent = "文章列表暂时无法读取。"; render([]); toast(error.message || "读取文章失败"); }
  finally { elements.search.disabled = false; elements.search.textContent = "查询文章"; }
}

async function openArticle(id) {
  elements.drawer.hidden = false; elements.backdrop.hidden = false; elements.drawerContent.textContent = "正在读取正文…";
  try {
    const response = await fetch(`/api/articles/${encodeURIComponent(id)}`); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || "读取文章详情失败"); const item = data.item;
    elements.drawerAccount.textContent = item.account_name; elements.drawerTitle.textContent = item.title; elements.drawerMeta.textContent = `${safeText(item.publish_time)} · 转发 ${number(item.interaction?.shareCount)} · 最近采集 ${safeText(item.last_updated_at)}`;
    elements.drawerUrl.href = item.url || "#"; elements.drawerUrl.hidden = !item.url; elements.drawerContent.textContent = item.content || "这篇文章尚未保存纯文本正文。";
  } catch (error) {
    // 抽屉内直接说明失败原因；仅弹 Toast 很容易在几秒后消失，用户无法判断是否需要重试。
    const message = error.message || "读取文章详情失败";
    elements.drawerContent.textContent = `无法读取文章详情：${message}\n\n请确认 MongoDB 服务可连接后重试。`;
    toast(message);
  }
}
function closeDrawer() { elements.drawer.hidden = true; elements.backdrop.hidden = true; }
function exportArticles(event) {
  event.preventDefault();
  const format = new FormData(elements.exportForm).get("articleExportFormat") || "csv";
  const exportQuery = params();
  exportQuery.delete("limit");
  exportQuery.delete("offset");
  exportQuery.set("format", format);
  // 导出严格复用当前筛选条件，避免用户看到的列表与下载文件范围不同。
  window.location.assign(`/api/articles/export?${exportQuery}`);
  elements.exporter.close();
}
elements.search.addEventListener("click", () => { offset = 0; loadArticles(); });
[elements.date, elements.sort].forEach((item) => item.addEventListener("change", () => { offset = 0; loadArticles(); }));
elements.todayPreset.addEventListener("click", () => { elements.date.value = beijingDateValue(); offset = 0; loadArticles(); });
elements.yesterdayPreset.addEventListener("click", () => { elements.date.value = beijingDateValue(-1); offset = 0; loadArticles(); });
elements.clearDate.addEventListener("click", () => { elements.date.value = ""; offset = 0; loadArticles(); });
[elements.account, elements.query, elements.minimumShare].forEach((item) => item.addEventListener("keydown", (event) => { if (event.key === "Enter") { offset = 0; loadArticles(); } }));
elements.previous.addEventListener("click", () => { offset = Math.max(0, offset - pageSize); loadArticles(); });
elements.next.addEventListener("click", () => { offset += pageSize; loadArticles(); });
elements.close.addEventListener("click", closeDrawer); elements.backdrop.addEventListener("click", closeDrawer);
elements.exportButton.addEventListener("click", () => elements.exporter.showModal());
elements.cancelExport.addEventListener("click", () => elements.exporter.close());
elements.exportForm.addEventListener("submit", exportArticles);
elements.date.value = beijingDateValue();
applyLinkedAccountFilter();
loadArticles();
