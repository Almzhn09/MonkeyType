"use strict";

const form = document.getElementById("company-form");
const create = document.getElementById("create-button");
const message = document.getElementById("form-message");
const list = document.getElementById("company-list");
const listMessage = document.getElementById("list-message");
const number = new Intl.NumberFormat("ru", { maximumFractionDigits: 3 });
const stateLabels = { queued: "В очереди", running: "Идёт аудит", done: "Аудит завершён", failed: "Ошибка аудита" };
let refreshTimer;
let requestOrder = 0;

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  result.textContent = text;
  return result;
}

function link(text, href) {
  const anchor = node("a", text);
  anchor.href = href;
  return anchor;
}

async function request(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401 || response.status === 403) {
    throw new Error("Откройте адрес, который Genesis показал при запуске.");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Запрос не выполнен.");
  return data;
}

function showMessage(text, error = false) {
  message.hidden = !text;
  message.textContent = text;
  message.classList.toggle("error", error);
}

function companyRow(company) {
  const item = node("li", "", "company");
  const title = node("div", "", "company-title");
  title.append(node("h3", company.label), node("span", stateLabels[company.status], `status ${company.status}`));
  const config = company.config;
  const details = company.result;
  const description = `${config.base_currency} · ${config.period.start_month} · ${config.size.invoices} сч.`;
  item.append(title, node("p", description, "company-meta"));
  if (details) {
    item.append(node("p", `${details.generated_company} · ${details.documents} документов · ${details.episode_id}`, "company-meta"));
    item.append(node("p", `Награда ${number.format(details.reward)} · F1 ${number.format(details.f1)}`, "company-score"));
    const actions = node("div", "", "company-actions");
    const report = link("Открыть отчёт", `/runs/${company.id}/report.html`);
    report.target = "_blank";
    report.rel = "noopener";
    actions.append(report, link("Траектория", `/runs/${company.id}/trajectories.jsonl`));
    item.append(actions);
  } else if (company.status === "failed") {
    item.append(node("p", company.error, "company-error"));
    const retry = node("button", "Повторить аудит", "retry");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        await request(`/api/companies/${company.id}/retry`, { method: "POST", body: "" });
        await refresh();
      } catch (error) {
        showMessage(error.message, true);
        retry.disabled = false;
      }
    });
    item.append(retry);
  }
  return item;
}

async function refresh() {
  clearTimeout(refreshTimer);
  const order = ++requestOrder;
  try {
    const companies = await request("/api/companies");
    if (order !== requestOrder) return;
    const completed = companies.filter(company => company.status === "done");
    list.replaceChildren(...companies.map(companyRow));
    document.getElementById("company-count").textContent = number.format(companies.length);
    document.getElementById("run-count").textContent = number.format(completed.length);
    document.getElementById("step-count").textContent = number.format(completed.reduce((sum, company) => sum + company.result.steps, 0));
    document.getElementById("export").hidden = completed.length === 0;
    listMessage.hidden = companies.length > 0;
    listMessage.textContent = "Добавьте вымышленную компанию, чтобы запустить первый аудит.";
    if (companies.some(company => company.status === "queued" || company.status === "running")) {
      refreshTimer = setTimeout(refresh, 1000);
    }
  } catch (error) {
    if (order !== requestOrder) return;
    listMessage.hidden = false;
    listMessage.textContent = error.message;
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  const fields = new FormData(form);
  const payload = Object.fromEntries(fields);
  payload.invoices = Number(payload.invoices);
  payload.months = Number(payload.months);
  payload.scenarios = fields.getAll("scenarios");
  create.disabled = true;
  showMessage("Добавляем компанию…");
  try {
    await request("/api/companies", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    form.elements.name.value = "";
    form.elements.seed.value = "";
    showMessage("Компания добавлена. Статус аудита виден в списке.");
    await refresh();
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    create.disabled = false;
  }
});

refresh();
