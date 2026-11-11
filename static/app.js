/* SZAUCTRACKER — локальный интерфейс */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  config: null,
  status: null,
  results: { deals: [], items: [] },
  categories: [],
  itemMeta: {},
  activeCategory: "",
  visibleDeals: [],
  lastRev: null,
  lastError: null,
  searchTimer: null,
  deadItems: [],
  analytics: { item: null, days: 7, variant: null, data: null },
  movement: { item: null, days: 7, data: null },
  signals: { days: 7, data: null },
  barter: { items: null, item: null, data: null, ready: false, error: null },
  fast: { events: [], seen: null, data: null },
  portfolio: null,
  ruleItem: null,
};

const QUALITY_NAMES = {
  0: "Обычный",
  1: "Необычный",
  2: "Особый",
  3: "Редкий",
  4: "Исключительный",
  5: "Легендарный",
  6: "Уникальный",
};

const BONUS_LABELS = {
  RADIATION_ACC: "Радиация",
  STAMINA_BONUS: "Выносливость",
  SPEED_MOD: "Скорость",
  REGENERATION_BONUS: "Регенерация",
  BLEEDING_ACC: "Кровотечение",
  HEALTH_BONUS: "Здоровье",
  BULLET_DAMAGE: "Урон пулями",
  DAMAGE: "Урон",
  FROST: "Мороз",
  BURN: "Ожог",
  ELECTRA: "Электрошок",
};

const nf = new Intl.NumberFormat("ru-RU");
const CATEGORY_FALLBACK = {
  artefact: "Артефакты",
  weapon: "Оружие",
  armor: "Броня и костюмы",
  attachment: "Обвесы",
  consumables: "Расходники",
  containers: "Контейнеры",
  device: "Устройства",
  backpacks: "Рюкзаки",
  weapon_modules: "Модули оружия",
  weapon_style: "Скины оружия",
  armor_style: "Скины брони",
  bullet: "Патроны",
  misc: "Разное",
  other: "Прочее",
};
const STATUS_LABELS = {
  ok: "лоты не дешевле порога",
  no_sales: "нет продаж за окно",
  no_data: "нет истории покупок",
  no_lots: "нет активных лотов",
  error: "ошибка запроса",
  stopped: "остановлено",
};
const BENCH_LABELS = {
  adaptive: "адаптивная (свежие + тренд)",
  mean: "средняя продаж",
  median: "медиана продаж",
  trimmed: "усечённая средняя продаж",
  lots: "активные лоты",
  blend: "продажи + лоты",
};
const DIRECTION_LABELS = {
  bulk_to_singles: "Купить стак → продать поштучно",
  singles_to_bulk: "Купить поштучно → продать стаком",
};

// ------------------------------------------------------------------ утилиты

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return nf.format(Math.round(Number(value)));
}

