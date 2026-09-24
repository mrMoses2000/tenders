const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const initData = tg?.initData || "";
const base = new URL(".", window.location.href).pathname;
const $ = id => document.getElementById(id);
const state = {tab: "cases", q: "", status: "", city: "", offset: 0, loading: false, controller: null};
const detailColumns = {spec: false, history: false, finance: false};
try { Object.assign(detailColumns, JSON.parse(localStorage.getItem("tender-detail-columns") || "{}")); } catch (_) { /* private storage */ }
const expenseNames = {delivery: "Доставка", handling: "Погрузка", packaging: "Упаковка",
  commission: "Комиссия", travel: "Поездка", storage: "Хранение", other: "Другое"};

const labels = {
  draft: "Черновик", needs_clarification: "Нужно уточнить", ready: "Готова к поиску",
  researching: "Поиск", contacting: "Поставщики", evaluating: "Оценка",
  report_ready: "Отчёт готов", selected: "Выбрана", closed: "Закрыта",
  cancelled: "Отменена", candidate: "Кандидат", active: "Активный",
  inactive: "Неактивный", blocked: "Заблокирован", merged: "Объединён",
  lead: "Лид", confirm: "Подтвердить", exact: "Совпадает",
  mismatch: "Не подходит", not_found: "Не найден", withdrawn: "Снят",
  in_stock: "В наличии", in_stock_unquantified: "Наличие без количества",
  out_of_stock: "Нет в наличии", preorder: "Под заказ"
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
function quantity(value, unit = "") {
  return value === null || value === undefined ? "—" : fmtNumber(value) + (unit ? " " + unit : "");
}
function detailOptions() {
  const panel = el("div", "detail-options");
  panel.append(el("span", "", "ПОКАЗЫВАТЬ"));
  [["spec", "Техспек"], ["history", "Этапы и количества"],
    ["finance", "Тендерная цена и расходы"]].forEach(([key, label]) => {
    const control = el("label", "detail-option");
    const input = el("input");
    input.type = "checkbox";
    input.checked = Boolean(detailColumns[key]);
    input.addEventListener("change", () => {
      detailColumns[key] = input.checked;
      document.querySelectorAll(`[data-detail-column="${key}"]`).forEach(node => { node.hidden = !input.checked; });
      try { localStorage.setItem("tender-detail-columns", JSON.stringify(detailColumns)); } catch (_) { /* private storage */ }
    });
    control.append(input, el("span", "", label));
    panel.append(control);
  });
  return panel;
}
function detailField(key, value, column) {
  const row = el("div", "item-field");
  if (column) { row.dataset.detailColumn = column; row.hidden = !detailColumns[column]; }
  row.append(el("span", "", key), el("strong", "", value));
  return row;
}
function offerCard(offer) {
  const line = el("div", "offer-line");
  const top = el("div", "offer-top");
  top.append(el("strong", "", offer.supplier_name), badge(offer.status));
  line.append(top, el("div", "offer-product", offer.supplier_product_name || "Товар не указан"));
  line.append(meta(
    offer.price_amount === null ? "Цена не указана" : "Цена из источника " + fmtNumber(offer.price_amount) + " " + (offer.currency === "KZT" ? "₸" : offer.currency || ""),
    offer.supplier_city, offer.availability_status === "unknown" ? "Наличие не подтверждено" : labels[offer.availability_status] || offer.availability_status,
    offer.observed_at ? "Проверено " + fmtDate(offer.observed_at) : "Дата не указана"
  ));
  if (offer.moq !== null && offer.moq !== undefined) line.append(el("div", "offer-product",
    "Минимальная партия: " + quantity(offer.moq, offer.price_unit === "piece" ? "шт" : "")));
  if (offer.pack_qty !== null && offer.pack_qty !== undefined) line.append(el("div", "offer-product",
    "В упаковке: " + quantity(offer.pack_qty, offer.price_unit === "piece" ? "шт" : "")));
  if (offer.source_url && /^https:\/\//i.test(offer.source_url)) {
    const source = el("a", "offer-source", "Открыть источник ↗");
    source.href = offer.source_url;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    line.append(source);
  }
  return line;
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
function itemBadge(item) {
  const hasFacts = item.event_count || item.purchased_quantity !== null || item.accepted_quantity !== null;
  if (hasFacts) {
    const required = item.quantity === null ? null : Number(item.quantity);
    const accepted = Number(item.accepted_quantity || 0);
    const cancelled = Number(item.cancelled_quantity || 0);
    const rejected = Number(item.rejected_quantity || 0);
    const delivered = Number(item.delivered_quantity || 0);
    const warehouse = Number(item.warehouse_received_quantity || 0);
    const label = required !== null && cancelled >= required ? "Отменено" :
      required !== null && accepted + cancelled >= required ? "Исполнено" :
        rejected > 0 ? "Отказ / замена" : accepted > 0 ? "Принято частично" :
          delivered > 0 ? "Передано заказчику" : warehouse > 0 ? "На складе" :
            "Закуплено";
    return el("span", "badge " + (rejected > 0 ? "danger" : ""), label);
  }
  if (item.source_record_url) return el("span", "badge warn", "История из Notion");
  return badge(item.status);
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
async function apiWrite(path, payload) {
  let response;
  try {
    response = await fetch(base + path.replace(/^\//, ""), {
      method: "POST", headers: {Authorization: "tma " + initData, "Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
  } catch (_) { throw new Error("Нет соединения с сервером"); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Не удалось сохранить запись");
  return data;
}
function selectField(label, options) {
  const wrapper = el("label", "entry-field");
  wrapper.append(el("span", "", label));
  const control = el("select");
  options.forEach(([value, text]) => control.add(new Option(text, value)));
  wrapper.append(control);
  return [wrapper, control];
}
function inputField(label, type, options = {}) {
  const wrapper = el("label", "entry-field");
  wrapper.append(el("span", "", label));
  const control = el("input");
  control.type = type;
  Object.assign(control, options);
  wrapper.append(control);
  return [wrapper, control];
}
function eventForm(caseId, item) {
  let idempotencyKey = crypto.randomUUID();
  let previousPayload = "";
  const details = el("details", "entry-panel");
  details.append(el("summary", "", "Зафиксировать событие по позиции"));
  const form = el("form", "entry-form");
  const [kindField, kind] = selectField("Событие", [
    ["warehouse_received", "Поступило на склад"],
    ["customer_delivered", "Передано заказчику"],
    ["customer_accepted", "Принято заказчиком"],
    ["customer_rejected", "Заказчик отклонил"],
    ["returned", "Товар возвращён"], ["cancelled", "Отменено"]
  ]);
  const [quantityField, quantityInput] = inputField("Количество (исправление — со знаком минус)", "number",
    {step: "0.000001", required: true, placeholder: item.unit || "Количество"});
  const [reasonField, reasonInput] = inputField("Причина / примечание", "text",
    {maxLength: 1000, placeholder: "Обязательно для отказа, возврата и отмены"});
  const submit = el("button", "entry-submit", "Сохранить событие");
  submit.type = "submit";
  const feedback = el("p", "entry-feedback");
  feedback.setAttribute("role", "status");
  form.append(kindField, quantityField, reasonField, submit, feedback,
    el("small", "data-note", "Запись будет помечена как сообщение пользователя до прикрепления документа."));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    submit.disabled = true;
    feedback.textContent = "Сохраняем…";
    try {
      const payload = {event_kind: kind.value,
        quantity_delta: quantityInput.value, reason: reasonInput.value.trim()
      };
      const fingerprint = JSON.stringify(payload);
      if (fingerprint !== previousPayload) idempotencyKey = crypto.randomUUID();
      previousPayload = fingerprint;
      await apiWrite("/api/cases/" + caseId + "/items/" + item.id + "/events", {
        ...payload, idempotency_key: idempotencyKey
      });
      await openDetail(caseId);
    } catch (error) { feedback.textContent = error.message; }
    finally { submit.disabled = false; }
  });
  details.append(form);
  return details;
}
function expenseForm(caseId, items) {
  let idempotencyKey = crypto.randomUUID();
  let previousPayload = "";
  const details = el("details", "entry-panel expense-entry");
  details.append(el("summary", "", "Добавить общий расход"));
  const form = el("form", "entry-form");
  const [categoryField, category] = selectField("Категория", Object.entries(expenseNames));
  const [kindField, kind] = selectField("Тип суммы", [["actual", "Факт"], ["planned", "План"]]);
  const [amountField, amount] = inputField("Общая сумма, ₸", "number",
    {min: "0", step: "0.01", required: true, placeholder: "0,00"});
  const [methodField, method] = selectField("Распределить", [
    ["equal", "Поровну"], ["quantity", "По количеству"], ["manual", "Вручную"]
  ]);
  const [noteField, note] = inputField("Основание / примечание", "text",
    {maxLength: 1000, placeholder: "Например, доставка одной машиной"});
  const itemTitle = el("span", "entry-label", "К каким позициям относится расход");
  const actions = el("div", "entry-actions");
  const selectAll = el("button", "", "Выбрать все");
  const clear = el("button", "", "Снять выбор");
  selectAll.type = clear.type = "button";
  actions.append(selectAll, clear);
  const itemList = el("div", "expense-items");
  const rows = items.map(item => {
    const row = el("label", "expense-item");
    const checkbox = el("input"); checkbox.type = "checkbox";
    const manual = el("input", "manual-amount");
    manual.type = "number"; manual.min = "0"; manual.step = "0.01";
    manual.placeholder = "Доля, ₸"; manual.hidden = true; manual.disabled = true;
    row.append(checkbox, el("span", "", item.line_number + ". " + item.name), manual);
    itemList.append(row);
    return {item, checkbox, manual};
  });
  selectAll.addEventListener("click", () => rows.forEach(row => { row.checkbox.checked = true; }));
  clear.addEventListener("click", () => rows.forEach(row => { row.checkbox.checked = false; }));
  method.addEventListener("change", () => rows.forEach(row => {
    row.manual.hidden = method.value !== "manual";
    row.manual.disabled = method.value !== "manual";
  }));
  const submit = el("button", "entry-submit", "Распределить и сохранить");
  submit.type = "submit";
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  form.append(categoryField, kindField, amountField, methodField, noteField,
    itemTitle, actions, itemList, submit, feedback,
    el("small", "data-note", "Запись будет помечена как сообщение пользователя до прикрепления документа."));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const selected = rows.filter(row => row.checkbox.checked);
    if (!selected.length) { feedback.textContent = "Выберите хотя бы одну позицию"; return; }
    const payload = {category: category.value,
      cost_kind: kind.value, total_cost: amount.value, allocation_method: method.value,
      request_item_ids: selected.map(row => row.item.id), note: note.value.trim()};
    if (method.value === "manual") payload.manual_amounts = Object.fromEntries(
      selected.map(row => [row.item.id, row.manual.value]));
    const fingerprint = JSON.stringify(payload);
    if (fingerprint !== previousPayload) idempotencyKey = crypto.randomUUID();
    previousPayload = fingerprint;
    payload.idempotency_key = idempotencyKey;
    submit.disabled = true; feedback.textContent = "Сохраняем…";
    try { await apiWrite("/api/cases/" + caseId + "/expenses", payload); await openDetail(caseId); }
    catch (error) { feedback.textContent = error.message; }
    finally { submit.disabled = false; }
  });
  details.append(form);
  return details;
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
  const previousScroll = dialog.open ? dialog.scrollTop : 0;
  const root = $("detail-content");
  root.replaceChildren(empty("Загружаем заявку", "Сверяем позиции и предложения."));
  if (!dialog.open) dialog.showModal();
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
    body.append(grid, detailOptions(), expenseForm(id, data.items), el("h3", "", "Позиции"));
    const itemFilter = el("div", "item-filter");
    const itemSearch = el("input", "offer-search item-search");
    itemSearch.type = "search";
    itemSearch.placeholder = "Название, номер или техспек позиции";
    itemSearch.setAttribute("aria-label", "Поиск позиций в заявке");
    const offerFilter = el("select", "item-filter-select");
    [["all", "Все позиции"], ["with", "С предложениями"], ["without", "Без предложений"]]
      .forEach(([value, label]) => offerFilter.add(new Option(label, value)));
    offerFilter.setAttribute("aria-label", "Фильтр позиций по предложениям");
    const filteredCount = el("span", "data-note");
    itemFilter.append(itemSearch, offerFilter, filteredCount);
    body.append(itemFilter);
    if (data.offers_truncated) body.append(el("p", "data-note", "Предложений больше 1000. Для полного просмотра нужен фильтр по позиции."));
    if (!data.items.length) body.append(empty("Позиций пока нет", "Бот добавит их после обработки заявки."));
    const itemBoxes = [];
    data.items.forEach(item => {
      const box = el("div", "item-card");
      itemBoxes.push({item, box});
      const head = el("div", "item-head");
      head.append(el("span", "", item.line_number + ". " + item.name), itemBadge(item));
      box.append(head);
      box.append(detailField("Требуется", quantity(item.quantity, item.unit)));
      if (item.movement_label) box.append(detailField("Этап по Notion", item.movement_label));
      if (item.reported_delivered_quantity !== null) box.append(detailField(
        "Поставлено по Notion", quantity(item.reported_delivered_quantity, item.unit)
      ));
      if (item.specification_text) box.append(detailField("Техспек", item.specification_text, "spec"));
      if (item.reconciliation_label) box.append(detailField("Уточнение", item.reconciliation_label, "history"));
      if (item.location_label) box.append(detailField("Местонахождение по Notion", item.location_label, "history"));
      if (item.reported_accepted_quantity !== null) box.append(detailField("Принято по Notion", quantity(item.reported_accepted_quantity, item.unit), "history"));
      if (item.reported_rejected_quantity !== null) box.append(detailField("Отклонено по Notion", quantity(item.reported_rejected_quantity, item.unit), "history"));
      if (item.reported_remaining_quantity !== null) box.append(detailField("Остаток по Notion", quantity(item.reported_remaining_quantity, item.unit), "history"));
      if (item.purchased_quantity !== null) box.append(detailField("Закуплено, подтверждено", quantity(item.purchased_quantity, item.unit), "history"));
      if (item.accepted_quantity !== null) box.append(detailField("Принято, подтверждено", quantity(item.accepted_quantity, item.unit), "history"));
      if (item.event_count) {
        box.append(detailField("Поступило на склад, события", quantity(item.warehouse_received_quantity, item.unit), "history"));
        box.append(detailField("Передано заказчику, события", quantity(item.delivered_quantity, item.unit), "history"));
        box.append(detailField("Отклонено заказчиком, события", quantity(item.rejected_quantity, item.unit), "history"));
        box.append(detailField("Возвращено, события", quantity(item.returned_quantity, item.unit), "history"));
        box.append(detailField("Отменено, события", quantity(item.cancelled_quantity, item.unit), "history"));
      }
      if (item.tender_unit_price_private !== null) box.append(detailField("Цена тендера за единицу", quantity(item.tender_unit_price_private, "₸"), "finance"));
      if (item.delivery_cost !== null) box.append(detailField("Распределённая доставка", quantity(item.delivery_cost, "₸"), "finance"));
      if (item.actual_cost !== null && item.actual_cost !== undefined) box.append(detailField("Дополнительные расходы, факт", quantity(item.actual_cost, "₸"), "finance"));
      if (item.planned_cost !== null && item.planned_cost !== undefined) box.append(detailField("Дополнительные расходы, план", quantity(item.planned_cost, "₸"), "finance"));
      const itemExpenses = (data.expenses || []).filter(expense => expense.request_item_id === item.id);
      if (itemExpenses.length) {
        const expenseDetails = el("details", "item-expenses");
        expenseDetails.dataset.detailColumn = "finance";
        expenseDetails.hidden = !detailColumns.finance;
        expenseDetails.append(el("summary", "", "Расходы · " + itemExpenses.length));
        itemExpenses.forEach(expense => expenseDetails.append(detailField(
          (expenseNames[expense.category] || expense.category) +
            (expense.cost_kind === "planned" ? " · план" : " · факт"),
          quantity(expense.allocated_amount, expense.currency === "KZT" ? "₸" : expense.currency)
        )));
        box.append(expenseDetails);
      }
      if (item.source_record_url && /^https:\/\//i.test(item.source_record_url)) {
        const notionLink = el("a", "item-source", "Открыть запись в Notion ↗");
        notionLink.href = item.source_record_url;
        notionLink.target = "_blank";
        notionLink.rel = "noopener noreferrer";
        notionLink.dataset.detailColumn = "history";
        notionLink.hidden = !detailColumns.history;
        box.append(notionLink);
      }
      const itemOffers = data.offers.filter(offer => offer.request_item_id === item.id);
      const offerDetails = el("details", "item-offers");
      offerDetails.append(el("summary", "", "Предложения · " + item.offer_count));
      if (itemOffers.length) {
        const offerSearch = el("input", "offer-search");
        offerSearch.type = "search";
        offerSearch.placeholder = "Поставщик или товар";
        offerSearch.setAttribute("aria-label", "Поиск предложений по позиции " + item.name);
        const offerList = el("div", "offer-list");
        const renderOffers = () => {
          const needle = offerSearch.value.trim().toLocaleLowerCase("ru");
          const filtered = itemOffers.filter(offer => (offer.supplier_name + " " + offer.supplier_product_name).toLocaleLowerCase("ru").includes(needle));
          filtered.sort((a, b) => (a.price_amount === null) - (b.price_amount === null) ||
            (a.price_amount !== null && b.price_amount !== null ? Number(a.price_amount) - Number(b.price_amount) : 0));
          offerList.replaceChildren(...filtered.map(offerCard));
          if (!filtered.length) offerList.append(el("p", "data-note", "Совпадений нет"));
        };
        offerSearch.addEventListener("input", renderOffers);
        offerDetails.append(offerSearch, offerList);
        renderOffers();
      } else offerDetails.append(el("p", "data-note", "Предложений пока нет"));
      if (c.source_kind === "notion_import" && ["ready", "evaluating", "report_ready"].includes(c.status)) {
        const search = el("div", "item-research");
        const button = el("button", "entry-submit", "Искать поставщиков для этой позиции");
        button.type = "button";
        const note = el("span", "data-note", "Поиск по названию и городу. Техспек сверьте с найденными предложениями.");
        button.addEventListener("click", async () => {
          button.disabled = true;
          note.textContent = "Ставим поиск в очередь…";
          try {
            await apiWrite("/api/cases/" + id + "/items/" + item.id + "/research", {});
            await openDetail(id);
          } catch (error) {
            note.textContent = error.message;
            button.disabled = false;
          }
        });
        search.append(button, note);
        offerDetails.insertBefore(search, offerDetails.children[1] || null);
      }
      box.append(offerDetails);
      box.append(eventForm(id, item));
      body.append(box);
    });
    const filterItems = () => {
      const needle = itemSearch.value.trim().toLocaleLowerCase("ru");
      let shown = 0;
      itemBoxes.forEach(({item, box}) => {
        const text = (item.line_number + " " + item.name + " " + (item.specification_text || ""))
          .toLocaleLowerCase("ru");
        const hasOffers = Number(item.offer_count) > 0;
        const visible = text.includes(needle) && (offerFilter.value === "all"
          || (offerFilter.value === "with" && hasOffers)
          || (offerFilter.value === "without" && !hasOffers));
        box.hidden = !visible;
        if (visible) shown += 1;
      });
      filteredCount.textContent = "Показано " + shown + " из " + itemBoxes.length;
    };
    itemSearch.addEventListener("input", filterItems);
    offerFilter.addEventListener("change", filterItems);
    filterItems();
    root.replaceChildren(body);
    dialog.scrollTop = previousScroll;
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
