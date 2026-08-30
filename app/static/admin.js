(() => {
  "use strict";

  const ACTIVE_JOB_STATUSES = new Set(["PENDING", "QUEUED", "RUNNING"]);
  const ROLE_DEFINITIONS = [
    { id: "document_conversion", name: "文档转换模型", requirement: "必须通过 Vision 测试", hint: "用于识别扫描件与文档中的视觉内容。绑定前必须通过 Text 与 Vision 测试。" },
    { id: "index_generation", name: "索引生成模型", requirement: "低成本 · 文本 / JSON", hint: "推荐低成本文本/JSON模型；系统支持结构化输出回退。" },
    { id: "query_router", name: "查询路由模型", requirement: "速度快 · JSON 稳定", hint: "推荐速度快、结构化输出稳定的模型。" },
    { id: "answer_generation", name: "最终回答模型", requirement: "能力较强 · 推理", hint: "推荐能力较强的推理模型，只根据已经选中的证据回答。" },
  ];
  const REASONING_EFFORT_OPTIONS = [
    { value: "model_default", label: "跟随模型默认" },
    { value: "minimal", label: "极低（minimal）" },
    { value: "low", label: "低（low）" },
    { value: "medium", label: "中（medium）" },
    { value: "high", label: "高（high）" },
    { value: "xhigh", label: "极高（xhigh）" },
  ];
  const state = {
    files: [], jobs: [], providers: [], profiles: [], roles: [], index: null, tuning: null,
    pollTimer: null, pollBusy: false, bootstrapped: false, replaceFileId: null,
    selectedFileIds: new Set(), currentFileFolderPath: "", currentFileIds: new Set(), fileBulkBusy: false,
    jobDetail: null, jobItemStatus: null,
    progressJobId: null, progressFileId: null, progressDetail: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const loginView = $("#login-view");
  const appView = $("#admin-app");
  const loginForm = $("#login-form");
  const passwordInput = $("#admin-password");
  const loginError = $("#login-error");
  const replaceInput = $("#replace-input");

  const node = (tag, className = "", text = "") => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== "") element.textContent = text;
    return element;
  };
  const errorMessage = (payload, status) => {
    if (typeof payload?.detail === "string") return payload.detail;
    if (Array.isArray(payload?.detail)) return payload.detail.map((item) => item.msg || "输入内容无效").join("；");
    return `请求失败（${status}）`;
  };
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {
        ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401 && url !== "/api/auth/admin/login") showLogin();
      throw new Error(errorMessage(payload, response.status));
    }
    return response;
  };
  const jsonApi = async (url, options = {}) => (await api(url, options)).json();
  const showLogin = () => {
    stopPolling();
    appView.hidden = true;
    loginView.hidden = false;
    passwordInput.focus();
  };
  const showApp = () => {
    loginView.hidden = true;
    appView.hidden = false;
    bootstrap();
  };
  const toast = (message, type = "success") => {
    const item = node("div", `toast${type === "error" ? " error" : ""}`, message);
    $("#toast-region").append(item);
    window.setTimeout(() => item.remove(), 4300);
  };
  const selectTab = (name) => {
    $$('[data-tab]').forEach((tab) => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    $$('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
    history.replaceState(null, "", `#${name}`);
  };
  const parseDate = (value) => {
    if (!value) return null;
    const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
    const date = new Date(hasZone ? value : `${value}Z`);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const formatDate = (value) => {
    const date = parseDate(value);
    if (!date) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).format(date);
  };
  const formatBytes = (value) => {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let size = bytes / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && size >= 1024; index += 1) { size /= 1024; unit = units[index]; }
    return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${unit}`;
  };
  const fileExtensionLabel = (file) => {
    const filename = String(file.filename || "");
    const inferred = filename.includes(".") ? filename.split(".").pop() : "";
    return String(file.extension || inferred || "文件").replace(/^\./, "").slice(0, 5).toUpperCase() || "文件";
  };
  const formatDuration = (job) => {
    if (!job.started_at) return "—";
    const seconds = Math.max(0, Math.round(Number(job.elapsed_seconds || 0)));
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
  };
  const statusClass = (status) => {
    if (["PRESENT", "READY", "INDEXED", "COMPLETED", "passed"].includes(status)) return "good";
    if (["FAILED", "MISSING", "STOPPED", "failed"].includes(status)) return "bad";
    if (["CHANGED", "STALE", "UNSUPPORTED", "PAUSED", "partial"].includes(status)) return "warn";
    if (["PENDING", "QUEUED", "RUNNING", "CONVERTING"].includes(status)) return "info";
    return "";
  };
  const statusLabel = (status) => ({
    PRESENT: "存在", MISSING: "缺失", NEW: "待转换", CHANGED: "有变化", QUEUED: "已排队",
    CONVERTING: "转换中", READY: "就绪", FAILED: "失败", UNSUPPORTED: "不支持",
    NOT_INDEXED: "未索引", INDEXED: "已索引", STALE: "已过期", PENDING: "准备中",
    RUNNING: "运行中", COMPLETED: "已完成", PAUSED: "已暂停", STOPPED: "已停止", passed: "通过", partial: "部分通过", failed: "失败",
  }[status] || status || "未测试");
  const pill = (status) => node("span", `status-pill ${statusClass(status)}`, statusLabel(status));
  const button = (label, action, extraClass = "") => {
    const result = node("button", `row-button ${extraClass}`.trim(), label);
    result.type = "button";
    result.dataset.action = action;
    return result;
  };
  const setBusy = (control, busy, busyLabel = "处理中…") => {
    if (!control) return;
    if (busy) {
      control.dataset.idleLabel = control.textContent;
      control.textContent = busyLabel;
      control.disabled = true;
    } else {
      control.textContent = control.dataset.idleLabel || control.textContent;
      control.disabled = false;
    }
  };
  const withBusy = async (control, work, busyLabel) => {
    setBusy(control, true, busyLabel);
    try { return await work(); } finally { setBusy(control, false); }
  };
  const fileNameFor = (fileId) => {
    if (!fileId) return "—";
    return state.files.find((file) => file.id === fileId)?.relative_path || `文件 #${fileId}`;
  };
  const currentFileProgressButton = (job) => {
    if (
      job.job_type !== "document_conversion"
      || !ACTIVE_JOB_STATUSES.has(job.status)
      || !job.current_file_id
    ) return node("span", "muted-text", "—");
    const control = button(fileNameFor(job.current_file_id), "show-current-file-progress", "current-file-link");
    control.dataset.jobId = String(job.id);
    control.dataset.fileId = String(job.current_file_id);
    control.title = "查看这个文件的实时转换进度";
    return control;
  };

  const CONVERTIBLE_FILE_STATUSES = new Set(["NEW", "CHANGED", "FAILED", "UNSUPPORTED"]);
  const isFileConvertible = (file) => file.source_status === "PRESENT" && CONVERTIBLE_FILE_STATUSES.has(file.conversion_status);
  const isFileDeletable = (file) => !(file.source_status === "MISSING" && file.index_status !== "NOT_INDEXED");
  const isFileBulkSelectable = (file) => isFileConvertible(file) || isFileDeletable(file);
  const selectedFiles = () => state.files.filter((file) => state.selectedFileIds.has(file.id));
  const updateFileSelection = () => {
    const selectable = state.files.filter((file) => state.currentFileIds.has(file.id) && isFileBulkSelectable(file));
    const selectedHere = selectable.filter((file) => state.selectedFileIds.has(file.id));
    const selected = selectedFiles();
    const convertible = selected.filter(isFileConvertible);
    const deletable = selected.filter(isFileDeletable);
    const selectAll = $("#files-select-all");
    selectAll.checked = selectable.length > 0 && selectedHere.length === selectable.length;
    selectAll.indeterminate = selectedHere.length > 0 && selectedHere.length < selectable.length;
    selectAll.disabled = state.fileBulkBusy || !selectable.length;
    $("#file-selection-summary").textContent = selected.length
      ? `已选择 ${selected.length} 个文件 · ${convertible.length} 个可转换`
      : "已选择 0 个文件";
    const convert = $("#batch-convert-files");
    const remove = $("#batch-delete-files");
    if (!state.fileBulkBusy) {
      convert.textContent = convertible.length ? `转换（${convertible.length}）` : "转换";
      remove.textContent = deletable.length ? `删除（${deletable.length}）` : "删除";
    }
    convert.disabled = state.fileBulkBusy || !convertible.length;
    remove.disabled = state.fileBulkBusy || !deletable.length;
    convert.title = selected.length > convertible.length ? "仅转换所选文件中尚未完成转换的文件" : "";
  };

  const loadFiles = async () => {
    state.files = await jsonApi("/api/admin/files");
    renderFiles();
  };
  const compareTreeNames = (left, right) => left.localeCompare(right, "zh-CN", { numeric: true, sensitivity: "base" });
  const filesInFolder = (folderPath) => {
    const prefix = `${folderPath}/`;
    return state.files.filter((file) => file.relative_path.startsWith(prefix));
  };
  const buildFileTree = (files) => {
    const root = { folders: new Map(), files: [], fileCount: 0 };
    files.forEach((file) => {
      const pathParts = file.relative_path.split("/").filter(Boolean);
      pathParts.pop();
      let branch = root;
      let folderPath = "";
      branch.fileCount += 1;
      pathParts.forEach((folderName) => {
        folderPath = folderPath ? `${folderPath}/${folderName}` : folderName;
        if (!branch.folders.has(folderName)) {
          branch.folders.set(folderName, { name: folderName, path: folderPath, folders: new Map(), files: [], fileCount: 0 });
        }
        branch = branch.folders.get(folderName);
        branch.fileCount += 1;
      });
      branch.files.push(file);
    });
    return root;
  };
  const resolveFileTreeBranch = (root, folderPath) => {
    let branch = root;
    const resolvedParts = [];
    for (const part of folderPath.split("/").filter(Boolean)) {
      const nextBranch = branch.folders.get(part);
      if (!nextBranch) break;
      branch = nextBranch;
      resolvedParts.push(part);
    }
    return { branch, path: resolvedParts.join("/") };
  };
  const renderFileBreadcrumbs = (branch) => {
    const breadcrumbs = $("#file-breadcrumbs");
    breadcrumbs.replaceChildren();
    const parts = state.currentFileFolderPath.split("/").filter(Boolean);
    const entries = [{ label: "知识库", path: "" }];
    let accumulatedPath = "";
    parts.forEach((part) => {
      accumulatedPath = accumulatedPath ? `${accumulatedPath}/${part}` : part;
      entries.push({ label: part, path: accumulatedPath });
    });
    entries.forEach((entry, index) => {
      if (index) {
        const separator = node("span", "file-breadcrumb-separator", "›");
        separator.setAttribute("aria-hidden", "true");
        breadcrumbs.append(separator);
      }
      const isCurrent = index === entries.length - 1;
      const crumb = node(isCurrent ? "span" : "button", "file-breadcrumb", entry.label);
      if (isCurrent) crumb.setAttribute("aria-current", "page");
      else {
        crumb.type = "button";
        crumb.dataset.action = "open-file-folder";
        crumb.dataset.folderPath = entry.path;
      }
      breadcrumbs.append(crumb);
    });
    const parentPath = parts.slice(0, -1).join("/");
    const up = $("#file-browser-up");
    up.disabled = !parts.length;
    up.dataset.folderPath = parentPath;
    $("#file-directory-summary").textContent = `${branch.folders.size} 个文件夹 · ${branch.files.length} 个文件`;
  };
  const appendFileRow = (body, file) => {
    const row = node("tr", "file-tree-row");
    row.dataset.fileId = String(file.id);
    const selectionCell = node("td", "selection-cell");
    const selection = node("input", "file-select-box");
    selection.type = "checkbox";
    selection.dataset.fileSelect = String(file.id);
    selection.setAttribute("aria-label", `选择文件 ${file.relative_path}`);
    selection.checked = state.selectedFileIds.has(file.id);
    selection.disabled = !isFileBulkSelectable(file);
    selectionCell.append(selection);
    const nameCell = node("td", "file-tree-name-cell");
    const entry = node("div", "file-tree-entry");
    const icon = node("span", "file-library-icon file", fileExtensionLabel(file));
    icon.setAttribute("aria-hidden", "true");
    entry.append(icon, node("strong", "file-name", file.filename));
    nameCell.append(entry);
    const sourceCell = node("td"); sourceCell.append(pill(file.source_status));
    const conversionCell = node("td"); conversionCell.append(pill(file.conversion_status));
    const indexCell = node("td"); indexCell.append(pill(file.index_status));
    const errorCell = node("td");
    if (file.last_error) {
      const error = node("span", "error-text", file.last_error); error.title = file.last_error; errorCell.append(error);
    } else { errorCell.textContent = "—"; errorCell.className = "muted-text"; }
    const actionsCell = node("td");
    const actions = node("div", "row-actions");
    const replace = button("更新", "replace-file");
    replace.title = "用新文件更新内容，保留当前路径";
    const remove = button("删除", "delete-file", "danger");
    const conversionActive = ["QUEUED", "CONVERTING"].includes(file.conversion_status);
    const convert = button(conversionActive ? "转换中" : file.conversion_status === "READY" ? "已转换" : "转换", "convert-file");
    const preview = button("Markdown 预览", "preview-markdown");
    const download = node("a", "row-button", "下载源文件");
    download.href = `/api/files/${file.id}/download`; download.setAttribute("download", ""); download.dataset.action = "download-file";
    remove.disabled = !isFileDeletable(file);
    convert.disabled = !isFileConvertible(file);
    convert.title = file.conversion_status === "READY" ? "该文件已经完成转换" : conversionActive ? "转换任务正在进行" : "";
    preview.disabled = !file.converted_at;
    if (file.source_status !== "PRESENT") { download.removeAttribute("href"); download.setAttribute("aria-disabled", "true"); }
    actions.append(replace, remove, convert, preview, download); actionsCell.append(actions);
    row.append(
      selectionCell, nameCell, node("td", "", formatBytes(file.size)), sourceCell, conversionCell, indexCell,
      node("td", "muted-text", formatDate(file.converted_at)), errorCell, actionsCell,
    );
    body.append(row);
  };
  const appendFileFolderRow = (body, folder) => {
    const descendantFiles = filesInFolder(folder.path);
    const convertibleFiles = descendantFiles.filter(isFileConvertible);
    const deletableFiles = descendantFiles.filter(isFileDeletable);
    const row = node("tr", "file-folder-row");
    row.dataset.folderPath = folder.path;
    const selectionCell = node("td", "selection-cell folder-selection-cell");
    selectionCell.setAttribute("aria-hidden", "true");
    const nameCell = node("td", "file-tree-name-cell");
    const open = node("button", "file-folder-open");
    open.type = "button";
    open.dataset.action = "open-file-folder";
    open.dataset.folderPath = folder.path;
    open.setAttribute("aria-label", `打开文件夹 ${folder.name}`);
    const icon = node("span", "file-library-icon folder");
    icon.setAttribute("aria-hidden", "true");
    open.append(icon, node("strong", "file-folder-name", folder.name));
    nameCell.append(open);
    const conversionSummary = convertibleFiles.length ? ` · ${convertibleFiles.length} 个待转换` : "";
    const meta = node("td", "file-folder-meta", `${folder.folders.size} 个子文件夹 · 共 ${folder.fileCount} 个文件${conversionSummary}`);
    meta.colSpan = 6;
    const actionsCell = node("td");
    const actions = node("div", "row-actions");
    const convertAction = button(convertibleFiles.length ? `转换文件夹（${convertibleFiles.length}）` : "无需转换", "convert-file-folder");
    const deleteAction = button("删除文件夹", "delete-file-folder", "danger");
    convertAction.dataset.folderPath = folder.path;
    deleteAction.dataset.folderPath = folder.path;
    convertAction.disabled = !convertibleFiles.length;
    convertAction.title = convertibleFiles.length ? "转换该文件夹及所有子文件夹中尚待处理的文件" : "该文件夹中没有需要转换的文件";
    deleteAction.disabled = !deletableFiles.length;
    deleteAction.title = deletableFiles.length ? "删除整个文件夹及其磁盘内容" : "该文件夹仅包含已标记缺失的索引记录";
    actions.append(convertAction, deleteAction);
    actionsCell.append(actions);
    row.append(selectionCell, nameCell, meta, actionsCell);
    body.append(row);
  };
  const openFileFolder = (folderPath = "") => {
    state.currentFileFolderPath = folderPath;
    renderFiles();
    $("#file-browser-up").focus();
  };
  const renderFiles = () => {
    const body = $("#files-body");
    body.replaceChildren();
    const selectableIds = new Set(state.files.filter(isFileBulkSelectable).map((file) => file.id));
    state.selectedFileIds = new Set([...state.selectedFileIds].filter((fileId) => selectableIds.has(fileId)));
    const tree = buildFileTree(state.files);
    const current = resolveFileTreeBranch(tree, state.currentFileFolderPath);
    state.currentFileFolderPath = current.path;
    state.currentFileIds = new Set(current.branch.files.map((file) => file.id));
    renderFileBreadcrumbs(current.branch);
    if (!state.files.length) {
      const row = node("tr");
      const cell = node("td", "empty-cell", "还没有文件。上传文件或扫描源目录后会显示在这里。");
      cell.colSpan = 9;
      row.append(cell);
      body.append(row);
      updateFileSelection();
      return;
    }
    [...current.branch.folders.values()]
      .sort((left, right) => compareTreeNames(left.name, right.name))
      .forEach((folder) => appendFileFolderRow(body, folder));
    [...current.branch.files]
      .sort((left, right) => compareTreeNames(left.filename, right.filename))
      .forEach((file) => appendFileRow(body, file));
    if (!current.branch.folders.size && !current.branch.files.length) {
      const row = node("tr");
      const cell = node("td", "empty-cell", "这个文件夹是空的。");
      cell.colSpan = 9;
      row.append(cell);
      body.append(row);
    }
    updateFileSelection();
  };
  const scanFiles = async (control) => withBusy(control, async () => {
    const result = await jsonApi("/api/admin/files/scan", { method: "POST", body: "{}" });
    await loadFiles();
    toast(`扫描完成：新增 ${result.new}，变化 ${result.changed}，移除未索引记录 ${result.removed}，缺失 ${result.missing}，未变化 ${result.unchanged}`);
  }, "扫描中…");
  const convertChanged = async (control) => withBusy(control, async () => {
    const job = await jsonApi("/api/admin/jobs/convert-changed", { method: "POST", body: JSON.stringify({ retry: false }) });
    await jobStarted(job, job.total_items ? `已提交 ${job.total_items} 个文件的转换任务` : "没有需要转换的文件");
  }, "提交中…");
  const IGNORED_UPLOAD_FILE_NAMES = new Set([".ds_store", ".localized", "desktop.ini", "ehthumbs.db", "icon\r", "thumbs.db"]);
  const IGNORED_UPLOAD_DIRECTORY_NAMES = new Set(["$recycle.bin", ".fseventsd", ".spotlight-v100", ".temporaryitems", ".trashes", "__macosx", "system volume information"]);
  const isIgnoredUploadPath = (path) => {
    const parts = String(path || "").replaceAll("\\", "/").split("/").filter((part) => part && part !== ".");
    if (!parts.length) return false;
    if (parts.slice(0, -1).some((part) => IGNORED_UPLOAD_DIRECTORY_NAMES.has(part.toLocaleLowerCase()))) return true;
    const filename = parts[parts.length - 1].toLocaleLowerCase();
    return IGNORED_UPLOAD_FILE_NAMES.has(filename) || filename.startsWith("._") || filename.startsWith("~$") || filename.startsWith(".~lock.");
  };
  const selectedFilesFor = (form) => {
    const folderFiles = [...(form.elements.folder_files.files || [])];
    return folderFiles.length ? folderFiles : [...(form.elements.file.files || [])];
  };
  const isIgnoredUploadFile = (file) => isIgnoredUploadPath(file.webkitRelativePath || file.name) || isIgnoredUploadPath(file.name);
  const uploadFilesFor = (form) => selectedFilesFor(form).filter((file) => !isIgnoredUploadFile(file));
  const ignoredUploadCountFor = (form) => selectedFilesFor(form).filter(isIgnoredUploadFile).length;
  const selectedUploadKind = (files) => {
    if (files.some((file) => Boolean(file.webkitRelativePath))) return "folder";
    return files.length === 1 ? "file" : files.length > 1 ? "files" : "";
  };
  const renderUploadSelection = () => {
    const form = $("#upload-form");
    const files = uploadFilesFor(form);
    const ignoredCount = ignoredUploadCountFor(form);
    const uploadKind = selectedUploadKind(files);
    const targetFolder = state.currentFileFolderPath || "知识库根目录";
    $("#upload-target-folder").textContent = `上传位置：${targetFolder}`;
    form.dataset.uploadKind = uploadKind;
    const selection = $("#upload-selection");
    const relativePathField = $("#upload-relative-path-field");
    relativePathField.hidden = uploadKind !== "file";
    form.elements.relative_path.disabled = uploadKind !== "file";
    selection.classList.toggle("has-files", files.length > 0 || ignoredCount > 0);
    if (!files.length) {
      selection.textContent = ignoredCount
        ? `已忽略 ${ignoredCount} 个系统文件，没有可上传的知识文件。`
        : `请选择文件或文件夹；内容将上传到“${targetFolder}”。`;
      return;
    }
    const totalSize = files.reduce((sum, file) => sum + file.size, 0);
    const ignoredNote = ignoredCount ? `；已忽略 ${ignoredCount} 个系统文件` : "";
    if (uploadKind === "folder") {
      const firstPath = files[0].webkitRelativePath || files[0].name;
      const rootName = firstPath.split("/")[0] || "所选文件夹";
      selection.textContent = `已选择“${rootName}”：${files.length} 个文件，共 ${formatBytes(totalSize)}；将上传到“${targetFolder}”并保留目录结构${ignoredNote}。`;
      return;
    }
    if (uploadKind === "files") {
      selection.textContent = `已选择 ${files.length} 个文件，共 ${formatBytes(totalSize)}；文件将上传到“${targetFolder}”${ignoredNote}。`;
      return;
    }
    selection.textContent = `已选择“${files[0].name}” · ${formatBytes(totalSize)}${ignoredNote}`;
  };
  const handleUploadSelection = (selectedInput) => {
    const form = $("#upload-form");
    $$('input[type="file"]', form).forEach((input) => { if (input !== selectedInput) input.value = ""; });
    const error = $("[data-upload-error]", form);
    error.hidden = true;
    renderUploadSelection();
  };
  const openUpload = () => {
    const form = $("#upload-form");
    form.reset();
    form.dataset.uploadKind = "";
    renderUploadSelection();
    $("#upload-dialog").showModal();
  };
  const submitUpload = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = $('button[type="submit"]', form);
    const error = $("[data-upload-error]", form);
    const files = uploadFilesFor(form);
    const ignoredCount = ignoredUploadCountFor(form);
    const uploadKind = selectedUploadKind(files);
    error.hidden = true;
    if (!files.length) {
      error.textContent = ignoredCount
        ? `已忽略 ${ignoredCount} 个系统文件，请选择要上传的知识文件。`
        : "请选择要上传的文件或文件夹。";
      error.hidden = false;
      return;
    }

    const customPath = form.elements.relative_path?.value.trim() || "";
    const failures = [];
    let uploaded = 0;
    setBusy(submit, true, `上传中 0/${files.length}…`);
    try {
      for (const [index, file] of files.entries()) {
        submit.textContent = `上传中 ${index + 1}/${files.length}…`;
        const folderPath = (file.webkitRelativePath || "").replaceAll("\\", "/");
        const pathInCurrentFolder = (path) => state.currentFileFolderPath ? `${state.currentFileFolderPath}/${path}` : path;
        const relativePath = folderPath
          ? pathInCurrentFolder(folderPath)
          : uploadKind === "file" && customPath ? customPath : pathInCurrentFolder(file.name);
        const data = new FormData();
        data.append("file", file);
        data.append("relative_path", relativePath);
        try {
          await api("/api/admin/files/upload", { method: "POST", body: data });
          uploaded += 1;
        } catch (uploadError) {
          failures.push({ relativePath, message: uploadError.message });
        }
      }
      await loadFiles();
      if (!failures.length) {
        $("#upload-dialog").close();
        form.reset();
        const ignoredSuffix = ignoredCount ? `；已忽略 ${ignoredCount} 个系统文件` : "";
        toast(`${uploadKind === "folder" ? `文件夹上传完成：${uploaded} 个文件` : uploaded > 1 ? `已上传 ${uploaded} 个文件` : "文件已上传"}${ignoredSuffix}`);
        return;
      }
      const examples = failures.slice(0, 3).map((failure) => `${failure.relativePath}：${failure.message}`).join("；");
      const remainder = failures.length > 3 ? `；另有 ${failures.length - 3} 个失败` : "";
      error.textContent = `已上传 ${uploaded} 个，失败 ${failures.length} 个。${examples}${remainder}`;
      error.hidden = false;
      toast(`上传完成：成功 ${uploaded}，失败 ${failures.length}`, "error");
    } catch (uploadError) {
      error.textContent = uploadError.message;
      error.hidden = false;
      toast(uploadError.message, "error");
    } finally {
      setBusy(submit, false);
      renderUploadSelection();
    }
  };
  const chooseReplacement = (fileId) => { state.replaceFileId = fileId; replaceInput.value = ""; replaceInput.click(); };
  const replaceFile = async () => {
    const file = replaceInput.files?.[0]; const fileId = state.replaceFileId;
    if (!file || !fileId) return;
    const data = new FormData(); data.append("file", file);
    try {
      await api(`/api/admin/files/${fileId}/replace`, { method: "PUT", body: data });
      await loadFiles(); toast("源文件已更新，路径保持不变");
    } catch (error) { toast(error.message, "error"); }
    finally { state.replaceFileId = null; replaceInput.value = ""; }
  };
  const deleteFile = async (fileId) => {
    const file = state.files.find((item) => item.id === fileId);
    if (!file) return;
    const removeRecord = file.index_status === "NOT_INDEXED";
    const outcome = removeRecord ? "文件尚未索引，记录也会从列表中直接移除。" : "文件已经索引，记录将保留为“缺失”以使索引失效。";
    if (!window.confirm(`确定删除源文件“${file.relative_path}”吗？${outcome}此操作不可撤销。`)) return;
    try {
      await api(`/api/admin/files/${fileId}`, { method: "DELETE" });
      await loadFiles(); toast(removeRecord ? "源文件及未索引记录已删除" : "源文件已删除；已索引记录保留为“缺失”状态");
    } catch (error) { toast(error.message, "error"); }
  };
  const convertFile = async (fileId, control) => withBusy(control, async () => {
    const job = await jsonApi(`/api/admin/files/${fileId}/convert`, { method: "POST", body: "{}" });
    await loadFiles(); await jobStarted(job, "转换任务已提交");
  }, "提交中…").catch((error) => toast(error.message, "error"));
  const batchConvertFiles = async (control) => {
    const files = selectedFiles().filter(isFileConvertible);
    if (!files.length) { toast("请先选择尚未完成转换的文件", "error"); return; }
    state.fileBulkBusy = true;
    setBusy(control, true, "提交中…");
    updateFileSelection();
    try {
      const job = await jsonApi("/api/admin/files/batch-convert", {
        method: "POST",
        body: JSON.stringify({ file_ids: files.map((file) => file.id) }),
      });
      files.forEach((file) => state.selectedFileIds.delete(file.id));
      await jobStarted(job, `已提交 ${job.total_items} 个文件的转换任务`);
    } catch (error) { toast(error.message, "error"); }
    finally {
      state.fileBulkBusy = false;
      setBusy(control, false);
      updateFileSelection();
    }
  };
  const batchDeleteFiles = async (control) => {
    const files = selectedFiles().filter(isFileDeletable);
    if (!files.length) { toast("请先选择要删除的文件", "error"); return; }
    const indexedCount = files.filter((file) => file.index_status !== "NOT_INDEXED").length;
    const indexedNote = indexedCount ? `其中 ${indexedCount} 个文件已经索引，删除后记录会保留为“缺失”以使索引失效。` : "";
    if (!window.confirm(`确定删除选中的 ${files.length} 个源文件吗？${indexedNote}此操作不可撤销。`)) return;

    state.fileBulkBusy = true;
    setBusy(control, true, `删除中 0/${files.length}…`);
    updateFileSelection();
    const failures = [];
    let deleted = 0;
    try {
      for (const [index, file] of files.entries()) {
        control.textContent = `删除中 ${index + 1}/${files.length}…`;
        try {
          await api(`/api/admin/files/${file.id}`, { method: "DELETE" });
          state.selectedFileIds.delete(file.id);
          deleted += 1;
        } catch (error) { failures.push(`${file.relative_path}：${error.message}`); }
      }
      await loadFiles();
      if (failures.length) {
        const detail = failures.slice(0, 3).join("；");
        toast(`批量删除完成：成功 ${deleted}，失败 ${failures.length}。${detail}`, "error");
      } else { toast(`已删除 ${deleted} 个源文件`); }
    } catch (error) { toast(error.message, "error"); }
    finally {
      state.fileBulkBusy = false;
      setBusy(control, false);
      updateFileSelection();
    }
  };
  const convertFileFolder = async (folderPath, control) => {
    const files = filesInFolder(folderPath).filter(isFileConvertible);
    if (!folderPath || !files.length) { toast("该文件夹中没有需要转换的文件", "error"); return; }
    await withBusy(control, async () => {
      const job = await jsonApi("/api/admin/files/folder/convert", {
        method: "POST",
        body: JSON.stringify({ folder_path: folderPath }),
      });
      await jobStarted(job, job.total_items ? `已提交文件夹“${folderPath}”中的 ${job.total_items} 个文件` : "该文件夹中没有需要转换的文件");
    }, "提交中…").catch((error) => toast(error.message, "error"));
  };
  const deleteFileFolder = async (folderPath, control) => {
    const files = filesInFolder(folderPath);
    const deletableFiles = files.filter(isFileDeletable);
    if (!folderPath || !deletableFiles.length) { toast("该文件夹中没有可删除的源文件", "error"); return; }
    const indexedCount = files.filter((file) => file.index_status !== "NOT_INDEXED").length;
    const indexedNote = indexedCount ? `其中 ${indexedCount} 个文件已有索引，其记录会保留为“缺失/过期”，直到重建索引。` : "";
    const warning = `确定删除整个文件夹“${folderPath}”吗？将删除该目录及所有子目录中的磁盘内容，包括尚未扫描到的文件。${indexedNote}此操作不可撤销。`;
    if (!window.confirm(warning)) return;

    await withBusy(control, async () => {
      const result = await jsonApi("/api/admin/files/folder/delete", {
        method: "POST",
        body: JSON.stringify({ folder_path: folderPath }),
      });
      files.forEach((file) => state.selectedFileIds.delete(file.id));
      if (state.currentFileFolderPath === folderPath || state.currentFileFolderPath.startsWith(`${folderPath}/`)) {
        state.currentFileFolderPath = folderPath.split("/").slice(0, -1).join("/");
      }
      await loadFiles();
      const retained = result.marked_missing ? `；${result.marked_missing} 条已索引记录保留为缺失` : "";
      toast(`文件夹“${result.folder_path}”已删除：移除 ${result.deleted_records} 条记录${retained}`);
    }, "删除中…").catch((error) => toast(error.message, "error"));
  };
  const previewMarkdown = async (fileId, control) => withBusy(control, async () => {
    const preview = await jsonApi(`/api/admin/files/${fileId}/markdown`);
    const content = preview.parts.map((part) => `<!-- ${part.part_id} · ${part.path} -->\n${part.content}`).join("\n\n---\n\n");
    showPreview(preview.relative_path, `Markdown · ${preview.parts.length} 个分片`, content);
  }, "读取中…").catch((error) => toast(error.message, "error"));

  const loadJobs = async () => {
    state.jobs = await jsonApi("/api/admin/jobs"); renderJobs(); syncPolling();
  };
  const jobTypeLabel = (type) => ({ document_conversion: "文档转换", index_generation: "索引生成", test_background: "后台测试" }[type] || type);
  const effectiveJobStatus = (job) => job.control_state === "PAUSED" ? "PAUSED" : job.control_state === "STOPPED" ? "STOPPED" : job.status;
  const isJobTerminal = (job) => ["COMPLETED", "FAILED"].includes(job.status);
  const isJobActive = (job) => ACTIVE_JOB_STATUSES.has(job.status) && job.control_state !== "STOPPED";
  const jobManagementActions = (job) => {
    const actions = node("div", "job-management-actions");
    const add = (label, action, extraClass = "") => {
      const control = button(label, action, extraClass); control.dataset.jobId = String(job.id); actions.append(control); return control;
    };
    if (job.job_type === "index_generation") add("查看进度", "show-index-progress");
    else { const view = add("查看", "show-job-items"); view.dataset.itemStatus = "ALL"; }
    if (!isJobTerminal(job)) {
      if (job.control_state === "PAUSED") add("继续", "resume-job");
      else if (job.control_state !== "STOPPED") add("暂停", "pause-job");
      if (job.control_state === "STOPPED") {
        const restart = add(job.status === "RUNNING" ? "停止中…" : "重启", "restart-job");
        restart.disabled = job.status === "RUNNING";
        restart.title = restart.disabled ? "Worker 正在安全停止当前请求" : "从未完成文件继续执行";
      } else add("停止", "stop-job", "danger");
    }
    const remove = add("删除", "delete-job", "danger");
    remove.disabled = !isJobTerminal(job) && job.control_state !== "STOPPED";
    if (remove.disabled) remove.title = "请先停止任务";
    return actions;
  };
  const jobCountButton = (job, itemStatus, count, extraClass = "") => {
    const label = itemStatus === "COMPLETED" ? "成功" : "失败";
    const control = button(String(count), "show-job-items", `job-count-link ${extraClass}`.trim());
    control.dataset.jobId = String(job.id);
    control.dataset.itemStatus = itemStatus;
    control.setAttribute("aria-label", `查看任务 #${job.id} 的${label}文件，共 ${count} 个`);
    control.title = `查看${label}文件`;
    return control;
  };
  const jobProgressCount = (job) => {
    const summary = node("span", "job-count-summary");
    summary.append(jobCountButton(job, "COMPLETED", job.completed_items), node("span", "", `/ ${job.total_items}`));
    return summary;
  };
  const jobProgressBar = (job) => {
    const handled = job.completed_items + job.failed_items;
    const percentage = job.total_items ? Math.min(100, handled / job.total_items * 100) : 0;
    const track = node("div", "job-progress-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", `任务 #${job.id} 进度`);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Math.round(percentage)));
    const progress = node("div", "job-progress-fill");
    progress.style.width = `${percentage}%`;
    track.append(progress);
    return track;
  };
  const jobTableCell = (label, className = "", text = "") => {
    const cell = node("td", className, text);
    cell.dataset.label = label;
    return cell;
  };
  const renderJobs = () => {
    const deleteAllControl = $("#delete-all-jobs");
    const activeJobCount = state.jobs.filter(isJobActive).length;
    const hasActiveJobs = activeJobCount > 0;
    $("#jobs-summary").textContent = !state.jobs.length
      ? "暂无任务"
      : hasActiveJobs
        ? `${activeJobCount} 个任务处理中 · 共 ${state.jobs.length} 个`
        : `当前无运行任务 · 共 ${state.jobs.length} 个`;
    deleteAllControl.disabled = !state.jobs.length || hasActiveJobs;
    deleteAllControl.title = !state.jobs.length
      ? "当前没有任务记录"
      : hasActiveJobs
        ? "请先停止正在运行的任务"
        : "删除全部任务记录";
    const body = $("#jobs-body"); body.replaceChildren();
    const showCurrentFileColumn = state.jobs.some(
      (job) => isJobActive(job) && job.job_type === "document_conversion" && job.current_file_id,
    );
    $("#jobs-current-file-heading").hidden = !showCurrentFileColumn;
    if (!state.jobs.length) {
      const row = node("tr"); const cell = node("td", "empty-cell", "暂无后台任务。"); cell.colSpan = showCurrentFileColumn ? 8 : 7; row.append(cell); body.append(row); return;
    }
    const orderedJobs = [...state.jobs].sort((left, right) => Number(isJobActive(right)) - Number(isJobActive(left)));
    orderedJobs.forEach((job) => {
      const row = node("tr");
      row.dataset.jobId = String(job.id);
      row.setAttribute("aria-label", `${jobTypeLabel(job.job_type)}任务 #${job.id}`);
      row.classList.toggle("job-row-active", isJobActive(job));
      const statusCell = jobTableCell("状态", "job-card-status"); statusCell.append(pill(effectiveJobStatus(job)));
      const completedCell = jobTableCell("成功 / 总数", "job-progress-cell job-card-progress"); completedCell.append(jobProgressCount(job));
      if (isJobActive(job)) completedCell.append(jobProgressBar(job));
      const failedCell = jobTableCell("失败数", "job-card-failed"); failedCell.append(jobCountButton(job, "FAILED", job.failed_items, "failed"));
      const cells = [
        statusCell, jobTableCell("类型", "job-card-type", jobTypeLabel(job.job_type)), completedCell,
        failedCell,
      ];
      if (showCurrentFileColumn) {
        const currentFileCell = jobTableCell("当前文件", "job-card-current");
        currentFileCell.append(currentFileProgressButton(job));
        cells.push(currentFileCell);
      }
      const actionsCell = jobTableCell("操作", "job-actions-cell");
      cells.push(
        jobTableCell("开始时间", "muted-text job-card-start", formatDate(job.started_at)),
        jobTableCell("耗时", "muted-text job-card-duration", formatDuration(job)),
        actionsCell,
      );
      row.append(...cells);
      actionsCell.append(jobManagementActions(job));
      body.append(row);
    });
  };
  const jobItemFile = (item) => state.files.find((file) => file.id === item.source_file_id);
  const progressPhaseLabel = (phase) => ({
    preparing: "准备文件", extracting: "提取内容", analyzing: "分析页面", visual_enrichment: "识别视觉内容",
    table_extraction: "提取表格", extracted: "提取完成", writing: "写入 Markdown", publishing: "发布产物", completed: "已完成",
    inventory: "盘点可索引文档", document_cards: "生成文档卡片", folder_indexes: "更新文件夹索引", validating: "验证新索引", failed: "生成失败",
  }[phase] || phase || "等待开始");
  const progressPercentage = (progress) => {
    if (!progress) return 0;
    if (progress.phase === "completed") return 100;
    if (progress.kind === "index") {
      if (progress.phase === "publishing") return 98;
      if (progress.phase === "validating") return 95;
      if (progress.phase === "folder_indexes") return 82 + (progress.total_folders ? 11 * Number(progress.folders_completed || 0) / Number(progress.total_folders) : 0);
      if (progress.phase === "document_cards") {
        if (progress.model_requests_total) return 8 + 72 * Number(progress.model_requests_completed || 0) / Number(progress.model_requests_total);
        return 8 + 72 * Number(progress.documents_completed || 0) / Math.max(1, Number(progress.total_documents || 1));
      }
      return progress.phase === "failed" ? 0 : 5;
    }
    if (progress.phase === "publishing") return 99;
    if (progress.phase === "writing") return progress.total_parts ? 90 + 9 * Number(progress.written_parts || 0) / Number(progress.total_parts) : 90;
    if (progress.total_pages) {
      if (progress.phase === "analyzing") return 35 * Number(progress.analyzed_pages || 0) / Number(progress.total_pages);
      return 35 + 55 * Math.min(1, (Number(progress.direct_text_pages || 0) + Number(progress.visual_pages_completed || 0)) / Number(progress.total_pages));
    }
    if (progress.embedded_visuals_total) return 55 + 35 * Number(progress.embedded_visuals_completed || 0) / Number(progress.embedded_visuals_total);
    if (progress.total_sheets) return 10 + 75 * Number(progress.completed_sheets || 0) / Number(progress.total_sheets);
    if (progress.image_total) return 50 + 40 * Number(progress.image_completed || 0) / Number(progress.image_total);
    if (progress.phase === "extracted") return 90;
    return progress.phase === "extracting" ? 8 : 3;
  };
  const progressMetrics = (progress) => {
    const metrics = [];
    const add = (label, value, suffix = "") => {
      if (value !== undefined && value !== null) metrics.push([label, `${Number(value).toLocaleString("zh-CN")}${suffix}`]);
    };
    const addText = (label, value) => { if (value !== undefined && value !== null && value !== "") metrics.push([label, String(value)]); };
    if (progress.kind === "index") {
      add("索引代数", progress.generation_number, ""); add("文档总数", progress.total_documents, " 个");
      add("需更新卡片", progress.documents_to_refresh, " 个"); add("复用文档卡片", progress.documents_reused, " 个");
      add("断点卡片命中", progress.document_card_cache_hits, " 个"); add("已完成文档", progress.documents_completed, " 个");
      addText("最近完成文档", progress.last_completed_document_name || progress.current_document_name);
      add("该文档分片", progress.last_completed_document_parts ?? progress.current_document_parts, " 个");
      add("模型请求", progress.model_requests_total, " 次"); add("已完成模型请求", progress.model_requests_completed, " 次");
      add("模型缓存命中", progress.model_cache_hits, " 个"); add("文件夹总数", progress.total_folders, " 个");
      add("复用文件夹", progress.folders_reused, " 个"); add("重建文件夹", progress.folders_rebuilt, " 个");
    } else if (progress.total_pages !== undefined) {
      add("总页数", progress.total_pages, " 页"); add("已分析", progress.analyzed_pages, " 页");
      add("直接文本页", progress.direct_text_pages, " 页"); add("需视觉模型页", progress.visual_pages, " 页");
      add("已完成视觉页", progress.visual_pages_completed, " 页"); add("视觉缓存命中", progress.visual_cache_hits, " 页");
      add("本次大模型请求", progress.model_requests, " 次");
    } else if (progress.kind === "pptx") {
      add("幻灯片总数", progress.total_slides, " 页"); add("已提取幻灯片", progress.slides_extracted, " 页");
      add("含直接文本页", progress.slides_with_text, " 页"); add("含嵌入图片页", progress.slides_with_visuals, " 页");
    } else if (progress.total_sheets !== undefined) {
      add("工作表总数", progress.total_sheets, " 个"); add("已提取工作表", progress.completed_sheets, " 个");
      add("表格分片", progress.table_parts_completed, " 个");
    } else if (progress.image_width !== undefined) {
      add("图像宽度", progress.image_width, " px"); add("图像高度", progress.image_height, " px"); add("帧数", progress.image_frames, " 帧");
    } else {
      add("本地提取文字", progress.deterministic_text_characters, " 字符");
    }
    if (progress.embedded_visuals_total !== undefined) {
      add("嵌入图片", progress.embedded_visuals_total, " 张"); add("已处理图片", progress.embedded_visuals_completed, " 张");
      add("图片缓存命中", progress.embedded_visuals_cache_hits, " 张"); add("本次大模型请求", progress.embedded_visuals_model_requests, " 次");
      add("已完成模型请求", progress.embedded_visuals_model_completed, " 次");
      add("旧式矢量图跳过", progress.embedded_visuals_legacy, " 张");
    }
    add("Markdown 分片", progress.total_parts, " 个"); add("已写入分片", progress.written_parts, " 个");
    return metrics;
  };
  const progressNote = (progress) => {
    if (!progress) return "转换 Worker 正在准备文件，稍后会出现更详细的统计。";
    if (progress.kind === "index") return "索引使用差量文档卡片和不可变代际快照：未变更的卡片和文件夹直接复用，大文档分批并发摘要，全部验证通过后才切换当前索引。";
    if (progress.total_pages !== undefined) return "PDF 有可用文本的页面会直接提取；只有低文本、扫描或视觉页调用大模型。缓存命中不会重复计费。";
    if (progress.kind === "pptx") return "PPT 的可编辑文本由本地直接提取，只有嵌入图片需要视觉模型；“含文本页”与“含图片页”可以重叠。";
    if (["xlsx", "xls", "csv", "tsv"].includes(progress.kind)) return "表格单元格在本地直接提取；仅 XLSX 中的嵌入图片可能调用视觉模型。";
    if (progress.kind === "docx") return "Word 文本在本地提取，只有嵌入图片需要视觉模型。";
    if (["png", "jpg", "jpeg", "webp"].includes(progress.kind)) return "图片文件会作为一个视觉任务处理，已命中缓存时不会再调用模型。";
    return "纯文本和结构化文件在本地直接转换，不需要视觉模型。";
  };
  const renderFileProgress = () => {
    const detail = state.progressDetail;
    if (!detail) return;
    const item = detail.job_type === "index_generation"
      ? detail.items[0]
      : detail.items.find((candidate) => candidate.source_file_id === state.progressFileId);
    const progress = item?.progress || null;
    const file = state.files.find((candidate) => candidate.id === state.progressFileId);
    $("#file-progress-kicker").textContent = `${jobTypeLabel(detail.job_type)} · 任务 #${detail.id}`;
    $("#file-progress-title").textContent = detail.job_type === "index_generation"
      ? (progress?.last_completed_document_name || progress?.current_document_name || `索引任务 #${detail.id}`)
      : (file?.relative_path || `文件 #${state.progressFileId}`);
    $("#file-progress-status").replaceChildren(pill(item?.status || "PENDING"));
    $("#file-progress-phase").textContent = progressPhaseLabel(progress?.phase);
    const percentage = Math.max(0, Math.min(100, progressPercentage(progress)));
    $("#file-progress-fill").style.width = `${percentage}%`;
    $("#file-progress-dialog [role='progressbar']").setAttribute("aria-valuenow", String(Math.round(percentage)));
    $("#file-progress-percent").textContent = `${Math.round(percentage)}%`;
    const metrics = $("#file-progress-metrics"); metrics.replaceChildren();
    const entries = progressMetrics(progress || {});
    if (!entries.length) metrics.append(node("p", "file-progress-empty", "正在等待第一次进度更新…"));
    else entries.forEach(([label, value]) => {
      const metric = node("div", `file-progress-metric${label === "最近完成文档" ? " wide" : ""}`);
      metric.append(node("span", "", label), node("strong", "", value)); metrics.append(metric);
    });
    $("#file-progress-note").textContent = item?.error ? `错误：${item.error}` : progressNote(progress);
    $("#file-progress-updated").textContent = progress?.updated_at ? `更新于 ${formatDate(progress.updated_at)}` : "等待 Worker 上报";
  };
  const refreshFileProgress = async () => {
    if (!state.progressJobId || !$("#file-progress-dialog").open) return;
    state.progressDetail = await jsonApi(`/api/admin/jobs/${state.progressJobId}`);
    renderFileProgress();
  };
  const openFileProgress = async (jobId, fileId, control) => {
    state.progressJobId = jobId; state.progressFileId = fileId; state.progressDetail = null;
    const dialog = $("#file-progress-dialog");
    $("#file-progress-title").textContent = fileId ? fileNameFor(fileId) : `索引任务 #${jobId}`;
    $("#file-progress-phase").textContent = "正在读取进度…";
    $("#file-progress-metrics").replaceChildren();
    if (!dialog.open) dialog.showModal();
    if (control) control.disabled = true;
    try { await refreshFileProgress(); }
    catch (error) { dialog.close(); toast(error.message, "error"); }
    finally { if (control) control.disabled = false; }
  };
  const retryableJobItems = () => {
    if (!["FAILED", "ALL"].includes(state.jobItemStatus) || !state.jobDetail) return [];
    return state.jobDetail.items.filter((item) => {
      const file = jobItemFile(item);
      return item.status === "FAILED" && file && isFileConvertible(file);
    });
  };
  const renderJobItems = () => {
    const detail = state.jobDetail;
    const itemStatus = state.jobItemStatus;
    if (!detail || !itemStatus) return;
    const successful = itemStatus === "COMPLETED";
    const allItems = itemStatus === "ALL";
    const items = allItems ? detail.items : detail.items.filter((item) => item.status === itemStatus);
    $("#job-items-kicker").textContent = `${jobTypeLabel(detail.job_type)} · 任务 #${detail.id}`;
    $("#job-items-title").textContent = allItems ? "任务明细" : successful ? "成功文件" : "失败文件";
    $("#job-items-summary").textContent = allItems
      ? `共 ${items.length} 个任务项；成功 ${detail.completed_items}，失败 ${detail.failed_items}`
      : `共 ${items.length} 个${successful ? "成功" : "失败"}项${successful ? "" : "；可重试的文件会显示操作按钮"}`;
    const body = $("#job-items-body"); body.replaceChildren();
    if (!items.length) {
      const row = node("tr"); const cell = node("td", "empty-cell", allItems ? "这个任务没有任务项。" : successful ? "这个任务还没有成功文件。" : "这个任务没有失败文件。");
      cell.colSpan = 6; row.append(cell); body.append(row);
    } else items.forEach((item) => {
      const file = jobItemFile(item);
      const row = node("tr"); row.dataset.fileId = item.source_file_id ? String(item.source_file_id) : "";
      const nameCell = node("td");
      const itemName = detail.job_type === "index_generation" ? "索引生成详情" : file?.relative_path || (item.source_file_id ? `文件 #${item.source_file_id}（记录已不存在）` : `任务项 #${item.id}`);
      if (item.source_file_id && item.progress) {
        const progressLink = button(itemName, "show-current-file-progress", "job-item-path current-file-link");
        progressLink.dataset.jobId = String(detail.id); progressLink.dataset.fileId = String(item.source_file_id); nameCell.append(progressLink);
      } else if (detail.job_type === "index_generation" && item.progress) {
        const progressLink = button(itemName, "show-index-progress", "job-item-path current-file-link");
        progressLink.dataset.jobId = String(detail.id); nameCell.append(progressLink);
      } else nameCell.append(node("span", "job-item-path", itemName));
      const statusCell = node("td"); statusCell.append(pill(item.status));
      const errorCell = node("td");
      if (item.error) errorCell.append(node("span", "job-item-error", item.error));
      else { errorCell.className = "muted-text"; errorCell.textContent = "—"; }
      const actionCell = node("td");
      if (item.status === "FAILED" && file && isFileConvertible(file)) {
        const retry = button("重试", "retry-job-item"); retry.dataset.fileId = String(file.id); actionCell.append(retry);
      } else {
        const reason = item.status === "FAILED" && file ? "已重试或状态已变化" : "—";
        actionCell.append(node("span", "job-item-action-note", reason));
      }
      row.append(nameCell, statusCell, node("td", "", String(item.attempts)), node("td", "muted-text", formatDate(item.finished_at)), errorCell, actionCell);
      body.append(row);
    });
    const retryAll = $("#retry-job-items");
    const retryable = retryableJobItems();
    retryAll.hidden = successful || !retryable.length;
    retryAll.textContent = retryable.length ? `重试失败文件（${retryable.length}）` : "重试失败文件";
  };
  const openJobItems = async (jobId, itemStatus, control) => {
    const dialog = $("#job-items-dialog");
    state.jobDetail = null; state.jobItemStatus = itemStatus;
    $("#job-items-kicker").textContent = `任务 #${jobId}`;
    $("#job-items-title").textContent = itemStatus === "ALL" ? "任务明细" : itemStatus === "COMPLETED" ? "成功文件" : "失败文件";
    $("#job-items-summary").textContent = "正在读取任务明细…";
    const body = $("#job-items-body"); body.replaceChildren();
    const row = node("tr"); const cell = node("td", "empty-cell", "正在读取任务明细…"); cell.colSpan = 6; row.append(cell); body.append(row);
    $("#retry-job-items").hidden = true;
    if (!dialog.open) dialog.showModal();
    control.disabled = true;
    try {
      state.jobDetail = await jsonApi(`/api/admin/jobs/${jobId}`);
      renderJobItems();
    } catch (error) {
      dialog.close(); toast(error.message, "error");
    } finally { control.disabled = false; }
  };
  const retryJobItem = async (fileId, control) => withBusy(control, async () => {
    const job = await jsonApi(`/api/admin/files/${fileId}/convert`, { method: "POST", body: "{}" });
    $("#job-items-dialog").close();
    await jobStarted(job, "失败文件已重新提交");
  }, "提交中…").catch((error) => toast(error.message, "error"));
  const retryJobItems = async (control) => {
    const items = retryableJobItems();
    if (!items.length) { toast("当前没有可重试的失败文件", "error"); return; }
    await withBusy(control, async () => {
      const job = await jsonApi("/api/admin/files/batch-convert", {
        method: "POST",
        body: JSON.stringify({ file_ids: items.map((item) => item.source_file_id) }),
      });
      $("#job-items-dialog").close();
      await jobStarted(job, `已重新提交 ${job.total_items} 个失败文件`);
    }, "提交中…").catch((error) => toast(error.message, "error"));
  };
  const updateJobControl = async (jobId, action, control, message) => withBusy(control, async () => {
    await jsonApi(`/api/admin/jobs/${jobId}/${action}`, { method: "POST", body: "{}" });
    await Promise.all([loadJobs(), loadFiles()]);
    toast(message);
  }, "处理中…").catch((error) => toast(error.message, "error"));
  const deleteJob = async (jobId, control) => {
    if (!window.confirm(`确定删除任务 #${jobId} 的任务记录吗？文件转换产物不会被删除。`)) return;
    await withBusy(control, async () => {
      await api(`/api/admin/jobs/${jobId}`, { method: "DELETE" });
      if (state.jobDetail?.id === jobId && $("#job-items-dialog").open) $("#job-items-dialog").close();
      await Promise.all([loadJobs(), loadFiles()]);
      toast(`任务 #${jobId} 已删除`);
    }, "删除中…").catch((error) => toast(error.message, "error"));
  };
  const deleteAllJobs = async (control) => {
    if (!state.jobs.length) return;
    if (!window.confirm(`确定删除全部 ${state.jobs.length} 条任务记录吗？文件转换产物不会被删除。此操作不可撤销。`)) return;
    await withBusy(control, async () => {
      const result = await jsonApi("/api/admin/jobs", { method: "DELETE" });
      state.jobDetail = null;
      if ($("#job-items-dialog").open) $("#job-items-dialog").close();
      if ($("#file-progress-dialog").open) $("#file-progress-dialog").close();
      await Promise.all([loadJobs(), loadFiles()]);
      toast(`已删除 ${result.deleted_count} 条任务记录`);
    }, "删除中…").catch((error) => toast(error.message, "error"));
  };
  const jobStarted = async (job, message) => {
    toast(message); await Promise.all([loadJobs(), loadFiles()]);
  };
  const syncPolling = () => {
    const needsPolling = state.jobs.some((job) => ACTIVE_JOB_STATUSES.has(job.status) && (job.control_state === "ACTIVE" || job.status === "RUNNING"));
    if (needsPolling) {
      if (!state.pollTimer) state.pollTimer = window.setInterval(async () => {
        if (state.pollBusy || appView.hidden) return;
        state.pollBusy = true;
        try { await Promise.all([loadJobs(), loadFiles(), loadIndex(), refreshFileProgress()]); }
        catch (error) { if (!appView.hidden) toast(error.message, "error"); }
        finally { state.pollBusy = false; }
      }, 1000);
    } else stopPolling();
  };
  const stopPolling = () => { if (state.pollTimer) window.clearInterval(state.pollTimer); state.pollTimer = null; };

  const providerTypeLabel = (type) => ({ openai_compatible: "OpenAI Compatible", azure_openai: "Azure OpenAI", sub2api: "Sub2API" }[type] || type);
  const protocolLabel = (protocol) => ({ auto: "Auto", responses: "Responses", chat_completions: "Chat Completions" }[protocol] || protocol || "继承 Provider");
  const loadModels = async () => {
    const [providers, profiles, roles] = await Promise.all([jsonApi("/api/admin/providers"), jsonApi("/api/admin/model-profiles"), jsonApi("/api/admin/model-roles")]);
    state.providers = providers; state.profiles = profiles; state.roles = roles;
    renderProviders(); renderProfiles(); renderRoles(); populateProfileProviderOptions();
  };
  const labeledValue = (className, label, value, useCode = false) => {
    const cell = node("div", className); cell.append(node("span", "", label), node(useCode ? "code" : "strong", "", value)); return cell;
  };
  const renderProviders = () => {
    const list = $("#providers-list"); list.replaceChildren();
    if (!state.providers.length) { list.append(node("div", "empty-state-card", "尚未配置 Provider。先添加一个远程连接，再为它创建 Model Profile。")); return; }
    state.providers.forEach((provider) => {
      const card = node("article", "provider-card"); card.dataset.providerId = String(provider.id);
      card.append(
        labeledValue("provider-cell", "Name", provider.name), labeledValue("provider-cell", "Type", providerTypeLabel(provider.provider_type)),
        labeledValue("provider-cell", "Base URL", provider.base_url, true), labeledValue("provider-cell", "Protocol", protocolLabel(provider.protocol_preference)),
      );
      const credential = node("div", "provider-cell"); credential.append(node("span", "", "Credential status"));
      const line = node("div", "credential-line"); line.append(node("b", "credential-check", "✓"), node("code", "", `${provider.api_key_masked}${provider.enabled ? "" : " · 已停用"}`)); credential.append(line);
      const actions = node("div", "card-actions"); actions.append(button("添加模型", "add-profile"), button("编辑", "edit-provider"), button("删除", "delete-provider", "danger"));
      card.append(credential, actions); list.append(card);
    });
  };
  const capability = (supported) => node("span", `capability ${supported ? "yes" : "no"}`, supported ? "✓ 支持" : "— 未通过");
  const renderProfiles = () => {
    const list = $("#profiles-list"); list.replaceChildren();
    if (!state.providers.length) { list.append(node("div", "empty-state-card", "添加 Provider 后即可创建 Model Profile。")); return; }
    state.providers.forEach((provider) => {
      const profiles = state.profiles.filter((profile) => profile.provider_id === provider.id);
      const group = node("article", "profile-group"); const heading = node("div", "profile-group-heading"); const text = node("div");
      text.append(node("strong", "", provider.name), node("span", "", `${profiles.length} 个 Model Profile`));
      const add = button("添加 Model Profile", "add-profile"); add.dataset.providerId = String(provider.id); heading.append(text, add);
      const table = node("div", "profile-table");
      if (!profiles.length) table.append(node("div", "empty-state-card", "此 Provider 下还没有 Model Profile。"));
      else profiles.forEach((profile) => {
        const row = node("div", "profile-row"); row.dataset.profileId = String(profile.id);
        const remoteModel = labeledValue("profile-cell", "Remote Model / Deployment", profile.remote_model_name, true);
        remoteModel.append(node("small", "profile-limit", `能力上限：${Number(profile.context_window || 32768).toLocaleString()} context · ${Number(profile.max_output_tokens || 4096).toLocaleString()} output`));
        row.append(
          labeledValue("profile-cell", "Profile Name", profile.name), remoteModel,
          labeledValue("profile-cell", "Protocol", protocolLabel(profile.tested_protocol || profile.protocol_override || provider.protocol_preference)),
        );
        const textCell = node("div", "profile-cell"); textCell.append(node("span", "", "Text"), capability(profile.supports_text));
        const visionCell = node("div", "profile-cell"); visionCell.append(node("span", "", "Vision"), capability(profile.supports_vision));
        const structuredCell = node("div", "profile-cell"); structuredCell.append(node("span", "", "Structured Output"), capability(profile.supports_structured_output));
        const lastTest = node("div", "profile-cell"); lastTest.append(node("span", "", "Last Test")); lastTest.append(profile.last_test_status ? pill(profile.last_test_status) : node("strong", "muted-text", "未测试"));
        if (profile.last_tested_at) lastTest.append(node("code", "", formatDate(profile.last_tested_at)));
        const latency = labeledValue("profile-cell", "Latency", profile.last_test_latency_ms === null ? "—" : `${profile.last_test_latency_ms} ms`);
        const actions = node("div", "card-actions"); actions.append(button("测试模型", "test-profile"), button("编辑", "edit-profile"), button("删除", "delete-profile", "danger"));
        row.append(textCell, visionCell, structuredCell, lastTest, latency, actions); table.append(row);
      });
      group.append(heading, table); list.append(group);
    });
  };
  const isProfileCompatible = (profile, roleId) => {
    if (!profile.enabled || !profile.supports_text) return false;
    if (roleId === "document_conversion") return profile.supports_vision;
    if (roleId === "query_router") return ["passed", "partial"].includes(profile.last_test_status);
    return true;
  };
  const renderRoles = () => {
    const grid = $("#roles-grid"); grid.replaceChildren();
    const roleRecords = new Map(state.roles.map((binding) => [binding.role, binding]));
    ROLE_DEFINITIONS.forEach((definition) => {
      const roleRecord = roleRecords.get(definition.id); const card = node("article", "role-card"); card.dataset.roleId = definition.id;
      const header = node("div", "role-card-header"); const title = node("div");
      title.append(node("h4", "", definition.name), node("p", "role-id", definition.id)); header.append(title, node("span", "status-pill info", definition.requirement));
      const select = node("select"); select.dataset.role = definition.id; select.dataset.roleConfig = "model"; select.setAttribute("aria-label", `${definition.name} Model Profile`); select.append(new Option("未绑定", ""));
      const currentId = roleRecord?.model_profile_id;
      state.profiles.forEach((profile) => {
        const provider = state.providers.find((item) => item.id === profile.provider_id);
        const option = new Option(`${provider?.name || "Provider"} / ${profile.name}`, String(profile.id));
        option.disabled = !isProfileCompatible(profile, definition.id) && currentId !== profile.id; select.append(option);
      });
      select.value = currentId === null || currentId === undefined ? "" : String(currentId);
      const effortSelect = node("select"); effortSelect.dataset.role = definition.id; effortSelect.dataset.roleConfig = "reasoning"; effortSelect.setAttribute("aria-label", `${definition.name}推理强度`);
      REASONING_EFFORT_OPTIONS.forEach((option) => effortSelect.append(new Option(option.label, option.value)));
      effortSelect.value = roleRecord?.reasoning_effort || roleRecord?.default_reasoning_effort || "model_default";
      effortSelect.disabled = !currentId;
      const configGrid = node("div", "role-config-grid");
      const modelField = node("label", "role-config-field"); modelField.append(node("span", "", "使用模型"), select);
      const effortField = node("label", "role-config-field"); effortField.append(node("span", "", "推理强度"), effortSelect);
      configGrid.append(modelField, effortField);
      const promptTasks = roleRecord?.prompt_tasks || [];
      const customizedPromptCount = promptTasks.filter((task) => task.prompt !== task.default_prompt).length;
      const promptDetails = node("details", "role-prompt-details");
      const summary = node("summary", "role-prompt-summary");
      summary.append(
        node("span", "", `任务提示词（${promptTasks.length}）`),
        node("small", customizedPromptCount ? "customized" : "", customizedPromptCount ? `${customizedPromptCount} 项已修改` : "当前使用默认值"),
      );
      promptDetails.append(summary);
      const promptList = node("div", "role-prompt-list");
      promptTasks.forEach((task) => {
        const field = node("details", "role-prompt-field");
        const labelRow = node("summary", "role-prompt-label");
        const promptTitle = node("span", "role-prompt-title");
        promptTitle.append(node("strong", "", task.name), node("small", "", task.description));
        labelRow.append(promptTitle, node("code", "", task.task));
        const textarea = node("textarea"); textarea.dataset.rolePrompt = task.task; textarea.dataset.defaultPrompt = task.default_prompt;
        textarea.setAttribute("aria-label", `${definition.name}：${task.name}`);
        textarea.value = task.prompt; textarea.rows = Math.min(9, Math.max(5, Math.ceil(task.prompt.length / 88))); textarea.spellcheck = false;
        field.append(labelRow, textarea); promptList.append(field);
      });
      const promptActions = node("div", "role-prompt-actions");
      const reset = button("恢复本角色默认值", "reset-role-prompts"); reset.dataset.role = definition.id;
      const save = button("保存提示词", "save-role-prompts", "primary"); save.dataset.role = definition.id;
      promptActions.append(reset, save); promptDetails.append(promptList, promptActions);
      card.append(header, node("p", "role-hint", definition.hint), configGrid, promptDetails); grid.append(card);
    });
  };
  const populateProfileProviderOptions = () => {
    const select = $('#profile-form select[name="provider_id"]'); const current = select.value; select.replaceChildren();
    state.providers.forEach((provider) => select.append(new Option(provider.name, String(provider.id))));
    if (current && state.providers.some((provider) => String(provider.id) === current)) select.value = current;
  };
  const parseObjectJSON = (value, label) => {
    let result; try { result = JSON.parse(value || "{}"); } catch (_error) { throw new Error(`${label} 必须是有效 JSON`); }
    if (!result || Array.isArray(result) || typeof result !== "object") throw new Error(`${label} 必须是 JSON 对象`); return result;
  };
  const updateAzureFields = () => { const form = $("#provider-form"); $("#azure-fields").hidden = form.elements.provider_type.value !== "azure_openai"; };
  const openProvider = (providerId = null) => {
    const dialog = $("#provider-dialog"); const form = $("#provider-form"); const error = $('[data-form-error]', form);
    form.reset(); error.hidden = true; form.elements.extra_headers_json.value = "{}"; form.elements.enabled.checked = true;
    const provider = state.providers.find((item) => item.id === providerId); form.elements.provider_id.value = provider ? String(provider.id) : "";
    $("#provider-dialog-title").textContent = provider ? "编辑 Provider" : "添加 Provider";
    $("#credential-help").textContent = provider ? `留空则保留 ${provider.api_key_masked}` : "必填"; form.elements.api_key.required = !provider;
    if (provider) {
      form.elements.name.value = provider.name; form.elements.provider_type.value = provider.provider_type; form.elements.base_url.value = provider.base_url;
      form.elements.protocol_preference.value = provider.protocol_preference; form.elements.azure_mode.value = provider.azure_mode;
      form.elements.azure_api_version.value = provider.azure_api_version || ""; form.elements.extra_headers_json.value = JSON.stringify(provider.extra_headers_json, null, 2); form.elements.enabled.checked = provider.enabled;
    }
    updateAzureFields(); dialog.showModal();
  };
  const submitProvider = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const error = $('[data-form-error]', form); const submit = $('button[type="submit"]', form); error.hidden = true;
    try {
      const providerId = Number(form.elements.provider_id.value) || null;
      const payload = {
        name: form.elements.name.value.trim(), provider_type: form.elements.provider_type.value, base_url: form.elements.base_url.value.trim(),
        protocol_preference: form.elements.protocol_preference.value, extra_headers_json: parseObjectJSON(form.elements.extra_headers_json.value, "Extra headers"),
        azure_mode: form.elements.azure_mode.value, azure_api_version: form.elements.azure_api_version.value.trim() || null, enabled: form.elements.enabled.checked,
      };
      if (form.elements.api_key.value) payload.api_key = form.elements.api_key.value;
      await withBusy(submit, () => jsonApi(providerId ? `/api/admin/providers/${providerId}` : "/api/admin/providers", { method: providerId ? "PUT" : "POST", body: JSON.stringify(payload) }), "保存中…");
      $("#provider-dialog").close(); await loadModels(); toast(providerId ? "Provider 已更新" : "Provider 已添加");
    } catch (caught) { error.textContent = caught.message; error.hidden = false; }
  };
  const deleteProvider = async (providerId) => {
    const provider = state.providers.find((item) => item.id === providerId);
    if (!provider || !window.confirm(`确定删除 Provider“${provider.name}”及其 Model Profiles 吗？此操作不可撤销。`)) return;
    try { await api(`/api/admin/providers/${providerId}`, { method: "DELETE" }); await loadModels(); toast("Provider 已删除"); }
    catch (error) { toast(error.message, "error"); }
  };
  const openProfile = (providerId = null, profileId = null) => {
    const dialog = $("#profile-dialog"); const form = $("#profile-form"); const error = $('[data-form-error]', form);
    form.reset(); error.hidden = true; populateProfileProviderOptions(); form.elements.enabled.checked = true;
    form.elements.context_window.value = "32768"; form.elements.max_output_tokens.value = "4096";
    const profile = state.profiles.find((item) => item.id === profileId); form.elements.profile_id.value = profile ? String(profile.id) : "";
    $("#profile-dialog-title").textContent = profile ? "编辑 Model Profile" : "添加 Model Profile";
    if (profile) {
      form.elements.provider_id.value = String(profile.provider_id); form.elements.name.value = profile.name; form.elements.remote_model_name.value = profile.remote_model_name;
      form.elements.context_window.value = String(profile.context_window || 32768); form.elements.max_output_tokens.value = String(profile.max_output_tokens || 4096);
      form.elements.enabled.checked = profile.enabled;
    } else if (providerId) form.elements.provider_id.value = String(providerId);
    dialog.showModal();
  };
  const submitProfile = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const error = $('[data-form-error]', form); const submit = $('button[type="submit"]', form); error.hidden = true;
    try {
      const profileId = Number(form.elements.profile_id.value) || null;
      const payload = {
        provider_id: Number(form.elements.provider_id.value), name: form.elements.name.value.trim(), remote_model_name: form.elements.remote_model_name.value.trim(),
        context_window: Number(form.elements.context_window.value), max_output_tokens: Number(form.elements.max_output_tokens.value),
        enabled: form.elements.enabled.checked,
      };
      await withBusy(submit, () => jsonApi(profileId ? `/api/admin/model-profiles/${profileId}` : "/api/admin/model-profiles", { method: profileId ? "PUT" : "POST", body: JSON.stringify(payload) }), "保存中…");
      $("#profile-dialog").close(); await loadModels(); toast(profileId ? "Model Profile 已更新；如配置变化请重新测试" : "Model Profile 已添加，请先测试模型");
    } catch (caught) { error.textContent = caught.message; error.hidden = false; }
  };
  const deleteProfile = async (profileId) => {
    const profile = state.profiles.find((item) => item.id === profileId);
    if (!profile || !window.confirm(`确定删除 Model Profile“${profile.name}”吗？此操作不可撤销。`)) return;
    try { await api(`/api/admin/model-profiles/${profileId}`, { method: "DELETE" }); await loadModels(); toast("Model Profile 已删除"); }
    catch (error) { toast(error.message, "error"); }
  };
  const testProfile = async (profileId, control) => withBusy(control, async () => {
    const result = await jsonApi(`/api/admin/model-profiles/${profileId}/test`, { method: "POST", body: "{}" }); await loadModels();
    const summary = [`Text ${result.supports_text ? "✓" : "×"}`, `Vision ${result.supports_vision ? "✓" : "×"}`, `Structured Output ${result.supports_structured_output ? "✓" : "回退"}`].join(" · ");
    toast(`模型测试${statusLabel(result.status)}，${result.latency_ms} ms；${summary}`, result.status === "failed" ? "error" : "success");
  }, "测试中…").catch((error) => toast(error.message, "error"));
  const bindRole = async (roleId) => {
    const card = $(`[data-role-id="${roleId}"]`, $("#roles-grid"));
    const modelSelect = $('select[data-role-config="model"]', card);
    const effortSelect = $('select[data-role-config="reasoning"]', card);
    const profileId = modelSelect.value;
    modelSelect.disabled = true; effortSelect.disabled = true;
    try {
      await jsonApi(`/api/admin/model-roles/${roleId}`, {
        method: "PUT",
        body: JSON.stringify({
          model_profile_id: profileId ? Number(profileId) : null,
          reasoning_effort: effortSelect.value,
        }),
      });
      await loadModels(); toast(`${ROLE_DEFINITIONS.find((role) => role.id === roleId)?.name || roleId}已保存`);
    } catch (error) { await loadModels().catch(() => {}); toast(error.message, "error"); }
    finally {
      if (card?.isConnected) {
        modelSelect.disabled = false;
        effortSelect.disabled = !modelSelect.value;
      }
    }
  };
  const resetRolePrompts = (roleId) => {
    const card = $(`[data-role-id="${roleId}"]`, $("#roles-grid"));
    if (!card) return;
    $$('textarea[data-role-prompt]', card).forEach((textarea) => { textarea.value = textarea.dataset.defaultPrompt || ""; });
    const details = $(".role-prompt-details", card); if (details) details.open = true;
    toast("已恢复默认内容，点击“保存提示词”后生效");
  };
  const saveRolePrompts = async (roleId, control) => {
    const card = $(`[data-role-id="${roleId}"]`, $("#roles-grid"));
    if (!card) return;
    const prompts = Object.fromEntries($$('textarea[data-role-prompt]', card).map((textarea) => [textarea.dataset.rolePrompt, textarea.value.trim()]));
    if (Object.values(prompts).some((value) => !value)) { toast("提示词不能为空", "error"); return; }
    await withBusy(control, async () => {
      await jsonApi(`/api/admin/model-roles/${roleId}/prompts`, { method: "PUT", body: JSON.stringify({ prompts }) });
      await loadModels(); toast(`${ROLE_DEFINITIONS.find((role) => role.id === roleId)?.name || roleId}提示词已保存`);
    }, "保存中…").catch((error) => toast(error.message, "error"));
  };
  const loadIndex = async () => { state.index = await jsonApi("/api/admin/index"); renderIndex(); };
  const TUNING_INTEGER_FIELDS = [
    "query_router_context_tokens", "navigation_default_max_output_tokens", "answer_context_tokens", "answer_max_output_tokens",
    "navigation_root_input_token_cap", "navigation_folder_input_token_cap", "navigation_context_safety_percent",
    "navigation_max_selected_documents", "navigation_max_selected_parts", "lexical_candidate_parts",
    "lexical_fallback_parts", "lexical_max_parts_per_document", "navigation_low_confidence_percent",
    "document_text_chars_per_part", "document_excel_rows_per_part", "root_max_document_types",
    "root_max_topics", "root_max_entities", "root_max_representative_titles", "folder_summary_topics",
  ];
  const renderTuning = () => {
    const form = $("#tuning-form");
    if (!form || !state.tuning) return;
    TUNING_INTEGER_FIELDS.forEach((name) => { form.elements[name].value = String(state.tuning[name]); });
    form.elements.answer_verbosity.value = state.tuning.answer_verbosity;
    $("#tuning-updated-at").textContent = state.tuning.updated_at
      ? `自定义配置保存于 ${formatDate(state.tuning.updated_at)}`
      : "当前使用系统默认配置";
  };
  const loadTuning = async () => { state.tuning = await jsonApi("/api/admin/tuning"); renderTuning(); };
  const tuningPayload = () => {
    const form = $("#tuning-form");
    return {
      ...Object.fromEntries(TUNING_INTEGER_FIELDS.map((name) => [name, Number(form.elements[name].value)])),
      answer_verbosity: form.elements.answer_verbosity.value,
    };
  };
  const submitTuning = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const error = $("[data-tuning-error]", form);
    const submit = $('button[type="submit"]', form);
    error.hidden = true;
    try {
      state.tuning = await withBusy(
        submit,
        () => jsonApi("/api/admin/tuning", { method: "PUT", body: JSON.stringify(tuningPayload()) }),
        "保存中…",
      );
      renderTuning();
      toast("问答调优配置已保存；检索参数立即生效，分块与 root 参数需重建相应产物");
    } catch (caught) { error.textContent = caught.message; error.hidden = false; }
  };
  const resetTuning = async (control) => {
    if (!window.confirm("恢复系统默认调优配置吗？")) return;
    await withBusy(control, async () => {
      state.tuning = await jsonApi("/api/admin/tuning", { method: "DELETE" });
      renderTuning();
      toast("已恢复系统默认调优配置");
    }, "恢复中…").catch((error) => toast(error.message, "error"));
  };
  const reconvertAll = async (control) => {
    if (!window.confirm("按当前分块配置重新转换全部文件吗？这会产生模型调用，完成后还需重新生成索引。")) return;
    await withBusy(control, async () => {
      const job = await jsonApi("/api/admin/jobs/reconvert-all", { method: "POST", body: "{}" });
      await jobStarted(job, job.total_items ? `已提交全部文件重转任务 #${job.id}` : "没有可重转的文件");
    }, "提交中…").catch((error) => toast(error.message, "error"));
  };
  const renderIndex = () => {
    const values = [state.index?.current_generation ?? "—", state.index?.document_count ?? 0, state.index?.folder_count ?? 0, state.index?.last_generated ? formatDate(state.index.last_generated) : "尚未生成"];
    $$(".metric-card strong", $("#index-summary")).forEach((element, index) => { element.textContent = String(values[index]); });
    $("#rebuild-index-button").textContent = state.index?.current_generation ? "差量更新索引" : "生成索引";
  };
  const setPreviewTab = (name) => {
    $$('[data-preview-tab]').forEach((tab) => {
      const active = tab.dataset.previewTab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    $("#preview-readable-panel").hidden = name !== "readable";
    $("#preview-source-panel").hidden = name !== "source";
  };
  const readablePreviewText = (value) => String(value || "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/Administrator preview only\.?(?: The sibling JSON file is canonical\.| root\.json is canonical\.)?/gi, "管理员预览内容。")
    .replace(/(\d+)\s+document\(s\)/gi, "$1 个文件")
    .replace(/Topics\s*:/gi, "主题：")
    .replace(/:\s*(\d+\s*个文件)/g, "：$1")
    .replace(/\.\s*主题：\s*/g, "。主题：")
    .replace(/主题：\s+/g, "主题：")
    .replace(/,\s*/g, "、")
    .replace(/\.(?=\s|$)/g, "。")
    .replace(/\s+([，。；：])/g, "$1")
    .trim();
  const previewHeadingText = (value) => ({
    "Knowledge Index": "知识库索引",
  }[String(value || "").trim()] || readablePreviewText(value));
  const previewTableHeading = (value) => ({
    Folder: "文件夹标识",
    "Source directory": "文件夹",
    Summary: "内容概要",
    Documents: "文件数",
    "JSON index": "数据文件",
    Document: "文件",
    Type: "类型",
    Source: "源文件",
  }[String(value || "").trim()] || readablePreviewText(value));
  const markdownTableCells = (line) => String(line || "").trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
  const isMarkdownDivider = (line) => markdownTableCells(line).every((cell) => /^:?-{3,}:?$/.test(cell));
  const renderReadableMarkdown = (container, markdown) => {
    container.replaceChildren();
    const lines = String(markdown || "").replace(/<!--[^]*?-->/g, "").split(/\r?\n/);
    let index = 0;
    while (index < lines.length) {
      const raw = lines[index];
      const line = raw.trim();
      if (!line || /^-{3,}$/.test(line)) { index += 1; continue; }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = Math.min(4, heading[1].length + 2);
        container.append(node(`h${level}`, "preview-readable-heading", previewHeadingText(heading[2])));
        index += 1;
        continue;
      }
      if (line.startsWith(">")) {
        container.append(node("p", "preview-readable-note", readablePreviewText(line.replace(/^>\s?/, ""))));
        index += 1;
        continue;
      }
      if (line.includes("|") && index + 1 < lines.length && isMarkdownDivider(lines[index + 1])) {
        const headers = markdownTableCells(line).map(previewTableHeading);
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].trim().includes("|")) {
          rows.push(markdownTableCells(lines[index]).map(readablePreviewText));
          index += 1;
        }
        const wrap = node("div", "preview-readable-table-wrap");
        const table = node("table", "preview-readable-table");
        const head = node("thead");
        const headRow = node("tr");
        headers.forEach((header) => headRow.append(node("th", "", header)));
        head.append(headRow); table.append(head);
        const body = node("tbody");
        rows.forEach((row) => {
          const tableRow = node("tr");
          row.forEach((cell) => tableRow.append(node("td", "", cell)));
          body.append(tableRow);
        });
        table.append(body); wrap.append(table); container.append(wrap);
        continue;
      }
      if (/^[-*+]\s+/.test(line)) {
        const list = node("ul", "preview-readable-list");
        while (index < lines.length && /^[-*+]\s+/.test(lines[index].trim())) {
          list.append(node("li", "", readablePreviewText(lines[index].trim().replace(/^[-*+]\s+/, ""))));
          index += 1;
        }
        container.append(list);
        continue;
      }
      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !/^(#{1,6})\s+/.test(lines[index].trim()) && !lines[index].trim().startsWith(">") && !lines[index].trim().includes("|")) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      container.append(node("p", "preview-readable-paragraph", readablePreviewText(paragraph.join(" "))));
    }
    if (!container.childNodes.length) container.append(node("p", "preview-readable-empty", "暂无可预览内容。"));
  };
  const renderReadableIndexJson = (container, content) => {
    container.replaceChildren();
    let parsed;
    try { parsed = JSON.parse(content); }
    catch (error) {
      container.append(node("p", "preview-readable-empty", "内容暂时无法转换为可读预览，请查看“源文件内容”。"));
      return;
    }
    const folders = Array.isArray(parsed?.folders) ? parsed.folders : [];
    const fileCount = folders.reduce((total, folder) => total + (Number(folder.document_count) || 0), 0);
    container.append(node("p", "preview-readable-summary", `${folders.length} 个文件夹 · 共 ${fileCount} 个文件`));
    const grid = node("div", "preview-index-grid");
    folders.forEach((folder) => {
      const card = node("article", "preview-index-card");
      const heading = node("div", "preview-index-card-heading");
      heading.append(node("h3", "", folder.source_directory || "知识库根目录"));
      heading.append(node("span", "preview-index-count", `${Number(folder.document_count) || 0} 个文件`));
      card.append(heading);
      card.append(node("p", "preview-index-summary", readablePreviewText(folder.summary) || "暂无内容概要。"));
      grid.append(card);
    });
    if (!folders.length) grid.append(node("p", "preview-readable-empty", "当前索引中没有文件夹。"));
    container.append(grid);
  };
  const showPreview = (title, kicker, content, format = "markdown") => {
    $("#preview-title").textContent = title;
    $("#preview-kicker").textContent = kicker;
    $("#preview-content").textContent = content;
    if (format === "json") renderReadableIndexJson($("#preview-readable-content"), content);
    else renderReadableMarkdown($("#preview-readable-content"), content);
    setPreviewTab("readable");
    $("#preview-dialog").showModal();
  };
  const previewRoot = async (format, control) => withBusy(control, async () => {
    const preview = await jsonApi(`/api/admin/index/root.${format}`);
    const content = format === "json" ? JSON.stringify(JSON.parse(preview.content), null, 2) : preview.content;
    showPreview(preview.filename, format === "json" ? "检索索引" : "可读索引", content, format === "json" ? "json" : "markdown");
  }, "读取中…").catch((error) => toast(error.message, "error"));
  const rebuildIndex = async (control) => withBusy(control, async () => {
    const job = await jsonApi("/api/admin/jobs/generate-index", { method: "POST", body: "{}" }); await jobStarted(job, state.index?.current_generation ? "索引差量更新任务已提交" : "首次索引生成任务已提交");
  }, "提交中…").catch((error) => toast(error.message, "error"));

  const bootstrap = async () => {
    const results = await Promise.allSettled([loadFiles(), loadJobs(), loadModels(), loadIndex(), loadTuning()]);
    state.bootstrapped = true;
    if (results[0].status === "fulfilled" && results[1].status === "fulfilled") renderJobs();
    const firstFailure = results.find((result) => result.status === "rejected");
    if (firstFailure && !appView.hidden) toast(firstFailure.reason.message, "error");
  };

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault(); loginError.hidden = true; const submit = $('button[type="submit"]', loginForm); setBusy(submit, true, "登录中…");
    try {
      await jsonApi("/api/auth/admin/login", { method: "POST", body: JSON.stringify({ password: passwordInput.value }) });
      passwordInput.value = ""; showApp();
    } catch (error) { loginError.textContent = error.message; loginError.hidden = false; }
    finally { setBusy(submit, false); }
  });
  $("#logout-button").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST", body: "{}" }).catch(() => {}); state.bootstrapped = false; showLogin();
  });
  $$('[data-tab]').forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));
  $$('[data-preview-tab]').forEach((tab) => tab.addEventListener("click", () => setPreviewTab(tab.dataset.previewTab)));
  $$('[data-close-dialog]').forEach((control) => control.addEventListener("click", () => control.closest("dialog").close()));
  $$("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); }));
  $("#file-progress-dialog").addEventListener("close", () => {
    state.progressJobId = null; state.progressFileId = null; state.progressDetail = null;
  });
  $("#upload-form").addEventListener("submit", submitUpload);
  $$('#upload-form button[data-upload-picker]').forEach((control) => control.addEventListener("click", () => {
    const inputName = control.dataset.uploadPicker === "folder" ? "folder_files" : "file";
    $("#upload-form").elements[inputName].click();
  }));
  $$('#upload-form input[type="file"]').forEach((input) => input.addEventListener("change", () => handleUploadSelection(input)));
  $("#provider-form").addEventListener("submit", submitProvider);
  $("#profile-form").addEventListener("submit", submitProfile);
  $("#tuning-form").addEventListener("submit", submitTuning);
  $('#provider-form select[name="provider_type"]').addEventListener("change", updateAzureFields);
  replaceInput.addEventListener("change", replaceFile);
  $("#files-select-all").addEventListener("change", (event) => {
    const checked = event.currentTarget.checked;
    state.files.filter((file) => state.currentFileIds.has(file.id) && isFileBulkSelectable(file)).forEach((file) => {
      if (checked) state.selectedFileIds.add(file.id);
      else state.selectedFileIds.delete(file.id);
    });
    $$('#files-body input[data-file-select]').forEach((selection) => { selection.checked = checked; });
    updateFileSelection();
  });
  $("#files-body").addEventListener("change", (event) => {
    const selection = event.target.closest("input[data-file-select]");
    if (!selection) return;
    const fileId = Number(selection.dataset.fileSelect);
    if (selection.checked) state.selectedFileIds.add(fileId);
    else state.selectedFileIds.delete(fileId);
    updateFileSelection();
  });
  $("#roles-grid").addEventListener("change", (event) => {
    const select = event.target.closest("select[data-role-config]"); if (select) bindRole(select.dataset.role);
  });
  document.addEventListener("click", (event) => {
    const control = event.target.closest("[data-action]"); if (!control || control.tagName === "A") return;
    const fileId = Number(control.closest("[data-file-id]")?.dataset.fileId);
    const providerId = Number(control.closest("[data-provider-id]")?.dataset.providerId || control.dataset.providerId);
    const profileId = Number(control.closest("[data-profile-id]")?.dataset.profileId);
    const actions = {
      "open-upload": () => openUpload(), scan: () => scanFiles(control).catch((error) => toast(error.message, "error")),
      "convert-changed": () => convertChanged(control).catch((error) => toast(error.message, "error")), "refresh-jobs": () => loadJobs().catch((error) => toast(error.message, "error")),
      "show-job-items": () => openJobItems(Number(control.dataset.jobId), control.dataset.itemStatus, control),
      "show-current-file-progress": () => openFileProgress(Number(control.dataset.jobId), Number(control.dataset.fileId), control),
      "show-index-progress": () => openFileProgress(Number(control.dataset.jobId), 0, control),
      "retry-job-item": () => retryJobItem(Number(control.dataset.fileId), control), "retry-job-items": () => retryJobItems(control),
      "pause-job": () => updateJobControl(Number(control.dataset.jobId), "pause", control, "任务已请求暂停"),
      "resume-job": () => updateJobControl(Number(control.dataset.jobId), "resume", control, "任务已继续"),
      "stop-job": () => updateJobControl(Number(control.dataset.jobId), "stop", control, "任务已请求停止"),
      "restart-job": () => updateJobControl(Number(control.dataset.jobId), "restart", control, "任务已重启"),
      "delete-job": () => deleteJob(Number(control.dataset.jobId), control),
      "delete-all-jobs": () => deleteAllJobs(control),
      "batch-convert-files": () => batchConvertFiles(control), "batch-delete-files": () => batchDeleteFiles(control),
      "open-file-folder": () => openFileFolder(control.dataset.folderPath || ""),
      "convert-file-folder": () => convertFileFolder(control.dataset.folderPath || "", control), "delete-file-folder": () => deleteFileFolder(control.dataset.folderPath || "", control),
      "replace-file": () => chooseReplacement(fileId), "delete-file": () => deleteFile(fileId), "convert-file": () => convertFile(fileId, control), "preview-markdown": () => previewMarkdown(fileId, control),
      "open-provider": () => openProvider(), "edit-provider": () => openProvider(providerId), "delete-provider": () => deleteProvider(providerId), "add-profile": () => openProfile(providerId),
      "edit-profile": () => openProfile(null, profileId), "delete-profile": () => deleteProfile(profileId), "test-profile": () => testProfile(profileId, control),
      "reset-role-prompts": () => resetRolePrompts(control.dataset.role), "save-role-prompts": () => saveRolePrompts(control.dataset.role, control),
      "preview-root-json": () => previewRoot("json", control), "preview-root-md": () => previewRoot("md", control), "rebuild-index": () => rebuildIndex(control),
      "reconvert-all": () => reconvertAll(control),
      "reset-tuning": () => resetTuning(control),
    };
    actions[control.dataset.action]?.();
  });

  const initialTab = location.hash.slice(1);
  if (initialTab === "index") selectTab("files");
  else if (["files", "jobs", "models", "tuning"].includes(initialTab)) selectTab(initialTab);
  jsonApi("/api/auth/me").then((session) => session.role === "admin" ? showApp() : showLogin()).catch(showLogin);
})();
