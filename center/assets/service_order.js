(() => {
  "use strict";

  const API = {
    health: "/api/health",
    filters: "/api/service-order/filters",
    overview: "/api/service-order/overview",
    newErrors: "/api/service-order/new-errors",
    newErrorValues: "/api/service-order/new-errors/values",
    notification: "/api/notifications/latest",
    events: "/api/service-order/events",
  };

  const FALLBACK_BUSINESSES = ["중부", "북부", "남부", "동부", "서부"];
  const SERIES_COLORS = ["#1e3a62", "#ee3341", "#2f7398", "#d98245", "#74629b", "#4d8588"];
  const SVG_NS = "http://www.w3.org/2000/svg";
  const now = new Date();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const state = {
    scope: "business",
    timeMode: "year",
    year: now.getFullYear(),
    month: "",
    start: "",
    end: "",
    business: "",
    person: null,
    people: [],
    businesses: [...FALLBACK_BUSINESSES],
    years: [now.getFullYear()],
    dateBounds: { start: "", end: "" },
    overview: emptyOverview(),
    rankingMetric: {
      subcategory: "count",
      person: "count",
    },
    insightView: "change",
    causeDimension: "business",
    trendView: "line",
    briefingMessages: ["선택한 조건의 현황을 정리하고 있습니다."],
    briefingIndex: 0,
    briefingTimer: null,
    requestSequence: 0,
    overviewController: null,
    eventSource: null,
    lastEventId: 0,
    liveRefreshTimer: null,
    liveRefreshInFlight: false,
    liveRefreshPending: false,
    liveRefreshMessage: "",
    liveRefreshSuppressToast: false,
    activeToastSignature: "",
    dismissedNewErrorSignatures: new Set(),
    suppressNextNewErrorToast: false,
    newErrorList: {
      open: false,
      loading: false,
      error: "",
      page: 1,
      pageSize: 50,
      total: 0,
      totalPages: 1,
      columns: [],
      rows: [],
      search: "",
      columnFilters: {},
      downloadUrl: "",
      activeFilterColumn: "",
      filterValues: [],
      filterDraftValues: new Set(),
      controller: null,
      filterValuesController: null,
      requestSequence: 0,
    },
  };

  const elements = {
    serverBox: document.querySelector("#server-box"),
    serverState: document.querySelector("#server-state"),
    serverAddress: document.querySelector("#server-address"),
    updatedTime: document.querySelector("#updated-time"),
    statusBriefing: document.querySelector("#status-briefing"),
    briefingMessage: document.querySelector("#briefing-message"),
    briefingPosition: document.querySelector("#briefing-position"),
    briefingPrev: document.querySelector("#briefing-prev"),
    briefingNext: document.querySelector("#briefing-next"),
    filterPanel: document.querySelector(".filter-panel"),
    filterStickyAnchor: document.querySelector("#filter-sticky-anchor"),
    scopeControl: document.querySelector("#scope-control"),
    personFilter: document.querySelector("#person-filter"),
    personSearch: document.querySelector("#person-search"),
    personOptions: document.querySelector("#person-options"),
    clearPerson: document.querySelector("#clear-person"),
    businessFilter: document.querySelector("#division-filter"),
    businessSelect: document.querySelector("#division-select"),
    periodControl: document.querySelector("#period-control"),
    monthInputs: document.querySelector("#month-inputs"),
    rangeInputs: document.querySelector("#range-inputs"),
    yearSelect: document.querySelector("#year-select"),
    monthSelect: document.querySelector("#month-select"),
    startDate: document.querySelector("#start-date"),
    endDate: document.querySelector("#end-date"),
    resetFilters: document.querySelector("#reset-filters"),
    activeFilterSummary: document.querySelector("#active-filter-summary"),
    subjectName: document.querySelector("#subject-name"),
    subjectBusiness: document.querySelector("#subject-division"),
    metricTotal: document.querySelector("#metric-total"),
    metricErrors: document.querySelector("#metric-errors"),
    metricErrorsNote: document.querySelector("#metric-errors-note"),
    metricRate: document.querySelector("#metric-rate"),
    metricRateNote: document.querySelector("#metric-rate-note"),
    newErrorMonitor: document.querySelector("#new-error-monitor"),
    newErrorCount: document.querySelector("#new-error-count"),
    newErrorDownload: document.querySelector("#new-error-download"),
    newErrorMessage: document.querySelector("#new-error-message"),
    newErrorListPanel: document.querySelector("#new-error-list-panel"),
    newErrorListClose: document.querySelector("#new-error-list-close"),
    newErrorListSummary: document.querySelector("#new-error-list-summary"),
    newErrorListSearch: document.querySelector("#new-error-list-search"),
    newErrorFilterReset: document.querySelector("#new-error-filter-reset"),
    newErrorTableShell: document.querySelector("#new-error-table-shell"),
    newErrorTableHead: document.querySelector("#new-error-table-head"),
    newErrorTableBody: document.querySelector("#new-error-table-body"),
    newErrorTableState: document.querySelector("#new-error-table-state"),
    newErrorPageRange: document.querySelector("#new-error-page-range"),
    newErrorPageLabel: document.querySelector("#new-error-page-label"),
    newErrorFirstPage: document.querySelector("#new-error-first-page"),
    newErrorPrevPage: document.querySelector("#new-error-prev-page"),
    newErrorNextPage: document.querySelector("#new-error-next-page"),
    newErrorLastPage: document.querySelector("#new-error-last-page"),
    newErrorFilterMenu: document.querySelector("#new-error-filter-menu"),
    newErrorFilterLabel: document.querySelector("#new-error-filter-label"),
    newErrorFilterClose: document.querySelector("#new-error-filter-close"),
    newErrorFilterInput: document.querySelector("#new-error-filter-input"),
    newErrorFilterHint: document.querySelector("#new-error-filter-hint"),
    newErrorFilterSelectAll: document.querySelector("#new-error-filter-select-all"),
    newErrorFilterSelectedCount: document.querySelector("#new-error-filter-selected-count"),
    newErrorFilterValues: document.querySelector("#new-error-filter-values"),
    newErrorFilterClear: document.querySelector("#new-error-filter-clear"),
    newErrorFilterApply: document.querySelector("#new-error-filter-apply"),
    trendDescription: document.querySelector("#trend-description"),
    trendViewTabs: document.querySelector("#trend-view-tabs"),
    chartLegend: document.querySelector("#chart-legend"),
    chartWrap: document.querySelector("#trend-chart-wrap"),
    chart: document.querySelector("#trend-chart"),
    chartEmpty: document.querySelector("#chart-empty"),
    chartTooltip: document.querySelector("#chart-tooltip"),
    businessOverviewList: document.querySelector("#business-overview-list"),
    businessOverviewEmpty: document.querySelector("#business-overview-empty"),
    businessOverviewPeriod: document.querySelector("#business-overview-period"),
    insightCaption: document.querySelector("#insight-caption"),
    insightViewTabs: document.querySelector("#insight-view-tabs"),
    changeView: document.querySelector("#change-view"),
    patternView: document.querySelector("#pattern-view"),
    causeSwitch: document.querySelector("#cause-switch"),
    comparisonChartWrap: document.querySelector("#comparison-chart-wrap"),
    comparisonChart: document.querySelector("#comparison-chart"),
    comparisonEmpty: document.querySelector("#comparison-empty"),
    comparisonTooltip: document.querySelector("#comparison-tooltip"),
    patternTotal: document.querySelector("#pattern-total"),
    patternRepeatBar: document.querySelector("#pattern-repeat-bar"),
    patternNewBar: document.querySelector("#pattern-new-bar"),
    patternRepeatValue: document.querySelector("#pattern-repeat-value"),
    patternNewValue: document.querySelector("#pattern-new-value"),
    patternList: document.querySelector("#pattern-list"),
    patternEmpty: document.querySelector("#pattern-empty"),
    subcategoryCaption: document.querySelector("#subcategory-caption"),
    subcategoryList: document.querySelector("#subcategory-ranking"),
    subcategoryEmpty: document.querySelector("#subcategory-empty"),
    personCaption: document.querySelector("#person-caption"),
    personList: document.querySelector("#person-ranking"),
    personEmpty: document.querySelector("#person-empty"),
    footerPeriod: document.querySelector("#footer-period"),
    toast: document.querySelector("#page-toast"),
    toastTitle: document.querySelector("#toast-title"),
    toastMessage: document.querySelector("#toast-message"),
    closeToast: document.querySelector("#close-toast"),
  };

  const numberFormatter = new Intl.NumberFormat("ko-KR");
  const dateTimeFormatter = new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  function emptyOverview() {
    return {
      context: {},
      summary: { total: null, errors: null, rate: null },
      series: [],
      rankings: {
        subcategory: { count: [], rate: [] },
        person: { count: [], rate: [] },
      },
      newErrors: { count: 0, asOfDate: "", sinceDate: "", lastUpdated: "", downloadUrl: "" },
      comparison: {
        available: false,
        summary: { total: 0, errors: 0, rate: 0, deltaTotal: 0, deltaCount: 0, deltaRate: 0 },
        rateBaseline: { center: null, lower: null, upper: null },
        causes: { business: [], subcategory: [], person: [] },
      },
      patterns: { total: 0, repeated: 0, newCount: 0, repeatedRate: 0, newRate: 0, items: [] },
      businessStatus: { items: [] },
      lastDataDate: "",
      dataBasisDate: "",
      updatedAt: null,
    };
  }

  function firstDefined(object, keys, fallback = undefined) {
    if (!object || typeof object !== "object") return fallback;
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null) return object[key];
    }
    return fallback;
  }

  function asNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(String(value).replace(/,/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function seriesColor(name, fallbackIndex = 0) {
    const businessIndex = FALLBACK_BUSINESSES.indexOf(String(name || "").trim());
    const colorIndex = businessIndex >= 0 ? businessIndex : fallbackIndex;
    return SERIES_COLORS[((colorIndex % SERIES_COLORS.length) + SERIES_COLORS.length) % SERIES_COLORS.length];
  }

  function formatCount(value) {
    const number = asNumber(value);
    return number === null ? "–" : `${numberFormatter.format(Math.round(number))}건`;
  }

  function formatPlainCount(value) {
    const number = asNumber(value);
    return number === null ? "–" : numberFormatter.format(Math.round(number));
  }

  function formatRate(value) {
    const number = asNumber(value);
    if (number === null) return "–";
    const digits = Math.abs(number) < 1 ? 2 : 1;
    return `${number.toLocaleString("ko-KR", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })}%`;
  }

  function formatSignedCount(value) {
    const number = asNumber(value);
    if (number === null) return "–";
    if (number === 0) return "변동 없음";
    return `${number > 0 ? "+" : "−"}${numberFormatter.format(Math.abs(Math.round(number)))}건`;
  }

  function formatSignedRate(value) {
    const number = asNumber(value);
    if (number === null) return "–";
    if (number === 0) return "변동 없음";
    return `${number > 0 ? "+" : "−"}${Math.abs(number).toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%p`;
  }

  function formatDateLabel(value) {
    if (!value) return "기간 미상";
    const text = String(value);
    const monthMatch = text.match(/^(\d{4})[-./](\d{1,2})$/);
    if (monthMatch) return `${monthMatch[1]}.${String(Number(monthMatch[2])).padStart(2, "0")}`;
    const dayMatch = text.match(/^(\d{4})[-./](\d{1,2})[-./](\d{1,2})/);
    if (dayMatch) return `${String(Number(dayMatch[2])).padStart(2, "0")}.${String(Number(dayMatch[3])).padStart(2, "0")}`;
    return text;
  }

  function formatFullDate(value) {
    if (!value) return "–";
    const text = String(value);
    const match = text.match(/^(\d{4})[-./](\d{1,2})(?:[-./](\d{1,2}))?/);
    if (match) {
      const month = String(Number(match[2])).padStart(2, "0");
      return match[3]
        ? `${match[1]}.${month}.${String(Number(match[3])).padStart(2, "0")}`
        : `${match[1]}.${month}`;
    }
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    return `${date.getFullYear()}.${String(date.getMonth() + 1).padStart(2, "0")}.${String(date.getDate()).padStart(2, "0")}`;
  }

  async function fetchJson(url, options = {}, timeout = 10000) {
    const ownController = !options.signal ? new AbortController() : null;
    const timer = ownController ? window.setTimeout(() => ownController.abort(), timeout) : null;
    try {
      const response = await fetch(url, {
        ...options,
        headers: { Accept: "application/json", ...(options.headers || {}) },
        signal: options.signal || ownController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally {
      if (timer) window.clearTimeout(timer);
    }
  }

  function setServerStatus(online) {
    elements.serverBox?.classList.toggle("is-online", online);
    elements.serverBox?.classList.toggle("is-offline", !online);
    if (elements.serverState) elements.serverState.textContent = online ? "서버 연결됨" : "서버 연결 확인 필요";
  }

  async function checkServer() {
    if (elements.serverAddress) elements.serverAddress.textContent = window.location.host || "desktop-004:8000";
    try {
      const result = await fetchJson(API.health, {}, 4500);
      setServerStatus(result?.status === "ok" || result?.status === "healthy" || result?.ok === true);
    } catch (_) {
      setServerStatus(false);
    }
  }

  function normalizePerson(item) {
    if (typeof item === "string") return { id: item, name: item, business: "" };
    const name = String(firstDefined(item, ["name", "person_name", "person", "employee_name", "생성인"], "")).trim();
    return {
      id: String(firstDefined(item, ["id", "person_id", "employee_id", "sap_id", "오더생성자"], name)).trim(),
      name,
      business: String(firstDefined(item, ["business", "business_name", "division", "department", "사업부"], "")).trim(),
    };
  }

  function normalizeFilters(payload) {
    const root = payload?.filters || payload || {};
    const rawBusinesses = firstDefined(root, ["businesses", "business_units", "divisions", "사업부"], []);
    const rawPeople = firstDefined(root, ["people", "persons", "employees", "인원"], []);
    const rawYears = firstDefined(root, ["years", "available_years", "연도"], []);
    const businesses = asArray(rawBusinesses)
      .map((item) => typeof item === "string" ? item : firstDefined(item, ["name", "business", "division", "label"], ""))
      .map((item) => String(item).trim())
      .filter(Boolean);
    const people = asArray(rawPeople).map(normalizePerson).filter((person) => person.name);
    const years = asArray(rawYears).map(Number).filter(Number.isFinite).sort((a, b) => b - a);
    const defaults = root.defaults || payload?.defaults || {};
    const bounds = root.date_bounds || payload?.date_bounds || {};
    return {
      businesses: businesses.length ? [...new Set(businesses)] : [...FALLBACK_BUSINESSES],
      people,
      years: years.length ? [...new Set(years)] : [now.getFullYear()],
      defaultYear: asNumber(firstDefined(defaults, ["year", "default_year"], null)),
      defaultMonth: firstDefined(defaults, ["month", "default_month"], ""),
      dateBounds: {
        start: String(firstDefined(bounds, ["start", "start_date", "min"], "") || ""),
        end: String(firstDefined(bounds, ["end", "end_date", "max"], "") || ""),
      },
    };
  }

  function populateFilterControls(filters) {
    state.businesses = filters.businesses;
    state.people = filters.people;
    state.years = filters.years;
    state.dateBounds = filters.dateBounds;
    if (filters.defaultYear && state.years.includes(filters.defaultYear)) state.year = filters.defaultYear;

    elements.businessSelect.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "전체 사업부";
    elements.businessSelect.append(allOption);
    state.businesses.forEach((business) => {
      const option = document.createElement("option");
      option.value = business;
      option.textContent = business;
      elements.businessSelect.append(option);
    });
    elements.businessSelect.value = state.business;

    elements.yearSelect.replaceChildren();
    state.years.forEach((year) => {
      const option = document.createElement("option");
      option.value = String(year);
      option.textContent = `${year}년`;
      elements.yearSelect.append(option);
    });
    if (!state.years.includes(state.year)) state.year = state.years[0];
    elements.yearSelect.value = String(state.year);
    [elements.startDate, elements.endDate].forEach((input) => {
      input.min = state.dateBounds.start;
      input.max = state.dateBounds.end;
    });
  }

  async function loadFilters() {
    try {
      const payload = await fetchJson(API.filters);
      populateFilterControls(normalizeFilters(payload));
    } catch (_) {
      populateFilterControls(normalizeFilters({}));
    }
  }

  function normalizePoint(item, fallbackLabel = "") {
    const count = asNumber(firstDefined(item, ["error_count", "errors", "count", "candidate_count", "오생성건수"], 0)) ?? 0;
    const total = asNumber(firstDefined(item, ["total_count", "total", "orders", "preprocessed_count", "전체데이터건수", "전처리건수"], null));
    let rate = asNumber(firstDefined(item, ["error_rate", "rate", "error_rate_pct", "오생성률"], null));
    if (rate === null && total) rate = count / total * 100;
    const date = String(firstDefined(item, ["date", "period", "key", "month", "day", "label"], fallbackLabel));
    return {
      date,
      label: String(firstDefined(item, ["label", "display", "period_label"], formatDateLabel(date))),
      count,
      total,
      rate,
    };
  }

  function normalizeTrend(payload, context) {
    const rawTrend = firstDefined(payload, ["trend", "timeline", "chart", "series"], []);
    const root = rawTrend && !Array.isArray(rawTrend) ? rawTrend : {};
    const labels = asArray(root.labels || root.categories).map(String);
    const seriesSource = asArray(root.series || root.datasets);

    if (seriesSource.length) {
      return seriesSource.map((series, seriesIndex) => {
        const name = String(firstDefined(series, ["name", "label", "business", "person"], context.label || `계열 ${seriesIndex + 1}`));
        const rawPoints = asArray(firstDefined(series, ["points", "data", "values"], []));
        const points = rawPoints.map((point, pointIndex) => {
          if (typeof point === "number") return normalizePoint({ count: point }, labels[pointIndex] || String(pointIndex + 1));
          return normalizePoint(point, labels[pointIndex] || String(pointIndex + 1));
        });
        return { name, points };
      }).filter((series) => series.points.length);
    }

    const flat = asArray(rawTrend);
    if (!flat.length) return [];

    if (Array.isArray(flat[0]?.series)) {
      const grouped = new Map();
      flat.forEach((period) => {
        const date = firstDefined(period, ["date", "period", "key", "label"], "");
        period.series.forEach((seriesItem, index) => {
          const name = String(firstDefined(seriesItem, ["name", "label", "business", "person"], `계열 ${index + 1}`));
          if (!grouped.has(name)) grouped.set(name, []);
          grouped.get(name).push(normalizePoint({ ...seriesItem, date }, String(date)));
        });
      });
      return [...grouped.entries()].map(([name, points]) => ({ name, points }));
    }

    const shouldGroup = state.scope === "business" && !state.business;
    const grouped = new Map();
    flat.forEach((item) => {
      const possibleName = firstDefined(item, ["series_name", "business", "business_name", "division", "사업부"], "");
      const name = String(possibleName || context.label || (shouldGroup ? "전체" : "오생성"));
      if (!grouped.has(name)) grouped.set(name, []);
      grouped.get(name).push(normalizePoint(item));
    });
    return [...grouped.entries()].map(([name, points]) => ({ name, points }));
  }

  function normalizeRankingItem(item, type) {
    if (typeof item === "string") {
      return { label: item, business: "", count: 0, total: null, rate: null };
    }
    const labelKeys = type === "person"
      ? ["label", "name", "person_name", "person", "생성인"]
      : ["label", "name", "subcategory", "category", "소분류"];
    const count = asNumber(firstDefined(item, ["error_count", "errors", "count", "candidate_count", "오생성건수"], 0)) ?? 0;
    const total = asNumber(firstDefined(item, ["total_count", "total", "orders", "preprocessed_count", "전체데이터건수", "전처리건수"], null));
    let rate = asNumber(firstDefined(item, ["error_rate", "rate", "error_rate_pct", "오생성률"], null));
    if (rate === null && total) rate = count / total * 100;
    return {
      label: String(firstDefined(item, labelKeys, "미분류")),
      business: String(firstDefined(item, ["business", "business_name", "division", "사업부"], "")),
      count,
      total,
      rate,
    };
  }

  function normalizeRankingGroup(payload, type) {
    const rankingsRoot = payload.rankings || {};
    const aliases = type === "person"
      ? ["person", "people", "persons", "person_rankings", "person_ranking"]
      : ["subcategory", "subcategories", "subcategory_rankings", "subcategory_ranking"];
    let source = null;
    for (const key of aliases) {
      if (rankingsRoot[key] !== undefined) source = rankingsRoot[key];
      if (payload[key] !== undefined) source = payload[key];
      if (source !== null) break;
    }
    source = source || {};
    const onlyArray = Array.isArray(source) ? source : null;
    const countSource = onlyArray || firstDefined(source, ["count", "by_count", "counts", "error_count"], []);
    const rateSource = onlyArray || firstDefined(source, ["rate", "by_rate", "rates", "error_rate"], []);
    const count = asArray(countSource).map((item) => normalizeRankingItem(item, type));
    const rate = asArray(rateSource).map((item) => normalizeRankingItem(item, type));
    count.sort((a, b) => b.count - a.count);
    rate.sort((a, b) => (b.rate ?? -Infinity) - (a.rate ?? -Infinity));
    return { count: count.slice(0, 10), rate: rate.slice(0, 10) };
  }

  function normalizeNewErrors(payload, fallbackUpdatedAt = "") {
    const source = payload?.new_errors || payload?.monitoring?.new_errors || {};
    return {
      count: asNumber(firstDefined(source, ["count", "new_count", "error_count", "errors"], 0)) ?? 0,
      asOfDate: String(firstDefined(source, ["as_of_date", "data_date", "last_data_date"], "") || ""),
      sinceDate: String(firstDefined(source, ["since_date", "from_date", "previous_update_date"], "") || ""),
      lastUpdated: String(firstDefined(source, ["last_updated", "updated_at", "generated_at"], fallbackUpdatedAt) || ""),
      downloadUrl: String(firstDefined(source, ["download_url", "export_url", "xlsx_url"], "") || ""),
    };
  }

  function normalizeComparisonCause(item) {
    return {
      label: String(firstDefined(item || {}, ["name", "label"], "미분류")),
      business: String(firstDefined(item || {}, ["business", "division"], "") || ""),
      currentCount: asNumber(firstDefined(item || {}, ["current_count", "current", "error_count"], 0)) ?? 0,
      previousCount: asNumber(firstDefined(item || {}, ["previous_count", "previous"], 0)) ?? 0,
      deltaCount: asNumber(firstDefined(item || {}, ["delta_count", "delta", "change"], 0)) ?? 0,
      currentRate: asNumber(firstDefined(item || {}, ["current_rate", "error_rate"], 0)) ?? 0,
      previousRate: asNumber(firstDefined(item || {}, ["previous_rate"], 0)) ?? 0,
      deltaRate: asNumber(firstDefined(item || {}, ["delta_rate", "rate_change"], 0)) ?? 0,
    };
  }

  function normalizeComparison(payload) {
    const source = payload?.comparison || {};
    const summary = source.summary || {};
    const baseline = source.rate_baseline || source.rateBaseline || {};
    const causes = source.causes || {};
    return {
      available: Boolean(source.available),
      currentPeriod: source.current_period || source.currentPeriod || {},
      previousPeriod: source.previous_period || source.previousPeriod || {},
      summary: {
        total: asNumber(firstDefined(summary, ["total_count", "total"], 0)) ?? 0,
        errors: asNumber(firstDefined(summary, ["error_count", "errors"], 0)) ?? 0,
        rate: asNumber(firstDefined(summary, ["error_rate", "rate"], 0)) ?? 0,
        deltaTotal: asNumber(firstDefined(summary, ["delta_total", "total_change"], 0)) ?? 0,
        deltaCount: asNumber(firstDefined(summary, ["delta_count", "error_change"], 0)) ?? 0,
        deltaRate: asNumber(firstDefined(summary, ["delta_rate", "rate_change"], 0)) ?? 0,
      },
      rateBaseline: {
        center: asNumber(firstDefined(baseline, ["center", "average"], null)),
        lower: asNumber(firstDefined(baseline, ["lower", "min"], null)),
        upper: asNumber(firstDefined(baseline, ["upper", "max"], null)),
      },
      causes: {
        business: asArray(causes.business).map(normalizeComparisonCause),
        subcategory: asArray(causes.subcategory).map(normalizeComparisonCause),
        person: asArray(causes.person).map(normalizeComparisonCause),
      },
    };
  }

  function normalizePatterns(payload) {
    const source = payload?.patterns || {};
    const items = asArray(source.items).map((item) => ({
      subcategory: String(firstDefined(item || {}, ["subcategory", "category"], "미분류")),
      signature: String(firstDefined(item || {}, ["signature", "pattern", "detail"], "내역 미확인")),
      count: asNumber(firstDefined(item || {}, ["count", "frequency"], 0)) ?? 0,
      lastDate: String(firstDefined(item || {}, ["last_date", "lastDate", "date"], "") || ""),
      months: asArray(firstDefined(item || {}, ["months", "monthly", "monthly_counts"], [])).map((month) => ({
        month: String(firstDefined(month || {}, ["month", "label", "date"], "") || ""),
        count: asNumber(firstDefined(month || {}, ["count", "frequency"], 0)) ?? 0,
      })).filter((month) => month.month && month.count > 0),
    }));
    return {
      total: asNumber(firstDefined(source, ["total_count", "total"], 0)) ?? 0,
      repeated: asNumber(firstDefined(source, ["repeated_count", "repeated"], 0)) ?? 0,
      newCount: asNumber(firstDefined(source, ["new_count", "new"], 0)) ?? 0,
      repeatedRate: asNumber(firstDefined(source, ["repeated_rate", "repeat_rate"], 0)) ?? 0,
      newRate: asNumber(firstDefined(source, ["new_rate"], 0)) ?? 0,
      items,
    };
  }

  function normalizeBusinessStatus(payload) {
    const source = payload?.business_status || payload?.businessStatus || {};
    const rawItems = Array.isArray(source) ? source : asArray(source.items);
    return {
      items: rawItems.map((item) => {
        const total = asNumber(firstDefined(item || {}, ["total_count", "total", "orders"], 0)) ?? 0;
        const errors = asNumber(firstDefined(item || {}, ["error_count", "errors", "count"], 0)) ?? 0;
        let rate = asNumber(firstDefined(item || {}, ["error_rate", "rate"], null));
        if (rate === null && total) rate = errors / total * 100;
        return {
          name: String(firstDefined(item || {}, ["name", "business", "label"], "사업부 미확인")),
          total,
          errors,
          rate,
          previousErrors: asNumber(firstDefined(item || {}, ["previous_error_count", "previous_errors"], null)),
          deltaCount: asNumber(firstDefined(item || {}, ["delta_count", "delta"], null)),
        };
      }),
    };
  }

  function normalizeOverview(payload) {
    const summary = payload?.summary || payload?.metrics || payload?.overview || {};
    const errors = asNumber(firstDefined(summary, ["error_count", "errors", "candidate_count", "confirmed_count", "오생성건수"], null));
    const total = asNumber(firstDefined(summary, ["total_count", "orders", "total", "preprocessed_count", "전체데이터건수", "전처리건수"], null));
    let rate = asNumber(firstDefined(summary, ["error_rate", "rate", "error_rate_pct", "오생성률"], null));
    if (rate === null && errors !== null && total) rate = errors / total * 100;
    const context = payload?.context || {};
    const updatedAt = firstDefined(payload || {}, ["updated_at", "last_updated", "generated_at"], new Date().toISOString());
    const dataBasisDate = String(firstDefined(
      payload || {},
      ["data_basis_date", "latest_data_date"],
      firstDefined(summary, ["last_data_date", "as_of_date", "data_date"], ""),
    ) || "");
    return {
      context,
      summary: { total, errors, rate },
      series: normalizeTrend(payload || {}, context),
      rankings: {
        subcategory: normalizeRankingGroup(payload || {}, "subcategory"),
        person: normalizeRankingGroup(payload || {}, "person"),
      },
      newErrors: normalizeNewErrors(payload || {}, updatedAt),
      comparison: normalizeComparison(payload || {}),
      patterns: normalizePatterns(payload || {}),
      businessStatus: normalizeBusinessStatus(payload || {}),
      lastDataDate: dataBasisDate,
      dataBasisDate,
      updatedAt,
    };
  }

  function periodLabel() {
    if (state.timeMode === "range") {
      if (!state.start || !state.end) return "조회 기간을 선택하세요.";
      return `${state.start.replaceAll("-", ".")} ~ ${state.end.replaceAll("-", ".")} · 일별`;
    }
    if (state.timeMode === "month" && state.month) return `${state.year}년 ${Number(state.month)}월 · 일별`;
    return `${state.year}년 · 월별`;
  }

  function businessOverviewPeriodLabel() {
    if (state.timeMode === "month" && state.month) return `${state.year}년 ${Number(state.month)}월 월별`;
    if (state.timeMode === "year") return `${state.year}년 연간`;
    if (state.start && state.end) return `${state.start.replaceAll("-", ".")} ~ ${state.end.replaceAll("-", ".")}`;
    return "선택 기간";
  }

  function compactPeriod(period) {
    const compactDate = (value) => {
      const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
      return match ? `${Number(match[2])}.${Number(match[3])}` : "";
    };
    const start = compactDate(period?.start);
    const end = compactDate(period?.end);
    if (!start) return "비교 기간";
    return end && end !== start ? `${start}–${end}` : start;
  }

  function subjectLabel() {
    if (state.scope === "person") {
      return state.person
        ? { name: state.person.name, detail: state.person.business || "사업부 미지정" }
        : { name: "인원을 선택하세요", detail: "이름 검색 후 현황을 확인할 수 있습니다." };
    }
    return state.business
      ? { name: state.business.endsWith("사업부") ? state.business : `${state.business}사업부`, detail: "선택 사업부 전체 인원" }
      : { name: "전체 사업부", detail: `${state.businesses.length}개 사업부 비교` };
  }

  function updateFilterSummary() {
    const subject = subjectLabel();
    const label = `${subject.name} · ${periodLabel()}`;
    elements.activeFilterSummary.textContent = label;
    elements.footerPeriod.textContent = label;
    elements.trendDescription.textContent = periodLabel().replace(" · ", " ") + " 추이";
    elements.subjectName.textContent = subject.name;
    elements.subjectBusiness.textContent = subject.detail;
  }

  function clearOverview() {
    state.overview = emptyOverview();
    renderOverview();
  }

  function activeFilterParams() {
    const params = new URLSearchParams({
      scope: state.scope,
      time_mode: state.timeMode,
    });
    if (state.person) params.set("person", state.person.name);
    const selectedBusiness = state.scope === "person" ? (state.person?.business || "") : state.business;
    if (selectedBusiness) params.set("business", selectedBusiness);
    if (state.timeMode === "year" || state.timeMode === "month") params.set("year", String(state.year));
    if (state.timeMode === "month") params.set("month", String(state.month));
    if (state.timeMode === "range") {
      params.set("start", state.start);
      params.set("end", state.end);
    }
    return params;
  }

  function overviewUrl() {
    const params = activeFilterParams();
    params.set("limit", "10");
    return `${API.overview}?${params.toString()}`;
  }

  function newErrorListUrl() {
    const list = state.newErrorList;
    const params = activeFilterParams();
    params.set("page", String(list.page));
    params.set("page_size", String(list.pageSize));
    if (list.search) params.set("search", list.search);
    if (Object.keys(list.columnFilters).length) {
      params.set("column_filters", JSON.stringify(list.columnFilters));
    }
    return `${API.newErrors}?${params.toString()}`;
  }

  function newErrorValuesUrl(column, search = "") {
    const params = activeFilterParams();
    params.set("column", column);
    params.set("limit", "200");
    if (search) params.set("search", search);
    return `${API.newErrorValues}?${params.toString()}`;
  }

  function canLoadOverview() {
    if (state.scope === "person" && !state.person) return false;
    if (state.timeMode === "range") return Boolean(state.start && state.end && state.start <= state.end);
    return true;
  }

  async function loadOverview({ silent = false, suppressNewErrorToast = false } = {}) {
    updateFilterSummary();
    syncBriefingVisibility();
    if (state.newErrorList.open) {
      state.newErrorList.downloadUrl = "";
      syncNewErrorDownload();
    }
    if (!canLoadOverview()) {
      if (state.timeMode === "range" && state.start && state.end && state.start > state.end) {
        showToast("기간을 확인해 주세요.", "종료일은 시작일보다 빠를 수 없습니다.", 5000);
      }
      clearOverview();
      return;
    }

    state.overviewController?.abort();
    const controller = new AbortController();
    state.overviewController = controller;
    const sequence = ++state.requestSequence;
    const requestTimer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const payload = await fetchJson(overviewUrl(), { signal: controller.signal }, 15000);
      if (sequence !== state.requestSequence) return;
      state.overview = normalizeOverview(payload);
      if (suppressNewErrorToast) state.suppressNextNewErrorToast = true;
      renderOverview();
      if (state.newErrorList.open) loadNewErrorList({ resetPage: true, silent: true });
    } catch (error) {
      if (error.name === "AbortError") return;
      if (sequence !== state.requestSequence) return;
      clearOverview();
    } finally {
      window.clearTimeout(requestTimer);
    }
  }

  function normalizeNewErrorList(payload) {
    const root = payload?.data && typeof payload.data === "object" ? payload.data : (payload || {});
    const rawRows = asArray(firstDefined(root, ["rows", "items", "results", "data"], []));
    let columns = asArray(firstDefined(root, ["columns", "headers", "fields"], [])).map((column, index) => {
      if (typeof column === "string") return column;
      return String(firstDefined(column, ["key", "name", "field", "label"], `열 ${index + 1}`));
    });
    if (!columns.length && rawRows[0] && !Array.isArray(rawRows[0]) && typeof rawRows[0] === "object") {
      columns = Object.keys(rawRows[0]);
    }
    const rows = rawRows.map((row) => {
      if (Array.isArray(row)) return Object.fromEntries(columns.map((column, index) => [column, row[index]]));
      return row && typeof row === "object" ? row : { [columns[0] || "값"]: row };
    });
    const total = Math.max(0, asNumber(firstDefined(root, ["total", "total_count", "count"], rows.length)) ?? rows.length);
    const pageSize = Math.max(1, asNumber(firstDefined(root, ["page_size", "limit", "per_page"], state.newErrorList.pageSize)) ?? state.newErrorList.pageSize);
    const page = Math.max(1, asNumber(firstDefined(root, ["page", "current_page"], state.newErrorList.page)) ?? state.newErrorList.page);
    const totalPages = Math.max(1, asNumber(firstDefined(root, ["total_pages", "pages"], Math.ceil(total / pageSize))) ?? Math.ceil(total / pageSize));
    const downloadUrl = String(firstDefined(root, ["download_url", "export_url", "xlsx_url"], "") || "");
    return { columns, rows, total, pageSize, page, totalPages, downloadUrl };
  }

  function displayCellValue(value, column = "") {
    if (value === null || value === undefined) return "";
    if (typeof value === "object") {
      try { return JSON.stringify(value); } catch (_) { return String(value); }
    }
    const text = String(value);
    if (String(column).replace(/\s/g, "") === "오더번호") {
      const integer = text.match(/^([+-]?\d+)\.0+$/);
      if (integer) return integer[1];
    }
    const midnight = text.match(/^(\d{4}-\d{2}-\d{2})(?:T|\s)00:00:00(?:\.0+)?(?:Z|[+-]\d{2}:?\d{2})?$/);
    return midnight ? midnight[1] : text;
  }

  function syncNewErrorDownload() {
    if (!elements.newErrorDownload) return;
    const list = state.newErrorList;
    const monitoring = state.overview.newErrors || {};
    const usesListQuery = list.open;
    const count = usesListQuery ? list.total : (asNumber(monitoring.count) ?? 0);
    const url = usesListQuery ? list.downloadUrl : String(monitoring.downloadUrl || "");
    const context = usesListQuery
      ? `현재 목록 조건 ${formatCount(count)}`
      : `현재 조회 조건 ${formatCount(count)}`;
    if (count > 0 && url) {
      elements.newErrorDownload.href = url;
      elements.newErrorDownload.removeAttribute("aria-disabled");
      elements.newErrorDownload.title = `${context} Excel 저장`;
      elements.newErrorDownload.setAttribute("aria-label", `${context} Excel 저장`);
    } else {
      elements.newErrorDownload.href = "#";
      elements.newErrorDownload.setAttribute("aria-disabled", "true");
      elements.newErrorDownload.title = usesListQuery && list.loading
        ? "목록 조건을 반영하는 중입니다."
        : "저장할 새로운 오생성이 없습니다.";
      elements.newErrorDownload.setAttribute("aria-label", `${context} Excel 저장 불가`);
    }
  }

  function closeNewErrorFilterMenu({ restoreFocus = false } = {}) {
    const list = state.newErrorList;
    const column = list.activeFilterColumn;
    const trigger = column
      ? elements.newErrorTableHead?.querySelector(`button[data-column-index="${list.columns.indexOf(column)}"]`)
      : null;
    elements.newErrorFilterMenu.hidden = true;
    trigger?.classList.remove("is-open");
    elements.newErrorTableHead?.querySelectorAll(".new-error-column-filter").forEach((button) => {
      button.setAttribute("aria-expanded", "false");
    });
    list.filterValuesController?.abort();
    list.filterValuesController = null;
    list.activeFilterColumn = "";
    if (restoreFocus) trigger?.focus();
  }

  function normalizeNewErrorFilterSelection(value) {
    const values = Array.isArray(value) ? value : [value];
    return [...new Set(values.map((item) => displayCellValue(item, state.newErrorList.activeFilterColumn).trim()).filter(Boolean))];
  }

  function applyNewErrorColumnFilter(column, value) {
    const normalized = normalizeNewErrorFilterSelection(value);
    if (normalized.length) state.newErrorList.columnFilters[column] = normalized;
    else delete state.newErrorList.columnFilters[column];
    closeNewErrorFilterMenu();
    loadNewErrorList({ resetPage: true });
  }

  function syncNewErrorFilterSelection() {
    const list = state.newErrorList;
    const selectedVisible = list.filterValues.filter((value) => list.filterDraftValues.has(value)).length;
    if (elements.newErrorFilterSelectAll) {
      elements.newErrorFilterSelectAll.disabled = list.filterValues.length === 0;
      elements.newErrorFilterSelectAll.checked = list.filterValues.length > 0 && selectedVisible === list.filterValues.length;
      elements.newErrorFilterSelectAll.indeterminate = selectedVisible > 0 && selectedVisible < list.filterValues.length;
    }
    if (elements.newErrorFilterSelectedCount) {
      elements.newErrorFilterSelectedCount.textContent = `${numberFormatter.format(list.filterDraftValues.size)}개 선택`;
    }
  }

  function renderNewErrorFilterValues(column, values, { total = values.length, truncated = false, source = "all" } = {}) {
    if (state.newErrorList.activeFilterColumn !== column) return;
    const list = state.newErrorList;
    list.filterValues = [...new Set(values.map((value) => displayCellValue(value, column).trim()).filter(Boolean))];
    elements.newErrorFilterValues.replaceChildren();
    elements.newErrorFilterHint.textContent = source === "all"
      ? (truncated
        ? `전체 ${numberFormatter.format(total)}개 값 중 앞의 ${numberFormatter.format(list.filterValues.length)}개 표시`
        : `전체 데이터의 값 ${numberFormatter.format(total)}개`)
      : "값 목록을 불러오지 못해 현재 페이지 값으로 표시합니다.";
    if (list.filterValues.length) {
      list.filterValues.forEach((value) => {
        const option = document.createElement("label");
        option.className = "new-error-filter-option";
        option.role = "option";
        option.title = value;
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = list.filterDraftValues.has(value);
        const text = document.createElement("span");
        text.textContent = value;
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) list.filterDraftValues.add(value);
          else list.filterDraftValues.delete(value);
          option.classList.toggle("is-selected", checkbox.checked);
          syncNewErrorFilterSelection();
        });
        option.classList.toggle("is-selected", checkbox.checked);
        option.append(checkbox, text);
        elements.newErrorFilterValues.append(option);
      });
    }
    if (!list.filterValues.length) {
      const empty = document.createElement("span");
      empty.className = "new-error-filter-empty";
      empty.textContent = "일치하는 값이 없습니다.";
      elements.newErrorFilterValues.append(empty);
    }
    syncNewErrorFilterSelection();
  }

  async function loadNewErrorFilterValues(column, search = "") {
    const list = state.newErrorList;
    list.filterValuesController?.abort();
    const controller = new AbortController();
    list.filterValuesController = controller;
    elements.newErrorFilterHint.textContent = "전체 데이터의 값을 불러오는 중입니다.";
    elements.newErrorFilterValues.replaceChildren();
    try {
      const payload = await fetchJson(newErrorValuesUrl(column, search.trim()), { signal: controller.signal }, 10000);
      if (controller !== list.filterValuesController || list.activeFilterColumn !== column) return;
      const values = asArray(firstDefined(payload || {}, ["values", "items", "results"], []));
      renderNewErrorFilterValues(column, values, {
        total: asNumber(firstDefined(payload || {}, ["total", "count"], values.length)) ?? values.length,
        truncated: Boolean(payload?.truncated),
        source: "all",
      });
    } catch (error) {
      if (error.name === "AbortError" || list.activeFilterColumn !== column) return;
      const fallback = [...new Set(list.rows.map((row) => displayCellValue(row[column], column).trim()).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b, "ko-KR", { numeric: true }))
        .slice(0, 40);
      renderNewErrorFilterValues(column, fallback, { source: "page" });
    }
  }

  function openNewErrorFilterMenu(column, trigger) {
    const list = state.newErrorList;
    list.activeFilterColumn = column;
    elements.newErrorFilterLabel.textContent = `${column} 필터`;
    elements.newErrorFilterInput.value = "";
    list.filterDraftValues = new Set(
      normalizeNewErrorFilterSelection(list.columnFilters[column] || [])
    );
    renderNewErrorFilterValues(column, [], { source: "all" });
    elements.newErrorTableHead.querySelectorAll(".new-error-column-filter").forEach((button) => {
      button.setAttribute("aria-expanded", String(button === trigger));
      button.classList.toggle("is-open", button === trigger);
    });
    elements.newErrorFilterMenu.hidden = false;
    const rect = trigger.getBoundingClientRect();
    const menuWidth = Math.min(292, window.innerWidth - 24);
    const left = Math.max(12, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 12));
    elements.newErrorFilterMenu.style.left = `${left}px`;
    elements.newErrorFilterMenu.style.top = `${Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - 430))}px`;
    loadNewErrorFilterValues(column, "");
    window.requestAnimationFrame(() => elements.newErrorFilterInput.focus());
  }

  function renderNewErrorList() {
    const list = state.newErrorList;
    const hasColumnFilters = Object.keys(list.columnFilters).length > 0;
    elements.newErrorFilterReset.hidden = !hasColumnFilters;
    elements.newErrorTableHead.replaceChildren();
    list.columns.forEach((column, index) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.title = column;
      const content = document.createElement("div");
      content.className = "new-error-column-head";
      const label = document.createElement("span");
      label.textContent = column;
      const filter = document.createElement("button");
      filter.type = "button";
      filter.className = "new-error-column-filter";
      filter.dataset.columnIndex = String(index);
      filter.setAttribute("aria-label", `${column} 열 필터 열기`);
      filter.setAttribute("aria-expanded", "false");
      filter.classList.toggle("is-filtered", Boolean(list.columnFilters[column]));
      filter.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!elements.newErrorFilterMenu.hidden && list.activeFilterColumn === column) {
          closeNewErrorFilterMenu({ restoreFocus: true });
        } else {
          openNewErrorFilterMenu(column, filter);
        }
      });
      content.append(label, filter);
      th.append(content);
      elements.newErrorTableHead.append(th);
    });

    elements.newErrorTableBody.replaceChildren();
    if (!list.loading && !list.error) {
      list.rows.forEach((row) => {
        const tr = document.createElement("tr");
        list.columns.forEach((column) => {
          const td = document.createElement("td");
          const value = displayCellValue(row[column], column);
          td.textContent = value;
          if (value) td.title = value;
          tr.append(td);
        });
        elements.newErrorTableBody.append(tr);
      });
    }

    elements.newErrorTableState.hidden = false;
    if (list.loading) {
      elements.newErrorTableState.textContent = "새로운 오생성 목록을 불러오고 있습니다.";
    } else if (list.error) {
      elements.newErrorTableState.textContent = list.error;
    } else if (!list.rows.length) {
      elements.newErrorTableState.textContent = "선택한 조건에 표시할 새로운 오생성이 없습니다.";
    } else {
      elements.newErrorTableState.hidden = true;
    }

    const total = list.total;
    const start = total ? (list.page - 1) * list.pageSize + 1 : 0;
    const end = total ? Math.min(total, start + list.rows.length - 1) : 0;
    elements.newErrorListSummary.textContent = `${periodLabel()} · 검색 결과 ${formatCount(total)}`;
    elements.newErrorPageRange.textContent = total ? `${numberFormatter.format(start)}–${numberFormatter.format(end)} / ${numberFormatter.format(total)}건` : "0건";
    elements.newErrorPageLabel.textContent = `${numberFormatter.format(list.page)} / ${numberFormatter.format(list.totalPages)}`;
    elements.newErrorFirstPage.disabled = list.loading || list.page <= 1;
    elements.newErrorPrevPage.disabled = list.loading || list.page <= 1;
    elements.newErrorNextPage.disabled = list.loading || list.page >= list.totalPages;
    elements.newErrorLastPage.disabled = list.loading || list.page >= list.totalPages;
    elements.newErrorTableShell.setAttribute("aria-busy", String(list.loading));
  }

  async function loadNewErrorList({ resetPage = false, silent = false } = {}) {
    const list = state.newErrorList;
    if (!list.open || !canLoadOverview()) return;
    if (resetPage) list.page = 1;
    list.controller?.abort();
    const controller = new AbortController();
    list.controller = controller;
    const sequence = ++list.requestSequence;
    list.loading = true;
    list.error = "";
    list.downloadUrl = "";
    syncNewErrorDownload();
    if (!silent) renderNewErrorList();
    try {
      const payload = await fetchJson(newErrorListUrl(), { signal: controller.signal }, 15000);
      if (sequence !== list.requestSequence) return;
      Object.assign(list, normalizeNewErrorList(payload), { loading: false, error: "" });
      renderNewErrorList();
      syncNewErrorDownload();
    } catch (error) {
      if (error.name === "AbortError" || sequence !== list.requestSequence) return;
      list.loading = false;
      list.error = "목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
      renderNewErrorList();
      syncNewErrorDownload();
    }
  }

  function setNewErrorListOpen(open) {
    const list = state.newErrorList;
    list.open = Boolean(open);
    elements.newErrorListPanel.hidden = !list.open;
    elements.newErrorCount.setAttribute("aria-expanded", String(list.open));
    const caption = elements.newErrorCount.querySelector("span");
    if (caption) caption.textContent = list.open ? "목록 닫기" : "목록 보기";
    if (!list.open) {
      list.controller?.abort();
      closeNewErrorFilterMenu();
      syncNewErrorDownload();
      return;
    }
    list.downloadUrl = "";
    syncNewErrorDownload();
    loadNewErrorList({ resetPage: true });
    window.requestAnimationFrame(() => {
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      elements.newErrorListPanel.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "nearest" });
    });
  }

  function renderNewErrorMonitor() {
    const monitoring = state.overview.newErrors || {};
    const suppressToast = state.suppressNextNewErrorToast;
    state.suppressNextNewErrorToast = false;
    const count = asNumber(monitoring.count) ?? 0;
    const asOfDate = monitoring.asOfDate || state.overview.lastDataDate || "";
    const lastUpdated = monitoring.lastUpdated || state.overview.updatedAt || asOfDate;
    const formattedAsOfDate = formatFullDate(asOfDate);
    const exactMessage = count > 0
      ? `${formatPlainCount(count)}건의 새로운 오생성이 생겼습니다. (데이터: ${formattedAsOfDate} 기준)`
      : `선택한 조건에서 새로 확인된 오생성이 없습니다. (데이터: ${formattedAsOfDate} 기준)`;

    const countStrong = elements.newErrorCount?.querySelector("strong");
    const countCaption = elements.newErrorCount?.querySelector("span");
    if (countStrong) countStrong.textContent = formatCount(count);
    if (countCaption) countCaption.textContent = state.newErrorList.open ? "목록 닫기" : "목록 보기";
    if (elements.newErrorMessage) elements.newErrorMessage.textContent = exactMessage;
    if (elements.newErrorCount) {
      elements.newErrorCount.disabled = count <= 0;
      elements.newErrorCount.setAttribute("aria-label", `${exactMessage} 목록 ${state.newErrorList.open ? "닫기" : "보기"}`);
    }
    syncNewErrorDownload();
    if (count <= 0 && state.newErrorList.open) {
      setNewErrorListOpen(false);
    }
    elements.newErrorMonitor?.classList.toggle("has-new-errors", count > 0);

    if (count > 0) {
      const signature = [count, asOfDate, monitoring.sinceDate, lastUpdated, monitoring.downloadUrl].join("|");
      if (suppressToast) {
        state.dismissedNewErrorSignatures.add(signature);
      } else if (!state.dismissedNewErrorSignatures.has(signature) && state.activeToastSignature !== signature) {
        showToast("새 오생성 알림", exactMessage, { signature });
      }
    }
  }

  function showBriefing(index, { animate = true } = {}) {
    const messages = state.briefingMessages.length
      ? state.briefingMessages
      : ["선택한 조건의 현황을 확인할 수 없습니다."];
    state.briefingIndex = (index + messages.length) % messages.length;
    if (elements.briefingMessage) {
      elements.briefingMessage.classList.remove("is-changing");
      elements.briefingMessage.textContent = messages[state.briefingIndex];
      if (animate && !reducedMotion) {
        void elements.briefingMessage.offsetWidth;
        elements.briefingMessage.classList.add("is-changing");
      }
    }
    if (elements.briefingPosition) {
      elements.briefingPosition.textContent = `${state.briefingIndex + 1} / ${messages.length}`;
    }
  }

  function stopBriefingRotation() {
    if (state.briefingTimer) window.clearInterval(state.briefingTimer);
    state.briefingTimer = null;
  }

  function startBriefingRotation() {
    stopBriefingRotation();
    if (state.briefingMessages.length <= 1) return;
    state.briefingTimer = window.setInterval(() => {
      showBriefing(state.briefingIndex + 1);
    }, 4500);
  }

  function isBriefingAvailable() {
    return state.scope === "business"
      && !state.business
      && (state.timeMode === "year" || (state.timeMode === "month" && Boolean(state.month)));
  }

  function syncBriefingVisibility() {
    if (!elements.statusBriefing) return false;
    const available = isBriefingAvailable();
    elements.statusBriefing.hidden = !available;
    elements.statusBriefing.setAttribute("aria-hidden", String(!available));
    if (!available) {
      stopBriefingRotation();
      state.briefingMessages = [];
      state.briefingIndex = 0;
      if (elements.briefingMessage) elements.briefingMessage.textContent = "";
      if (elements.briefingPosition) elements.briefingPosition.textContent = "";
    }
    return available;
  }

  function renderBriefing() {
    if (!syncBriefingVisibility()) return;
    const summary = state.overview.summary;
    const comparison = state.overview.comparison;
    const messages = [];
    if (summary.rate === null) {
      messages.push("선택한 조건에 표시할 오생성 현황이 없습니다.");
    } else if (comparison.available) {
      const delta = comparison.summary.deltaRate;
      const deltaText = Math.abs(delta).toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const comparisonText = delta > 0
        ? `${deltaText}%p 상승했습니다.`
        : delta < 0 ? `${deltaText}%p 하락했습니다.` : "변동이 없습니다.";
      messages.push(`${Number(state.month)}월 오생성률은 ${formatRate(summary.rate)}로 전월보다 ${comparisonText}`);
    } else {
      messages.push(`${periodLabel().replace(" · 일별", "").replace(" · 월별", "")} 오생성률은 ${formatRate(summary.rate)}입니다.`);
    }

    const businessCauses = comparison.causes.business || [];
    if (comparison.available && businessCauses.length) {
      const biggest = [...businessCauses].sort((a, b) => Math.abs(b.deltaCount) - Math.abs(a.deltaCount))[0];
      const businessName = biggest.label.endsWith("사업부") ? biggest.label : `${biggest.label}사업부`;
      const action = biggest.deltaCount > 0 ? "증가" : biggest.deltaCount < 0 ? "감소" : "변동 없음";
      const amount = biggest.deltaCount === 0 ? "" : `${numberFormatter.format(Math.abs(biggest.deltaCount))}건 `;
      messages.push(`${businessName}는 전월 대비 ${amount}${action}로 사업부 중 변화폭이 가장 큽니다.`);
    } else {
      messages.push("전월 비교 자료가 반영되면 사업부별 주요 증감 원인을 알려드립니다.");
    }

    const topSubcategory = state.overview.rankings.subcategory?.count?.[0];
    if (topSubcategory) {
      messages.push(`${topSubcategory.label} 소분류가 오생성 ${formatCount(topSubcategory.count)}으로 가장 많이 발생했습니다.`);
    } else {
      messages.push("선택한 조건에 집계된 소분류 오생성이 없습니다.");
    }

    state.briefingMessages = messages.slice(0, 3);
    state.briefingIndex = 0;
    showBriefing(0, { animate: false });
    startBriefingRotation();
  }

  function showComparisonTooltip(item, eventOrPosition) {
    if (!elements.comparisonTooltip || !elements.comparisonChartWrap) return;
    elements.comparisonTooltip.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = item.label;
    const values = document.createElement("span");
    values.className = "tooltip-values";
    [
      ["이번 달", formatCount(item.currentCount)],
      ["전월", formatCount(item.previousCount)],
      ["증감", formatSignedCount(item.deltaCount)],
      ["오생성률", formatRate(item.currentRate)],
    ].forEach(([label, value]) => {
      const labelElement = document.createElement("span");
      labelElement.textContent = label;
      const valueElement = document.createElement("b");
      valueElement.textContent = value;
      values.append(labelElement, valueElement);
    });
    elements.comparisonTooltip.append(title, values);
    elements.comparisonTooltip.hidden = false;
    const wrapRect = elements.comparisonChartWrap.getBoundingClientRect();
    const x = eventOrPosition?.clientX !== undefined
      ? eventOrPosition.clientX - wrapRect.left
      : eventOrPosition?.x ?? wrapRect.width / 2;
    const y = eventOrPosition?.clientY !== undefined
      ? eventOrPosition.clientY - wrapRect.top
      : eventOrPosition?.y ?? wrapRect.height / 2;
    const tooltipRect = elements.comparisonTooltip.getBoundingClientRect();
    elements.comparisonTooltip.style.left = `${Math.max(6, Math.min(x + 12, wrapRect.width - tooltipRect.width - 6))}px`;
    elements.comparisonTooltip.style.top = `${Math.max(6, Math.min(y - tooltipRect.height - 12, wrapRect.height - tooltipRect.height - 6))}px`;
  }

  function renderComparisonChart() {
    if (!elements.comparisonChart) return;
    elements.comparisonChart.replaceChildren();
    if (elements.comparisonTooltip) elements.comparisonTooltip.hidden = true;
    const comparison = state.overview.comparison;
    const itemLimit = state.causeDimension === "subcategory" ? 8 : 10;
    const items = asArray(comparison.causes[state.causeDimension]).slice(0, itemLimit);
    const hasData = comparison.available && items.length > 0;
    elements.comparisonChart.hidden = !hasData;
    elements.comparisonEmpty.hidden = hasData;
    if (!hasData) return;
    const width = 760;
    const height = 420;
    const margins = { top: 52, right: 20, bottom: 72, left: 52 };
    const plotWidth = width - margins.left - margins.right;
    const plotHeight = height - margins.top - margins.bottom;
    const baselineY = margins.top + plotHeight / 2;
    const maxAbs = Math.max(1, ...items.map((item) => Math.abs(item.deltaCount)));
    const halfHeight = plotHeight / 2 - 8;
    const slotWidth = plotWidth / items.length;
    const barWidth = Math.min(32, Math.max(16, slotWidth * 0.26));

    [0.5, 1].forEach((ratio) => {
      const offset = halfHeight * ratio;
      [baselineY - offset, baselineY + offset].forEach((y) => {
        elements.comparisonChart.append(createSvgElement("line", {
          x1: margins.left,
          x2: width - margins.right,
          y1: y,
          y2: y,
          class: "comparison-grid-line",
        }));
      });
      const label = numberFormatter.format(Math.round(maxAbs * ratio));
      elements.comparisonChart.append(
        createSvgElement("text", { x: margins.left - 10, y: baselineY - offset + 4, "text-anchor": "end", class: "chart-axis-text" }, label),
        createSvgElement("text", { x: margins.left - 10, y: baselineY + offset + 4, "text-anchor": "end", class: "chart-axis-text" }, `−${label}`),
      );
    });
    elements.comparisonChart.append(createSvgElement("line", {
      x1: margins.left,
      x2: width - margins.right,
      y1: baselineY,
      y2: baselineY,
      class: "comparison-zero-line",
    }));

    items.forEach((item, index) => {
      const x = margins.left + slotWidth * index + (slotWidth - barWidth) / 2;
      const magnitude = Math.abs(item.deltaCount) / maxAbs * halfHeight;
      const visibleHeight = item.deltaCount === 0 ? 4 : Math.max(7, magnitude);
      const y = item.deltaCount >= 0 ? baselineY - visibleHeight : baselineY;
      const className = item.deltaCount > 0
        ? "comparison-bar is-positive"
        : item.deltaCount < 0 ? "comparison-bar is-negative" : "comparison-bar is-neutral";
      const bar = createSvgElement("rect", {
        x,
        y,
        width: barWidth,
        height: visibleHeight,
        rx: 5,
        class: className,
        tabindex: "0",
        role: "button",
        "aria-label": `${item.label}, 전월 대비 ${formatSignedCount(item.deltaCount)}`,
        style: `animation-delay:${index * 42}ms`,
      });
      const valueY = item.deltaCount >= 0 ? y - 9 : y + visibleHeight + 17;
      const value = createSvgElement("text", {
        x: x + barWidth / 2,
        y: valueY,
        "text-anchor": "middle",
        class: "comparison-value",
      }, item.deltaCount > 0 ? `+${numberFormatter.format(item.deltaCount)}` : numberFormatter.format(item.deltaCount));
      const shortLabel = item.label.length > 9 ? `${item.label.slice(0, 8)}…` : item.label;
      const label = createSvgElement("text", {
        x: x + barWidth / 2,
        y: height - 24,
        "text-anchor": "middle",
        class: "comparison-label",
      }, shortLabel);
      bar.addEventListener("mouseenter", (event) => showComparisonTooltip(item, event));
      bar.addEventListener("mousemove", (event) => showComparisonTooltip(item, event));
      bar.addEventListener("mouseleave", () => { elements.comparisonTooltip.hidden = true; });
      bar.addEventListener("focus", () => {
        const rect = elements.comparisonChart.getBoundingClientRect();
        showComparisonTooltip(item, { x: (x + barWidth / 2) / width * rect.width, y: y / height * rect.height });
      });
      bar.addEventListener("blur", () => { elements.comparisonTooltip.hidden = true; });
      elements.comparisonChart.append(bar, value, label);
    });
  }

  function renderPatterns() {
    const patterns = state.overview.patterns;
    elements.patternTotal.textContent = formatCount(patterns.total);
    elements.patternRepeatValue.textContent = `${formatCount(patterns.repeated)} · ${formatRate(patterns.repeatedRate)}`;
    elements.patternNewValue.textContent = `${formatCount(patterns.newCount)} · ${formatRate(patterns.newRate)}`;
    elements.patternRepeatBar.style.setProperty("--segment-width", `${Math.max(0, Math.min(100, patterns.repeatedRate))}%`);
    elements.patternNewBar.style.setProperty("--segment-width", `${Math.max(0, Math.min(100, patterns.newRate))}%`);
    elements.patternList.replaceChildren();
    patterns.items.forEach((item) => {
      const row = document.createElement("li");
      row.className = "pattern-item";
      const category = document.createElement("span");
      category.className = "pattern-category";
      category.textContent = item.subcategory;
      const signature = document.createElement("span");
      signature.className = "pattern-signature";
      signature.textContent = item.signature;
      signature.title = item.signature;
      const months = document.createElement("span");
      months.className = "pattern-months";
      item.months.forEach((month) => {
        const chip = document.createElement("span");
        chip.textContent = `${formatFullDate(month.month)} ${formatCount(month.count)}`;
        months.append(chip);
      });
      const count = document.createElement("strong");
      count.className = "pattern-count";
      count.textContent = formatCount(item.count);
      const date = document.createElement("span");
      date.className = "pattern-date";
      date.textContent = `최근 ${formatFullDate(item.lastDate)}`;
      row.append(category, signature, months, count, date);
      elements.patternList.append(row);
    });
    elements.patternEmpty.hidden = patterns.items.length > 0;
  }

  function renderBusinessOverview() {
    if (!elements.businessOverviewList) return;
    const items = asArray(state.overview.businessStatus?.items);
    elements.businessOverviewList.replaceChildren();
    elements.businessOverviewEmpty.hidden = items.length > 0;
    elements.businessOverviewPeriod.textContent = businessOverviewPeriodLabel();

    items.forEach((item, index) => {
      const delta = asNumber(item.deltaCount);
      const row = document.createElement("div");
      row.className = "business-overview-row";
      row.style.setProperty("--business-color", seriesColor(item.name, index));

      const name = document.createElement("div");
      name.className = "business-overview-name";
      const marker = document.createElement("i");
      marker.setAttribute("aria-hidden", "true");
      const nameText = document.createElement("span");
      const nameStrong = document.createElement("strong");
      nameStrong.textContent = item.name;
      const total = document.createElement("small");
      total.textContent = `전체 ${formatPlainCount(item.total)}`;
      nameText.append(nameStrong, total);
      name.append(marker, nameText);

      const errors = document.createElement("strong");
      errors.className = "business-overview-value";
      errors.textContent = formatCount(item.errors);

      const rate = document.createElement("strong");
      rate.className = "business-overview-value is-rate";
      rate.textContent = formatRate(item.rate);

      const change = document.createElement("strong");
      change.className = "business-overview-change";
      if (delta === null) {
        change.textContent = "비교 없음";
        change.classList.add("is-neutral");
      } else {
        change.textContent = formatSignedCount(delta);
        change.classList.add(delta > 0 ? "is-positive" : delta < 0 ? "is-negative" : "is-neutral");
      }
      row.append(name, errors, rate, change);
      elements.businessOverviewList.append(row);
    });
  }

  function renderInsightView() {
    const isChange = state.insightView === "change";
    elements.changeView.hidden = !isChange;
    elements.patternView.hidden = isChange;
    elements.insightCaption.textContent = isChange
      ? (state.overview.comparison.available
        ? `${compactPeriod(state.overview.comparison.currentPeriod)}와 ${compactPeriod(state.overview.comparison.previousPeriod)} 동기간 비교`
        : "선택 월과 전월의 오생성을 비교합니다.")
      : "반복 탐지와 신규 오생성의 구성을 확인합니다.";
    elements.insightViewTabs.querySelectorAll("button[data-insight-view]").forEach((button) => {
      const selected = button.dataset.insightView === state.insightView;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    if (isChange) renderComparisonChart();
    else renderPatterns();
  }

  function renderAnalysis() {
    renderInsightView();
  }

  function renderOverview() {
    updateFilterSummary();
    const summary = state.overview.summary;
    elements.metricTotal.textContent = formatPlainCount(summary.total);
    elements.metricErrors.textContent = formatPlainCount(summary.errors);
    elements.metricRate.textContent = formatRate(summary.rate);
    if (elements.metricErrorsNote) {
      elements.metricErrorsNote.textContent = `오생성률 ${formatRate(summary.rate)}`;
    }
    if (elements.metricRateNote) elements.metricRateNote.textContent = `오생성 ${formatCount(summary.errors)}`;
    const subject = subjectLabel();
    const comparison = state.overview.comparison;
    const deltaClassNames = ["is-positive", "is-negative", "is-neutral"];
    elements.subjectBusiness.classList.remove(...deltaClassNames);
    if (summary.errors === null || !comparison.available) {
      elements.subjectBusiness.textContent = "전월 비교 없음";
      elements.subjectBusiness.classList.add("is-neutral");
    } else {
      const delta = comparison.summary.deltaCount;
      elements.subjectBusiness.textContent = `전월 대비 ${formatSignedCount(delta)}`;
      elements.subjectBusiness.classList.add(
        delta > 0 ? "is-positive" : delta < 0 ? "is-negative" : "is-neutral"
      );
    }
    const summaryTooltip = [
      `대상: ${subject.name}${subject.detail ? ` (${subject.detail})` : ""}`,
      `기간: ${periodLabel()}`,
      `전체 데이터: ${formatCount(summary.total)}`,
      `오생성: ${formatCount(summary.errors)}`,
      `오생성률: ${formatRate(summary.rate)}`,
      "산식: 오생성 건수 ÷ 전체 데이터 건수 × 100",
    ].join("\n");
    document.querySelectorAll(".subject-summary, .metric-card").forEach((card) => {
      card.title = summaryTooltip;
      card.setAttribute("aria-label", summaryTooltip.replaceAll("\n", ", "));
    });
    const latestDataDate = state.overview.lastDataDate || state.overview.newErrors?.asOfDate;
    if (latestDataDate) {
      elements.updatedTime.textContent = `마지막 업데이트 ${formatFullDate(latestDataDate)}`;
    } else if (state.overview.updatedAt) {
      const date = new Date(state.overview.updatedAt);
      elements.updatedTime.textContent = Number.isNaN(date.getTime())
        ? `조회 ${state.overview.updatedAt}`
        : `조회 ${dateTimeFormatter.format(date)}`;
    } else {
      elements.updatedTime.textContent = "반영 자료 없음";
    }
    renderNewErrorMonitor();
    renderChart(summary.total === 0 && summary.errors === 0 ? [] : state.overview.series);
    renderBusinessOverview();
    renderAnalysis();
    renderRanking("subcategory");
    renderRanking("person");
    renderBriefing();
  }

  function createSvgElement(name, attributes = {}, text = "") {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text) element.textContent = text;
    return element;
  }

  function niceMaximum(value) {
    if (!Number.isFinite(value) || value <= 0) return 4;
    const integerStep = Math.max(1, Math.ceil(value * 1.06 / 4));
    return integerStep * 4;
  }

  function showChartTooltip(point, seriesName, eventOrPosition) {
    elements.chartTooltip.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = formatFullDate(point.date || point.label);
    const series = document.createElement("span");
    series.className = "tooltip-series";
    series.textContent = seriesName;
    const values = document.createElement("span");
    values.className = "tooltip-values";
    const tooltipRows = point.isTotal
      ? [["오생성", formatCount(point.count)]]
      : [["오생성", formatCount(point.count)], ["오생성률", formatRate(point.rate)], ["전체 데이터", formatCount(point.total)]];
    tooltipRows.forEach(([label, value]) => {
      const labelElement = document.createElement("span");
      labelElement.textContent = label;
      const valueElement = document.createElement("b");
      valueElement.textContent = value;
      values.append(labelElement, valueElement);
    });
    elements.chartTooltip.append(title, series, values);
    elements.chartTooltip.hidden = false;

    const wrapRect = elements.chartWrap.getBoundingClientRect();
    let x;
    let y;
    if (eventOrPosition?.clientX !== undefined) {
      x = eventOrPosition.clientX - wrapRect.left;
      y = eventOrPosition.clientY - wrapRect.top;
    } else {
      x = eventOrPosition?.x ?? wrapRect.width / 2;
      y = eventOrPosition?.y ?? wrapRect.height / 2;
    }
    const tooltipRect = elements.chartTooltip.getBoundingClientRect();
    const left = Math.max(6, Math.min(x + 13, wrapRect.width - tooltipRect.width - 6));
    const top = Math.max(6, Math.min(y - tooltipRect.height - 12, wrapRect.height - tooltipRect.height - 6));
    elements.chartTooltip.style.left = `${left}px`;
    elements.chartTooltip.style.top = `${top}px`;
  }

  function hideChartTooltip() {
    elements.chartTooltip.hidden = true;
  }

  function pointIsInSelectedPeriod(point) {
    const raw = String(point?.date || "").trim();
    const match = raw.match(/^(\d{4})[-./](\d{1,2})(?:[-./](\d{1,2}))?/);
    if (!match) return true;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3] || 1);
    const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    if (state.timeMode === "range") return (!state.start || iso >= state.start) && (!state.end || iso <= state.end);
    if (year !== Number(state.year)) return false;
    if (state.timeMode === "month" && state.month) return month === Number(state.month);
    return true;
  }

  function renderChart(series) {
    elements.chart.replaceChildren();
    elements.chartLegend.replaceChildren();
    hideChartTooltip();
    elements.trendViewTabs?.querySelectorAll("button[data-trend-view]").forEach((button) => {
      const selected = button.dataset.trendView === state.trendView;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    const validSeries = asArray(series)
      .map((item) => ({ ...item, points: asArray(item.points).filter(pointIsInSelectedPeriod) }))
      .filter((item) => item.points.length);
    const categories = [];
    const categorySet = new Set();
    validSeries.forEach((item) => item.points.forEach((point) => {
      const key = String(point.date || point.label);
      if (!categorySet.has(key)) {
        categorySet.add(key);
        categories.push({ key, label: point.label || formatDateLabel(key) });
      }
    }));
    const aggregatePoints = categories.map((category) => {
      let count = 0;
      let total = 0;
      validSeries.forEach((item) => {
        const point = item.points.find((candidate) => String(candidate.date || candidate.label) === category.key);
        count += asNumber(point?.count) ?? 0;
        total += asNumber(point?.total) ?? 0;
      });
      return {
        date: category.key,
        label: category.label,
        count,
        total,
        rate: total ? count / total * 100 : 0,
      };
    });
    const hasData = aggregatePoints.length > 0;
    elements.chartEmpty.hidden = hasData;
    elements.chart.hidden = !hasData;
    if (!hasData) return;

    const aggregateLabel = subjectLabel().name;
    const isMultiBusiness = state.scope === "business" && !state.business && validSeries.length > 1;
    const isStackedView = state.trendView === "stacked";
    elements.trendDescription.textContent = `${periodLabel().replace(" · ", " ")} ${isStackedView ? "누적 막대" : "시계열"} 추이`;
    const aggregateColor = state.scope === "business" && state.business
      ? seriesColor(validSeries[0]?.name || state.business)
      : "#1e3a62";
    const legendDefinitions = isMultiBusiness && !isStackedView
      ? [["전체 오생성", "#9eabba"], ...validSeries.map((item, index) => [item.name, seriesColor(item.name, index)])]
      : isMultiBusiness
        ? validSeries.map((item, index) => [item.name, seriesColor(item.name, index)])
      : [[aggregateLabel, aggregateColor]];
    legendDefinitions.forEach(([text, color]) => {
      const legend = document.createElement("span");
      legend.className = "legend-item";
      if (text === "전체 오생성") legend.classList.add("is-total");
      const mark = document.createElement("i");
      mark.style.background = color;
      const label = document.createElement("span");
      label.textContent = text;
      legend.append(mark, label);
      elements.chartLegend.append(legend);
    });

    const margins = { top: 20, right: 26, bottom: 47, left: 52 };
    const width = 1040;
    const height = 340;
    const plotWidth = width - margins.left - margins.right;
    const plotHeight = height - margins.top - margins.bottom;
    const maxY = niceMaximum(Math.max(...aggregatePoints.map((point) => point.count)));
    const slotWidth = plotWidth / Math.max(1, categories.length);
    const xFor = (index) => margins.left + slotWidth * index + slotWidth / 2;
    const yFor = (value) => margins.top + plotHeight - Math.max(0, value) / maxY * plotHeight;

    for (let tick = 0; tick <= 4; tick += 1) {
      const value = maxY / 4 * tick;
      const y = yFor(value);
      elements.chart.append(createSvgElement("line", {
        x1: margins.left,
        x2: width - margins.right,
        y1: y,
        y2: y,
        class: tick === 0 ? "chart-axis-line" : "chart-grid-line",
      }));
      elements.chart.append(createSvgElement("text", {
        x: margins.left - 11,
        y: y + 4,
        "text-anchor": "end",
        class: "chart-axis-text",
      }, numberFormatter.format(Math.round(value))));
    }

    const maximumLabels = window.innerWidth < 620 ? 6 : 12;
    const labelStep = Math.max(1, Math.ceil(categories.length / maximumLabels));
    categories.forEach((category, index) => {
      if (index % labelStep !== 0 && index !== categories.length - 1) return;
      elements.chart.append(createSvgElement("text", {
        x: xFor(index),
        y: height - 17,
        "text-anchor": "middle",
        class: "chart-axis-text",
      }, category.label));
    });

    if (isMultiBusiness && !isStackedView) {
      const defs = createSvgElement("defs");
      const gradient = createSvgElement("linearGradient", {
        id: "total-area-gradient",
        x1: "0",
        y1: "0",
        x2: "0",
        y2: "1",
      });
      gradient.append(
        createSvgElement("stop", { offset: "0%", "stop-color": "#b6c0cb", "stop-opacity": "0.46" }),
        createSvgElement("stop", { offset: "100%", "stop-color": "#e8edf2", "stop-opacity": "0.1" })
      );
      defs.append(gradient);
      elements.chart.append(defs);

      const baselineY = yFor(0);
      const totalCoordinates = aggregatePoints.map((point, index) => [xFor(index), yFor(point.count)]);
      const totalLine = totalCoordinates.map(([x, y], index) => `${index ? "L" : "M"}${x} ${y}`).join(" ");
      const totalArea = [
        `M${totalCoordinates[0][0]} ${baselineY}`,
        ...totalCoordinates.map(([x, y]) => `L${x} ${y}`),
        `L${totalCoordinates[totalCoordinates.length - 1][0]} ${baselineY}`,
        "Z",
      ].join(" ");
      elements.chart.append(createSvgElement("path", { d: totalArea, class: "chart-total-area" }));
      elements.chart.append(createSvgElement("path", { d: totalLine, class: "chart-total-line" }));
      aggregatePoints.forEach((point, index) => {
        const x = xFor(index);
        const y = yFor(point.count);
        const totalPoint = { ...point, isTotal: true };
        const hit = createSvgElement("circle", {
          cx: x,
          cy: y,
          r: 10,
          class: "chart-total-point-hit",
          tabindex: "0",
          role: "button",
          "aria-label": `${point.label}, 전체 오생성 ${formatCount(point.count)}`,
        });
        hit.addEventListener("mouseenter", (event) => showChartTooltip(totalPoint, "전체 오생성", event));
        hit.addEventListener("mousemove", (event) => showChartTooltip(totalPoint, "전체 오생성", event));
        hit.addEventListener("mouseleave", hideChartTooltip);
        hit.addEventListener("focus", () => showChartTooltip(totalPoint, "전체 오생성", { x: x / width * elements.chart.getBoundingClientRect().width, y: y / height * elements.chart.getBoundingClientRect().height }));
        hit.addEventListener("blur", hideChartTooltip);
        elements.chart.append(hit);
        elements.chart.append(createSvgElement("circle", { cx: x, cy: y, r: 2.7, class: "chart-total-point" }));
      });

      const maps = validSeries.map((item) => new Map(
        item.points.map((point) => [String(point.date || point.label), point])
      ));
      validSeries.forEach((item, seriesIndex) => {
        const color = seriesColor(item.name, seriesIndex);
        const points = categories.map((category) => {
          const source = maps[seriesIndex].get(category.key);
          return source || { date: category.key, label: category.label, count: 0, total: 0, rate: 0 };
        });
        const isZeroSeries = !points.some((point) => (asNumber(point.count) ?? 0) > 0);
        const path = points.map((point, index) => `${index ? "L" : "M"}${xFor(index)} ${yFor(asNumber(point.count) ?? 0)}`).join(" ");
        elements.chart.append(createSvgElement("path", {
          d: path,
          class: `chart-series-line${isZeroSeries ? " is-zero" : ""}`,
          style: `stroke:${color};animation-delay:${seriesIndex * 90}ms`,
        }));
        points.forEach((point, index) => {
          const x = xFor(index);
          const y = yFor(asNumber(point.count) ?? 0);
          const hit = createSvgElement("circle", {
            cx: x,
            cy: y,
            r: 9,
            class: "chart-point-hit",
            tabindex: "0",
            role: "button",
            "aria-label": `${point.label}, ${item.name}, 오생성 ${formatCount(point.count)}`,
          });
          hit.addEventListener("mouseenter", (event) => showChartTooltip(point, item.name, event));
          hit.addEventListener("mousemove", (event) => showChartTooltip(point, item.name, event));
          hit.addEventListener("mouseleave", hideChartTooltip);
          hit.addEventListener("focus", () => showChartTooltip(point, item.name, { x: x / width * elements.chart.getBoundingClientRect().width, y: y / height * elements.chart.getBoundingClientRect().height }));
          hit.addEventListener("blur", hideChartTooltip);
          elements.chart.append(hit);
          elements.chart.append(createSvgElement("circle", {
            cx: x,
            cy: y,
            r: (asNumber(point.count) ?? 0) === 0 ? 2.4 : 3.5,
            class: `chart-point${(asNumber(point.count) ?? 0) === 0 ? " is-zero" : ""}`,
            style: `fill:${color};animation-delay:${seriesIndex * 90 + index * 14}ms`,
          }));
        });
      });
      return;
    }

    if (isMultiBusiness && isStackedView) {
      const maps = validSeries.map((item) => new Map(
        item.points.map((point) => [String(point.date || point.label), point])
      ));
      const barWidth = Math.min(44, Math.max(17, slotWidth * 0.68));
      aggregatePoints.forEach((aggregate, index) => {
        const xCenter = xFor(index);
        let stackedCount = 0;
        validSeries.forEach((item, seriesIndex) => {
          const source = maps[seriesIndex].get(String(aggregate.date));
          const point = source || {
            date: aggregate.date,
            label: aggregate.label,
            count: 0,
            total: 0,
            rate: 0,
          };
          const count = asNumber(point.count) ?? 0;
          if (count <= 0) return;
          const segmentBottom = yFor(stackedCount);
          stackedCount += count;
          const segmentTop = yFor(stackedCount);
          const segment = createSvgElement("rect", {
            x: xCenter - barWidth / 2,
            y: segmentTop,
            width: barWidth,
            height: Math.max(1, segmentBottom - segmentTop),
            rx: 3,
            class: "chart-count-bar chart-business-bar",
            tabindex: "0",
            role: "button",
            "aria-label": `${aggregate.label}, ${item.name}, 오생성 ${formatCount(count)}`,
            style: `fill:${seriesColor(item.name, seriesIndex)};animation-delay:${index * 22 + seriesIndex * 18}ms`,
          });
          segment.addEventListener("mouseenter", (event) => showChartTooltip(point, item.name, event));
          segment.addEventListener("mousemove", (event) => showChartTooltip(point, item.name, event));
          segment.addEventListener("mouseleave", hideChartTooltip);
          segment.addEventListener("focus", () => showChartTooltip(point, item.name, {
            x: xCenter / width * elements.chart.getBoundingClientRect().width,
            y: segmentTop / height * elements.chart.getBoundingClientRect().height,
          }));
          segment.addEventListener("blur", hideChartTooltip);
          elements.chart.append(segment);
        });
        if (stackedCount === 0) {
          elements.chart.append(createSvgElement("rect", {
            x: xCenter - barWidth / 2,
            y: yFor(0) - 2,
            width: barWidth,
            height: 2,
            rx: 2,
            class: "chart-count-bar is-zero",
          }));
        }
      });
      return;
    }

    const barWidth = Math.min(44, Math.max(17, slotWidth * 0.68));
    aggregatePoints.forEach((point, index) => {
      const xCenter = xFor(index);
      const y = yFor(point.count);
      const baselineY = yFor(0);
      const visibleHeight = point.count === 0 ? 2 : Math.max(4, baselineY - y);
      const bar = createSvgElement("rect", {
        x: xCenter - barWidth / 2,
        y: baselineY - visibleHeight,
        width: barWidth,
        height: visibleHeight,
        rx: 4,
        class: `chart-count-bar${point.count === 0 ? " is-zero" : ""}`,
        tabindex: "0",
        role: "button",
        "aria-label": `${point.label}, 오생성 ${formatCount(point.count)}, 오생성률 ${formatRate(point.rate)}`,
        style: `fill:${aggregateColor};animation-delay:${index * 22}ms`,
      });
      bar.addEventListener("mouseenter", (event) => showChartTooltip(point, aggregateLabel, event));
      bar.addEventListener("mousemove", (event) => showChartTooltip(point, aggregateLabel, event));
      bar.addEventListener("mouseleave", hideChartTooltip);
      bar.addEventListener("focus", () => showChartTooltip(point, aggregateLabel, { x: xCenter / width * elements.chart.getBoundingClientRect().width, y: y / height * elements.chart.getBoundingClientRect().height }));
      bar.addEventListener("blur", hideChartTooltip);
      elements.chart.append(bar);
    });
  }

  function renderRanking(type) {
    const metric = state.rankingMetric[type];
    const isRate = metric === "rate";
    const group = state.overview.rankings[type] || { count: [], rate: [] };
    const items = asArray(group[metric]);
    const list = type === "subcategory" ? elements.subcategoryList : elements.personList;
    const empty = type === "subcategory" ? elements.subcategoryEmpty : elements.personEmpty;
    const caption = type === "subcategory" ? elements.subcategoryCaption : elements.personCaption;
    caption.textContent = isRate ? "오생성률" : "오생성 건수";
    list.replaceChildren();
    empty.hidden = items.length > 0;
    list.hidden = items.length === 0;
    if (!items.length) return;

    const maximum = Math.max(...items.map((item) => isRate ? (item.rate || 0) : (item.count || 0)), 0);
    items.forEach((item, index) => {
      const value = isRate ? item.rate : item.count;
      const row = document.createElement("li");
      row.className = "ranking-row";
      if (index < 3) row.classList.add(`is-top-${index + 1}`);
      row.tabIndex = 0;

      const rank = document.createElement("span");
      rank.className = "ranking-number";
      rank.textContent = String(index + 1);

      const name = document.createElement("span");
      name.className = "ranking-name";
      const strong = document.createElement("strong");
      strong.textContent = item.label;
      name.append(strong);
      if (item.business) {
        const business = document.createElement("small");
        business.textContent = item.business;
        name.append(business);
      }

      const track = document.createElement("span");
      track.className = "ranking-track";
      const bar = document.createElement("span");
      bar.className = "ranking-bar";
      const ratio = maximum > 0 && value > 0 ? Math.max(2, value / maximum * 100) : 0;
      bar.style.setProperty("--bar-width", `${ratio}%`);
      track.append(bar);

      const valueElement = document.createElement("span");
      valueElement.className = "ranking-value";
      const primaryValue = document.createElement("strong");
      primaryValue.textContent = isRate ? formatRate(item.rate) : formatCount(item.count);
      const secondaryValue = document.createElement("small");
      secondaryValue.textContent = isRate ? formatCount(item.count) : formatRate(item.rate);
      valueElement.append(primaryValue, secondaryValue);

      const tooltip = document.createElement("span");
      tooltip.className = "ranking-tooltip";
      [["대상", subjectLabel().name], ["오생성", formatCount(item.count)], ["오생성률", formatRate(item.rate)], ["전체 데이터", formatCount(item.total)]].forEach(([label, formatted]) => {
        const line = document.createElement("span");
        line.textContent = label;
        const bold = document.createElement("b");
        bold.textContent = formatted;
        line.append(bold);
        tooltip.append(line);
      });

      row.setAttribute("aria-label", `${index + 1}위 ${item.label}, 대상 ${subjectLabel().name}, 오생성 ${formatCount(item.count)}, 오생성률 ${formatRate(item.rate)}, 전체 데이터 ${formatCount(item.total)}`);
      row.append(rank, name, track, valueElement, tooltip);
      list.append(row);
    });
  }

  function updateScopeUi() {
    elements.scopeControl.querySelectorAll("button").forEach((button) => {
      const buttonScope = button.dataset.scope === "division" ? "business" : button.dataset.scope;
      const selected = buttonScope === state.scope;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    elements.personFilter.hidden = state.scope !== "person";
    elements.businessFilter.hidden = state.scope !== "business";
  }

  function updatePeriodUi() {
    elements.periodControl.querySelectorAll("button").forEach((button) => {
      const selected = button.dataset.period === (state.timeMode === "range" ? "range" : "month");
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    elements.monthInputs.hidden = state.timeMode === "range";
    elements.rangeInputs.hidden = state.timeMode !== "range";
    elements.yearSelect.value = String(state.year);
    elements.monthSelect.value = state.month;
  }

  function setActivePersonOption(option) {
    const options = [...elements.personOptions.querySelectorAll(".person-option")];
    options.forEach((candidate) => {
      const active = candidate === option;
      candidate.classList.toggle("is-focused", active);
      candidate.setAttribute("aria-selected", String(active));
    });
    if (option?.id) elements.personSearch.setAttribute("aria-activedescendant", option.id);
    else elements.personSearch.removeAttribute("aria-activedescendant");
  }

  function clearActivePersonOption({ restoreCommittedSelection = true } = {}) {
    elements.personSearch.removeAttribute("aria-activedescendant");
    elements.personOptions.querySelectorAll(".person-option").forEach((option) => {
      option.classList.remove("is-focused");
      const committed = restoreCommittedSelection
        && state.person
        && option.dataset.personId === state.person.id;
      option.setAttribute("aria-selected", String(Boolean(committed)));
    });
  }

  function renderPersonOptions(query) {
    const normalized = query.trim().toLocaleLowerCase("ko-KR");
    const matches = state.people.filter((person) => {
      if (!normalized) return true;
      return `${person.name} ${person.business}`.toLocaleLowerCase("ko-KR").includes(normalized);
    }).slice(0, 10);
    elements.personOptions.replaceChildren();
    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = state.people.length ? "일치하는 인원이 없습니다." : "반영된 인원 자료가 없습니다.";
      elements.personOptions.append(empty);
    } else {
      matches.forEach((person, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "person-option";
        option.id = `person-option-${index}`;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.dataset.personId = person.id;
        const name = document.createElement("strong");
        name.textContent = person.name;
        const business = document.createElement("span");
        business.textContent = person.business || "사업부 미지정";
        option.append(name, business);
        option.addEventListener("mousedown", (event) => event.preventDefault());
        option.addEventListener("click", () => selectPerson(person));
        elements.personOptions.append(option);
      });
    }
    elements.personOptions.hidden = false;
    elements.personSearch.setAttribute("aria-expanded", "true");
    const selected = [...elements.personOptions.querySelectorAll(".person-option")]
      .find((option) => state.person && option.dataset.personId === state.person.id);
    if (selected) setActivePersonOption(selected);
    else clearActivePersonOption({ restoreCommittedSelection: false });
  }

  function closePersonOptions() {
    elements.personOptions.hidden = true;
    elements.personSearch.setAttribute("aria-expanded", "false");
    clearActivePersonOption();
  }

  function selectPerson(person) {
    state.person = person;
    elements.personSearch.value = person.name;
    elements.clearPerson.hidden = false;
    closePersonOptions();
    loadOverview();
  }

  function clearPersonSelection() {
    state.person = null;
    elements.personSearch.value = "";
    elements.clearPerson.hidden = true;
    closePersonOptions();
    clearOverview();
    updateFilterSummary();
    elements.personSearch.focus();
  }

  function defaultDateRange() {
    const boundedEnd = state.dateBounds.end ? new Date(`${state.dateBounds.end}T00:00:00`) : null;
    const end = boundedEnd && !Number.isNaN(boundedEnd.getTime()) ? boundedEnd : new Date();
    const start = new Date(end.getFullYear(), end.getMonth(), 1);
    const toIso = (date) => {
      const adjusted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
      return adjusted.toISOString().slice(0, 10);
    };
    const startValue = toIso(start);
    return {
      start: state.dateBounds.start && startValue < state.dateBounds.start ? state.dateBounds.start : startValue,
      end: toIso(end),
    };
  }

  function resetAllFilters() {
    state.scope = "business";
    state.timeMode = "year";
    state.year = state.years.includes(now.getFullYear()) ? now.getFullYear() : state.years[0];
    state.month = "";
    state.start = "";
    state.end = "";
    state.business = "";
    state.person = null;
    elements.personSearch.value = "";
    elements.clearPerson.hidden = true;
    elements.businessSelect.value = "";
    elements.startDate.value = "";
    elements.endDate.value = "";
    updateScopeUi();
    updatePeriodUi();
    loadOverview();
  }

  function showToast(title, message, options = {}) {
    const normalizedOptions = options && typeof options === "object" ? options : {};
    state.activeToastSignature = String(normalizedOptions.signature || "");
    elements.toastTitle.textContent = title;
    elements.toastMessage.textContent = message;
    elements.toast.hidden = false;
  }

  function syncStickyFilter() {
    if (!elements.filterPanel || !elements.filterStickyAnchor) return;
    const stuck = elements.filterStickyAnchor.getBoundingClientRect().top <= 10;
    elements.filterPanel.classList.toggle("is-stuck", stuck);
  }

  async function showEntryNotification() {
    try {
      const response = await fetchJson(API.notification, {}, 5000);
      const notification = response?.notification || response || {};
      state.lastEventId = asNumber(firstDefined(notification, ["id", "event_id"], 0)) || 0;
      const title = firstDefined(notification, ["title", "subject"], "서비스오더에 접속했습니다.");
      const message = firstDefined(notification, ["message", "body", "text"], "최신 반영 현황을 확인해 주세요.");
      showToast(String(title), String(message));
    } catch (_) {
      showToast("서비스오더에 접속했습니다.", "최신 반영 현황을 확인하고 있습니다.");
    }
  }

  function scheduleDashboardRefresh(message = "", { suppressNewErrorToast = false } = {}) {
    state.liveRefreshPending = true;
    if (message) state.liveRefreshMessage = message;
    state.liveRefreshSuppressToast = (
      state.liveRefreshSuppressToast || suppressNewErrorToast
    );
    window.clearTimeout(state.liveRefreshTimer);
    const flushRefresh = async () => {
      if (state.liveRefreshInFlight) {
        state.liveRefreshTimer = window.setTimeout(flushRefresh, 80);
        return;
      }
      state.liveRefreshInFlight = true;
      try {
        while (state.liveRefreshPending) {
          state.liveRefreshPending = false;
          const refreshMessage = state.liveRefreshMessage;
          const suppressToast = state.liveRefreshSuppressToast;
          state.liveRefreshMessage = "";
          state.liveRefreshSuppressToast = false;
          await loadFilters();
          await loadOverview({
            silent: true,
            suppressNewErrorToast: suppressToast,
          });
          if (
            refreshMessage
            && (asNumber(state.overview.newErrors?.count) ?? 0) <= 0
          ) {
            showToast(
              "대시보드를 최신 상태로 반영했습니다.",
              refreshMessage,
            );
          }
        }
      } finally {
        state.liveRefreshInFlight = false;
        if (state.liveRefreshPending) {
          state.liveRefreshTimer = window.setTimeout(flushRefresh, 80);
        }
      }
    };
    state.liveRefreshTimer = window.setTimeout(flushRefresh, 180);
  }

  function connectLiveEvents() {
    if (!("EventSource" in window)) return;
    state.eventSource?.close();
    const eventUrl = state.lastEventId ? `${API.events}?after=${state.lastEventId}` : API.events;
    const source = new EventSource(eventUrl);
    state.eventSource = source;
    const handleMessage = (event) => {
      let payload = {};
      try { payload = JSON.parse(event.data || "{}"); } catch (_) { payload = { message: event.data }; }
      if (["ping", "heartbeat", "connected"].includes(payload.type)) return;
      const eventType = event.type === "message" ? String(payload.type || "message") : event.type;
      const isRollback = eventType === "error_approval_rolled_back";
      state.lastEventId = Math.max(state.lastEventId, asNumber(firstDefined(payload, ["id", "event_id"], 0)) || 0);
      scheduleDashboardRefresh(
        String(payload.message || "관리자 페이지의 오생성 반영 결과를 불러왔습니다."),
        { suppressNewErrorToast: isRollback },
      );
    };
    source.addEventListener("dashboard_updated", handleMessage);
    source.addEventListener("service_order_updated", handleMessage);
    source.addEventListener("analysis_completed", handleMessage);
    source.addEventListener("review_updated", handleMessage);
    source.addEventListener("new_errors_approved", handleMessage);
    source.addEventListener("error_approval_rolled_back", handleMessage);
    source.addEventListener("confirmed_errors_excluded", handleMessage);
    source.addEventListener("confirmed_errors_restored", handleMessage);
    source.onmessage = handleMessage;
  }

  function bindEvents() {
    let newErrorSearchTimer = 0;
    let newErrorValueTimer = 0;
    elements.briefingPrev?.addEventListener("click", () => {
      showBriefing(state.briefingIndex - 1);
      startBriefingRotation();
    });
    elements.briefingNext?.addEventListener("click", () => {
      showBriefing(state.briefingIndex + 1);
      startBriefingRotation();
    });
    elements.insightViewTabs?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-insight-view]");
      if (!button) return;
      state.insightView = button.dataset.insightView;
      renderInsightView();
    });
    elements.trendViewTabs?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-trend-view]");
      if (!button) return;
      state.trendView = button.dataset.trendView === "stacked" ? "stacked" : "line";
      renderChart(state.overview.series);
    });
    elements.causeSwitch?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-cause]");
      if (!button) return;
      state.causeDimension = button.dataset.cause;
      elements.causeSwitch.querySelectorAll("button[data-cause]").forEach((candidate) => {
        const selected = candidate === button;
        candidate.classList.toggle("is-selected", selected);
        candidate.setAttribute("aria-pressed", String(selected));
      });
      renderComparisonChart();
    });
    elements.scopeControl.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-scope]");
      if (!button) return;
      state.scope = button.dataset.scope === "division" ? "business" : button.dataset.scope;
      updateScopeUi();
      loadOverview();
    });

    elements.periodControl.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-period]");
      if (!button) return;
      if (button.dataset.period === "range") {
        state.timeMode = "range";
        if (!state.start || !state.end) {
          const range = defaultDateRange();
          state.start = range.start;
          state.end = range.end;
          elements.startDate.value = state.start;
          elements.endDate.value = state.end;
        }
      } else {
        state.month = "";
        state.timeMode = "year";
      }
      updatePeriodUi();
      loadOverview();
    });

    elements.personSearch.addEventListener("input", () => {
      if (state.person && elements.personSearch.value !== state.person.name) {
        state.person = null;
        elements.clearPerson.hidden = true;
        clearOverview();
      }
      renderPersonOptions(elements.personSearch.value);
    });
    elements.personSearch.addEventListener("focus", () => renderPersonOptions(elements.personSearch.value));
    elements.personSearch.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closePersonOptions();
        return;
      }
      if (["ArrowDown", "ArrowUp"].includes(event.key) && elements.personOptions.hidden) {
        renderPersonOptions(elements.personSearch.value);
      }
      const options = [...elements.personOptions.querySelectorAll(".person-option")];
      const activeId = elements.personSearch.getAttribute("aria-activedescendant");
      const activeOption = activeId ? options.find((option) => option.id === activeId) : null;
      if (event.key === "Enter" && !elements.personOptions.hidden && options.length) {
        event.preventDefault();
        (activeOption || options[0]).click();
        return;
      }
      if (!["ArrowDown", "ArrowUp"].includes(event.key) || !options.length) return;
      event.preventDefault();
      const current = activeOption ? options.indexOf(activeOption) : -1;
      const next = event.key === "ArrowDown"
        ? (current + 1) % options.length
        : (current <= 0 ? options.length - 1 : current - 1);
      setActivePersonOption(options[next]);
      options[next].scrollIntoView({ block: "nearest" });
    });
    elements.personSearch.addEventListener("blur", () => window.setTimeout(closePersonOptions, 120));
    elements.clearPerson.addEventListener("click", clearPersonSelection);

    elements.businessSelect.addEventListener("change", () => {
      state.business = elements.businessSelect.value;
      loadOverview();
    });
    elements.yearSelect.addEventListener("change", () => {
      state.year = Number(elements.yearSelect.value);
      loadOverview();
    });
    elements.monthSelect.addEventListener("change", () => {
      state.month = elements.monthSelect.value;
      state.timeMode = state.month ? "month" : "year";
      updatePeriodUi();
      loadOverview();
    });
    elements.startDate.addEventListener("change", () => {
      state.start = elements.startDate.value;
      loadOverview();
    });
    elements.endDate.addEventListener("change", () => {
      state.end = elements.endDate.value;
      loadOverview();
    });
    elements.resetFilters.addEventListener("click", resetAllFilters);
    elements.newErrorCount?.addEventListener("click", () => {
      if (!elements.newErrorCount.disabled) setNewErrorListOpen(!state.newErrorList.open);
    });
    elements.newErrorDownload?.addEventListener("click", (event) => {
      if (elements.newErrorDownload.getAttribute("aria-disabled") === "true") event.preventDefault();
    });
    elements.newErrorListClose?.addEventListener("click", () => {
      setNewErrorListOpen(false);
      elements.newErrorCount.focus();
    });
    elements.newErrorListSearch?.addEventListener("input", () => {
      window.clearTimeout(newErrorSearchTimer);
      state.newErrorList.downloadUrl = "";
      syncNewErrorDownload();
      newErrorSearchTimer = window.setTimeout(() => {
        state.newErrorList.search = elements.newErrorListSearch.value.trim();
        loadNewErrorList({ resetPage: true });
      }, 280);
    });
    elements.newErrorListSearch?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      window.clearTimeout(newErrorSearchTimer);
      state.newErrorList.search = elements.newErrorListSearch.value.trim();
      loadNewErrorList({ resetPage: true });
    });
    elements.newErrorFilterReset?.addEventListener("click", () => {
      state.newErrorList.columnFilters = {};
      closeNewErrorFilterMenu();
      loadNewErrorList({ resetPage: true });
    });
    elements.newErrorFilterClose?.addEventListener("click", () => {
      closeNewErrorFilterMenu({ restoreFocus: true });
    });
    elements.newErrorFilterApply?.addEventListener("click", () => {
      const column = state.newErrorList.activeFilterColumn;
      if (column) applyNewErrorColumnFilter(column, [...state.newErrorList.filterDraftValues]);
    });
    elements.newErrorFilterClear?.addEventListener("click", () => {
      const column = state.newErrorList.activeFilterColumn;
      if (column) applyNewErrorColumnFilter(column, []);
    });
    elements.newErrorFilterInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const column = state.newErrorList.activeFilterColumn;
        if (column) applyNewErrorColumnFilter(column, [...state.newErrorList.filterDraftValues]);
      } else if (event.key === "Escape") {
        closeNewErrorFilterMenu({ restoreFocus: true });
      }
    });
    elements.newErrorFilterInput?.addEventListener("input", () => {
      window.clearTimeout(newErrorValueTimer);
      const column = state.newErrorList.activeFilterColumn;
      if (!column) return;
      newErrorValueTimer = window.setTimeout(() => {
        loadNewErrorFilterValues(column, elements.newErrorFilterInput.value);
      }, 220);
    });
    elements.newErrorFilterSelectAll?.addEventListener("change", () => {
      const list = state.newErrorList;
      list.filterValues.forEach((value) => {
        if (elements.newErrorFilterSelectAll.checked) list.filterDraftValues.add(value);
        else list.filterDraftValues.delete(value);
      });
      renderNewErrorFilterValues(list.activeFilterColumn, list.filterValues, { source: "all" });
    });
    elements.newErrorTableShell?.addEventListener("scroll", () => closeNewErrorFilterMenu(), { passive: true });
    elements.newErrorFirstPage?.addEventListener("click", () => {
      state.newErrorList.page = 1;
      loadNewErrorList();
    });
    elements.newErrorPrevPage?.addEventListener("click", () => {
      state.newErrorList.page = Math.max(1, state.newErrorList.page - 1);
      loadNewErrorList();
    });
    elements.newErrorNextPage?.addEventListener("click", () => {
      state.newErrorList.page = Math.min(state.newErrorList.totalPages, state.newErrorList.page + 1);
      loadNewErrorList();
    });
    elements.newErrorLastPage?.addEventListener("click", () => {
      state.newErrorList.page = state.newErrorList.totalPages;
      loadNewErrorList();
    });
    document.addEventListener("pointerdown", (event) => {
      if (elements.newErrorFilterMenu.hidden) return;
      if (elements.newErrorFilterMenu.contains(event.target) || event.target.closest(".new-error-column-filter")) return;
      closeNewErrorFilterMenu();
    });
    elements.closeToast.addEventListener("click", () => {
      if (state.activeToastSignature) state.dismissedNewErrorSignatures.add(state.activeToastSignature);
      state.activeToastSignature = "";
      elements.toast.hidden = true;
    });

    document.querySelectorAll(".ranking-panel").forEach((panel) => {
      panel.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-metric]");
        if (!button) return;
        const type = panel.dataset.ranking;
        state.rankingMetric[type] = button.dataset.metric;
        panel.querySelectorAll("button[data-metric]").forEach((candidate) => {
          const selected = candidate === button;
          candidate.classList.toggle("is-selected", selected);
          candidate.setAttribute("aria-pressed", String(selected));
        });
        renderRanking(type);
      });
    });

    window.addEventListener("beforeunload", () => {
      state.eventSource?.close();
      stopBriefingRotation();
    });
    window.addEventListener("scroll", syncStickyFilter, { passive: true });
    window.addEventListener("storage", (event) => {
      if (event.key === "service_order.dashboard_refresh" && event.newValue) {
        scheduleDashboardRefresh("관리자 페이지의 승인 결과를 반영했습니다.");
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        checkServer();
        scheduleDashboardRefresh();
      }
    });
    window.addEventListener("resize", () => {
      closeNewErrorFilterMenu();
      if (state.overview.series.length) renderChart(state.overview.series);
      if (state.insightView === "change") renderComparisonChart();
      syncStickyFilter();
    });
  }

  async function initialize() {
    bindEvents();
    syncStickyFilter();
    updateScopeUi();
    updatePeriodUi();
    updateFilterSummary();
    renderOverview();
    showToast("서비스오더에 접속했습니다.", "최신 반영 현황을 확인하고 있습니다.");
    await Promise.all([checkServer(), loadFilters(), showEntryNotification()]);
    updatePeriodUi();
    updateFilterSummary();
    if (state.scope === "business") await loadOverview();
    connectLiveEvents();
    window.setInterval(checkServer, 15000);
    window.setInterval(() => {
      if (!document.hidden) scheduleDashboardRefresh();
    }, 30000);
  }

  initialize();
})();
