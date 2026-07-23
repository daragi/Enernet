(() => {
  const form = document.querySelector("#analysis-form");
  if (!form) return;

  const elements = {
    fileInput: document.querySelector("#order-file"),
    uploadZone: document.querySelector("#upload-zone"),
    selectedFile: document.querySelector("#selected-file"),
    submitButton: document.querySelector("#analyze-button"),
    processPanel: document.querySelector("#process-panel"),
    processTitle: document.querySelector("#process-title"),
    processMessage: document.querySelector("#process-message"),
    processProgressTrack: document.querySelector(".process-progress-track"),
    processProgressBar: document.querySelector("#process-progress-bar"),
    processPercent: document.querySelector("#process-percent"),
    resultPanel: document.querySelector("#result-panel"),
    errorPanel: document.querySelector("#error-panel"),
    workspace: document.querySelector("#candidate-workspace"),
    sheet: document.querySelector("#candidate-sheet"),
    sheetScroll: document.querySelector("#sheet-scroll"),
    loading: document.querySelector("#sheet-loading"),
    empty: document.querySelector("#sheet-empty"),
    table: document.querySelector("#candidate-table"),
    head: document.querySelector("#candidate-head"),
    body: document.querySelector("#candidate-body"),
    gridTitle: document.querySelector("#candidate-grid-title"),
    gridDescription: document.querySelector("#candidate-grid-description"),
    datasetTabs: [...document.querySelectorAll(".dataset-tab")],
    candidateTabCount: document.querySelector("#candidate-tab-count"),
    errorTabCount: document.querySelector("#error-tab-count"),
    selectionSummary: document.querySelector("#selection-summary"),
    selectionMode: document.querySelector("#selection-mode"),
    checkedCount: document.querySelector("#checked-count"),
    candidateTotal: document.querySelector("#candidate-total"),
    pageRange: document.querySelector("#page-range"),
    pageLabel: document.querySelector("#page-label"),
    prevButton: document.querySelector("#page-prev"),
    nextButton: document.querySelector("#page-next"),
    refreshButton: document.querySelector("#grid-refresh"),
    fullscreenButton: document.querySelector("#grid-fullscreen"),
    clearFiltersButton: document.querySelector("#grid-clear-filters"),
    approveButton: document.querySelector("#approve-selection"),
    rollbackButton: document.querySelector("#rollback-last"),
    excludeErrorsButton: document.querySelector("#exclude-errors"),
    restoreErrorsButton: document.querySelector("#restore-errors"),
    candidateOnly: [...document.querySelectorAll("[data-candidate-only]")],
    autoOnly: [...document.querySelectorAll("[data-auto-only]")],
    toast: document.querySelector("#admin-toast"),
    toastMessage: document.querySelector("#admin-toast-message"),
    toastClose: document.querySelector("#admin-toast-close"),
    updateAlert: document.querySelector("#admin-update-alert"),
    updateMessage: document.querySelector("#admin-update-message"),
    updateClose: document.querySelector("#admin-update-close"),
    newErrorCard: document.querySelector("#admin-new-error"),
    newErrorSummary: document.querySelector("#new-error-admin-summary"),
    newErrorCount: document.querySelector("#new-error-admin-count"),
    newErrorToggle: document.querySelector("#new-error-count-toggle"),
    newErrorDownload: document.querySelector("#new-error-admin-download"),
    newErrorPanel: document.querySelector("#new-error-list-panel"),
    newErrorLoading: document.querySelector("#new-error-loading"),
    newErrorScroll: document.querySelector("#new-error-scroll"),
    newErrorTable: document.querySelector("#new-error-table"),
    newErrorHead: document.querySelector("#new-error-head"),
    newErrorBody: document.querySelector("#new-error-body"),
    newErrorEmpty: document.querySelector("#new-error-empty"),
    newErrorClearFilters: document.querySelector("#new-error-clear-filters"),
    newErrorRefresh: document.querySelector("#new-error-refresh"),
    newErrorPageRange: document.querySelector("#new-error-page-range"),
    newErrorPageLabel: document.querySelector("#new-error-page-label"),
    newErrorPrev: document.querySelector("#new-error-page-prev"),
    newErrorNext: document.querySelector("#new-error-page-next"),
  };

  const state = {
    jobId: null,
    dataset: "candidate",
    page: 1,
    pageSize: 100,
    total: 0,
    totalPages: 1,
    columns: [],
    rows: [],
    columnFilters: { candidate: {}, auto_error: {}, preprocessed: {} },
    draftIds: new Set(),
    errorDraftIds: new Set(),
    errorDraftStatus: new Map(),
    canRollback: false,
    fallbackFullscreen: false,
    gridController: null,
    gridRequestId: 0,
    pendingAnalysisJobId: null,
    analysisRequestInFlight: false,
    mutationInFlight: false,
    lastEventId: 0,
    liveRefreshInFlight: false,
    analysisProgressTimer: null,
    analysisProgressId: null,
    analysisProgressRequestId: 0,
    newErrors: {
      page: 1,
      pageSize: 100,
      total: 0,
      totalPages: 1,
      baseTotal: 0,
      columns: [],
      rows: [],
      summary: {},
      downloadUrl: "",
      columnFilters: {},
      loaded: false,
      controller: null,
      requestId: 0,
    },
    filterContext: null,
  };

  const numberFormat = new Intl.NumberFormat("ko-KR");
  const currentYear = new Date().getFullYear();
  let toastTimer;
  let eventSource;

  const textValue = (value) => {
    if (value === null || value === undefined) return "";
    if (typeof value === "object") {
      try {
        return JSON.stringify(value);
      } catch (_error) {
        return String(value);
      }
    }
    return String(value);
  };

  const responseError = (payload, status) => {
    const detail = payload?.detail ?? payload?.message;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail && typeof detail === "object") return textValue(detail);
    return `서버 오류 (${status})`;
  };

  const setAnalysisProgress = (percent, message, progressState = "running") => {
    const normalized = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
    elements.processProgressBar.style.width = `${normalized}%`;
    elements.processPercent.textContent = `${normalized}%`;
    elements.processProgressTrack.setAttribute("aria-valuenow", String(normalized));
    elements.processMessage.textContent = message || "분석을 진행하고 있습니다.";
    elements.processTitle.textContent = progressState === "complete"
      ? "분석을 완료했습니다."
      : progressState === "error"
        ? "분석을 완료하지 못했습니다."
        : "데이터를 분석하고 있습니다.";
  };

  const stopAnalysisProgressPolling = () => {
    window.clearTimeout(state.analysisProgressTimer);
    state.analysisProgressTimer = null;
    state.analysisProgressId = null;
    state.analysisProgressRequestId += 1;
  };

  const startAnalysisProgressPolling = (analysisId) => {
    stopAnalysisProgressPolling();
    state.analysisProgressId = analysisId;
    const requestId = state.analysisProgressRequestId;
    const poll = async () => {
      if (requestId !== state.analysisProgressRequestId || state.analysisProgressId !== analysisId) return;
      try {
        const response = await fetch(`/api/admin/analysis-progress/${encodeURIComponent(analysisId)}`, {
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (response.status !== 404) {
          const payload = await response.json().catch(() => ({}));
          if (response.ok) {
            setAnalysisProgress(payload.percent, payload.message, payload.state);
            if (payload.state === "complete" || payload.state === "error") return;
          }
        }
      } catch (_error) {
        // The POST response remains authoritative; retry transient polling errors.
      }
      if (requestId === state.analysisProgressRequestId) {
        state.analysisProgressTimer = window.setTimeout(poll, 350);
      }
    };
    state.analysisProgressTimer = window.setTimeout(poll, 80);
  };

  const hideToast = () => {
    window.clearTimeout(toastTimer);
    if (elements.toast) elements.toast.hidden = true;
  };

  const showToast = (message) => {
    if (!elements.toast || !elements.toastMessage) return;
    window.clearTimeout(toastTimer);
    elements.toastMessage.textContent = message;
    elements.toast.hidden = false;
  };

  const showError = (message, options = {}) => {
    const { hideResults = false } = options;
    elements.processPanel.hidden = true;
    if (hideResults) {
      elements.resultPanel.hidden = true;
      elements.workspace.hidden = true;
    }
    elements.errorPanel.hidden = false;
    document.querySelector("#error-message").textContent = message;
    elements.errorPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const formatUpdateDate = (dateText) => {
    const match = String(dateText || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? `${match[1]}.${match[2]}.${match[3]}` : "확인되지 않음";
  };

  const formatDateTime = (dateText) => {
    const date = new Date(dateText);
    if (!dateText || Number.isNaN(date.getTime())) return formatUpdateDate(dateText);
    return new Intl.DateTimeFormat("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  };

  const localDateKey = (date = new Date()) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  };

  const setUpdateAlert = (message, visible = true) => {
    if (!elements.updateAlert || !elements.updateMessage) return;
    elements.updateMessage.textContent = message;
    elements.updateAlert.hidden = !visible;
    document.body.classList.toggle("has-update-alert", visible);
  };

  const checkUpdateRequirement = async () => {
    try {
      const response = await fetch("/api/health", {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      const lastDate = payload?.data_basis_date || payload?.current_job?.period?.end || payload?.dashboard?.end;
      if (!lastDate) {
        setUpdateAlert("마지막 데이터 업데이트 날짜가 없습니다. 최신 원본 데이터를 업로드해 주세요.");
      } else if (String(lastDate).slice(0, 10) < localDateKey()) {
        setUpdateAlert(`최종 반영 데이터: ${formatUpdateDate(lastDate)} 기준 · 이후 데이터를 업데이트해 주세요.`);
      } else {
        setUpdateAlert(`최종 반영 데이터: ${formatUpdateDate(lastDate)} 기준`, true);
      }
      return payload;
    } catch (_error) {
      setUpdateAlert("마지막 데이터 업데이트 날짜를 확인할 수 없습니다. 서버 상태를 확인해 주세요.");
      return null;
    }
  };

  const setFile = (file) => {
    if (!file) {
      elements.selectedFile.textContent = "선택된 파일 없음";
      elements.submitButton.disabled = true;
      elements.uploadZone.classList.remove("has-file");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      showError(".xlsx 형식의 Excel 파일만 선택할 수 있습니다.");
      elements.fileInput.value = "";
      setFile(null);
      return;
    }
    const size = file.size / 1024 / 1024;
    if (size > 150) {
      showError("파일 크기는 150MB 이하여야 합니다.");
      elements.fileInput.value = "";
      setFile(null);
      return;
    }
    elements.selectedFile.textContent = `${file.name} · ${size.toFixed(1)}MB`;
    elements.submitButton.disabled = false;
    elements.uploadZone.classList.add("has-file");
    elements.errorPanel.hidden = true;
  };

  const columnWidth = (name) => {
    const normalized = String(name).replace(/\s/g, "");
    if (/내역|내용|상담|메모|사유/.test(normalized)) return 350;
    if (/일시|일자|날짜|생성일|완료일/.test(normalized)) return 155;
    if (/부서|사업부|소분류|유형|상태/.test(normalized)) return 135;
    if (/고객|주소|오더|번호/.test(normalized)) return 170;
    if (/성명|이름|담당|생성자/.test(normalized)) return 115;
    return Math.max(115, Math.min(210, String(name).length * 16 + 48));
  };

  const makeCell = (tag, text, className) => {
    const cell = document.createElement(tag);
    if (className) cell.className = className;
    if (text !== undefined) {
      const value = textValue(text);
      cell.textContent = value;
      if (value.length > 8) cell.title = value;
    }
    return cell;
  };

  const addColumnResizer = (header, column) => {
    const handle = document.createElement("span");
    handle.className = "column-resizer";
    handle.setAttribute("aria-hidden", "true");
    handle.addEventListener("click", (event) => event.stopPropagation());
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      closeFilterMenu();
      const startX = event.clientX;
      const startWidth = column.getBoundingClientRect().width;
      handle.classList.add("is-resizing");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";

      const onMove = (moveEvent) => {
        const width = Math.max(80, Math.min(800, startWidth + moveEvent.clientX - startX));
        column.style.width = `${width}px`;
      };
      const onEnd = () => {
        handle.classList.remove("is-resizing");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onEnd);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onEnd, { once: true });
    });
    header.append(handle);
  };

  const filterMenu = document.createElement("div");
  filterMenu.className = "excel-filter-menu";
  filterMenu.hidden = true;
  filterMenu.setAttribute("role", "dialog");
  filterMenu.setAttribute("aria-label", "열 필터");
  filterMenu.innerHTML = `
    <div class="excel-filter-menu-title"><span></span><button type="button" aria-label="필터 메뉴 닫기">×</button></div>
    <input class="excel-filter-input" type="search" autocomplete="off" placeholder="값 검색">
    <small class="excel-filter-hint">전체 데이터의 고유 값을 불러옵니다.</small>
    <div class="excel-filter-selection">
      <label><input type="checkbox"><span>(현재 목록 전체 선택)</span></label>
      <strong>0개 선택</strong>
    </div>
    <div class="excel-filter-values"></div>
    <div class="excel-filter-actions"><button type="button" data-action="reset">전체 보기</button><button type="button" data-action="apply">적용</button></div>
  `;
  document.body.append(filterMenu);
  const filterMenuTitle = filterMenu.querySelector(".excel-filter-menu-title span");
  const filterMenuClose = filterMenu.querySelector(".excel-filter-menu-title button");
  const filterMenuInput = filterMenu.querySelector(".excel-filter-input");
  const filterMenuHint = filterMenu.querySelector(".excel-filter-hint");
  const filterMenuSelectAll = filterMenu.querySelector(".excel-filter-selection input");
  const filterMenuSelectedCount = filterMenu.querySelector(".excel-filter-selection strong");
  const filterMenuValues = filterMenu.querySelector(".excel-filter-values");
  const filterMenuReset = filterMenu.querySelector("[data-action='reset']");
  const filterMenuApply = filterMenu.querySelector("[data-action='apply']");

  function closeFilterMenu(options = {}) {
    const { restoreFocus = false } = options;
    const trigger = state.filterContext?.trigger;
    trigger?.classList.remove("is-open");
    window.clearTimeout(state.filterContext?.timer);
    state.filterContext?.controller?.abort();
    filterMenu.hidden = true;
    state.filterContext = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  const columnValues = (rows, column) => {
    const seen = new Set();
    const values = [];
    rows.forEach((row) => {
      const value = textValue(row?.[column]).trim();
      if (seen.has(value)) return;
      seen.add(value);
      values.push(value);
    });
    return values.sort((left, right) => left.localeCompare(right, "ko", { numeric: true })).slice(0, 120);
  };

  const normalizeFilterSelection = (value) => {
    if (Array.isArray(value)) {
      return [...new Set(value.map((item) => textValue(item).trim()))];
    }
    const normalized = textValue(value).trim();
    return normalized ? [normalized] : [];
  };

  const hasFilterSelection = (value) => normalizeFilterSelection(value).length > 0;

  const activeFilters = (filters) => Object.fromEntries(
    Object.entries(filters || {})
      .map(([column, value]) => [column, normalizeFilterSelection(value)])
      .filter(([, values]) => values.length > 0),
  );

  const visibleFilterValues = () => {
    const context = state.filterContext;
    if (!context) return [];
    const query = filterMenuInput.value.trim().toLocaleLowerCase("ko");
    return context.values.filter((value) => !query || value.toLocaleLowerCase("ko").includes(query));
  };

  const syncFilterSelectionSummary = (visible = visibleFilterValues()) => {
    const context = state.filterContext;
    if (!context) return;
    const selectedVisible = visible.filter((value) => context.selectedValues.has(value)).length;
    filterMenuSelectAll.disabled = visible.length === 0;
    filterMenuSelectAll.checked = visible.length > 0 && selectedVisible === visible.length;
    filterMenuSelectAll.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
    filterMenuSelectedCount.textContent = `${numberFormat.format(context.selectedValues.size)}개 선택`;
  };

  const renderFilterValues = () => {
    const context = state.filterContext;
    if (!context) return;
    const visible = visibleFilterValues();
    filterMenuValues.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("span");
      empty.className = "excel-filter-empty";
      empty.textContent = "현재 페이지에서 일치하는 값이 없습니다.";
      filterMenuValues.append(empty);
      syncFilterSelectionSummary(visible);
      return;
    }
    visible.forEach((value) => {
      const label = document.createElement("label");
      label.className = "excel-filter-value";
      label.title = value || "빈 값";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = context.selectedValues.has(value);
      const text = document.createElement("span");
      text.textContent = value || "(빈 값)";
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) context.selectedValues.add(value);
        else context.selectedValues.delete(value);
        label.classList.toggle("is-selected", checkbox.checked);
        syncFilterSelectionSummary();
      });
      label.classList.toggle("is-selected", checkbox.checked);
      label.append(checkbox, text);
      filterMenuValues.append(label);
    });
    syncFilterSelectionSummary(visible);
  };

  const loadFilterValues = async () => {
    const context = state.filterContext;
    if (!context?.loadValues) return;
    context.controller?.abort();
    const controller = new AbortController();
    context.controller = controller;
    const requestId = (context.requestId || 0) + 1;
    context.requestId = requestId;
    filterMenuHint.textContent = "전체 데이터의 값을 불러오는 중입니다.";
    try {
      const payload = await context.loadValues(filterMenuInput.value.trim(), controller.signal);
      if (state.filterContext !== context || context.requestId !== requestId) return;
      context.values = [...new Set((payload.values || []).map((value) => textValue(value).trim()))];
      const total = Number(payload.total) || context.values.length;
      filterMenuHint.textContent = payload.truncated
        ? `전체 ${numberFormat.format(total)}개 값 중 앞의 ${numberFormat.format(context.values.length)}개 표시`
        : `전체 데이터의 값 ${numberFormat.format(total)}개`;
      renderFilterValues();
    } catch (error) {
      if (error.name === "AbortError" || state.filterContext !== context) return;
      filterMenuHint.textContent = "값 목록을 불러오지 못해 현재 페이지 값으로 표시합니다.";
      renderFilterValues();
    }
  };

  const positionFilterMenu = (trigger) => {
    const rect = trigger.getBoundingClientRect();
    const menuWidth = Math.min(292, window.innerWidth - 24);
    const left = Math.max(12, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 12));
    filterMenu.style.left = `${left}px`;
    filterMenu.style.top = `${Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - 430))}px`;
  };

  const openFilterMenu = ({ trigger, column, currentValue, values, loadValues, apply }) => {
    if (state.filterContext?.trigger === trigger && !filterMenu.hidden) {
      closeFilterMenu({ restoreFocus: true });
      return;
    }
    closeFilterMenu();
    state.filterContext = {
      trigger,
      column,
      currentValue,
      selectedValues: new Set(normalizeFilterSelection(currentValue)),
      values,
      loadValues,
      apply,
    };
    filterMenuTitle.textContent = `${column} 필터`;
    filterMenuInput.value = "";
    trigger.classList.add("is-open");
    filterMenu.hidden = false;
    filterMenuHint.textContent = loadValues
      ? "전체 데이터의 값을 불러오는 중입니다."
      : "현재 페이지의 고유 값을 표시합니다.";
    renderFilterValues();
    positionFilterMenu(trigger);
    filterMenuInput.focus();
    filterMenuInput.select();
    loadFilterValues();
  };

  filterMenu.addEventListener("click", (event) => event.stopPropagation());
  filterMenuInput.addEventListener("input", () => {
    renderFilterValues();
    const context = state.filterContext;
    if (!context?.loadValues) return;
    window.clearTimeout(context.timer);
    context.timer = window.setTimeout(loadFilterValues, 180);
  });
  filterMenuInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const context = state.filterContext;
      if (!context) return;
      context.apply([...context.selectedValues]);
      closeFilterMenu();
    } else if (event.key === "Escape") {
      closeFilterMenu({ restoreFocus: true });
    }
  });
  filterMenuSelectAll.addEventListener("change", () => {
    const context = state.filterContext;
    if (!context) return;
    visibleFilterValues().forEach((value) => {
      if (filterMenuSelectAll.checked) context.selectedValues.add(value);
      else context.selectedValues.delete(value);
    });
    renderFilterValues();
  });
  filterMenuClose.addEventListener("click", () => closeFilterMenu({ restoreFocus: true }));
  filterMenuReset.addEventListener("click", () => {
    const context = state.filterContext;
    if (!context) return;
    context.apply([]);
    closeFilterMenu();
  });
  filterMenuApply.addEventListener("click", () => {
    const context = state.filterContext;
    if (!context) return;
    context.apply([...context.selectedValues]);
    closeFilterMenu();
  });
  document.addEventListener("click", () => closeFilterMenu());
  window.addEventListener("resize", () => closeFilterMenu());

  const createFilterHeader = ({ name, column, rows, currentValue, loadValues, apply }) => {
    const header = makeCell("th");
    header.dataset.column = name;
    const content = document.createElement("div");
    content.className = "column-header-content";
    const label = document.createElement("span");
    label.className = "column-header-label";
    label.textContent = name;
    label.title = name;
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "column-filter-trigger";
    const getCurrentValue = typeof currentValue === "function" ? currentValue : () => currentValue;
    const initialValue = getCurrentValue() || [];
    trigger.classList.toggle("has-filter", hasFilterSelection(initialValue));
    trigger.dataset.filterColumn = name;
    trigger.title = hasFilterSelection(initialValue)
      ? `${name}: ${normalizeFilterSelection(initialValue).length}개 선택`
      : `${name} 열 필터`;
    trigger.setAttribute("aria-label", trigger.title);
    trigger.addEventListener("pointerdown", (event) => event.stopPropagation());
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const activeValue = getCurrentValue() || [];
      openFilterMenu({
        trigger,
        column: name,
        currentValue: activeValue,
        values: columnValues(rows(), name),
        loadValues,
        apply,
      });
    });
    content.append(label, trigger);
    header.append(content);
    addColumnResizer(header, column);
    return header;
  };

  const syncFilterTriggers = (head, filters) => {
    head.querySelectorAll(".column-filter-trigger").forEach((trigger) => {
      const value = filters[trigger.dataset.filterColumn] || [];
      trigger.classList.toggle("has-filter", hasFilterSelection(value));
      trigger.title = hasFilterSelection(value)
        ? `${trigger.dataset.filterColumn}: ${normalizeFilterSelection(value).length}개 선택`
        : `${trigger.dataset.filterColumn} 열 필터`;
      trigger.setAttribute("aria-label", trigger.title);
    });
  };

  const activeGridFilters = () => activeFilters(state.columnFilters[state.dataset]);

  const activeSelection = () => (
    state.dataset === "auto_error" ? state.errorDraftIds : state.draftIds
  );

  const activeNewErrorFilters = () => activeFilters(state.newErrors.columnFilters);

  const updateToolbarState = () => {
    const isCandidate = state.dataset === "candidate";
    const isAutoError = state.dataset === "auto_error";
    const selection = activeSelection();
    elements.selectionSummary.hidden = !(isCandidate || isAutoError);
    elements.candidateOnly.forEach((element) => { element.hidden = !isCandidate; });
    elements.autoOnly.forEach((element) => { element.hidden = !isAutoError; });
    elements.checkedCount.textContent = numberFormat.format(selection.size);
    elements.candidateTotal.textContent = numberFormat.format(state.total || 0);
    elements.selectionMode.textContent = isCandidate ? "승인 선택" : "선택";
    elements.approveButton.disabled = !isCandidate || !state.jobId || selection.size === 0;
    elements.rollbackButton.disabled = !isCandidate || !state.jobId || !state.canRollback;
    const selectedStatuses = [...state.errorDraftIds]
      .map((rowId) => state.errorDraftStatus.get(String(rowId)))
      .filter(Boolean);
    elements.excludeErrorsButton.disabled = !isAutoError
      || !selectedStatuses.length
      || selectedStatuses.some((status) => status !== "확정");
    elements.restoreErrorsButton.disabled = true;
    elements.clearFiltersButton.disabled = Object.keys(activeGridFilters()).length === 0;
    elements.newErrorClearFilters.disabled = Object.keys(activeNewErrorFilters()).length === 0;
  };

  const syncHeaderCheckbox = () => {
    const headerCheckbox = elements.head.querySelector(".page-select-checkbox");
    const selectable = ["candidate", "auto_error"].includes(state.dataset);
    if (!headerCheckbox || !selectable) return;
    const selection = activeSelection();
    const eligibleRows = state.dataset === "candidate"
      ? state.rows.filter((row) => !row.checked)
      : state.rows;
    const selected = eligibleRows.filter((row) => selection.has(String(row.row_id))).length;
    headerCheckbox.disabled = eligibleRows.length === 0;
    headerCheckbox.checked = eligibleRows.length > 0 && selected === eligibleRows.length;
    headerCheckbox.indeterminate = selected > 0 && selected < eligibleRows.length;
  };

  const setDraft = (row, selected, checkbox, rowElement) => {
    const key = String(row.row_id);
    const selection = activeSelection();
    if (selected) selection.add(key);
    else selection.delete(key);
    if (state.dataset === "auto_error") {
      if (selected) state.errorDraftStatus.set(key, textValue(row["집계상태"]));
      else state.errorDraftStatus.delete(key);
    }
    checkbox.checked = selected;
    rowElement.classList.toggle("is-draft", selected);
    updateToolbarState();
    syncHeaderCheckbox();
  };

  const setCurrentPageDraft = (selected) => {
    if (!["candidate", "auto_error"].includes(state.dataset)) return;
    const selection = activeSelection();
    state.rows.forEach((row) => {
      if (state.dataset === "candidate" && row.checked) return;
      const key = String(row.row_id);
      if (selected) selection.add(key);
      else selection.delete(key);
      if (state.dataset === "auto_error") {
        if (selected) state.errorDraftStatus.set(key, textValue(row["집계상태"]));
        else state.errorDraftStatus.delete(key);
      }
    });
    elements.body.querySelectorAll("tr[data-row-id]").forEach((rowElement) => {
      if (rowElement.classList.contains("is-approved")) return;
      const selectedOnRow = selection.has(rowElement.dataset.rowId);
      rowElement.classList.toggle("is-draft", selectedOnRow);
      const checkbox = rowElement.querySelector("input[type='checkbox']");
      if (checkbox) checkbox.checked = selectedOnRow;
    });
    updateToolbarState();
    syncHeaderCheckbox();
  };

  const applyGridFilter = (column, value) => {
    const filters = state.columnFilters[state.dataset];
    const selected = normalizeFilterSelection(value);
    if (selected.length) filters[column] = selected;
    else delete filters[column];
    updateToolbarState();
    loadGrid(1);
  };

  const fetchGridColumnValues = async (dataset, column, search, signal) => {
    const query = new URLSearchParams({
      job_id: state.jobId,
      dataset,
      column,
      limit: "200",
    });
    if (search) query.set("search", search);
    const response = await fetch(`/api/admin/grid/values?${query}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseError(payload, response.status));
    return payload;
  };

  const buildGridHead = (columns) => {
    state.columns = columns;
    elements.head.replaceChildren();
    elements.table.querySelector("colgroup")?.remove();

    const colgroup = document.createElement("colgroup");
    const checkCol = document.createElement("col");
    checkCol.style.width = "48px";
    const numberCol = document.createElement("col");
    numberCol.style.width = "58px";
    colgroup.append(checkCol, numberCol);
    const dataColumns = columns.map((name) => {
      const col = document.createElement("col");
      col.style.width = `${columnWidth(name)}px`;
      colgroup.append(col);
      return col;
    });
    elements.table.insertBefore(colgroup, elements.head);

    const headingRow = document.createElement("tr");
    headingRow.className = "column-heading-row";
    const checkHeader = makeCell("th", undefined, "sheet-check-cell");
    if (["candidate", "auto_error"].includes(state.dataset)) {
      const allCheckbox = document.createElement("input");
      allCheckbox.type = "checkbox";
      allCheckbox.className = "page-select-checkbox";
      allCheckbox.title = "현재 페이지 전체 선택";
      allCheckbox.setAttribute("aria-label", "현재 페이지 전체 선택");
      allCheckbox.addEventListener("change", () => setCurrentPageDraft(allCheckbox.checked));
      checkHeader.append(allCheckbox);
    }
    headingRow.append(checkHeader, makeCell("th", "행", "sheet-row-number"));
    const dataset = state.dataset;
    columns.forEach((name, index) => {
      headingRow.append(createFilterHeader({
        name,
        column: dataColumns[index],
        rows: () => state.rows,
        currentValue: () => state.columnFilters[dataset]?.[name] || "",
        loadValues: (search, signal) => fetchGridColumnValues(dataset, name, search, signal),
        apply: (value) => applyGridFilter(name, value),
      }));
    });
    elements.head.append(headingRow);
  };

  const renderRows = () => {
    elements.body.replaceChildren();
    const isCandidate = state.dataset === "candidate";
    const isAutoError = state.dataset === "auto_error";
    const selection = activeSelection();
    state.rows.forEach((row, rowIndex) => {
      const key = String(row.row_id);
      const approved = isCandidate && Boolean(row.checked);
      if (approved) state.draftIds.delete(key);
      const selected = (isCandidate || isAutoError) && !approved && selection.has(key);
      const tr = document.createElement("tr");
      tr.dataset.rowId = key;
      tr.classList.toggle("is-approved", approved);
      tr.classList.toggle("is-draft", selected);
      if (approved) tr.title = "승인되어 대시보드에 반영된 항목";

      const checkCell = makeCell("td", undefined, "sheet-check-cell");
      if (isCandidate || isAutoError) {
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selected;
        checkbox.disabled = approved;
        checkbox.setAttribute(
          "aria-label",
          approved
            ? `${(state.page - 1) * state.pageSize + rowIndex + 1}행 승인 완료`
            : `${(state.page - 1) * state.pageSize + rowIndex + 1}행 선택`,
        );
        checkbox.addEventListener("change", () => setDraft(row, checkbox.checked, checkbox, tr));
        checkCell.append(checkbox);
      }
      tr.append(
        checkCell,
        makeCell("td", (state.page - 1) * state.pageSize + rowIndex + 1, "sheet-row-number"),
      );
      state.columns.forEach((column) => tr.append(makeCell("td", row[column])));
      elements.body.append(tr);
    });

    const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0;
    const end = Math.min(state.total, state.page * state.pageSize);
    elements.pageRange.textContent = state.total
      ? `${numberFormat.format(start)}–${numberFormat.format(end)} / ${numberFormat.format(state.total)}건`
      : "0건";
    elements.pageLabel.textContent = `${numberFormat.format(state.page)} / ${numberFormat.format(state.totalPages)}`;
    elements.prevButton.disabled = state.page <= 1;
    elements.nextButton.disabled = state.page >= state.totalPages;
    elements.empty.hidden = state.rows.length > 0;
    elements.sheetScroll.hidden = state.rows.length === 0;
    syncFilterTriggers(elements.head, state.columnFilters[state.dataset]);
    updateToolbarState();
    syncHeaderCheckbox();
  };

  const renderGrid = (payload) => {
    state.page = Number(payload.page) || 1;
    state.pageSize = Number(payload.page_size) || 100;
    state.total = Number(payload.total) || 0;
    state.totalPages = Math.max(1, Number(payload.total_pages) || 1);
    state.rows = Array.isArray(payload.rows) ? payload.rows : [];
    state.canRollback = Boolean(payload.can_rollback);
    const columns = Array.isArray(payload.columns) ? payload.columns : [];
    if (JSON.stringify(columns) !== JSON.stringify(state.columns)) buildGridHead(columns);
    renderRows();
  };

  const loadGrid = async (page = state.page) => {
    if (!state.jobId) return;
    state.gridController?.abort();
    const controller = new AbortController();
    state.gridController = controller;
    const requestId = ++state.gridRequestId;
    elements.loading.hidden = false;
    elements.errorPanel.hidden = true;
    try {
      const query = new URLSearchParams({
        job_id: state.jobId,
        dataset: state.dataset,
        page: String(page),
        page_size: String(state.pageSize),
      });
      const filters = activeGridFilters();
      if (Object.keys(filters).length) query.set("column_filters", JSON.stringify(filters));
      const response = await fetch(`/api/admin/grid?${query}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      renderGrid(payload);
      elements.workspace.hidden = false;
    } catch (error) {
      if (error.name !== "AbortError") showError(error.message || "데이터를 불러오지 못했습니다.");
    } finally {
      if (requestId === state.gridRequestId) elements.loading.hidden = true;
    }
  };

  const switchDataset = async (dataset, options = {}) => {
    const { load = true } = options;
    if (!state.jobId || !["candidate", "auto_error"].includes(dataset)) return;
    closeFilterMenu();
    state.dataset = dataset;
    state.page = 1;
    state.total = 0;
    state.totalPages = 1;
    state.rows = [];
    state.columns = [];
    elements.head.replaceChildren();
    elements.body.replaceChildren();
    elements.table.querySelector("colgroup")?.remove();
    const isCandidate = dataset === "candidate";
    const isAutoError = dataset === "auto_error";
    elements.datasetTabs.forEach((tab) => {
      const active = tab.dataset.dataset === dataset;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    elements.sheet.setAttribute(
      "aria-labelledby",
      isCandidate ? "candidate-dataset-tab" : "auto-error-dataset-tab",
    );
    elements.gridTitle.textContent = isCandidate ? "후보군" : "오생성";
    elements.gridDescription.textContent = isCandidate
      ? "승인할 항목을 선택하세요."
      : "후보군으로 되돌릴 항목을 선택하세요.";
    updateToolbarState();
    if (load) await loadGrid(1);
  };

  const newErrorBaseQuery = () => new URLSearchParams({
    scope: "business",
    time_mode: "year",
    year: String(currentYear),
  });

  const newErrorDownloadUrl = (serverUrl = "") => {
    const url = new URL(
      serverUrl || `/api/service-order/new-errors/export?${newErrorBaseQuery()}`,
      window.location.origin,
    );
    const filters = activeNewErrorFilters();
    if (Object.keys(filters).length) url.searchParams.set("column_filters", JSON.stringify(filters));
    else url.searchParams.delete("column_filters");
    return `${url.pathname}${url.search}`;
  };

  const fetchNewErrorColumnValues = async (column, search, signal) => {
    const query = newErrorBaseQuery();
    query.set("column", column);
    query.set("limit", "200");
    if (search) query.set("search", search);
    const response = await fetch(`/api/service-order/new-errors/values?${query}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseError(payload, response.status));
    return payload;
  };

  const updateNewErrorSummary = () => {
    const summary = state.newErrors.summary || {};
    const count = state.newErrors.baseTotal;
    const hasListFilters = Object.keys(activeNewErrorFilters()).length > 0;
    const downloadableCount = hasListFilters ? state.newErrors.total : count;
    elements.newErrorCount.textContent = numberFormat.format(count);
    const asOf = summary.as_of_date || summary.asOfDate;
    const since = summary.since_date || summary.sinceDate;
    const start = summary.start;
    const end = summary.end;
    if (count > 0) {
      const period = start && end ? `${formatUpdateDate(start)}–${formatUpdateDate(end)}` : `${currentYear}년`;
      const basis = asOf ? ` · 데이터 ${formatUpdateDate(asOf)} 기준` : "";
      const approved = since ? ` · 승인 시작 ${formatDateTime(since)}` : "";
      elements.newErrorSummary.textContent = `${period} 승인 데이터${basis}${approved}`;
      if (downloadableCount > 0) {
        elements.newErrorDownload.href = newErrorDownloadUrl(
          state.newErrors.downloadUrl || summary.download_url || "",
        );
        elements.newErrorDownload.classList.remove("is-disabled");
        elements.newErrorDownload.removeAttribute("aria-disabled");
      } else {
        elements.newErrorDownload.href = "#";
        elements.newErrorDownload.classList.add("is-disabled");
        elements.newErrorDownload.setAttribute("aria-disabled", "true");
      }
    } else {
      elements.newErrorSummary.textContent = `${currentYear}년에 새로 승인된 오생성이 없습니다.`;
      elements.newErrorDownload.href = "#";
      elements.newErrorDownload.classList.add("is-disabled");
      elements.newErrorDownload.setAttribute("aria-disabled", "true");
    }
  };

  const applyNewErrorFilter = (column, value) => {
    const selected = normalizeFilterSelection(value);
    if (selected.length) state.newErrors.columnFilters[column] = selected;
    else delete state.newErrors.columnFilters[column];
    state.newErrors.downloadUrl = newErrorDownloadUrl();
    updateNewErrorSummary();
    updateToolbarState();
    loadNewErrors(1);
  };

  const buildNewErrorHead = (columns) => {
    state.newErrors.columns = columns;
    elements.newErrorHead.replaceChildren();
    elements.newErrorTable.querySelector("colgroup")?.remove();
    const colgroup = document.createElement("colgroup");
    const numberCol = document.createElement("col");
    numberCol.style.width = "58px";
    colgroup.append(numberCol);
    const dataColumns = columns.map((name) => {
      const col = document.createElement("col");
      col.style.width = `${columnWidth(name)}px`;
      colgroup.append(col);
      return col;
    });
    elements.newErrorTable.insertBefore(colgroup, elements.newErrorHead);

    const headingRow = document.createElement("tr");
    headingRow.className = "column-heading-row";
    headingRow.append(makeCell("th", "행", "sheet-row-number"));
    columns.forEach((name, index) => {
      headingRow.append(createFilterHeader({
        name,
        column: dataColumns[index],
        rows: () => state.newErrors.rows,
        currentValue: () => state.newErrors.columnFilters[name] || "",
        loadValues: (search, signal) => fetchNewErrorColumnValues(name, search, signal),
        apply: (value) => applyNewErrorFilter(name, value),
      }));
    });
    elements.newErrorHead.append(headingRow);
  };

  const renderNewErrorRows = () => {
    const data = state.newErrors;
    elements.newErrorBody.replaceChildren();
    data.rows.forEach((row, rowIndex) => {
      const tr = document.createElement("tr");
      tr.append(makeCell("td", (data.page - 1) * data.pageSize + rowIndex + 1, "sheet-row-number"));
      data.columns.forEach((column) => tr.append(makeCell("td", row[column])));
      elements.newErrorBody.append(tr);
    });
    const start = data.total ? (data.page - 1) * data.pageSize + 1 : 0;
    const end = Math.min(data.total, data.page * data.pageSize);
    elements.newErrorPageRange.textContent = data.total
      ? `${numberFormat.format(start)}–${numberFormat.format(end)} / ${numberFormat.format(data.total)}건`
      : "0건";
    elements.newErrorPageLabel.textContent = `${numberFormat.format(data.page)} / ${numberFormat.format(data.totalPages)}`;
    elements.newErrorPrev.disabled = data.page <= 1;
    elements.newErrorNext.disabled = data.page >= data.totalPages;
    elements.newErrorEmpty.hidden = data.rows.length > 0;
    elements.newErrorScroll.hidden = data.rows.length === 0;
    syncFilterTriggers(elements.newErrorHead, data.columnFilters);
    updateToolbarState();
  };

  const loadNewErrors = async (page = state.newErrors.page, options = {}) => {
    const { quiet = false } = options;
    state.newErrors.controller?.abort();
    const controller = new AbortController();
    state.newErrors.controller = controller;
    const requestId = ++state.newErrors.requestId;
    if (!quiet && !elements.newErrorPanel.hidden) elements.newErrorLoading.hidden = false;
    try {
      const query = newErrorBaseQuery();
      query.set("page", String(page));
      query.set("page_size", String(state.newErrors.pageSize));
      const filters = activeNewErrorFilters();
      if (Object.keys(filters).length) query.set("column_filters", JSON.stringify(filters));
      const response = await fetch(`/api/service-order/new-errors?${query}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      const data = state.newErrors;
      data.page = Number(payload.page) || 1;
      data.pageSize = Number(payload.page_size) || 100;
      data.total = Number(payload.total) || 0;
      data.totalPages = Math.max(1, Number(payload.total_pages) || 1);
      data.rows = Array.isArray(payload.rows) ? payload.rows : [];
      data.summary = payload.summary && typeof payload.summary === "object" ? payload.summary : {};
      data.downloadUrl = newErrorDownloadUrl(payload.download_url || "");
      if (!Object.keys(filters).length) data.baseTotal = Number(data.summary.count ?? data.total) || 0;
      const columns = Array.isArray(payload.columns) ? payload.columns : [];
      if (JSON.stringify(columns) !== JSON.stringify(data.columns)) buildNewErrorHead(columns);
      data.loaded = true;
      renderNewErrorRows();
      updateNewErrorSummary();
    } catch (error) {
      if (error.name === "AbortError") return;
      if (!state.newErrors.loaded) {
        state.newErrors.baseTotal = 0;
        elements.newErrorSummary.textContent = "새로운 오생성 목록을 불러오지 못했습니다.";
        elements.newErrorCount.textContent = "-";
        elements.newErrorDownload.href = "#";
        elements.newErrorDownload.classList.add("is-disabled");
        elements.newErrorDownload.setAttribute("aria-disabled", "true");
      }
      if (!quiet && !elements.newErrorPanel.hidden) {
        elements.newErrorEmpty.hidden = false;
        elements.newErrorEmpty.textContent = error.message || "새로운 오생성을 불러오지 못했습니다.";
        elements.newErrorScroll.hidden = true;
      }
    } finally {
      if (requestId === state.newErrors.requestId) elements.newErrorLoading.hidden = true;
    }
  };

  const setApprovalBusy = (busy) => {
    state.mutationInFlight = busy;
    elements.approveButton.disabled = busy || state.draftIds.size === 0;
    elements.rollbackButton.disabled = busy || !state.canRollback;
    elements.excludeErrorsButton.disabled = busy || state.errorDraftIds.size === 0;
    elements.restoreErrorsButton.disabled = busy || state.errorDraftIds.size === 0;
    elements.refreshButton.disabled = busy;
    elements.clearFiltersButton.disabled = busy || Object.keys(activeGridFilters()).length === 0;
  };

  const publishDashboardRefresh = (type, payload = {}) => {
    try {
      localStorage.setItem("service_order.dashboard_refresh", JSON.stringify({
        type,
        at: Date.now(),
        jobId: state.jobId,
        dataBasisDate: payload?.data_end || payload?.classification?.data_end || "",
      }));
    } catch (_) {
      // EventSource remains the primary channel when browser storage is unavailable.
    }
  };

  const approveDraft = async () => {
    const rowIds = [...state.draftIds].map((value) => {
      const number = Number(value);
      return Number.isSafeInteger(number) ? number : value;
    });
    if (!state.jobId || !rowIds.length) return;
    hideToast();
    setApprovalBusy(true);
    try {
      const response = await fetch("/api/admin/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ job_id: state.jobId, row_ids: rowIds }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      const approvedCount = Number(payload.approved_count) || rowIds.length;
      state.draftIds.clear();
      state.canRollback = true;
      const currentJob = await fetchCurrentJob();
      if (currentJob?.job_id === state.jobId) renderAnalysisResult(currentJob);
      await Promise.all([switchDataset("auto_error"), loadNewErrors(1, { quiet: true })]);
      publishDashboardRefresh("candidate_approved", payload);
      showToast(payload.notification?.message || `${numberFormat.format(approvedCount)}건을 승인해 대시보드에 반영했습니다.`);
    } catch (error) {
      showToast(error.message || "선택한 후보를 승인하지 못했습니다.");
    } finally {
      setApprovalBusy(false);
      updateToolbarState();
    }
  };

  const rollbackLastApproval = async () => {
    if (!state.jobId || !state.canRollback) return;
    if (!window.confirm(
      "현재 파일과 관계없이 시스템에 반영된 가장 최근 승인 묶음을 롤백할까요?",
    )) return;
    hideToast();
    setApprovalBusy(true);
    try {
      const response = await fetch("/api/admin/rollback", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ job_id: state.jobId }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      const currentJob = await fetchCurrentJob();
      if (currentJob?.job_id === state.jobId) renderAnalysisResult(currentJob);
      await Promise.all([
        switchDataset("candidate"),
        loadNewErrors(1, { quiet: true }),
      ]);
      publishDashboardRefresh("approval_rolled_back", payload);
      const rolledBackCount = Number(payload.rolled_back_count) || 0;
      showToast(payload.notification?.message || `마지막 승인 ${numberFormat.format(rolledBackCount)}건을 롤백했습니다.`);
    } catch (error) {
      showToast(error.message || "마지막 승인을 롤백하지 못했습니다.");
    } finally {
      setApprovalBusy(false);
      updateToolbarState();
    }
  };

  const changeConfirmedErrors = async (action) => {
    const rowIds = [...state.errorDraftIds].map(Number).filter(Number.isSafeInteger);
    if (!state.jobId || !rowIds.length) return;
    const isExclude = action === "exclude";
    if (!window.confirm(
      isExclude
        ? `선택한 ${numberFormat.format(rowIds.length)}건을 후보군으로 되돌릴까요?`
        : `선택한 ${numberFormat.format(rowIds.length)}건을 오생성 집계에 복구할까요?`,
    )) return;
    hideToast();
    setApprovalBusy(true);
    try {
      const response = await fetch(`/api/admin/errors/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ job_id: state.jobId, row_ids: rowIds }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      state.errorDraftIds.clear();
      state.errorDraftStatus.clear();
      const currentJob = await fetchCurrentJob();
      if (currentJob?.job_id === state.jobId) renderAnalysisResult(currentJob);
      await Promise.all([
        switchDataset(isExclude ? "candidate" : "auto_error"),
        loadNewErrors(1, { quiet: true }),
      ]);
      publishDashboardRefresh(`confirmed_error_${action}`, payload);
      showToast(payload.notification?.message || "확정 오생성 상태를 변경했습니다.");
    } catch (error) {
      showToast(error.message || "확정 오생성 상태를 변경하지 못했습니다.");
    } finally {
      setApprovalBusy(false);
      updateToolbarState();
    }
  };

  const numberOrDash = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? numberFormat.format(number) : "-";
  };

  const configureDownload = (selector, nameSelector, file) => {
    const link = document.querySelector(selector);
    const name = document.querySelector(nameSelector);
    if (file?.url) {
      link.href = file.url;
      link.removeAttribute("aria-disabled");
      link.classList.remove("is-disabled");
      name.textContent = file.name || "Excel 다운로드";
    } else {
      link.href = "#";
      link.setAttribute("aria-disabled", "true");
      link.classList.add("is-disabled");
      name.textContent = "현재 작업 데이터";
    }
  };

  const renderAnalysisResult = (payload, options = {}) => {
    const { restored = false } = options;
    const preprocess = payload.preprocess || {};
    const candidate = payload.candidate || {};
    const classificationCount = preprocess["결과행수"] ?? payload.preprocessed_count;
    const displayTotalCount = (
      preprocess["표기집계행수"]
      ?? payload.aggregate?.total_count
      ?? classificationCount
    );
    const candidateCount = candidate["후보행수"] ?? payload.candidate_count;
    const autoErrorCount = (
      payload.confirmed_error_count
      ?? candidate["자동오생성행수"]
      ?? 0
    );
    const periodStart = payload.period?.start || payload.start_date;
    const periodEnd = payload.period?.end || payload.end_date;
    let periodText = periodStart && periodEnd
      ? `${periodStart} ~ ${periodEnd} · ${payload.source_name || "업로드 파일"}`
      : `${payload.source_name || "최근 업로드 파일"}${restored && payload.created_at ? ` · ${formatDateTime(payload.created_at)} 분석 결과 복원` : ""}`;
    document.querySelector("#result-period").textContent = periodText;
    document.querySelector("#metric-preprocessed").textContent = numberOrDash(displayTotalCount);
    document.querySelector("#metric-auto-error").textContent = numberOrDash(autoErrorCount);
    document.querySelector("#metric-candidate").textContent = numberOrDash(candidateCount);
    elements.candidateTabCount.textContent = numberOrDash(candidateCount);
    elements.errorTabCount.textContent = numberOrDash(autoErrorCount);
    configureDownload("#download-preprocessed", "#preprocessed-name", payload.files?.preprocessed);
    configureDownload("#download-candidate", "#candidate-name", payload.files?.candidate);
    elements.resultPanel.hidden = false;
  };

  const activateJob = async (payload, options = {}) => {
    state.gridController?.abort();
    state.jobId = payload.job_id;
    state.columnFilters = { candidate: {}, auto_error: {}, preprocessed: {} };
    state.draftIds.clear();
    state.errorDraftIds.clear();
    state.errorDraftStatus.clear();
    state.canRollback = Boolean(payload.can_rollback);
    renderAnalysisResult(payload, options);
    await switchDataset("candidate");
  };

  const restoreCurrentJob = async (currentJob) => {
    if (!currentJob?.job_id) return;
    try {
      await activateJob(currentJob, { restored: true });
    } catch (_error) {
      state.jobId = null;
      elements.resultPanel.hidden = true;
      elements.workspace.hidden = true;
    }
  };

  const fetchCurrentJob = async (fallback = null) => {
    try {
      const response = await fetch("/api/admin/current-job", {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      return payload.current_job || null;
    } catch (_error) {
      return fallback;
    }
  };

  const fetchLatestEventId = async () => {
    try {
      const response = await fetch("/api/service-order/notifications/latest", {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) return 0;
      return Number(payload?.notification?.id) || 0;
    } catch (_error) {
      return 0;
    }
  };

  elements.toastClose?.addEventListener("click", hideToast);
  elements.updateClose?.addEventListener("click", () => setUpdateAlert("", false));
  elements.fileInput.addEventListener("change", () => setFile(elements.fileInput.files[0]));

  ["dragenter", "dragover"].forEach((eventName) => {
    elements.uploadZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.uploadZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    elements.uploadZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.uploadZone.classList.remove("is-dragging");
    });
  });
  elements.uploadZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    elements.fileInput.files = transfer.files;
    setFile(file);
  });

  elements.datasetTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => switchDataset(tab.dataset.dataset));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const nextIndex = (index + offset + elements.datasetTabs.length) % elements.datasetTabs.length;
      const nextTab = elements.datasetTabs[nextIndex];
      nextTab.focus();
      switchDataset(nextTab.dataset.dataset);
    });
  });
  elements.prevButton?.addEventListener("click", () => loadGrid(Math.max(1, state.page - 1)));
  elements.nextButton?.addEventListener("click", () => loadGrid(Math.min(state.totalPages, state.page + 1)));
  elements.refreshButton?.addEventListener("click", () => loadGrid(state.page));
  elements.approveButton?.addEventListener("click", approveDraft);
  elements.rollbackButton?.addEventListener("click", rollbackLastApproval);
  elements.excludeErrorsButton?.addEventListener("click", () => changeConfirmedErrors("exclude"));
  elements.restoreErrorsButton?.addEventListener("click", () => changeConfirmedErrors("restore"));
  elements.clearFiltersButton?.addEventListener("click", () => {
    state.columnFilters[state.dataset] = {};
    closeFilterMenu();
    updateToolbarState();
    loadGrid(1);
  });

  elements.newErrorToggle?.addEventListener("click", async () => {
    const willOpen = elements.newErrorPanel.hidden;
    elements.newErrorPanel.hidden = !willOpen;
    elements.newErrorToggle.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) await loadNewErrors(state.newErrors.page);
  });
  elements.newErrorDownload?.addEventListener("click", (event) => {
    if (elements.newErrorDownload.getAttribute("aria-disabled") === "true") event.preventDefault();
  });
  elements.newErrorRefresh?.addEventListener("click", () => loadNewErrors(state.newErrors.page));
  elements.newErrorClearFilters?.addEventListener("click", () => {
    state.newErrors.columnFilters = {};
    state.newErrors.downloadUrl = newErrorDownloadUrl();
    closeFilterMenu();
    updateToolbarState();
    updateNewErrorSummary();
    loadNewErrors(1);
  });
  elements.newErrorPrev?.addEventListener("click", () => loadNewErrors(Math.max(1, state.newErrors.page - 1)));
  elements.newErrorNext?.addEventListener("click", () => loadNewErrors(Math.min(state.newErrors.totalPages, state.newErrors.page + 1)));

  const updateFullscreenLabel = () => {
    const active = document.fullscreenElement === elements.workspace || state.fallbackFullscreen;
    elements.fullscreenButton.textContent = active ? "전체 화면 닫기" : "전체 화면";
  };

  elements.fullscreenButton?.addEventListener("click", async () => {
    closeFilterMenu();
    try {
      if (document.fullscreenElement === elements.workspace) {
        await document.exitFullscreen();
      } else if (elements.workspace.requestFullscreen) {
        await elements.workspace.requestFullscreen();
      } else {
        state.fallbackFullscreen = !state.fallbackFullscreen;
        elements.workspace.classList.toggle("is-fullscreen", state.fallbackFullscreen);
      }
    } catch (_error) {
      state.fallbackFullscreen = !state.fallbackFullscreen;
      elements.workspace.classList.toggle("is-fullscreen", state.fallbackFullscreen);
    }
    updateFullscreenLabel();
  });
  document.addEventListener("fullscreenchange", updateFullscreenLabel);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = elements.fileInput.files[0];
    if (!file) return;

    hideToast();
    stopAnalysisProgressPolling();
    elements.submitButton.disabled = true;
    state.analysisRequestInFlight = true;
    elements.submitButton.textContent = "분석 실행 중…";
    elements.processPanel.hidden = false;
    elements.errorPanel.hidden = true;
    closeFilterMenu();
    setAnalysisProgress(0, "업로드를 준비하고 있습니다.");

    const body = new FormData();
    body.append("file", file);
    const analysisId = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `analysis_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    startAnalysisProgressPolling(analysisId);
    try {
      const response = await fetch("/api/admin/analyze", {
        method: "POST",
        body,
        headers: { "X-Analysis-Id": analysisId },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(responseError(payload, response.status));
      setAnalysisProgress(100, "분석을 완료했습니다.", "complete");
      stopAnalysisProgressPolling();
      await activateJob(payload);
      publishDashboardRefresh("analysis_completed", payload);
      elements.processPanel.hidden = true;
      elements.workspace.scrollIntoView({ behavior: "smooth", block: "start" });
      showToast("분석을 완료했습니다. 후보를 선택한 뒤 승인해 주세요.");
      await Promise.all([checkUpdateRequirement(), loadNewErrors(1, { quiet: true })]);
    } catch (error) {
      stopAnalysisProgressPolling();
      showError(error.message || "알 수 없는 오류가 발생했습니다.");
    } finally {
      state.analysisRequestInFlight = false;
      elements.submitButton.disabled = false;
      elements.submitButton.textContent = "분석";
    }
  });

  const connectEvents = () => {
    if (!("EventSource" in window)) return;
    eventSource?.close();
    const eventUrl = state.lastEventId
      ? `/api/service-order/events?after=${state.lastEventId}`
      : "/api/service-order/events";
    eventSource = new EventSource(eventUrl);
    [
      "new_errors_approved",
      "error_approval_rolled_back",
      "confirmed_errors_excluded",
      "confirmed_errors_restored",
    ].forEach((eventName) => {
      eventSource.addEventListener(eventName, async (event) => {
        let envelope = {};
        try {
          envelope = JSON.parse(event.data || "{}");
        } catch (_error) {
          envelope = {};
        }
        state.lastEventId = Math.max(
          state.lastEventId,
          Number(envelope?.id) || 0,
        );
        if (state.mutationInFlight || state.liveRefreshInFlight) return;
        state.liveRefreshInFlight = true;
        try {
          const currentJob = await fetchCurrentJob();
          if (currentJob?.job_id === state.jobId) {
            renderAnalysisResult(currentJob);
            const targetDataset = [
              "error_approval_rolled_back",
              "confirmed_errors_excluded",
            ].includes(eventName)
              ? "candidate"
              : "auto_error";
            await Promise.all([
              switchDataset(targetDataset),
              loadNewErrors(1, { quiet: true }),
            ]);
          } else {
            await loadNewErrors(1, { quiet: true });
          }
          if (envelope?.message) showToast(String(envelope.message));
        } finally {
          state.liveRefreshInFlight = false;
        }
      });
    });
    eventSource.addEventListener("analysis_completed", async (event) => {
      let envelope;
      try {
        envelope = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      state.lastEventId = Math.max(
        state.lastEventId,
        Number(envelope?.id) || 0,
      );
      const eventJobId = envelope?.data?.job_id || envelope?.job_id;
      if (
        !eventJobId
        || eventJobId === state.jobId
        || state.analysisRequestInFlight
        || state.pendingAnalysisJobId
      ) return;
      state.pendingAnalysisJobId = eventJobId;
      try {
        const currentJob = await fetchCurrentJob();
        if (!currentJob?.job_id || currentJob.job_id === state.jobId) return;
        await activateJob(currentJob, { restored: true });
        showToast(`새 분석이 완료되어 ${currentJob.source_name || "최신 파일"} 결과로 교체했습니다.`);
      } catch (_error) {
        showToast("새 분석 결과를 자동으로 불러오지 못했습니다. 새로고침해 주세요.");
      } finally {
        state.pendingAnalysisJobId = null;
      }
    });
  };

  window.addEventListener("beforeunload", () => {
    stopAnalysisProgressPolling();
    state.gridController?.abort();
    state.newErrors.controller?.abort();
    eventSource?.close();
  });

  const boot = async () => {
    updateToolbarState();
    const health = await checkUpdateRequirement();
    const currentJob = await fetchCurrentJob(health?.current_job || null);
    await Promise.all([
      restoreCurrentJob(currentJob),
      loadNewErrors(1, { quiet: true }),
    ]);
    state.lastEventId = await fetchLatestEventId();
    connectEvents();
  };

  boot();
})();
