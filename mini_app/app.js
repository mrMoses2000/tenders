const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const initData = tg?.initData || "";
const base = new URL(".", window.location.href).pathname;
const $ = id => document.getElementById(id);
const state = {tab: "cases", q: "", status: "", city: "", offset: 0, loading: false,
  controller: null, listRevision: 0, detailId: null, detailRevision: 0,
  detailController: null, detailRefreshController: null, detailSnapshot: null,
  authenticated: false};
const detailColumns = {spec: false, history: false, finance: true, delivery: true};
try { Object.assign(detailColumns, JSON.parse(localStorage.getItem("tender-detail-columns") || "{}")); } catch (_) { /* private storage */ }
const configurableFields = ["Требуется", "Этап по Notion", "Поставлено по Notion", "Техспек",
  "Уточнение", "Местонахождение по Notion", "Принято по Notion", "Отклонено по Notion",
  "Остаток по Notion", "Закуплено, подтверждено", "Сейчас в пути", "Принято, подтверждено",
  "Поступило на склад, события", "Передано заказчику, события", "Отклонено заказчиком, события",
  "Возвращено, события", "Отменено, события", "Цена тендера за единицу",
  "Распределённая доставка", "Дополнительные расходы, факт", "Дополнительные расходы, план"];
let hiddenFields = ["Дополнительные расходы, план"];
try {
  const saved = JSON.parse(localStorage.getItem("tender-hidden-fields") || "[]");
  if (Array.isArray(saved)) hiddenFields = saved.filter(value => configurableFields.includes(value));
} catch (_) { /* private storage */ }
const expenseNames = {delivery: "Доставка", handling: "Погрузка", packaging: "Упаковка",
  commission: "Комиссия", travel: "Поездка", storage: "Хранение", other: "Другое"};
const loadingNames = {unknown: "Условия погрузки неизвестны", included: "Погрузка включена в тариф",
  separate: "Погрузка оплачивается отдельно", not_required: "Погрузка не требуется"};

const labels = {
  draft: "Черновик", needs_clarification: "Нужно уточнить", ready: "Готова к поиску",
  researching: "Поиск", contacting: "Поставщики", evaluating: "Оценка",
  report_ready: "Отчёт готов", selected: "Выбрана", closed: "Закрыта",
  cancelled: "Отменена", candidate: "Кандидат", active: "Активный",
  inactive: "Неактивный", blocked: "Заблокирован", merged: "Объединён",
  lead: "Лид", confirm: "Подтвердить", exact: "Совпадает",
  mismatch: "Не подходит", not_found: "Не найден", withdrawn: "Снят",
  in_stock: "В наличии", in_stock_unquantified: "Наличие без количества",
  out_of_stock: "Нет в наличии", preorder: "Под заказ",
  untracked: "Статус не указан", planned: "Запланирована",
  in_transit: "В пути", arrived: "Прибыла", completed: "Рейс завершён"
};
const comparisonBlockers = {
  technical_not_verified: "ТЗ ещё не проверено", price_not_confirmed: "Цена не подтверждена продавцом",
  required_quantity_unknown: "Не указано нужное количество", price_unknown: "Цена неизвестна",
  currency_not_comparable: "Другая валюта", stock_quantity_unknown: "Количество на складе неизвестно",
  stock_not_confirmed: "Остаток только из объявления", out_of_stock: "Нет в наличии",
  pack_quantity_unknown: "Неизвестна фасовка", price_unit_unknown_or_mismatch: "Неясная единица цены",
  order_multiple_unknown: "Неизвестна кратность заказа", insufficient_stock: "Не хватает на всю партию",
  invalid_moq: "Ошибка минимальной партии", invalid_order_multiple: "Ошибка кратности заказа"
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
function fmtDateTime(value) {
  return value ? new Intl.DateTimeFormat("ru-KZ", {dateStyle: "medium", timeStyle: "short"}).format(new Date(value)) : "Не указана";
}
function pipelineWaitLabel(value) {
  return ({
    no_research_run: "Запуск поиска ещё не создан",
    owner_paused: "Пайплайн приостановлен владельцем",
    provider_quota_wait: "Ожидание проверки лимита провайдера",
    queued_checkpoint_terminal_job: "В очереди есть задание с завершённой задачей",
    queued_checkpoint_without_runnable_job: "Позиция в очереди без активного задания",
    queue_job_active_or_scheduled: "Есть активные или запланированные задания",
    no_runnable_work: "Сейчас нет задания, которое можно выполнить"
  })[value] || value || "Причина ожидания не указана";
}
function fmtNumber(value) {
  return new Intl.NumberFormat("ru-KZ", {maximumFractionDigits: 2}).format(Number(value));
}
function quantity(value, unit = "") {
  return value === null || value === undefined ? "—" : fmtNumber(value) + (unit ? " " + unit : "");
}
function applyDetailVisibility() {
  document.querySelectorAll("[data-detail-column], [data-field-key]").forEach(node => {
    node.hidden = Boolean((node.dataset.detailColumn && !detailColumns[node.dataset.detailColumn])
      || (node.dataset.fieldKey && hiddenFields.includes(node.dataset.fieldKey)));
  });
}
function canRefreshDetailInPlace(root, activeElement = document.activeElement) {
  if (root.contains(activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(activeElement.tagName)) {
    return false;
  }
  return Array.from(root.querySelectorAll("input,select,textarea")).every(control =>
    control.dataset.freshValue !== undefined
      && control.value === control.dataset.freshValue
      && String(Boolean(control.checked)) === control.dataset.freshChecked);
}
function captureDetailFormBaseline(root) {
  root.querySelectorAll("input,select,textarea").forEach(control => {
    control.dataset.freshValue = control.value;
    control.dataset.freshChecked = String(Boolean(control.checked));
  });
}
function freshnessAction(visible, caseId = state.detailId) {
  const top = document.querySelector(".detail-top");
  if (!top) return;
  let action = top.querySelector("[data-freshness-action]");
  if (!action) {
    action = el("button", "entry-submit freshness-action", "Есть новые данные · обновить");
    action.type = "button";
    action.dataset.freshnessAction = "true";
    action.addEventListener("click", () => {
      const id = state.detailId;
      action.hidden = true;
      if (id && $("detail").open) openDetail(id);
    });
    top.append(action);
  }
  action.hidden = !visible || !caseId || caseId !== state.detailId;
}
function detailOptions() {
  const panel = el("div", "detail-options");
  panel.append(el("span", "", "ПОКАЗЫВАТЬ"));
  [["spec", "Техспек"], ["history", "Этапы и количества"],
    ["delivery", "Доставки"], ["finance", "Тендерная цена и расходы"]]
    .forEach(([key, label]) => {
    const control = el("label", "detail-option");
    const input = el("input");
    input.type = "checkbox";
    input.checked = Boolean(detailColumns[key]);
    input.addEventListener("change", () => {
      detailColumns[key] = input.checked;
      applyDetailVisibility();
      try { localStorage.setItem("tender-detail-columns", JSON.stringify(detailColumns)); } catch (_) { /* private storage */ }
    });
    control.append(input, el("span", "", label));
    panel.append(control);
  });
  const fields = el("details", "field-options");
  fields.append(el("summary", "", "Выбрать отдельные поля позиций"));
  const choices = el("div", "field-options-grid");
  configurableFields.forEach(key => {
    const label = el("label", "detail-option");
    const checkbox = el("input"); checkbox.type = "checkbox";
    checkbox.checked = !hiddenFields.includes(key);
    checkbox.addEventListener("change", () => {
      hiddenFields = checkbox.checked ? hiddenFields.filter(value => value !== key)
        : [...hiddenFields, key];
      try { localStorage.setItem("tender-hidden-fields", JSON.stringify(hiddenFields)); } catch (_) { /* private storage */ }
      applyDetailVisibility();
    });
    label.append(checkbox, el("span", "", key));
    choices.append(label);
  });
  fields.append(choices);
  panel.append(fields);
  return panel;
}
function detailField(key, value, column) {
  const row = el("div", "item-field");
  if (column) row.dataset.detailColumn = column;
  if (configurableFields.includes(key)) row.dataset.fieldKey = key;
  row.hidden = Boolean((column && !detailColumns[column]) || hiddenFields.includes(key));
  row.append(el("span", "", key), el("strong", "", value));
  return row;
}
function twoGisFirmCardUrl(sourceProvider, placeId, sourceUrl) {
  if (sourceProvider !== "2gis" || !/^\d{5,30}$/.test(String(placeId || ""))) return null;
  try {
    const origin = new URL(sourceUrl);
    if (origin.protocol !== "https:" || origin.hostname !== "2gis.kz") return null;
    const city = origin.pathname.split("/")[1];
    if (!/^[a-z0-9_-]+$/i.test(city || "")) return null;
    return "https://2gis.kz/" + city + "/firm/" + placeId;
  } catch (_) {
    return null;
  }
}
function offerCard(offer, context = {}) {
  const line = el("div", "offer-line");
  const top = el("div", "offer-top");
  top.append(el("strong", "", offer.supplier_name), badge(offer.status));
  if (offer.comparison?.rank && offer.comparison.rank <= 5) {
    top.append(el("span", "offer-rank", "№" + offer.comparison.rank));
  }
  line.append(top, el("div", "offer-product", offer.supplier_product_name || "Товар не указан"));
  line.append(meta(
    offer.price_amount === null
      ? (offer.quote_snapshot_version ? "Цена требует уточнения" : "Цена не указана")
      : "Цена из источника " + fmtNumber(offer.price_amount) + " " + (offer.currency === "KZT" ? "₸" : offer.currency || ""),
    offer.price_unit ? "за " + offer.price_unit : "Единица цены неизвестна",
    offer.supplier_city,
    offer.stock_source_type === "supplier_message" && offer.available_qty !== null
      ? "Поставщик сообщил остаток: " + fmtNumber(offer.available_qty)
      : offer.stock_source_type === "manual" && offer.available_qty !== null
        ? "Остаток внесён оператором: " + fmtNumber(offer.available_qty)
        : "Остаток партии не подтверждён",
    offer.observed_at ? "Проверено " + fmtDate(offer.observed_at) : "Дата не указана"
  ));
  if (offer.quote_snapshot_version) {
    const priceFact = offer.quote_snapshot_facts?.price_amount;
    const priceSource = {supplier_message: "ответ поставщика", manual: "запись оператора"}
      [priceFact?.source_type] || "источник";
    const snapshotText = priceFact
      ? `Снимок условий v${offer.quote_snapshot_version} · цена: ${priceSource}` +
        (priceFact.observed_at ? ` · ${fmtDate(priceFact.observed_at)}` : "")
      : `Цена требует уточнения · актуальный снимок v${offer.quote_snapshot_version} не содержит цену.`;
    line.append(el("p", "data-note", snapshotText));
  }
  const comparison = offer.comparison;
  if (comparison?.goods_total !== null && comparison?.goods_total !== undefined) {
    line.append(detailField("Товар на нужную партию",
      quantity(comparison.goods_total, comparison.currency === "KZT" ? "₸" : comparison.currency)));
    if (comparison.purchased_quantity !== null) line.append(detailField("Купить с учётом фасовки",
      quantity(comparison.purchased_quantity, context.item?.unit || "")));
  }
  if (comparison?.tender_total !== null && comparison?.tender_total !== undefined) {
    line.append(detailField("Сумма по тендеру", quantity(comparison.tender_total, "₸")));
  }
  if (comparison?.tender_difference !== null && comparison?.tender_difference !== undefined) {
    line.append(detailField("Разница без доставки", quantity(comparison.tender_difference, "₸")));
  }
  if (comparison?.blockers?.length) {
    line.append(el("p", "offer-blockers", "Для точного сравнения: " +
      comparison.blockers.map(code => comparisonBlockers[code] || code).join("; ")));
  } else if (comparison?.rank) {
    line.append(el("p", "offer-ready", "ТЗ и количество проверены; цена получена от поставщика."));
  }
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
  const firmCard = twoGisFirmCardUrl(
    offer.source_provider, offer.provider_place_id, offer.location_source_url
  );
  if (firmCard) {
    const link = el("a", "offer-source", "Карточка фирмы в 2ГИС ↗");
    link.href = firmCard;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    line.append(link);
  }
  if (offer.supplier_id) line.append(supplierDetails(offer.supplier_id, offer.id));
  if (context.caseId && context.editable) {
    line.append(manualSupplierQuotePanel(offer, context));
  }
  if (context.caseId && context.editable) line.append(technicalReviewPanel(offer, context));
  return line;
}

function localDateTimeInputValue(value = new Date()) {
  return new Date(value.getTime() - value.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 16);
}

function manualSupplierQuotePanel(offer, {caseId, item}) {
  const panel = el("details", "entry-panel manual-quote-entry");
  panel.append(el("summary", "", "Записать КП поставщика"));
  const buildForm = () => {
    panel.append(el("p", "data-note", "Сохранит сведения, которые вы уже получили, с пометкой «внесено оператором». Форма не пишет поставщику и не создаёт закупку."));

    const form = el("form", "entry-form");
    form.append(el("p", "data-note", "Поставщик: " + (offer.supplier_name || "не указан") +
      ". Товар/модель: " + (offer.supplier_product_name || "не указаны") +
      (offer.model_sku ? " · SKU: " + offer.model_sku : "") +
      ". Этот вариант нельзя изменить в форме КП."));

    const variantConfirmed = el("input");
    variantConfirmed.type = "checkbox";
    variantConfirmed.required = true;
    const variantLabel = el("label", "item-requirement-hard",
      "Подтверждаю, что полученное предложение относится к показанному товару/модели.");
    variantLabel.append(variantConfirmed);
    form.append(variantLabel);

    const [priceField, price] = inputField("Цена за единицу предложения", "number", {
      min: "0.01", step: "0.01", required: true, inputMode: "decimal"
    });
    const [currencyField, currency] = inputField("Валюта (ISO 4217)", "text", {
      value: "KZT", minLength: 3, maxLength: 3, pattern: "[A-Z]{3}", required: true,
      autocomplete: "off", spellcheck: false, title: "Три заглавные латинские буквы, например KZT"
    });
    currency.addEventListener("input", () => {
      const cursor = currency.selectionStart;
      currency.value = currency.value.toUpperCase();
      if (cursor !== null) currency.setSelectionRange(cursor, cursor);
    });
    const [unitField, priceUnit] = inputField("Единица цены (например, шт., кг, комплект)", "text", {
      minLength: 1, maxLength: 50, required: true,
      placeholder: item?.unit ? "Подтвердите единицу цены; в заявке указано: " + item.unit : "Укажите единицу цены"
    });
    const [packField, packQty] = inputField("Количество в упаковке (если указано)", "number", {
      min: "0.000001", step: "0.000001", inputMode: "decimal"
    });
    const [moqField, moq] = inputField("Минимальный заказ (если указан)", "number", {
      min: "0.000001", step: "0.000001", inputMode: "decimal"
    });
    const [multipleField, orderMultiple] = inputField("Кратность заказа (если указана)", "number", {
      min: "0.000001", step: "0.000001", inputMode: "decimal"
    });
    const [availableField, availableQty] = inputField("Количество в наличии (если указано)", "number", {
      min: "0", step: "0.000001", inputMode: "decimal"
    });
    const [availabilityField, availability] = selectField("Наличие", [
      ["unknown", "Не подтверждено"],
      ["in_stock_unquantified", "Есть, количество не уточнено"],
      ["in_stock", "Есть, количество указано"],
      ["preorder", "Под заказ"],
      ["out_of_stock", "Нет в наличии"]
    ]);
    const [deliveryField, delivery] = selectField("Получение / доставка (если известно)", [
      ["", "Не указано"], ["unknown", "Не уточнено"], ["pickup", "Самовывоз"],
      ["delivery", "Доставка"], ["both", "Самовывоз или доставка"],
      ["unavailable", "Недоступно"]
    ]);
    const [vatField, vat] = selectField("НДС (если известно)", [
      ["", "Не указано"], ["unknown", "Не уточнено"], ["included", "Включён"],
      ["excluded", "Не включён"], ["not_applicable", "Не применяется"]
    ]);
    const [documentsField, documents] = selectField("Документы (если известно)", [
      ["", "Не указано"], ["unknown", "Не уточнено"], ["available", "Есть"],
      ["partial", "Частично"], ["unavailable", "Нет"]
    ]);
    const [observedField, observedAt] = inputField("Когда поставщик подтвердил цену (часовой пояс устройства)", "datetime-local", {
      value: localDateTimeInputValue(), required: true
    });
    const sourceField = el("label", "entry-field");
    sourceField.append(el("span", "", "Исходный ответ и источник (кто сообщил, где и что подтвердил)"));
    const sourceText = el("textarea", "technical-note");
    sourceText.required = true;
    sourceText.minLength = 3;
    sourceText.maxLength = 4000;
    sourceText.rows = 4;
    sourceText.placeholder = "Укажите содержание полученного КП или ответа и контекст его получения.";
    sourceField.append(sourceText);
    const [sourceUrlField, sourceUrl] = inputField("Ссылка на источник (если есть)", "url", {
      maxLength: 2000, placeholder: "https://…"
    });

    const save = el("button", "entry-submit", "Сохранить КП");
    save.type = "submit";
    const feedback = el("p", "entry-feedback");
    form.append(priceField, currencyField, unitField, packField, moqField, multipleField,
      availableField, availabilityField, deliveryField, vatField, documentsField,
      observedField, sourceField, sourceUrlField, save, feedback);

    let retryFingerprint = null;
    let retryKey = null;
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const priceText = price.value.trim();
      if (!/^\d+(?:\.\d{1,2})?$/.test(priceText) || !(Number(priceText) > 0)) {
        price.focus();
        feedback.textContent = "Введите цену больше нуля не более чем с двумя знаками после запятой.";
        return;
      }
      if (!priceUnit.value.trim()) {
        priceUnit.focus();
        feedback.textContent = "Укажите единицу, за которую поставщик назвал цену.";
        return;
      }
      if (sourceText.value.trim().length < 3) {
        sourceText.focus();
        feedback.textContent = "Укажите исходный ответ и источник (не менее 3 символов).";
        return;
      }
      if (availability.value === "in_stock" && availableQty.value.trim() === "") {
        availableQty.focus();
        feedback.textContent = "Для статуса «В наличии» укажите количество.";
        return;
      }
      let observedIso;
      try {
        const observed = new Date(observedAt.value);
        if (!observedAt.value || Number.isNaN(observed.getTime())) throw new Error("invalid");
        observedIso = observed.toISOString();
      } catch (_) {
        observedAt.focus();
        feedback.textContent = "Укажите дату и время подтверждения.";
        return;
      }
      if (sourceUrl.value.trim()) {
        try {
          const url = new URL(sourceUrl.value.trim());
          if (!(["http:", "https:"].includes(url.protocol))) throw new Error("unsupported");
        } catch (_) {
          sourceUrl.focus();
          feedback.textContent = "Ссылка должна начинаться с http:// или https://.";
          return;
        }
      }
      const payload = {
        variant_confirmed: variantConfirmed.checked,
        price_amount: priceText,
        currency: currency.value.trim().toUpperCase(),
        price_unit: priceUnit.value.trim(),
        availability_status: availability.value,
        observed_at: observedIso,
        source_text: sourceText.value.trim()
      };
      const optionalDecimals = [
        ["pack_qty", packQty], ["moq", moq], ["order_multiple", orderMultiple],
        ["available_qty", availableQty]
      ];
      for (const [key, field] of optionalDecimals) {
        if (field.value.trim() !== "") payload[key] = field.value.trim();
      }
      if (sourceUrl.value.trim()) payload.source_url = sourceUrl.value.trim();
      for (const [key, field] of [["delivery_status", delivery], ["vat_status", vat],
        ["document_status", documents]]) {
        if (field.value) payload[key] = field.value;
      }
      const fingerprint = JSON.stringify(payload);
      if (fingerprint !== retryFingerprint) {
        retryFingerprint = fingerprint;
        retryKey = crypto.randomUUID();
      }
      payload.idempotency_key = retryKey;
      form.querySelectorAll("input,select,textarea,button").forEach(control => {
        control.disabled = true;
      });
      feedback.textContent = "";
      try {
        await apiWrite("/api/cases/" + encodeURIComponent(caseId) + "/offers/" +
          encodeURIComponent(offer.id) + "/manual-quotes", payload);
        await openDetail(caseId);
      } catch (error) {
        feedback.textContent = error.message;
        form.querySelectorAll("input,select,textarea,button").forEach(control => {
          control.disabled = false;
        });
        save.focus();
      }
    });
    panel.append(form);
    captureDetailFormBaseline(form);
  };
  panel.addEventListener("toggle", () => {
    if (panel.open && panel.dataset.formBuilt !== "true") {
      panel.dataset.formBuilt = "true";
      buildForm();
    }
  });
  return panel;
}

