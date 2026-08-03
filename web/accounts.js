const pageSize = 20;
let offset = 0;
let total = 0;
let importPayload = null;

const elements = {
  query: document.querySelector("#accountQuery"),
  category: document.querySelector("#categoryFilter"),
  status: document.querySelector("#statusFilter"),
  search: document.querySelector("#searchAccounts"),
  list: document.querySelector("#accountList"),
  summary: document.querySelector("#accountResultSummary"),
  previous: document.querySelector("#previousPage"),
  next: document.querySelector("#nextPage"),
  page: document.querySelector("#pageText"),
  toast: document.querySelector("#toast"),
  totalAccounts: document.querySelector("#totalAccounts"),
  missingAccounts: document.querySelector("#missingAccounts"),
  aliasAccounts: document.querySelector("#aliasAccounts"),
  missingAlert: document.querySelector("#missingAlert"),
  missingAlertCount: document.querySelector("#missingAlertCount"),
  showMissing: document.querySelector("#showMissingAccounts"),
  overviewButtons: document.querySelectorAll("[data-account-status]"),
  create: document.querySelector("#createAccount"),
  exporter: document.querySelector("#accountExporter"),
  exportButton: document.querySelector("#exportAccounts"),
  exportForm: document.querySelector("#exportForm"),
  cancelExport: document.querySelector("#cancelExport"),
  importer: document.querySelector("#accountImporter"),
  importButton: document.querySelector("#importAccounts"),
  importFile: document.querySelector("#importFile"),
  importPreview: document.querySelector("#importPreview"),
  previewImport: document.querySelector("#previewImport"),
  applyImport: document.querySelector("#applyImport"),
  cancelImport: document.querySelector("#cancelImport"),
  editor: document.querySelector("#accountEditor"),
  editorTitle: document.querySelector("#accountEditorTitle"),
  accountForm: document.querySelector("#accountForm"),
  cancelEdit: document.querySelector("#cancelEdit"),
  saveAccount: document.querySelector("#saveAccount"),
  editRecordId: document.querySelector("#editRecordId"),
  editName: document.querySelector("#editName"),
  editSearchName: document.querySelector("#editSearchName"),
  editSourceId: document.querySelector("#editSourceId"),
  editCategory: document.querySelector("#editCategory"),
  editAccountType: document.querySelector("#editAccountType"),
  renameNote: document.querySelector("#renameNote"),
  categoryOptions: document.querySelector("#categoryOptions"),
};

function toast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => elements.toast.classList.remove("show"), 2600);
}

function safeText(value) { return value || "—"; }
function number(value) { return typeof value === "number" ? value.toLocaleString("zh-CN") : "0"; }

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.message || "请求失败");
  return data;
}

function renderSummary(summary = {}) {
  // 接口在不同运行环境下可能把聚合数传为字符串；"0" 在 JavaScript 中是真值，
  // 若不先转数值就会错误展示“0 个待补齐”的黄色告警。
  const total = Number(summary.total) || 0;
  const missing = Number(summary.missing) || 0;
  const alias = Number(summary.alias) || 0;
  elements.totalAccounts.textContent = number(total);
  elements.missingAccounts.textContent = number(missing);
  elements.aliasAccounts.textContent = number(alias);
  elements.missingAlertCount.textContent = number(missing);
  elements.missingAlert.hidden = missing <= 0;
  updateOverviewSelection();
}