function moneyFine(value) {
  const number = Number(value);
  if (value === null || value === undefined || Number.isNaN(number)) return "—";
  if (Math.abs(number) < 100) {
    return number.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  return money(number);
}

function pct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(digits).replace(".", ",")}%`;
}

function timeLeft(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  if (minutes <= 0) return "истёк";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = Math.floor(minutes % 60);
  if (days > 0) return `${days} д ${hours} ч`;
  if (hours > 0) return `${hours} ч ${mins} м`;
  return `${mins} м`;
}

function dateTime(value) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function categoryLabel(top) {
  const found = state.categories.find((c) => c.id === top);
  return found ? found.label : (CATEGORY_FALLBACK[top] || top);
}

function categoryOf(item) {
  return String(item || "other").split("/")[0];
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  let data = null;
  try { data = await response.json(); } catch { /* пустой ответ */ }
  if (!response.ok) {
    const message = (data && (data.error || data.title)) || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function toast(message, kind = "") {
  const host = $("#toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .2s";
    setTimeout(() => node.remove(), 220);
  }, 4200);
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(`Скопировано: ${text}`);
}

// ------------------------------------------------------------------ вкладки

function switchTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
}

function bindTabs() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    switchTab(tab.dataset.tab);
    if (tab.dataset.tab === "analytics") ensureAnalytics();
    if (tab.dataset.tab === "movement") ensureMovement();
    if (tab.dataset.tab === "signals") ensureSignals();
    if (tab.dataset.tab === "barter") ensureBarter();
    if (tab.dataset.tab === "portfolio") ensurePortfolio();
  }));
  const initial = location.hash.replace("#", "");
  if (["deals", "stacks", "analytics", "movement", "signals", "barter", "portfolio", "watch", "settings"].includes(initial)) {
    switchTab(initial);
    if (initial === "analytics") ensureAnalytics();
    if (initial === "movement") ensureMovement();
    if (initial === "signals") ensureSignals();
    if (initial === "barter") ensureBarter();
    if (initial === "portfolio") ensurePortfolio();
  }
}

// ------------------------------------------------------------------ статус

function renderStatus(status) {
  state.status = status;
  if (status && Array.isArray(status.dead_items)) {
    const previous = (state.deadItems || []).length;
    if (status.dead_items.length !== previous) {
      state.deadItems = status.dead_items;
      renderDeadHint();
    }
  }
  const pill = $("#statusPill");
  const text = $("#statusText");
  const button = $("#scanBtn");
  const cancelButton = $("#cancelBtn");
  pill.className = "pill";
  cancelButton.classList.add("hidden");

  if (!status) return;

  if (status.running) {
    pill.classList.add("running");
    const total = status.total ? ` ${status.done}/${status.total}` : "";
    const eta = status.eta_sec;
    const etaText = eta ? ` · ~${eta >= 60 ? `${Math.ceil(eta / 60)} мин` : `${eta} с`}` : "";
    text.textContent = `сканирование${total}${etaText}`;
    button.disabled = true;
    button.textContent = "Сканирую…";
    cancelButton.classList.remove("hidden");
  } else if (status.error) {
    pill.classList.add("error");
    text.textContent = "ошибка";
    button.disabled = false;
    button.textContent = "Сканировать";
    if (status.error !== state.lastError) {
      state.lastError = status.error;
      toast(status.error, "bad");
    }
  } else if (status.phase === "остановлено") {
    pill.classList.add("idle");
    text.textContent = "остановлено";
    button.disabled = false;
    button.textContent = "Сканировать";
  } else if (status.results_rev) {
    pill.classList.add("done");
    text.textContent = "готово";
    button.disabled = false;
    button.textContent = "Сканировать";
  } else {
    pill.classList.add("idle");
    text.textContent = "ожидание";
    button.disabled = false;
    button.textContent = "Сканировать";
  }

  const apiStats = status.api || {};
  const pieces = [];
  if (state.results.region) pieces.push(state.results.region);
  if (apiStats.requests) pieces.push(`запросов ${apiStats.requests}`);
  if (apiStats.units) pieces.push(`токенов ${apiStats.units}`);
  if (apiStats.remaining !== null && apiStats.remaining !== undefined) {
    pieces.push(`осталось ${apiStats.remaining}/${apiStats.limit || 400}`);
  }
  if (state.results.demo) pieces.push("ДЕМО");
  $("#apiInfo").textContent = pieces.length ? `API: ${pieces.join(" · ")}` : "API: —";

  if (status.running) {
    const total = status.total || 0;
    $("#calcHint").textContent = total
      ? `Идёт скан: ${status.done}/${total}${status.current ? ` · ${status.current}` : ""} · ${status.phase || ""}`
      : "Идёт подготовка…";
  }
}

async function poll() {
  try {
    const status = await api("/api/status");
    renderStatus(status);
    if (status.results_rev && status.results_rev !== state.lastRev) {
      state.lastRev = status.results_rev;
      await loadResults();
    }
    await pollFast();
  } catch (error) {
    // Сервер мог перезапускаться — молча ждём следующий тик.
  }
}

// ------------------------------------------------------------------ конфиг

async function loadConfig() {
  const data = await api("/api/config");
  state.config = data.config;
  state.catalogError = data.catalog_error || null;
  state.deadItems = data.dead_items || [];
  renderRegionSelect(data.regions || []);
  renderBanner();
  renderSettings();
  renderWatchList();
  renderDeadHint();
  const watchCount = (state.config.watchlist || []).length;
  $("#watchBadge").textContent = String(watchCount);
}

function renderDeadHint() {
  const hint = $("#deadHint");
  if (!hint) return;
  const ids = state.deadItems || [];
  if (!ids.length) {
    hint.classList.add("hidden");
    hint.innerHTML = "";
    return;
  }
  hint.classList.remove("hidden");
  hint.innerHTML = `⚠ ${ids.length} недействительных ID пропускаются при скане. <button type="button" class="btn small" id="deadCleanupBtn">Удалить из наблюдения</button>`;
  $("#deadCleanupBtn").onclick = async () => {
    if (!confirm(`Удалить ${ids.length} недействительных ID из списка наблюдения?`)) return;
    try {
      const result = await api("/api/dead/cleanup", { method: "POST", body: {} });
      state.config.watchlist = result.watchlist;
      state.deadItems = [];
      $("#watchBadge").textContent = String(result.count);
      renderDeadHint();
      renderWatchList();
      renderAnalyticsItems();
      toast(`Удалено ${result.removed}. Запустите скан, чтобы обновить результаты.`);
    } catch (error) {
      toast(error.message, "bad");
    }
  };
}

function renderRegionSelect(regions) {
  const select = $("#regionSelect");
  const current = state.config ? state.config.region : "RU";
  select.innerHTML = (regions || []).map(
    (region) => `<option value="${esc(region.id)}"${region.id === current ? " selected" : ""}>${esc(region.name)} (${esc(region.id)})</option>`
  ).join("");
}

function renderBanner() {
  const banner = $("#banner");
  const cfg = state.config;
  if (!cfg) return;

  let html = "";
  let cls = "banner";
  if (cfg.demo) {
    cls += " demo";
    html = `Демо-режим: ключи не используются. Демо-API отвечает только на тестовый предмет <code>y1q9</code>, цены в нём синтетические.`;
  } else if (!cfg.configured) {
    html = `Ключи API не заданы. Получите Client-Id и Client-Secret у Telegram-бота <a href="https://t.me/stalzone_api_bot" target="_blank" rel="noreferrer">@stalzone_api_bot</a> и вставьте их в настройках.`;
  }
  if (state.catalogError) {
    html += `${html ? "<br>" : ""}Справочник предметов не загрузился: ${esc(state.catalogError)}`;
  }

  if (!html) {
    banner.className = "banner hidden";
    banner.innerHTML = "";
    return;
  }

  banner.className = cls;
  banner.innerHTML = `<span>${html}</span><button type="button" class="btn small" id="bannerSettings">Настройки</button>`;
  $("#bannerSettings").addEventListener("click", () => switchTab("settings"));
}

function renderSettings() {
  const cfg = state.config;
  if (!cfg) return;
  const form = $("#settingsForm");

  form.elements.client_id.value = cfg.client_id || "";
  form.elements.client_secret.value = "";
  form.elements.client_secret.placeholder = cfg.has_secret
    ? "•••••••• сохранён — оставьте пустым, чтобы не менять"
    : "секретный ключ";
  form.elements.region.value = cfg.region;
  form.elements.demo.checked = !!cfg.demo;
  form.elements.days.value = cfg.days;
  form.elements.threshold_pct.value = cfg.threshold_pct;
  form.elements.commission_pct.value = cfg.commission_pct;
  form.elements.stack_qty.value = cfg.stack_qty;
  form.elements.stack_threshold_pct.value = cfg.stack_threshold_pct;
  form.elements.stack_min_sales.value = cfg.stack_min_sales;
  form.elements.benchmark.value = cfg.benchmark;
  form.elements.cap_by_market.checked = !!cfg.cap_by_market;
  form.elements.min_volume.value = cfg.min_volume;
  form.elements.min_price.value = cfg.min_price;
  form.elements.max_price.value = cfg.max_price;
  form.elements.min_profit.value = cfg.min_profit;
  form.elements.lots_pages.value = cfg.lots_pages;
  form.elements.history_pages.value = cfg.history_pages;
  form.elements.group_by_qlt.checked = !!cfg.group_by_qlt;
  form.elements.group_by_ptn.checked = !!cfg.group_by_ptn;
  form.elements.group_by_stack.checked = cfg.group_by_stack !== false;
  form.elements.auto_scan.checked = !!cfg.auto_scan;
  form.elements.scan_interval_min.value = cfg.scan_interval_min;
  form.elements.scan_workers.value = cfg.scan_workers;
  form.elements.history_ttl_min.value = cfg.history_ttl_min;
  form.elements.discovery_enabled.checked = cfg.discovery_enabled !== false;
  form.elements.discovery_limit.value = cfg.discovery_limit || 10;
  form.elements.fast_interval_sec.value = cfg.fast_interval_sec || 30;

  $("#secretHint").textContent = cfg.has_secret
    ? "Секрет сохранён. Он не отображается в браузере и не покидает этот компьютер."
    : "Секрет ещё не сохранён.";
}

function collectSettings() {
  const form = $("#settingsForm");
  const value = (name) => form.elements[name].value;
  const checked = (name) => form.elements[name].checked;
  return {
    client_id: value("client_id").trim(),
    client_secret: value("client_secret"),
    region: value("region"),
    demo: checked("demo"),
    days: Number(value("days")),
    threshold_pct: Number(value("threshold_pct")),
    commission_pct: Number(value("commission_pct")),
    stack_qty: Number(value("stack_qty")),
    stack_threshold_pct: Number(value("stack_threshold_pct")),
    stack_min_sales: Number(value("stack_min_sales")),
    benchmark: value("benchmark"),
    cap_by_market: checked("cap_by_market"),
    min_volume: Number(value("min_volume")),
    min_price: Number(value("min_price")),
    max_price: Number(value("max_price")),
    min_profit: Number(value("min_profit")),
    lots_pages: Number(value("lots_pages")),
    history_pages: Number(value("history_pages")),
    group_by_qlt: checked("group_by_qlt"),
    group_by_ptn: checked("group_by_ptn"),
    group_by_stack: checked("group_by_stack"),
    auto_scan: checked("auto_scan"),
    scan_interval_min: Number(value("scan_interval_min")),
    scan_workers: Number(value("scan_workers")),
    history_ttl_min: Number(value("history_ttl_min")),
    discovery_enabled: checked("discovery_enabled"),
    discovery_limit: Number(value("discovery_limit")),
    fast_interval_sec: Number(value("fast_interval_sec")),
  };
}

async function saveSettings({ quiet = false } = {}) {
  const payload = collectSettings();
  const data = await api("/api/config", { method: "POST", body: payload });
  state.config = data.config;
  renderBanner();
  renderSettings();
  renderWatchList();
  $("#watchBadge").textContent = String((state.config.watchlist || []).length);
  if (!quiet) toast("Настройки сохранены");
  return data.config;
}

function bindSettings() {
  $("#settingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = $("#settingsStatus");
    status.className = "muted";
    status.textContent = "Сохранение…";
    try {
      await saveSettings();
      status.className = "ok";
      status.textContent = "Сохранено";
    } catch (error) {
      status.className = "bad";
      status.textContent = error.message;
      toast(error.message, "bad");
    }
  });

  $("#saveAndScanBtn").addEventListener("click", async () => {
    try {
      await saveSettings({ quiet: true });
      await startScan();
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  $("#checkBtn").addEventListener("click", async () => {
    const status = $("#settingsStatus");
    status.className = "muted";
    status.textContent = "Сохраняю и проверяю ключи…";
    try {
      // Проверяем именно то, что введено в форму: сначала сохраняем,
      // иначе /api/check смотрел бы старые ключи из конфига.
      await saveSettings({ quiet: true });
      const result = await api("/api/check", { method: "POST", body: {} });
      const limits = result.limits || {};
      const rate = limits.remaining !== null && limits.remaining !== undefined
        ? ` · токенов ${limits.remaining}/${limits.limit ?? 400}`
        : "";
      status.className = "ok";
      status.textContent = `Доступ есть · режим ${result.mode === "demo" ? "демо" : "боевой"} · регион ${result.region}${rate}`;
      toast("Ключи рабочие");
    } catch (error) {
      status.className = "bad";
      status.textContent = error.message;
      toast(error.message, "bad");
    }
  });

  $("#clearSecretBtn").addEventListener("click", async () => {
    if (!confirm("Удалить сохранённый Client-Secret?")) return;
    try {
      const data = await api("/api/config", { method: "POST", body: { clear_secret: true } });
      state.config = data.config;
      renderSettings();
      renderBanner();
      toast("Секрет удалён", "warn");
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  $("#scanBtn").addEventListener("click", () => startScan());
  $("#cancelBtn").addEventListener("click", async () => {
    try {
      await api("/api/cancel", { method: "POST", body: {} });
      toast("Останавливаю скан…", "warn");
      setTimeout(poll, 400);
    } catch (error) {
      toast(error.message, "bad");
    }
  });
}

async function startScan() {
  if (!state.config) return;
  if (!state.config.demo && !state.config.configured) {
    toast("Сначала укажите Client-Id и Client-Secret в настройках", "warn");
    switchTab("settings");
    return;
  }
  if (!(state.config.watchlist || []).length && !state.config.demo) {
    toast("Список наблюдения пуст — добавьте предметы", "warn");
    switchTab("watch");
    return;
  }
  try {
    state.lastError = null;
    const result = await api("/api/scan", { method: "POST", body: {} });
    if (result.started) toast("Скан запущен");
    renderStatus(result.status || null);
    setTimeout(poll, 400);
  } catch (error) {
    if (error.status === 409) {
      toast("Скан уже выполняется", "warn");
    } else {
      toast(error.message, "bad");
    }
  }
}

// ------------------------------------------------------------------ результаты

async function loadResults() {
  const results = await api("/api/results");
  if (!results || !results.generated_at) {
    state.results = { deals: [], items: [] };
  } else {
    state.results = results;
  }
  renderSummary();
  renderCategoryFilter();
  renderBonusFilter();
  renderDeals();
  renderNoDeals();
  renderStacks();
  $("#csvLink").classList.toggle("hidden", !(state.results.deals || []).length);
  $("#dealsBadge").textContent = String((state.results.deals || []).length || 0);
  const stacks = state.results.stacks || [];
  $("#stacksBadge").textContent = String(stacks.length);
  $("#stacksBadge").className = `badge${stacks.length ? "" : " muted"}`;
  $("#stackCsvLink").classList.toggle("hidden", !stacks.length);
  if (!state.status || !state.status.running) renderCalcHint();
}

function renderSummary() {
  const r = state.results;
  const deals = r.deals || [];
  $("#statDeals").textContent = deals.length ? String(deals.length) : "0";
  $("#statDealsSub").textContent = deals.length
    ? "выгодных лотов ниже порога"
    : r.generated_at
      ? "ниже порога не найдено"
      : "ещё не сканировали";
  $("#statProfit").textContent = deals.length ? money(r.potential_profit) : "—";
  $("#statItems").textContent = r.items_total !== undefined ? String(r.items_scanned ?? 0) : "—";
  $("#statItemsSub").textContent = r.items_total !== undefined
    ? `из ${r.items_total}${r.items_errors ? ` · ошибок ${r.items_errors}` : ""}${r.items_no_data ? ` · без данных ${r.items_no_data}` : ""}`
    : "—";
  $("#statUpdated").textContent = r.generated_at ? dateTime(r.generated_at).split(", ")[1] || dateTime(r.generated_at) : "—";
  $("#statUpdatedSub").textContent = r.generated_at
    ? `${r.elapsed_sec ?? "?"} с · ${BENCH_LABELS[r.benchmark] || r.benchmark || ""}`
    : "—";
}

function renderCalcHint() {
  const r = state.results;
  if (!r.generated_at) {
    $("#calcHint").textContent = state.config
      ? `Окно ${state.config.days} дн · порог ${state.config.threshold_pct}% · комиссия ${state.config.commission_pct}% · база: ${BENCH_LABELS[state.config.benchmark]}`
      : "";
    $("#coverageHint").classList.add("hidden");
    return;
  }
  $("#calcHint").innerHTML =
    `Окно <b>${r.days} дн.</b> · база: <b>${esc(BENCH_LABELS[r.benchmark] || r.benchmark)}</b> · ` +
    `порог <b>${r.threshold_pct}%</b> · мин. прибыль <b>${money(r.min_profit)}</b> ₽ · ` +
    `комиссия <b>${r.commission_pct}%</b> · ` +
    `обновлено ${esc(dateTime(r.generated_at))}` +
    (r.capped_count ? ` · <b>${r.capped_count}</b> лот(ов) с базой по рынку` : "") +
    (r.discovery_items ? ` · discovery: <b>${r.discovery_items}</b> предмет(ов), <b>${r.discovery_deals_count || 0}</b> сделок` : "") +
    (r.demo ? " · <b>ДЕМО</b>" : "");

  const hint = $("#coverageHint");
  if (r.coverage_limited) {
    const hours = r.coverage_min_hours;
    hint.classList.remove("hidden");
    hint.textContent =
      `⚠ Даже нескольких страниц истории не хватает на ${r.days} дн. — для части предметов покрытие всего ${hours} ч. ` +
      `Приложение сохраняет историю локально: оставьте его запущенным, и средняя уточнится до полного окна.`;
  } else {
    hint.classList.add("hidden");
  }
}

function renderBonusFilter() {
  const select = $("#dealBonus");
  if (!select) return;
  const current = select.value;
  const bonuses = new Set();
  for (const deal of state.results.deals || []) {
    for (const bonus of deal.bonus_properties || []) bonuses.add(bonus);
  }
  select.innerHTML = `<option value="">Любой бонус</option>` + [...bonuses].sort()
    .map((bonus) => `<option value="${esc(bonus)}">${esc(BONUS_LABELS[bonus] || bonus)}</option>`)
    .join("");
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}

function renderCategoryFilter() {
  const select = $("#dealCategory");
  const previous = select.value;
  const counts = new Map();
  const source = (state.results.deals || []).length ? state.results.deals : state.results.items || [];
  for (const row of source) {
    const top = categoryOf(row.category);
    counts.set(top, (counts.get(top) || 0) + 1);
  }
  const options = [`<option value="">Все категории</option>`];
  for (const [id, count] of Array.from(counts.entries()).sort((a, b) => b[1] - a[1])) {
    options.push(`<option value="${esc(id)}">${esc(categoryLabel(id))} (${count})</option>`);
  }
  select.innerHTML = options.join("");
  if (previous && counts.has(previous)) select.value = previous;
}

function filteredDeals() {
  const query = $("#dealSearch").value.trim().toLowerCase();
  const category = $("#dealCategory").value;
  const minDiscount = Number($("#dealMinDiscount").value || 0);
  const minProfit = Number($("#dealMinProfit").value || 0);
  const minPrice = Number($("#dealMinPrice").value || 0);
  const maxLots = Number($("#dealMaxLots").value || 0);
  const minQlt = $("#dealMinQlt").value;
  const bonus = $("#dealBonus").value;
  const minReliability = $("#dealMinReliability").value;
  const maxTime = Number($("#dealMaxTime").value || 0);
  const onlyLiquid = $("#onlyLiquid").checked;
  const budget = Number($("#dealBudget").value || 0);
  const source = $("#dealSource") ? $("#dealSource").value : "";
  const reliabilityRank = { low: 0, medium: 1, high: 2 };

  let rows = (state.results.deals || []).filter((deal) => {
    if (deal.discount_pct < minDiscount) return false;
    if ((deal.total_net || 0) < minProfit) return false;
    if ((deal.unit_price || 0) < minPrice) return false;
    if (maxLots && (deal.lots_count || 1) > maxLots) return false;
    if (minQlt !== "" && deal.qlt !== Number(minQlt)) return false;
    if (bonus && !(deal.bonus_properties || []).includes(bonus)) return false;
    if (category && categoryOf(deal.category) !== category) return false;
    if (onlyLiquid && (deal.volume || 0) < 3) return false;
    if (minReliability && (reliabilityRank[deal.reliability || "low"] ?? 0) < reliabilityRank[minReliability]) return false;
    if (maxTime && (deal.time_left_min ?? Number.POSITIVE_INFINITY) > maxTime * 60) return false;
    if (budget && (deal.total_cost || 0) > budget) return false;
    if (source && (deal.source || "watch") !== source) return false;
    if (query) {
      const haystack = [deal.name, deal.item_id, deal.qlt_label, deal.ptn_label, deal.category]
        .filter(Boolean).join(" ").toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });

  const sorters = {
    score: (a, b) => (b.score ?? 0) - (a.score ?? 0) || b.total_net - a.total_net,
    discount: (a, b) => b.discount_pct - a.discount_pct || b.total_net - a.total_net,
    profit: (a, b) => b.net_unit - a.net_unit || b.discount_pct - a.discount_pct,
    total: (a, b) => b.total_net - a.total_net,
    time: (a, b) => (a.time_left_min ?? Number.POSITIVE_INFINITY) - (b.time_left_min ?? Number.POSITIVE_INFINITY),
    volume: (a, b) => b.volume - a.volume || b.discount_pct - a.discount_pct,
  };
  rows.sort(sorters[$("#dealSort").value] || sorters.total);
  return rows;
}

function scoreClass(score) {
  if (score == null) return "";
  if (score >= 80) return "great";
  if (score >= 60) return "good";
  if (score >= 40) return "mid";
  return "low";
}

function comparableLabel(basis) {
  if (!basis) return "—";
  if (basis === "overall") return "весь вариант";
  if (basis === "percent:known") return "известный процент";
  if (basis === "percent:unknown") return "процент неизвестен";
  if (basis.startsWith("percent:")) return `процент ${basis.slice(8)}%`;
  if (basis.startsWith("amount-near:")) return `ближайший стак ${basis.slice(12)}`;
  if (basis.startsWith("amount:")) return `стак ${basis.slice(7)}`;
  return basis;
}

function reliabilityHtml(level) {
  const text = { high: "высокая", medium: "средняя", low: "низкая" }[level] || "—";
  return `<span class="reliability ${esc(level || "low")}" title="Надёжность оценки: ${text} (объём продаж)"><i></i><i></i><i></i><span class="rel-text">${text}</span></span>`;
}

function dealRowHtml(deal, index) {
  const tags = [];
  if (deal.qlt_label) tags.push(`<span class="tag qlt-${esc(deal.qlt)}">${esc(deal.qlt_label)}</span>`);
  if (deal.ptn_label) tags.push(`<span class="tag">${esc(deal.ptn_label)}</span>`);
  if (deal.amount > 1 && deal.amount_band_label) {
    tags.push(`<span class="tag" title="Сравнение с продажами стаков этого размера">стак ${esc(deal.amount_band_label)}</span>`);
  }
  if (deal.variant_unknown) {
    tags.push(`<span class="tag warn" title="API не прислал качество/вариант — цена может быть смешана">вариант?</span>`);
  }
  if ((deal.source || "watch") === "discovery") {
    tags.push(`<span class="tag discovery" title="Предмет не входит в список наблюдения">вне списка</span>`);
  }
  for (const flag of deal.restrictions || []) {
    const label = flag === "sell_is_blocked" ? "продажа заблокирована" : "персональный";
    tags.push(`<span class="tag bad" title="Проверьте, можно ли перепродать этот предмет">${esc(label)}</span>`);
  }
  const hot = deal.discount_pct >= 20 ? " hot" : "";
  const market = deal.market_min !== null && deal.market_min !== undefined
    ? `${moneyFine(deal.market_min)}–${moneyFine(deal.market_p25 ?? deal.market_median)}`
    : "—";
  const coverage = deal.coverage || {};
  const coverageText = coverage.records
    ? `${coverage.records} продаж · ${coverage.span_hours} ч${coverage.limited ? " (обрезано API)" : ""}`
    : "нет данных";
  const commission = (deal.commission_pct || 0) / 100;
  const fastPrice = deal.market_min ? deal.market_min * 0.99 : null;
  const marketPrice = deal.market_p25 || deal.market_median || deal.benchmark;
  const fastNet = fastPrice ? fastPrice * (1 - commission) : null;
  const marketNet = marketPrice ? marketPrice * (1 - commission) : null;
  const fastProfit = fastNet !== null ? fastNet - deal.unit_price : null;
  const marketProfit = marketNet !== null ? marketNet - deal.unit_price : null;
  const bonuses = (deal.bonus_properties || []).map((b) => BONUS_LABELS[b] || b);
  return `
    <tr class="deal-row" data-i="${index}">
      <td>
        <div class="item-line">
          <img class="item-icon" loading="lazy" src="/api/icon?item=${encodeURIComponent(deal.item_id)}" alt="" onerror="this.classList.add('missing')">
          <div>
            <div class="item-name">${esc(deal.name)}</div>
            <div class="item-meta">${esc(deal.item_id)} · ${esc(categoryLabel(categoryOf(deal.category)))}</div>
          </div>
        </div>
      </td>
      <td>${tags.join(" ") || '<span class="muted">—</span>'}</td>
      <td class="num price">${moneyFine(deal.unit_price)}</td>
      <td class="num stack-qty">${money(deal.amount)}${deal.lots_count > 1 ? `<small>${deal.lots_count} лот(ов)</small>` : ""}</td>
      <td class="num profit total">+${money(deal.total_net)}</td>
      <td class="num disc${hot}" title="Насколько цена лота ниже базы">${pct(deal.discount_pct)}</td>
      <td class="num"><span class="score ${scoreClass(deal.score)}" title="Рейтинг ${esc(deal.score_label || "")}: прибыль, скидка, ликвидность, риск">${deal.score ?? "—"}</span></td>
      <td class="num profit">+${moneyFine(deal.net_unit)}</td>
      <td class="num">${moneyFine(deal.benchmark)}</td>
      <td class="num market" title="Активные лоты того же варианта: минимум–25-й перцентиль">${market}</td>
      <td class="num" title="Продано ${money(deal.volume)} шт. за ${state.results.days} дн. · ~${deal.sales_per_day}/день">${money(deal.volume)}</td>
      <td class="num">${esc(timeLeft(deal.time_left_min))}</td>
      <td>
        <button type="button" class="btn icon" data-copy="${esc(deal.name)}" title="Скопировать название для поиска в игре">⧉</button>
        ${(deal.source || "watch") === "discovery" ? `<button type="button" class="btn icon" data-watch-add="${esc(deal.item_id)}" title="Добавить в наблюдение">＋</button>` : ""}
      </td>
    </tr>
    <tr class="details-row hidden" data-d="${index}">
      <td colspan="13">
        <div class="details-grid">
          <div class="detail-item">Средняя продаж<b>${moneyFine(deal.mean)}</b></div>
          <div class="detail-item">Медиана продаж<b>${moneyFine(deal.median)}</b></div>
          <div class="detail-item">25-й перцентиль<b>${moneyFine(deal.p25)}</b></div>
          <div class="detail-item">75-й перцентиль<b>${moneyFine(deal.p75)}</b></div>
          <div class="detail-item">Рынок: минимум<b>${moneyFine(deal.market_min)}</b></div>
          <div class="detail-item">Рынок: 25-й перцентиль<b>${moneyFine(deal.market_p25)}</b></div>
          <div class="detail-item">Рынок: медиана<b>${moneyFine(deal.market_median)}</b></div>
          <div class="detail-item">Лотов в продаже<b>${money(deal.market_lots)}</b></div>
          <div class="detail-item">База продаж<b>${moneyFine(deal.sales_base)}</b></div>
          <div class="detail-item">Потолок по лотам<b>${moneyFine(deal.market_cap)}</b></div>
          <div class="detail-item">База расчёта<b>${esc(BENCH_LABELS[deal.benchmark_mode] || deal.benchmark_mode || "")}${deal.capped ? " · ограничена рынком" : ""}</b></div>
          <div class="detail-item">Сравнение<b>${esc(comparableLabel(deal.comparable_basis))}</b></div>
          <div class="detail-item">Сопоставимых продаж<b>${money(deal.volume)} шт. / ${deal.sales_count} лот(ов)</b></div>
          <div class="detail-item">Размер стека<b>${esc(deal.amount_band_label || (deal.amount > 1 ? `${deal.amount} шт.` : "1 шт."))}</b></div>
          ${deal.amount_fallback ? `<div class="detail-item">Точность<b class="bad">сравнение с ближайшим диапазоном стака</b></div>` : ""}
          ${deal.variant_unknown ? `<div class="detail-item">Вариант<b class="bad">не определён API</b></div>` : ""}
          ${(deal.restrictions || []).length ? `<div class="detail-item">Ограничения<b class="bad">${esc((deal.restrictions || []).join(", "))}</b></div>` : ""}
          <div class="detail-item">Отброшено (вне рынка ×10)<b>${money(deal.sales_filtered || 0)}</b></div>
          <div class="detail-item">Темп продаж<b>~${deal.sales_per_day} шт./день</b></div>
          <div class="detail-item">Хватит на<b>${deal.sales_per_day ? `~${Math.ceil((deal.amount || 0) / deal.sales_per_day)} дн. продаж` : "—"}</b></div>
          <div class="detail-item">Совет: быстро<b class="${fastProfit > 0 ? "good" : "bad"}">${fastPrice !== null ? `${moneyFine(fastPrice)} · ${fastProfit > 0 ? "+" : ""}${moneyFine(fastProfit)}/шт` : "—"}</b></div>
          <div class="detail-item">Совет: по рынку<b class="${marketProfit > 0 ? "good" : "bad"}">${marketPrice ? `${moneyFine(marketPrice)} · ${marketProfit > 0 ? "+" : ""}${moneyFine(marketProfit)}/шт` : "—"}</b></div>
          ${bonuses.length ? `<div class="detail-item">Бонусы<b>${esc(bonuses.join(", "))}</b></div>` : ""}
          <div class="detail-item">График<b><button type="button" class="btn small" data-analytics="${esc(`${deal.item_id}|${deal.qlt ?? ""}|${deal.ptn ?? ""}`)}">Открыть</button></b></div>
          <div class="detail-item">Портфель<b><button type="button" class="btn small primary" data-buy="${index}">Купил</button></b></div>
          <div class="detail-item">Валовая прибыль<b>+${moneyFine(deal.gross_unit)} / шт.</b></div>
          <div class="detail-item">Лотов в возможности<b>${money(deal.lots_count || 1)}</b></div>
          <div class="detail-item">Затраты всего<b>${money(deal.total_cost)}</b></div>
          <div class="detail-item">Надёжность<b>${reliabilityHtml(deal.reliability)}</b></div>
          <div class="detail-item">История<b>${esc(coverageText)}</b></div>
          <div class="detail-item">Конец лота<b>${esc(dateTime(deal.end_time))}</b></div>
          <div class="detail-item">Стартовая цена<b>${money(deal.start_price)}</b></div>
          <div class="detail-json">additional: ${esc(JSON.stringify(deal.additional || {}, null, 0))}</div>
        </div>
      </td>
    </tr>`;
}

function renderDeals() {
  const rows = filteredDeals();
  state.visibleDeals = rows;
  const budget = Number($("#dealBudget").value || 0);
  const budgetHint = $("#budgetHint");
  if (budgetHint) {
    if (budget > 0) {
      const invested = rows.reduce((sum, deal) => sum + (deal.total_cost || 0), 0);
      const profit = rows.reduce((sum, deal) => sum + (deal.total_net || 0), 0);
      budgetHint.textContent = rows.length
        ? `Под бюджет ${money(budget)}: ${rows.length} сдел(ок) · вложить ${money(invested)} · прибыль ${money(profit)}`
        : `Под бюджет ${money(budget)} ничего не подходит.`;
    } else {
      budgetHint.textContent = "";
    }
  }
  const body = $("#dealsBody");
  const table = body.closest(".table-wrap");
  const empty = $("#dealsEmpty");
  body.innerHTML = rows.map(dealRowHtml).join("");
  const hasAny = rows.length > 0;
  table.querySelector(".deals-table").classList.toggle("hidden", !hasAny);
  empty.classList.toggle("hidden", hasAny);
  if (!hasAny) {
    const total = (state.results.deals || []).length;
    $("#dealsEmptyText").textContent = total
      ? "Под текущие фильтры ничего не подходит. Ослабьте условия."
      : state.results.generated_at
        ? "Сделок по порогу и минимальной прибыли не найдено. Снизьте порог или мин. прибыль в настройках, либо добавьте предметы."
        : "Добавьте предметы во вкладке «Наблюдение» и нажмите «Сканировать».";
  }
}

function renderNoDeals() {
  const items = (state.results.items || []).filter((item) => !(item.deals_count || 0));
  $("#noDealsCount").textContent = String(items.length);
  $("#noDealsBlock").classList.toggle("hidden", items.length === 0);
  const body = $("#noDealsBody");
  if (!items.length) { body.innerHTML = ""; return; }
  items.sort((a, b) => (a.status === "error" ? 1 : 0) - (b.status === "error" ? 1 : 0) || a.name.localeCompare(b.name, "ru"));
  body.innerHTML = items.map((item) => {
    const volume = item.volume || 0;
    const best = item.best_discount_pct ?? -999;
    const minLot = item.min_lot_unit ?? null;
    const statusText = item.status === "error" ? (item.error || "ошибка") : (STATUS_LABELS[item.status] || item.status);
    return `
      <tr>
        <td><div class="item-line"><img class="item-icon" loading="lazy" src="/api/icon?item=${encodeURIComponent(item.id)}" alt="" onerror="this.classList.add('missing')"><div><div class="item-name">${esc(item.name)}</div><div class="item-meta">${esc(item.id)} · ${esc(categoryLabel(categoryOf(item.category)))}</div></div></div></td>
        <td><span class="status-label status-${esc(item.status)}">${esc(statusText)}</span></td>
        <td class="num">${money(item.lots_total)}</td>
        <td class="num">${money(volume)}</td>
        <td class="num">${minLot === null ? "—" : money(minLot)}</td>
        <td class="num">${best > -999 ? `−${pct(best)}` : "—"}</td>
      </tr>`;
  }).join("");
}

function bindDealsControls() {
  for (const id of ["dealSearch", "dealCategory", "dealMinDiscount", "dealMinProfit", "dealMinPrice", "dealMaxLots", "dealMinQlt", "dealMinPercent", "dealBonus", "dealMinReliability", "dealMaxTime", "dealBudget", "dealSource", "dealSort", "onlyLiquid"]) {
    const element = document.getElementById(id);
    if (element) element.addEventListener(id === "dealSearch" || id === "dealBudget" ? "input" : "change", renderDeals);
  }
  $("#dealsBody").addEventListener("click", async (event) => {
    const chartButton = event.target.closest("[data-analytics]");
    if (chartButton) {
      event.stopPropagation();
      openAnalyticsFromToken(chartButton.getAttribute("data-analytics"));
      return;
    }
    const buyButton = event.target.closest("[data-buy]");
    if (buyButton) {
      event.stopPropagation();
      await buyDeal(Number(buyButton.getAttribute("data-buy")));
      return;
    }
    const copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      event.stopPropagation();
      await copyToClipboard(copyButton.getAttribute("data-copy"));
      return;
    }
    const addButton = event.target.closest("[data-watch-add]");
    if (addButton) {
      event.stopPropagation();
      await watchAction({ add: [addButton.getAttribute("data-watch-add")] });
      return;
    }
    const row = event.target.closest("tr.deal-row");
    if (!row) return;
    const details = document.querySelector(`tr.details-row[data-d="${row.dataset.i}"]`);
    if (details) details.classList.toggle("hidden");
  });
}

// ------------------------------------------------------------------ стаки

function stackRowHtml(stack, index) {
  const dirClass = stack.direction === "bulk_to_singles" ? "bulk_to_singles" : "singles_to_bulk";
  const sm = stack.singles_market || {};
  const bm = stack.bulk_market || {};
  const coverage = stack.coverage || {};
  const coverageText = coverage.records
    ? `${coverage.records} продаж · ${coverage.span_hours} ч${coverage.limited ? " (обрезано API)" : ""}`
    : "нет данных";
  const qtyNote = stack.qty !== stack.qty_target ? `<small>цель ${money(stack.qty_target)}</small>` : "";
  const singleLot = bm.single_lot
    ? `${money(bm.single_lot.unit)} / шт · ${money(bm.single_lot.amount)} шт.`
    : "нет лота нужного объёма";
  return `
    <tr class="deal-row stack-row" data-si="${index}">
      <td>
        <div class="item-name">${esc(stack.name)}</div>
        <div class="item-meta">${esc(stack.item_id)} · ${esc(categoryLabel(categoryOf(stack.category)))}</div>
      </td>
      <td>
        <span class="direction">
          <b class="dir-${dirClass}">${esc(stack.label)}</b>
          <span class="dir-sub">${esc(stack.note || "")}</span>
        </span>
      </td>
      <td class="num stack-qty">${money(stack.qty)}${qtyNote}</td>
      <td class="num stack-price">${money(stack.buy_cost)}<small>${money(stack.buy_unit)} / шт · ${money(stack.buy_lots_used)} лот(ов)</small></td>
      <td class="num stack-price">${money(stack.sell_net)}<small>${money(stack.sell_net_unit)} / шт после ком. · ${money(stack.sell_lots)} лот(ов)</small></td>
      <td class="num profit total">+${money(stack.profit)}</td>
      <td class="num disc hot">+${pct(stack.margin_pct)}</td>
      <td class="num">${money(sm.available)}<small>от ${money(sm.min)}</small></td>
      <td class="num">${money(bm.available)}<small>от ${money(bm.min)}</small></td>
      <td class="num">${money(sm.sales_count)} / ${money(bm.sales_count)}<small>${money(sm.sales_units)} / ${money(bm.sales_units)} шт.</small></td>
      <td><button type="button" class="btn icon" data-scopy="${esc(stack.name)}" title="Скопировать название">⧉</button></td>
    </tr>
    <tr class="details-row hidden" data-sd="${index}">
      <td colspan="11">
        <div class="details-grid">
          <div class="detail-item">Купить всего<b>${money(stack.buy_cost)}</b></div>
          <div class="detail-item">Купить за шт.<b>${money(stack.buy_unit)}</b></div>
          <div class="detail-item">Лотов куплено<b>${money(stack.buy_lots_used)}${stack.buy_single_lot ? " (один лот)" : " (сборный)"}</b></div>
          <div class="detail-item">Продать всего<b>${money(stack.sell_revenue)}</b></div>
          <div class="detail-item">Продать после ком.<b>${money(stack.sell_net)}</b></div>
          <div class="detail-item">Продать за шт.<b>${money(stack.sell_net_unit)}</b></div>
          <div class="detail-item">Лотов на продажу<b>${money(stack.sell_lots)}</b></div>
          <div class="detail-item">Надёжность<b>${reliabilityHtml(stack.reliability)}</b></div>
          <div class="detail-item">Одиночные: доступно<b>${money(sm.available)} шт.</b></div>
          <div class="detail-item">Одиночные: мин / медиана<b>${money(sm.min)} / ${money(sm.median)}</b></div>
          <div class="detail-item">Одиночные: продаж<b>${money(sm.sales_count)} / ${money(sm.sales_units)} шт. · мед. ${money(sm.sales_median)}</b></div>
          <div class="detail-item">Стаки: доступно<b>${money(bm.available)} шт. в ${money(bm.lots)} лот(ах)</b></div>
          <div class="detail-item">Стаки: мин / медиана<b>${money(bm.min)} / ${money(bm.median)}</b></div>
          <div class="detail-item">Стаки: один лот<b>${esc(singleLot)}</b></div>
          <div class="detail-item">Стаки: продаж<b>${money(bm.sales_count)} / ${money(bm.sales_units)} шт. · мед. ${money(bm.sales_median)}</b></div>
          <div class="detail-item">История<b>${esc(coverageText)}</b></div>
          <div class="detail-item">Порог / комиссия<b>${state.results.stack_threshold_pct ?? "—"}% / ${state.results.commission_pct ?? "—"}%</b></div>
          <div class="detail-json">additional: ${esc(JSON.stringify(stack.additional || {}, null, 0))}</div>
        </div>
      </td>
    </tr>`;
}

function filteredStacks() {
  const query = $("#stackSearch").value.trim().toLowerCase();
  const direction = $("#stackDirection").value;
  const minMargin = Number($("#stackMinMargin").value || 0);
  let rows = (state.results.stacks || []).filter((stack) => {
    if (direction && stack.direction !== direction) return false;
    if (stack.margin_pct < minMargin) return false;
    if (query) {
      const haystack = [stack.name, stack.item_id, stack.label, stack.qlt_label, stack.ptn_label]
        .filter(Boolean).join(" ").toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
  const sorters = {
    margin: (a, b) => b.margin_pct - a.margin_pct || b.profit - a.profit,
    profit: (a, b) => b.profit - a.profit,
    qty: (a, b) => b.qty - a.qty || b.margin_pct - a.margin_pct,
  };
  rows.sort(sorters[$("#stackSort").value] || sorters.margin);
  return rows;
}

function scanEstimate(cfg) {
  const count = (cfg.watchlist || []).length;
  const lotsPages = Math.max(1, Number(cfg.lots_pages) || 1);
  const interval = Math.max(1, Number(cfg.scan_interval_min) || 5);
  const ttl = Math.max(1, Number(cfg.history_ttl_min) || 30);
  const historyShare = Math.min(1, interval / ttl);
  const avg = Math.ceil(count * lotsPages + count * historyShare);
  const first = Math.ceil(count * lotsPages + count);
  const rate = 3.1; // ~185 запросов/мин, параллельно
  return {
    count,
    avg,
    first,
    avgSeconds: Math.ceil(avg / rate),
    firstSeconds: Math.ceil(first / rate),
    ttl,
  };
}

function updateStackScope() {
  const cfg = state.config;
  const host = $("#stackScopeHint");
  if (!host || !cfg) return;
  const estimate = scanEstimate(cfg);
  host.textContent = estimate.count
    ? `В наблюдении ${estimate.count} предметов · ≈ ${estimate.avg} запросов (первый скан — до ${estimate.first}) · ~${estimate.avgSeconds}–${estimate.firstSeconds} с. Стаки считаются по тем же данным, без дополнительных запросов.`
    : "Список наблюдения пуст.";
}

function renderStacks() {
  const r = state.results;
  const stacks = r.stacks || [];
  $("#statStacks").textContent = String(r.stacks_count ?? stacks.length ?? 0);
  $("#statStacksProfit").textContent = stacks.length ? money(r.stacks_potential_profit) : "—";
  $("#statStackQty").textContent = String(r.stack_qty ?? (state.config ? state.config.stack_qty : 100));
  $("#statStackThreshold").textContent = `${r.stack_threshold_pct ?? (state.config ? state.config.stack_threshold_pct : 10)}%`;
  updateStackScope();

  const rows = filteredStacks();
  state.visibleStacks = rows;
  $("#stacksBody").innerHTML = rows.map(stackRowHtml).join("");
  const hasAny = rows.length > 0;
  $("#stacksBody").closest("table").classList.toggle("hidden", !hasAny);
  $("#stacksEmpty").classList.toggle("hidden", hasAny);
  if (!hasAny) {
    $("#stacksEmptyText").textContent = stacks.length
      ? "Под текущие фильтры ничего не подходит. Ослабьте условия."
      : r.generated_at
        ? "Возможностей с текущим порогом нет. Добавьте расходники/ресурсы или снизьте порог в настройках."
        : "Добавьте расходники или ресурсы и запустите скан. Артефакты по 1 шт. здесь не участвуют.";
  }

  const hint = $("#stackCoverageHint");
  if (r.coverage_limited) {
    hint.classList.remove("hidden");
    hint.textContent =
      `⚠ История по части предметов короче окна (минимум ${r.coverage_min_hours} ч). ` +
      `Маржа стаков использует накопленные продажи и уточнится после нескольких сканов.`;
  } else {
    hint.classList.add("hidden");
  }
}

function bindStacks() {
  for (const id of ["stackSearch", "stackDirection", "stackMinMargin", "stackSort"]) {
    const element = document.getElementById(id);
    element.addEventListener(id === "stackSearch" ? "input" : "change", renderStacks);
  }
  $("#stacksBody").addEventListener("click", async (event) => {
    const copyButton = event.target.closest("[data-scopy]");
    if (copyButton) {
      event.stopPropagation();
      await copyToClipboard(copyButton.getAttribute("data-scopy"));
      return;
    }
    const row = event.target.closest("tr.stack-row");
    if (!row) return;
    const details = document.querySelector(`tr.details-row[data-sd="${row.dataset.si}"]`);
    if (details) details.classList.toggle("hidden");
  });
  $("#stackScanBtn").addEventListener("click", () => startScan());
  $$("[data-stack-cat]").forEach((button) => {
    button.addEventListener("click", async () => {
      const category = button.getAttribute("data-stack-cat");
      const limit = Number(button.getAttribute("data-limit") || 100);
      const label = categoryLabel(category);
      if (!confirm(`Добавить до ${limit} предметов «${label}» в наблюдение?\n\nСкан такого списка займёт несколько минут из-за лимита API.`)) return;
      await watchAction({ add_category: category, limit });
      renderStacks();
    });
  });
}

// ------------------------------------------------------------------ аналитика

function variantKey(qlt, ptn) {
  return `${qlt === null || qlt === undefined ? "null" : qlt}:${ptn === null || ptn === undefined ? "null" : ptn}`;
}

function variantLabel(qlt, ptn, category, units, count) {
  const quality = qlt === null || qlt === undefined ? "Без качества" : (QUALITY_NAMES[qlt] || `Качество ${qlt}`);
  let pattern = "";
  if (ptn !== null && ptn !== undefined) {
    pattern = String(category || "").startsWith("artefact") ? ` · паттерн ${ptn}` : ` · +${ptn}`;
  } else if (String(category || "").startsWith("artefact")) {
    pattern = " · без паттерна";
  }
  const suffix = units !== undefined ? ` · ${money(units)} шт. / ${count} прод.` : "";
  return `${quality}${pattern}${suffix}`;
}

function adviceCard(label, price, net, note) {
  const bad = !price;
  return `<div class="advice-card${bad ? " bad" : ""}">
    <div class="advice-label">${esc(label)}</div>
    <div class="price">${bad ? "—" : moneyFine(price)}</div>
    <div class="sub">${bad ? esc(note) : `после комиссии ${moneyFine(net)} · ${esc(note)}`}</div>
  </div>`;
}

function renderAnalyticsChart(data) {
  const host = $("#analyticsChart");
  if (!host) return;
  const series = data.series || [];
  if (!series.length) {
    host.innerHTML = `<div class="empty">Нет накопленных продаж за ${money(data.days)} дн.</div>`;
    return;
  }
  const width = Math.max(560, host.clientWidth || 760);
  const height = 280;
  const m = { l: 58, r: 16, t: 16, b: 34 };
  const w = width - m.l - m.r;
  const h = height - m.t - m.b;
  const values = series.flatMap((p) => [p.p25, p.p75, p.median]).filter((v) => Number.isFinite(v));
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
    host.innerHTML = `<div class="empty">Недостаточно данных для графика.</div>`;
    return;
  }
  if (hi === lo) {
    const pad = Math.abs(hi) * 0.05 || 1;
    lo -= pad;
    hi += pad;
  }
  const pad = (hi - lo) * 0.08;
  lo -= pad;
  hi += pad;
  const n = series.length;
  const X = (i) => m.l + (n === 1 ? w / 2 : (w * i) / (n - 1));
  const Y = (v) => m.t + h - ((v - lo) / (hi - lo)) * h;
  const maxUnits = Math.max(...series.map((p) => p.units), 1);
  const barW = Math.min(16, Math.max(2, (w / Math.max(1, n)) * 0.45));
  const bars = series.map((p, i) => {
    const bh = (p.units / maxUnits) * h * 0.18;
    return `<rect x="${X(i) - barW / 2}" y="${m.t + h - bh}" width="${barW}" height="${bh}" fill="rgba(168,224,95,.10)"><title>${esc(p.date)}: ${money(p.units)} шт.</title></rect>`;
  }).join("");
  const top = series.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.p75).toFixed(1)}`).join(" ");
  const bottom = series.slice().reverse().map((p) => {
    const i = series.indexOf(p);
    return `${X(i).toFixed(1)},${Y(p.p25).toFixed(1)}`;
  }).join(" L");
  const band = `<path d="${top} L${bottom} Z" fill="rgba(168,224,95,.12)" stroke="none"/>`;
  const line = series.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.median).toFixed(1)}`).join(" ");
  const dots = series.map((p, i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.median).toFixed(1)}" r="2.5" fill="#a8e05f"><title>${esc(p.date)}: медиана ${money(p.median)} · ${money(p.p25)}–${money(p.p75)} · ${money(p.units)} шт.</title></circle>`).join("");
  const grid = [0, 1, 2, 3, 4].map((k) => {
    const value = lo + ((hi - lo) * k) / 4;
    const y = Y(value);
    return `<line x1="${m.l}" x2="${m.l + w}" y1="${y}" y2="${y}" stroke="#1b2620"/><text x="${m.l - 8}" y="${y + 4}" text-anchor="end" fill="#64786b" font-size="10" font-family="monospace">${money(value)}</text>`;
  }).join("");
  const tickIndexes = [0, Math.floor((n - 1) / 3), Math.floor(((n - 1) * 2) / 3), n - 1].filter((v, i, a) => a.indexOf(v) === i);
  const xLabels = tickIndexes.map((i) => `<text x="${X(i).toFixed(1)}" y="${height - 12}" text-anchor="middle" fill="#64786b" font-size="10" font-family="monospace">${esc(series[i].date.slice(5))}</text>`).join("");
  host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="История цены"><title>История цены: медиана и перцентили</title>${grid}${bars}${band}<path d="${line}" fill="none" stroke="#a8e05f" stroke-width="2"/>${dots}${xLabels}</svg>`;
}

function renderAnalyticsMarket(data) {
  const host = $("#analyticsMarket");
  if (!host) return;
  const market = data.market;
  const stats = data.stats || {};
  const rec = data.recommendation;
  const error = data.history_error ? `<p class="hint warn">${esc(data.history_error)}</p>` : "";
  if (!market) {
    host.innerHTML = `${error}<p class="hint">Нет активных лотов для выбранного варианта. Возможно, предмет не попал в последний скан.</p>
      <div class="analytics-stats">
        <div class="analytics-stat"><span>Средняя продаж</span><b>${moneyFine(stats.median)}</b></div>
        <div class="analytics-stat"><span>Объём в окне</span><b>${money(stats.volume)}</b></div>
      </div>`;
    return;
  }
  const stat = (label, value) => `<div class="analytics-stat"><span>${esc(label)}</span><b>${value}</b></div>`;
  host.innerHTML = `${error}<div class="analytics-cards">
    ${adviceCard("Быстрая продажа", rec ? rec.fast : null, rec ? rec.fast_net : null, "подрезать минимум на 1%")}
    ${adviceCard("По рынку", rec ? rec.market : null, rec ? rec.market_net : null, "25-й перцентиль конкурентов")}
    <div class="analytics-stats">
      ${stat("Рынок (мин–p25)", `${moneyFine(market.min)}–${moneyFine(market.p25 ?? market.median)}`)}
      ${stat("Лотов", money(market.count))}
      ${stat("Средняя продаж", moneyFine(stats.median))}
      ${stat("Продаж/день", rec && rec.sales_per_day ? moneyFine(rec.sales_per_day) : "—")}
      ${stat("Объём в окне", money(stats.volume))}
      ${stat("Хватит на 1 шт.", rec && rec.days_to_sell_one ? `~${rec.days_to_sell_one} дн.` : "—")}
    </div>
  </div>`;
}

function renderAnalyticsDepth(data) {
  const host = $("#analyticsDepth");
  if (!host) return;
  const depth = data.depth || [];
  const info = $("#analyticsDepthInfo");
  if (!depth.length) {
    host.innerHTML = `<p class="hint">Нет активных лотов для выбранного варианта.</p>`;
    if (info) info.textContent = "";
    return;
  }
  const maxUnits = Math.max(...depth.map((row) => row.units), 1);
  const totalUnits = depth.reduce((sum, row) => sum + row.units, 0);
  if (info) info.textContent = `${depth.length} уровней · ${money(totalUnits)} шт.`;
  host.innerHTML = `<div class="depth-list">${depth.slice(0, 15).map((row) => `
    <div class="depth-row">
      <span class="depth-price">${moneyFine(row.unit)}</span>
      <span class="depth-bar"><i style="width:${Math.max(2, (row.units / maxUnits) * 100).toFixed(1)}%"></i></span>
      <span class="depth-units">${money(row.units)} шт.</span>
      <span class="depth-lots">${money(row.lots)} лот.</span>
    </div>`).join("")}</div>`;
}

function renderAnalyticsRecent(data) {
  const body = $("#analyticsRecent");
  if (!body) return;
  const rows = data.recent || [];
  $("#analyticsRecentCount").textContent = rows.length ? `${rows.length} последних` : "";
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted">Нет продаж за период.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((row) => {
    const quality = row.qlt === null ? "—" : (QUALITY_NAMES[row.qlt] || row.qlt);
    let pattern = "";
    if (row.ptn !== null) {
      pattern = String(data.category || "").startsWith("artefact") ? ` · паттерн ${row.ptn}` : ` · +${row.ptn}`;
    }
    return `<tr><td>${esc(dateTime(row.time))}</td><td>${esc(quality + pattern)}</td><td class="num">${moneyFine(row.unit)}</td><td class="num">${money(row.amount)}</td><td class="num">${money(row.price)}</td></tr>`;
  }).join("");
}

function renderAnalyticsVariants(data) {
  const select = $("#analyticsVariant");
  if (!select) return;
  const variants = data.variants || [];
  if (!variants.length) {
    select.innerHTML = `<option value="">нет истории</option>`;
    return;
  }
  select.innerHTML = variants.map((variant) => `<option value="${variantKey(variant.qlt, variant.ptn)}">${esc(variantLabel(variant.qlt, variant.ptn, data.category, variant.units, variant.count))}</option>`).join("");
  const selected = variantKey(data.selected ? data.selected.qlt : null, data.selected ? data.selected.ptn : null);
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
}

async function renderAnalyticsItems() {
  const select = $("#analyticsItem");
  if (!select) return;
  const watch = (state.config && state.config.watchlist) || [];
  if (!watch.length) {
    select.innerHTML = `<option value="">Нет предметов</option>`;
    return;
  }
  await resolveWatchMeta();
  const current = select.value || state.analytics.item;
  select.innerHTML = watch.map((id) => {
    const meta = state.itemMeta[id];
    const name = meta ? (meta.name.ru || meta.name.en || id) : id;
    return `<option value="${esc(id)}">${esc(name)} (${esc(id)})</option>`;
  }).join("");
  const chosen = watch.includes(current) ? current : watch[0];
  state.analytics.item = chosen;
  select.value = chosen;
}

async function loadAnalytics() {
  const item = $("#analyticsItem") ? $("#analyticsItem").value : "";
  if (!item) return;
  state.analytics.item = item;
  state.analytics.days = Number($("#analyticsDays").value || 7);
  const variant = state.analytics.variant || ($("#analyticsVariant") ? $("#analyticsVariant").value : "");
  let url = `/api/analytics?item=${encodeURIComponent(item)}&days=${state.analytics.days}`;
  if (variant) url += `&variant=${encodeURIComponent(variant)}`;
  try {
    const data = await api(url);
    state.analytics.data = data;
    if (data.selected) state.analytics.variant = variantKey(data.selected.qlt, data.selected.ptn);
    const range = $("#analyticsRange");
    if (range) range.textContent = `${data.name} · ${data.days} дн. · ${(data.series || []).length} дн. с продажами`;
    renderAnalyticsVariants(data);
    renderAnalyticsChart(data);
    renderAnalyticsMarket(data);
    renderAnalyticsDepth(data);
    renderAnalyticsRecent(data);
  } catch (error) {
    const chart = $("#analyticsChart");
    if (chart) chart.innerHTML = `<div class="empty">Не удалось загрузить: ${esc(error.message)}</div>`;
    const market = $("#analyticsMarket");
    if (market) market.innerHTML = "";
    const recent = $("#analyticsRecent");
    if (recent) recent.innerHTML = "";
  }
}

async function ensureAnalytics() {
  if (!$("#analyticsItem") || !$("#analyticsItem").options.length) await renderAnalyticsItems();
  if (!state.analytics.item) {
    const watch = (state.config && state.config.watchlist) || [];
    const deal = (state.results.deals || [])[0];
    state.analytics.item = watch[0] || (deal ? deal.item_id : null);
  }
  if (!state.analytics.item) return;
  const select = $("#analyticsItem");
  if (select && [...select.options].some((option) => option.value === state.analytics.item)) select.value = state.analytics.item;
  if (!state.analytics.data || state.analytics.data.item !== state.analytics.item) await loadAnalytics();
}

function openAnalyticsFromToken(token) {
  const parts = String(token || "").split("|");
  const qlt = parts[1] === "" || parts[1] === undefined ? null : Number(parts[1]);
  const ptn = parts[2] === "" || parts[2] === undefined ? null : Number(parts[2]);
  openAnalytics(parts[0], qlt, ptn);
}

async function openAnalytics(item, qlt, ptn) {
  switchTab("analytics");
  await renderAnalyticsItems();
  const select = $("#analyticsItem");
  if (select && item) {
    if (![...select.options].some((option) => option.value === item)) select.add(new Option(item, item));
    select.value = item;
    state.analytics.item = item;
  }
  state.analytics.variant = variantKey(qlt, ptn);
  await loadAnalytics();
}

// ------------------------------------------------------------------ движение

const MOVEMENT_LABELS = {
  appeared: "Появился",
  price: "Цена изменилась",
  probable_sold: "Вероятно продан",
  expired: "Истёк",
  missing: "Пропал",
  reappeared: "Снова появился",
};

async function renderMovementItems() {
  const select = $("#movementItem");
  if (!select) return;
  const watch = (state.config && state.config.watchlist) || [];
  if (!watch.length) {
    select.innerHTML = `<option value="">Нет предметов</option>`;
    return;
  }
  await resolveWatchMeta();
  const current = select.value || state.movement.item;
  select.innerHTML = watch.map((id) => {
    const meta = state.itemMeta[id];
    const name = meta ? (meta.name.ru || meta.name.en || id) : id;
    return `<option value="${esc(id)}">${esc(name)} (${esc(id)})</option>`;
  }).join("");
  const chosen = watch.includes(current) ? current : watch[0];
  state.movement.item = chosen;
  select.value = chosen;
}

function movementChartHtml(data) {
  const series = data.series || [];
  if (!series.length) return `<div class="empty">Событий за период нет.</div>`;
  const width = Math.max(520, 720);
  const height = 250;
  const m = { l: 48, r: 16, t: 16, b: 34 };
  const w = width - m.l - m.r;
  const h = height - m.t - m.b;
  const keys = ["appeared", "probable_sold", "expired", "missing"];
  const colors = { appeared: "#a8e05f", probable_sold: "#6fb6d8", expired: "#e8a94a", missing: "#e2675a" };
  const maxValue = Math.max(1, ...series.flatMap((p) => keys.map((k) => p[k] || 0)));
  const n = series.length;
  const groupW = w / Math.max(1, n);
  const barW = Math.max(2, Math.min(10, groupW / 6));
  const bars = series.map((p, i) => {
    const x0 = m.l + i * groupW + groupW / 2 - barW * 2;
    return keys.map((key, k) => {
      const value = p[key] || 0;
      const bh = (value / maxValue) * h;
      return `<rect x="${(x0 + k * barW).toFixed(1)}" y="${(m.t + h - bh).toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" fill="${colors[key]}"><title>${esc(p.date)} · ${MOVEMENT_LABELS[key]}: ${value}</title></rect>`;
    }).join("");
  }).join("");
  const grid = [0, 1, 2, 3, 4].map((k) => {
    const value = (maxValue * k) / 4;
    const y = m.t + h - (value / maxValue) * h;
    return `<line x1="${m.l}" x2="${m.l + w}" y1="${y}" y2="${y}" stroke="#1b2620"/><text x="${m.l - 8}" y="${y + 4}" text-anchor="end" fill="#64786b" font-size="10" font-family="monospace">${Math.round(value)}</text>`;
  }).join("");
  const ticks = [0, Math.floor((n - 1) / 3), Math.floor(((n - 1) * 2) / 3), n - 1].filter((v, i, a) => a.indexOf(v) === i);
  const labels = ticks.map((i) => `<text x="${(m.l + i * groupW + groupW / 2).toFixed(1)}" y="${height - 12}" text-anchor="middle" fill="#64786b" font-size="10" font-family="monospace">${esc(String(series[i].date).slice(5))}</text>`).join("");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="События лотов"><title>События лотов по дням</title>${grid}${bars}${labels}</svg>`;
}