function technicalReviewPanel(offer, {caseId, item, requirements = [], reviews = []}) {
  const panel = el("details", "technical-review");
  panel.append(el("summary", "", "Проверить соответствие ТЗ"));
  const description = el("p", "data-note", item.specification_text || "Техспек позиции отсутствует");
  panel.append(description);
  if (!item.specification_text) return panel;
  const itemRequirements = requirements.filter(row => row.request_item_id === item.id);
  const relevant = itemRequirements.filter(row => row.is_hard);
  const notesByRequirement = new Map();
  const evidenceByRequirement = new Map();
  {
    const wholeSpecOnly = itemRequirements.length === 0;
    const endpoint = "/api/cases/" + caseId + "/offers/" + offer.id + "/technical-assessment";
    const assistant = el("div", "technical-assistant");
    const actions = el("div", "technical-actions");
    const request = el("button", "entry-submit", "Предложить проверку по доказательствам");
    const refresh = el("button", "entry-submit", "Обновить результат");
    request.type = refresh.type = "button";
    const status = el("p", "data-note", wholeSpecOnly
      ? "ИИ проверит техспек целиком, без выдуманной разбивки на требования. Это только подсказка; решение принимает оператор."
      : "ИИ может подготовить подсказки по отдельным требованиям; решение принимает оператор.");
    const suggestions = el("div", "technical-suggestions");
    let loaded = false;
    let polls = 0;
    let timer = null;
    function renderAssessment(value) {
      suggestions.replaceChildren();
      if (!value) { status.textContent = "Подсказки ещё не запрашивались."; return; }
      const captions = {queued: "Проверяем доказательства…", failed: "Проверка не удалась: " +
        (value.error_code || "неизвестная причина"), stale: "Доказательства изменились. Запустите новую проверку.",
      operator_review_required: "Подсказки готовы. Сверьте цитаты и подтвердите каждое решение сами."};
      status.textContent = captions[value.status] || value.status;
      (value.claims || []).forEach(claim => {
        const requirement = relevant.find(row => row.id === claim.requirement_id);
        const card = el("div", "technical-suggestion");
        const verdict = {match: "Вероятно подходит", mismatch: "Возможно не подходит",
          unknown: "Недостаточно данных"}[claim.verdict] || "Неизвестно";
        const requirementLabel = wholeSpecOnly
          ? "Весь техспек · без атомарной разбивки"
          : (requirement?.requirement_key || "Требование");
        card.append(el("strong", "", requirementLabel + " · " + verdict),
          el("p", "data-note", claim.note || ""));
        if (claim.evidence_quote) card.append(el("blockquote", "", claim.evidence_quote));
        const manualRequirementId = wholeSpecOnly ? null : claim.requirement_id;
        if (value.status === "operator_review_required" && claim.verdict !== "unknown"
            && notesByRequirement.has(manualRequirementId)) {
          const use = el("button", "entry-submit", "Перенести основание в ручную проверку");
          use.type = "button";
          use.addEventListener("click", () => {
            const note = notesByRequirement.get(manualRequirementId);
            if (!note) return;
            note.value = claim.note + (claim.evidence_quote ? " Цитата: «" + claim.evidence_quote + "»" : "");
            if (claim.source_evidence_id) evidenceByRequirement.set(
              manualRequirementId, claim.source_evidence_id);
            note.focus(); note.scrollIntoView({block: "center", behavior: "smooth"});
          });
          card.append(use);
        }
        suggestions.append(card);
      });
    }
    async function loadAssessment() {
      if (timer) { clearTimeout(timer); timer = null; }
      try {
        const response = await api(endpoint);
        renderAssessment(response.assessment);
        if (response.assessment?.status === "queued" && polls < 48 && panel.isConnected) {
          polls += 1;
          timer = setTimeout(loadAssessment, 5000);
        }
      } catch (error) { status.textContent = error.message; }
    }
    request.addEventListener("click", async () => {
      request.disabled = true;
      polls = 0;
      try {
        await apiWrite(endpoint, {request_key: crypto.randomUUID()});
        await loadAssessment();
      } catch (error) { status.textContent = error.message; }
      finally { request.disabled = false; }
    });
    refresh.addEventListener("click", () => { polls = 0; loadAssessment(); });
    panel.addEventListener("toggle", () => {
      if (panel.open && !loaded) { loaded = true; loadAssessment(); }
      if (!panel.open && timer) { clearTimeout(timer); timer = null; }
    });
    actions.append(request, refresh);
    assistant.append(actions, status, suggestions);
    panel.append(assistant);
  }
  const checks = [{id: null, label: "Весь техспек"}, ...relevant.map(row => ({
    id: row.id,
    label: row.requirement_key + (row.expected_value?.value ? " · " + row.expected_value.value : "")
  }))];
  checks.forEach(check => {
    const latest = reviews.find(row => row.offer_id === offer.id && row.requirement_id === check.id);
    const box = el("div", "technical-check");
    box.append(el("strong", "", check.label));
    if (latest) box.append(el("small", "data-note", "Последняя проверка: " +
      (latest.verdict === "match" ? "подходит" : "не подходит") + " · " + latest.note));
    const note = el("textarea", "technical-note");
    note.placeholder = "На основании какой характеристики, карточки или ответа?";
    note.maxLength = 2000;
    note.setAttribute("aria-label", "Основание проверки: " + check.label);
    notesByRequirement.set(check.id, note);
    box.append(note);
    const actions = el("div", "technical-actions");
    [["match", "Подходит"], ["mismatch", "Не подходит"]].forEach(([verdict, label]) => {
      const button = el("button", "entry-submit", label);
      button.type = "button";
      button.addEventListener("click", async () => {
        if (note.value.trim().length < 3) {
          note.focus(); return;
        }
        button.disabled = true;
        try {
          await apiWrite("/api/cases/" + caseId + "/offers/" + offer.id + "/technical-reviews", {
            idempotency_key: crypto.randomUUID(), requirement_id: check.id,
            source_evidence_id: evidenceByRequirement.get(check.id) ||
              offer.source_evidence_id || null,
            verdict, note: note.value.trim()
          });
          await openDetail(caseId);
        } catch (error) {
          box.append(el("p", "entry-feedback", error.message));
          button.disabled = false;
        }
      });
      actions.append(button);
    });
    box.append(actions); panel.append(box);
  });
  return panel;
}

function negotiationLabel(conversation) {
  if (conversation.opted_out || conversation.state === "opted_out") return "Попросил не писать";
  if (conversation.state === "closed_no_reply") return "Не ответил";
  if (conversation.last_reply_kind === "needs_time") return "Поставщик попросил подождать";
  if (conversation.last_reply_kind === "referral") return "Дал другой контакт";
  if (conversation.last_reply_kind === "spec_mismatch") return "Характеристики не подходят";
  if (conversation.last_reply_kind === "unavailable") return "Товара нет в наличии";
  if (conversation.last_reply_kind === "available") return "Наличие подтвердил, проверяем ТЗ";
  if (conversation.last_reply_kind === "spec_confirmed") return "Характеристики подтвердил, уточняем цену";
  if (conversation.last_reply_kind === "price_received") return "Цена получена, требуется сравнение";
  if (conversation.state === "escalated") return "Ответ требует проверки";
  if (!Number(conversation.outbound_count)) {
    if (conversation.approval_status === "expired") return "Черновик истёк — обновите в заявке";
    if (conversation.approval_status === "requested") return "Сообщение ожидает одобрения";
    if (conversation.approval_status === "approved") return "Сообщение поставлено в очередь";
    return "Пока не связались";
  }
  if (!Number(conversation.inbound_count)) return "Написали, ждём ответа";
  if (conversation.state === "waiting_spec") return "Ответил, уточняем характеристики";
  if (conversation.state === "waiting_price") return "Ответил, уточняем цену";
  if (conversation.state === "complete") return "Ответ получен, проверьте результат";
  return "Переписка ведётся";
}