function updateOverviewSelection() {
  // 概览卡和筛选下拉框共用同一个状态，避免两处显示的筛选条件不一致。
  elements.overviewButtons.forEach((button) => {
    const selected = button.dataset.accountStatus === elements.status.value;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function renderCategories(categories = []) {
  const selected = elements.category.value;
  elements.category.replaceChildren(new Option("全部分类", ""));
  elements.categoryOptions.replaceChildren();
  categories.forEach((category) => {
    elements.category.add(new Option(category, category));
    elements.categoryOptions.appendChild(new Option(category, category));
  });
  // 查询后仍保留操作者选择的分类，避免页面状态被接口响应重置。
  elements.category.value = categories.includes(selected) ? selected : "";
}

function createCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function openEditor(item = null) {
  elements.accountForm.reset();
  const editing = Boolean(item);
  elements.editorTitle.textContent = editing ? "编辑公众号" : "添加公众号";
  elements.editRecordId.value = item?.id || "";
  elements.editName.value = item?.name || "";
  elements.editSearchName.value = item?.search_name || "";
  elements.editSourceId.value = item?.source_id || "";
  elements.editCategory.value = item?.category || "未分类";
  elements.editAccountType.value = item?.account_type || "公众号";
  elements.renameNote.hidden = !editing || !item.article_count;
  // 有历史文章时直接禁用改名，而不是让保存后才给出难理解的失败提示。
  elements.editName.readOnly = Boolean(editing && item.article_count);
  elements.editor.showModal();
}

function render(items) {
  elements.list.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "account-empty";
    cell.textContent = "没有符合当前筛选条件的公众号。可以调整条件后再次查询。";
    row.appendChild(cell);
    elements.list.appendChild(row);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    const nameText = document.createElement("strong");
    nameText.textContent = safeText(item.name);
    name.appendChild(nameText);

    const search = document.createElement("td");
    const searchName = document.createElement("span");
    searchName.textContent = safeText(item.search_name);
    search.appendChild(searchName);
    if (item.alias_configured) {
      const badge = document.createElement("span");
      badge.className = "alias-badge";
      badge.textContent = "别名";
      search.appendChild(badge);
    }

    const status = document.createElement("td");
    const statusBadge = document.createElement("span");
    statusBadge.className = `coverage-badge ${item.coverage_status}`;
    statusBadge.textContent = item.coverage_status === "covered" ? "已入库" : "尚未入库";
    status.appendChild(statusBadge);

    const action = document.createElement("td");
    action.className = "account-actions";
    const edit = document.createElement("button");
    edit.className = "account-action action-button";
    edit.type = "button";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openEditor(item));
    const link = document.createElement("a");
    link.className = "account-action";
    link.href = `/articles.html?account=${encodeURIComponent(item.name)}&date=all`;
    link.textContent = "查看文章";
    action.append(edit, link);

    row.append(name, createCell(safeText(item.category)), search, createCell(number(item.article_count)), createCell(safeText(item.latest_publish)), status, action);
    elements.list.appendChild(row);
  });
}

async function loadAccounts() {
  elements.search.disabled = true;
  elements.search.textContent = "查询中…";
  try {
    const params = new URLSearchParams({
      q: elements.query.value.trim(), category: elements.category.value,
      status: elements.status.value, limit: String(pageSize), offset: String(offset),
    });
    const data = await requestJson(`/api/accounts?${params}`);
    total = Number(data.total || 0);
    renderSummary(data.summary);
    renderCategories(data.categories || []);
    render(data.items || []);
    const start = total ? offset + 1 : 0;
    const end = Math.min(offset + pageSize, total);
    elements.summary.textContent = `共 ${total.toLocaleString("zh-CN")} 个账号，当前显示 ${start}-${end} 个。`;
    elements.page.textContent = `第 ${Math.floor(offset / pageSize) + 1} 页`;
    elements.previous.disabled = offset === 0;
    elements.next.disabled = offset + pageSize >= total;
  } catch (error) {
    elements.summary.textContent = "账号列表暂时无法读取。";
    render([]);
    toast(error.message || "读取公众号失败");
  } finally {
    elements.search.disabled = false;
    elements.search.textContent = "查询";
  }
}

function importFormat(file) { return file?.name.toLowerCase().endsWith(".json") ? "json" : "csv"; }

function renderImportPreview(summary, items) {
  elements.importPreview.hidden = false;
  elements.importPreview.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = `预览：新增 ${summary.create} 个，更新 ${summary.update} 个，错误 ${summary.error} 个`;
  elements.importPreview.appendChild(title);
  const detail = document.createElement("p");
  detail.textContent = summary.error ? "存在错误时不能保存。请在原文件中修正后重新预览。" : "确认后才会写入公众号配置；文章数据不会改变。";
  elements.importPreview.appendChild(detail);
  const problems = items.filter((item) => item.status === "error").slice(0, 5);
  if (problems.length) {
    const list = document.createElement("ul");
    problems.forEach((problem) => {
      const line = document.createElement("li");
      line.textContent = `第 ${problem.line} 行：${problem.message}`;
      list.appendChild(line);
    });
    elements.importPreview.appendChild(list);
  }
}