function renderMovement(data) {
  const summary = data || {};
  const events = summary.events || {};
  const sold = events.probable_sold || 0;
  $("#mvLots").textContent = money(summary.supply_lots || 0);
  $("#mvUnits").textContent = `${money(summary.supply_units || 0)} шт. в продаже`;
  $("#mvMedian").textContent = moneyFine(summary.median_ask);
  $("#mvSold").textContent = money(sold);
  $("#mvSoldSub").textContent = summary.probable_sold_pct != null
    ? `${summary.probable_sold_pct}% от исчезнувших`
    : "пока нет исчезновений";
  $("#mvLifetime").textContent = summary.avg_lifetime_hours != null ? `${summary.avg_lifetime_hours} ч` : "—";
  const range = $("#movementRange");
  if (range) range.textContent = `${summary.name || summary.item || ""} · ${summary.days || 0} дн.`;
  $("#movementChart").innerHTML = movementChartHtml(summary);
  const stat = (label, value) => `<div class="analytics-stat"><span>${esc(label)}</span><b>${value}</b></div>`;
  $("#movementStats").innerHTML = `<div class="analytics-stats">
    ${stat("Появилось", money(events.appeared || 0))}
    ${stat("Сменили цену", money(events.price || 0))}
    ${stat("Вероятно продано", money(sold))}
    ${stat("Истекло", money(events.expired || 0))}
    ${stat("Пропало", money(events.missing || 0))}
    ${stat("Закрыто наблюдений", money(summary.closed || 0))}
    ${stat("Исчезло всего", money(summary.disappeared || 0))}
    ${stat("Доля вероятных продаж", summary.probable_sold_pct != null ? `${summary.probable_sold_pct}%` : "—")}
  </div>`;
  const recent = summary.recent || [];
  $("#movementEventsCount").textContent = recent.length ? `${recent.length} событий` : "";
  $("#movementEmpty").classList.toggle("hidden", recent.length > 0 || (summary.supply_lots || 0) > 0);
  $("#movementEvents").innerHTML = recent.map((event) => {
    const detail = event.detail || {};
    let text = "";
    if (event.event === "price") text = `${moneyFine(detail.old)} → ${moneyFine(detail.new)}`;
    else if (event.event === "probable_sold") text = `продажа ${moneyFine(detail.sale_price)} · ${dateTime(detail.sale_time)}`;
    else if (event.event === "expired") text = `endTime ${dateTime(detail.end_time)}`;
    const variant = `${event.qlt != null ? QUALITY_NAMES[event.qlt] || `q${event.qlt}` : "—"}${event.ptn != null ? ` · +${event.ptn}` : ""}`;
    return `<tr><td>${esc(dateTime(event.at))}</td><td>${esc(MOVEMENT_LABELS[event.event] || event.event)}</td><td class="num">${money(event.amount)}</td><td class="num">${moneyFine(event.unit)}</td><td>${esc(variant)}</td><td class="muted">${esc(text)}</td></tr>`;
  }).join("");
}