function supplierDetails(supplierId, offerId = null) {
  const details = el("details", "supplier-details");
  details.append(el("summary", "", "Поставщик · контакты и переговоры"));
  const content = el("div", "supplier-details-content");
  details.append(content);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    content.replaceChildren(el("p", "data-note", "Загружаем карточку…"));
    try {
      const data = await api("/api/suppliers/" + encodeURIComponent(supplierId));
      content.replaceChildren();
      content.append(el("strong", "", data.supplier.display_name));
      if (data.supplier.legal_name) content.append(detailField(
        "Юридическое название", data.supplier.legal_name));
      if (["blocked", "merged"].includes(data.supplier.status)) content.append(el(
        "p", "data-note", "Поставщик отмечен как недоступный для связи."));
      const related = (data.conversations || []).filter(row => !offerId || row.offer_id === offerId);
      content.append(el("p", "data-note", related.length
        ? "Переговоры по этой позиции" : "По этой позиции переписка пока не начата"));
      related.forEach(row => {
        const entry = el("div", "supplier-conversation");
        entry.append(el("strong", "", negotiationLabel(row)),
          meta("Отправлено: " + row.outbound_count, "Ответов: " + row.inbound_count,
            row.last_message_at ? "Последнее сообщение: " + fmtDate(row.last_message_at) : ""));
        if (row.last_message_text) entry.append(el("p", "data-note",
          (row.last_message_direction === "inbound" ? "Поставщик: " : "Бот: ")
            + row.last_message_text.slice(0, 500)));
        renderSupplierMedia(row, entry);
        content.append(entry);
      });
      const relatedConversationIds = new Set(related.map(row => row.id));
      (data.media_messages || []).filter(row => relatedConversationIds.has(row.conversation_id))
        .forEach(row => {
          const entry = el("div", "supplier-conversation");
          entry.append(meta("Медиа поставщика", fmtDate(row.received_at)));
          renderSupplierMedia(row, entry);
          content.append(entry);
        });
      (data.referrals || []).filter(row => relatedConversationIds.has(row.conversation_id))
        .forEach(referral => {
          const entry = el("div", "supplier-contact");
          entry.append(el("span", "", "Поставщик предложил другой номер: "
            + referral.normalized_phone + " · требуется проверка связи и WhatsApp"));
          const phone = el("a", "offer-source", "Позвонить");
          phone.href = "tel:" + referral.normalized_phone;
          const wa = el("a", "offer-source", "Проверить WhatsApp ↗");
          wa.href = "https://wa.me/" + referral.normalized_phone.replace(/\D/g, "");
          wa.target = "_blank"; wa.rel = "noopener noreferrer";
          entry.append(phone, wa); content.append(entry);
        });
      (data.contact_enrichment || []).slice(0, 1).forEach(check => {
        const text = {
          blocked: "Карточка 2GIS была недоступна: " + (check.access_issue || "неизвестная причина"),
          contact_needs_review: "Карточка проверена: "
            + (check.conflicting_contacts || 0) + " совпадающих с другими фирмами и "
            + (check.invalid_contacts || 0) + " некорректных контактов требуют проверки",
          no_phone: "Карточка проверена, доступного номера не найдено",
          contact_found: "Контакты получены из карточки 2GIS"
        }[check.outcome] || "Карточка 2GIS проверена";
        const entry = el("p", "data-note", text);
        if (check.card_url && /^https:\/\/2gis\.kz\//i.test(check.card_url)) {
          const link = el("a", "offer-source", " Открыть карточку ↗");
          link.href = check.card_url; link.target = "_blank";
          link.rel = "noopener noreferrer"; entry.append(link);
        }
        content.append(entry);
      });
      if (!(data.contacts || []).length) content.append(el("p", "data-note",
        "Контакт не найден в карточке. Нужна дополнительная проверка."));
      (data.contacts || []).forEach(contact => {
        const row = el("div", "supplier-contact");
        row.append(el("span", "", (contact.label || contact.contact_type) + " · "
          + contact.display_value));
        if (["phone", "whatsapp"].includes(contact.contact_type)) {
          const phone = el("a", "offer-source", "Позвонить");
          phone.href = "tel:" + contact.normalized_value;
          row.append(phone);
          const wa = el("a", "offer-source", contact.is_whatsapp_confirmed
            ? "Открыть WhatsApp ↗" : "Проверить WhatsApp ↗");
          wa.href = "https://wa.me/" + contact.normalized_value.replace(/\D/g, "");
          wa.target = "_blank"; wa.rel = "noopener noreferrer";
          row.append(wa);
          if (!contact.is_whatsapp_confirmed) row.append(el("small", "data-note",
            "Наличие WhatsApp на этом номере ещё не подтверждено"));
        }
        if (contact.source_url && /^https:\/\//i.test(contact.source_url)) {
          const source = el("a", "offer-source", "Карточка источника ↗");
          source.href = contact.source_url; source.target = "_blank";
          source.rel = "noopener noreferrer"; row.append(source);
        }
        content.append(row);
      });
      (data.locations || []).slice(0, 5).forEach(location => {
        content.append(detailField("Адрес", [location.city, location.address_text,
          location.market_name, location.pavilion].filter(Boolean).join(", ") || "Не указан"));
        const firmCard = twoGisFirmCardUrl(
          location.source_provider, location.provider_place_id, location.source_url
        );
        if (firmCard) {
          const link = el("a", "offer-source", "Карточка фирмы в 2ГИС ↗");
          link.href = firmCard;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          content.append(link);
        }
      });
    } catch (error) {
      loaded = false;
      content.replaceChildren(el("p", "entry-feedback", error.message));
    }
  });
  return details;
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
    const purchased = Number(item.purchased_quantity || 0);
    const label = required !== null && cancelled >= required ? "Отменено" :
      required !== null && accepted + cancelled >= required && purchased >= accepted
        && delivered - rejected - Number(item.returned_quantity || 0) >= accepted
        ? "Исполнено" :
        rejected > 0 ? "Отказ / замена" : accepted > 0 ? "Принято частично" :
          delivered > 0 ? "Передано заказчику" : warehouse > 0 ? "На складе" :
            "Закуплено";
    return el("span", "badge " + (rejected > 0 ? "danger" : ""), label);
  }
  if (item.source_record_url) return el("span", "badge warn", "История из Notion");
  return badge(item.status);
}
const caseStageGroups = [
  ["owner_sourcing", "Уже закуплено / исключено"],
  ["purchase_review", "Покупка требует сверки"],
  ["needs_clarification", "Нужно уточнить"],
  ["comparable_options", "Есть сравнимое КП"],
  ["supplier_candidates", "Кандидаты поставщиков"],
  ["in_research", "Поиск идёт"],
  ["research_review", "Поиск завершён · нужна проверка"],
  ["not_started", "Поиск не запущен"]
];

function effectiveOwnerSourcingInstruction(sourceItem, currentSourceHash) {
  if (!currentSourceHash) return null;
  return (sourceItem?.sourcing_evidence || []).find(evidence =>
    evidence.applies_to_sourcing === true
      && evidence.source_payload_sha256 === currentSourceHash) || null;
}

function classifyCaseItemStage(item, sourceItem, progress, offers, currentSourceHash) {
  const instruction = effectiveOwnerSourcingInstruction(sourceItem, currentSourceHash);
  if (sourceItem?.stage === "bought" || instruction) return "owner_sourcing";
  if (sourceItem?.stage === "evidence_only"
      || (sourceItem?.sourcing_evidence || []).length > 0) return "purchase_review";
  if (sourceItem?.stage === "needs_review" || item.status === "needs_clarification") {
    return "needs_clarification";
  }
  if ((offers || []).some(offer => offer.comparison?.rank !== null
      && offer.comparison?.rank !== undefined
      && offer.comparison?.price_state === "supplier_confirmed")) {
    return "comparable_options";
  }
  if ((offers || []).length > 0 || Number(progress?.found_leads || 0) > 0) {
    return "supplier_candidates";
  }
  if (["queued", "researching", "paused"].includes(progress?.status)
      || item.status === "researching") return "in_research";
  if (item.status === "offers_found") return "supplier_candidates";
  if (["succeeded", "partial", "failed"].includes(progress?.status)
      || item.status === "no_match") return "research_review";
  return "not_started";
}

function caseStageBadgeLabel(stageKey, sourceItem, currentSourceHash) {
  if (stageKey === "owner_sourcing") {
    if (sourceItem?.stage === "bought") return "Закуплено · учёт";
    const instruction = effectiveOwnerSourcingInstruction(sourceItem, currentSourceHash);
    if (instruction?.disposition === "skip") return "Исключено владельцем";
    if (instruction?.disposition === "bought") return "Покупка заявлена владельцем";
    if (instruction?.disposition === "reduce_quantity") return "Количество изменено владельцем";
  }
  return caseStageGroups.find(([key]) => key === stageKey)?.[1] || "Этап не указан";
}

function renderCaseStageSummary(groupedItems, itemCount, onSelect) {
  const element = el("section", "stage-summary");
  element.setAttribute("aria-label", "Сводка по этапам позиций");
  const filters = el("div", "stage-filters");
  const buttons = new Map();
  const addFilter = (key, label, count) => {
    const button = el("button", "stage-filter", label + " · " + count);
    button.type = "button";
    button.setAttribute("aria-pressed", key === "all" ? "true" : "false");
    button.addEventListener("click", () => onSelect(key));
    buttons.set(key, button);
    filters.append(button);
  };
  addFilter("all", "Все позиции", itemCount);
  groupedItems.groups.filter(group => group.items.length > 0).forEach(group =>
    addFilter(group.key, group.label, group.items.length));
  element.append(filters);
  const waiting = el("p", "stage-waiting");
  waiting.textContent = groupedItems.waitingConversations === null
    ? "Ожидание ответа: счётчик недоступен"
    : "Ожидают ответа: " + countLabel(groupedItems.waitingConversations,
      "диалог", "диалога", "диалогов") + " · число диалогов, не позиций";
  element.append(waiting);
  return {element, buttons};
}

function groupCaseItems(items, sourceVisibility, researchProgress, offers, waitingChats) {
  const sourceByItem = new Map((sourceVisibility?.items || [])
    .map(source => [source.item_id, source]));
  const progressByItem = new Map((researchProgress || [])
    .map(progress => [progress.request_item_id, progress]));
  const offersByItem = new Map();
  (offers || []).forEach(offer => {
    const group = offersByItem.get(offer.request_item_id) || [];
    group.push(offer);
    offersByItem.set(offer.request_item_id, group);
  });
  const groups = caseStageGroups.map(([key, label]) => ({key, label, items: []}));
  const byKey = new Map(groups.map(group => [group.key, group]));
  const assignments = new Map();
  for (const item of items || []) {
    const stage = classifyCaseItemStage(item, sourceByItem.get(item.id),
      progressByItem.get(item.id), offersByItem.get(item.id),
      sourceVisibility?.source_payload_sha256);
    byKey.get(stage).items.push(item);
    assignments.set(item.id, stage);
  }
  return {
    groups,
    assignments,
    waitingConversations: waitingChats === null || waitingChats === undefined
      || !Number.isFinite(Number(waitingChats))
      ? null : Math.max(0, Number(waitingChats))
  };
}

