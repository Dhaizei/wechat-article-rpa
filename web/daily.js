const state = { category: "all", account: "", data: null, reportRequest: 0, activeRequest: null };

const elements = {
  date: document.querySelector("#reportDate"), previous: document.querySelector("#previousDay"), next: document.querySelector("#nextDay"),
  status: document.querySelector("#dailyStatus"), lead: document.querySelector("#dailyLead"), hot: document.querySelector("#hotList"),
  categories: document.querySelector("#dailyCategories"), feed: document.querySelector("#dailyFeed"), feedTitle: document.querySelector("#feedTitle"), feedSummary: document.querySelector("#feedSummary"),
  activity: document.querySelector("#accountActivity"), toast: document.querySelector("#dailyToast"),
};

function beijingDate(dayOffset = 0) {
  const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(Date.now() + dayOffset * 86400000));
  const values = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function text(value, fallback = "—") { return String(value || fallback); }
function toast(message) { elements.toast.textContent = message; elements.toast.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3200); }
function syncReportUrl() {
  // 日期切换不触发整页跳转，但需要同步到地址栏：刷新、复制链接或前进后退时仍能回到同一份日报。
  const params = new URLSearchParams(window.location.search);
  params.set("date", elements.date.value);
  if (state.account) params.set("account", state.account); else params.delete("account");
  const query = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
}
function addLink(parent, item, className = "") {
  const link = document.createElement(item.url ? "a" : "span");
  if (item.url) { link.href = item.url; link.target = "_blank"; link.rel = "noreferrer"; }
  link.className = className; link.textContent = item.title; parent.appendChild(link);
}

function renderLead(item) {
  elements.lead.replaceChildren();
  if (!item) { elements.lead.innerHTML = '<p class="daily-empty">当天尚未发现文章，切换日期后再查看。</p>'; return; }
  const content = document.createElement("div"); content.className = "lead-copy";
  const category = document.createElement("p"); category.className = "lead-category"; category.textContent = item.category;
  const title = document.createElement(item.url ? "a" : "h1"); title.className = "lead-title"; title.textContent = item.title;
  if (item.url) { title.href = item.url; title.target = "_blank"; title.rel = "noreferrer"; }
  const excerpt = document.createElement("p"); excerpt.className = "lead-excerpt"; excerpt.textContent = item.excerpt || "已采集文章正文，可打开原文阅读。";
  const meta = document.createElement("p"); meta.className = "lead-meta"; meta.textContent = `${item.account_name} · ${item.publish_time} · 转发 ${number(item.share_count)}`;
  content.append(category, title, excerpt, meta);
  const marker = document.createElement("div"); marker.className = "lead-marker"; marker.setAttribute("aria-hidden", "true"); marker.textContent = "今日主推";
  elements.lead.append(content, marker);
}

function renderHot(items) {
  elements.hot.replaceChildren();
  if (!items.length) { elements.hot.innerHTML = '<li class="daily-empty">没有可展示的热点文章。</li>'; return; }
  items.forEach((item, index) => {
    const row = document.createElement("li");
    const rank = document.createElement("span"); rank.className = `hot-rank rank-${index + 1}`; rank.textContent = String(index + 1);
    const main = document.createElement("div"); main.className = "hot-main"; addLink(main, item, "hot-title");
    const account = document.createElement("small"); account.textContent = item.account_name; main.appendChild(account);
    const shares = document.createElement("span"); shares.className = "hot-share"; shares.textContent = number(item.share_count);
    row.append(rank, main, shares); elements.hot.appendChild(row);
  });
}

function renderCategories(categories, selected) {
  elements.categories.replaceChildren();
  categories.forEach((category) => {
    const button = document.createElement("button"); button.type = "button"; button.textContent = `${category.label} ${category.count}`;
    button.className = category.key === selected ? "active" : "";
    button.addEventListener("click", () => { if (category.key !== state.category) { state.category = category.key; loadReport(); } });
    elements.categories.appendChild(button);
  });
}

function renderFeed(items) {
  elements.feed.replaceChildren();
  if (!items.length) { elements.feed.innerHTML = '<p class="daily-empty">该分类当天没有文章。</p>'; return; }
  items.forEach((item) => {
    const article = document.createElement("article"); article.className = "daily-article";
    const account = document.createElement("p"); account.className = "article-account-name"; account.textContent = item.account_name;
    const title = document.createElement(item.url ? "a" : "h3"); title.className = "article-title"; title.textContent = item.title;
    if (item.url) { title.href = item.url; title.target = "_blank"; title.rel = "noreferrer"; }
    const excerpt = document.createElement("p"); excerpt.className = "article-excerpt"; excerpt.textContent = item.excerpt || "暂无可展示的正文摘要。";
    const meta = document.createElement("p"); meta.className = "article-meta"; meta.textContent = `${item.publish_time} · 转发 ${number(item.share_count)}`;
    article.append(account, title, excerpt, meta); elements.feed.appendChild(article);
  });
}