async function loadMovement() {
  const item = $("#movementItem") ? $("#movementItem").value : "";
  if (!item) return;
  state.movement.item = item;
  state.movement.days = Number($("#movementDays").value || 7);
  try {
    const data = await api(`/api/movement?item=${encodeURIComponent(item)}&days=${state.movement.days}`);
    state.movement.data = data;
    renderMovement(data);
  } catch (error) {
    $("#movementChart").innerHTML = `<div class="empty">Не удалось загрузить: ${esc(error.message)}</div>`;
    $("#movementStats").innerHTML = "";
    $("#movementEvents").innerHTML = "";
  }
}

async function ensureMovement() {
  if (!$("#movementItem") || !$("#movementItem").options.length) await renderMovementItems();
  if (!state.movement.item) state.movement.item = ((state.config && state.config.watchlist) || [])[0] || null;
  if (!state.movement.item) return;
  const select = $("#movementItem");
  if (select && [...select.options].some((option) => option.value === state.movement.item)) select.value = state.movement.item;
  await loadMovement();
}

function bindMovement() {
  const itemSelect = $("#movementItem");
  const daysSelect = $("#movementDays");
  if (!itemSelect || !daysSelect) return;
  itemSelect.addEventListener("change", loadMovement);
  daysSelect.addEventListener("change", loadMovement);
  $("#movementRefresh").addEventListener("click", loadMovement);
}