function renderSupplierMedia(row, entry) {
        if (row.media_kind) {
          entry.append(meta(row.media_kind === "audio" ? "Голосовое" : "Фото",
            "Обработка: " + row.media_state,
            row.media_language ? "Язык: " + row.media_language : ""));
          if (row.media_text) entry.append(el("p", "data-note",
            "Распознанный текст: " + row.media_text.slice(0, 2000)));
          if (row.media_needs_clarification) entry.append(el("p", "data-note",
            "Нужны уточнения поставщика; распознавание не подтверждает предложение."));
          if (row.media_error) entry.append(el("p", "data-note", "Ошибка: " + row.media_error));
          const openMedia = el("button", "entry-submit",
            row.media_kind === "audio" ? "Прослушать" : "Показать фото");
          openMedia.type = "button";
          openMedia.addEventListener("click", async () => {
              openMedia.disabled = true;
              let objectUrl;
              try {
                const response = await fetch(base + "api/supplier-messages/"
                  + encodeURIComponent(row.last_message_id) + "/media", {
                  headers: {Authorization: "tma " + initData}
                });
                if (!response.ok) throw new Error("Не удалось загрузить медиа");
                objectUrl = URL.createObjectURL(await response.blob());
                const preview = el(row.media_kind === "audio" ? "audio" : "img", "supplier-media");
                if (row.media_kind === "audio") preview.controls = true;
                else preview.alt = "Фото поставщика";
                preview.src = objectUrl;
                entry.append(preview);
                const revokeWhenRemoved = new MutationObserver(() => {
                  if (!preview.isConnected) {
                    URL.revokeObjectURL(objectUrl); revokeWhenRemoved.disconnect();
                  }
                });
                revokeWhenRemoved.observe(document.body, {childList: true, subtree: true});
              } catch (error) {
                if (objectUrl) URL.revokeObjectURL(objectUrl);
                entry.append(el("p", "data-note", error.message));
                openMedia.disabled = false;
              }
            });
          entry.append(openMedia);
        }
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
async function loadCaseDetail(id, signal) {
  const path = "/api/cases/" + encodeURIComponent(id);
  const first = await api(path, signal);
  const offers = [];
  const byId = new Map();
  for (const offer of (Array.isArray(first.offers) ? first.offers : [])) {
    if (offer?.id === undefined || offer?.id === null) offers.push(offer);
    else if (byId.has(String(offer.id))) offers[byId.get(String(offer.id))] = offer;
    else {
      byId.set(String(offer.id), offers.length);
      offers.push(offer);
    }
  }
  let nextOffset = first.offers_next_offset;
  let page = first;
  const firstHasPagination = Object.prototype.hasOwnProperty.call(first, "offers_next_offset");
  let incomplete = Boolean(first.offers_truncated && !firstHasPagination);
  if (first.offers_truncated && firstHasPagination && nextOffset == null) incomplete = true;
  let requestedOffset = Number(first.offers_offset ?? 0);
  if (!Number.isInteger(requestedOffset) || requestedOffset < 0) {
    requestedOffset = 0;
    incomplete = true;
  }
  if (firstHasPagination && requestedOffset !== 0) incomplete = true;
  const seenOffsets = new Set([requestedOffset]);

  for (let pagesLoaded = 1; nextOffset !== null && nextOffset !== undefined && pagesLoaded < 99;
    pagesLoaded += 1) {
    const offset = Number(nextOffset);
    if (!Number.isInteger(offset) || offset <= requestedOffset || seenOffsets.has(offset)) {
      incomplete = true;
      break;
    }
    seenOffsets.add(offset);
    let nextPage;
    try {
      nextPage = await api(path + "?offers_offset=" + encodeURIComponent(String(offset)), signal);
    } catch (error) {
      if (signal?.aborted || error.name === "AbortError") throw error;
      incomplete = true;
      break;
    }
    if (Number(nextPage.offers_offset) !== offset || !Array.isArray(nextPage.offers)) {
      incomplete = true;
      break;
    }
    let added = 0;
    for (const offer of nextPage.offers) {
      if (offer?.id === undefined || offer?.id === null) {
        offers.push(offer);
        added += 1;
        continue;
      }
      const key = String(offer.id);
      if (byId.has(key)) offers[byId.get(key)] = offer;
      else {
        byId.set(key, offers.length);
        offers.push(offer);
        added += 1;
      }
    }
    if (added === 0 && nextPage.offers_next_offset !== null
      && nextPage.offers_next_offset !== undefined) {
      incomplete = true;
      break;
    }
    page = nextPage;
    requestedOffset = offset;
    nextOffset = nextPage.offers_next_offset;
  }
  if (nextOffset !== null && nextOffset !== undefined) incomplete = true;
  if (page.offers_truncated && (nextOffset === null || nextOffset === undefined)) incomplete = true;
  const total = Number(page.offers_total ?? first.offers_total);
  if (Number.isFinite(total) && total >= 0 && offers.length < total) incomplete = true;
  return {
    ...first,
    offers,
    offers_truncated: incomplete
  };
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
function artifactOptions(artifacts) {
  return [["", "Без документа · запись пользователя"], ...artifacts.map(artifact => [
    artifact.id,
    (artifact.original_filename || (artifact.kind === "photo" ? "Фото" : "Документ"))
      + " · " + fmtDate(artifact.created_at)
  ])];
}
function eventForm(caseId, item, data) {
  let idempotencyKey = crypto.randomUUID();
  let previousPayload = "";
  const details = el("details", "entry-panel");
  details.append(el("summary", "", "Зафиксировать событие по позиции"));
  const form = el("form", "entry-form");
  const [kindField, kind] = selectField("Событие", [
    ["warehouse_received", "Поступило на склад"],
    ["customer_delivered", "Передано заказчику"],
    ["customer_accepted", "Принято заказчиком"],
    ["customer_revoked", "Отозвать приёмку"],
    ["customer_rejected", "Заказчик отклонил"],
    ["returned", "Товар возвращён"], ["cancelled", "Отменено"]
  ]);
  const [quantityField, quantityInput] = inputField("Количество (исправление — со знаком минус)", "number",
    {step: "0.000001", required: true, placeholder: item.unit || "Количество"});
  const [reasonField, reasonInput] = inputField("Причина / примечание", "text",
    {maxLength: 1000, placeholder: "Обязательно для отказа, возврата и отмены"});
  const [customerField, customer] = inputField("Заказчик", "text",
    {maxLength: 200, placeholder: "Название организации"});
  customer.value = data.case.customer_name || "";
  const [artifactField, artifact] = selectField("Документ из Telegram", artifactOptions(data.artifacts || []));
  const linkedRuns = (data.delivery_items || []).filter(line => line.request_item_id === item.id)
    .map(line => (data.deliveries || []).find(run => run.id === line.delivery_run_id))
    .filter(run => run && !["planned", "cancelled"].includes(run.status));
  const [deliveryField, delivery] = selectField("Связать с рейсом", [
    ["", "Без конкретного рейса"],
    ...linkedRuns.map(run => [run.id, run.tracking_ref || run.carrier_name || "Рейс " + run.id.slice(0, 8)])
  ]);
  customerField.hidden = true;
  kind.addEventListener("change", () => {
    customerField.hidden = !["customer_accepted", "customer_revoked"].includes(kind.value);
  });
  const submit = el("button", "entry-submit", "Сохранить событие");
  submit.type = "submit";
  const feedback = el("p", "entry-feedback");
  feedback.setAttribute("role", "status");
  form.append(kindField, quantityField, customerField, deliveryField, artifactField,
    reasonField, submit, feedback,
    el("small", "data-note", "Если файл выбран, событие связывается с документом из Telegram. Иначе это запись пользователя."));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    submit.disabled = true;
    feedback.textContent = "Сохраняем…";
    try {
      const payload = {event_kind: kind.value,
        quantity_delta: quantityInput.value, reason: reasonInput.value.trim(),
        customer_name: customer.value.trim(),
        source_artifact_id: artifact.value || null,
        delivery_run_id: delivery.value || null
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
function purchaseForm(caseId, data) {
  const details = el("details", "entry-panel purchase-entry");
  details.append(el("summary", "", "Зафиксировать закупку"));
  const form = el("form", "entry-form");
  const [sellerField, seller] = inputField("Продавец", "text",
    {maxLength: 200, required: true, placeholder: "Название поставщика или магазина"});
  const [kindField, kind] = selectField("Тип документа", [
    ["other", "Без документа / другое"], ["receipt", "Чек"],
    ["cash_receipt", "Кассовый чек"], ["invoice", "Счёт"], ["waybill", "Накладная"]
  ]);
  const [artifactField, artifact] = selectField("Файл из Telegram", artifactOptions(data.artifacts || []));
  const [numberField, number] = inputField("Номер документа", "text", {maxLength: 120});
  const [noteField, note] = inputField("Основание записи", "text", {
    maxLength: 1000, placeholder: "Обязательно, если чек или накладная не приложены"
  });
  const cargo = el("div", "purchase-lines");
  const rows = data.items.map(item => {
    const row = el("div", "purchase-line");
    row.append(el("strong", "", item.line_number + ". " + item.name));
    const controls = el("div", "purchase-line-controls");
    const qty = el("input"); qty.type = "number"; qty.min = "0.000001";
    qty.step = "0.000001"; qty.placeholder = "Количество, " + (item.unit || "ед.");
    const remaining = Number(item.quantity || 0) - Number(item.purchased_quantity || 0);
    if (remaining > 0) qty.placeholder = "Осталось " + fmtNumber(remaining) + " " + (item.unit || "");
    qty.setAttribute("aria-label", "Закупленное количество: " + item.name);
    const cost = el("input"); cost.type = "number"; cost.min = "0";
    cost.step = "0.0001"; cost.placeholder = "Цена за ед., ₸";
    cost.setAttribute("aria-label", "Цена за единицу: " + item.name);
    const matching = (data.offers || []).filter(offer => offer.request_item_id === item.id);
    const offer = el("select");
    offer.add(new Option("Без найденного предложения", ""));
    matching.forEach(candidate => offer.add(new Option(
      candidate.supplier_name + " · " + (candidate.supplier_product_name || "Товар"), candidate.id
    )));
    offer.setAttribute("aria-label", "Предложение поставщика: " + item.name);
    controls.append(qty, cost, offer);
    row.append(controls);
    cargo.append(row);
    return {item, qty, cost, offer};
  });
  const submit = el("button", "entry-submit", "Сохранить закупку");
  submit.type = "submit";
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  let key = crypto.randomUUID();
  let previous = "";
  form.append(sellerField, kindField, artifactField, numberField, noteField,
    el("span", "entry-label", "Позиции в одной закупке"), cargo, submit, feedback,
    el("small", "data-note", "Один чек или одна запись могут охватывать несколько позиций. Закупка сама по себе не закрывает позицию."));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const lines = rows.filter(row => row.qty.value && Number(row.qty.value) > 0)
      .map(row => ({request_item_id: row.item.id, purchased_quantity: row.qty.value,
        unit_cost: row.cost.value, offer_id: row.offer.value || null}));
    if (!lines.length) { feedback.textContent = "Укажите количество хотя бы одной позиции"; return; }
    if (lines.some(line => line.unit_cost === "")) {
      feedback.textContent = "Укажите фактическую цену за единицу каждой закупаемой позиции"; return;
    }
    if (!artifact.value && !note.value.trim()) {
      feedback.textContent = "Без файла укажите основание записи"; return;
    }
    const payload = {seller_name: seller.value.trim(), document_kind: kind.value,
      source_artifact_id: artifact.value || null, document_number: number.value.trim(),
      note: note.value.trim(), lines};
    const fingerprint = JSON.stringify(payload);
    if (fingerprint !== previous) key = crypto.randomUUID();
    previous = fingerprint;
    submit.disabled = true; feedback.textContent = "Сохраняем…";
    try {
      await apiWrite("/api/cases/" + caseId + "/purchases", {...payload, idempotency_key: key});
      await openDetail(caseId);
    } catch (error) { feedback.textContent = error.message; }
    finally { submit.disabled = false; }
  });
  details.append(form);
  return details;
}
function closurePanel(caseId, data) {
  const section = el("section", "closure-panel");
  section.append(el("h3", "", "Закрытие заявки"));
  const financial = data.financial || {};
  const currency = financial.currency === "KZT" ? "₸" : financial.currency || "₸";
  const grid = el("div", "closure-finance");
  [["Закупка", financial.product_cost_known], ["Доставка", financial.delivery_cost_known],
    ["Другие расходы", financial.additional_cost_known],
    ["Выручка", financial.tender_revenue_private],
    ["Расчётная маржа", financial.provisional_margin]].forEach(([name, value]) => {
    grid.append(detailField(name, value === null || value === undefined ? "Не указана" : quantity(value, currency)));
  });
  section.append(grid);
  if (data.case.status === "cancelled") {
    section.append(el("p", "data-note", "Заявка отменена. Закрытие исполнения недоступно."));
    return section;
  }
  if (data.case.status === "closed") {
    section.append(el("p", "data-note", "Заявка закрыта. Новые факты можно внести после повторного открытия с причиной."));
    const reopen = el("button", "entry-submit", "Открыть повторно");
    reopen.type = "button";
    const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
    reopen.addEventListener("click", async () => {
      const reason = window.prompt("Причина повторного открытия заявки");
      if (!reason?.trim()) return;
      reopen.disabled = true;
      try {
        await apiWrite("/api/cases/" + caseId + "/closure",
          {action: "reopened", reason: reason.trim(), idempotency_key: crypto.randomUUID()});
        await openDetail(caseId); await refreshDashboard();
      } catch (error) { feedback.textContent = error.message; reopen.disabled = false; }
    });
    section.append(reopen, feedback);
    return section;
  }
  const issues = data.closure?.issues || [];
  if (issues.length) {
    const list = el("div", "closure-issues");
    list.append(el("strong", "", "Пока нельзя закрыть · " + issues.length));
    issues.forEach(issue => list.append(el("p", "", issue.message)));
    section.append(list);
  } else section.append(el("p", "closure-ready", "Все позиции и доставки сверены. Заявку можно закрыть."));
  const close = el("button", "entry-submit", "Закрыть заявку");
  close.type = "button"; close.disabled = Boolean(issues.length);
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  close.addEventListener("click", async () => {
    if (!window.confirm("Закрыть заявку после проверки фактов и расходов?")) return;
    close.disabled = true;
    try {
      await apiWrite("/api/cases/" + caseId + "/closure",
        {action: "closed", idempotency_key: crypto.randomUUID()});
      await openDetail(caseId); await refreshDashboard();
    } catch (error) { feedback.textContent = error.message; close.disabled = false; }
  });
  section.append(close, feedback);
  return section;
}
function deliverySection(caseId, data) {
  const section = el("section", "delivery-section");
  section.dataset.detailColumn = "delivery";
  section.hidden = !detailColumns.delivery;
  section.append(el("h3", "", "Доставки"));
  const details = el("details", "entry-panel delivery-entry");
  details.append(el("summary", "", "Создать доставку с составом груза"));
  const form = el("form", "entry-form");
  const [carrierField, carrier] = inputField("Перевозчик / водитель", "text",
    {maxLength: 200, placeholder: "Можно указать позже"});
  const [phoneField, phone] = inputField("Телефон перевозчика", "tel",
    {maxLength: 30, placeholder: "+7…"});
  const [trackingField, tracking] = inputField("Номер рейса / отслеживания", "text",
    {maxLength: 120});
  const [expectedField, expected] = inputField("Ожидаемое прибытие", "datetime-local");
  const [loadingField, loading] = selectField("Погрузка", Object.entries(loadingNames));
  const [includedField, included] = inputField("Стоимость погрузки внутри тарифа, ₸", "number",
    {min: "0", step: "0.01", placeholder: "Если известна"});
  includedField.hidden = true;
  loading.addEventListener("change", () => { includedField.hidden = loading.value !== "included"; });
  const [noteField, note] = inputField("Примечание", "text", {maxLength: 1000});
  const cargo = el("div", "delivery-cargo");
  const rows = data.items.map(item => {
    const row = el("div", "delivery-cargo-row");
    const quantityInput = el("input");
    quantityInput.type = "number"; quantityInput.min = "0.000001";
    quantityInput.step = "0.000001"; quantityInput.placeholder = item.unit || "Кол-во";
    quantityInput.setAttribute("aria-label", "Количество груза: " + item.name);
    const purchase = el("select");
    purchase.add(new Option("Без привязки к закупке", ""));
    (data.purchase_lines || []).filter(line => line.request_item_id === item.id)
      .forEach(line => purchase.add(new Option(
        (line.seller_name || line.supplier_name || "Закупка") + " · "
          + quantity(line.purchased_quantity, item.unit) + " · " + fmtDate(line.created_at),
        line.id
      )));
    purchase.setAttribute("aria-label", "Строка закупки: " + item.name);
    row.append(el("span", "", item.line_number + ". " + item.name), quantityInput, purchase);
    cargo.append(row);
    return {item, quantityInput, purchase};
  });
  const submit = el("button", "entry-submit", "Сохранить доставку");
  submit.type = "submit";
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  let key = crypto.randomUUID();
  let previous = "";
  form.append(carrierField, phoneField, trackingField, expectedField,
    loadingField, includedField, noteField,
    el("span", "entry-label", "Количество по позициям в этом рейсе"), cargo,
    submit, feedback, el("small", "data-note",
      "Создание рейса не подтверждает покупку, поступление или приёмку товара."));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const items = rows.filter(row => row.quantityInput.value && Number(row.quantityInput.value) > 0)
      .map(row => ({request_item_id: row.item.id, quantity: row.quantityInput.value,
        purchase_item_id: row.purchase.value || null}));
    if (!items.length) { feedback.textContent = "Укажите количество хотя бы одной позиции"; return; }
    const payload = {items, carrier_name: carrier.value.trim(), carrier_phone: phone.value.trim(),
      tracking_ref: tracking.value.trim(), note: note.value.trim(),
      expected_at: expected.value ? new Date(expected.value).toISOString() : null,
      loading_mode: loading.value,
      included_loading_value: loading.value === "included" && included.value ? included.value : null};
    const fingerprint = JSON.stringify(payload);
    if (fingerprint !== previous) key = crypto.randomUUID();
    previous = fingerprint;
    submit.disabled = true; feedback.textContent = "Сохраняем…";
    try {
      await apiWrite("/api/cases/" + caseId + "/deliveries", {...payload, idempotency_key: key});
      await openDetail(caseId);
    } catch (error) { feedback.textContent = error.message; }
    finally { submit.disabled = false; }
  });
  details.append(form);
  if (!["closed", "cancelled"].includes(data.case.status)) section.append(details);
  if (!(data.deliveries || []).length) {
    section.append(el("p", "data-note", "Рейсов пока нет. Добавьте состав груза, чтобы видеть, что запланировано и что в пути."));
  }
  (data.deliveries || []).forEach(run => {
    const card = el("div", "delivery-card");
    const head = el("div", "delivery-card-head");
    head.append(el("strong", "", run.tracking_ref || run.carrier_name || "Доставка " + run.id.slice(0, 8)),
      badge(run.status));
    card.append(head, meta(run.carrier_name, run.carrier_phone,
      run.expected_at ? "Ожидается " + fmtDate(run.expected_at) : ""));
    const cargoLines = (data.delivery_items || []).filter(line => line.delivery_run_id === run.id);
    cargoLines.forEach(line => card.append(detailField(
      line.line_number + ". " + line.name,
      line.quantity === null ? "Количество не указано" : quantity(line.quantity, line.unit)
    )));
    if (run.total_cost !== null) card.append(detailField("Доставка, факт", quantity(run.total_cost, "₸")));
    if (run.actual_cost !== null) card.append(detailField("Связанные расходы, факт", quantity(run.actual_cost, "₸")));
    if (run.planned_cost !== null) card.append(detailField("Связанные расходы, план", quantity(run.planned_cost, "₸")));
    card.append(detailField("Погрузка", loadingNames[run.loading_mode || "unknown"]));
    if (run.included_loading_value !== null) card.append(detailField(
      "Из тарифа приходится на погрузку", quantity(run.included_loading_value, "₸")));
    if (run.loading_note) card.append(el("p", "data-note", run.loading_note));
    card.append(detailField("Начислено за рейс", Number(run.charge_total) > 0
      ? quantity(run.charge_total, "₸") : "Стоимость ещё не внесена"));
    card.append(detailField("Оплачено с учётом возвратов", quantity(run.net_paid, "₸")));
    if (Number(run.balance_due) > 0) card.append(detailField(
      "Осталось оплатить", quantity(run.balance_due, "₸")));
    if (Number(run.balance_due) < 0) card.append(detailField(
      "Переплата / аванс", quantity(-Number(run.balance_due), "₸")));
    const payments = (data.delivery_payments || []).filter(p => p.delivery_run_id === run.id);
    if (payments.length) {
      const history = el("details", "item-expenses");
      history.append(el("summary", "", "Оплаты и возвраты · " + payments.length));
      payments.forEach(payment => history.append(detailField(
        (payment.kind === "refund" ? "Возврат" : "Оплата") + " · " + fmtDate(payment.paid_at),
        quantity(payment.amount, "₸") + (payment.payee ? " · " + payment.payee : "")
      )));
      card.append(history);
    }
    card.append(deliveryTermsForm(caseId, run, data.artifacts || []));
    card.append(deliveryPaymentForm(caseId, run, data.artifacts || []));
    if (run.note) card.append(el("p", "data-note", run.note));
    const next = {planned: [["in_transit", "Отправить"], ["cancelled", "Отменить"]],
      in_transit: [["arrived", "Отметить прибытие"], ["completed", "Завершить рейс"], ["cancelled", "Отменить"]],
      arrived: [["completed", "Завершить рейс"], ["cancelled", "Отменить"]]}[run.status] || [];
    if (next.length && !["closed", "cancelled"].includes(data.case.status)) {
      const actions = el("div", "entry-actions delivery-actions");
      next.forEach(([status, text]) => {
        const button = el("button", "", text); button.type = "button";
        button.addEventListener("click", async () => {
          const reason = status === "cancelled" ? window.prompt("Причина отмены рейса") : "";
          if (status === "cancelled" && !reason?.trim()) return;
          button.disabled = true;
          try {
            await apiWrite("/api/cases/" + caseId + "/deliveries/" + run.id + "/status",
              {status, reason: reason || "", idempotency_key: crypto.randomUUID()});
            await openDetail(caseId);
          } catch (error) { card.append(el("p", "entry-feedback", error.message)); button.disabled = false; }
        });
        actions.append(button);
      });
      card.append(actions);
    }
    section.append(card);
  });
  return section;
}

function deliveryTermsForm(caseId, run, artifacts) {
  const details = el("details", "entry-panel");
  details.append(el("summary", "", "Уточнить условия погрузки"));
  const form = el("form", "entry-form");
  const [modeField, mode] = selectField("Погрузка", Object.entries(loadingNames));
  mode.value = run.loading_mode || "unknown";
  const [valueField, value] = inputField("Стоимость внутри тарифа, ₸", "number",
    {min: "0", step: "0.01", placeholder: "Если известна"});
  value.value = run.included_loading_value || "";
  const [noteField, note] = inputField("Условия / основание", "text", {maxLength: 1000});
  const [artifactField, artifact] = selectField("Документ из Telegram", artifactOptions(artifacts));
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  const submit = el("button", "entry-submit", "Сохранить условия"); submit.type = "submit";
  const toggle = () => { valueField.hidden = mode.value !== "included"; };
  mode.addEventListener("change", toggle); toggle();
  form.append(modeField, valueField, noteField, artifactField, submit, feedback);
  form.addEventListener("submit", async event => {
    event.preventDefault(); submit.disabled = true;
    try {
      await apiWrite("/api/cases/" + caseId + "/deliveries/" + run.id + "/terms", {
        loading_mode: mode.value,
        included_loading_value: mode.value === "included" && value.value ? value.value : null,
        note: note.value.trim(), source_artifact_id: artifact.value || null,
        idempotency_key: crypto.randomUUID()
      });
      await openDetail(caseId);
    } catch (error) { feedback.textContent = error.message; submit.disabled = false; }
  });
  details.append(form);
  return details;
}

function deliveryPaymentForm(caseId, run, artifacts) {
  const details = el("details", "entry-panel");
  details.append(el("summary", "", "Зафиксировать оплату или возврат"));
  const form = el("form", "entry-form");
  const [kindField, kind] = selectField("Операция", [["payment", "Оплата"], ["refund", "Возврат"]]);
  const [amountField, amount] = inputField("Сумма, ₸", "number",
    {min: "0.01", step: "0.01", required: true});
  const [payeeField, payee] = inputField("Получатель", "text", {maxLength: 200});
  const [noteField, note] = inputField("Примечание", "text", {maxLength: 1000});
  const [artifactField, artifact] = selectField("Документ из Telegram", artifactOptions(artifacts));
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  const submit = el("button", "entry-submit", "Сохранить оплату"); submit.type = "submit";
  form.append(kindField, amountField, payeeField, noteField, artifactField, submit, feedback);
  let key = crypto.randomUUID(); let previous = ""; let paidAt = new Date().toISOString();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const payload = {kind: kind.value, amount: amount.value, payee: payee.value.trim(),
      note: note.value.trim(), source_artifact_id: artifact.value || null};
    const fingerprint = JSON.stringify(payload);
    if (fingerprint !== previous) { key = crypto.randomUUID(); paidAt = new Date().toISOString(); }
    previous = fingerprint; submit.disabled = true;
    try {
      await apiWrite("/api/cases/" + caseId + "/deliveries/" + run.id + "/payments",
        {...payload, paid_at: paidAt, idempotency_key: key});
      await openDetail(caseId);
    } catch (error) { feedback.textContent = error.message; submit.disabled = false; }
  });
  details.append(form);
  return details;
}

