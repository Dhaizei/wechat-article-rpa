const state = { query: "", category: "", offset: 0, limit: 20, total: 0, selected: "", items: [] };
const elements = {
  query: document.querySelector("#accountQuery"), tabs: document.querySelector("#categoryTabs"),
  rows: document.querySelector("#directoryRows"), summary: document.querySelector("#directorySummary"),
  previous: document.querySelector("#previousPage"), next: document.querySelector("#nextPage"),
  preview: document.querySelector("#accountPreview"), total: document.querySelector("#totalAccounts"),
  covered: document.querySelector("#coveredAccounts"), missing: document.querySelector("#missingAccounts"),
  toast: document.querySelector("#directoryToast"),
};

function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function toast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3400);
}

function renderTabs(categories) {
  elements.tabs.replaceChildren();
  [{ name: "", label: "全部" }, ...categories.map((item) => ({ name: item, label: item }))].forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = item.name === state.category ? "active" : "";
    button.textContent = item.label;
    button.addEventListener("click", () => {
      if (item.name === state.category) return;
      state.category = item.name;
      state.offset = 0;
      loadAccounts();
    });
    elements.tabs.append(button);
  });
}

function renderRows(items) {
  elements.rows.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "没有符合条件的公众号。";
    row.append(cell);
    elements.rows.append(row);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    if (item.name === state.selected) row.className = "selected";
    const name = document.createElement("td");
    const strong = document.createElement("strong"); strong.textContent = item.name;
    const small = document.createElement("small");
    small.textContent = item.search_name !== item.name ? `采集搜索名：${item.search_name}` : item.account_type;
    name.append(strong, small);
    const category = document.createElement("td"); category.textContent = item.category;
    const latest = document.createElement("td"); latest.textContent = item.latest_publish || "—";
    const count = document.createElement("td"); count.textContent = number(item.article_count);
    const status = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill${item.coverage_status === "missing" ? " pending" : ""}`;
    pill.textContent = item.coverage_status === "covered" ? "已入库" : "待核对";
    status.append(pill);
    row.append(name, category, latest, count, status);
    row.addEventListener("click", () => selectAccount(item));
    elements.rows.append(row);
  });
}

async function selectAccount(item) {
  state.selected = item.name;
  renderRows(state.items);
  elements.preview.innerHTML = '<p class="directory-empty">正在读取最近文章…</p>';
  try {
    const response = await fetch(`/api/articles?date=all&account=${encodeURIComponent(item.name)}&sort=publish_desc&limit=3`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "读取文章失败");
    renderPreview(item, data.items || []);
  } catch (error) {
    elements.preview.innerHTML = '<p class="directory-empty">无法读取该公众号文章，请稍后重试。</p>';
    toast(error.message || "读取文章失败");
  }
}

function renderPreview(account, articles) {
  elements.preview.replaceChildren();
  const head = document.createElement("div"); head.className = "preview-head";
  const title = document.createElement("h2"); title.textContent = account.name;
  const category = document.createElement("span"); category.className = "preview-category"; category.textContent = account.category;
  head.append(title, category);
  const subtitle = document.createElement("p"); subtitle.className = "preview-subtitle";
  subtitle.textContent = `采集搜索名：${account.search_name} · 已入库 ${number(account.article_count)} 篇`;
  const list = document.createElement("ol"); list.className = "preview-articles";
  if (!articles.length) {
    const empty = document.createElement("li"); empty.textContent = "该公众号尚未发现已入库文章。"; list.append(empty);
  }
  articles.forEach((article) => {
    const row = document.createElement("li");
    const link = document.createElement(article.url ? "a" : "span"); link.textContent = article.title;
    if (article.url) { link.href = article.url; link.target = "_blank"; link.rel = "noreferrer"; }
    const meta = document.createElement("small"); meta.textContent = `${article.publish_time || "—"} · 转发 ${number(article.share_count)}`;
    row.append(link, meta); list.append(row);
  });
  // 团队端只跳转到只读文章动态，不暴露采集控制台路径。
  const params = new URLSearchParams({ account: account.name });
  const latestDate = String(account.latest_publish || "").slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(latestDate)) params.set("date", latestDate);
  const action = document.createElement("a"); action.className = "preview-action";
  action.href = `/daily.html?${params}`;
  action.textContent = "查看该公众号最新文章";
  elements.preview.append(head, subtitle, list, action);
}

async function loadAccounts() {
  try {
    const params = new URLSearchParams({ q: state.query, category: state.category, limit: String(state.limit), offset: String(state.offset) });
    const response = await fetch(`/api/accounts?${params}`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "读取公众号目录失败");
    state.items = data.items || [];
    state.total = Number(data.total || 0);
    if (!state.items.some((item) => item.name === state.selected)) state.selected = "";
    renderTabs(data.categories || []);
    renderRows(state.items);
    elements.summary.textContent = `共 ${number(data.total)} 个公众号，当前显示 ${state.offset + (state.items.length ? 1 : 0)}–${state.offset + state.items.length} 个。`;
    elements.total.textContent = number(data.summary?.total);
    elements.covered.textContent = number(data.summary?.covered);
    elements.missing.textContent = number(data.summary?.missing);
    elements.previous.disabled = state.offset <= 0;
    elements.next.disabled = state.offset + state.limit >= state.total;
    if (!state.selected && state.items[0]) selectAccount(state.items[0]);
  } catch (error) {
    elements.rows.innerHTML = '<tr><td colspan="5">公众号目录暂时无法读取。</td></tr>';
    toast(error.message || "读取公众号目录失败");
  }
}

let queryTimer;
elements.query.addEventListener("input", () => {
  clearTimeout(queryTimer);
  queryTimer = setTimeout(() => { state.query = elements.query.value.trim(); state.offset = 0; loadAccounts(); }, 280);
});
elements.previous.addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadAccounts(); });
elements.next.addEventListener("click", () => { state.offset += state.limit; loadAccounts(); });
loadAccounts();