// ------------------------------------------------------------------ бартер

async function renderBarterItems() {
  const select = $("#barterItem");
  if (!select) return;
  try {
    const data = await api("/api/barter/items");
    state.barter.ready = !!data.ready;
    state.barter.error = data.error || null;
    const items = (data.items || []).slice().sort((a, b) => a.name.localeCompare(b.name, "ru"));
    if (!items.length) {
      select.innerHTML = `<option value="">${esc(data.error || "Нет рецептов")}</option>`;
      state.barter.items = [];
      return;
    }
    const current = select.value || state.barter.item;
    select.innerHTML = items.map((item) => `<option value="${esc(item.id)}">${esc(item.name)} (${esc(item.id)})</option>`).join("");
    const chosen = items.some((item) => item.id === current) ? current : items[0].id;
    state.barter.item = chosen;
    state.barter.items = items;
    select.value = chosen;
  } catch (error) {
    select.innerHTML = `<option value="">Ошибка: ${esc(error.message)}</option>`;
    state.barter.error = error.message;
  }
}

function barterOfferHtml(offer) {
  const profitClass = (offer.profit || 0) > 0 ? "good" : "bad";
  const badge = offer.available
    ? `<span class="tag discovery">хватает лотов</span>`
    : `<span class="tag bad">не хватает лотов</span>`;
  const components = (offer.components || []).map((part) => {
    const price = part.unit == null ? "нет цены" : `${moneyFine(part.unit)} × ${money(part.amount)} = ${moneyFine(part.cost)}`;
    return `<div class="barter-component ${part.enough ? "" : "bad"}">${esc(part.name)} <span>${price}</span></div>`;
  }).join("");
  return `<div class="barter-offer">
    <div class="barter-head">
      <b>${esc(offer.settlement)}</b>
      <span>уровень ${money(offer.level)}</span>
      ${badge}
    </div>
    <div class="barter-grid">
      <div><span>Деньги</span><b>${money(offer.money)}</b></div>
      <div><span>Компоненты</span><b>${moneyFine(offer.components_cost)}</b></div>
      <div><span>Себестоимость</span><b>${moneyFine(offer.total_cost)}</b></div>
      <div><span>Цена продажи</span><b>${moneyFine(offer.sale_price)}</b></div>
      <div><span>Профит</span><b class="${profitClass}">${offer.profit == null ? "—" : `${offer.profit > 0 ? "+" : ""}${moneyFine(offer.profit)}`}</b></div>
      <div><span>Маржа</span><b class="${profitClass}">${offer.margin_pct == null ? "—" : pct(offer.margin_pct)}</b></div>
    </div>
    <div class="barter-components">${components}</div>
  </div>`;
}