function expenseForm(caseId, items, deliveries, deliveryItems, artifacts) {
  let idempotencyKey = crypto.randomUUID();
  let previousPayload = "";
  const details = el("details", "entry-panel expense-entry");
  details.append(el("summary", "", "Добавить фактический расход"));
  const form = el("form", "entry-form");
  const [categoryField, category] = selectField("Категория", Object.entries(expenseNames));
  const [deliveryField, delivery] = selectField("Связать с доставкой", [
    ["", "Без конкретного рейса"],
    ...deliveries.map(run => [run.id, (run.tracking_ref || run.carrier_name || "Рейс " + run.id.slice(0, 8))
      + " · " + (labels[run.status] || run.status)])
  ]);
  const [amountField, amount] = inputField("Общая сумма, ₸", "number",
    {min: "0", step: "0.01", required: true, placeholder: "0,00"});
  const [methodField, method] = selectField("Распределить", [
    ["equal", "Поровну"], ["quantity", "По количеству"], ["manual", "Вручную"]
  ]);
  const [noteField, note] = inputField("Основание / примечание", "text",
    {maxLength: 1000, placeholder: "Например, доставка одной машиной"});
  const [artifactField, artifact] = selectField("Документ из Telegram", artifactOptions(artifacts));
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
  selectAll.addEventListener("click", () => rows.forEach(row => {
    row.checkbox.checked = !row.checkbox.disabled;
  }));
  clear.addEventListener("click", () => rows.forEach(row => { row.checkbox.checked = false; }));
  method.addEventListener("change", () => rows.forEach(row => {
    row.manual.hidden = method.value !== "manual";
    row.manual.disabled = method.value !== "manual";
  }));
  delivery.addEventListener("change", () => {
    const allowed = new Set(deliveryItems.filter(line => line.delivery_run_id === delivery.value)
      .map(line => line.request_item_id));
    rows.forEach(row => {
      row.checkbox.disabled = Boolean(delivery.value) && !allowed.has(row.item.id);
      row.checkbox.checked = Boolean(delivery.value) && allowed.has(row.item.id);
    });
  });
  const submit = el("button", "entry-submit", "Распределить и сохранить");
  submit.type = "submit";
  const feedback = el("p", "entry-feedback"); feedback.setAttribute("role", "status");
  form.append(categoryField, deliveryField, amountField, methodField,
    artifactField, noteField,
    itemTitle, actions, itemList, submit, feedback,
    el("small", "data-note", "Вносите только фактическую сумму. Запись будет помечена как сообщение пользователя до прикрепления документа."));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const selected = rows.filter(row => row.checkbox.checked && !row.checkbox.disabled);
    if (!selected.length) { feedback.textContent = "Выберите хотя бы одну позицию"; return; }
    const payload = {category: category.value,
      cost_kind: "actual", total_cost: amount.value, allocation_method: method.value,
      request_item_ids: selected.map(row => row.item.id), note: note.value.trim(),
      source_artifact_id: artifact.value || null};
    if (delivery.value) payload.delivery_run_id = delivery.value;
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
  body.append(supplierDetails(row.id));
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
  const revision = ++state.listRevision;
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
    if (signal.aborted || revision !== state.listRevision) return false;
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
    return true;
  } catch (error) {
    if (!signal.aborted && revision === state.listRevision) {
      $("list").replaceChildren(empty(error.message, "Проверьте подключение и повторите попытку."));
      $("load-more").hidden = true;
    }
    return false;
  } finally {
    if (!signal.aborted && revision === state.listRevision) state.loading = false;
  }
}
function outreachDraftsPanel(caseId, drafts, enabled) {
  const panel = el("details", "outreach-drafts");
  panel.append(el("summary", "", "Черновики сообщений поставщикам · " + drafts.length));
  const content = el("div", "outreach-drafts-content");
  content.append(el("p", "data-note", "Проверьте получателя и текст. Нажатие «Одобрить и отправить» " +
    "передаст выбранные сообщения в очередь WhatsApp. За раз можно выбрать до 10. " +
    "Отправка доступна с 09:00 до 20:00 по Алматы. Устаревшие тексты сначала " +
    "обновите, затем прочитайте новые черновики и одобрите отдельно."));
  if (drafts.length > 300) content.append(el("p", "data-note", "Показаны первые 301 черновик."));
  if (!enabled) content.append(el("p", "entry-feedback", "WhatsApp отправка сейчас выключена."));
  const selected = new Set();
  const controls = el("div", "outreach-controls");
  const selectTen = el("button", "entry-submit", "Выбрать 10");
  const refresh = el("button", "entry-submit", "Обновить текст");
  const approve = el("button", "entry-submit", "Одобрить и отправить");
  selectTen.type = refresh.type = approve.type = "button";
  refresh.disabled = approve.disabled = true;
  const checkboxes = [];
  function update() {
    const chosen = drafts.filter(draft => selected.has(draft.approval_id));
    const stale = chosen.filter(draft => draft.needs_refresh);
    refresh.disabled = !selected.size || stale.length !== chosen.length;
    approve.disabled = !enabled || !selected.size || stale.length > 0;
    refresh.textContent = "Обновить текст · " + selected.size;
    approve.textContent = "Одобрить и отправить · " + selected.size;
  }
  selectTen.addEventListener("click", () => {
    selected.clear();
    checkboxes.forEach((checkbox, index) => {
      checkbox.checked = index < 10;
      if (checkbox.checked) selected.add(checkbox.value);
    });
    update();
  });
  approve.addEventListener("click", async () => {
    approve.disabled = true;
    try {
      await apiWrite("/api/cases/" + caseId + "/outreach/approve", {
        approval_ids: [...selected]
      });
      await openDetail(caseId);
    } catch (error) {
      content.append(el("p", "entry-feedback", error.message));
      update();
    }
  });
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    try {
      await apiWrite("/api/cases/" + caseId + "/outreach/refresh", {
        approval_ids: [...selected]
      });
      await openDetail(caseId);
    } catch (error) {
      content.append(el("p", "entry-feedback", error.message));
      update();
    }
  });
  controls.append(selectTen, refresh, approve);
  content.append(controls);
  drafts.forEach(draft => {
    const row = el("label", "outreach-draft");
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.value = draft.approval_id;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && selected.size >= 10) {
        checkbox.checked = false;
        return;
      }
      if (checkbox.checked) selected.add(checkbox.value);
      else selected.delete(checkbox.value);
      update();
    });
    checkboxes.push(checkbox);
    const text = el("div", "outreach-draft-text");
    text.append(el("strong", "", draft.line_number + ". " + draft.item_name +
      " · " + draft.supplier_name));
    text.append(el("small", "", draft.recipient +
      (new Date(draft.expires_at) <= new Date() ? " · прежнее одобрение истекло" : "")));
    if (draft.needs_refresh) text.append(el("small", "entry-feedback", "Текст нужно обновить перед отправкой"));
    text.append(el("p", "", draft.text));
    row.append(checkbox, text);
    content.append(row);
  });
  panel.append(content);
  return panel;
}

function itemRequirementsPanel(caseId, item, requirements, editable) {
  const rows = requirements.filter(row => row.request_item_id === item.id);
  const panel = el("details", "item-requirements-panel");
  panel.append(el("summary", "", "Атомарные требования · " + rows.length));
  panel.append(el("p", "data-note", "Добавляйте отдельные проверяемые характеристики и указывайте, где они записаны в исходном техспеке. Сохранённые требования не редактируются, чтобы не менять историю проверок."));
  if (rows.length) {
    const list = el("ul", "item-requirements-list");
    rows.forEach(row => {
      let expected = row.expected_value;
      if (typeof expected === "string") {
        try { expected = JSON.parse(expected); } catch (_) { /* keep legacy text */ }
      }
      const value = expected && typeof expected === "object"
        ? [expected.value, expected.unit].filter(Boolean).join(" ") : String(expected ?? "");
      const label = [row.requirement_key, value].filter(Boolean).join(" — ");
      const detail = [row.operator && row.operator !== "equals" ? row.operator : "",
        row.is_hard ? "обязательное" : "желательное", row.source_pointer].filter(Boolean).join(" · ");
      const entry = el("li", "item-requirement-row");
      entry.append(el("strong", "", label), el("small", "", detail));
      list.append(entry);
    });
    panel.append(list);
  }
  if (!editable || !item.specification_text) return panel;

  const form = el("form", "item-requirement-form");
  const name = el("input"); name.required = true; name.maxLength = 160;
  const value = el("input"); value.required = true; value.maxLength = 500;
  const unit = el("input"); unit.maxLength = 40;
  const source = el("input"); source.required = true; source.maxLength = 500;
  const operator = el("select");
  [["equals", "Точное значение"], ["contains", "Содержит"], ["minimum", "Не меньше"],
    ["maximum", "Не больше"], ["range", "Диапазон"], ["one_of", "Одно из"],
    ["present", "Должно быть указано"]].forEach(([key, caption]) => operator.add(new Option(caption, key)));
  const hard = el("input"); hard.type = "checkbox"; hard.checked = true;
  const hardLabel = el("label", "item-requirement-hard", "Обязательное требование");
  hardLabel.append(hard);
  const field = (caption, control) => {
    const label = el("label", "entry-field", caption); label.append(control); return label;
  };
  form.append(field("Характеристика", name), field("Ожидаемое значение", value),
    field("Единица", unit), field("Тип условия", operator),
    field("Источник в техспеке (пункт, страница или цитата)", source), hardLabel);
  const save = el("button", "entry-submit", "Добавить требование"); save.type = "submit";
  form.append(save);
  form.addEventListener("submit", async event => {
    event.preventDefault(); save.disabled = true;
    try {
      await apiWrite("/api/cases/" + caseId + "/items/" + item.id + "/requirements", {
        idempotency_key: crypto.randomUUID(), requirement_key: name.value,
        expected_value: value.value, unit: unit.value, operator: operator.value,
        is_hard: hard.checked, source_pointer: source.value
      });
      await openDetail(caseId);
    } catch (error) {
      form.append(el("p", "entry-feedback", error.message)); save.disabled = false;
    }
  });
  panel.append(form);
  return panel;
}

