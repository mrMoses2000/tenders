const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const initData = tg?.initData || "";
const base = new URL(".", window.location.href).pathname;
const $ = id => document.getElementById(id);
const state = {tab: "cases", q: "", status: "", city: "", offset: 0, loading: false, controller: null};

const labels = {
  draft: "Черновик", needs_clarification: "Нужно уточнить", ready: "Готова к поиску",
  researching: "Поиск", contacting: "Поставщики", evaluating: "Оценка",
  report_ready: "Отчёт готов", selected: "Выбрана", closed: "Закрыта",
  cancelled: "Отменена", candidate: "Кандидат", active: "Активный",
  inactive: "Неактивный", blocked: "Заблокирован", merged: "Объединён",
  lead: "Лид", confirm: "Подтвердить", exact: "Совпадает",
  mismatch: "Не подходит", not_found: "Не найден", withdrawn: "Снят"
};

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function fmtDate(value) {
  return value ? new Intl.DateTimeFormat("ru-KZ", {day: "numeric", month: "short", year: "numeric"}).format(new Date(value)) : "Не указана";
}
function fmtNumber(value) {
  return new Intl.NumberFormat("ru-KZ", {maximumFractionDigits: 2}).format(Number(value));
}
function countLabel(count, one, few, many) {
  const value = Number(count);
  const mod10 = value % 10;
  const mod100 = value % 100;
  return value + " " + (mod10 === 1 && mod100 !== 11 ? one :
    mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? few : many);
}
function badge(status) {
  const tone = ["needs_clarification", "confirm"].includes(status) ? "warn" :
    ["cancelled", "blocked", "mismatch"].includes(status) ? "danger" :
      ["closed", "draft", "inactive", "merged"].includes(status) ? "mute" : "";
  return el("span", "badge " + tone, labels[status] || status);
}
async function api(path, signal) {
  let response;
  try {
    response = await fetch(base + path.replace(/^\//, ""), {
      headers: {Authorization: "tma " + initData}, signal
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("Нет соединения с сервером");
  }
  if (!response.ok) throw new Error(response.status === 401 || response.status === 403
    ? "Откройте приложение через Telegram" : "Не удалось загрузить данные");
  return response.json();
}
function empty(title, description) {
  const box = el("div", "empty");
  box.append(el("span", "empty-symbol", "00"), el("h3", "", title), el("p", "", description));
  return box;
}
function meta(...values) {
  const node = el("div", "record-meta");
  values.filter(Boolean).forEach(value => node.append(el("span", "", value)));
  return node;
}
function caseCard(row, position) {
  const card = el("button", "record");
  card.type = "button";
  card.setAttribute("aria-label", "Открыть заявку " + (row.title || "без названия"));
  const body = el("div", "record-body");
  body.append(
    el("span", "record-title", row.title || "Заявка без названия"),
    meta(row.city || "Город не указан", countLabel(row.item_count, "позиция", "позиции", "позиций"), row.customer_name)
  );
  const bottom = el("div", "record-bottom");
  bottom.append(badge(row.status), el("span", "", fmtDate(row.updated_at)));
  body.append(bottom);
  card.append(el("span", "record-index", String(position).padStart(2, "0")), body, el("span", "record-arrow", "↗"));
  card.addEventListener("click", () => openDetail(row.id));
  return card;
}
function supplierCard(row, position) {
  const card = el("div", "record");
  const body = el("div", "record-body");
  body.append(el("span", "record-title", row.display_name), meta(countLabel(row.offer_count, "предложение", "предложения", "предложений")));
  const bottom = el("div", "record-bottom");
  bottom.append(badge(row.status), el("span", "", row.last_offer_at ? fmtDate(row.last_offer_at) : "Проверка не проводилась"));
  body.append(bottom);
  card.append(el("span", "record-index", String(position).padStart(2, "0")), body);
  return card;
}
function setTab(tab) {
  state.tab = tab;
  state.q = "";
  state.status = "";
  state.city = "";
  $("search").value = "";
  $("status-filter").value = "";
  $("city-filter").value = "";
  document.querySelectorAll(".nav-item").forEach(node => {
    const active = node.dataset.tab === tab;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  });
  $("page-title").textContent = tab === "cases" ? "Мои заявки" : "Поставщики";
  $("page-index").textContent = tab === "cases" ? "01 / 02" : "02 / 02";
  $("section-kicker").textContent = tab === "cases" ? "РЕЕСТР / 01" : "КАТАЛОГ / 02";
  $("page-subtitle").textContent = tab === "cases"
    ? "Текущий статус, позиции и предложения — без потери контекста."
    : "Каждый контакт связан с конкретными предложениями по вашим заявкам.";
  $("list-title").textContent = tab === "cases" ? "Заявки" : "Поставщики";
  $("search").placeholder = tab === "cases" ? "Поиск по заявкам и городам" : "Поиск поставщика";
  $("status-filter").hidden = tab !== "cases";
  $("city-filter").hidden = tab !== "cases";
  $("result-count").textContent = "";
  if (initData) load();
  else $("list").replaceChildren(empty("Вход через Telegram", "Откройте Mini App из меню вашего бота, чтобы увидеть заявки."));
}
async function load(append = false) {
  state.controller?.abort();
  state.controller = new AbortController();
  const signal = state.controller.signal;
  state.loading = true;
  if (!append) {
    state.offset = 0;
    $("list").replaceChildren(empty("Загружаем данные", "Сверяемся с базой закупок."));
  }
  const params = new URLSearchParams({limit: "20", offset: String(state.offset)});
  if (state.q) params.set("q", state.q);
  if (state.tab === "cases") {
    if (state.status) params.set("status", state.status);
    if (state.city) params.set("city", state.city);
  }
  try {
    const data = await api("/api/" + state.tab + "?" + params, signal);
    if (signal.aborted) return;
    if (!append) $("list").replaceChildren();
    data.rows.forEach((row, index) => $("list").append(state.tab === "cases"
      ? caseCard(row, state.offset + index + 1) : supplierCard(row, state.offset + index + 1)));
    if (!data.rows.length && !append) {
      const filtered = Boolean(state.q || state.status || state.city);
      $("list").append(empty(
        filtered ? "Совпадений нет" : state.tab === "cases" ? "Заявок пока нет" : "Поставщиков пока нет",
        filtered ? "Измените запрос или сбросьте фильтры." : state.tab === "cases"
          ? "Отправьте первую заявку боту — после обработки она появится здесь."
          : "Поставщики появятся, когда бот найдёт предложения по вашим заявкам."
      ));
    }
    if (state.tab === "cases" && data.cities) {
      const selected = state.city;
      $("city-filter").replaceChildren(new Option("Все города", ""));
      data.cities.forEach(city => $("city-filter").add(new Option(city, city)));
      $("city-filter").value = selected;
    }
    state.offset += data.rows.length;
    $("load-more").hidden = !data.has_more;
    $("result-count").textContent = state.offset ? String(state.offset).padStart(2, "0") + " / ПОКАЗАНО" : "";
  } catch (error) {
    if (!signal.aborted) {
      $("list").replaceChildren(empty(error.message, "Проверьте подключение и повторите попытку."));
      $("load-more").hidden = true;
    }
  } finally {
    if (!signal.aborted) state.loading = false;
  }
}
async function openDetail(id) {
  const dialog = $("detail");
  const root = $("detail-content");
  root.replaceChildren(empty("Загружаем заявку", "Сверяем позиции и предложения."));
  dialog.showModal();
  try {
    const data = await api("/api/cases/" + encodeURIComponent(id));
    const c = data.case;
    const body = el("div", "detail-body");
    body.append(badge(c.status), el("h2", "", c.title || "Заявка без названия"),
      el("p", "detail-sub", [c.city, c.customer_name].filter(Boolean).join(" / ") || "Основная информация"));
    const grid = el("div", "info-grid");
    [["Город", c.city || "Не указан"], ["Срок", fmtDate(c.deadline_at)],
      ["Позиций", String(data.items.length)], ["Обновлено", fmtDate(c.updated_at)]].forEach(([key, value]) => {
      const cell = el("div", "info-cell");
      cell.append(el("small", "", key), el("strong", "", value));
      grid.append(cell);
    });
    body.append(grid, el("h3", "", "Позиции"));
    if (!data.items.length) body.append(empty("Позиций пока нет", "Бот добавит их после обработки заявки."));
    data.items.forEach(item => {
      const box = el("div", "item-card");
      const head = el("div", "item-head");
      head.append(el("span", "", item.line_number + ". " + item.name), badge(item.status));
      box.append(head);
      if (item.quantity) box.append(el("p", "item-spec", fmtNumber(item.quantity) + " " + (item.unit || "")));
      if (item.specification_text) box.append(el("p", "item-spec", item.specification_text));
      data.offers.filter(offer => offer.request_item_id === item.id).forEach(offer => {
        const line = el("div", "offer-line");
        line.append(el("strong", "", offer.supplier_name), el("span", "", [
          labels[offer.status] || offer.status,
          offer.price_amount !== null ? fmtNumber(offer.price_amount) + " " + (offer.currency || "") : "Цена не указана"
        ].join(" / ")));
        box.append(line);
      });
      body.append(box);
    });
    root.replaceChildren(body);
  } catch (error) {
    root.replaceChildren(empty(error.message, "Повторите попытку позже."));
  }
}
async function refreshDashboard() {
  if (!initData) return;
  try {
    const summary = await api("/api/summary");
    $("active-count").textContent = summary.active_cases;
    $("items-count").textContent = summary.items;
    $("attention-count").textContent = summary.needs_attention;
    await load();
  } catch (error) {
    $("list").replaceChildren(empty(error.message, "Повторите попытку позже."));
  }
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => setTab(button.dataset.tab)));
let debounce;
$("search").addEventListener("input", event => {
  clearTimeout(debounce);
  debounce = setTimeout(() => { state.q = event.target.value.trim(); load(); }, 250);
});
$("status-filter").addEventListener("change", event => { state.status = event.target.value; load(); });
$("city-filter").addEventListener("change", event => { state.city = event.target.value; load(); });
$("load-more").addEventListener("click", () => { if (!state.loading) load(true); });
$("refresh").addEventListener("click", refreshDashboard);
$("close-detail").addEventListener("click", () => $("detail").close());

async function start() {
  if (!initData) {
    document.querySelector(".stats").hidden = true;
    $("controls").hidden = true;
    $("refresh").hidden = true;
    $("list").replaceChildren(empty("Вход через Telegram", "Откройте Mini App из меню вашего бота, чтобы увидеть заявки."));
    return;
  }
  try {
    const user = await api("/api/me");
    $("account-name").textContent = user.display_name || "Мой кабинет";
    await refreshDashboard();
  } catch (error) {
    document.querySelector(".stats").hidden = true;
    $("controls").hidden = true;
    $("refresh").hidden = true;
    $("list").replaceChildren(empty(error.message, "Повторите вход через бота."));
  }
}
start();