function renderActivity(accounts) {
  elements.activity.replaceChildren();
  if (!accounts.length) { elements.activity.innerHTML = '<p class="daily-empty">当天尚无公众号动态。</p>'; return; }
  accounts.forEach((account) => {
    const group = document.createElement("section"); group.className = "activity-group";
    const head = document.createElement("div"); head.className = "activity-head";
    const name = document.createElement("h3"); name.textContent = account.account_name;
    const count = document.createElement("span"); count.textContent = `今日 ${account.count} 篇`;
    head.append(name, count); group.appendChild(head);
    const list = document.createElement("ul");
    account.articles.forEach((item) => { const row = document.createElement("li"); addLink(row, item); const time = document.createElement("time"); time.textContent = item.publish_time.slice(11) || item.publish_time; row.appendChild(time); list.appendChild(row); });
    group.appendChild(list); elements.activity.appendChild(group);
  });
}

function render(data) {
  state.data = data;
  const accountName = String(data.selected_account || state.account || "").trim();
  const excludedCount = Number(data.summary.excluded_count || 0);
  // 招聘类内容在日报展示层自动隐藏，但保留提示，避免团队误以为采集遗漏。
  elements.status.textContent = `${number(data.summary.article_count)} 篇文章 · ${number(data.summary.account_count)} 个公众号${excludedCount ? ` · 已过滤 ${number(excludedCount)} 篇招聘内容` : ""}`;
  renderLead(data.lead); renderHot(data.hot_items); renderCategories(data.categories, data.selected_category); renderFeed(data.feed_items); renderActivity(data.account_activity);
  const categoryLabel = data.selected_category === "all" ? "全部分类" : data.selected_category;
  // 从目录进入时明确当前来源账号，避免团队成员误以为仍在查看全量文章。
  // 历史日期仍沿用同一页面时，不能继续写“今日”，否则团队成员会误把归档内容当成当天内容。
  // 按公众号查看时同样保留日期，避免只看到公众号名称而误以为列表总是当天数据。
  elements.feedTitle.textContent = accountName
    ? `${data.date_label} · ${accountName} 的文章`
    : `${data.date_label} 文章`;
  elements.feedSummary.textContent = `${data.date_label} · ${accountName || categoryLabel} · 按转发排序 · 展示 ${number(data.summary.visible_count)} 篇`;
}

async function loadReport() {
  // 日期连点会并发发起多个请求；只允许最后一次选择回写页面，避免旧响应覆盖新日期。
  const requestId = ++state.reportRequest;
  // 主动中止上一份日报请求，避免网络较慢时旧日期请求堆积，使最后一次点击更快得到可见反馈。
  if (state.activeRequest) state.activeRequest.abort();
  const controller = new AbortController();
  state.activeRequest = controller;
  syncReportUrl();
  elements.status.textContent = "正在更新日报…";
  try {
    const params = new URLSearchParams({ date: elements.date.value, category: state.category });
    if (state.account) params.set("account", state.account);
    const response = await fetch(`/api/daily-report?${params}`, { signal: controller.signal }); const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "读取日报失败");
    if (requestId !== state.reportRequest) return;
    render(data);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (requestId !== state.reportRequest) return;
    elements.status.textContent = "日报暂时无法读取";
    elements.lead.innerHTML = '<p class="daily-empty">日报数据暂时不可用，请稍后重试。</p>';
    elements.hot.replaceChildren(); elements.feed.replaceChildren(); elements.activity.replaceChildren();
    toast(error.message || "读取日报失败");
  } finally {
    if (state.activeRequest === controller) state.activeRequest = null;
  }
}

function shiftDate(days) {
  const [year, month, day] = elements.date.value.split("-").map(Number);
  // 日报筛选使用北京时间自然日。这里用 UTC 日历运算，避免 toISOString 在 UTC+8 零点回退一天。
  const current = new Date(Date.UTC(year, month - 1, day + days));
  const nextDate = [current.getUTCFullYear(), String(current.getUTCMonth() + 1).padStart(2, "0"), String(current.getUTCDate()).padStart(2, "0")].join("-");
  elements.date.value = nextDate;
  state.category = "all";
  loadReport();
}
elements.previous.addEventListener("click", () => shiftDate(-1));
elements.next.addEventListener("click", () => shiftDate(1));
elements.date.addEventListener("change", () => { state.category = "all"; loadReport(); });
// 公众号目录、每日新闻跳转到文章动态时可带日期，确保“查看原始文章”不丢失汇总范围。
const requestedParams = new URLSearchParams(window.location.search);
const requestedDate = requestedParams.get("date");
state.account = String(requestedParams.get("account") || "").trim();
elements.date.value = /^\d{4}-\d{2}-\d{2}$/.test(requestedDate || "") ? requestedDate : beijingDate();
loadReport();