function researchGeographyText(decision) {
  const labels = {
    city_verified: "Город соответствует заданной территории",
    accepted_by_address: "Адрес подтверждает соответствие заданной территории",
    outside_area: "Адрес вне заданной территории",
    unknown: "Территория не подтверждена",
    not_applicable: "Проверка территории не применяется"
  };
  return typeof decision === "string" && Object.prototype.hasOwnProperty.call(labels, decision)
    ? labels[decision] : null;
}

function caseAdoptionPanel(caseId) {
  const panel = el("details", "entry-panel");
  panel.append(el("summary", "", "Подключить существующую заявку к исследованию"));
  panel.append(el("p", "data-note", "Сначала проверим текущий разбор, позиции и действующее согласие. Заявка и её позиции не будут переписаны."));
  const preflightButton = el("button", "entry-submit", "Проверить заявку");
  preflightButton.type = "button";
  const feedback = el("p", "entry-feedback");
  panel.append(preflightButton, feedback);
  preflightButton.addEventListener("click", async () => {
    preflightButton.disabled = true;
    feedback.textContent = "Сверяем текущие позиции и согласие…";
    try {
      const facts = await api("/api/cases/" + encodeURIComponent(caseId) + "/owner-research-preflight");
      feedback.textContent = "Проверено: " + facts.items.length + " исходных позиций · города " +
        facts.ordered_cities.join(" → ") + " · SHA разбора " + facts.source_payload_sha256.slice(0, 12) + "…";
      const form = el("form", "entry-form");
      const assertionWrap = el("label", "entry-field");
      assertionWrap.append(el("span", "", "Подтверждение владельца"));
      const assertion = el("textarea");
      assertion.required = true;
      assertion.maxLength = 2000;
      assertion.value = "Подтверждаю подключение именно этой существующей заявки к исследованию по указанному согласию.";
      assertionWrap.append(assertion);
      const referenceWrap = el("label", "entry-field");
      referenceWrap.append(el("span", "", "Основание подключения"));
      const reference = el("input");
      reference.required = true;
      reference.maxLength = 500;
      reference.value = "owner-confirmed-existing-case";
      referenceWrap.append(reference);
      const adopt = el("button", "entry-submit", "Подтвердить и поставить поиск в очередь");
      adopt.type = "submit";
      const result = el("p", "entry-feedback");
      form.append(assertionWrap, referenceWrap, adopt, result);
      panel.append(form);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        adopt.disabled = true;
        result.textContent = "Фиксируем подтверждение владельца…";
        try {
          await apiWrite("/api/cases/" + encodeURIComponent(caseId) + "/owner-research-adopt", {
            preflight_sha256: facts.preflight_sha256,
            owner_assertion: assertion.value.trim(),
            adoption_reference: reference.value.trim()
          });
          result.textContent = "Подключено. Нативная очередь получила задание подготовить поиск.";
          await openDetail(caseId);
        } catch (error) {
          result.textContent = error.message;
          adopt.disabled = false;
        }
      });
    } catch (error) {
      feedback.textContent = error.message;
      preflightButton.disabled = false;
    }
  });
  return panel;
}

function sourcingInstructionForm(caseId, item, sourceItem, currentSourceHash) {
  const existing = effectiveOwnerSourcingInstruction(sourceItem, currentSourceHash);
  if (existing) {
    const saved = el("details", "entry-panel source-instruction");
    saved.append(el("summary", "", "Указание владельца для текущего разбора"));
    const quantityValue = Number(existing.quantity || 0);
    const remaining = Math.max(0, Number(item.quantity || 0) - quantityValue);
    if (existing.disposition === "skip") {
      saved.append(el("p", "data-note",
        "Позиция исключена из поиска по указанию владельца. Это sourcing instruction, не запись бухгалтерского учёта."));
    } else if (existing.disposition === "bought") {
      saved.append(el("p", "data-note", "Владелец сообщает о покупке "
        + quantity(quantityValue, item.unit || "") + "; для поиска останется "
        + quantity(remaining, item.unit || "") + ". Это не подтверждённый учётной системой платёж."));
    } else {
      saved.append(el("p", "data-note", "По указанию владельца искать "
        + quantity(remaining, item.unit || "") + " из исходных "
        + quantity(item.quantity, item.unit || "") + ". Это не бухгалтерская запись."));
    }
    if (existing.source_reference) {
      saved.append(el("p", "data-note", "Источник, указанный владельцем: " + existing.source_reference));
    }
    saved.append(el("p", "data-note", sourceItem.disposition_applied_to_plan
      ? "Указание включено в привязку плана поиска."
      : "Указание сохранено, но ещё не включено в привязку плана поиска."));
    saved.append(el("p", "data-note", "Исправление эффективного указания требует проверки новой версии разбора."));
    return saved;
  }

  const details = el("details", "entry-panel");
  details.append(el("summary", "", "Свидетельство о закупке или исключении позиции"));
  details.append(el("p", "data-note", "По умолчанию это только свидетельство. Отдельный флажок явно применяет указание владельца к следующему плану поиска; SHA источника указывается владельцем и не проверяется как байты файла."));
  const form = el("form", "entry-form");
  const [sourceField, sourceRef] = inputField("Источник / ссылка владельца", "text", {maxLength: 500, required: true});
  const [hashField, sourceHash] = inputField("SHA-256 источника (указан владельцем)", "text", {
    maxLength: 64, minLength: 64, required: true, pattern: "[0-9a-f]{64}"});
  const [kindField, kind] = selectField("Указание по позиции", [
    ["skip", "Уже обеспечено · исключить из поиска"],
    ["bought", "Уже закуплено · указать купленное количество"],
    ["reduce_quantity", "Уменьшить искомое количество"]
  ]);
  const [quantityField, amount] = inputField("Уже купленное количество", "number", {
    step: "0.000001", min: "0.000001", max: String(item.quantity || ""), required: true});
  const [assertionField, assertion] = inputField("Подтверждение владельца", "text", {
    maxLength: 4000, required: true});
  const [reasonField, reason] = inputField("Почему источник соответствует этой точной позиции", "text", {
    maxLength: 2000, required: true});
  const applyWrap = el("label", "entry-field");
  const apply = el("input");
  apply.type = "checkbox";
  const canApply = Boolean(sourceItem && Array.isArray(sourceItem.issues)
    && sourceItem.issues.length === 0);
  apply.disabled = !canApply;
  const applyText = el("span", "", canApply
    ? "Явно применить к следующему плану поиска по этой точной позиции"
    : "Нужно уточнить позицию; это свидетельство не будет менять план поиска");
  applyWrap.append(apply, applyText);
  const feedback = el("p", "entry-feedback");
  const save = el("button", "entry-submit", "Сохранить свидетельство владельца");
  save.type = "submit";
  const setQuantityState = () => {
    quantityField.hidden = kind.value === "skip";
    amount.required = kind.value !== "skip";
  };
  kind.addEventListener("change", setQuantityState);
  setQuantityState();
  form.append(sourceField, hashField, kindField, quantityField, assertionField, reasonField,
    applyWrap, save, feedback);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    save.disabled = true;
    feedback.textContent = "Сохраняем…";
    try {
      await apiWrite("/api/cases/" + encodeURIComponent(caseId) + "/items/" +
        encodeURIComponent(item.id) + "/sourcing-dispositions", {
          source_kind: "operator_attestation", source_artifact_id: null,
          source_reference: sourceRef.value.trim(), source_sha256: sourceHash.value.trim(),
          disposition: kind.value, quantity: kind.value === "skip" ? null : amount.value,
          unit: item.unit || "", owner_assertion: assertion.value.trim(),
          exact_match_rationale: reason.value.trim(), applies_to_sourcing: apply.checked,
          source_payload_sha256: apply.checked ? currentSourceHash : null,
          idempotency_key: crypto.randomUUID()
        });
      feedback.textContent = apply.checked
        ? "Указание владельца сохранено и связано с текущим разбором для следующего плана поиска."
        : "Свидетельство сохранено. Объём поиска не менялся.";
      await openDetail(caseId);
    } catch (error) {
      feedback.textContent = error.message;
      save.disabled = false;
    }
  });
  return details;
}

function sourceItemPanel(source, currentSourceHash) {
  const panel = el("details", "item-requirements-panel");
  const stages = {ready_for_search: "Готово к поиску", needs_review: "Нужно уточнение",
    comparable_quote: "Есть сравнимое КП", bought: "Закуплено, подтверждено",
    evidence_only: "Есть свидетельство владельца · поиск не изменён",
    owner_excluded_from_sourcing: "По явному указанию владельца исключено из поиска"};
  const currentInstruction = effectiveOwnerSourcingInstruction(source, currentSourceHash);
  const stageLabel = source.stage === "bought" ? "Закуплено, внесено в учёт"
    : currentInstruction?.disposition === "skip"
      ? "Исключено из поиска по указанию владельца"
      : source.sourcing_evidence?.length ? "Покупка требует сверки"
        : stages[source.stage] || "Источник позиции";
  panel.append(el("summary", "", stageLabel));
  if (source.source_reference) panel.append(el("p", "data-note", "Источник: " + source.source_reference));
  const issues = (source.issues || []).map(issue => ({name_missing: "Не указано название",
    low_confidence: "Низкая уверенность распознавания",
    source_projection_unavailable: "Нет связанного разбора источника"}[issue]
    || (issue.startsWith("requirements.") ? "Неоднозначное техническое требование" : "Уточнить: " + issue)));
  for (const text of [...new Set(issues)]) panel.append(el("p", "data-note", text));
  for (const requirement of (source.requirements || []).filter(row => row.ambiguous)) {
    panel.append(el("p", "data-note", [requirement.name, requirement.value,
      requirement.source_reference].filter(Boolean).join(" · ")));
  }
  for (const evidence of source.sourcing_evidence || []) {
    panel.append(el("p", "data-note", "По словам владельца: «" + evidence.owner_assertion + "»"));
    if (evidence.source_reference) panel.append(el("p", "data-note",
      (evidence.source_kind === "operator_attestation" ? "Источник, указанный владельцем: "
        : "Источник: ") + evidence.source_reference));
    panel.append(el("p", "data-note", evidence.source_sha256_basis === "owner_declared"
      ? "SHA-256 заявлен владельцем; файл не проверялся системой."
      : "SHA-256 файла Telegram проверен по сохранённому источнику."));
    const appliesToCurrentParse = evidence.applies_to_sourcing
      && evidence.source_payload_sha256 === currentSourceHash;
    panel.append(el("p", "data-note", appliesToCurrentParse
      ? "Указание владельца связано с текущей версией разбора. "
        + (source.disposition_applied_to_plan
          ? "Оно включено в привязку плана поиска. "
          : "Оно ещё не включено в привязку плана поиска. ")
        + "Это не запись в бухгалтерии."
      : evidence.applies_to_sourcing
        ? "Инструкция относилась к другой версии разбора; текущий поиск она не меняет."
        : "Покупка требует сверки: свидетельство не меняет поиск и не подтверждает факт бухгалтерского учёта."));
  }
  return panel;
}

function researchIssueText(code) {
  const labels = {source_exhausted: "Просмотренные результаты закончились",
    target_reached: "Цель поиска достигнута", no_new_candidates: "Новых продавцов в этой порции нет",
    batch_limit_reached: "Нужен дополнительный поиск", own_database_reused: "Проверена сохранённая база",
    phone_found: "Телефон найден", not_attempted: "Получение телефона не подтверждено",
    button_not_found: "Кнопка показа телефона не найдена", click_failed: "Не удалось открыть телефон",
    login_required: "Для телефона требуется вход", revealed_empty: "После нажатия номер не показан",
    format_invalid: "Не удалось прочитать номер", phone_not_visible: "Телефон не виден в карточке",
    contact_not_published: "В просмотренной карточке контакт не опубликован",
    permission_denied: "Браузер не получил разрешение", mcp_disconnected: "Соединение с браузером прервалось",
    cli_usage_limit: "Исчерпан лимит использования браузерного провайдера",
    cli_rate_limit: "Провайдер временно ограничил частоту запросов",
    cli_auth_failed: "Не удалось авторизовать браузерный провайдер",
    resource_wait_timeout: "Браузер занят другим заданием; ожидание истекло",
    access_blocked: "Источник ограничил доступ", attempt_failed: "Контакт не удалось проверить",
    provider_timeout: "Источник не ответил вовремя", captcha: "Источник требует CAPTCHA"};
  if (labels[code]) return labels[code];
  const diagnosticCodes = ["cli_usage_limit", "cli_rate_limit", "cli_auth_failed", "resource_wait_timeout"];
  const diagnosticCode = diagnosticCodes.find(value => typeof code === "string" && code.includes(value));
  if (diagnosticCode) return labels[diagnosticCode];
  if (/timeout/i.test(code)) return "Источник не ответил вовремя";
  if (/captcha/i.test(code)) return "Источник требует CAPTCHA";
  if (/login/i.test(code)) return "Источник требует вход";
  if (/provider_failure/i.test(code)) return "Не удалось прочитать ответ источника";
  if (/access_issue/i.test(code)) return "Источник ограничил доступ";
  return "Поиск требует проверки";
}