async function previewImport() {
  const file = elements.importFile.files[0];
  if (!file) { toast("请先选择 CSV 或 JSON 文件"); return; }
  elements.previewImport.disabled = true;
  elements.previewImport.textContent = "解析中…";
  try {
    importPayload = { format: importFormat(file), content: await file.text() };
    const data = await requestJson("/api/accounts/import/preview", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(importPayload),
    });
    renderImportPreview(data.summary, data.items || []);
    elements.applyImport.disabled = Boolean(data.summary.error);
  } catch (error) {
    importPayload = null;
    elements.applyImport.disabled = true;
    toast(error.message || "导入预览失败");
  } finally {
    elements.previewImport.disabled = false;
    elements.previewImport.textContent = "预览变更";
  }
}

async function applyImport() {
  if (!importPayload) { toast("请先预览导入文件"); return; }
  elements.applyImport.disabled = true;
  elements.applyImport.textContent = "保存中…";
  try {
    const data = await requestJson("/api/accounts/import/apply", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(importPayload),
    });
    elements.importer.close();
    toast(data.message || "公众号配置已保存");
    restartQuery();
  } catch (error) {
    toast(error.message || "保存导入配置失败");
  } finally {
    elements.applyImport.disabled = false;
    elements.applyImport.textContent = "确认保存";
  }
}

function resetImporter() {
  importPayload = null;
  elements.importFile.value = "";
  elements.importPreview.hidden = true;
  elements.importPreview.replaceChildren();
  elements.applyImport.disabled = true;
}

async function saveAccount(event) {
  event.preventDefault();
  elements.saveAccount.disabled = true;
  elements.saveAccount.textContent = "保存中…";
  try {
    const data = await requestJson("/api/accounts/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        record_id: elements.editRecordId.value,
        name: elements.editName.value.trim(),
        search_name: elements.editSearchName.value.trim(),
        source_id: elements.editSourceId.value.trim(),
        category: elements.editCategory.value.trim(),
        account_type: elements.editAccountType.value.trim(),
      }),
    });
    elements.editor.close();
    toast(data.message || "公众号配置已保存");
    restartQuery();
  } catch (error) {
    toast(error.message || "保存配置失败");
  } finally {
    elements.saveAccount.disabled = false;
    elements.saveAccount.textContent = "保存配置";
  }
}

function restartQuery() { offset = 0; loadAccounts(); }

function exportAccounts(event) {
  event.preventDefault();
  const format = new FormData(elements.exportForm).get("exportFormat") || "csv";
  // 导出是只读下载；交给浏览器处理，避免前端自行保存文件造成兼容性问题。
  window.location.assign(`/api/accounts/export?format=${encodeURIComponent(format)}`);
  elements.exporter.close();
}

elements.search.addEventListener("click", restartQuery);
elements.query.addEventListener("keydown", (event) => { if (event.key === "Enter") restartQuery(); });
elements.category.addEventListener("change", restartQuery);
elements.status.addEventListener("change", restartQuery);
elements.showMissing.addEventListener("click", () => { elements.status.value = "missing"; restartQuery(); });
elements.overviewButtons.forEach((button) => button.addEventListener("click", () => {
  elements.status.value = button.dataset.accountStatus || "all";
  restartQuery();
}));
elements.previous.addEventListener("click", () => { offset = Math.max(0, offset - pageSize); loadAccounts(); });
elements.next.addEventListener("click", () => { offset += pageSize; loadAccounts(); });
elements.create.addEventListener("click", () => openEditor());
elements.exportButton.addEventListener("click", () => elements.exporter.showModal());
elements.cancelExport.addEventListener("click", () => elements.exporter.close());
elements.exportForm.addEventListener("submit", exportAccounts);
elements.importButton.addEventListener("click", () => { resetImporter(); elements.importer.showModal(); });
elements.cancelEdit.addEventListener("click", () => elements.editor.close());
elements.cancelImport.addEventListener("click", () => elements.importer.close());
elements.accountForm.addEventListener("submit", saveAccount);
elements.previewImport.addEventListener("click", previewImport);
elements.applyImport.addEventListener("click", applyImport);
loadAccounts();