function renderBarter(data) {
  const summary = data || {};
  const output = summary.output || {};
  $("#barterPrice").textContent = moneyFine(output.sales_median);
  $("#barterAsk").textContent = moneyFine(output.unit_min);
  $("#barterRecipes").textContent = money(summary.recipes || 0);
  const best = summary.best;
  $("#barterBest").textContent = best && best.profit != null ? `${best.profit > 0 ? "+" : ""}${money(best.profit)}` : "—";
  $("#barterBestSub").textContent = best && best.margin_pct != null ? `${pct(best.margin_pct)} маржи` : "после комиссии";
  $("#barterInfo").textContent = summary.name ? `${summary.name} · комиссия ${summary.commission_pct}%` : "";
  const error = summary.error ? `<p class="hint warn">${esc(summary.error)}</p>` : "";
  const errors = summary.errors && Object.keys(summary.errors).length
    ? `<p class="hint warn">Не удалось получить цены: ${esc(Object.keys(summary.errors).join(", "))}</p>`
    : "";
  if (!summary.offers || !summary.offers.length) {
    $("#barterResult").innerHTML = `${error}${errors}<div class="empty">${esc(summary.error || "Для выбранного предмета нет рецептов.")}</div>`;
    return;
  }
  $("#barterResult").innerHTML = `${error}${errors}${summary.offers.map(barterOfferHtml).join("")}`;
}