async function openDetail(id, options = {}) {
  const quiet = Boolean(options.quiet);
  const dialog = $("detail");
  const previousScroll = dialog.open ? dialog.scrollTop : 0;
  const root = $("detail-content");
  const requestRevision = ++state.detailRevision;
  state.detailId = id;
  state.detailController?.abort();
  state.detailRefreshController?.abort();
  const controller = new AbortController();
  state.detailController = controller;
  if (!quiet) root.replaceChildren(empty("Загружаем заявку", "Сверяем позиции и предложения."));
  if (!dialog.open) dialog.showModal();
  try {
    const data = options.data || await loadCaseDetail(id, controller.signal);
    if (controller.signal.aborted || requestRevision !== state.detailRevision
      || state.detailId !== id || !dialog.open) return;
    state.detailSnapshot = JSON.stringify(data);
    freshnessAction(false, id);
    const c = data.case;
    const editable = !["closed", "cancelled"].includes(c.status);
    const body = el("div", "detail-body");
    body.append(badge(c.status), el("h2", "", c.title || "Заявка без названия"),
      el("p", "detail-sub", [c.city, c.customer_name].filter(Boolean).join(" / ") || "Основная информация"));
    const grid = el("div", "info-grid");
    [["Город поиска", c.city || "Не указан"], ["Доставка", c.delivery_address || "Не указана"],
      ["Срок", fmtDate(c.deadline_at)],
      ["Позиций", String(data.items.length)], ["Обновлено", fmtDate(c.updated_at)]].forEach(([key, value]) => {
      const cell = el("div", "info-cell");
      cell.append(el("small", "", key), el("strong", "", value));
      grid.append(cell);
    });
    body.append(grid, detailOptions());
    const sourceVisibility = data.source_visibility;
    if (sourceVisibility?.research_consent === "manual" && c.status === "ready") {
      body.append(caseAdoptionPanel(id));
    }
    if (sourceVisibility?.warnings?.length) {
      const warnings = el("details", "item-requirements-panel");
      warnings.append(el("summary", "", "Замечания к источнику · " + sourceVisibility.warnings.length));
      sourceVisibility.warnings.forEach(text => warnings.append(el("p", "data-note", text)));
      body.append(warnings);
    }
    if (sourceVisibility?.report_checkpoint_at) body.append(el("p", "data-note",
      "Контрольная отметка: " + fmtDateTime(sourceVisibility.report_checkpoint_at)));
    const metrics = data.pipeline_metrics;
    if (metrics) {
      body.append(el("h3", "", "Поиск, предложения и связь"));
      body.append(el("p", "data-note",
        `Лиды: ${metrics.leads}; публичные цены: ${metrics.public_prices}; ` +
        `цены из ответов: ${metrics.supplier_prices}; сопоставимые подтверждённые: ${metrics.comparable_quotes}.`));
      body.append(el("p", "data-note",
        `Принято WAHA: ${metrics.accepted}; в журнале диалогов: ${metrics.outbound_projected}; ` +
        `доставлено: ${metrics.delivered}; прочитано: ${metrics.read}; ` +
        `доставка не подтверждена: ${metrics.delivery_unknown}; ошибки ACK: ${metrics.delivery_failed}.`));
      body.append(el("p", "data-note",
        `Приём попытки отправки не подтверждён: ${metrics.acceptance_unknown ?? 0}; ` +
        `принято WAHA, ожидает записи в диалог: ${metrics.accepted_pending_projection ?? 0}.`));
      body.append(el("p", "data-note",
        `Незавершённые переговоры: ${metrics.waiting_chats}; рабочие группы: ${metrics.active_groups}. ` +
        "Недоступная история WAHA не доказывает отсутствие ответа."));
    }
    const health = data.pipeline_health;
    if (health?.provider_auth?.available === false) {
      body.append(el("p", "data-note", "Авторизация обработчика AGY отсутствует. " +
        "Задания, которым он нужен, ожидают восстановления доступа; сообщения сохранены."));
    }
    if (health) {
      const account = health.account || {};
      const ingress = health.ingress || {};
      const research = health.research || {};
      body.append(el("h3", "", "Надёжность и ожидания"));
      if (Number(account.accounts || 0) > 0) {
        body.append(el("p", "data-note",
          `WhatsApp-сессии: ${account.accounts}; на ручной паузе: ${account.held}; ` +
          `с ограничением/timelock: ${account.restricted}; недоступны по GET: ${account.unavailable}; ` +
          `защитный контур открыт: ${account.circuits_open}; ` +
          `состояние неизвестно: ${account.state_unknown}; разрешено по сохранённым правилам: ${account.outbound_allowed}.`));
        body.append(el("p", "data-note",
          `Фактический статус WAHA по последнему GET: ${account.session_status || "не записан"}; ` +
          `состояние аккаунта наблюдалось: ${fmtDateTime(account.provider_observed_at)}; ` +
          `следующая проверка аккаунта: ${fmtDateTime(account.next_probe_at)}.`));
        body.append(el("p", "data-note",
          `Суточные лимиты связанных сессий: ${account.daily_message_limit ?? 0} сообщений, ` +
          `${account.daily_new_contact_limit ?? 0} новых контактов; ` +
          `одновременные отправки по аккаунтам: ${account.max_concurrent_posts ?? 0}. ` +
          `Рабочие группы назначаются отдельно: ${metrics?.active_groups ?? 0}.`));
      }
      body.append(el("p", "data-note",
        `Webhook события за 7 дней (вся система): приняты ${ingress.accepted_7d ?? 0}, ` +
        `проигнорированы ${ingress.ignored_7d ?? 0}, отклонены ${ingress.rejected_7d ?? 0}; ` +
        `последнее событие: ${fmtDateTime(ingress.last_seen_at)}.`));
      if (research.run_status) {
        body.append(el("p", "data-note",
          `Состояние поиска: ${research.run_status}; причина ожидания: ${pipelineWaitLabel(research.wait_reason)}; ` +
          `последний сохранённый прогресс: ${fmtDateTime(research.last_real_progress_at)}; ` +
          `следующее пробуждение: ${fmtDateTime(research.next_wake_at)}.`));
        body.append(el("p", "data-note",
          `Заданий очереди без активного владельца: ${research.queued_without_runnable_job}; ` +
          `с завершённым заданием: ${research.queued_with_terminal_job}; ` +
          `остановка без задания: ${research.stalled ? "да" : "нет"}.`));
      }
    }
    const reviews = data.pipeline_reviews;
    if (reviews && Object.keys(reviews.counts || {}).length) {
      body.append(el("h3", "", "Требуется проверка"));
      const labels = {unknown_send: "Результат отправки не подтверждён",
        blocked_reply: "Ответ ждёт подтверждения исходного сообщения",
        reply_processing_failed: "Ошибка обработки сохранённого ответа"};
      Object.entries(reviews.counts).forEach(([kind, count]) => {
        body.append(el("p", "data-note", `${labels[kind] || kind}: ${count}.`));
      });
      body.append(el("p", "data-note", "Исходные сообщения и идентификаторы сохранены. " +
        "Неопределённые отправки не повторяются автоматически."));
      if (reviews.truncated) body.append(el("p", "data-note",
        "Показаны первые 100 записей; общий счётчик включает остальные."));
      (reviews.items || []).forEach(review => {
        body.append(el("p", "data-note", `${labels[review.kind] || review.kind}: ` +
          `${review.subject_id}; причина: ${review.reason_code}; ` +
          `последняя проверка: ${fmtDateTime(review.last_seen_at)}.`));
        const context = [];
        const supplier = [review.supplier_name, review.supplier_contact]
          .filter(Boolean).join(" · ");
        if (supplier) context.push("Поставщик: " + supplier + ".");
        if (review.conversation_state || review.external_chat_id) {
          context.push("Диалог: " + (review.conversation_state || "состояние неизвестно") +
            (review.opted_out ? "; отказался от сообщений" : "") +
            (review.external_chat_id ? "; WhatsApp " + review.external_chat_id : "") + ".");
        }
        if (review.kind === "unknown_send") {
          if (review.outbound_status || review.outbound_error_code || review.provider_request_id) {
            context.push("Отправка: " + (review.outbound_status || "состояние неизвестно") +
              "; ошибка " + (review.outbound_error_code || "нет") +
              "; запрос провайдера " + (review.provider_request_id || "не записан") + ".");
          }
          if (review.outbound_text) context.push("Текст отправки: «" +
            review.outbound_text + (review.outbound_text_truncated ? "…»" : "»"));
        } else {
          if (review.inbound_text) context.push("Ответ поставщика: «" +
            review.inbound_text + (review.inbound_text_truncated ? "…»" : "»") +
            (review.inbound_received_at ? " · " + fmtDateTime(review.inbound_received_at) : ""));
          if (review.kind === "reply_processing_failed" && review.reply_job_status) {
            context.push("Обработка ответа: " + review.reply_job_status +
              "; попытки " + review.reply_job_attempts + "/" +
              review.reply_job_max_attempts + "; ошибка " +
              (review.reply_job_error_code || "нет") + ".");
          }
        }
        if (context.length) body.append(el("p", "data-note", context.join(" ")));
      });
    }
    const runtime = data.pipeline_runtime;
    if (runtime?.enabled) {
      if (runtime.last_tick_summary?.waha_status && runtime.last_tick_summary.waha_status !== "WORKING") {
        body.append(el("p", "data-note", "WhatsApp недоступен: " + runtime.last_tick_summary.waha_status +
          ". Отправки ожидают восстановления соединения."));
      }
      body.append(el("p", "data-note", runtime.paused ? "Пайплайн приостановлен владельцем."
        : runtime.quota_probe_at ? "Ожидание лимита. Следующая проверка: " + fmtDate(runtime.quota_probe_at)
        : "Следующая проверка очереди: " + fmtDate(runtime.next_tick_at)));
    }
    const progressRows = data.research_progress || [];
    const progressByItem = new Map(progressRows.map(row => [row.request_item_id, row]));
    if (progressRows.length) {
      const active = progressRows.filter(row => row.status === "researching").length;
      const complete = progressRows.filter(row => ["succeeded", "partial", "failed"].includes(row.status)).length;
      body.append(el("p", "data-note", `Поиск: ${complete} из ${progressRows.length} позиций обработано; ${active} сейчас в работе. ` +
        "Найденный контакт ещё не подтверждает цену, наличие и соответствие техспеку."));
      const discoveryTotals = progressRows.reduce((totals, row) => {
        (row.sources || []).forEach(source => {
          totals.discovered += Number(source.discovered_cards || 0);
          totals.extracted += Number(source.extracted_cards || 0);
        });
        return totals;
      }, {discovered: 0, extracted: 0});
      if (discoveryTotals.discovered || discoveryTotals.extracted) {
        body.append(el("p", "data-note",
          `В выдаче найдено карточек ${discoveryTotals.discovered}; проверено ${discoveryTotals.extracted}. ` +
          "Карточки источников ещё не являются предложениями или подтверждёнными ценами."));
      }
      const refresh = el("button", "entry-submit", "Обновить ход поиска");
      refresh.type = "button";
      refresh.addEventListener("click", () => openDetail(id));
      body.append(refresh);
      if (progressRows.some(row => ["queued", "researching", "paused"].includes(row.status))) {
        const paused = progressRows.some(row => row.status === "paused");
        const control = el("button", "entry-submit", paused ? "Продолжить поиск" : "Приостановить поиск");
        control.type = "button";
        control.addEventListener("click", async () => {
          control.disabled = true;
          try {
            await apiWrite("/api/cases/" + encodeURIComponent(id) + "/research-control",
              {action: paused ? "resume" : "pause"});
            await openDetail(id);
          } catch (error) { body.append(el("p", "entry-feedback", error.message)); control.disabled = false; }
        });
        body.append(control);
        if (!paused && progressRows.some(row => Number(row.branches_failed || 0) > 0)) {
          const retry = el("button", "entry-submit", "Повторить технические ошибки");
          retry.type = "button";
          retry.addEventListener("click", async () => {
            retry.disabled = true;
            try {
              const result = await apiWrite("/api/cases/" + encodeURIComponent(id) + "/research-control",
                {action: "retry_failed"});
              if (result.retried_count) await openDetail(id);
              else {
                body.append(el("p", "data-note", "Нет ошибок, доступных для повторной попытки."));
                retry.disabled = false;
              }
            } catch (error) { body.append(el("p", "entry-feedback", error.message)); retry.disabled = false; }
          });
          body.append(retry);
        }
      }
    }
    if (editable && data.outreach_drafts?.length) {
      body.append(outreachDraftsPanel(id, data.outreach_drafts, data.outreach_enabled));
    }
    body.append(el("h3", "", "Позиции по этапам"));
    body.append(el("p", "stage-explainer", "Этапы показывают ход сверки и поиска, а не готовность к покупке. " +
      "Сравнимым считается только подтверждённая цена по нужной партии и проверенному ТЗ."));
    const groupedItems = groupCaseItems(data.items, sourceVisibility, progressRows,
      data.offers, metrics?.waiting_chats);
    let activeStageFilter = "all";
    let filterItems;
    let stageSections;
    const stageSummaryModel = renderCaseStageSummary(groupedItems, data.items.length, key => {
      activeStageFilter = key;
      stageSummaryModel.buttons.forEach((button, buttonKey) =>
        button.setAttribute("aria-pressed", buttonKey === key ? "true" : "false"));
      if (key !== "all") stageSections.get(key).section.open = true;
      filterItems();
    });
    body.append(stageSummaryModel.element);

    const itemFilter = el("div", "item-filter");
    const itemSearch = el("input", "offer-search item-search");
    itemSearch.type = "search";
    itemSearch.placeholder = "Название, номер или техспек позиции";
    itemSearch.setAttribute("aria-label", "Поиск позиций в заявке");
    const offerFilter = el("select", "item-filter-select");
    [["all", "Все предложения"], ["with", "Есть предложения"], ["without", "Нет предложений"]]
      .forEach(([value, label]) => offerFilter.add(new Option(label, value)));
    offerFilter.setAttribute("aria-label", "Фильтр позиций по предложениям");
    const filteredCount = el("span", "data-note");
    itemFilter.append(itemSearch, offerFilter, filteredCount);
    body.append(itemFilter);
    const stageList = el("div", "stage-groups");
    stageSections = new Map();
    groupedItems.groups.forEach(group => {
      const section = el("details", "stage-group");
      section.open = ["needs_clarification", "comparable_options"].includes(group.key);
      const summary = el("summary", "stage-group-summary");
      const title = el("span", "", group.label);
      const count = el("span", "stage-group-count", String(group.items.length));
      summary.append(title, count);
      const content = el("div", "stage-group-items");
      section.append(summary, content);
      section.hidden = group.items.length === 0;
      stageList.append(section);
      stageSections.set(group.key, {section, summary, count, content});
    });
    if (data.offers_truncated) body.append(el("p", "data-note", "Загружены не все предложения. Обновите заявку, чтобы получить оставшиеся."));
    if (!data.items.length) body.append(empty("Позиций пока нет", "Бот добавит их после обработки заявки."));
    body.append(stageList);
    const itemBoxes = [];
    data.items.forEach(item => {
      const box = el("div", "item-card");
      const sourceItem = sourceVisibility?.items?.find(row => row.item_id === item.id);
      const stageKey = groupedItems.assignments.get(item.id) || "not_started";
      itemBoxes.push({item, box, stageKey});
      const head = el("div", "item-head");
      const statusBadges = el("div", "item-statuses");
      statusBadges.append(itemBadge(item));
      statusBadges.append(el("span", "badge stage-badge",
        caseStageBadgeLabel(stageKey, sourceItem, sourceVisibility?.source_payload_sha256)));
      if (Number(item.in_transit_quantity || 0) > 0) {
        const transitBadge = el("span", "badge delivery-badge",
          "В пути · " + fmtNumber(item.in_transit_quantity));
        transitBadge.dataset.detailColumn = "delivery";
        transitBadge.hidden = !detailColumns.delivery;
        statusBadges.append(transitBadge);
      }
      head.append(el("span", "", item.line_number + ". " + item.name), statusBadges);
      box.append(head);
      const progress = progressByItem.get(item.id);
      if (progress) {
        const stages = {queued: "В очереди", researching: "Ищем", paused: "Приостановлено",
          succeeded: "Поиск завершён", partial: "Найдено частично", failed: "Источники недоступны"};
        box.append(detailField("Поиск", `${stages[progress.status] || progress.status} · контактов ${progress.eligible_suppliers}/${progress.target_suppliers}`));
        box.append(detailField("Результаты", `Карточек ${progress.found_leads || 0} · цен от поставщиков ${progress.qualified_price_suppliers || 0}`));
        if (progress.scope_policy?.scope === "citywide") {
          box.append(detailField("Территория",
            `Весь город ${progress.scope_policy.city} · приоритет: ${progress.scope_policy.preferred_area_text}`));
        }
        const categories = Array.isArray(progress.search_profile_categories)
          ? progress.search_profile_categories.filter(value => typeof value === "string") : [];
        const intentCount = Number(progress.search_profile_intent_count || 0);
        if (categories.length || intentCount) {
          const categoryText = categories.length ? categories.join(", ") : "не указаны";
          box.append(detailField("Профиль поиска",
            `Категории: ${categoryText} · вариантов запроса: ${intentCount}`));
        }
        if (progress.branches_failed || progress.branches_blocked) {
          box.append(el("p", "data-note", "Часть источников не ответила. Найденные карточки сохранены."));
        }
        const issues = (progress.sources || []).filter(source => source.error || source.reason
          || source.failure_code || source.contact_attempts?.length
          || Number(source.discovered_cards || 0) > 0);
        if (issues.length) {
          const details = el("details", "supplier-details");
          details.append(el("summary", "", "Источники и найденные карточки"));
          issues.forEach(source => {
            const sourceName = {"2gis_web": "2GIS", marketplace: "OLX", web: "Сайты", own_database: "Своя база"}[source.source] || "Источник";
            if (Number(source.discovered_cards || 0) > 0) {
              details.append(el("p", "data-note",
                `${sourceName}: карточек найдено ${source.discovered_cards || 0} · ` +
                `проверено ${source.extracted_cards || 0} · ожидают ${source.pending_cards || 0} · ` +
                `ошибка ${source.failed_cards || 0}`));
            }
            if (source.failure_code) details.append(el("p", "data-note",
              `${sourceName}: ${researchIssueText(source.failure_code)}`));
            if (source.error || source.reason) details.append(el("p", "data-note",
              `${sourceName}: ${researchIssueText(source.error || source.reason)} · порция ${source.batch}`));
            (source.contact_attempts || []).forEach(attempt => {
              const geography = researchGeographyText(attempt.geographic_decision);
              details.append(el("p", "data-note",
                `${sourceName} · ${attempt.external_id || "карточка"}: ${researchIssueText(attempt.contact_discovery_reason)}` +
                (geography ? ` · ${geography}` : "")));
            });
          });
          box.append(details);
        }
      }
      box.append(detailField("Требуется", quantity(item.quantity, item.unit)));
      if (item.movement_label) box.append(detailField("Этап по Notion", item.movement_label));
      if (item.reported_delivered_quantity !== null) box.append(detailField(
        "Поставлено по Notion", quantity(item.reported_delivered_quantity, item.unit)
      ));
      if (item.specification_text) box.append(detailField("Техспек", item.specification_text, "spec"));
      if (sourceItem) box.append(sourceItemPanel(sourceItem, sourceVisibility?.source_payload_sha256));
      const currentOwnerInstruction = effectiveOwnerSourcingInstruction(
        sourceItem, sourceVisibility?.source_payload_sha256);
      if ((sourceVisibility?.research_consent === "manual"
          && sourceVisibility?.source_payload_sha256 && c.status === "ready")
          || currentOwnerInstruction) {
        box.append(sourcingInstructionForm(
          id, item, sourceItem, sourceVisibility.source_payload_sha256));
      }
      box.append(itemRequirementsPanel(id, item, data.requirements || [], editable));
      if (item.reconciliation_label) box.append(detailField("Уточнение", item.reconciliation_label, "history"));
      if (item.location_label) box.append(detailField("Местонахождение по Notion", item.location_label, "history"));
      if (item.reported_accepted_quantity !== null) box.append(detailField("Принято по Notion", quantity(item.reported_accepted_quantity, item.unit), "history"));
      if (item.reported_rejected_quantity !== null) box.append(detailField("Отклонено по Notion", quantity(item.reported_rejected_quantity, item.unit), "history"));
      if (item.reported_remaining_quantity !== null) box.append(detailField("Остаток по Notion", quantity(item.reported_remaining_quantity, item.unit), "history"));
      if (item.purchased_quantity !== null) box.append(detailField("Закуплено, подтверждено", quantity(item.purchased_quantity, item.unit), "history"));
      if (item.in_transit_quantity !== null) box.append(detailField("Сейчас в пути", quantity(item.in_transit_quantity, item.unit), "delivery"));
      if (item.accepted_quantity !== null) box.append(detailField("Принято, подтверждено", quantity(item.accepted_quantity, item.unit), "history"));
      if (item.event_count) {
        box.append(detailField("Поступило на склад, события", quantity(item.warehouse_received_quantity, item.unit), "history"));
        box.append(detailField("Передано заказчику, события", quantity(item.delivered_quantity, item.unit), "history"));
        box.append(detailField("Отклонено заказчиком, события", quantity(item.rejected_quantity, item.unit), "history"));
        box.append(detailField("Возвращено, события", quantity(item.returned_quantity, item.unit), "history"));
        box.append(detailField("Отменено, события", quantity(item.cancelled_quantity, item.unit), "history"));
      }
      if (item.tender_unit_price_private !== null) box.append(detailField("Цена тендера за единицу", quantity(item.tender_unit_price_private, "₸"), "finance"));
      if (editable) {
        const priceForm = el("form", "item-tender-price");
        priceForm.dataset.detailColumn = "finance";
        priceForm.hidden = !detailColumns.finance;
        const priceLabel = el("label", "", "Цена тендера за единицу, ₸");
        const priceInput = el("input");
        priceInput.type = "number";
        priceInput.min = "0";
        priceInput.step = "0.0001";
        priceInput.required = true;
        priceInput.value = item.tender_unit_price_private ?? "";
        priceLabel.append(priceInput);
        const save = el("button", "entry-submit", "Сохранить цену");
        save.type = "submit";
        priceForm.append(priceLabel, save);
        priceForm.addEventListener("submit", async event => {
          event.preventDefault();
          save.disabled = true;
          try {
            await apiWrite("/api/cases/" + id + "/items/" + item.id + "/tender-price", {
              idempotency_key: crypto.randomUUID(),
              tender_unit_price_private: priceInput.value
            });
            await openDetail(id);
          } catch (error) {
            priceForm.append(el("p", "entry-feedback", error.message));
            save.disabled = false;
          }
        });
        box.append(priceForm);
      }
      if (item.delivery_cost !== null) box.append(detailField("Распределённая доставка", quantity(item.delivery_cost, "₸"), "finance"));
      if (item.actual_cost !== null && item.actual_cost !== undefined) box.append(detailField("Дополнительные расходы, факт", quantity(item.actual_cost, "₸"), "finance"));
      if (item.planned_cost !== null && item.planned_cost !== undefined) box.append(detailField("Дополнительные расходы, план", quantity(item.planned_cost, "₸"), "finance"));
      const purchases = (data.purchase_lines || []).filter(line => line.request_item_id === item.id);
      if (purchases.length) {
        const purchaseDetails = el("details", "item-expenses");
        purchaseDetails.append(el("summary", "", "Закупки · " + purchases.length));
        purchases.forEach(line => {
          const source = line.provenance === "documented" ? "Документ" : "Запись пользователя";
          purchaseDetails.append(detailField(
            line.seller_name || line.supplier_name || "Продавец не указан",
            quantity(line.purchased_quantity, item.unit) + " × " +
              (line.unit_cost === null ? "цена не указана" : quantity(line.unit_cost, line.currency === "KZT" ? "₸" : line.currency))
          ));
          purchaseDetails.append(el("p", "data-note", [source,
            line.supplier_name ? "Предложение: " + line.supplier_name : "",
            line.document_number, line.note]
            .filter(Boolean).join(" · ")));
        });
        box.append(purchaseDetails);
      }
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
      const offerArea = el("div", "item-offers");
      const context = {caseId: id, item, editable,
        requirements: data.requirements || [], reviews: data.technical_reviews || []};
      const topFive = itemOffers.filter(offer => offer.comparison?.rank &&
        offer.comparison.rank <= 5).sort((a, b) => a.comparison.rank - b.comparison.rank);
      offerArea.append(el("h4", "offer-section-title", "Лучшие подтверждённые · " + topFive.length));
      if (topFive.length) offerArea.append(...topFive.map(offer => offerCard(offer, context)));
      else offerArea.append(el("p", "data-note", "Пока нет предложения, у которого проверены " +
        "ТЗ, цена и количество на всю партию."));
      const otherOffers = itemOffers.filter(offer => !offer.comparison?.rank ||
        offer.comparison.rank > 5);
      const offerDetails = el("details", "other-offers");
      offerDetails.append(el("summary", "", "Остальные предложения · " + otherOffers.length));
      if (otherOffers.length) {
        const offerSearch = el("input", "offer-search");
        offerSearch.type = "search";
        offerSearch.placeholder = "Поставщик или товар";
        offerSearch.setAttribute("aria-label", "Поиск предложений по позиции " + item.name);
        const offerList = el("div", "offer-list");
        const renderOffers = () => {
          const needle = offerSearch.value.trim().toLocaleLowerCase("ru");
          const filtered = otherOffers.filter(offer =>
            (offer.supplier_name + " " + offer.supplier_product_name)
              .toLocaleLowerCase("ru").includes(needle));
          filtered.sort((a, b) => (a.comparison?.goods_total === null) -
            (b.comparison?.goods_total === null) ||
            (a.comparison?.goods_total !== null && b.comparison?.goods_total !== null
              ? Number(a.comparison.goods_total) - Number(b.comparison.goods_total) : 0));
          offerList.replaceChildren(...filtered.map(offer => offerCard(offer, context)));
          if (!filtered.length) offerList.append(el("p", "data-note", "Совпадений нет"));
        };
        offerSearch.addEventListener("input", renderOffers);
        offerDetails.append(offerSearch, offerList);
        renderOffers();
      } else offerDetails.append(el("p", "data-note", "Других предложений нет"));
      offerArea.append(offerDetails);
      if (c.item_research_enabled && ["ready", "evaluating", "report_ready"].includes(c.status)) {
        const search = el("div", "item-research");
        const button = el("button", "entry-submit", "Искать поставщиков для этой позиции");
        button.type = "button";
        const note = el("span", "data-note", "Поиск по товару, краткому описанию и городу. Полный техспек сверьте с предложениями.");
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
        offerArea.append(search);
      }
      box.append(offerArea);
      if (editable) box.append(eventForm(id, item, data));
      stageSections.get(stageKey).content.append(box);
    });
    filterItems = () => {
      const needle = itemSearch.value.trim().toLocaleLowerCase("ru");
      let shown = 0;
      const visibleByStage = new Map();
      itemBoxes.forEach(({item, box, stageKey}) => {
        const text = (item.line_number + " " + item.name + " " + (item.specification_text || ""))
          .toLocaleLowerCase("ru");
        const hasOffers = Number(item.offer_count) > 0;
        const visible = text.includes(needle)
          && (activeStageFilter === "all" || activeStageFilter === stageKey)
          && (offerFilter.value === "all"
            || (offerFilter.value === "with" && hasOffers)
            || (offerFilter.value === "without" && !hasOffers));
        box.hidden = !visible;
        if (visible) {
          shown += 1;
          visibleByStage.set(stageKey, (visibleByStage.get(stageKey) || 0) + 1);
        }
      });
      stageSections.forEach(({section, count}, key) => {
        const visibleCount = visibleByStage.get(key) || 0;
        section.hidden = visibleCount === 0;
        count.textContent = String(visibleCount);
      });
      filteredCount.textContent = "Показано " + shown + " из " + itemBoxes.length;
    };
    itemSearch.addEventListener("input", filterItems);
    offerFilter.addEventListener("change", filterItems);
    filterItems();
    body.append(el("h3", "", "Исполнение и учёт"));
    if (editable) body.append(purchaseForm(id, data));
    body.append(deliverySection(id, data));
    if (editable) body.append(expenseForm(id, data.items, data.deliveries || [],
      data.delivery_items || [], data.artifacts || []));
    body.append(closurePanel(id, data));
    captureDetailFormBaseline(body);
    root.replaceChildren(body);
    dialog.scrollTop = previousScroll;
  } catch (error) {
    if (!quiet && !controller.signal.aborted && requestRevision === state.detailRevision
      && state.detailId === id && dialog.open) {
      root.replaceChildren(empty(error.message, "Повторите попытку позже."));
    }
  }
}
async function refreshDashboard({background = false} = {}) {
  if (!initData || !state.authenticated) return;
  if ($("detail").open && !background) return;
  try {
    const summary = await api("/api/summary");
    $("active-count").textContent = summary.active_cases;
    $("items-count").textContent = summary.items;
    $("attention-count").textContent = summary.needs_attention;
    if (!background) {
      await load();
      return;
    }
    if ($("detail").open || state.loading) return;
    const active = document.activeElement;
    if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) return;
    const loadedCount = state.offset;
    const previousScroll = window.scrollY;
    let expectedRevision;
    if (!await load()) return;
    expectedRevision = state.listRevision;
    while (state.offset < loadedCount && !$("load-more").hidden
      && expectedRevision === state.listRevision) {
      if (!await load(true)) return;
      expectedRevision = state.listRevision;
    }
    if (expectedRevision === state.listRevision) window.scrollTo(0, previousScroll);
  } catch (error) {
    if (!background) $("list").replaceChildren(empty(error.message, "Повторите попытку позже."));
  }
}

