(() => {
  "use strict";

  const DATABASE_NAME = "mini-kb-agent-local-chat";
  const DATABASE_VERSION = 1;
  const SESSION_STORE = "sessions";
  const DEFAULT_TITLE = "新会话";
  const state = {
    sessionId: null,
    session: null,
    busy: false,
    sidebarOpen: false,
    initialized: false,
    databasePromise: null,
    sourceReferences: new Map(),
    missingSourceReferences: new Set(),
    sourceLibraryFiles: [],
    sourceLibraryFolder: "",
    sourceLibraryQuery: "",
    sourceLibraryLoading: false,
  };
  const loginViewEl = document.querySelector("#login-view");
  const loginFormEl = document.querySelector("#login-form");
  const passwordEl = document.querySelector("#chat-password");
  const loginErrorEl = document.querySelector("#login-error");
  const chatAppEl = document.querySelector("#chat-app");
  const sessionsEl = document.querySelector("#sessions");
  const titleEl = document.querySelector("#session-title");
  const messagesEl = document.querySelector("#messages");
  const formEl = document.querySelector("#composer");
  const questionEl = document.querySelector("#question");
  const sendEl = document.querySelector("#send");
  const newEl = document.querySelector("#new-session");
  const logoutEl = document.querySelector("#logout-button");
  const openSessionsEl = document.querySelector("#open-sessions");
  const closeSessionsEl = document.querySelector("#close-sessions");
  const sidebarBackdropEl = document.querySelector("#sidebar-backdrop");
  const sourceLibraryButtonEl = document.querySelector("#source-library-button");
  const sourceLibraryDialogEl = document.querySelector("#source-library-dialog");
  const sourceLibraryCloseEl = document.querySelector("#source-library-close");
  const sourceLibraryUpEl = document.querySelector("#source-library-up");
  const sourceLibraryBreadcrumbsEl = document.querySelector("#source-library-breadcrumbs");
  const sourceLibrarySearchEl = document.querySelector("#source-library-search");
  const sourceLibrarySummaryEl = document.querySelector("#source-library-summary");
  const sourceLibraryListEl = document.querySelector("#source-library-list");
  const mobileSidebarMedia = window.matchMedia("(max-width: 760px)");

  const requestResult = (request) => new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result), { once: true });
    request.addEventListener("error", () => reject(request.error), { once: true });
  });

  const openDatabase = () => {
    if (state.databasePromise) return state.databasePromise;
    state.databasePromise = new Promise((resolve, reject) => {
      const request = window.indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.addEventListener("upgradeneeded", () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(SESSION_STORE)) {
          const store = database.createObjectStore(SESSION_STORE, { keyPath: "id" });
          store.createIndex("updatedAt", "updatedAt");
        }
      });
      request.addEventListener("success", () => resolve(request.result), { once: true });
      request.addEventListener("error", () => reject(request.error), { once: true });
      request.addEventListener("blocked", () => reject(new Error("无法打开本地聊天记录")), { once: true });
    });
    return state.databasePromise;
  };

  const sessionStore = async (mode = "readonly") => {
    const database = await openDatabase();
    return database.transaction(SESSION_STORE, mode).objectStore(SESSION_STORE);
  };

  const getAllSessions = async () => {
    const store = await sessionStore();
    const sessions = await requestResult(store.getAll());
    return sessions.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  };

  const getSession = async (sessionId) => {
    const store = await sessionStore();
    return requestResult(store.get(sessionId));
  };

  const putSession = async (session) => {
    const store = await sessionStore("readwrite");
    await requestResult(store.put(session));
  };

  const removeSession = async (sessionId) => {
    const store = await sessionStore("readwrite");
    await requestResult(store.delete(sessionId));
  };

  const makeId = () => window.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;

  const sessionIdFromLocation = () => {
    const match = window.location.pathname.match(/^\/chat\/([^/]+)\/?$/);
    if (!match) return null;
    try {
      return decodeURIComponent(match[1]);
    } catch (_error) {
      return null;
    }
  };

  const setSessionLocation = (sessionId, { replace = false } = {}) => {
    const url = sessionId ? `/chat/${encodeURIComponent(sessionId)}` : "/chat";
    window.history[replace ? "replaceState" : "pushState"](null, "", url);
  };

  const setSidebarOpen = (open) => {
    const shouldOpen = Boolean(open && mobileSidebarMedia.matches);
    state.sidebarOpen = shouldOpen;
    chatAppEl.classList.toggle("sidebar-open", shouldOpen);
    document.body.classList.toggle("sidebar-open-mobile", shouldOpen);
    openSessionsEl.setAttribute("aria-expanded", String(shouldOpen));
    sidebarBackdropEl.setAttribute("aria-hidden", String(!shouldOpen));
    sidebarBackdropEl.tabIndex = shouldOpen ? 0 : -1;
    if (shouldOpen) {
      window.setTimeout(() => closeSessionsEl.focus(), 0);
    }
  };

  const closeSidebarAndRestoreFocus = () => {
    setSidebarOpen(false);
    if (mobileSidebarMedia.matches) {
      window.setTimeout(() => openSessionsEl.focus(), 0);
    }
  };

  const showLogin = () => {
    setSidebarOpen(false);
    if (sourceLibraryDialogEl.open) sourceLibraryDialogEl.close();
    chatAppEl.hidden = true;
    loginViewEl.hidden = false;
    passwordEl.focus();
  };

  const showApp = async () => {
    loginViewEl.hidden = true;
    chatAppEl.hidden = false;
    if (!state.initialized) await initializeChat();
    questionEl.focus();
  };

  const tracePresentation = {
    request_received: {
      progress: 8,
      title: "正在启动处理",
      subtitle: "问题已接收，正在准备检索",
    },
    navigation_started: {
      progress: 14,
      title: "正在分析问题",
      subtitle: "正在识别问题类型和资料范围",
      ongoing: true,
    },
    navigation_waiting: {
      progress: 18,
      title: "正在分析问题",
      subtitle: "正在识别问题类型和资料范围",
      ongoing: true,
    },
    intent_detected: {
      progress: 24,
      title: "正在查找资料",
      subtitle: "已理解问题类型",
    },
    folders_selected: {
      progress: 54,
      title: "正在匹配文件",
      subtitle: "已定位需要查找的资料范围",
    },
    documents_selected: {
      progress: 66,
      title: "正在读取证据",
      subtitle: "已找到可能相关的文件",
    },
    answer_generating: {
      progress: 88,
      title: "正在组织回答",
      subtitle: "正在把证据整理成可核对的答案",
      ongoing: true,
    },
    answer_waiting: {
      progress: 89,
      title: "回答模型正在推理",
      subtitle: "连接保持正常，仍在等待模型返回",
      ongoing: true,
    },
    answer_reasoning_summary: {
      progress: 90,
      title: "正在整理答案",
      subtitle: "正在核对证据并组织回答",
      ongoing: true,
    },
    answer_output_progress: {
      progress: 92,
      title: "模型正在生成回答",
      subtitle: "正在完成结构化输出与证据校验",
      ongoing: true,
    },
    conflict_detected: {
      progress: 93,
      title: "正在标注来源差异",
      subtitle: "发现不同资料中存在冲突信息",
    },
    download_ready: {
      progress: 96,
      title: "正在完成回答",
      subtitle: "已准备好相关知识文件",
    },
  };

  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401 && url !== "/api/auth/chat/login") showLogin();
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    return response;
  };

  const formatFileSize = (value) => {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / (1024 ** 2)).toFixed(bytes < 10 * (1024 ** 2) ? 1 : 0)} MB`;
    return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  };

  const normalizeLibraryFolder = (value) => String(value || "")
    .replaceAll("\\", "/")
    .split("/")
    .filter(Boolean)
    .join("/");

  const libraryParentFolder = (folder) => {
    const parts = normalizeLibraryFolder(folder).split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
  };

  const setLibraryFolder = (folder) => {
    state.sourceLibraryFolder = normalizeLibraryFolder(folder);
    state.sourceLibraryQuery = "";
    sourceLibrarySearchEl.value = "";
    renderSourceLibrary();
  };

  const renderLibraryBreadcrumbs = () => {
    sourceLibraryBreadcrumbsEl.replaceChildren();
    const folders = state.sourceLibraryFolder.split("/").filter(Boolean);
    const appendCrumb = (label, folder, current) => {
      if (sourceLibraryBreadcrumbsEl.childNodes.length) {
        addText(sourceLibraryBreadcrumbsEl, "span", "source-library-breadcrumb-separator", "/");
      }
      if (current) {
        const crumb = addText(sourceLibraryBreadcrumbsEl, "span", "source-library-breadcrumb current", label);
        crumb.setAttribute("aria-current", "page");
        return;
      }
      const crumb = addText(sourceLibraryBreadcrumbsEl, "button", "source-library-breadcrumb", label);
      crumb.type = "button";
      crumb.addEventListener("click", () => setLibraryFolder(folder));
    };
    appendCrumb("知识库", "", folders.length === 0);
    folders.forEach((folder, index) => {
      appendCrumb(folder, folders.slice(0, index + 1).join("/"), index === folders.length - 1);
    });
  };

  const renderLibraryFolder = (folderName, stats) => {
    const folder = document.createElement("button");
    folder.className = "source-library-row source-library-folder";
    folder.type = "button";
    const icon = addText(folder, "span", "source-library-icon folder", "");
    icon.setAttribute("aria-hidden", "true");
    const meta = document.createElement("span");
    meta.className = "source-library-meta";
    addText(meta, "strong", "", folderName);
    addText(meta, "span", "", `${stats.subfolderCount} 个子文件夹 · 共 ${stats.fileCount} 个文件`);
    folder.append(meta);
    folder.addEventListener("click", () => {
      setLibraryFolder([state.sourceLibraryFolder, folderName].filter(Boolean).join("/"));
    });
    sourceLibraryListEl.append(folder);
  };

  const renderLibraryFile = (file) => {
    const row = document.createElement("div");
    row.className = "source-library-row source-library-file";
    const extension = String(file.extension || "").replace(/^\./, "").slice(0, 5).toUpperCase();
    const icon = addText(row, "span", "source-library-icon file", extension || "文件");
    icon.setAttribute("aria-hidden", "true");
    const meta = document.createElement("span");
    meta.className = "source-library-meta";
    const title = document.createElement("span");
    title.className = "source-library-file-title";
    addText(title, "strong", "", file.filename);
    if (file.available && file.download_url) {
      const download = addText(title, "a", "source-library-download-icon", "↓");
      download.href = file.download_url;
      download.setAttribute("download", "");
      download.setAttribute("aria-label", `下载知识文件：${file.filename}`);
      download.title = `下载知识文件：${file.filename}`;
    }
    meta.append(title);
    addText(meta, "span", "", formatFileSize(file.size));
    if (!file.available || !file.download_url) {
      addText(meta, "span", "source-library-unavailable", "文件暂不可下载");
    }
    row.append(meta);
    sourceLibraryListEl.append(row);
  };

  function renderSourceLibrary() {
    renderLibraryBreadcrumbs();
    sourceLibraryUpEl.disabled = !state.sourceLibraryFolder;
    sourceLibraryListEl.replaceChildren();
    const query = state.sourceLibraryQuery.trim().toLocaleLowerCase("zh-CN");
    if (query) {
      const matches = state.sourceLibraryFiles.filter((file) => (
        String(file.relative_path || "").toLocaleLowerCase("zh-CN").includes(query)
      ));
      sourceLibrarySummaryEl.textContent = `找到 ${matches.length} 个文件`;
      matches.forEach(renderLibraryFile);
      if (!matches.length) {
        addText(sourceLibraryListEl, "p", "source-library-empty", "没有匹配的知识文件");
      }
      return;
    }

    const prefix = state.sourceLibraryFolder ? `${state.sourceLibraryFolder}/` : "";
    const folders = new Set();
    const files = [];
    state.sourceLibraryFiles.forEach((file) => {
      if (!String(file.relative_path || "").startsWith(prefix)) return;
      const remainder = file.relative_path.slice(prefix.length);
      const separator = remainder.indexOf("/");
      if (separator >= 0) folders.add(remainder.slice(0, separator));
      else files.push(file);
    });
    const sortedFolders = [...folders].sort((left, right) => left.localeCompare(right, "zh-CN"));
    files.sort((left, right) => left.filename.localeCompare(right.filename, "zh-CN"));
    sourceLibrarySummaryEl.textContent = `${sortedFolders.length} 个文件夹 · ${files.length} 个文件`;
    sortedFolders.forEach((folderName) => {
      const folderPrefix = `${prefix}${folderName}/`;
      const descendantFiles = state.sourceLibraryFiles.filter((file) => (
        String(file.relative_path || "").startsWith(folderPrefix)
      ));
      const immediateSubfolders = new Set();
      descendantFiles.forEach((file) => {
        const remainder = String(file.relative_path || "").slice(folderPrefix.length);
        const separator = remainder.indexOf("/");
        if (separator >= 0) immediateSubfolders.add(remainder.slice(0, separator));
      });
      renderLibraryFolder(folderName, {
        subfolderCount: immediateSubfolders.size,
        fileCount: descendantFiles.length,
      });
    });
    files.forEach(renderLibraryFile);
    if (!sortedFolders.length && !files.length) {
      addText(sourceLibraryListEl, "p", "source-library-empty", "此文件夹中没有知识库文件");
    }
  }

  const loadSourceLibrary = async () => {
    if (state.sourceLibraryLoading) return;
    state.sourceLibraryLoading = true;
    sourceLibrarySummaryEl.textContent = "正在读取知识库…";
    sourceLibraryListEl.replaceChildren();
    addText(sourceLibraryListEl, "p", "source-library-empty", "正在加载…");
    try {
      const response = await api("/api/files");
      const files = await response.json();
      state.sourceLibraryFiles = files.filter((file) => file.index_status === "INDEXED");
      renderSourceLibrary();
    } catch (error) {
      sourceLibrarySummaryEl.textContent = "读取失败";
      sourceLibraryListEl.replaceChildren();
      addText(sourceLibraryListEl, "p", "source-library-empty error", error.message);
    } finally {
      state.sourceLibraryLoading = false;
    }
  };

  const openSourceLibrary = async () => {
    state.sourceLibraryFolder = "";
    state.sourceLibraryQuery = "";
    sourceLibrarySearchEl.value = "";
    if (!sourceLibraryDialogEl.open) sourceLibraryDialogEl.showModal();
    await loadSourceLibrary();
    sourceLibrarySearchEl.focus();
  };

  const clearMessages = () => {
    messagesEl.replaceChildren();
  };

  const addText = (parent, tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    parent.append(node);
    return node;
  };

  const copyToClipboard = async (text) => {
    const value = String(text || "");
    if (!value) return false;
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch (_error) {
        // Fall through to the selection-based compatibility path.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.className = "clipboard-fallback";
    document.body.append(textarea);
    textarea.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_error) {
      copied = false;
    }
    textarea.remove();
    return copied;
  };

  const appendInlineMarkdown = (parent, source) => {
    const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\[[^\]\n]+\]\([^\n)]+\)|\*[^*\n]+\*|_[^_\n]+_)/g;
    let cursor = 0;
    for (const match of source.matchAll(tokenPattern)) {
      if (match.index > cursor) {
        parent.append(document.createTextNode(source.slice(cursor, match.index)));
      }
      const token = match[0];
      if (token.startsWith("`")) {
        addText(parent, "code", "", token.slice(1, -1));
      } else if (token.startsWith("**") || token.startsWith("__")) {
        const strong = document.createElement("strong");
        appendInlineMarkdown(strong, token.slice(2, -2));
        parent.append(strong);
      } else if (token.startsWith("~~")) {
        const deleted = document.createElement("del");
        appendInlineMarkdown(deleted, token.slice(2, -2));
        parent.append(deleted);
      } else if (token.startsWith("[")) {
        const linkMatch = token.match(/^\[([^\]]+)\]\((\S+?)(?:\s+["']([^"']*)["'])?\)$/);
        if (!linkMatch) {
          parent.append(document.createTextNode(token));
        } else {
          const link = document.createElement("a");
          appendInlineMarkdown(link, linkMatch[1]);
          try {
            const target = new URL(linkMatch[2], window.location.origin);
            if (!["http:", "https:"].includes(target.protocol)) throw new Error("unsafe link");
            link.href = target.href;
            if (target.origin !== window.location.origin) {
              link.target = "_blank";
              link.rel = "noopener noreferrer";
            }
            if (
              target.origin === window.location.origin
              && /^\/api\/files\/\d+\/download$/.test(target.pathname)
            ) {
              link.setAttribute("download", "");
              link.title = linkMatch[3] || "下载知识文件";
            }
            if (linkMatch[3]) link.title = linkMatch[3];
            parent.append(link);
          } catch (_error) {
            parent.append(document.createTextNode(linkMatch[1]));
          }
        }
      } else {
        const emphasis = document.createElement("em");
        appendInlineMarkdown(emphasis, token.slice(1, -1));
        parent.append(emphasis);
      }
      cursor = match.index + token.length;
    }
    if (cursor < source.length) {
      parent.append(document.createTextNode(source.slice(cursor)));
    }
  };

  const tableCells = (line) => line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());

  const isTableDivider = (line) => {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  };

  const renderMarkdown = (container, markdown) => {
    container.replaceChildren();
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*```([\w+-]*)\s*$/);
      if (fence) {
        const codeLines = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          codeLines.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const pre = document.createElement("pre");
        const code = addText(pre, "code", fence[1] ? `language-${fence[1]}` : "", codeLines.join("\n"));
        code.setAttribute("translate", "no");
        container.append(pre);
        continue;
      }

      const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
      if (heading) {
        const node = document.createElement(`h${heading[1].length}`);
        appendInlineMarkdown(node, heading[2].trim());
        container.append(node);
        index += 1;
        continue;
      }

      if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        const headers = tableCells(line);
        const table = document.createElement("table");
        const headRow = document.createElement("tr");
        headers.forEach((cell) => {
          const th = document.createElement("th");
          appendInlineMarkdown(th, cell);
          headRow.append(th);
        });
        const thead = document.createElement("thead");
        thead.append(headRow);
        table.append(thead);
        index += 2;
        const tbody = document.createElement("tbody");
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          const row = document.createElement("tr");
          tableCells(lines[index]).forEach((cell) => {
            const td = document.createElement("td");
            appendInlineMarkdown(td, cell);
            row.append(td);
          });
          tbody.append(row);
          index += 1;
        }
        table.append(tbody);
        const wrapper = document.createElement("div");
        wrapper.className = "markdown-table-wrapper";
        wrapper.append(table);
        container.append(wrapper);
        continue;
      }

      const listMatch = line.match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
      if (listMatch) {
        const ordered = /\d+\./.test(listMatch[1]);
        const list = document.createElement(ordered ? "ol" : "ul");
        if (ordered) {
          const start = Number.parseInt(listMatch[1], 10);
          if (Number.isInteger(start) && start > 0) list.start = start;
        }
        while (index < lines.length) {
          const itemMatch = lines[index].match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
          if (!itemMatch || /\d+\./.test(itemMatch[1]) !== ordered) break;
          const item = document.createElement("li");
          appendInlineMarkdown(item, itemMatch[2].trim());
          list.append(item);
          index += 1;
        }
        container.append(list);
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quoteLines = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        const quote = document.createElement("blockquote");
        renderMarkdown(quote, quoteLines.join("\n"));
        container.append(quote);
        continue;
      }

      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        container.append(document.createElement("hr"));
        index += 1;
        continue;
      }

      const paragraphLines = [line.trim()];
      index += 1;
      while (
        index < lines.length
        && lines[index].trim()
        && !/^\s*(?:```|#{1,6}\s|>|[-+*]\s+|\d+\.\s+)/.test(lines[index])
        && !(lines[index].includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1]))
      ) {
        paragraphLines.push(lines[index].trim());
        index += 1;
      }
      const paragraph = document.createElement("p");
      paragraphLines.forEach((paragraphLine, lineIndex) => {
        if (lineIndex) paragraph.append(document.createElement("br"));
        appendInlineMarkdown(paragraph, paragraphLine);
      });
      container.append(paragraph);
    }
  };

  const formatDuration = (elapsedMilliseconds) => {
    const seconds = Math.max(0, Math.floor(elapsedMilliseconds / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  };

  const updateTraceClock = (trace) => {
    if (!trace.startedAt) return;
    trace.elapsed.textContent = `已用时 ${formatDuration(Date.now() - trace.startedAt)}`;
    const phaseElapsed = trace.phaseStartedAt
      ? Date.now() - trace.phaseStartedAt
      : 0;
    if (["navigation_started", "navigation_waiting"].includes(trace.currentType) && phaseElapsed >= 20000) {
      trace.subtitle.textContent = "检索范围较大，仍在筛选相关资料…";
    }
    if ([
      "answer_generating",
      "answer_waiting",
      "answer_reasoning_summary",
      "answer_output_progress",
    ].includes(trace.currentType) && phaseElapsed >= 30000) {
      trace.subtitle.textContent = "回答模型响应较慢，仍在等待结果…";
    }
  };

  const startTrace = (trace) => {
    trace.startedAt = Date.now();
    trace.details.open = true;
    trace.details.classList.add("is-active");
    trace.details.setAttribute("aria-busy", "true");
    updateTraceClock(trace);
    trace.timerId = window.setInterval(() => updateTraceClock(trace), 1000);
  };

  const stopTraceClock = (trace) => {
    if (trace.timerId !== null) {
      window.clearInterval(trace.timerId);
      trace.timerId = null;
    }
  };

  const setTraceProgress = (trace, progress) => {
    const bounded = Math.max(0, Math.min(100, progress));
    trace.progressFill.style.width = `${bounded}%`;
    trace.progressBar.setAttribute("aria-valuenow", String(bounded));
  };

  const finishTrace = (trace, status) => {
    stopTraceClock(trace);
    trace.details.classList.remove("is-active", "is-complete", "is-error");
    trace.details.classList.add(status === "error" ? "is-error" : "is-complete");
    trace.details.setAttribute("aria-busy", "false");
    trace.list.querySelectorAll(".is-current").forEach((item) => {
      item.classList.remove("is-current");
      item.classList.add("is-complete");
    });

    if (status === "error") {
      trace.title.textContent = "处理未完成";
      trace.subtitle.textContent = "可展开查看停留的步骤和错误信息";
      trace.elapsed.textContent = trace.startedAt
        ? `用时 ${formatDuration(Date.now() - trace.startedAt)}`
        : "已停止";
      return;
    }

    setTraceProgress(trace, 100);
    trace.title.textContent = "处理完成";
    trace.subtitle.textContent = `已完成 ${trace.list.children.length} 个主要步骤`;
    trace.elapsed.textContent = trace.startedAt
      ? `用时 ${formatDuration(Date.now() - trace.startedAt)}`
      : "已完成";
  };

  const markTraceInterrupted = (trace, traceEvents) => {
    const lastEvent = traceEvents.at(-1);
    if (!lastEvent || ["completed", "error"].includes(lastEvent.event_type)) return;
    stopTraceClock(trace);
    trace.details.classList.remove("is-active");
    trace.details.classList.add("is-interrupted");
    trace.details.setAttribute("aria-busy", "false");
    trace.list.querySelectorAll(".is-current").forEach((item) => {
      item.classList.remove("is-current");
      item.classList.add("is-complete");
    });
    trace.title.textContent = "上次处理已中断";
    trace.subtitle.textContent = `中断前停留在：${lastEvent.data?.message || lastEvent.event_type}`;
    trace.elapsed.textContent = "可重新提问";
  };

  const createTrace = (events = []) => {
    const details = document.createElement("details");
    details.className = "trace";

    const summary = document.createElement("summary");
    summary.className = "trace-summary";
    const statusIcon = document.createElement("span");
    statusIcon.className = "trace-status-icon";
    statusIcon.setAttribute("aria-hidden", "true");
    const summaryCopy = document.createElement("span");
    summaryCopy.className = "trace-summary-copy";
    const title = addText(summaryCopy, "strong", "trace-title", "处理过程");
    const subtitle = addText(summaryCopy, "span", "trace-subtitle", "等待开始检索资料");
    const elapsed = addText(summary, "span", "trace-elapsed", "");
    summary.prepend(statusIcon, summaryCopy);
    details.append(summary);

    const progressBar = document.createElement("div");
    progressBar.className = "trace-progress";
    progressBar.setAttribute("role", "progressbar");
    progressBar.setAttribute("aria-label", "回答处理进度");
    progressBar.setAttribute("aria-valuemin", "0");
    progressBar.setAttribute("aria-valuemax", "100");
    progressBar.setAttribute("aria-valuenow", "4");
    const progressFill = document.createElement("span");
    progressFill.className = "trace-progress-fill";
    progressBar.append(progressFill);
    details.append(progressBar);

    const list = document.createElement("ol");
    list.className = "trace-list";
    details.append(list);

    const trace = {
      details, summary, statusIcon, title, subtitle, elapsed,
      progressBar, progressFill, list, startedAt: null, timerId: null,
      currentType: null, phaseStartedAt: null,
    };
    events.forEach((event) => appendTrace(trace, event.event_type, event.data, true));
    return trace;
  };

  const appendTrace = (trace, type, data = {}, historical = false) => {
    const displayMessage = data.message || type;
    // Older locally stored conversations can contain these implementation-level
    // events. Keep their history readable using the same concise projection.
    if (["root_index_loaded", "folder_index_loaded", "document_loading"].includes(type)) {
      return null;
    }
    if (["navigation_waiting", "intent_detected"].includes(type)) {
      const navigationItem = trace.list.querySelector('[data-event-type="navigation_started"]');
      if (navigationItem) {
        navigationItem.querySelector(".trace-item-text").textContent = displayMessage;
        const presentation = tracePresentation[type];
        navigationItem.classList.toggle("is-current", type === "navigation_waiting" && !historical);
        navigationItem.classList.toggle("is-complete", type === "intent_detected" || historical);
        if (type === "intent_detected") navigationItem.dataset.eventType = type;
        if (presentation) {
          trace.currentType = type;
          if (!historical) trace.phaseStartedAt = Date.now();
          trace.title.textContent = presentation.title;
          trace.subtitle.textContent = data.message || presentation.subtitle;
          setTraceProgress(trace, presentation.progress);
        }
        return navigationItem;
      }
    }
    if (["answer_waiting", "answer_output_progress"].includes(type)) {
      const answerItem = trace.list.querySelector('[data-event-type="answer_generating"]');
      if (answerItem) {
        answerItem.querySelector(".trace-item-text").textContent = type === "answer_output_progress"
          ? "正在生成回答"
          : "正在根据相关资料整理回答";
        const presentation = tracePresentation[type];
        answerItem.classList.toggle("is-current", !historical);
        answerItem.classList.toggle("is-complete", historical);
        if (presentation) {
          trace.currentType = type;
          if (!historical) trace.phaseStartedAt = Date.now();
          trace.title.textContent = presentation.title;
          trace.subtitle.textContent = data.message || presentation.subtitle;
          setTraceProgress(trace, presentation.progress);
        }
        return answerItem;
      }
    }
    if (type === "answer_reasoning_summary") {
      trace.list.querySelectorAll(".is-current").forEach((item) => {
        item.classList.remove("is-current");
        item.classList.add("is-complete");
      });
      const presentation = tracePresentation[type];
      trace.currentType = type;
      if (!historical) trace.phaseStartedAt = Date.now();
      trace.title.textContent = presentation.title;
      trace.subtitle.textContent = presentation.subtitle;
      setTraceProgress(trace, presentation.progress);
      return null;
    }
    const coalescedTypes = new Set([
      "navigation_waiting",
      "answer_waiting",
      "answer_output_progress",
    ]);
    const existing = coalescedTypes.has(type)
      ? trace.list.querySelector(`[data-event-type="${type}"]`)
      : null;

    if (existing) {
      trace.list.querySelectorAll(".is-current").forEach((item) => {
        if (item === existing) return;
        item.classList.remove("is-current");
        item.classList.add("is-complete");
      });
      existing.querySelector(".trace-item-text").textContent = displayMessage;
      const presentation = tracePresentation[type];
      if (presentation) {
        existing.classList.toggle("is-current", Boolean(presentation.ongoing && !historical));
        existing.classList.toggle("is-complete", Boolean(!presentation.ongoing || historical));
        trace.currentType = type;
        if (!historical) trace.phaseStartedAt = Date.now();
        trace.title.textContent = presentation.title;
        trace.subtitle.textContent = data.message || presentation.subtitle;
        setTraceProgress(trace, presentation.progress);
      }
      return existing;
    }

    trace.list.querySelectorAll(".is-current").forEach((item) => {
      item.classList.remove("is-current");
      item.classList.add("is-complete");
    });

    const item = document.createElement("li");
    item.className = "trace-item";
    item.dataset.eventType = type;
    const icon = document.createElement("span");
    icon.className = "trace-item-icon";
    icon.setAttribute("aria-hidden", "true");
    if (type === "documents_selected" && Array.isArray(data.documents) && data.documents.length) {
      const details = document.createElement("details");
      details.className = "trace-item-details";
      addText(details, "summary", "trace-item-text", displayMessage);
      const sources = document.createElement("ul");
      sources.className = "trace-source-list";
      data.documents.forEach((documentData) => {
        const sourcePath = documentData?.source_path || documentData?.title;
        if (sourcePath) addText(sources, "li", "", sourcePath);
      });
      details.append(sources);
      item.append(details);
    } else {
      addText(item, "span", "trace-item-text", displayMessage);
    }
    item.prepend(icon);

    const presentation = tracePresentation[type];
    if (presentation?.ongoing) item.classList.add("is-current");
    else item.classList.add("is-complete");
    if (type === "conflict_detected") item.classList.add("warning");
    if (type === "error") item.classList.add("error");
    trace.list.append(item);

    if (presentation) {
      trace.currentType = type;
      if (!historical) trace.phaseStartedAt = Date.now();
      trace.title.textContent = presentation.title;
      trace.subtitle.textContent = data.message || presentation.subtitle;
      setTraceProgress(trace, presentation.progress);
    }

    if (type === "completed") finishTrace(trace, "complete");
    if (type === "error") finishTrace(trace, "error");

    if (!historical && typeof item.animate === "function") {
      item.animate(
        [
          { opacity: 0, transform: "translateY(5px)" },
          { opacity: 1, transform: "translateY(0)" },
        ],
        { duration: 260, easing: "ease-out" },
      );
    }
    return item;
  };

  const createAnswerPlaceholder = () => {
    const placeholder = document.createElement("div");
    placeholder.className = "answer-placeholder";
    placeholder.setAttribute("aria-label", "正在准备回答");
    const heading = document.createElement("div");
    heading.className = "answer-placeholder-heading";
    const spark = document.createElement("span");
    spark.className = "thinking-spark";
    spark.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    const title = addText(copy, "strong", "answer-placeholder-title", "正在准备回答");
    const subtitle = addText(copy, "span", "answer-placeholder-subtitle", "检索到证据后，答案会出现在这里");
    heading.append(spark, copy);
    placeholder.append(heading);
    const reasoning = document.createElement("div");
    reasoning.className = "model-reasoning-summary";
    reasoning.hidden = true;
    addText(reasoning, "span", "model-reasoning-label", "思考进度");
    const reasoningText = addText(reasoning, "div", "model-reasoning-text markdown-body", "");
    placeholder.append(reasoning);
    ["82%", "96%", "68%"].forEach((width) => {
      const line = document.createElement("span");
      line.className = "skeleton-line";
      line.style.width = width;
      placeholder.append(line);
    });
    return { root: placeholder, title, subtitle, reasoning, reasoningText };
  };

  const updateAnswerPlaceholder = (placeholder, type, data) => {
    if (!placeholder) return;
    if (type === "answer_generating") {
      placeholder.title.textContent = "正在组织回答";
      placeholder.subtitle.textContent = data.message || "正在根据读取到的证据组织答案";
    }
    if (type === "answer_waiting") {
      placeholder.title.textContent = "回答模型正在推理";
      placeholder.subtitle.textContent = data.message || "连接保持正常，仍在等待模型返回";
    }
    if (type === "answer_reasoning_summary" && data.summary) {
      placeholder.title.textContent = "正在整理答案";
      placeholder.subtitle.textContent = "模型正在核对证据并组织回答";
      renderMarkdown(placeholder.reasoningText, data.summary);
      placeholder.reasoning.hidden = false;
    }
    if (type === "answer_output_progress") {
      placeholder.title.textContent = "模型正在生成回答";
      placeholder.subtitle.textContent = data.message || "正在完成结构化输出与证据校验";
    }
  };

  const renderUserMessage = (content) => {
    messagesEl.querySelector(".empty-state")?.remove();
    const article = document.createElement("article");
    article.className = "message user";
    addText(article, "div", "bubble", content);
    messagesEl.append(article);
  };

  const renderAssistantMessage = async (message, traceEvents = []) => {
    const article = document.createElement("article");
    article.className = "message assistant";
    const trace = createTrace(traceEvents);
    article.append(trace.details);
    const answer = message.answer || {
      answer_markdown: message.content,
      citations: [], conflicts: [], downloads: [],
    };
    await hydrateAnswerSources(answer);
    renderAnswer(article, answer);
    messagesEl.append(article);
  };

  const renderTraceOnly = (traceEvents) => {
    const article = document.createElement("article");
    article.className = "message assistant";
    const trace = createTrace(traceEvents);
    markTraceInterrupted(trace, traceEvents);
    trace.details.open = true;
    article.append(trace.details);
    messagesEl.append(article);
  };

  const renderAnswerExtras = (parent, answer) => {
    const handoff = answer.research_handoff;
    if (handoff?.prompt) {
      const section = document.createElement("section");
      section.className = "answer-section research-handoff-section";
      addText(section, "h3", "", "建议补充外部查询");
      const card = document.createElement("div");
      card.className = "research-handoff-card";
      addText(card, "p", "research-handoff-reason", handoff.reason);

      const addInformationList = (title, values) => {
        if (!Array.isArray(values) || !values.length) return;
        addText(card, "h4", "research-handoff-label", title);
        const list = document.createElement("ul");
        list.className = "research-handoff-list";
        values.forEach((value) => addText(list, "li", "", value));
        card.append(list);
      };
      addInformationList("知识库已有信息", handoff.known_information);
      addInformationList("仍需查询的参数", handoff.missing_information);

      const promptHeading = document.createElement("div");
      promptHeading.className = "research-prompt-heading";
      addText(promptHeading, "h4", "research-handoff-label", "可复制提示词");
      const copyButton = addText(promptHeading, "button", "research-copy-button", "复制提示词");
      copyButton.type = "button";
      copyButton.addEventListener("click", async () => {
        copyButton.disabled = true;
        const copied = await copyToClipboard(handoff.prompt);
        copyButton.textContent = copied ? "已复制" : "复制失败，请手动选择";
        window.setTimeout(() => {
          copyButton.disabled = false;
          copyButton.textContent = "复制提示词";
        }, 1800);
      });
      card.append(promptHeading);
      const promptBlock = document.createElement("pre");
      addText(promptBlock, "code", "", handoff.prompt).setAttribute("translate", "no");
      card.append(promptBlock);
      addText(
        card,
        "p",
        "research-privacy-note",
        "发送到第三方大模型前，请检查并删除不适合外发的项目、客户或其他敏感信息。",
      );
      section.append(card);
      parent.append(section);
    }

    if (answer.conflicts?.length) {
      const section = document.createElement("section");
      section.className = "answer-section";
      addText(section, "h3", "", "冲突警告");
      answer.conflicts.forEach((conflict) => {
        const card = document.createElement("div");
        card.className = "conflict-card";
        addText(card, "strong", "", conflict.subject);
        conflict.values.forEach((value) => {
          const source = sourceForDocument(answer, value.document_id);
          const location = humanSourceLocation(value.anchor);
          const row = document.createElement("p");
          row.append(document.createTextNode(`${value.value} · `));
          if (source?.download_url && source?.filename) {
            const sourceLink = addText(row, "a", "conflict-source-link", source.filename);
            sourceLink.href = source.download_url;
            sourceLink.setAttribute("download", "");
            sourceLink.title = `下载知识文件：${source.filename}`;
          } else {
            row.append(document.createTextNode(source?.filename || "知识文件"));
          }
          row.append(document.createTextNode(` · ${location}`));
          card.append(row);
        });
        addText(card, "p", "", conflict.analysis);
        section.append(card);
      });
      parent.append(section);
    }

    if (answer.citations?.length) {
      const section = document.createElement("section");
      section.className = "answer-section sources-section";
      addText(section, "h3", "", "参考来源");
      groupedSources(answer.citations).forEach((source) => {
        const card = document.createElement("div");
        card.className = "source-card";
        const meta = document.createElement("span");
        meta.className = "source-meta";
        addText(meta, "strong", "", source.filename);
        if (source.locations.length) {
          addText(meta, "span", "", `原文位置：${source.locations.join("、")}`);
        }
        if (source.directory) addText(meta, "span", "", `文件目录：${source.directory}`);
        card.append(meta);
        if (source.downloadUrl) {
          const download = document.createElement("a");
          download.className = "source-action";
          download.href = source.downloadUrl;
          download.setAttribute("download", "");
          download.setAttribute("aria-label", `下载知识文件：${source.filename}`);
          download.title = `下载知识文件：${source.filename}`;
          addText(download, "span", "source-action-label", "下载知识文件");
          const icon = addText(download, "span", "source-action-icon", "↓");
          icon.setAttribute("aria-hidden", "true");
          card.append(download);
        } else {
          addText(card, "span", "source-action unavailable", "文件暂不可下载");
        }
        section.append(card);
      });
      parent.append(section);
    }

    const citedDocumentIds = new Set(
      (answer.citations || []).map((citation) => String(citation.document_id)),
    );
    const uncitedDownloads = (answer.downloads || []).filter(
      (download) => !citedDocumentIds.has(String(download.document_id)),
    );
    if (uncitedDownloads.length) {
      const section = document.createElement("section");
      section.className = "answer-section";
      addText(section, "h3", "", "文件下载");
      uncitedDownloads.forEach((download) => {
        const card = document.createElement("div");
        card.className = "download-card";
        const meta = document.createElement("div");
        meta.className = "download-meta";
        addText(meta, "strong", "", download.filename);
        addText(meta, "span", "", download.relative_directory || "知识库根目录");
        if (download.display_path) addText(meta, "span", "", download.display_path);
        const link = addText(card, "a", "download-link", "下载");
        link.href = download.download_url;
        link.setAttribute("download", "");
        card.prepend(meta);
        section.append(card);
      });
      parent.append(section);
    }
  };

  const renderAnswer = (parent, answer) => {
    const answerBody = addText(parent, "div", "answer-body markdown-body", "");
    renderMarkdown(answerBody, answerMarkdownWithSourceLinks(answer));
    renderAnswerExtras(parent, answer);
  };

  const renderAnswerProgressively = async (parent, answer) => {
    const markdown = answerMarkdownWithSourceLinks(answer);
    const characters = Array.from(markdown);
    const answerBody = addText(parent, "div", "answer-body markdown-body is-streaming", "");
    const hydration = hydrateAnswerSources(answer).catch(() => undefined);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || characters.length < 80) {
      renderMarkdown(answerBody, markdown);
      await hydration;
      renderMarkdown(answerBody, answerMarkdownWithSourceLinks(answer));
      answerBody.classList.remove("is-streaming");
      renderAnswerExtras(parent, answer);
      scrollToBottom();
      return;
    }

    let followAnswer = true;
    const stopFollowing = () => { followAnswer = false; };
    messagesEl.addEventListener("wheel", stopFollowing, { passive: true });
    messagesEl.addEventListener("touchmove", stopFollowing, { passive: true });
    const duration = Math.min(5200, Math.max(1200, characters.length * 3.2));
    let lastVisible = 0;
    await new Promise((resolve) => {
      let startedAt;
      const frame = (now) => {
        if (startedAt === undefined) startedAt = now;
        const progress = Math.min(1, (now - startedAt) / duration);
        const visible = Math.max(1, Math.ceil(characters.length * progress));
        if (visible !== lastVisible) {
          renderMarkdown(answerBody, characters.slice(0, visible).join(""));
          lastVisible = visible;
          if (followAnswer) scrollToBottom();
        }
        if (progress < 1) window.requestAnimationFrame(frame);
        else resolve();
      };
      window.requestAnimationFrame(frame);
    });
    messagesEl.removeEventListener("wheel", stopFollowing);
    messagesEl.removeEventListener("touchmove", stopFollowing);
    await hydration;
    renderMarkdown(answerBody, answerMarkdownWithSourceLinks(answer));
    answerBody.classList.remove("is-streaming");
    renderAnswerExtras(parent, answer);
    if (followAnswer) scrollToBottom();
  };

  const parseAnchor = (anchor) => {
    if (anchor && typeof anchor === "object" && !Array.isArray(anchor)) return anchor;
    if (typeof anchor !== "string") return {};
    try {
      const parsed = JSON.parse(anchor);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (_error) {
      return {};
    }
  };

  const humanSourceLocation = (anchor) => {
    const value = parseAnchor(anchor);
    const locations = [];
    if (Number.isInteger(value.page)) locations.push(`第 ${value.page} 页`);
    if (Number.isInteger(value.slide)) locations.push(`第 ${value.slide} 张幻灯片`);
    if (typeof value.sheet === "string" && value.sheet.trim()) {
      locations.push(`工作表“${value.sheet.trim()}”`);
    }
    if (typeof value.rows === "string" && value.rows.trim() && value.rows !== "empty") {
      locations.push(`第 ${value.rows.trim()} 行`);
    }
    if (typeof value.section === "string" && value.section.trim()) {
      const section = value.section.trim();
      const numbered = section.match(/^section-(\d+)$/i);
      if (numbered) locations.push(`第 ${numbered[1]} 部分`);
      else if (section === "document") locations.push("全文");
      else if (section === "image") locations.push("图片内容");
      else locations.push(`章节“${section}”`);
    }
    if (typeof value.segment === "string" && /^\d+\/\d+$/.test(value.segment.trim())) {
      locations.push(`片段 ${value.segment.trim()}`);
    }
    return locations.join("·") || "原文相关位置";
  };

  const anchorAliases = (anchor) => {
    const value = parseAnchor(anchor);
    const aliases = [];
    if (typeof value.section === "string") aliases.push(value.section);
    if (Number.isInteger(value.page)) {
      aliases.push(`Page ${value.page}`, `page ${value.page}`, `第${value.page}页`);
    }
    if (Number.isInteger(value.slide)) {
      aliases.push(`Slide ${value.slide}`, `slide ${value.slide}`, `第${value.slide}页`);
    }
    return aliases;
  };

  const markdownLinkLabel = (value) => String(value)
    .replaceAll("\\", "\\\\")
    .replaceAll("[", "\\[")
    .replaceAll("]", "\\]");

  const answerMarkdownWithSourceLinks = (answer) => {
    const replacements = new Map();
    (answer.citations || []).forEach((citation) => {
      const source = sourceForDocument(answer, citation.document_id);
      const downloadUrl = citation.download_url || source?.download_url;
      const filename = citation.source_filename || source?.filename;
      if (!downloadUrl || !filename) return;
      const location = citation.source_location || humanSourceLocation(citation.anchor);
      const label = `${filename}${location ? `（${location}）` : ""}`;
      [citation.label, citation.part_id, ...anchorAliases(citation.anchor)].forEach((alias) => {
        if (typeof alias === "string" && alias.trim() && !replacements.has(alias.trim())) {
          replacements.set(alias.trim(), `[${markdownLinkLabel(label)}](${downloadUrl})`);
        }
      });
    });
    if (!replacements.size) return answer.answer_markdown || "";
    let inFence = false;
    return String(answer.answer_markdown || "").split("\n").map((line) => {
      if (/^\s*```/.test(line)) {
        inFence = !inFence;
        return line;
      }
      if (inFence) return line;
      return line.replace(/(?<!!)\[([^\]\n]+)\](?!\s*\()/g, (marker, label) => (
        replacements.get(label.trim()) || marker
      ));
    }).join("\n");
  };

  const sourceForDocument = (answer, documentId) => {
    const normalizedId = String(documentId);
    const citation = (answer.citations || []).find(
      (item) => String(item.document_id) === normalizedId && item.source_filename,
    );
    if (citation) {
      return {
        filename: citation.source_filename,
        relative_path: citation.source_path,
        display_path: citation.display_path,
        download_url: citation.download_url,
      };
    }
    const download = (answer.downloads || []).find(
      (item) => String(item.document_id) === normalizedId,
    );
    if (download) return download;
    return state.sourceReferences.get(normalizedId) || null;
  };

  const groupedSources = (citations) => {
    const grouped = new Map();
    citations.forEach((citation) => {
      const documentId = String(citation.document_id);
      const cached = state.sourceReferences.get(documentId);
      const filename = citation.source_filename || cached?.filename || citation.label || "知识文件";
      const relativePath = citation.source_path || cached?.relative_path || "";
      const directory = relativePath.includes("/")
        ? relativePath.slice(0, relativePath.lastIndexOf("/"))
        : "";
      if (!grouped.has(documentId)) {
        grouped.set(documentId, {
          documentId,
          filename,
          directory,
          downloadUrl: citation.download_url || cached?.download_url || null,
          locations: [],
        });
      }
      const location = citation.source_location || humanSourceLocation(citation.anchor);
      const source = grouped.get(documentId);
      if (location && !source.locations.includes(location)) source.locations.push(location);
    });
    return [...grouped.values()];
  };

  const hydrateAnswerSources = async (answer) => {
    const downloads = new Map(
      (answer.downloads || []).map((download) => [String(download.document_id), download]),
    );
    const ids = new Set();
    (answer.citations || []).forEach((citation) => ids.add(String(citation.document_id)));
    (answer.conflicts || []).forEach((conflict) => {
      conflict.values?.forEach((value) => ids.add(String(value.document_id)));
    });
    downloads.forEach((_download, id) => ids.add(id));

    downloads.forEach((download, id) => {
      if (!state.sourceReferences.has(id)) state.sourceReferences.set(id, download);
    });
    (answer.citations || []).forEach((citation) => {
      if (citation.source_filename) {
        state.sourceReferences.set(String(citation.document_id), {
          filename: citation.source_filename,
          relative_path: citation.source_path,
          display_path: citation.display_path,
          download_url: citation.download_url,
        });
      }
    });

    const unresolved = [...ids].filter((id) => (
      /^\d+$/.test(id)
      && !state.sourceReferences.has(id)
      && !state.missingSourceReferences.has(id)
    ));
    if (unresolved.length) {
      try {
        const response = await api("/api/files/references", {
          method: "POST",
          body: JSON.stringify({ document_ids: unresolved.map(Number) }),
        });
        const references = await response.json();
        const returnedIds = new Set();
        references.forEach((reference) => {
          const id = String(reference.document_id);
          state.sourceReferences.set(id, reference);
          returnedIds.add(id);
        });
        unresolved.forEach((id) => {
          if (!returnedIds.has(id)) state.missingSourceReferences.add(id);
        });
      } catch (_error) {
        // Keep the historical answer readable even if metadata refresh fails.
      }
    }

    (answer.citations || []).forEach((citation) => {
      const reference = state.sourceReferences.get(String(citation.document_id));
      if (!reference) return;
      citation.source_filename ||= reference.filename;
      citation.source_path ||= reference.relative_path;
      citation.display_path ||= reference.display_path;
      citation.download_url ||= reference.download_url;
      citation.source_location ||= humanSourceLocation(citation.anchor);
    });
  };

  const loadSessions = async () => {
    const sessions = await getAllSessions();
    sessionsEl.replaceChildren();
    sessions.forEach((session) => {
      const row = document.createElement("div");
      row.className = "session-row";
      const button = addText(row, "button", "session-button", session.title);
      button.type = "button";
      button.dataset.sessionId = String(session.id);
      if (session.id === state.sessionId) button.classList.add("active");
      button.addEventListener("click", () => openSession(session.id));
      const remove = addText(row, "button", "session-delete-button", "×");
      remove.type = "button";
      remove.title = `删除会话：${session.title}`;
      remove.setAttribute("aria-label", `删除会话：${session.title}`);
      remove.addEventListener("click", () => deleteSession(session.id));
      sessionsEl.append(row);
    });
  };

  const renderSession = async (session) => {
    state.sessionId = session.id;
    state.session = session;
    titleEl.textContent = session.title;
    clearMessages();
    let pendingEvents = [];
    for (const message of session.messages) {
      if (message.role === "user") {
        if (pendingEvents.length) renderTraceOnly(pendingEvents);
        renderUserMessage(message.content);
        pendingEvents = message.events || [];
      } else if (message.role === "assistant") {
        await renderAssistantMessage(message, pendingEvents);
        pendingEvents = [];
      }
    }
    if (pendingEvents.length) renderTraceOnly(pendingEvents);
    scrollToBottom();
  };

  const showNewSession = ({ replaceLocation = false } = {}) => {
    state.sessionId = null;
    state.session = null;
    titleEl.textContent = DEFAULT_TITLE;
    clearMessages();
    const empty = document.createElement("div");
    empty.id = "empty-state";
    empty.className = "empty-state";
    addText(empty, "p", "", "输入问题，开始知识问答。");
    messagesEl.append(empty);
    if (replaceLocation) setSessionLocation(null, { replace: true });
  };

  const openSession = async (sessionId, { navigate = true } = {}) => {
    if (state.busy) return;
    const session = await getSession(sessionId);
    if (!session) {
      showNewSession({ replaceLocation: true });
      await loadSessions();
      setSidebarOpen(false);
      return;
    }
    await renderSession(session);
    if (navigate) setSessionLocation(session.id);
    await loadSessions();
    setSidebarOpen(false);
  };

  const createSession = async ({ replaceLocation = false } = {}) => {
    if (state.busy) return null;
    const now = new Date().toISOString();
    const session = {
      id: makeId(),
      title: DEFAULT_TITLE,
      messages: [],
      createdAt: now,
      updatedAt: now,
    };
    await putSession(session);
    await renderSession(session);
    setSessionLocation(session.id, { replace: replaceLocation });
    await loadSessions();
    setSidebarOpen(false);
    questionEl.focus();
    return session;
  };

  const deleteSession = async (sessionId = state.sessionId) => {
    if (!sessionId || state.busy) return;
    const session = await getSession(sessionId);
    if (!session) return;
    if (!window.confirm(`删除会话“${session.title}”？此操作只会删除本浏览器中的记录。`)) return;
    await removeSession(sessionId);
    if (sessionId === state.sessionId) showNewSession({ replaceLocation: true });
    await loadSessions();
  };

  const parseSSE = async (response, onEvent) => {
    if (!response.body) throw new Error("浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        let type = "message";
        let data = "";
        block.split("\n").forEach((line) => {
          if (line.startsWith("event:")) type = line.slice(6).trim();
          if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        if (data) await onEvent(type, JSON.parse(data));
      }
      if (done) break;
    }
  };

  const setBusy = (busy) => {
    state.busy = busy;
    sendEl.disabled = busy;
    sendEl.classList.toggle("is-busy", busy);
    sendEl.textContent = busy ? "思考中" : "发送";
    messagesEl.setAttribute("aria-busy", String(busy));
  };

  const fallbackTitle = (question) => {
    const compact = question.replace(/\s+/g, " ").trim();
    return compact.slice(0, 30) || DEFAULT_TITLE;
  };

  const titleMessages = (session) => session.messages
    .filter((message) => ["user", "assistant"].includes(message.role))
    .slice(0, 6)
    .map((message) => ({
      role: message.role,
      content: message.content.slice(0, 12000),
    }));

  const generateTitle = async (session, firstQuestion) => {
    if (session.title !== DEFAULT_TITLE) return;
    try {
      const response = await api("/api/chat/title", {
        method: "POST",
        body: JSON.stringify({ messages: titleMessages(session) }),
      });
      const payload = await response.json();
      session.title = payload.title || fallbackTitle(firstQuestion);
    } catch (_error) {
      session.title = fallbackTitle(firstQuestion);
    }
    session.updatedAt = new Date().toISOString();
    await putSession(session);
    if (state.sessionId === session.id) titleEl.textContent = session.title;
    await loadSessions();
  };

  const eventForStorage = (type, data) => {
    const storedData = { ...data };
    delete storedData.type;
    if (type === "completed") delete storedData.answer;
    return {
      event_type: type,
      data: storedData,
      created_at: new Date().toISOString(),
    };
  };

  const storeTraceEvent = (events, type, data) => {
    const event = eventForStorage(type, data);
    if ([
      "navigation_waiting",
      "answer_waiting",
      "answer_reasoning_summary",
      "answer_output_progress",
    ].includes(type)) {
      for (let index = events.length - 1; index >= 0; index -= 1) {
        if (events[index].event_type === type) {
          events[index] = event;
          return;
        }
      }
    }
    events.push(event);
  };

  const submitQuestion = async (question) => {
    let session = state.session;
    if (!session) session = await createSession({ replaceLocation: true });
    if (!session) return;
    setBusy(true);
    const userMessage = {
      id: makeId(),
      role: "user",
      content: question,
      events: [],
      created_at: new Date().toISOString(),
    };
    session.messages.push(userMessage);
    session.updatedAt = userMessage.created_at;
    await putSession(session);
    renderUserMessage(question);
    const assistant = document.createElement("article");
    assistant.className = "message assistant is-thinking";
    const trace = createTrace();
    startTrace(trace);
    assistant.append(trace.details);
    const placeholder = createAnswerPlaceholder();
    assistant.append(placeholder.root);
    messagesEl.append(assistant);
    scrollToBottom();

    try {
      const response = await api("/api/chat/stream", {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      await parseSSE(response, async (type, data) => {
        appendTrace(trace, type, data);
        updateAnswerPlaceholder(placeholder, type, data);
        storeTraceEvent(userMessage.events, type, data);
        if (type === "completed") {
          placeholder.root.remove();
          assistant.classList.remove("is-thinking");
          const completedAt = new Date().toISOString();
          session.messages.push({
            id: makeId(),
            role: "assistant",
            content: data.answer?.answer_markdown || "",
            answer: data.answer,
            created_at: completedAt,
          });
          session.updatedAt = completedAt;
          await putSession(session);
          await renderAnswerProgressively(assistant, data.answer);
          window.setTimeout(() => {
            if (trace.details.classList.contains("is-complete")) trace.details.open = false;
          }, 700);
        }
        if (type === "error") {
          placeholder.root.remove();
          assistant.classList.remove("is-thinking");
        }
        session.updatedAt = new Date().toISOString();
        await putSession(session);
        if (type !== "completed") scrollToBottom();
      });
      await loadSessions();
      if (session.messages.some((message) => message.role === "assistant")) {
        await generateTitle(session, question);
      }
    } catch (error) {
      placeholder.root.remove();
      assistant.classList.remove("is-thinking");
      appendTrace(trace, "error", { message: error.message });
      storeTraceEvent(userMessage.events, "error", { message: error.message });
      session.updatedAt = new Date().toISOString();
      await putSession(session).catch(() => {});
    } finally {
      setBusy(false);
      questionEl.focus();
    }
  };

  const scrollToBottom = () => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  };

  formEl.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = questionEl.value.trim();
    if (!question || state.busy) return;
    questionEl.value = "";
    submitQuestion(question);
  });
  questionEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      formEl.requestSubmit();
    }
  });
  newEl.addEventListener("click", () => createSession());
  openSessionsEl.addEventListener("click", () => setSidebarOpen(true));
  closeSessionsEl.addEventListener("click", closeSidebarAndRestoreFocus);
  sidebarBackdropEl.addEventListener("click", closeSidebarAndRestoreFocus);
  sourceLibraryButtonEl.addEventListener("click", openSourceLibrary);
  sourceLibraryCloseEl.addEventListener("click", () => sourceLibraryDialogEl.close());
  sourceLibraryUpEl.addEventListener("click", () => setLibraryFolder(
    libraryParentFolder(state.sourceLibraryFolder),
  ));
  sourceLibrarySearchEl.addEventListener("input", () => {
    state.sourceLibraryQuery = sourceLibrarySearchEl.value;
    renderSourceLibrary();
  });
  sourceLibraryDialogEl.addEventListener("click", (event) => {
    if (event.target === sourceLibraryDialogEl) sourceLibraryDialogEl.close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.sidebarOpen) closeSidebarAndRestoreFocus();
  });

  mobileSidebarMedia.addEventListener("change", (event) => {
    if (!event.matches) setSidebarOpen(false);
  });

  loginFormEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginErrorEl.hidden = true;
    const submit = loginFormEl.querySelector('button[type="submit"]');
    submit.disabled = true;
    submit.textContent = "登录中…";
    try {
      await api("/api/auth/chat/login", {
        method: "POST",
        body: JSON.stringify({ password: passwordEl.value }),
      });
      passwordEl.value = "";
      await showApp();
    } catch (error) {
      loginErrorEl.textContent = error.message;
      loginErrorEl.hidden = false;
      passwordEl.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = "进入聊天";
    }
  });

  logoutEl.addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    showLogin();
  });

  window.addEventListener("popstate", async () => {
    if (state.busy) return;
    const sessionId = sessionIdFromLocation();
    if (sessionId) await openSession(sessionId, { navigate: false });
    else {
      showNewSession();
      await loadSessions();
    }
  });

  async function initializeChat() {
    state.initialized = true;
    try {
      await openDatabase();
      const sessionId = sessionIdFromLocation();
      if (sessionId) await openSession(sessionId, { navigate: false });
      else {
        showNewSession();
        await loadSessions();
      }
    } catch (error) {
      state.initialized = false;
      clearMessages();
      addText(messagesEl, "div", "empty-state", `无法读取本地聊天记录：${error.message}`);
    }
  }

  api("/api/auth/me")
    .then(() => showApp())
    .catch(() => showLogin());
})();