async function loadBarter() {
  const item = $("#barterItem") ? $("#barterItem").value : "";
  if (!item) return;
  state.barter.item = item;
  $("#barterResult").innerHTML = `<div class="empty">Считаю цены компонентов…</div>`;
  try {
    const data = await api(`/api/barter?item=${encodeURIComponent(item)}`);
    state.barter.data = data;
    renderBarter(data);
  } catch (error) {
    $("#barterResult").innerHTML = `<div class="empty">Не удалось рассчитать: ${esc(error.message)}</div>`;
  }
}

async function ensureBarter() {
  if (!state.barter.items || !state.barter.items.length) await renderBarterItems();
  if (!state.barter.item) state.barter.item = (state.barter.items || [])[0]?.id || null;
  if (!state.barter.item) return;
  const select = $("#barterItem");
  if (select && [...select.options].some((option) => option.value === state.barter.item)) select.value = state.barter.item;
  if (!state.barter.data || state.barter.data.item !== state.barter.item) await loadBarter();
}

function bindBarter() {
  const select = $("#barterItem");
  const refresh = $("#barterRefresh");
  if (!select || !refresh) return;
  select.addEventListener("change", loadBarter);
  refresh.addEventListener("click", loadBarter);
}

// ------------------------------------------------------------------ быстрый мониторинг

async function pollFast() {
  try {
    const data = await api("/api/fast");
    state.fast.data = data;
    renderFast(data);
  } catch (error) {
    // Сервер мог перезапускаться.
  }
}

function renderFast(data) {
  const feed = $("#fastFeed");
  if (!feed) return;
  const events = data.events || [];
  const first = state.fast.seen === null;
  if (first) state.fast.seen = new Set(events.map((event) => event.id));
  for (const event of events) {
    if (!state.fast.seen.has(event.id)) {
      state.fast.seen.add(event.id);
      if (!first) toast(`⚡ ${event.name}: ${moneyFine(event.unit)} · −${pct(event.discount_pct)}`, "warn");
    }
  }
  const enabled = !!data.enabled;
  if (!enabled && !events.length) {
    feed.classList.add("hidden");
    return;
  }
  feed.classList.remove("hidden");
  let status = enabled ? `${data.items.length} предмет(ов) · каждые ${data.interval} с` : "нет предметов";
  if (data.paused) status = data.paused;
  if (data.error) status = `ошибка: ${data.error}`;
  $("#fastStatus").textContent = status;
  $("#fastEvents").innerHTML = events.slice(0, 5).map((event) => `
    <div class="fast-event">
      <b>${esc(event.name)}</b>
      <span>${moneyFine(event.unit)} · база ${moneyFine(event.benchmark)}</span>
      <strong>${pct(event.discount_pct)}</strong>
      <small>${esc(dateTime(event.at))}</small>
    </div>`).join("");
}

// ------------------------------------------------------------------ сигналы

const SIGNAL_OUTCOME_LABELS = {
  active: "активен",
  sold: "вероятно продан",
  closed: "закрыт",
  unknown: "—",
};

async function loadSignals() {
  const days = Number($("#signalsDays").value || 7);
  state.signals.days = days;
  try {
    const data = await api(`/api/signals?days=${days}`);
    state.signals.data = data;
    renderSignals(data);
  } catch (error) {
    $("#signalsInfo").textContent = `Ошибка: ${error.message}`;
    $("#signalsBody").innerHTML = "";
    $("#signalsEmpty").classList.remove("hidden");
  }
}

function renderSignals(data) {
  const counts = data.counts || {};
  $("#sgTotal").textContent = money(data.total || 0);
  $("#sgActive").textContent = money(counts.active || 0);
  $("#sgActiveSub").textContent = `${money(data.active_profit || 0)} потенциал`;
  $("#sgSold").textContent = money(counts.sold || 0);
  $("#sgHit").textContent = data.hit_rate_pct == null ? "нет закрытых" : `hit rate ${pct(data.hit_rate_pct)}`;
  $("#sgClosed").textContent = money(counts.closed || 0);
  $("#sgProfit").textContent = money(data.sold_profit || 0);
  $("#signalsInfo").textContent = `за ${data.days || 0} дн. · ${data.total || 0} сигналов`;
  const outcome = $("#signalsOutcome").value;
  const rows = (data.signals || []).filter((row) => !outcome || row.outcome === outcome);
  $("#signalsBody").innerHTML = rows.map((row) => {
    const outcomeClass = row.outcome === "sold" ? "discovery" : row.outcome === "closed" ? "bad" : row.outcome === "active" ? "" : "warn";
    return `<tr>
      <td>${esc(dateTime(row.first_at))}</td>
      <td><div class="item-line"><img class="item-icon" loading="lazy" src="/api/icon?item=${encodeURIComponent(row.item)}" alt="" onerror="this.classList.add('missing')"><div><div class="item-name">${esc(row.name)}</div><div class="item-meta">${esc(row.item)}${row.amount > 1 ? ` · ${money(row.amount)} шт.` : ""}</div></div></div></td>
      <td class="num">${moneyFine(row.unit)}</td>
      <td class="num">${moneyFine(row.benchmark)}</td>
      <td class="num disc">${row.discount_pct == null ? "—" : pct(row.discount_pct)}</td>
      <td class="num">${row.score == null ? "—" : row.score}</td>
      <td>${esc(row.source === "discovery" ? "вне списка" : "наблюдение")}</td>
      <td><span class="tag ${outcomeClass}" title="Оценка ${moneyFine(row.total_net)}">${esc(SIGNAL_OUTCOME_LABELS[row.outcome] || row.outcome)}</span></td>
    </tr>`;
  }).join("");
  $("#signalsEmpty").classList.toggle("hidden", rows.length > 0);
}

async function ensureSignals() {
  await loadSignals();
}

function bindSignals() {
  const refresh = $("#signalsRefresh");
  if (!refresh) return;
  refresh.addEventListener("click", loadSignals);
  $("#signalsDays").addEventListener("change", loadSignals);
  $("#signalsOutcome").addEventListener("change", () => {
    if (state.signals.data) renderSignals(state.signals.data);
  });
}

function bindAnalytics() {
  const itemSelect = $("#analyticsItem");
  const daysSelect = $("#analyticsDays");
  const variantSelect = $("#analyticsVariant");
  if (!itemSelect) return;
  itemSelect.addEventListener("change", () => {
    state.analytics.item = itemSelect.value;
    state.analytics.variant = null;
    if (variantSelect) variantSelect.innerHTML = "";
    loadAnalytics();
  });
  daysSelect.addEventListener("change", () => {
    state.analytics.days = Number(daysSelect.value || 7);
    state.analytics.variant = variantSelect ? (variantSelect.value || null) : null;
    loadAnalytics();
  });
  variantSelect.addEventListener("change", () => {
    state.analytics.variant = variantSelect.value || null;
    loadAnalytics();
  });
  $("#analyticsRefresh").addEventListener("click", () => loadAnalytics());
  window.addEventListener("resize", () => {
    if (state.analytics.data) renderAnalyticsChart(state.analytics.data);
  });
}


// ------------------------------------------------------------------ портфель

function portfolioVariant(position) {
  const quality = position.qlt === null || position.qlt === undefined ? "" : (QUALITY_NAMES[position.qlt] || `Качество ${position.qlt}`);
  let pattern = "";
  if (position.ptn !== null && position.ptn !== undefined) pattern = ` · +${position.ptn}`;
  return [quality, pattern.replace(" · ", "")].filter(Boolean).join(" · ");
}