async function refreshOpenDetail() {
  const dialog = $("detail");
  const id = state.detailId;
  const snapshot = state.detailSnapshot;
  const revision = state.detailRevision;
  if (!dialog.open || !id || !snapshot || !initData || !state.authenticated) return;
  state.detailRefreshController?.abort();
  const controller = new AbortController();
  state.detailRefreshController = controller;
  try {
    const data = await loadCaseDetail(id, controller.signal);
    if (controller.signal.aborted || !dialog.open || id !== state.detailId
      || revision !== state.detailRevision || snapshot !== state.detailSnapshot) return;
    if (JSON.stringify(data) === snapshot) return;
    const root = $("detail-content");
    if (canRefreshDetailInPlace(root)) {
      await openDetail(id, {data, quiet: true});
    } else {
      freshnessAction(true, id);
    }
  } catch (_) {
    // Background refresh failures stay quiet; manual refresh remains available.
  } finally {
    if (state.detailRefreshController === controller) state.detailRefreshController = null;
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
$("detail").addEventListener("close", () => {
  state.detailRevision += 1;
  state.detailId = null;
  state.detailSnapshot = null;
  state.detailController?.abort();
  state.detailRefreshController?.abort();
  freshnessAction(false, null);
});
setInterval(() => {
  if (!state.authenticated || document.visibilityState !== "visible") return;
  if ($("detail").open) refreshOpenDetail();
  else refreshDashboard({background: true});
}, 30000);

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
    state.authenticated = true;
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