async function loadPortfolio() {
  try {
    const data = await api("/api/portfolio");
    state.portfolio = data;
    renderPortfolio();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function renderPortfolio() {
  const data = state.portfolio;
  if (!data) return;
  const summary = data.summary || {};
  $("#pfOpen").textContent = String(summary.open_count || 0);
  $("#pfInvested").textContent = money(summary.invested || 0);
  const unrealized = Number(summary.unrealized || 0);
  const realized = Number(summary.realized || 0);
  const unrealizedEl = $("#pfUnrealized");
  const realizedEl = $("#pfRealized");
  unrealizedEl.textContent = `${unrealized > 0 ? "+" : ""}${money(unrealized)}`;
  realizedEl.textContent = `${realized > 0 ? "+" : ""}${money(realized)}`;
  unrealizedEl.classList.toggle("loss", unrealized < 0);
  realizedEl.classList.toggle("loss", realized < 0);
  const openPositions = (data.positions || []).filter((position) => position.status === "open");
  const units = openPositions.reduce((sum, position) => sum + (position.amount || 0), 0);
  $("#pfOpenSub").textContent = `${money(units)} шт.`;

  const filter = $("#portfolioFilter").value;
  const positions = (data.positions || []).filter((position) => filter === "all" || position.status === filter);
  const body = $("#portfolioBody");
  const table = body.closest("table");
  const empty = $("#portfolioEmpty");
  const hasAny = positions.length > 0;
  table.classList.toggle("hidden", !hasAny);
  empty.classList.toggle("hidden", hasAny);
  if (!hasAny) {
    $("#portfolioEmpty p").textContent = filter === "sold"
      ? "Проданных позиций пока нет."
      : filter === "all"
        ? "Портфель пуст. Откройте «Сделки» и нажмите «Купил»."
        : "Открытых позиций нет.";
    body.innerHTML = "";
    return;
  }
  body.innerHTML = positions.map((position) => {
    const isSold = position.status === "sold";
    const price = isSold ? position.sold_price : position.current_price;
    const value = isSold ? (position.sold_price || 0) * position.amount : position.current_value;
    const profit = position.profit;
    const profitClass = profit === null || profit === undefined ? "muted" : (profit >= 0 ? "profit" : "loss");
    const status = isSold ? '<span class="tag">Продана</span>' : '<span class="tag pct">Открыта</span>';
    const actions = isSold
      ? `<button type="button" class="btn icon" data-pf-remove="${position.id}" title="Удалить">×</button>`
      : `<button type="button" class="btn small" data-pf-sell="${position.id}">Продать</button>
         <button type="button" class="btn icon" data-pf-remove="${position.id}" title="Удалить">×</button>`;
    return `<tr>
      <td><div class="item-name">${esc(position.name)}</div><div class="item-meta">${esc(position.item)}${portfolioVariant(position) ? ` · ${esc(portfolioVariant(position))}` : ""}</div></td>
      <td>${money(position.amount)} × ${moneyFine(position.buy_price)}<div class="item-meta">${esc(dateTime(position.bought_at))}</div></td>
      <td class="num">${money(position.invested)}</td>
      <td class="num">${price === null || price === undefined ? "—" : moneyFine(price)}</td>
      <td class="num">${value === null || value === undefined ? "—" : money(value)}</td>
      <td class="num ${profitClass}">${profit === null || profit === undefined ? "—" : `${profit > 0 ? "+" : ""}${money(profit)}`}</td>
      <td class="num">${position.target_price ? moneyFine(position.target_price) : "—"}</td>
      <td>${status}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
}

async function buyDeal(index) {
  const deal = state.visibleDeals[index];
  if (!deal) return;
  const amount = deal.amount || 1;
  const price = deal.unit_price;
  const total = price * amount;
  if (!confirm(`Добавить в портфель:\n${deal.name}\n${money(amount)} шт. × ${moneyFine(price)} = ${money(total)}?`)) return;
  try {
    await api("/api/portfolio/buy", {
      method: "POST",
      body: {
        item: deal.item_id,
        name: deal.name,
        qlt: deal.qlt,
        ptn: deal.ptn,
        buy_price: price,
        amount,
        target_price: deal.market_p25 || deal.benchmark,
      },
    });
    state.portfolio = null;
    toast("Добавлено в портфель");
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function sellPosition(positionId) {
  const position = (state.portfolio && state.portfolio.positions || []).find((row) => row.id === positionId);
  if (!position) return;
  const suggested = position.current_price || position.target_price || position.buy_price;
  const raw = prompt(`Цена продажи за штуку для «${position.name}»:`, String(Math.round(suggested)));
  if (raw === null) return;
  const price = Number(String(raw).replace(",", "."));
  if (!Number.isFinite(price) || price <= 0) {
    toast("Некорректная цена", "warn");
    return;
  }
  try {
    await api("/api/portfolio/sell", { method: "POST", body: { id: positionId, price } });
    await loadPortfolio();
    toast("Позиция закрыта");
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function removePosition(positionId) {
  if (!confirm("Удалить позицию из портфеля?")) return;
  try {
    await api("/api/portfolio/remove", { method: "POST", body: { id: positionId } });
    await loadPortfolio();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function ensurePortfolio() {
  if (!state.portfolio) loadPortfolio();
  else renderPortfolio();
}

function bindPortfolio() {
  $("#portfolioFilter").addEventListener("change", renderPortfolio);
  $("#portfolioRefresh").addEventListener("click", () => loadPortfolio());
  $("#portfolioBody").addEventListener("click", (event) => {
    const sell = event.target.closest("[data-pf-sell]");
    if (sell) { sellPosition(Number(sell.getAttribute("data-pf-sell"))); return; }
    const remove = event.target.closest("[data-pf-remove]");
    if (remove) { removePosition(Number(remove.getAttribute("data-pf-remove"))); return; }
  });
}


// ------------------------------------------------------------------ наблюдение

async function loadCatalog() {
  try {
    const data = await api("/api/catalog/categories");
    state.categories = data.categories || [];
    state.catalogError = data.ready ? null : (data.error || null);
    $("#catalogState").textContent = data.ready
      ? `справочник: ${state.categories.reduce((sum, c) => sum + c.count, 0)} предметов`
      : (data.error || "справочник загружается…");
    renderCategoryChips();
    renderBanner();
    if (data.ready && !state.catalogSearchDone) {
      state.catalogSearchDone = true;
      runCatalogSearch();
    }
  } catch (error) {
    $("#catalogState").textContent = "справочник недоступен";
  }
}

function renderCategoryChips() {
  const host = $("#categoryChips");
  host.innerHTML = state.categories.map((category) => `
    <span class="chip${state.activeCategory === category.id ? " active" : ""}" data-cat="${esc(category.id)}">
      <button type="button" class="main" data-filter="${esc(category.id)}">${esc(category.label)} <span class="muted">${category.count}</span></button>
      <button type="button" class="add" data-addcat="${esc(category.id)}" title="Добавить до 100 предметов категории">＋</button>
    </span>`).join("");
}

function bindWatch() {
  $("#catalogSearch").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(runCatalogSearch, 250);
  });

  $("#categoryChips").addEventListener("click", async (event) => {
    const add = event.target.closest("[data-addcat]");
    if (add) {
      const category = add.getAttribute("data-addcat");
      const label = categoryLabel(category);
      const count = (state.categories.find((c) => c.id === category) || {}).count || 0;
      const limit = Math.min(100, count);
      if (!confirm(`Добавить до ${limit} предметов из категории «${label}»?\n\nСкан такого списка займёт несколько минут из-за лимита API.`)) return;
      await watchAction({ add_category: category, limit });
      return;
    }
    const filter = event.target.closest("[data-filter]");
    if (filter) {
      const category = filter.getAttribute("data-filter");
      state.activeCategory = state.activeCategory === category ? "" : category;
      renderCategoryChips();
      runCatalogSearch();
    }
  });

  $("#catalogResults").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-add]");
    if (!button) return;
    const id = button.getAttribute("data-add");
    await watchAction({ add: [id] });
  });

  $("#watchList").addEventListener("click", async (event) => {
    const ruleButton = event.target.closest("[data-rule]");
    if (ruleButton) {
      openRuleEditor(ruleButton.getAttribute("data-rule"));
      return;
    }
    const button = event.target.closest("[data-remove]");
    if (!button) return;
    await watchAction({ remove: [button.getAttribute("data-remove")] });
  });

  $("#clearWatchBtn").addEventListener("click", async () => {
    if (!confirm("Очистить весь список наблюдения?")) return;
    await watchAction({ clear: true });
  });
}

async function watchAction(payload) {
  try {
    const result = await api("/api/watch", { method: "POST", body: payload });
    state.config.watchlist = result.watchlist;
    $("#watchBadge").textContent = String(result.count);
    renderWatchList();
    renderAnalyticsItems();
    runCatalogSearch();
    if (result.count === 0) toast("Список наблюдения пуст", "warn");
    else toast(`В списке ${result.count} предмет(ов). Запустите скан, чтобы обновить сделки.`);
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function runCatalogSearch() {
  const query = $("#catalogSearch").value.trim();
  const host = $("#catalogResults");
  if (!query && !state.activeCategory) {
    host.innerHTML = `<div class="list-empty">Начните вводить название или выберите категорию выше.</div>`;
    return;
  }
  if (!state.catalog) host.innerHTML = `<div class="list-empty">Загрузка…</div>`;
  try {
    const url = `/api/catalog/search?q=${encodeURIComponent(query)}&category=${encodeURIComponent(state.activeCategory)}&limit=60`;
    const data = await api(url);
    if (!data.ready) {
      host.innerHTML = `<div class="list-empty">Справочник ещё загружается…</div>`;
      return;
    }
    const watch = new Set(state.config ? state.config.watchlist : []);
    if (!data.items.length) {
      host.innerHTML = `<div class="list-empty">Ничего не найдено.</div>`;
      return;
    }
    for (const item of data.items) state.itemMeta[item.id] = item;
    host.innerHTML = data.items.map((item) => {
      const name = item.name.ru || item.name.en || item.id;
      const inWatch = watch.has(item.id);
      return `
        <div class="pick-row">
          <div class="grow">
            <div class="name">${esc(name)}</div>
            <div class="sub">${esc(item.id)}</div>
          </div>
          <span class="cat">${esc(categoryLabel(categoryOf(item.category)))}</span>
          <button type="button" class="btn small${inWatch ? " ghost" : ""}" data-add="${esc(item.id)}" ${inWatch ? "disabled" : ""}>${inWatch ? "✓ в списке" : "+ добавить"}</button>
        </div>`;
    }).join("");
  } catch (error) {
    host.innerHTML = `<div class="list-empty">Ошибка: ${esc(error.message)}</div>`;
  }
}

async function resolveWatchMeta() {
  const watch = (state.config && state.config.watchlist) || [];
  const missing = watch.filter((id) => !state.itemMeta[id]);
  if (!missing.length) return;
  try {
    const data = await api(`/api/catalog/items?ids=${encodeURIComponent(missing.join(","))}`);
    for (const item of data.items || []) state.itemMeta[item.id] = item;
  } catch {
    // Названия будут заменены ID — не критично.
  }
}

function populateRuleBonuses(current) {
  const select = $("#ruleBonus");
  if (!select) return;
  const entries = Object.entries(BONUS_LABELS).sort((a, b) => a[1].localeCompare(b[1], "ru"));
  select.innerHTML = `<option value="">Любой</option>` + entries.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  if (current && !entries.some(([value]) => value === current)) {
    select.add(new Option(current, current));
  }
  select.value = current || "";
}

function openRuleEditor(itemId) {
  const rule = (state.config.item_rules || {})[itemId] || {};
  state.ruleItem = itemId;
  const meta = state.itemMeta[itemId];
  const name = meta ? (meta.name.ru || meta.name.en || itemId) : itemId;
  $("#ruleTitle").textContent = `Правило: ${name}`;
  $("#ruleEnabled").checked = rule.enabled !== false;
  $("#ruleFast").checked = !!rule.fast;
  $("#ruleMinPrice").value = rule.min_price ?? "";
  $("#ruleMinProfit").value = rule.min_profit ?? "";
  $("#ruleQlt").value = rule.qlt ?? "";
  populateRuleBonuses(rule.bonus || "");
  const panel = $("#ruleEditor");
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function saveRule(clear = false) {
  const item = state.ruleItem;
  if (!item) return;
  const rule = clear ? {} : { enabled: $("#ruleEnabled").checked, fast: $("#ruleFast").checked };
  if (!clear) {
    const minPrice = $("#ruleMinPrice").value.trim();
    const minProfit = $("#ruleMinProfit").value.trim();
    const qlt = $("#ruleQlt").value;
    const bonus = $("#ruleBonus").value;
    if (minPrice !== "") rule.min_price = Number(minPrice);
    if (minProfit !== "") rule.min_profit = Number(minProfit);
    if (qlt !== "") rule.qlt = Number(qlt);
    if (bonus) rule.bonus = bonus;
  }
  try {
    const data = await api("/api/rules", { method: "POST", body: { item, rule } });
    state.config.item_rules = data.item_rules || {};
    $("#ruleEditor").classList.add("hidden");
    state.ruleItem = null;
    renderWatchList();
    toast(clear ? "Правило сброшено" : "Правило сохранено. Запустите скан.");
  } catch (error) {
    toast(error.message, "bad");
  }
}

function bindRuleEditor() {
  const save = $("#ruleSave");
  if (!save) return;
  save.addEventListener("click", () => saveRule(false));
  $("#ruleReset").addEventListener("click", () => saveRule(true));
  $("#ruleClose").addEventListener("click", () => {
    $("#ruleEditor").classList.add("hidden");
    state.ruleItem = null;
  });
}

async function renderWatchList() {
  const host = $("#watchList");
  const watch = (state.config && state.config.watchlist) || [];
  $("#watchCount").textContent = String(watch.length);
  if (!watch.length) {
    host.innerHTML = `<div class="list-empty">Список пуст. Добавьте предметы слева.</div>`;
    updateWatchEstimate();
    return;
  }
  await resolveWatchMeta();
  host.innerHTML = watch.map((id) => {
    const meta = state.itemMeta[id];
    const name = meta ? (meta.name.ru || meta.name.en || id) : id;
    const category = meta ? categoryLabel(categoryOf(meta.category)) : "—";
    const rule = (state.config.item_rules || {})[id] || {};
    const disabled = rule.enabled === false;
    const ruleTag = `${rule.fast ? '<span class="tag warn">⚡</span>' : ""}${Object.keys(rule).length ? `<span class="tag">${disabled ? "выкл" : "правило"}</span>` : ""}`;
    return `
      <div class="watch-row${disabled ? " disabled" : ""}">
        <div class="grow">
          <div class="item-line">
            <img class="item-icon" loading="lazy" src="/api/icon?item=${encodeURIComponent(id)}" alt="" onerror="this.classList.add('missing')">
            <div>
              <div class="name">${esc(name)}</div>
              <div class="sub">${esc(id)} · ${esc(category)}</div>
            </div>
          </div>
        </div>
        ${ruleTag}
        <button type="button" class="btn icon" data-rule="${esc(id)}" title="Правило предмета">⚙</button>
        <button type="button" class="btn icon" data-remove="${esc(id)}" title="Убрать">×</button>
      </div>`;
  }).join("");
  updateWatchEstimate();
}

function updateWatchEstimate() {
  const cfg = state.config;
  if (!cfg) return;
  const estimate = scanEstimate(cfg);
  $("#watchEstimate").textContent = estimate.count
    ? `≈ ${estimate.avg} запросов на скан (первый — до ${estimate.first}) · ~${estimate.avgSeconds}–${estimate.firstSeconds} с. Лоты обновляются каждый скан, история — раз в ${estimate.ttl} мин. Лимит 400 токенов/мин.`
    : "Пока список пуст.";
  updateStackScope();
}

// ------------------------------------------------------------------ старт

async function init() {
  bindTabs();
  bindDealsControls();
  bindStacks();
  bindAnalytics();
  bindMovement();
  bindSignals();
  bindBarter();
  bindPortfolio();
  bindRuleEditor();
  bindWatch();
  bindSettings();
  $("#noDealsBlock").open = false;

  try {
    await loadConfig();
    await renderAnalyticsItems();
    await renderMovementItems();
    if (location.hash === "#movement") await ensureMovement();
    await poll();
    await loadResults();
    await loadCatalog();
    setInterval(loadCatalog, 30000);
    setInterval(poll, 2500);
  } catch (error) {
    toast(`Не удалось загрузить состояние: ${error.message}`, "bad");
  }
}

document.addEventListener("DOMContentLoaded", init);
