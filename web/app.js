"use strict";

const DEMO_CODE = `import sys
import os

def calculate_score(user_input_list):
    # Process input and return score
    try:
        total = sum(user_input_list)
        score = (total / len(user_input_list)) * 100
        return score
    except Exception as e:
        return f'Error: {e}'

if __name__ == '__main__':
    data_score = [5, 10, 15, 20, 25]
    print(f"Final score: {data_score}")`;

const state = {
  currentView: "welcome",
  bridgeReady: false,
  bridgeStatus: "connecting",
  appInfo: null,
  activeTab: null,
  openTabs: [],
  activePath: "",
  rootPath: "",
  currentPath: "",
  currentName: "",
  nodes: [],
  treeChildren: new Map(),
  expandedFolders: new Set(),
  demo: false,
  terminalIsDemo: false,
  recentItems: [],
  updateTimer: 0,
  modified: false,
  commandIndex: 0,
  pendingReturn: false,
  orbitOffset: 0,
  orbitHistory: [],
  orbitDragging: false,
  orbitPointerDown: false,
  orbitDragMoved: false,
  orbitDragStartY: 0,
  orbitDragStartOffset: 0,
  folderLoading: false,
  aiAvailable: false,
  aiBusy: false,
  aiMessages: [],
  aiOpen: false,
  fileActionBusy: false,
  creationType: "",
  pendingWorkspaceAction: "",
  applicationStatus: "Initialising",
  applicationStatusTimer: 0,
  runActive: false,
  runStartedAt: 0,
  workspaceZoom: 1,
  fileFilter: "",
  openFileFilter: "",
  terminalHeight: 0,
  terminalExpanded: false,
  findMatches: [],
  findIndex: -1,
  findCaseSensitive: false,
  findReplaceOpen: false,
  persistedWorkspace: null,
  settings: null,
  searchOpen: false,
  searchPreviousFocus: null,
  projectSearch: { files: [], flat: [], index: -1, running: false },
  problems: [],
  git: { is_repo: false, branch: "", files: {} },
  autosave: { mode: "off", delay: 1000, timer: 0, saving: false, suppressed: false },
  recoveryTimer: 0,
  fileVersions: new Map(),
  aiChangePreview: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const bridgeWaiters = new Set();

function api() {
  return window.pywebview?.api || null;
}

function setBridgeStatus(status, message = "") {
  state.bridgeStatus = status;
  state.bridgeReady = status === "connected" && Boolean(api());
  const indicator = $("#engine-indicator");
  const text = $("#engine-indicator-text");
  const statusNode = $("#status-backend");
  const readyNode = $("#status-ready");
  const workspaceEngine = $("#workspace-engine");
  const aiEngine = $("#ai-engine-pill");
  if (indicator) indicator.className = `engine-indicator ${status}`;
  if (text) {
    text.textContent = message || {
      connecting: "Connecting to NexCore Engine…",
      connected: "NexCore Engine Connected",
      error: "Backend connection failed",
    }[status];
  }
  if (statusNode) {
    statusNode.className = status;
    statusNode.innerHTML = `<i></i> ${status === "connected" ? "Engine connected" : status === "error" ? "Backend unavailable" : "Connecting"}`;
  }
  if (readyNode) readyNode.textContent = status === "connected" ? "Ready" : status === "error" ? "Offline mode" : "Initialising";
  if (workspaceEngine) {
    workspaceEngine.className = status;
    workspaceEngine.innerHTML = `<i></i> ${status === "connected" ? "Engine connected" : status === "connecting" ? "Connecting" : "Engine unavailable"}`;
  }
  if (status === "connected" && state.applicationStatus === "Initialising") setApplicationStatus("Ready");
  if (status === "error") setApplicationStatus("Engine unavailable", "error", false);
  if (aiEngine) aiEngine.innerHTML = `<i></i> ${status === "connected" ? "Engine Connected" : "Engine Unavailable"}`;
}

function setApplicationStatus(text, className = "", reset = true) {
  state.applicationStatus = text;
  const node = $("#workspace-app-status");
  if (node) {
    node.textContent = text;
    node.className = className;
  }
  window.clearTimeout(state.applicationStatusTimer);
  if (reset && !["Ready", "Running", "Initialising"].includes(text)) {
    state.applicationStatusTimer = window.setTimeout(() => {
      if (!state.runActive) setApplicationStatus("Ready", "", false);
    }, 2200);
  }
}

function updateWorkspaceStatus() {
  const project = $("#workspace-project");
  const projectText = state.rootPath
    ? `Project: ${state.currentName || basename(state.rootPath)}`
    : state.activeTab && !state.demo
      ? `File: ${state.activeTab.filename}`
      : "No project open";
  if (project) {
    project.textContent = projectText;
    project.title = state.rootPath || state.activeTab?.path || projectText;
  }
  const metadata = $("#workspace-file-metadata");
  if (metadata) {
    const content = $("#editor")?.value || "";
    const language = languageInfo(state.activeTab?.path || "")[0];
    metadata.textContent = state.activeTab
      ? `${language} · ${(state.activeTab.encoding || "UTF-8").toUpperCase()} · ${content.includes("\r\n") ? "CRLF" : "LF"} · Spaces: 4`
      : "";
    metadata.classList.toggle("hidden", !state.activeTab);
  }
  updateMenuAvailability();
}

function updateMenuAvailability() {
  const hasFile = Boolean(state.activeTab && !state.demo);
  const hasFolder = Boolean(state.rootPath && !state.demo);
  const requirements = {
    file: hasFile,
    folder: hasFolder,
    tabs: state.openTabs.length > 1,
    history: state.orbitHistory.length > 0,
    runnable: hasFile && Boolean(state.activeTab?.path?.toLowerCase().endsWith(".py")),
    running: state.runActive,
  };
  $$(".workspace-topbar [data-requires]").forEach((item) => {
    const enabled = Boolean(requirements[item.dataset.requires]);
    item.disabled = !enabled;
    item.setAttribute("aria-disabled", String(!enabled));
  });
}

function resolveBridgeWaiters(bridge) {
  bridgeWaiters.forEach((resolve) => resolve(bridge));
  bridgeWaiters.clear();
}

async function awaitBridge(timeoutMs = 12000) {
  const current = api();
  if (current) {
    if (!state.bridgeReady) setBridgeStatus("connected");
    return current;
  }
  if (state.bridgeStatus === "error") return null;
  setBridgeStatus("connecting");
  return new Promise((resolve) => {
    let settled = false;
    const finish = (bridge) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      bridgeWaiters.delete(onReady);
      resolve(bridge);
    };
    const onReady = (bridge) => finish(bridge);
    bridgeWaiters.add(onReady);
    const timer = window.setTimeout(() => {
      if (!api()) setBridgeStatus("error", "Backend connection failed");
      finish(api());
    }, timeoutMs);
  });
}

function friendlyError(error, fallback = "The NexCore Engine could not complete that action.") {
  const text = String(error || fallback);
  if (/permission|access denied/i.test(text)) return "Permission denied for the selected path.";
  if (/not exist|no such|not found/i.test(text)) return "The selected file or folder no longer exists.";
  return text.length > 180 ? fallback : text;
}

async function callBridge(method, ...args) {
  const bridge = await awaitBridge();
  if (!bridge) {
    toast("Backend unavailable", "Launch NexCore with python main.py to use desktop actions.", true);
    return null;
  }
  if (typeof bridge[method] !== "function") {
    const error = new Error(`Undefined bridge method: ${method}`);
    console.error("[NexCore Bridge]", error);
    toast("Backend action unavailable", `${method} is not exposed by the NexCore Engine.`, true);
    return null;
  }
  try {
    return await bridge[method](...args);
  } catch (error) {
    console.error("[NexCore Bridge]", error);
    toast("NexCore Engine error", friendlyError(error), true);
    return null;
  }
}

function toast(title, message, isError = false) {
  const node = $("#toast");
  if (!node) return;
  $("#toast-title").textContent = title;
  $("#toast-message").textContent = message || "";
  node.classList.toggle("error", isError);
  node.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.add("hidden"), 3200);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function basename(path = "") {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

function samePath(left = "", right = "") {
  return left.replaceAll("/", "\\").toLowerCase() === right.replaceAll("/", "\\").toLowerCase();
}

function isValidTab(tab) {
  return Boolean(tab && typeof tab.tab_id === "string" && typeof tab.filename === "string"
    && typeof tab.path === "string" && typeof tab.content === "string");
}

function isValidFolderResult(result) {
  return Boolean(result?.root && typeof result.root.path === "string" && typeof result.root.name === "string"
    && Array.isArray(result.children));
}

function sortNodes(nodes) {
  return [...(nodes || [])].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
  });
}

function fileKind(path = "") {
  const extension = path.split(".").pop().toLowerCase();
  if (extension === "py") return "python";
  if (["js", "jsx", "ts", "tsx"].includes(extension)) return "web";
  if (extension === "html") return "html";
  if (extension === "css") return "css";
  if (extension === "json") return "json";
  return "file";
}

function languageInfo(path = "") {
  const extension = path.split(".").pop().toLowerCase();
  return {
    py: ["Python", "Python"], js: ["JavaScript", "JavaScript"], jsx: ["JavaScript React", "JavaScript"],
    ts: ["TypeScript", "TypeScript"], tsx: ["TypeScript React", "TypeScript"],
    html: ["HTML", "HTML"], css: ["CSS", "CSS"], json: ["JSON", "JSON"],
    md: ["Markdown", "Markdown"],
  }[extension] || ["Plain Text", "Text"];
}

function treeIcon(node) {
  if (node.is_dir) return `<svg class="tree-icon folder" viewBox="0 0 24 24"><path d="M3 6h7l2 2h9v11H3Z" fill="currentColor" opacity=".8"/></svg>`;
  const kind = fileKind(node.path || node.name);
  return `<svg class="tree-icon ${kind}" viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6Z" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M15 3v5h4" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
}

function renderExplorer() {
  const target = $("#orbital-list");
  if (!target) return;
  target.replaceChildren();
  const projectName = $("#orbital-project-name");
  if (projectName) projectName.textContent = state.currentName || (state.demo ? "NexCore Demo" : "Folders");
  setFileActionBusy(state.fileActionBusy);
  const parent = $("#orbit-parent");
  parent?.classList.toggle("hidden", state.orbitHistory.length === 0);
  if ($("#orbit-breadcrumb")) $("#orbit-breadcrumb").textContent = orbitalRelativePath();
  state.nodes = sortNodes(state.nodes);
  const query = state.fileFilter.trim().toLowerCase();
  const nodes = query ? state.nodes.filter((node) => node.name.toLowerCase().includes(query)) : state.nodes;
  state.orbitOffset = Math.max(0, Math.min(state.orbitOffset, Math.max(0, nodes.length - 1)));
  const spacing = Math.max(82, Math.min(125, window.innerHeight * .13));
  const centre = Math.max(175, target.clientHeight * .48);
  if (!nodes.length) {
    const empty = document.createElement("div");
    empty.className = "orbit-empty";
    empty.innerHTML = `<strong>${query ? "No matching files" : "This folder is empty"}</strong><div><button type="button" data-orbit-empty="back">Back to parent</button><button type="button" data-orbit-empty="root">Project root</button></div>`;
    empty.querySelector('[data-orbit-empty="back"]').addEventListener("click", leaveOrbitalFolder);
    empty.querySelector('[data-orbit-empty="root"]').addEventListener("click", reloadProjectRoot);
    target.appendChild(empty);
    return;
  }
  nodes.forEach((node, index) => {
    const t = index - state.orbitOffset;
    const distance = Math.abs(t);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `orbit-node${distance < .35 ? " selected" : ""}`;
    button.dataset.path = node.path || "";
    button.dataset.index = String(index);
    button.title = node.path || node.name;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(distance < .35));
    button.style.setProperty("--orbit-x", `${Math.min(245, 62 + 29 * t * t)}px`);
    button.style.setProperty("--orbit-y", `${centre + t * spacing - 46}px`);
    button.style.setProperty("--orbit-scale", String(Math.max(.68, 1.08 - distance * .1)));
    button.style.setProperty("--orbit-opacity", String(Math.max(.18, 1 - distance * .19)));
    const kind = node.is_dir ? "folder" : fileKind(node.path || node.name);
    const modified = !node.is_dir && state.openTabs.some((tab) => samePath(tab.path, node.path) && tab.is_modified);
    const relative = state.rootPath && node.path?.toLowerCase().startsWith(state.rootPath.toLowerCase()) ? node.path.slice(state.rootPath.length).replace(/^[\\/]+/, "") : "";
    const gitStatus = state.git.files?.[relative] || state.git.files?.[relative.replaceAll("\\", "/")] || "";
    button.innerHTML = `<span class="orbit-icon ${kind}">${orbitalIcon(node, kind)}</span><strong>${escapeHtml(node.name)}${gitStatus ? `<em class="git-indicator git-${gitStatus.toLowerCase()}">${gitStatus}</em>` : ""}${modified ? `<i class="modified-dot"></i>` : ""}</strong>`;
    button.addEventListener("click", async () => {
      if (state.orbitDragMoved) return;
      state.orbitOffset = index;
      renderExplorer();
      if (node.is_dir) await enterOrbitalFolder(node);
      else await openTreeFile(node);
    });
    target.appendChild(button);
  });
}

function orbitalIcon(node, kind) {
  if (node.is_dir) return `<svg viewBox="0 0 24 24"><path d="M3 6h7l2 2h9v11H3Z"/></svg>`;
  if (kind === "python") return `<svg viewBox="0 0 24 24"><path d="M12 3c-4 0-4 2-4 2v4h7v2H7s-3 0-3 4 3 4 3 4h3"/><path d="M12 21c4 0 4-2 4-2v-4H9v-2h8s3 0 3-4-3-4-3-4h-3"/></svg>`;
  if (kind === "html") return `<svg viewBox="0 0 24 24"><path d="m5 4 2 16 5 2 5-2 2-16Z"/><path d="M8 9h8l-1 7-3 1-3-1"/></svg>`;
  if (kind === "css") return `<svg viewBox="0 0 24 24"><path d="m5 4 2 16 5 2 5-2 2-16Z"/><path d="M8 9h8m-8 4h7"/></svg>`;
  return `<svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6Z"/><path d="M15 3v5h4"/></svg>`;
}

function orbitalRelativePath() {
  if (!state.currentPath || !state.rootPath || samePath(state.currentPath, state.rootPath)) return "Project root";
  const relative = state.currentPath.slice(state.rootPath.length).replace(/^[\\/]+/, "");
  return `Project root / ${relative.split(/[\\/]/).join(" / ")}`;
}

async function enterOrbitalFolder(node) {
  if (state.folderLoading) return false;
  if (!node?.path) {
    toast("Unable to open folder", "Folder path is missing.", true);
    return false;
  }
  state.folderLoading = true;
  $("#orbital-navigation")?.classList.add("loading");
  try {
    const result = node.isDemo
      ? { ok: true, path: node.path, name: node.name, children: node.children || [] }
      : await callBridge("list_children", node.path);
    if (!result) {
      toast("Unable to open folder", "No response from the NexCore backend.", true);
      return false;
    }
    if (!result.ok || !Array.isArray(result.children)) {
      console.error("[NexCore Folder]", result.error || "Invalid folder response");
      toast("Unable to open folder", friendlyError(result.error, "The folder could not be read."), true);
      return false;
    }
    state.orbitHistory.push({
      path: state.currentPath,
      name: state.currentName,
      nodes: state.nodes,
      offset: state.orbitOffset,
    });
    state.currentPath = result.path || node.path;
    state.nodes = sortNodes(result.children);
    state.orbitOffset = 0;
    updateFolderContext();
    renderExplorer();
    persistWorkspaceSettings();
    return true;
  } finally {
    state.folderLoading = false;
    $("#orbital-navigation")?.classList.remove("loading");
  }
}

function leaveOrbitalFolder() {
  const previous = state.orbitHistory.pop();
  if (!previous) return;
  state.currentPath = previous.path;
  state.nodes = previous.nodes;
  state.orbitOffset = previous.offset;
  renderExplorer();
  updateFolderContext();
  persistWorkspaceSettings();
}

async function reloadProjectRoot() {
  if (!state.rootPath || state.demo) {
    toast("Open a project folder first", "There is no real project root to reload.", true);
    return false;
  }
  setFileActionBusy(true);
  try {
    const result = await callBridge("list_directory", state.rootPath);
    if (!result?.ok || !isValidFolderResult(result)) {
      toast("Project unavailable", friendlyError(result?.error), true);
      return false;
    }
    state.currentPath = result.root.path;
    state.currentName = result.root.name;
    state.nodes = sortNodes(result.children);
    state.orbitHistory = [];
    const activeIndex = state.nodes.findIndex((node) => samePath(node.path, state.activePath));
    state.orbitOffset = activeIndex >= 0 ? activeIndex : 0;
    renderExplorer();
    updateFolderContext();
    persistWorkspaceSettings();
    toast("Project root refreshed", state.currentName);
    refreshGitStatus();
    return true;
  } finally {
    setFileActionBusy(false);
  }
}

function setFileActionBusy(busy) {
  state.fileActionBusy = busy;
  const hasProject = Boolean(state.rootPath && !state.demo);
  $$(".orbital-file-action").forEach((button) => {
    const requiresProject = ["new-file", "new-folder", "refresh", "root"].includes(button.dataset.fileAction);
    button.disabled = busy || (requiresProject && !hasProject);
  });
  $("#orbital-navigation")?.classList.toggle("loading", busy);
}

async function refreshCurrentFolder({ announce = true, focusPath = "" } = {}) {
  const targetPath = state.currentPath || state.rootPath;
  if (!targetPath || state.demo) {
    toast("Open a project folder first", "File management requires a real project folder.", true);
    return false;
  }
  const selectedPath = focusPath || state.nodes[Math.round(state.orbitOffset)]?.path || "";
  setApplicationStatus("Loading folder…", "", false);
  setFileActionBusy(true);
  try {
    const atRoot = samePath(targetPath, state.rootPath);
    const result = atRoot
      ? await callBridge("list_directory", state.rootPath)
      : await callBridge("list_children", targetPath);
    const children = result?.children;
    if (!result?.ok || !Array.isArray(children)) {
      setApplicationStatus("Error", "error");
      toast("Refresh failed", friendlyError(result?.error, "Unable to reload this folder."), true);
      return false;
    }
    state.nodes = sortNodes(children);
    state.currentPath = atRoot ? (result.root?.path || state.rootPath) : (result.path || targetPath);
    const selectedIndex = state.nodes.findIndex((node) => samePath(node.path, selectedPath));
    state.orbitOffset = selectedIndex >= 0
      ? selectedIndex
      : Math.max(0, Math.min(state.orbitOffset, Math.max(0, state.nodes.length - 1)));
    renderExplorer();
    updateFolderContext();
    persistWorkspaceSettings();
    if (announce) toast("Folder refreshed", orbitalRelativePath());
    refreshGitStatus();
    setApplicationStatus("Ready", "", false);
    return true;
  } finally {
    setFileActionBusy(false);
  }
}

function openCreationModal(type) {
  if (!state.rootPath || state.demo) {
    toast("Open a project folder first", "Choose a real project before creating files or folders.", true);
    return;
  }
  state.creationType = type;
  const isFile = type === "file";
  $("#creation-title").textContent = isFile ? "Create New File" : "Create New Folder";
  $("#creation-label").textContent = isFile ? "Filename" : "Folder name";
  $("#creation-name").placeholder = isFile ? "example.py" : "components";
  $("#creation-name").value = "";
  $("#creation-error").textContent = "";
  $("#creation-location").textContent = orbitalRelativePath();
  $("#creation-modal").classList.remove("hidden");
  window.setTimeout(() => $("#creation-name")?.focus(), 30);
}

function closeCreationModal() {
  state.creationType = "";
  $("#creation-modal")?.classList.add("hidden");
  $("#creation-error").textContent = "";
}

function validateCreationName(name) {
  const value = String(name || "").trim();
  if (!value) return "Name cannot be empty.";
  if (value === "." || value === ".." || /[<>:"/\\|?*]/.test(value) || /[. ]$/.test(value)) {
    return "Enter a valid name without path characters.";
  }
  return "";
}

async function submitCreation() {
  if (state.fileActionBusy) return false;
  const name = $("#creation-name")?.value.trim() || "";
  const validation = validateCreationName(name);
  if (validation) {
    $("#creation-error").textContent = validation;
    return false;
  }
  const parent = state.currentPath || state.rootPath;
  setFileActionBusy(true);
  try {
    const method = state.creationType === "file" ? "create_file" : "create_folder";
    const result = await callBridge(method, parent, name);
    if (!result?.ok) {
      $("#creation-error").textContent = friendlyError(result?.error, `Unable to create ${state.creationType}.`);
      return false;
    }
    const creationType = state.creationType;
    closeCreationModal();
    state.fileFilter = "";
    if ($("#orbital-filter-input")) $("#orbital-filter-input").value = "";
    await refreshCurrentFolder({ announce: false, focusPath: result.path });
    if (creationType === "file" && isValidTab(result.tab)) {
      loadRealTab(result.tab);
      addRecentItem(result.tab.path, "file");
      $("#editor")?.focus();
      toast("File created", name);
    } else {
      toast("Folder created", name);
    }
    return true;
  } finally {
    setFileActionBusy(false);
  }
}

function requestOpenFolder() {
  if (requestUnsavedProtection("open-folder")) return;
  openFolder();
}

function renderTreeLevel(nodes, target, depth) {
  sortNodes(nodes).forEach((node) => {
    const branch = document.createElement("div");
    branch.className = "tree-branch";
    const item = document.createElement("button");
    item.type = "button";
    item.className = "tree-item";
    item.style.setProperty("--depth", depth);
    item.dataset.path = node.path || "";
    const expanded = state.expandedFolders.has(node.path);
    item.classList.toggle("expanded", expanded);
    item.classList.toggle("active", Boolean(state.activePath && samePath(node.path, state.activePath)));
    item.innerHTML = `${node.is_dir ? `<span class="tree-chevron">›</span>` : `<span class="tree-chevron"></span>`}${treeIcon(node)}<span class="tree-label">${escapeHtml(node.name)}</span>${!node.is_dir && state.openTabs.some((tab) => samePath(tab.path, node.path) && tab.is_modified) ? `<span class="tree-modified">●</span>` : ""}`;
    item.addEventListener("click", async () => {
      if (node.is_dir) await toggleExplorerFolder(node);
      else await openTreeFile(node);
    });
    branch.appendChild(item);
    if (node.is_dir && expanded) {
      const children = document.createElement("div");
      children.className = "tree-children";
      renderTreeLevel(state.treeChildren.get(node.path) || [], children, depth + 1);
      branch.appendChild(children);
    }
    target.appendChild(branch);
  });
}

async function toggleExplorerFolder(node) {
  if (state.expandedFolders.has(node.path)) {
    state.expandedFolders.delete(node.path);
    renderExplorer();
    return;
  }
  if (!state.treeChildren.has(node.path)) {
    if (node.isDemo) {
      state.treeChildren.set(node.path, node.children || []);
    } else {
      const result = await callBridge("list_children", node.path);
      if (!result?.ok || !Array.isArray(result.children)) {
        toast("Could not expand folder", friendlyError(result?.error), true);
        return;
      }
      state.treeChildren.set(node.path, sortNodes(result.children));
    }
  }
  state.expandedFolders.add(node.path);
  renderExplorer();
}

async function openTreeFile(node) {
  if (node.isDemo) {
    const tab = node.tab || { tab_id: `demo-${node.name}`, filename: node.name, path: node.name, content: DEMO_CODE, is_modified: false, is_untitled: false, encoding: "utf-8" };
    loadRealTab(tab, true);
    return;
  }
  const result = await callBridge("open_file", node.path);
  handleOpenFileResult(result, node.path);
}

function handleOpenFileResult(result, requestedPath = "") {
  if (!result || result.cancelled) return false;
  if (result.requires_confirmation) {
    state.pendingLargeFile = result.metadata?.path || requestedPath;
    $("#file-warning-message").textContent = `${result.metadata?.name || basename(requestedPath)} is ${Math.ceil((result.metadata?.size || 0) / 1024)} KB and may be slow to display.`;
    $("#file-warning-modal")?.classList.remove("hidden");
    return false;
  }
  if (result.binary) {
    const metadata = result.metadata || {};
    toast("This file cannot be displayed as text", `${metadata.name || basename(requestedPath)} · ${metadata.size || 0} bytes · ${metadata.path || requestedPath}`, true);
    return false;
  }
  if (!result.ok || !isValidTab(result.tab)) {
    toast("Could not open file", friendlyError(result.error), true);
    return false;
  }
  loadRealTab(result.tab);
  if (!result.metadata?.large) $("#highlight-layer")?.classList.remove("hidden");
  if (result.metadata?.mtime) state.fileVersions.set(result.tab.path, result.metadata.mtime);
  addRecentItem(result.tab.path, "file");
  return true;
}

function upsertOpenTab(tab) {
  const index = state.openTabs.findIndex((item) => item.tab_id === tab.tab_id);
  if (index >= 0) state.openTabs[index] = { ...state.openTabs[index], ...tab };
  else state.openTabs.push(tab);
}

function renderTabs() {
  const target = $("#open-files-menu");
  if (!target) return;
  target.replaceChildren();
  const filter = document.createElement("input");
  filter.className = "open-files-filter";
  filter.type = "text";
  filter.placeholder = "Filter open files…";
  filter.setAttribute("aria-label", "Filter open files");
  filter.value = state.openFileFilter;
  filter.addEventListener("input", () => {
    state.openFileFilter = filter.value;
    renderTabs();
    const next = $("#open-files-menu .open-files-filter");
    next?.focus();
    next?.setSelectionRange(next.value.length, next.value.length);
  });
  filter.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      if (filter.value) {
        state.openFileFilter = "";
        renderTabs();
      } else closeOpenFilesMenu();
    }
  });
  target.appendChild(filter);
  if (!state.openTabs.length) {
    target.innerHTML = `<div class="open-file-row">No open files</div>`;
  }
  const query = state.openFileFilter.trim().toLowerCase();
  const visibleTabs = state.openTabs.filter((tab) => !query || `${tab.filename} ${tab.path}`.toLowerCase().includes(query));
  if (state.openTabs.length && !visibleTabs.length) {
    const empty = document.createElement("div");
    empty.className = "open-file-row";
    empty.textContent = "No matching open files";
    target.appendChild(empty);
  }
  visibleTabs.forEach((tab) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `open-file-row${state.activeTab?.tab_id === tab.tab_id ? " active" : ""}`;
    button.innerHTML = `<span class="mini-file-icon ${fileKind(tab.path)}"></span><span class="open-file-copy"><strong>${escapeHtml(tab.filename)}</strong><small>${escapeHtml(tab.path)}</small></span><span>${tab.is_modified ? "●" : ""}</span><span class="open-file-close" role="button" aria-label="Close ${escapeHtml(tab.filename)}">×</span>`;
    button.addEventListener("click", (event) => {
      if (event.target.closest(".open-file-close")) closeEditorTab(tab);
      else { activateEditorTab(tab); closeOpenFilesMenu(); }
    });
    button.addEventListener("auxclick", (event) => { if (event.button === 1) closeEditorTab(tab); });
    target.appendChild(button);
  });
  const activeName = $("#active-file-name");
  if (activeName) activeName.textContent = state.activeTab?.filename || "No file open";
  const activePath = $("#active-file-path");
  if (activePath) {
    const path = state.activeTab?.path || "";
    activePath.textContent = state.rootPath && path.toLowerCase().startsWith(state.rootPath.toLowerCase())
      ? path.slice(state.rootPath.length).replace(/^[\\/]+/, "")
      : path;
    activePath.title = path;
  }
  $("#active-file-modified")?.classList.toggle("active", Boolean(state.modified));
  const saveState = $("#editor-save-state");
  if (saveState) {
    saveState.textContent = state.modified ? "Modified" : state.activeTab ? "Saved" : "Ready";
    saveState.classList.toggle("modified", state.modified);
  }
  renderExplorer();
}

function closeOpenFilesMenu() {
  $("#open-files-menu")?.classList.add("hidden");
  $("#active-file-button")?.setAttribute("aria-expanded", "false");
}

async function activateEditorTab(tab) {
  if (state.activeTab?.tab_id === tab.tab_id) return;
  if (state.activeTab) {
    state.activeTab.cursor = $("#editor")?.selectionStart || 0;
    state.activeTab.scrollTop = $("#editor")?.scrollTop || 0;
    upsertOpenTab(state.activeTab);
  }
  if (state.activeTab && state.modified && !state.demo) {
    window.clearTimeout(state.updateTimer);
    await callBridge("update_file", state.activeTab.tab_id, $("#editor").value);
  }
  if (!state.demo && !String(tab.tab_id).startsWith("demo-")) {
    const result = await callBridge("activate_tab", tab.tab_id);
    if (!result?.ok || !isValidTab(result.tab)) return toast("Could not activate tab", friendlyError(result?.error), true);
    tab = result.tab;
  }
  loadRealTab(tab, String(tab.tab_id).startsWith("demo-"));
}

async function closeEditorTab(tab, force = false) {
  if (String(tab.tab_id).startsWith("demo-")) {
    state.openTabs = state.openTabs.filter((item) => item.tab_id !== tab.tab_id);
    const next = state.openTabs.at(-1) || null;
    state.activeTab = next;
    if (next) loadRealTab(next, String(next.tab_id).startsWith("demo-"));
    else clearActiveEditor();
    persistWorkspaceSettings();
    return;
  }
  if (tab.is_modified && !force) {
    requestUnsavedProtection("close-tab", tab);
    return;
  }
  if (force && tab.recovery_id) await callBridge("discard_recovery_snapshot", tab.recovery_id);
  const result = await callBridge("close_tab", tab.tab_id, force);
  if (!result?.ok) return toast("Could not close tab", friendlyError(result?.error), true);
  state.openTabs = Array.isArray(result.tabs) ? result.tabs : state.openTabs.filter((item) => item.tab_id !== tab.tab_id);
  if (result.active_tab) loadRealTab(result.active_tab);
  else clearActiveEditor();
  persistWorkspaceSettings();
}

function clearActiveEditor() {
  state.activeTab = null;
  state.activePath = "";
  state.modified = false;
  const editor = $("#editor");
  if (editor) editor.value = "";
  updateHighlighting();
  renderTabs();
  renderBreadcrumbs();
  updateEditorChrome();
}

function renderBreadcrumbs() {
  const target = $("#breadcrumbs");
  if (!target) return;
  if (!state.activeTab) {
    target.innerHTML = `<span>No file open</span>`;
    return;
  }
  const parts = state.activeTab.path.split(/[\\/]/).filter(Boolean);
  target.innerHTML = parts.map((part, index) => `${index ? `<span class="breadcrumb-chevron">›</span>` : ""}<button class="breadcrumb-segment" type="button">${escapeHtml(part)}</button>`).join("");
}

function updateLineNumbers() {
  const editor = $("#editor"), gutter = $("#line-numbers");
  if (!editor || !gutter) return;
  const count = Math.max(1, editor.value.split("\n").length);
  gutter.textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
  gutter.scrollTop = editor.scrollTop;
}

function updateCaretFeedback() {
  const editor = $("#editor"), gutter = $("#line-numbers");
  if (!editor || !gutter) return;
  const line = editor.value.slice(0, editor.selectionStart).split("\n").length;
  const computed = getComputedStyle(editor);
  const lineHeight = Number.parseFloat(computed.lineHeight) || 20;
  const paddingTop = Number.parseFloat(computed.paddingTop) || 0;
  const y = paddingTop + (line - 1) * lineHeight - editor.scrollTop;
  editor.style.backgroundImage = "linear-gradient(rgba(34,211,238,.055),rgba(34,211,238,.055))";
  editor.style.backgroundSize = `100% ${lineHeight}px`;
  editor.style.backgroundRepeat = "no-repeat";
  editor.style.backgroundPosition = `0 ${y}px`;
  gutter.dataset.activeLine = String(line);
}

function updateMinimap() {
  const editor = $("#editor"), code = $("#minimap-code"), viewport = $("#minimap-viewport");
  if (!editor || !code || !viewport) return;
  code.textContent = editor.value;
  const ratio = editor.scrollHeight ? editor.clientHeight / editor.scrollHeight : 1;
  viewport.style.height = `${Math.max(12, Math.min(100, ratio * 100))}%`;
  viewport.style.top = `${Math.max(0, (editor.scrollTop / Math.max(1, editor.scrollHeight)) * 100)}%`;
}

function updateEditorChrome() {
  const info = languageInfo(state.activeTab?.path || "");
  const content = $("#editor")?.value || "";
  if ($("#status-language")) $("#status-language").textContent = info[0];
  if ($("#status-encoding")) $("#status-encoding").textContent = state.activeTab?.encoding?.toUpperCase() || "UTF-8";
  if ($("#status-eol")) $("#status-eol").textContent = content.includes("\r\n") ? "CRLF" : "LF";
  if ($("#workspace-project")) $("#workspace-project").textContent = state.currentName || state.activeTab?.filename || "No project";
  if ($("#active-file-name")) $("#active-file-name").textContent = state.activeTab?.filename || "No file open";
  $("#active-file-icon")?.setAttribute("class", `mini-file-icon ${fileKind(state.activeTab?.path || "")}`);
  renderAIContext();
  updateWorkspaceStatus();
}

function renderAIContext() {
  const target = $("#ai-recent-context");
  if (!target) return;
  const tabs = state.openTabs.slice(-4).reverse();
  if (!tabs.length) {
    target.innerHTML = `<p>No recent files yet.</p>`;
    return;
  }
  target.innerHTML = tabs.map((tab) => `<div class="context-row"><span>${fileKind(tab.path) === "python" ? "◆" : "◇"}</span><div><strong>${escapeHtml(tab.filename)}</strong><small>${tab.tab_id === state.activeTab?.tab_id ? "Active now" : "Open tab"}</small></div></div>`).join("");
}

function updateOutline() {
  const target = $("#outline-content");
  if (!target) return;
  const content = $("#editor")?.value || "";
  const symbols = [...content.matchAll(/^\s*(?:def|class|function)\s+([A-Za-z_$][\w$]*)/gm)].map((match) => match[1]);
  target.innerHTML = symbols.length ? symbols.map((name) => `<div>◇ ${escapeHtml(name)}</div>`).join("") : `<div>No symbols found.</div>`;
}

function updateFolderContext() {
  const project = $("#status-project");
  if (project) project.textContent = state.currentName ? `Project: ${state.currentName}` : state.activeTab ? `File: ${state.activeTab.filename}` : "No project open";
  if ($("#explorer-project-name")) $("#explorer-project-name").textContent = state.currentName ? state.currentName.toUpperCase() : state.demo ? "NEXCORE DEMO" : "NO FOLDER OPEN";
  if ($("#workspace-project")) $("#workspace-project").textContent = state.currentName || state.activeTab?.filename || "No project";
  if ($("#orbital-project-name")) $("#orbital-project-name").textContent = state.currentName || (state.demo ? "NexCore Demo" : "Folders");
  if ($("#orbit-breadcrumb")) $("#orbit-breadcrumb").textContent = orbitalRelativePath();
  updatePrompt();
  updateWorkspaceStatus();
}

function updatePrompt() {
  const prompt = $("#terminal-prompt");
  if (!prompt) return;
  prompt.textContent = `${state.currentPath || "D:\\Ide"}>`;
  prompt.title = state.currentPath || "D:\\Ide";
}

const KEYWORDS = new Set(["and","as","assert","async","await","break","case","class","continue","def","del","elif","else","except","False","finally","for","from","global","if","import","in","is","lambda","match","None","nonlocal","not","or","pass","raise","return","True","try","while","with","yield"]);
const BUILTINS = new Set(["abs","all","any","bin","bool","bytearray","bytes","callable","chr","dict","dir","divmod","enumerate","eval","filter","float","format","frozenset","getattr","hasattr","hash","help","hex","id","input","int","isinstance","issubclass","iter","len","list","map","max","memoryview","min","next","object","oct","open","ord","pow","print","property","range","repr","reversed","round","set","setattr","slice","sorted","str","sum","super","tuple","type","vars","zip","__import__"]);

function tokenizePython(source) {
  const tokens = [];
  let index = 0;
  while (index < source.length) {
    const char = source[index];
    if (char === "#") {
      let end = source.indexOf("\n", index);
      if (end < 0) end = source.length;
      tokens.push({ text: source.slice(index, end), type: "comment" }); index = end; continue;
    }
    if (char === "'" || char === '"') {
      const quote = char;
      const triple = source.slice(index, index + 3) === quote.repeat(3);
      let end = index + (triple ? 3 : 1);
      while (end < source.length) {
        if (source[end] === "\\") { end += 2; continue; }
        if (triple && source.slice(end, end + 3) === quote.repeat(3)) { end += 3; break; }
        if (!triple && source[end] === quote) { end += 1; break; }
        end += 1;
      }
      tokens.push({ text: source.slice(index, end), type: "string" }); index = end; continue;
    }
    const number = source.slice(index).match(/^(?:0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/);
    if (number) { tokens.push({ text: number[0], type: "number" }); index += number[0].length; continue; }
    const identifier = source.slice(index).match(/^[A-Za-z_]\w*/);
    if (identifier) { tokens.push({ text: identifier[0], type: "identifier" }); index += identifier[0].length; continue; }
    const other = source.slice(index).match(/^[^#'"A-Za-z_0-9]+/);
    const text = other ? other[0] : char;
    tokens.push({ text, type: "normal" }); index += text.length;
  }
  return tokens;
}

function highlightPython(source) {
  let previousWord = "", importLine = false, functionSignature = false, parameterDepth = 0;
  return tokenizePython(source).map((token) => {
    let type = token.type;
    if (type === "identifier") {
      if (previousWord === "def") { type = "function"; functionSignature = true; }
      else if (previousWord === "class") type = "class";
      else if (importLine && !KEYWORDS.has(token.text)) type = "import";
      else if (functionSignature && parameterDepth > 0) type = "parameter";
      else if (KEYWORDS.has(token.text)) type = "keyword";
      else if (BUILTINS.has(token.text)) type = "builtin";
      else type = "normal";
      if (token.text === "import" || token.text === "from") importLine = true;
      previousWord = token.text;
    } else if (type === "normal") {
      for (const char of token.text) {
        if (functionSignature && char === "(") parameterDepth += 1;
        if (functionSignature && char === ")") { parameterDepth = Math.max(0, parameterDepth - 1); if (!parameterDepth) functionSignature = false; }
        if (char === "\n") { importLine = false; previousWord = ""; }
      }
    }
    return `<span class="tok-${type}">${escapeHtml(token.text)}</span>`;
  }).join("");
}

function highlightGeneric(source, extension) {
  const keywords = new Set(["import","export","from","const","let","var","function","return","if","else","for","while","switch","case","break","continue","class","extends","new","async","await","try","catch","finally","throw","typeof","interface","type","implements","public","private","protected","static","this","true","false","null","undefined"]);
  let index = 0;
  const parts = [];
  while (index < source.length) {
    if (source.startsWith("//", index) || source.startsWith("/*", index)) {
      const block = source.startsWith("/*", index);
      let end = block ? source.indexOf("*/", index + 2) : source.indexOf("\n", index);
      if (end < 0) end = source.length; else if (block) end += 2;
      parts.push(`<span class="tok-comment">${escapeHtml(source.slice(index, end))}</span>`); index = end; continue;
    }
    const char = source[index];
    if (char === "'" || char === '"' || char === "`") {
      let end = index + 1;
      while (end < source.length) {
        if (source[end] === "\\") { end += 2; continue; }
        if (source[end] === char) { end += 1; break; }
        end += 1;
      }
      parts.push(`<span class="tok-string">${escapeHtml(source.slice(index, end))}</span>`); index = end; continue;
    }
    const number = source.slice(index).match(/^\d+(?:\.\d+)?/);
    if (number) { parts.push(`<span class="tok-number">${number[0]}</span>`); index += number[0].length; continue; }
    const identifier = source.slice(index).match(/^[A-Za-z_$][\w$-]*/);
    if (identifier) {
      const word = identifier[0];
      const before = source.slice(Math.max(0, index - 2), index);
      let type = keywords.has(word) ? "keyword" : /^[A-Z]/.test(word) ? "type" : before.endsWith(".") || (extension === "css" && source.slice(index + word.length).trimStart().startsWith(":")) ? "property" : "normal";
      parts.push(`<span class="tok-${type}">${escapeHtml(word)}</span>`); index += word.length; continue;
    }
    if ((extension === "html" || extension === "jsx" || extension === "tsx") && char === "<") {
      const end = source.indexOf(">", index);
      if (end >= 0) { parts.push(`<span class="tok-tag">${escapeHtml(source.slice(index, end + 1))}</span>`); index = end + 1; continue; }
    }
    parts.push(escapeHtml(char)); index += 1;
  }
  return parts.join("");
}

function updateHighlighting() {
  const editor = $("#editor"), code = $("#highlight-code");
  if (!editor || !code) return;
  const extension = (state.activeTab?.path || "").split(".").pop().toLowerCase();
  const highlighted = extension === "py" ? highlightPython(editor.value) : highlightGeneric(editor.value, extension);
  code.innerHTML = `${highlighted}${editor.value.endsWith("\n") ? " " : "\n"}`;
  updateLineNumbers();
  updateMinimap();
  syncEditorScroll();
  updateCaretFeedback();
}

function syncEditorScroll() {
  const editor = $("#editor"), layer = $("#highlight-layer");
  if (!editor || !layer) return;
  layer.scrollTop = editor.scrollTop;
  layer.scrollLeft = editor.scrollLeft;
  const gutter = $("#line-numbers");
  if (gutter) gutter.scrollTop = editor.scrollTop;
  updateMinimap();
}

function loadRealTab(tab, demoTab = false) {
  if (!isValidTab(tab)) {
    toast("Invalid file response", "The backend did not return a usable editor tab.", true);
    return false;
  }
  state.activeTab = tab;
  state.activePath = tab.path;
  state.demo = demoTab;
  if (!demoTab) state.openTabs = state.openTabs.filter((item) => !String(item.tab_id).startsWith("demo-"));
  state.modified = Boolean(tab.is_modified);
  upsertOpenTab(tab);
  $("#editor").value = tab.content;
  if (state.terminalIsDemo) clearTerminal();
  updateHighlighting();
  $("#editor").scrollTop = 0;
  $("#editor").scrollLeft = 0;
  syncEditorScroll();
  renderTabs();
  renderExplorer();
  renderBreadcrumbs();
  updateEditorChrome();
  updateFolderContext();
  persistWorkspaceSettings();
  return true;
}

function initializeDemoWorkspace() {
  if (state.activeTab || state.rootPath) return;
  state.demo = true;
  const demoFolder = { name: "Folders", path: "demo/src", is_dir: true, isDemo: true, children: [] };
  state.nodes = [
    demoFolder,
    { name: "analysis.py", path: "demo/analysis.py", is_dir: false, isDemo: true },
    { name: "helper.py", path: "demo/helper.py", is_dir: false, isDemo: true },
    { name: "main.py", path: "demo/main.py", is_dir: false, isDemo: true },
    { name: "Mode.py", path: "demo/Mode.py", is_dir: false, isDemo: true },
  ];
  state.treeChildren = new Map([[demoFolder.path, demoFolder.children]]);
  state.expandedFolders = new Set([demoFolder.path]);
  state.orbitHistory = [];
  state.orbitOffset = 1;
  state.currentPath = "demo";
  state.currentName = "NexCore Demo";
  const demoTab = { tab_id: "demo-analysis.py", filename: "analysis.py", path: "demo/analysis.py", content: DEMO_CODE, is_modified: false, is_untitled: false, encoding: "utf-8" };
  state.openTabs = [demoTab];
  loadRealTab(demoTab, true);
  seedDemoTerminal();
}

function clearEditorForFolder() {
  if (state.activeTab) return;
  $("#editor").value = "";
  updateHighlighting();
  renderBreadcrumbs();
  updateEditorChrome();
}

async function openFileFromWelcome() {
  const result = await callBridge("open_file", "");
  if (!result || result.cancelled) return false;
  if (!result.ok || !isValidTab(result.tab)) {
    toast("Could not open file", friendlyError(result.error, "The backend returned an invalid file response."), true);
    return false;
  }
  loadRealTab(result.tab);
  addRecentItem(result.tab.path, "file");
  showWorkspaceView(true);
  return true;
}

async function openFolderFromWelcome() {
  const result = await callBridge("choose_directory");
  if (!result || result.cancelled) return false;
  if (!result.ok || !isValidFolderResult(result)) {
    toast("Could not open folder", friendlyError(result.error, "The backend returned an invalid folder response."), true);
    return false;
  }
  applyFolderResult(result);
  addRecentItem(result.root.path, "folder");
  showWorkspaceView();
  return true;
}

function applyFolderResult(result) {
  state.rootPath = result.root.path;
  state.currentPath = result.root.path;
  state.currentName = result.root.name;
  state.nodes = sortNodes(result.children || []);
  state.treeChildren = new Map();
  state.expandedFolders = new Set();
  state.orbitHistory = [];
  state.orbitOffset = 0;
  state.demo = false;
  state.openTabs = state.openTabs.filter((tab) => !String(tab.tab_id).startsWith("demo-"));
  if (String(state.activeTab?.tab_id || "").startsWith("demo-")) state.activeTab = null;
  if (state.terminalIsDemo) clearTerminal();
  clearEditorForFolder();
  updateFolderContext();
  renderExplorer();
  renderTabs();
  refreshGitStatus();
}

async function newFileFromWelcome() {
  const result = await callBridge("new_file");
  if (!result) return false;
  if (!result.ok || !isValidTab(result.tab)) {
    toast("Could not create file", friendlyError(result.error, "The backend returned an invalid untitled tab."), true);
    return false;
  }
  loadRealTab(result.tab);
  showWorkspaceView(true);
  return true;
}

async function openRecentProject(path) {
  const item = state.recentItems.find((entry) => samePath(entry.path, path));
  if (!item) return false;
  let result;
  if (item.type === "folder") {
    result = await callBridge("list_directory", item.path);
    if (result?.ok && isValidFolderResult(result)) {
      applyFolderResult(result);
      addRecentItem(item.path, "folder");
      showWorkspaceView();
      return true;
    }
  } else {
    result = await callBridge("open_file", item.path);
    if (result?.ok && isValidTab(result.tab)) {
      loadRealTab(result.tab);
      addRecentItem(item.path, "file");
      showWorkspaceView(true);
      return true;
    }
  }
  removeRecentItem(item.path);
  toast("Recent item unavailable", friendlyError(result?.error, "This path no longer exists and was removed from Recent."), true);
  return false;
}

function enterOrbitalWorkspace() {
  if (!state.activeTab && !state.rootPath) initializeDemoWorkspace();
  showWorkspaceView(Boolean(state.activeTab));
}

function setView(name) {
  const welcome = $("#welcome-view"), workspace = $("#workspace-view");
  const showWorkspace = name === "workspace";
  state.currentView = name;
  welcome.classList.toggle("hidden", showWorkspace);
  workspace.classList.toggle("hidden", !showWorkspace);
  welcome.classList.toggle("active", !showWorkspace);
  workspace.classList.toggle("active", showWorkspace);
  welcome.setAttribute("aria-hidden", String(showWorkspace));
  workspace.setAttribute("aria-hidden", String(!showWorkspace));
  welcome.inert = showWorkspace;
  workspace.inert = !showWorkspace;
  const shown = showWorkspace ? workspace : welcome;
  shown.classList.add("transitioning-in");
  window.setTimeout(() => shown.classList.remove("transitioning-in"), 320);
}

function showWelcomeView() {
  closeOverlays();
  setView("welcome");
  renderRecentItems();
}

function showWorkspaceView(focusEditor = false) {
  closeOverlays();
  setView("workspace");
  window.requestAnimationFrame(() => {
    renderExplorer();
    renderTabs();
    renderBreadcrumbs();
    updateHighlighting();
    syncEditorScroll();
    updateFolderContext();
    updatePrompt();
    updateEditorChrome();
    updateLineNumbers();
    updateMinimap();
    if (state.aiOpen) toggleAI(true);
    window.dispatchEvent(new Event("resize"));
    if (focusEditor) $("#editor")?.focus();
  });
}

function returnToWelcome() {
  if (requestUnsavedProtection("return-home")) return;
  showWelcomeView();
}

function modifiedTabs() {
  return state.openTabs.filter((tab) => tab.is_modified && !String(tab.tab_id).startsWith("demo-"));
}

function requestUnsavedProtection(action, tab = null) {
  const dirty = tab ? [tab] : modifiedTabs();
  if (!dirty.length) return false;
  if (state.activeTab?.is_modified) saveActiveRecovery();
  state.pendingWorkspaceAction = action;
  if (tab) state.pendingTabClose = tab;
  const multiple = dirty.length > 1;
  $("#unsaved-title").textContent = multiple
    ? `You have unsaved changes in ${dirty.length} files.`
    : `You have unsaved changes in “${dirty[0].filename}”.`;
  $("#unsaved-message").textContent = "Save your changes before continuing?";
  $("#unsaved-save").textContent = multiple ? "Save All" : "Save";
  $("#unsaved-discard").textContent = "Don’t Save";
  $("#unsaved-modal")?.classList.remove("hidden");
  $("#unsaved-save")?.focus();
  return true;
}

async function saveAllModified() {
  const dirty = modifiedTabs();
  for (const tab of dirty) {
    if (state.activeTab?.tab_id !== tab.tab_id) await activateEditorTab(tab);
    if (!(await saveActiveFile())) return false;
  }
  return true;
}

async function discardAllModified() {
  for (const tab of [...modifiedTabs()]) await closeEditorTab(tab, true);
}

async function completeProtectedAction(action) {
  if (action === "open-folder") return openFolder();
  if (action === "exit-app") return callBridge("window_close");
  if (action === "return-home") return showWelcomeView();
}

function closeUnsavedModal() {
  state.pendingReturn = false;
  state.pendingTabClose = null;
  state.pendingWorkspaceAction = "";
  $("#unsaved-modal")?.classList.add("hidden");
}

function markModified() {
  if (!state.activeTab || state.demo) return;
  state.modified = true;
  setApplicationStatus("Modified", "", false);
  if (state.activeTab) {
    state.activeTab.content = $("#editor").value;
    state.activeTab.is_modified = true;
    upsertOpenTab(state.activeTab);
    renderTabs();
    renderExplorer();
  }
  window.clearTimeout(state.updateTimer);
  const tabId = state.activeTab.tab_id;
  const content = $("#editor").value;
  state.updateTimer = window.setTimeout(() => {
    callBridge("update_file", tabId, content);
  }, 160);
  scheduleAutosave();
  scheduleRecovery();
}

function scheduleAutosave() {
  window.clearTimeout(state.autosave.timer);
  if (state.autosave.mode !== "delay" || state.autosave.suppressed || !state.activeTab || state.activeTab.is_untitled || state.demo) return;
  state.autosave.timer = window.setTimeout(autosaveActiveFile, state.autosave.delay);
}

async function autosaveActiveFile() {
  if (state.autosave.saving || !state.modified || !state.activeTab || state.activeTab.is_untitled || !$("#unsaved-modal")?.classList.contains("hidden")) return;
  state.autosave.saving = true;
  const recoveryId = state.activeTab.recovery_id;
  setApplicationStatus("Autosaving…", "", false);
  const result = await callBridge("save_file", state.activeTab.tab_id, $("#editor").value, state.activeTab.path);
  state.autosave.saving = false;
  if (!result?.ok || !isValidTab(result.tab)) {
    setApplicationStatus("Autosave failed", "error");
    return;
  }
  state.activeTab = result.tab;
  state.modified = false;
  upsertOpenTab(result.tab);
  renderTabs();
  setApplicationStatus("Autosaved");
  if (recoveryId) await callBridge("discard_recovery_snapshot", recoveryId);
}

function scheduleRecovery() {
  if (state.recoveryTimer) return;
  state.recoveryTimer = window.setTimeout(saveActiveRecovery, Math.max(5000, Number(state.settings?.files?.recovery_interval || 10) * 1000));
}

async function saveActiveRecovery() {
  state.recoveryTimer = 0;
  if (!state.modified || !state.activeTab || state.demo) return;
  const result = await callBridge("save_recovery_snapshot", {
    tab_id: state.activeTab.tab_id,
    path: state.activeTab.path || "",
    filename: state.activeTab.filename,
    project_root: state.rootPath,
    content: $("#editor").value,
    saved_hash: state.activeTab.saved_hash || "",
    encoding: state.activeTab.encoding || "utf-8",
    cursor: $("#editor").selectionStart,
    scroll_top: $("#editor").scrollTop,
  });
  if (result?.ok) state.activeTab.recovery_id = result.record?.id;
  if (state.modified) scheduleRecovery();
}

async function clearActiveRecovery() {
  const recordId = state.activeTab?.recovery_id;
  if (recordId) await callBridge("discard_recovery_snapshot", recordId);
  if (state.activeTab) delete state.activeTab.recovery_id;
}

async function saveActiveFile() {
  if (!state.activeTab || state.demo) {
    toast("No real file open", "Create or open a file before saving.", true);
    return false;
  }
  setApplicationStatus("Saving…");
  const recoveryId = state.activeTab.recovery_id;
  window.clearTimeout(state.updateTimer);
  const result = await callBridge("save_file", state.activeTab.tab_id, $("#editor").value, "");
  if (result?.cancelled) {
    setApplicationStatus("Ready");
    return false;
  }
  if (!result?.ok || !isValidTab(result.tab)) {
    setApplicationStatus("Error", "error");
    toast("Save failed", friendlyError(result?.error, "The backend returned an invalid save response."), true);
    return false;
  }
  state.activeTab = result.tab;
  state.activePath = result.tab.path;
  state.modified = false;
  upsertOpenTab(result.tab);
  renderTabs();
  renderExplorer();
  renderBreadcrumbs();
  updateEditorChrome();
  addRecentItem(result.tab.path, "file");
  updateFolderContext();
  setApplicationStatus("Saved");
  if (recoveryId) await callBridge("discard_recovery_snapshot", recoveryId);
  refreshGitStatus();
  return true;
}

async function saveActiveFileAs() {
  if (!state.activeTab || state.demo) return toast("No real file open", "Open or create a file first.", true);
  setApplicationStatus("Saving…");
  const recoveryId = state.activeTab.recovery_id;
  const result = await callBridge("save_file_as", state.activeTab.tab_id, $("#editor").value);
  if (!result || result.cancelled) return false;
  if (!result.ok || !isValidTab(result.tab)) {
    setApplicationStatus("Error", "error");
    toast("Save As failed", friendlyError(result.error), true);
    return false;
  }
  state.modified = false;
  state.activeTab = result.tab;
  state.activePath = result.tab.path;
  upsertOpenTab(result.tab);
  addRecentItem(result.tab.path, "file");
  renderTabs(); renderBreadcrumbs(); renderExplorer(); updateEditorChrome();
  setApplicationStatus("Saved");
  if (recoveryId) await callBridge("discard_recovery_snapshot", recoveryId);
  refreshGitStatus();
  return true;
}

function toggleExplorer(force) {
  if (state.currentView !== "workspace") showWorkspaceView();
  const view = $("#workspace-view");
  const hidden = typeof force === "boolean" ? force : !view?.classList.contains("navigation-hidden");
  view?.classList.toggle("navigation-hidden", hidden);
  if (!hidden) $("#orbital-navigation")?.focus();
}

function toggleAI(force) {
  if (state.currentView !== "workspace") showWorkspaceView();
  const drawer = $("#ai-drawer");
  if (!drawer) return;
  const open = typeof force === "boolean" ? force : !drawer.classList.contains("open");
  state.aiOpen = open;
  $("#workspace-view")?.classList.toggle("ai-open", open);
  drawer.classList.toggle("open", open);
  drawer.setAttribute("aria-hidden", String(!open));
  $("#ai-toggle")?.setAttribute("aria-expanded", String(open));
  updateWorkspaceLayout();
  persistWorkspaceSettings();
  if (open) window.setTimeout(() => $("#ai-input")?.focus(), 260);
}

function updateWorkspaceLayout() {
  const terminal = $("#terminal-panel");
  if (!terminal) return;
  // Reading the computed size after the class change starts the CSS
  // transition reliably in WebView2 and lets terminal internals reflow.
  terminal.getBoundingClientRect();
  window.requestAnimationFrame(() => {
    terminal.dispatchEvent(new Event("resize"));
    renderExplorer();
  });
}

function setAIAvailability(status) {
  state.aiAvailable = Boolean(status?.available);
  if ($("#setting-ai-status")) $("#setting-ai-status").textContent = state.aiAvailable ? `${status.provider || "AI"} · Configured` : "Not configured";
  const pill = $("#ai-engine-pill");
  if (pill) {
    pill.classList.toggle("available", state.aiAvailable);
    pill.innerHTML = `<i></i> ${state.aiAvailable ? `${escapeHtml(status.provider || "AI")} configured` : "Not configured"}`;
  }
  const conversation = $("#ai-conversation");
  if (conversation && !state.aiMessages.length) {
    conversation.innerHTML = `<div class="ai-config-message">${state.aiAvailable
      ? `Connected to ${escapeHtml(status.provider || "the configured AI service")}.`
      : "NexCore AI is not configured yet. Set ANTHROPIC_API_KEY in the Python environment to enable real responses."}</div>`;
  }
}

function appendAIMessage(role, content) {
  const conversation = $("#ai-conversation");
  if (!conversation) return null;
  conversation.querySelector(".ai-config-message")?.remove();
  const message = document.createElement("div");
  message.className = `ai-message ${role}`;
  message.textContent = content;
  conversation.appendChild(message);
  conversation.scrollTop = conversation.scrollHeight;
  if (role === "user" || role === "assistant") state.aiMessages.push({ role, content });
  return message;
}

async function sendAIMessage(prompt, contextOverride = "") {
  const text = String(prompt || "").trim();
  if (!text || state.aiBusy) return false;
  toggleAI(true);
  appendAIMessage("user", text);
  if (!state.aiAvailable) {
    appendAIMessage("error", "NexCore AI is not configured yet.");
    return false;
  }
  state.aiBusy = true;
  const loading = appendAIMessage("loading", "NexCore AI is thinking…");
  const editor = $("#editor");
  const requestContent = editor?.value || "";
  const requestHash = await hashText(requestContent);
  const contextParts = [];
  if (contextOverride) contextParts.push(`Selected code:\n${contextOverride}`);
  else if ($("#ai-selection-context")?.checked) {
    const selected = editor?.value.slice(editor.selectionStart, editor.selectionEnd) || "";
    if (selected) contextParts.push(`Selected code:\n${selected}`);
  }
  if ($("#ai-file-context")?.checked) contextParts.push(`Active file:\n${editor?.value || ""}`);
  if ($("#ai-terminal-context")?.checked) {
    const errors = [...$$("#terminal-output .stderr, #terminal-output .error")].map((node) => node.textContent).join("\n");
    if (errors) contextParts.push(`Terminal errors:\n${errors}`);
  }
  if ($("#ai-project-context")?.checked) {
    contextParts.push(`Project structure:\n${state.nodes.map((node) => `${node.is_dir ? "[folder]" : "[file]"} ${node.name}`).join("\n")}`);
  }
  if ($("#ai-problems-context")?.checked && state.problems.length) {
    contextParts.push(`Current problems:\n${state.problems.map((problem) => `${problem.severity}: ${problem.path}:${problem.line} ${problem.message}`).join("\n")}`);
  }
  if ($("#ai-git-context")?.checked && state.activeTab?.path) {
    const gitDiff = await callBridge("get_git_diff", state.activeTab.path);
    if (gitDiff?.ok && gitDiff.diff) contextParts.push(`Git diff:\n${gitDiff.diff}`);
  }
  const includeContext = contextParts.length > 0;
  const context = contextParts.join("\n\n");
  if (includeContext) appendAIMessage("context", `Context sent:\n${context}`);
  const history = state.aiMessages.slice(0, -1);
  const result = await callBridge(
    "ai_chat",
    text,
    state.activeTab?.filename || "",
    context,
    includeContext,
    history,
  );
  loading?.remove();
  state.aiBusy = false;
  if (!result?.ok) {
    console.error("[NexCore AI]", result?.error || "No backend response");
    appendAIMessage("error", friendlyError(result?.error, "NexCore AI could not complete the request."));
    return false;
  }
  appendAIMessage("assistant", result.reply);
  const codeMatch = String(result.reply || "").match(/```(?:[A-Za-z0-9_+-]+)?\s*\n([\s\S]*?)```/);
  if (codeMatch && state.activeTab) showAIChangePreview(codeMatch[1].replace(/\n$/, ""), requestContent, requestHash);
  return true;
}

async function hashText(text) {
  const bytes = new TextEncoder().encode(String(text));
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function showAIChangePreview(proposed, original, originalHash) {
  const before = original.split("\n"), after = proposed.split("\n");
  const diff = [];
  const count = Math.max(before.length, after.length);
  for (let index = 0; index < count; index += 1) {
    if (before[index] === after[index]) diff.push(`  ${before[index] || ""}`);
    else {
      if (before[index] !== undefined) diff.push(`- ${before[index]}`);
      if (after[index] !== undefined) diff.push(`+ ${after[index]}`);
    }
  }
  state.aiChangePreview = { proposed, original, originalHash, tabId: state.activeTab.tab_id, filename: state.activeTab.filename };
  $("#ai-change-title").textContent = `AI Change Preview · ${state.activeTab.filename}`;
  $("#ai-change-diff").textContent = diff.join("\n");
  $("#ai-change-modal")?.classList.remove("hidden");
}

function handleAIQuickAction(action) {
  const editor = $("#editor");
  const selected = editor?.value.slice(editor.selectionStart, editor.selectionEnd) || "";
  if (action === "generate") {
    toggleAI(true);
    const input = $("#ai-input");
    if (input) {
      input.value = "Generate code that ";
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
    return;
  }
  if (!state.activeTab) {
    toggleAI(true);
    appendAIMessage("error", "Open a file before using this action.");
    return;
  }
  const prompts = {
    explain: "Explain this code clearly and identify its important behavior.",
    refactor: "Suggest a refactoring for this file. Present proposed code, but do not apply it.",
    bugs: "Review this code for bugs, edge cases, and potential failures.",
    comments: "Propose a commented version of this code. Do not apply changes automatically.",
  };
  $("#ai-file-context").checked = true;
  sendAIMessage(prompts[action], selected);
}

function toggleTerminal(force) {
  const panel = $("#terminal-panel");
  if (!panel) return;
  const collapsed = force ?? !panel.classList.contains("terminal-collapsed");
  panel.classList.toggle("terminal-collapsed", collapsed);
  persistWorkspaceSettings();
}

function terminalHeightLimits() {
  const available = Math.max(400, window.innerHeight - 70 - 48);
  return { min: 120, max: Math.max(120, Math.floor(available * .48)) };
}

function setTerminalHeight(height, persist = true) {
  const limits = terminalHeightLimits();
  state.terminalHeight = Math.max(limits.min, Math.min(limits.max, Number(height) || Math.floor(window.innerHeight * .265)));
  document.documentElement.style.setProperty("--terminal-height", `${state.terminalHeight}px`);
  $("#terminal-splitter")?.setAttribute("aria-valuenow", String(Math.round(state.terminalHeight)));
  if (persist) persistWorkspaceSettings();
}

function resetTerminalHeight() {
  setTerminalHeight(Math.floor(window.innerHeight * .265));
}

function persistWorkspaceSettings() {
  const payload = {
    terminalHeight: state.terminalHeight,
    terminalCollapsed: $("#terminal-panel")?.classList.contains("terminal-collapsed") || false,
    aiOpen: state.aiOpen,
    statusbarHidden: $("#workspace-view")?.classList.contains("statusbar-hidden") || false,
    navigationHidden: $("#workspace-view")?.classList.contains("navigation-hidden") || false,
    fileFilter: state.fileFilter,
    rootPath: state.rootPath,
    currentPath: state.currentPath,
    orbitOffset: state.orbitOffset,
    openFiles: state.openTabs.filter((tab) => !tab.is_untitled && !String(tab.tab_id).startsWith("demo-")).map((tab) => ({ path: tab.path, cursor: tab.cursor || 0, scrollTop: tab.scrollTop || 0 })),
    activePath: state.activeTab && !state.activeTab.is_untitled ? state.activeTab.path : "",
  };
  try {
    localStorage.setItem("nexcore.workspace", JSON.stringify(payload));
  } catch {}
  if (state.bridgeReady) callBridge("save_workspace_state", payload);
}

function restoreWorkspaceSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("nexcore.workspace") || "{}");
    state.persistedWorkspace = saved;
    setTerminalHeight(Number(saved.terminalHeight) || Math.floor(window.innerHeight * .265), false);
    $("#terminal-panel")?.classList.toggle("terminal-collapsed", Boolean(saved.terminalCollapsed));
    $("#workspace-view")?.classList.toggle("statusbar-hidden", Boolean(saved.statusbarHidden));
    $("#workspace-view")?.classList.toggle("navigation-hidden", Boolean(saved.navigationHidden));
    state.fileFilter = typeof saved.fileFilter === "string" ? saved.fileFilter : "";
    state.aiOpen = Boolean(saved.aiOpen);
    $("#orbital-filter-input").value = state.fileFilter;
  } catch {
    resetTerminalHeight();
  }
}

async function restorePersistedPaths() {
  const saved = state.persistedWorkspace;
  if (!saved || typeof saved !== "object") return;
  if (typeof saved.rootPath === "string" && saved.rootPath) {
    const root = await callBridge("list_directory", saved.rootPath);
    if (root?.ok && isValidFolderResult(root)) {
      applyFolderResult(root);
      if (saved.currentPath && !samePath(saved.currentPath, saved.rootPath)) {
        const current = await callBridge("list_children", saved.currentPath);
        if (current?.ok && Array.isArray(current.children)) {
          state.currentPath = current.path;
          state.nodes = sortNodes(current.children);
        }
      }
      state.orbitOffset = Math.max(0, Math.min(Number(saved.orbitOffset) || 0, Math.max(0, state.nodes.length - 1)));
      updateFolderContext();
      renderExplorer();
    }
  }
  const validFiles = Array.isArray(saved.openFiles) ? saved.openFiles.map((item) => typeof item === "string" ? { path: item } : item).filter((item) => item && typeof item.path === "string").slice(0, 20) : [];
  for (const savedFile of validFiles) {
    const result = await callBridge("open_file", savedFile.path);
    if (result?.ok && isValidTab(result.tab)) {
      result.tab.cursor = Number(savedFile.cursor) || 0;
      result.tab.scrollTop = Number(savedFile.scrollTop) || 0;
      loadRealTab(result.tab);
    }
  }
  if (saved.activePath) {
    const active = state.openTabs.find((tab) => samePath(tab.path, saved.activePath));
    if (active) {
      await activateEditorTab(active);
      $("#editor")?.setSelectionRange(active.cursor || 0, active.cursor || 0);
      $("#editor").scrollTop = active.scrollTop || 0;
    }
  }
  persistWorkspaceSettings();
}

function showTerminal() {
  if (state.currentView !== "workspace") showWorkspaceView();
  toggleTerminal(false);
}

async function runActiveFile() {
  if (!state.activeTab || state.demo) {
    toast("Nothing to run", "Open or create and save a Python file first.", true);
    return false;
  }
  if (!state.activeTab.path.toLowerCase().endsWith(".py")) {
    toast("Unsupported run target", "This build executes Python files only.", true);
    return false;
  }
  if (!(await analyzeActivePython())) return false;
  if (!(await saveActiveFile())) return false;
  if (state.terminalIsDemo) clearTerminal();
  setRunStatus("Starting", "running");
  state.runActive = true;
  state.runStartedAt = performance.now();
  setApplicationStatus("Running", "running", false);
  updateMenuAvailability();
  const result = await callBridge("run_file", state.activeTab.path);
  if (!result?.ok) {
    state.runActive = false;
    setRunStatus("Error", "error");
    setApplicationStatus("Error", "error");
    updateMenuAvailability();
    toast("Run failed", friendlyError(result?.error, "The active file could not be executed."), true);
    return false;
  }
  return true;
}

function clearDemoTerminal() {
  if (state.terminalIsDemo) clearTerminal();
}

function clearTerminal() {
  $("#terminal-output")?.replaceChildren();
  state.terminalIsDemo = false;
}

function appendTerminal(text, kind = "stdout") {
  clearDemoTerminal();
  const output = $("#terminal-output");
  if (!output) return;
  const span = document.createElement("span");
  span.className = String(kind || "stdout").toLowerCase();
  span.textContent = String(text ?? "");
  output.appendChild(span);
  output.scrollTop = output.scrollHeight;
  if (["stderr", "error"].includes(span.className)) {
    const match = String(text).match(/File "([^"]+)", line (\d+)/);
    if (match && (!state.rootPath || match[1].toLowerCase().startsWith(state.rootPath.toLowerCase()))) {
      const exists = state.problems.some((item) => samePath(item.path || "", match[1]) && item.line === Number(match[2]) && item.source === "Runtime");
      if (!exists) {
        state.problems.push({ severity: "error", path: match[1], line: Number(match[2]), column: 1, message: "Python traceback", source: "Runtime" });
        renderProblems();
      }
    }
  }
}

function appendStyledLine(parts) {
  const output = $("#terminal-output");
  if (!output) return;
  const line = document.createElement("div");
  parts.forEach(([text, className]) => {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    line.appendChild(span);
  });
  output.appendChild(line);
}

function seedDemoTerminal() {
  clearTerminal();
  renderProblems();
  appendStyledLine([["D:\\Ide> ", "prompt"], ["python", "command"], [" calculate_score.py", "stdout"]]);
  appendStyledLine([[">>> Score calculation initiated...", "stdout"]]);
  appendStyledLine([["Calculating score for [5, 10, 15, 20, 25]...", "stdout"]]);
  appendStyledLine([["Score result: 1500.0", "stdout"]]);
  state.terminalIsDemo = true;
}

function setRunStatus(text, className = "") {
  const status = $("#run-status");
  if (!status) return;
  status.textContent = text;
  status.className = `run-status${className ? ` ${className}` : ""}`;
}

window.NexCoreEvents = {
  dispatch(name, payload = {}) {
    if (name === "terminal-output") return appendTerminal(payload.text, payload.kind || "stdout");
    if (name === "terminal-state") {
      if (payload.is_running) {
        state.runActive = true;
        setRunStatus("Running", "running");
        setApplicationStatus("Running", "running", false);
      }
      else {
        const failed = Number(payload.exit_code || 0) !== 0;
        state.runActive = false;
        const seconds = state.runStartedAt ? (performance.now() - state.runStartedAt) / 1000 : 0;
        setRunStatus(failed ? `Exit ${payload.exit_code}` : `Completed in ${seconds.toFixed(1)}s`, failed ? "error" : "");
        setApplicationStatus(failed ? "Error" : "Completed", failed ? "error" : "");
      }
      updateMenuAvailability();
      return;
    }
    if (name === "run-state") {
      if (payload.running) {
        state.runActive = true;
        setRunStatus("Running", "running");
        setApplicationStatus("Running", "running", false);
        appendTerminal("Running active file...\n", "system");
      } else {
        const code = payload.exit_code ?? 0;
        const failed = Number(code) !== 0;
        state.runActive = false;
        appendTerminal(`\nProcess finished with exit code ${code}\n`, failed ? "stderr" : "success");
        const seconds = state.runStartedAt ? (performance.now() - state.runStartedAt) / 1000 : 0;
        setRunStatus(failed ? `Exit ${code}` : `Completed in ${seconds.toFixed(1)}s`, failed ? "error" : "");
        setApplicationStatus(failed ? "Error" : "Completed", failed ? "error" : "");
      }
      updateMenuAvailability();
    }
  },
};

async function executeApplicationCommand(raw) {
  const command = raw.trim().toLowerCase();
  if (!command) return;
  clearDemoTerminal();
  appendStyledLine([[`${state.currentPath || "D:\\Ide"}> `, "prompt"], [raw.trim(), "command"]]);
  if (command === "run") await runActiveFile();
  else if (command === "stop") await callBridge("stop_run");
  else if (command === "clear") clearTerminal();
  else if (command === "open") await openFile();
  else if (command === "save") await saveActiveFile();
  else appendTerminal("Supported commands: run, stop, clear, open, save\n", "stderr");
}

function loadRecentItems() {
  try {
    const parsed = JSON.parse(localStorage.getItem("nexcore.recentItems") || "[]");
    state.recentItems = Array.isArray(parsed)
      ? parsed.filter((item) => item && typeof item.path === "string" && ["file", "folder"].includes(item.type)).slice(0, 8)
      : [];
  } catch {
    state.recentItems = [];
  }
  renderRecentItems();
}

function persistRecentItems() {
  try { localStorage.setItem("nexcore.recentItems", JSON.stringify(state.recentItems.slice(0, 8))); } catch {}
}

function addRecentItem(path, type) {
  if (!path || !["file", "folder"].includes(type)) return;
  state.recentItems = [{ path, type, openedAt: Date.now() }, ...state.recentItems.filter((item) => !samePath(item.path, path))].slice(0, 8);
  persistRecentItems();
  renderRecentItems();
}

function removeRecentItem(path) {
  state.recentItems = state.recentItems.filter((item) => !samePath(item.path, path));
  persistRecentItems();
  renderRecentItems();
}

function recentIcon(type) {
  return type === "folder"
    ? `<svg viewBox="0 0 24 24"><path d="M3 6h7l2 2h9v11H3Z"/></svg>`
    : `<svg viewBox="0 0 24 24"><path d="M5 3h10l4 4v14H5Z"/><path d="M10 12h5m-5 4h5"/></svg>`;
}

function renderRecentItems() {
  const list = $("#recent-list");
  if (!list) return;
  $("#recent-count").textContent = `${state.recentItems.length} item${state.recentItems.length === 1 ? "" : "s"}`;
  if (!state.recentItems.length) {
    list.innerHTML = `<p class="empty-recents">Projects and files you open will appear here.</p>`;
    return;
  }
  list.replaceChildren();
  state.recentItems.forEach((item) => {
    const row = document.createElement("div");
    row.className = "recent-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.innerHTML = `<span class="recent-icon ${item.type}">${recentIcon(item.type)}</span><span class="recent-copy"><strong>${escapeHtml(basename(item.path))}</strong><small>${escapeHtml(item.path)}</small></span><button class="recent-remove" type="button" aria-label="Remove ${escapeHtml(basename(item.path))}">×</button>`;
    const open = () => openRecentProject(item.path);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
    row.querySelector(".recent-remove").addEventListener("click", (event) => {
      event.stopPropagation();
      removeRecentItem(item.path);
    });
    list.appendChild(row);
  });
}

const commandActions = [
  { label: "New File", hint: "Create an untitled Python file", action: () => newFileFromWelcome() },
  { label: "New Folder", hint: "Create a folder in the current project", action: () => openCreationModal("folder") },
  { label: "Open File", hint: "Choose a file from disk", action: () => state.currentView === "welcome" ? openFileFromWelcome() : openFile() },
  { label: "Open Folder", hint: "Choose a project folder", action: () => state.currentView === "welcome" ? openFolderFromWelcome() : openFolder() },
  { label: "Save", hint: "Save active file", action: saveActiveFile },
  { label: "Save As", hint: "Save active file to a new path", action: saveActiveFileAs },
  { label: "Open Recent Project", hint: "Jump to recent projects", action: focusRecentSection },
  { label: "Enter Orbital Workspace", hint: "Show the coding workspace", action: enterOrbitalWorkspace },
  { label: "Run Active File", hint: "Save and execute", action: runActiveFile },
  { label: "Stop Execution", hint: "Stop the running process", action: () => callBridge("stop_run") },
  { label: "Clear Terminal", hint: "Clear output", action: clearTerminal },
  { label: "Show Terminal", hint: "Reveal integrated terminal", action: showTerminal },
  { label: "Toggle Explorer", hint: "Show or hide Explorer", action: toggleExplorer },
  { label: "Toggle AI Assistant", hint: "Show or hide AI panel", action: toggleAI },
  { label: "Refresh Folder", hint: "Reload the current orbital folder", action: refreshCurrentFolder },
  { label: "Search Project", hint: "Search text across the active project", action: openProjectSearch },
  { label: "Open Settings", hint: "Configure NexCore", action: openSettings },
  { label: "Return to Project Root", hint: "Show the project root folder", action: reloadProjectRoot },
  { label: "Return Home", hint: "Show the welcome screen", action: returnToWelcome },
];

function filteredCommands() {
  const input = state.currentView === "workspace" ? $("#workspace-command-search") : $("#command-search");
  const query = (input?.value || "").trim().toLowerCase();
  const tabCommands = state.openTabs.map((tab) => ({ label: `Open ${tab.filename}`, hint: "Open tab", action: () => activateEditorTab(tab) }));
  const projectCommands = state.nodes.slice(0, 30).map((node) => ({
    label: node.name,
    hint: node.is_dir ? "Orbital folder" : "Project file",
    action: () => node.is_dir ? enterOrbitalFolder(node) : openTreeFile(node),
  }));
  const recentCommands = state.recentItems.slice(0, 12).map((item) => ({
    label: basename(item.path),
    hint: `Recent ${item.type || "project"}`,
    action: () => openRecentProject(item.path),
  }));
  return [...commandActions, ...tabCommands, ...projectCommands, ...recentCommands].filter((command) => !query || `${command.label} ${command.hint}`.toLowerCase().includes(query));
}

function renderCommandResults() {
  const results = state.currentView === "workspace" ? $("#workspace-command-results") : $("#command-results");
  const input = state.currentView === "workspace" ? $("#workspace-command-search") : $("#command-search");
  if (!results || !input) return;
  const commands = filteredCommands();
  state.commandIndex = Math.max(0, Math.min(state.commandIndex, Math.max(0, commands.length - 1)));
  results.replaceChildren();
  commands.forEach((command, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `command-result${index === state.commandIndex ? " selected" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(index === state.commandIndex));
    button.innerHTML = `<span>${escapeHtml(command.label)}</span><small>${escapeHtml(command.hint)}</small>`;
    button.addEventListener("mouseenter", () => { state.commandIndex = index; renderCommandResults(); });
    button.addEventListener("click", () => executeCommand(command));
    results.appendChild(button);
  });
  results.classList.toggle("hidden", !commands.length);
  input.setAttribute("aria-expanded", String(Boolean(commands.length)));
}

function executeCommand(command) {
  closeCommandResults();
  const input = state.currentView === "workspace" ? $("#workspace-command-search") : $("#command-search");
  if (input) input.value = "";
  command?.action();
}

function closeCommandResults() {
  $("#command-results")?.classList.add("hidden");
  $("#workspace-command-results")?.classList.add("hidden");
  $("#command-search")?.setAttribute("aria-expanded", "false");
  $("#workspace-command-search")?.setAttribute("aria-expanded", "false");
}

function focusRecentSection() {
  $("#recent-section")?.scrollIntoView({ behavior: "smooth", block: "center" });
  const firstRecent = $(".recent-row");
  if (firstRecent) firstRecent.focus();
  else toast("Recent projects", "Projects and files you open will appear here.");
  closeCommandResults();
}

function closeMenus() {
  $$(".glass-menu").forEach((menu) => menu.classList.add("hidden"));
  $$(".menu-trigger[data-menu]").forEach((trigger) => trigger.setAttribute("aria-expanded", "false"));
}

function closeOverlays() {
  closeMenus();
  closeCommandResults();
  closeOpenFilesMenu();
  closeProjectSearch();
  $("#settings-panel")?.classList.add("hidden");
  $("#about-panel")?.classList.add("hidden");
  $("#toast")?.classList.add("hidden");
}

function toggleMenu(id, trigger) {
  const menu = $(`#${id}`);
  if (!menu) return;
  const willOpen = menu.classList.contains("hidden");
  closeMenus();
  if (willOpen && trigger) {
    const header = trigger.closest("header");
    const triggerBox = trigger.getBoundingClientRect();
    const headerBox = header?.getBoundingClientRect();
    menu.style.left = `${Math.max(8, triggerBox.left - (headerBox?.left || 0))}px`;
  }
  menu.classList.toggle("hidden", !willOpen);
  trigger?.setAttribute("aria-expanded", String(willOpen));
  if (willOpen) {
    updateMenuAvailability();
    menu.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
  }
}

async function performEditorAction(action) {
  const editor = $("#editor");
  if (!editor || !state.activeTab || state.demo) {
    toast("Editor action", "Open or create a file to use this command.");
    return;
  }
  if (state.currentView !== "workspace") showWorkspaceView(true);
  await new Promise((resolve) => requestAnimationFrame(resolve));
  editor.focus();
  if (action === "select-all") {
    editor.select();
    return;
  }
  if (action === "select-line") {
    const start = editor.value.lastIndexOf("\n", Math.max(0, editor.selectionStart - 1)) + 1;
    const nextBreak = editor.value.indexOf("\n", editor.selectionEnd);
    editor.setSelectionRange(start, nextBreak < 0 ? editor.value.length : nextBreak);
    return;
  }
  if (action === "edit-copy") {
    const selected = editor.value.slice(editor.selectionStart, editor.selectionEnd);
    if (!selected) return toast("Copy", "Select some code first.");
    try { await navigator.clipboard.writeText(selected); }
    catch { document.execCommand("copy"); }
    return;
  }
  if (action === "edit-paste") {
    try {
      const text = await navigator.clipboard.readText();
      editor.setRangeText(text, editor.selectionStart, editor.selectionEnd, "end");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    } catch {
      if (!document.execCommand("paste")) toast("Paste unavailable", "Use Ctrl+V to paste into the editor.");
    }
    return;
  }
  if (action === "edit-cut") {
    const selected = editor.value.slice(editor.selectionStart, editor.selectionEnd);
    if (!selected) return toast("Cut", "Select some code first.");
    try { await navigator.clipboard.writeText(selected); } catch {}
    editor.setRangeText("", editor.selectionStart, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  document.execCommand(action === "edit-redo" ? "redo" : "undo");
}

function currentLineBounds(editor) {
  const start = editor.value.lastIndexOf("\n", Math.max(0, editor.selectionStart - 1)) + 1;
  const breakIndex = editor.value.indexOf("\n", editor.selectionEnd);
  return { start, end: breakIndex < 0 ? editor.value.length : breakIndex, hasTrailingBreak: breakIndex >= 0 };
}

function transformCurrentLine(action) {
  const editor = $("#editor");
  if (!editor || !state.activeTab || state.demo) return;
  const { start, end, hasTrailingBreak } = currentLineBounds(editor);
  const line = editor.value.slice(start, end);
  if (action === "move-line-up" && start > 0) {
    const previousEnd = start - 1;
    const previousStart = editor.value.lastIndexOf("\n", Math.max(0, previousEnd - 1)) + 1;
    const previous = editor.value.slice(previousStart, previousEnd);
    editor.setRangeText(`${line}\n${previous}`, previousStart, end, "select");
  } else if (action === "move-line-down" && hasTrailingBreak) {
    const nextStart = end + 1;
    const nextBreak = editor.value.indexOf("\n", nextStart);
    const nextEnd = nextBreak < 0 ? editor.value.length : nextBreak;
    const next = editor.value.slice(nextStart, nextEnd);
    editor.setRangeText(`${next}\n${line}`, start, nextEnd, "select");
  } else if (action === "copy-line-up") {
    editor.setRangeText(`${line}\n`, start, start, "end");
  } else if (action === "copy-line-down") {
    editor.setRangeText(`${hasTrailingBreak ? "\n" : "\n"}${line}`, end, end, "end");
  } else {
    return;
  }
  editor.dispatchEvent(new Event("input", { bubbles: true }));
  editor.focus();
}

function goToLine() {
  const editor = $("#editor");
  if (!editor || !state.activeTab) return;
  const requested = Number(window.prompt("Go to line:", "1"));
  if (!Number.isInteger(requested) || requested < 1) return;
  const lines = editor.value.split("\n");
  const line = Math.min(requested, lines.length);
  const offset = lines.slice(0, line - 1).reduce((total, value) => total + value.length + 1, 0);
  editor.focus();
  editor.setSelectionRange(offset, offset + lines[line - 1].length);
}

function openFindBar(replace = false) {
  if (!state.activeTab) return toast("Find", "Open a file first.");
  state.findReplaceOpen = replace;
  $("#editor-findbar")?.classList.remove("hidden");
  $("#editor-replace-row")?.classList.toggle("hidden", !replace);
  const editor = $("#editor");
  const input = $("#editor-find-input");
  const selected = editor?.value.slice(editor.selectionStart, editor.selectionEnd) || "";
  if (selected && !selected.includes("\n")) input.value = selected;
  updateFindMatches();
  input?.focus();
  input?.select();
}

function closeFindBar() {
  $("#editor-findbar")?.classList.add("hidden");
  $("#editor")?.focus();
}

function updateFindMatches(selectCurrent = false) {
  const editor = $("#editor"), input = $("#editor-find-input");
  if (!editor || !input) return;
  const query = input.value;
  state.findMatches = [];
  if (query) {
    const haystack = state.findCaseSensitive ? editor.value : editor.value.toLowerCase();
    const needle = state.findCaseSensitive ? query : query.toLowerCase();
    for (let index = 0; index <= haystack.length - needle.length;) {
      const found = haystack.indexOf(needle, index);
      if (found < 0) break;
      state.findMatches.push(found);
      index = found + Math.max(1, needle.length);
    }
  }
  state.findIndex = state.findMatches.length
    ? Math.max(0, Math.min(state.findIndex, state.findMatches.length - 1))
    : -1;
  $("#editor-find-count").textContent = state.findMatches.length ? `${state.findIndex + 1}/${state.findMatches.length}` : "0/0";
  if (selectCurrent && state.findIndex >= 0) {
    const index = state.findMatches[state.findIndex];
    editor.focus();
    editor.setSelectionRange(index, index + query.length);
  }
}

function stepFind(direction) {
  updateFindMatches();
  if (!state.findMatches.length) return;
  state.findIndex = (state.findIndex + direction + state.findMatches.length) % state.findMatches.length;
  updateFindMatches(true);
}

function replaceFind(all = false) {
  const editor = $("#editor"), query = $("#editor-find-input")?.value || "", replacement = $("#editor-replace-input")?.value || "";
  if (!editor || !query || !state.findMatches.length) return;
  if (all) {
    const flags = state.findCaseSensitive ? "g" : "gi";
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    editor.value = editor.value.replace(new RegExp(escaped, flags), replacement);
  } else {
    const index = state.findMatches[Math.max(0, state.findIndex)];
    editor.setRangeText(replacement, index, index + query.length, "end");
  }
  editor.dispatchEvent(new Event("input", { bubbles: true }));
  updateFindMatches(true);
}

function findInEditor() {
  openFindBar(false);
}

function cycleOpenFile(direction) {
  if (state.openTabs.length < 2) return;
  const index = Math.max(0, state.openTabs.findIndex((tab) => tab.tab_id === state.activeTab?.tab_id));
  activateEditorTab(state.openTabs[(index + direction + state.openTabs.length) % state.openTabs.length]);
}

function setWorkspaceZoom(value) {
  state.workspaceZoom = Math.max(.85, Math.min(1.2, value));
  $("#workspace-view")?.style.setProperty("--workspace-ui-scale", String(state.workspaceZoom));
  $("#workspace-view")?.style.setProperty("zoom", String(state.workspaceZoom));
}

function openProjectSearch() {
  if (state.currentView === "welcome" && !state.rootPath) return toast("Search Project", "Open a project folder to use project search.", true);
  if (state.currentView !== "workspace") showWorkspaceView();
  const modal = $("#project-search-panel");
  state.searchPreviousFocus = document.activeElement;
  state.searchOpen = true;
  modal?.classList.remove("hidden");
  modal?.classList.add("open");
  modal?.setAttribute("aria-hidden", "false");
  const available = Boolean(state.rootPath && !state.demo);
  $("#project-search-run").disabled = !available;
  if (!available) {
    $("#project-search-status").textContent = "Open a project folder to use project search.";
    $("#project-search-results").innerHTML = `<p class="project-search-empty">Open a project folder to use project search.</p>`;
  } else if (!state.projectSearch.files.length && !$("#project-search-query")?.value) {
    $("#project-search-status").textContent = "Type a term to search this project.";
    $("#project-search-results").innerHTML = `<p class="project-search-empty">Type a term to search this project.</p>`;
  }
  requestAnimationFrame(() => $("#project-search-query")?.focus());
}

function closeProjectSearch() {
  const modal = $("#project-search-panel");
  state.searchOpen = false;
  modal?.classList.remove("open");
  modal?.classList.add("hidden");
  modal?.setAttribute("aria-hidden", "true");
  const previousFocus = state.searchPreviousFocus;
  state.searchPreviousFocus = null;
  if (previousFocus?.isConnected && !previousFocus.closest(".hidden")) previousFocus.focus();
  else if (state.currentView === "workspace") $("#editor")?.focus();
}

async function runProjectSearch() {
  const query = $("#project-search-query")?.value || "";
  if (!query.trim() || state.projectSearch.running) return;
  state.projectSearch.running = true;
  $("#project-search-status").textContent = "Searching project…";
  $("#project-search-run").disabled = true;
  const result = await callBridge("search_project", state.rootPath, query, {
    case_sensitive: $("#project-search-case")?.getAttribute("aria-pressed") === "true",
    whole_word: $("#project-search-word")?.getAttribute("aria-pressed") === "true",
    regex: $("#project-search-regex")?.getAttribute("aria-pressed") === "true",
    include_patterns: $("#project-search-include")?.value || "",
    exclude_patterns: $("#project-search-exclude")?.value || "",
    max_results: 1000,
  });
  state.projectSearch.running = false;
  $("#project-search-run").disabled = false;
  if (!result?.ok) {
    $("#project-search-status").textContent = friendlyError(result?.error, "Search failed.");
    $("#project-search-results")?.replaceChildren();
    return;
  }
  state.projectSearch.files = result.files || [];
  state.projectSearch.flat = state.projectSearch.files.flatMap((file) => file.matches.map((match) => ({ ...match, path: file.path, relative_path: file.relative_path })));
  state.projectSearch.index = state.projectSearch.flat.length ? 0 : -1;
  $("#project-search-status").textContent = `${result.total_matches} match${result.total_matches === 1 ? "" : "es"}${result.truncated ? " · Results truncated" : ""}`;
  renderProjectSearchResults(query);
}

function renderProjectSearchResults(query) {
  const target = $("#project-search-results");
  if (!target) return;
  target.replaceChildren();
  if (!state.projectSearch.files.length) {
    target.innerHTML = `<p class="project-search-empty">No matches found.</p>`;
    return;
  }
  state.projectSearch.files.forEach((file) => {
    const group = document.createElement("section");
    group.className = "search-file-group";
    const header = document.createElement("button");
    header.type = "button";
    header.className = "search-file-header";
    header.textContent = `${file.relative_path} (${file.matches.length})`;
    const rows = document.createElement("div");
    header.addEventListener("click", () => rows.classList.toggle("hidden"));
    group.append(header, rows);
    file.matches.forEach((match) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "search-result-row";
      row.dataset.path = file.path;
      row.dataset.line = String(match.line);
      row.dataset.column = String(match.column);
      const before = escapeHtml(match.text.slice(0, match.match_start));
      const found = escapeHtml(match.text.slice(match.match_start, match.match_end));
      const after = escapeHtml(match.text.slice(match.match_end));
      row.innerHTML = `<span>${match.line}:${match.column}</span><span>${before}<mark>${found}</mark>${after}</span>`;
      row.addEventListener("click", () => openSearchLocation(file.path, match.line, match.column, Math.max(1, match.match_end - match.match_start)));
      rows.appendChild(row);
    });
    target.appendChild(group);
  });
}

async function openSearchLocation(path, line, column = 1, length = 1) {
  const result = await callBridge("open_file", path);
  if (!handleOpenFileResult(result, path)) return;
  showWorkspaceView();
  const editor = $("#editor");
  const lines = editor.value.split("\n");
  const safeLine = Math.max(1, Math.min(Number(line) || 1, lines.length));
  const offset = lines.slice(0, safeLine - 1).reduce((total, value) => total + value.length + 1, 0) + Math.max(0, Number(column) - 1);
  editor.focus();
  editor.setSelectionRange(offset, Math.min(editor.value.length, offset + length));
  const lineHeight = Number.parseFloat(getComputedStyle(editor).lineHeight) || 20;
  editor.scrollTop = Math.max(0, (safeLine - 4) * lineHeight);
  syncEditorScroll();
}

function stepProjectSearch(direction) {
  const results = state.projectSearch.flat;
  if (!results.length) return;
  state.projectSearch.index = (state.projectSearch.index + direction + results.length) % results.length;
  const match = results[state.projectSearch.index];
  openSearchLocation(match.path, match.line, match.column, Math.max(1, match.match_end - match.match_start));
}

function renderProblems() {
  const target = $("#problems-panel");
  if (!target) return;
  target.replaceChildren();
  const grouped = new Map();
  state.problems.forEach((problem) => {
    const key = problem.path || "Application";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(problem);
  });
  if (!state.problems.length) target.innerHTML = `<div class="orbit-empty"><strong>No problems detected</strong></div>`;
  grouped.forEach((problems, path) => {
    const group = document.createElement("section");
    group.className = "problem-group";
    group.innerHTML = `<strong>${escapeHtml(basename(path))}</strong>`;
    problems.forEach((problem) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `problem-row ${problem.severity}`;
      row.innerHTML = `<i>●</i><span>${escapeHtml(problem.message)}</span><small>Line ${problem.line || 1} · ${escapeHtml(problem.source || "NexCore")}</small>`;
      row.addEventListener("click", () => problem.path && openSearchLocation(problem.path, problem.line, problem.column));
      group.appendChild(row);
    });
    target.appendChild(group);
  });
  const errors = state.problems.filter((item) => item.severity === "error").length;
  const warnings = state.problems.filter((item) => item.severity === "warning").length;
  const infos = state.problems.filter((item) => item.severity === "info").length;
  $("#workspace-problems").textContent = `${errors} Errors · ${warnings} Warnings · ${infos} Infos`;
  $("#problems-tab-count").textContent = String(state.problems.length);
}

async function analyzeActivePython() {
  if (!state.activeTab?.path?.toLowerCase().endsWith(".py") || state.demo) return true;
  if (state.modified && !(await saveActiveFile())) return false;
  const result = await callBridge("analyze_python_file", state.activeTab.path);
  state.problems = state.problems.filter((item) => !samePath(item.path || "", state.activeTab.path));
  if (result?.ok) state.problems.push(...(result.problems || []));
  else state.problems.push({ severity: "error", path: state.activeTab.path, line: 1, column: 1, message: friendlyError(result?.error), source: "NexCore" });
  renderProblems();
  if (result?.problems?.length) {
    showTerminalView("problems");
    return false;
  }
  return true;
}

function showTerminalView(name) {
  $$("[data-orbital-terminal-tab]").forEach((button) => button.classList.toggle("active", button.dataset.orbitalTerminalTab === name));
  $("#problems-panel")?.classList.toggle("hidden", name !== "problems");
  $("#terminal-output")?.classList.toggle("hidden", name === "problems");
  $("#command-form")?.classList.toggle("hidden", name !== "terminal");
  showTerminal();
}

async function refreshGitStatus() {
  if (!state.rootPath || state.demo || state.settings?.git?.show_indicators === false) {
    state.git = { is_repo: false, branch: "", files: {} };
    renderExplorer();
    updateGitStatusBar();
    return;
  }
  const result = await callBridge("get_git_status", state.rootPath);
  state.git = result?.ok ? result : { is_repo: false, branch: "", files: {} };
  renderExplorer();
  updateGitStatusBar();
}

function updateGitStatusBar() {
  let node = $("#workspace-git");
  if (!node) {
    node = document.createElement("button");
    node.id = "workspace-git";
    node.type = "button";
    node.className = "status-git";
    $(".workspace-status-left")?.appendChild(node);
  }
  const changed = Object.keys(state.git.files || {}).length;
  node.textContent = state.git.is_repo ? `${state.git.branch || "HEAD"}${changed ? "*" : ""}` : "";
  node.title = state.git.is_repo ? `${changed} changed file${changed === 1 ? "" : "s"}` : "Not a Git repository";
  node.classList.toggle("hidden", !state.git.is_repo);
  node.onclick = () => toast("Git Status", state.git.is_repo ? `${state.git.branch || "Detached HEAD"} · ${changed} changed file${changed === 1 ? "" : "s"}` : "Not a Git repository");
}

const SHORTCUTS = [
  ["Files","New File","Ctrl+N"],["Files","New Folder","Ctrl+Shift+N"],["Files","Open File","Ctrl+O"],["Files","Open Folder","Ctrl+Shift+O"],["Files","Save","Ctrl+S"],["Files","Save As","Ctrl+Shift+S"],
  ["Search","Quick Open / Commands","Ctrl+P"],["Search","Search Project","Ctrl+Shift+F"],["Editor","Find","Ctrl+F"],["Editor","Replace","Ctrl+H"],["Editor","Next File","Ctrl+Tab"],["Editor","Previous File","Ctrl+Shift+Tab"],["Editor","Close File","Ctrl+W"],
  ["Terminal","Toggle Terminal","Ctrl+J"],["AI","Toggle AI","Ctrl+Shift+A"],["General","Settings","Ctrl+,"],["Terminal","Run","F5"],["Terminal","Stop","Shift+F5"],["Workspace","Home","Ctrl+Shift+H"],["General","Close Overlay","Escape"],
];

function openShortcuts() {
  $("#shortcuts-modal")?.classList.remove("hidden");
  renderShortcuts();
  $("#shortcuts-filter")?.focus();
}

function renderShortcuts() {
  const query = ($("#shortcuts-filter")?.value || "").toLowerCase();
  const target = $("#shortcuts-list");
  target.replaceChildren();
  SHORTCUTS.filter((item) => !query || item.join(" ").toLowerCase().includes(query)).forEach(([category, command, shortcut]) => {
    const row = document.createElement("div");
    row.className = "shortcut-row";
    row.innerHTML = `<small>${category}</small><span>${command}</span><kbd>${shortcut}</kbd>`;
    target.appendChild(row);
  });
}

async function openSettings() {
  const result = await callBridge("get_settings");
  if (!result?.ok) return toast("Settings unavailable", friendlyError(result?.error), true);
  state.settings = result.settings;
  populateSettingsForm();
  $("#settings-modal")?.classList.remove("hidden");
}

function populateSettingsForm() {
  const settings = state.settings || {};
  $("#setting-restore").value = settings.workspace?.restore_on_startup || "never";
  $("#setting-autosave").value = settings.editor?.autosave_mode || "off";
  $("#setting-autosave-delay").value = String(settings.editor?.autosave_delay || 1000);
  $("#setting-font-size").value = settings.editor?.font_size || 14;
  $("#setting-line-height").value = settings.editor?.line_height || 1.55;
  $("#setting-tab-size").value = settings.editor?.tab_size || 4;
  $("#setting-current-line").checked = settings.editor?.current_line !== false;
  $("#setting-indent-guides").checked = settings.editor?.indent_guides !== false;
  $("#setting-git").checked = settings.git?.show_indicators !== false;
  $("#setting-reduced-motion").checked = Boolean(settings.appearance?.reduced_motion);
}

function renderRecoveryRecords() {
  const target = $("#recovery-list");
  if (!target) return;
  target.replaceChildren();
  (state.recoveryRecords || []).forEach((record) => {
    const row = document.createElement("div");
    row.className = "recovery-item";
    row.innerHTML = `<div><strong>${escapeHtml(record.filename || "Untitled")}</strong><small>${escapeHtml(record.project_root || "No project")} · ${new Date((record.timestamp || 0) * 1000).toLocaleString()}${record.disk_changed ? " · Disk file changed" : ""}</small></div><div><button type="button" data-preview-recovery="${record.id}">Preview</button></div>`;
    target.appendChild(row);
  });
  $$("[data-preview-recovery]").forEach((button) => button.addEventListener("click", async () => {
    const result = await callBridge("restore_recovery_snapshot", button.dataset.previewRecovery);
    if (result?.ok) {
      const preview = $("#recovery-preview");
      preview.textContent = result.record.content || "";
      preview.classList.remove("hidden");
    }
  }));
}

function applySettings(settings) {
  state.settings = settings;
  state.autosave.mode = settings.editor?.autosave_mode || "off";
  state.autosave.delay = Number(settings.editor?.autosave_delay) || 1000;
  document.documentElement.style.setProperty("--editor-font-size", `${settings.editor?.font_size || 14}px`);
  document.documentElement.style.setProperty("--editor-line-height", String(settings.editor?.line_height || 1.55));
  $("#workspace-view")?.classList.toggle("hide-indent-guides", settings.editor?.indent_guides === false);
  document.body.classList.toggle("reduce-motion", Boolean(settings.appearance?.reduced_motion));
}

function showEditorContextMenu(event) {
  event.preventDefault();
  const selected = Boolean($("#editor")?.value.slice($("#editor").selectionStart, $("#editor").selectionEnd));
  const menu = $("#editor-context-menu");
  const items = [
    ["Undo","edit-undo","Ctrl Z"],["Redo","edit-redo","Ctrl Y"],null,
    ["Cut","edit-cut","Ctrl X",!selected],["Copy","edit-copy","Ctrl C",!selected],["Paste","edit-paste","Ctrl V"],["Select All","select-all","Ctrl A"],null,
    ["Find","editor-find","Ctrl F"],["Replace","editor-replace","Ctrl H"],["Run File","run-file","F5"],null,
    ["Explain Selection","context-explain","",!selected],["Refactor Selection","context-refactor","",!selected],["Add Comments","context-comments","",!selected],["Format Document","format-document"],["Copy File Path","copy-file-path"],["Reveal in Orbital Navigation","reveal-file"],
  ];
  menu.replaceChildren();
  items.forEach((item) => {
    if (!item) {
      const separator = document.createElement("span"); separator.className = "menu-separator"; menu.appendChild(separator); return;
    }
    const button = document.createElement("button");
    button.type = "button"; button.dataset.contextAction = item[1]; button.disabled = Boolean(item[3]);
    button.innerHTML = `<span>${item[0]}</span>${item[2] ? `<kbd>${item[2]}</kbd>` : ""}`;
    menu.appendChild(button);
  });
  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(event.clientX, window.innerWidth - rect.width - 8)}px`;
  menu.style.top = `${Math.min(event.clientY, window.innerHeight - rect.height - 50)}px`;
  window.setTimeout(() => menu.querySelector("button:not(:disabled)")?.focus(), 0);
}

async function checkExternalFileState() {
  if (state.pendingExternalChange || !state.bridgeReady || !state.activeTab?.path || state.demo || !state.rootPath || !state.activeTab.path.toLowerCase().startsWith(state.rootPath.toLowerCase())) return;
  const result = await callBridge("inspect_file", state.activeTab.path, state.settings?.files?.max_file_size || 2_000_000);
  if (!result?.ok) {
    state.pendingExternalChange = { path: state.activeTab.path, deleted: true };
    $("#external-change-title").textContent = "File deleted on disk";
    $("#external-change-message").textContent = "The editor buffer is preserved. Recreate it with Save As or close the tab.";
    $("#external-change-modal")?.classList.remove("hidden");
    return;
  }
  const previous = state.fileVersions.get(state.activeTab.path);
  if (previous && previous !== result.mtime) {
    state.pendingExternalChange = { path: state.activeTab.path, deleted: false, mtime: result.mtime };
    $("#external-change-title").textContent = "This file has changed outside NexCore.";
    $("#external-change-message").textContent = state.modified ? "Your modified buffer will not be reloaded automatically." : "Reload the file or keep the current editor version.";
    $("#external-change-modal")?.classList.remove("hidden");
  }
}

function focusSearch() {
  const input = state.currentView === "workspace" ? $("#workspace-command-search") : $("#command-search");
  input?.focus();
  input?.select();
  state.commandIndex = 0;
  renderCommandResults();
}

async function openFile() {
  setApplicationStatus("Opening file…");
  const result = await callBridge("open_file", "");
  if (!result || result.cancelled) {
    setApplicationStatus("Ready");
    return false;
  }
  if (!handleOpenFileResult(result)) {
    setApplicationStatus(result?.cancelled ? "Ready" : "Error", result?.cancelled ? "" : "error");
    return false;
  }
  if (state.currentView === "welcome") showWorkspaceView(true);
  else $("#editor")?.focus();
  setApplicationStatus("Ready");
  return true;
}

async function openFolder() {
  setApplicationStatus("Loading folder…");
  const result = await callBridge("choose_directory");
  if (!result || result.cancelled) {
    setApplicationStatus("Ready");
    return false;
  }
  if (!result.ok || !isValidFolderResult(result)) {
    setApplicationStatus("Error", "error");
    toast("Could not open folder", friendlyError(result.error, "Invalid folder response."), true);
    return false;
  }
  applyFolderResult(result);
  addRecentItem(result.root.path, "folder");
  if (state.currentView === "welcome") showWorkspaceView();
  setApplicationStatus("Ready");
  return true;
}

function handleAction(action) {
  closeMenus();
  const actions = {
    home: showWelcomeView,
    "new-file": newFileFromWelcome,
    "new-folder": () => openCreationModal("folder"),
    "open-file": () => state.currentView === "welcome" ? openFileFromWelcome() : openFile(),
    "open-folder": () => state.currentView === "welcome" ? openFolderFromWelcome() : requestOpenFolder(),
    "open-recent": focusRecentSection,
    "enter-workspace": enterOrbitalWorkspace,
    "run-file": async () => {
      if (!state.activeTab || state.demo) {
        toast("Run Active File", "Open a saved Python file before running it.", true);
        return;
      }
      if (state.currentView === "welcome") showWorkspaceView();
      await runActiveFile();
    },
    "stop-run": async () => {
      await callBridge("stop_run");
      state.runActive = false;
      setApplicationStatus("Completed");
      updateMenuAvailability();
    },
    "show-terminal": showTerminal,
    "clear-terminal": clearTerminal,
    "focus-terminal": () => {
      showTerminal();
      window.setTimeout(() => $("#command-input")?.focus(), 0);
    },
    "return-home": returnToWelcome,
    "exit-app": () => requestUnsavedProtection("exit-app") || callBridge("window_close"),
    "focus-search": focusSearch,
    "focus-editor": () => {
      if (!state.activeTab) return toast("Active Editor", "Open or create a file first.");
      showWorkspaceView(true);
    },
    "edit-undo": () => performEditorAction("edit-undo"),
    "edit-redo": () => performEditorAction("edit-redo"),
    "edit-cut": () => performEditorAction("edit-cut"),
    "edit-copy": () => performEditorAction("edit-copy"),
    "edit-paste": () => performEditorAction("edit-paste"),
    "select-all": () => performEditorAction("select-all"),
    "select-line": () => performEditorAction("select-line"),
    "move-line-up": () => transformCurrentLine("move-line-up"),
    "move-line-down": () => transformCurrentLine("move-line-down"),
    "copy-line-up": () => transformCurrentLine("copy-line-up"),
    "copy-line-down": () => transformCurrentLine("copy-line-down"),
    "editor-find": findInEditor,
    "editor-replace": () => openFindBar(true),
    "go-line": goToLine,
    "go-back": leaveOrbitalFolder,
    "go-parent": leaveOrbitalFolder,
    "project-root": reloadProjectRoot,
    "next-file": () => cycleOpenFile(1),
    "previous-file": () => cycleOpenFile(-1),
    "python-development": async () => state.activeTab ? showWorkspaceView(true) : newFileFromWelcome(),
    "toggle-settings": () => $("#settings-panel")?.classList.toggle("hidden"),
    "show-about": () => $("#about-panel")?.classList.toggle("hidden"),
    "save-file": saveActiveFile,
    "save-as": saveActiveFileAs,
    "toggle-explorer": () => {
      if (state.currentView === "welcome") enterOrbitalWorkspace();
      toggleExplorer();
    },
    "show-explorer": () => {
      if (!state.rootPath && !state.demo) {
        if (state.currentView === "welcome") openFolderFromWelcome();
        else openFolder();
      } else {
        showWorkspaceView();
        toggleExplorer(false);
        $("#orbital-navigation")?.focus();
      }
    },
    "toggle-ai": () => {
      if (state.currentView === "welcome") enterOrbitalWorkspace();
      toggleAI();
    },
    "toggle-terminal": () => {
      if (state.currentView === "welcome") showTerminal();
      else toggleTerminal();
    },
    "toggle-statusbar": () => $("#workspace-view")?.classList.toggle("statusbar-hidden"),
    "toggle-minimap": () => $("#workspace-view")?.classList.toggle("minimap-hidden"),
    "zoom-in": () => setWorkspaceZoom(state.workspaceZoom + .05),
    "zoom-out": () => setWorkspaceZoom(state.workspaceZoom - .05),
    "zoom-reset": () => setWorkspaceZoom(1),
    "show-shortcuts": openShortcuts,
    "search-project": openProjectSearch,
    "open-settings": openSettings,
    "source-control": () => toast("Source Control", "Source control UI is not configured in this build."),
    "extensions-info": () => toast("Extensions", "Extension management is not configured in this build."),
    "user-info": () => toast("NexCore Profile", "Local desktop profile"),
    "ai-unavailable": () => toast("NexCore AI", "NexCore AI service is not configured yet.", true),
    "tab-more": () => toast("Open tabs", `${state.openTabs.length} file${state.openTabs.length === 1 ? "" : "s"} open.`),
  };
  actions[action]?.();
}

function maximizeWindow() {
  return callBridge("window_toggle_maximize", window.screen.availWidth, window.screen.availHeight, window.screen.availLeft, window.screen.availTop);
}

function hideToolbarTooltip() {
  const tooltip = $("#nexcore-toolbar-tooltip");
  if (!tooltip) return;
  tooltip.classList.add("hidden");
  tooltip.textContent = "";
}

function positionToolbarTooltip(button, tooltip) {
  const buttonRect = button.getBoundingClientRect();
  const tooltipRect = tooltip.getBoundingClientRect();
  const gap = 10;
  let left = buttonRect.left + buttonRect.width / 2 - tooltipRect.width / 2;
  let top = buttonRect.top - tooltipRect.height - gap;
  if (top < 8) top = buttonRect.bottom + gap;
  left = Math.max(8, Math.min(left, window.innerWidth - tooltipRect.width - 8));
  top = Math.min(top, window.innerHeight - tooltipRect.height - 8);
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
}

function showToolbarTooltip(button) {
  const tooltip = $("#nexcore-toolbar-tooltip");
  const label = button?.dataset.tooltip;
  if (!tooltip || !label) return;
  tooltip.textContent = label;
  tooltip.classList.remove("hidden");
  positionToolbarTooltip(button, tooltip);
}

function wireEvents() {
  document.addEventListener("click", (event) => {
    const actionNode = event.target.closest("[data-action]");
    const menuTrigger = event.target.closest("[data-menu]");
    if (menuTrigger) {
      event.stopPropagation();
      toggleMenu(menuTrigger.dataset.menu, menuTrigger);
      return;
    }
    if (actionNode) {
      event.stopPropagation();
      handleAction(actionNode.dataset.action);
      return;
    }
    if (!event.target.closest(".glass-menu") && !event.target.closest(".command-search-wrap")) {
      closeMenus();
      closeCommandResults();
    }
    if (!event.target.closest("#open-files-menu") && !event.target.closest("#active-file-button")) closeOpenFilesMenu();
    if (!event.target.closest("#editor-context-menu")) $("#editor-context-menu")?.classList.add("hidden");
  });

  $("#active-file-button")?.addEventListener("click", () => {
    const menu = $("#open-files-menu");
    const open = menu?.classList.contains("hidden");
    menu?.classList.toggle("hidden", !open);
    $("#active-file-button")?.setAttribute("aria-expanded", String(open));
    if (open) {
      renderTabs();
      window.setTimeout(() => $("#open-files-menu .open-files-filter")?.focus(), 0);
    }
  });
  $("#open-files-menu")?.addEventListener("keydown", (event) => {
    const items = [...$$("#open-files-menu .open-file-row")].filter((item) => item.tagName === "BUTTON");
    const index = items.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      items[(Math.max(-1, index) + direction + items.length) % items.length]?.focus();
    } else if (event.key === "Enter" && document.activeElement?.classList.contains("open-file-row")) {
      event.preventDefault();
      document.activeElement.click();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeOpenFilesMenu();
      $("#active-file-button")?.focus();
    }
  });
  $("#project-root-node")?.addEventListener("click", async () => {
    if (!state.rootPath && !state.demo) return openFolder();
    if (state.demo) {
      state.currentPath = "demo";
      state.orbitHistory = [];
      return renderExplorer();
    }
    await reloadProjectRoot();
  });
  $("#orbit-parent")?.addEventListener("click", leaveOrbitalFolder);
  $$(".orbital-file-action[data-tooltip]").forEach((button) => {
    button.addEventListener("mouseenter", () => showToolbarTooltip(button));
    button.addEventListener("mouseleave", hideToolbarTooltip);
    button.addEventListener("focus", () => showToolbarTooltip(button));
    button.addEventListener("blur", hideToolbarTooltip);
  });
  window.addEventListener("resize", hideToolbarTooltip);
  window.addEventListener("scroll", hideToolbarTooltip, true);
  $$("[data-file-action]").forEach((button) => button.addEventListener("click", () => {
    if (state.fileActionBusy) return;
    const actions = {
      "new-file": () => openCreationModal("file"),
      "new-folder": () => openCreationModal("folder"),
      "open-file": openFile,
      "open-folder": requestOpenFolder,
      refresh: () => refreshCurrentFolder(),
      root: reloadProjectRoot,
      filter: () => {
        const panel = $("#orbital-filter");
        const open = panel?.classList.contains("hidden");
        panel?.classList.toggle("hidden", !open);
        $("#orbital-navigation")?.classList.toggle("filter-open", open);
        button.setAttribute("aria-expanded", String(open));
        if (open) window.setTimeout(() => $("#orbital-filter-input")?.focus(), 0);
      },
    };
    actions[button.dataset.fileAction]?.();
  }));
  $("#orbital-filter-input")?.addEventListener("input", (event) => {
    state.fileFilter = event.target.value;
    state.orbitOffset = 0;
    renderExplorer();
    persistWorkspaceSettings();
  });
  $("#orbital-filter-input")?.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    state.fileFilter = "";
    event.target.value = "";
    renderExplorer();
  });
  $("#orbital-filter-clear")?.addEventListener("click", () => {
    state.fileFilter = "";
    $("#orbital-filter-input").value = "";
    renderExplorer();
    $("#orbital-filter-input")?.focus();
  });

  const orbit = $("#orbital-navigation");
  orbit?.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (!state.nodes.length) return;
    state.orbitOffset = Math.max(0, Math.min(state.nodes.length - 1, state.orbitOffset + Math.sign(event.deltaY)));
    renderExplorer();
  }, { passive: false });
  orbit?.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".project-orbit-node,.orbit-parent,.orbital-file-toolbar")) return;
    state.orbitPointerDown = true;
    state.orbitDragging = false;
    state.orbitDragMoved = false;
    state.orbitDragStartY = event.clientY;
    state.orbitDragStartOffset = state.orbitOffset;
  });
  orbit?.addEventListener("pointermove", (event) => {
    if (!state.orbitPointerDown || !state.nodes.length) return;
    const delta = state.orbitDragStartY - event.clientY;
    if (!state.orbitDragging && Math.abs(delta) < 6) return;
    if (!state.orbitDragging) {
      state.orbitDragging = true;
      state.orbitDragMoved = true;
      try { orbit.setPointerCapture(event.pointerId); } catch {}
    }
    state.orbitOffset = Math.max(0, Math.min(state.nodes.length - 1, state.orbitDragStartOffset + delta / 105));
    renderExplorer();
  });
  orbit?.addEventListener("pointerup", (event) => {
    if (!state.orbitPointerDown) return;
    state.orbitPointerDown = false;
    if (!state.orbitDragging) {
      state.orbitDragMoved = false;
      return;
    }
    state.orbitDragging = false;
    state.orbitOffset = Math.round(state.orbitOffset);
    renderExplorer();
    try { orbit.releasePointerCapture(event.pointerId); } catch {}
    window.setTimeout(() => { state.orbitDragMoved = false; }, 80);
  });
  orbit?.addEventListener("pointercancel", () => {
    state.orbitPointerDown = false;
    state.orbitDragging = false;
    state.orbitDragMoved = false;
  });
  orbit?.addEventListener("keydown", (event) => {
    if (!state.nodes.length && event.key !== "Backspace") return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      state.orbitOffset = Math.max(0, Math.min(state.nodes.length - 1, Math.round(state.orbitOffset) + direction));
      renderExplorer();
    } else if (event.key === "Enter") {
      event.preventDefault();
      $("#orbital-list .orbit-node.selected")?.click();
    } else if (event.key === "Backspace") {
      event.preventDefault();
      leaveOrbitalFolder();
    }
  });
  $("#run-file")?.addEventListener("click", runActiveFile);
  $("#stop-run")?.addEventListener("click", () => callBridge("stop_run"));
  $("#clear-terminal")?.addEventListener("click", clearTerminal);
  $("#terminal-copy")?.addEventListener("click", async () => {
    const text = $("#terminal-output")?.innerText || "";
    try {
      await navigator.clipboard.writeText(text);
      toast("Terminal output copied", `${text.length} characters`);
    } catch {
      toast("Copy unavailable", "Select the terminal output and use Ctrl+C.", true);
    }
  });
  $("#terminal-expand")?.addEventListener("click", () => {
    state.terminalExpanded = !state.terminalExpanded;
    if (state.terminalExpanded) setTerminalHeight(terminalHeightLimits().max);
    else resetTerminalHeight();
    $("#terminal-expand").textContent = state.terminalExpanded ? "Restore" : "Expand";
  });
  $("#active-file-close")?.addEventListener("click", () => state.activeTab && closeEditorTab(state.activeTab));
  [["#minimize","window_minimize"],["#welcome-minimize","window_minimize"],["#workspace-minimize","window_minimize"],["#welcome-close","window_close"]].forEach(([selector, method]) => $(selector)?.addEventListener("click", () => callBridge(method)));
  ["#close", "#workspace-close"].forEach((selector) => $(selector)?.addEventListener("click", () => {
    if (!requestUnsavedProtection("exit-app")) callBridge("window_close");
  }));
  $("#maximize")?.addEventListener("click", maximizeWindow);
  $("#welcome-maximize")?.addEventListener("click", maximizeWindow);
  $("#workspace-maximize")?.addEventListener("click", maximizeWindow);

  const editor = $("#editor");
  editor?.addEventListener("input", () => { updateHighlighting(); markModified(); updateCaretFeedback(); updateFindMatches(); });
  editor?.addEventListener("scroll", () => { syncEditorScroll(); updateCaretFeedback(); }, { passive: true });
  ["click", "keyup", "select"].forEach((name) => editor?.addEventListener(name, updateCaretFeedback));
  editor?.addEventListener("blur", () => { if (state.autosave.mode === "focus") autosaveActiveFile(); });
  editor?.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    editor.setRangeText("    ", editor.selectionStart, editor.selectionEnd, "end");
    editor.dispatchEvent(new Event("input", { bubbles: true }));
  });

  const splitter = $("#terminal-splitter");
  splitter?.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    splitter.classList.add("dragging");
    splitter.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startHeight = state.terminalHeight;
    const move = (moveEvent) => setTerminalHeight(startHeight + startY - moveEvent.clientY, false);
    const finish = (upEvent) => {
      splitter.classList.remove("dragging");
      splitter.removeEventListener("pointermove", move);
      splitter.removeEventListener("pointerup", finish);
      try { splitter.releasePointerCapture(upEvent.pointerId); } catch {}
      persistWorkspaceSettings();
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", finish);
  });
  splitter?.addEventListener("dblclick", resetTerminalHeight);
  splitter?.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    setTerminalHeight(state.terminalHeight + (event.key === "ArrowUp" ? 12 : -12));
  });

  $("#editor-find-input")?.addEventListener("input", () => { state.findIndex = 0; updateFindMatches(true); });
  $("#editor-find-input")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); stepFind(event.shiftKey ? -1 : 1); }
    else if (event.key === "Escape") { event.preventDefault(); closeFindBar(); }
  });
  $("#editor-find-case")?.addEventListener("click", (event) => {
    state.findCaseSensitive = !state.findCaseSensitive;
    event.currentTarget.setAttribute("aria-pressed", String(state.findCaseSensitive));
    updateFindMatches(true);
  });
  $$("[data-find-action]").forEach((button) => button.addEventListener("click", () => {
    const action = button.dataset.findAction;
    if (action === "previous") stepFind(-1);
    else if (action === "next") stepFind(1);
    else if (action === "replace") replaceFind(false);
    else if (action === "replace-all") replaceFind(true);
    else closeFindBar();
  }));

  $("#command-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#command-input"), command = input.value;
    input.value = "";
    await executeApplicationCommand(command);
  });

  [$("#command-search"), $("#workspace-command-search")].filter(Boolean).forEach((search) => {
    search.addEventListener("focus", renderCommandResults);
    search.addEventListener("input", () => { state.commandIndex = 0; renderCommandResults(); });
    search.addEventListener("keydown", (event) => {
      const commands = filteredCommands();
      if (event.key === "ArrowDown") { event.preventDefault(); state.commandIndex = Math.min(commands.length - 1, state.commandIndex + 1); renderCommandResults(); }
      else if (event.key === "ArrowUp") { event.preventDefault(); state.commandIndex = Math.max(0, state.commandIndex - 1); renderCommandResults(); }
      else if (event.key === "Enter") { event.preventDefault(); executeCommand(commands[state.commandIndex]); }
      else if (event.key === "Escape") { event.preventDefault(); closeCommandResults(); search.blur(); }
    });
  });

  $$(".workspace-topbar .menu-trigger").forEach((trigger) => {
    trigger.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "Enter", " "].includes(event.key)) return;
      event.preventDefault();
      if (trigger.getAttribute("aria-expanded") !== "true") toggleMenu(trigger.dataset.menu, trigger);
      $(`#${trigger.dataset.menu}`)?.querySelector("button:not(:disabled)")?.focus();
    });
  });
  $$(".workspace-topbar .glass-menu").forEach((menu) => {
    menu.addEventListener("keydown", (event) => {
      const items = [...menu.querySelectorAll("button:not(:disabled)")];
      const index = items.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        items[(Math.max(0, index) + direction + items.length) % items.length]?.focus();
      } else if (event.key === "Home") {
        event.preventDefault(); items[0]?.focus();
      } else if (event.key === "End") {
        event.preventDefault(); items.at(-1)?.focus();
      } else if (event.key === "Escape") {
        event.preventDefault();
        const trigger = $(`.workspace-topbar [data-menu="${menu.id}"]`);
        closeMenus();
        trigger?.focus();
      }
    });
  });

  $$(".collapsed-section").forEach((button) => button.addEventListener("click", () => {
    const content = $(`#${button.dataset.section}-content`);
    const open = content?.classList.contains("hidden");
    content?.classList.toggle("hidden", !open);
    const arrow = button.querySelector("span");
    if (arrow) arrow.textContent = open ? "⌄" : "›";
  }));
  $("#ai-toggle")?.addEventListener("click", () => toggleAI());
  $("#ai-close")?.addEventListener("click", () => toggleAI(false));
  $("#ai-new-chat")?.addEventListener("click", () => {
    state.aiMessages = [];
    const conversation = $("#ai-conversation");
    if (conversation) conversation.innerHTML = `<div class="ai-config-message">${state.aiAvailable ? "New conversation ready." : "NexCore AI is not configured yet."}</div>`;
  });
  $("#ai-attach")?.addEventListener("click", () => {
    const context = $("#ai-file-context");
    if (context) context.checked = !context.checked;
  });
  $$("[data-ai-action]").forEach((button) => button.addEventListener("click", () => handleAIQuickAction(button.dataset.aiAction)));
  $$("[data-terminal-tab]").forEach((button) => button.addEventListener("click", () => {
    $$("[data-terminal-tab]").forEach((item) => item.classList.toggle("active", item === button));
    if (button.dataset.terminalTab !== "terminal" && button.dataset.terminalTab !== "output") {
      toast(button.textContent.trim(), "No entries are currently available.");
    }
  }));
  $("#ai-composer")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#ai-input");
    const message = input?.value.trim();
    if (!message) return;
    input.value = "";
    sendAIMessage(message);
  });
  $("#project-search-close")?.addEventListener("click", closeProjectSearch);
  $("#project-search-run")?.addEventListener("click", runProjectSearch);
  $("#project-search-previous")?.addEventListener("click", () => stepProjectSearch(-1));
  $("#project-search-next")?.addEventListener("click", () => stepProjectSearch(1));
  $("#project-search-query")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); runProjectSearch(); }
    else if (event.key === "Escape") { event.preventDefault(); closeProjectSearch(); }
  });
  ["case", "word", "regex"].forEach((name) => $(`#project-search-${name}`)?.addEventListener("click", (event) => {
    event.currentTarget.setAttribute("aria-pressed", String(event.currentTarget.getAttribute("aria-pressed") !== "true"));
  }));
  $("#project-replace-toggle")?.addEventListener("click", () => {
    const input = $("#project-search-replace");
    input?.classList.toggle("hidden");
    $("#project-replace-toggle").textContent = input?.classList.contains("hidden") ? "Show Replace" : "Hide Replace";
  });
  $("#project-search-filters-toggle")?.addEventListener("click", (event) => {
    const filters = $("#project-search-filters");
    const open = filters?.classList.contains("hidden");
    filters?.classList.toggle("hidden", !open);
    event.currentTarget.setAttribute("aria-expanded", String(open));
    if (open) window.setTimeout(() => $("#project-search-include")?.focus(), 0);
  });
  $("#project-search-clear")?.addEventListener("click", () => {
    ["query", "replace", "include", "exclude"].forEach((name) => { const input = $(`#project-search-${name}`); if (input) input.value = ""; });
    state.projectSearch = { files: [], flat: [], index: -1, running: false };
    $("#project-search-results").innerHTML = `<p class="project-search-empty">Type a term to search this project.</p>`;
    $("#project-search-status").textContent = "Type a term to search this project.";
    $("#project-search-query")?.focus();
  });
  $("#project-search-results")?.addEventListener("keydown", (event) => {
    const rows = [...$$("#project-search-results .search-result-row")];
    const index = rows.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      rows[(Math.max(0, index) + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length]?.focus();
    } else if (event.key === "Enter" && document.activeElement?.classList.contains("search-result-row")) {
      event.preventDefault(); document.activeElement.click();
    }
  });
  $$("[data-orbital-terminal-tab]").forEach((button) => button.addEventListener("click", () => showTerminalView(button.dataset.orbitalTerminalTab)));

  $("#editor")?.addEventListener("contextmenu", showEditorContextMenu);
  $("#editor-context-menu")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-context-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.contextAction;
    $("#editor-context-menu").classList.add("hidden");
    if (action.startsWith("context-")) {
      const selected = $("#editor").value.slice($("#editor").selectionStart, $("#editor").selectionEnd);
      if (!state.aiAvailable) return toast("NexCore AI", "NexCore AI is not configured yet.", true);
      const prompts = { "context-explain": "Explain this selected code.", "context-refactor": "Refactor this selected code and return a complete replacement.", "context-comments": "Add useful comments to this selected code and return a complete replacement." };
      $("#ai-selection-context").checked = true;
      return sendAIMessage(prompts[action], selected);
    }
    if (action === "copy-file-path") {
      try { await navigator.clipboard.writeText(state.activeTab?.path || ""); } catch {}
      return;
    }
    if (action === "reveal-file") {
      const index = state.nodes.findIndex((node) => samePath(node.path, state.activePath));
      if (index >= 0) { state.orbitOffset = index; renderExplorer(); $("#orbital-navigation")?.focus(); }
      return;
    }
    if (action === "format-document") return toast("Format Document", "Python formatter is not configured.");
    handleAction(action);
  });
  $("#editor-context-menu")?.addEventListener("keydown", (event) => {
    const items = [...$$("#editor-context-menu button:not(:disabled)")];
    const index = items.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      items[(Math.max(0, index) + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    } else if (event.key === "Escape") {
      event.preventDefault(); $("#editor-context-menu")?.classList.add("hidden"); $("#editor")?.focus();
    }
  });

  $("#settings-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const settings = structuredClone(state.settings || {});
    settings.workspace ||= {};
    settings.workspace.restore_on_startup = $("#setting-restore").value;
    settings.editor.autosave_mode = $("#setting-autosave").value;
    settings.editor.autosave_delay = Number($("#setting-autosave-delay").value);
    settings.editor.font_size = Number($("#setting-font-size").value);
    settings.editor.line_height = Number($("#setting-line-height").value);
    settings.editor.tab_size = Number($("#setting-tab-size").value);
    settings.editor.current_line = $("#setting-current-line").checked;
    settings.editor.indent_guides = $("#setting-indent-guides").checked;
    settings.git.show_indicators = $("#setting-git").checked;
    settings.appearance.reduced_motion = $("#setting-reduced-motion").checked;
    const result = await callBridge("update_settings", settings);
    if (!result?.ok) return toast("Settings could not be saved", friendlyError(result?.error), true);
    applySettings(result.settings);
    $("#settings-modal").classList.add("hidden");
    refreshGitStatus();
    toast("Settings saved", "NexCore preferences were updated.");
  });
  $$("[data-settings-action]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.settingsAction;
    if (action === "close") $("#settings-modal")?.classList.add("hidden");
    else if (action === "export") {
      const result = await callBridge("export_settings");
      if (result?.ok) toast("Settings exported", result.path);
    } else if (action === "import") {
      const result = await callBridge("import_settings");
      if (result?.ok) { state.settings = result.settings; populateSettingsForm(); applySettings(result.settings); toast("Settings imported", "Preferences were validated and applied."); }
      else if (!result?.cancelled) toast("Import failed", friendlyError(result?.error), true);
    } else {
      const result = await callBridge("update_settings", {});
      if (result?.ok) { state.settings = result.settings; populateSettingsForm(); applySettings(result.settings); }
    }
  }));
  $("#shortcuts-filter")?.addEventListener("input", renderShortcuts);
  $$("[data-shortcuts-action=close]").forEach((button) => button.addEventListener("click", () => $("#shortcuts-modal")?.classList.add("hidden")));

  $$("[data-recovery-action]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.recoveryAction;
    if (action === "later") return $("#recovery-modal")?.classList.add("hidden");
    const records = state.recoveryRecords || [];
    for (const record of records) {
      if (action === "discard-all") await callBridge("discard_recovery_snapshot", record.id);
      else {
        const result = await callBridge("restore_recovery_snapshot", record.id);
        if (result?.ok && result.record) {
          const restored = result.record;
          const tabResult = restored.path ? await callBridge("open_file", restored.path) : await callBridge("new_file");
          if (tabResult?.ok && isValidTab(tabResult.tab)) {
            tabResult.tab.content = restored.content;
            tabResult.tab.is_modified = true;
            loadRealTab(tabResult.tab);
            $("#editor").value = restored.content;
            markModified();
          }
        }
      }
    }
    $("#recovery-modal")?.classList.add("hidden");
  }));
  $$("[data-workspace-restore]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.workspaceRestore;
    if (action === "always" || action === "never") {
      const settings = structuredClone(state.settings || {});
      settings.workspace ||= {};
      settings.workspace.restore_on_startup = action;
      const result = await callBridge("update_settings", settings);
      if (result?.ok) applySettings(result.settings);
    }
    const modal = $("#workspace-restore-modal");
    modal?.classList.add("hidden");
    modal?.setAttribute("aria-hidden", "true");
    if (action === "restore" || action === "always") await restorePersistedPaths();
    else showWelcomeView();
  }));
  $$("[data-ai-change]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.aiChange;
    const preview = state.aiChangePreview;
    if (!preview || action === "reject") {
      state.aiChangePreview = null;
      return $("#ai-change-modal")?.classList.add("hidden");
    }
    if (action === "copy") {
      try { await navigator.clipboard.writeText(preview.proposed); toast("Proposed code copied", preview.filename); } catch { toast("Copy unavailable", "Clipboard access failed.", true); }
      return;
    }
    if (action === "save-as") {
      const created = await callBridge("new_file");
      if (created?.ok && isValidTab(created.tab)) {
        loadRealTab(created.tab);
        $("#editor").value = preview.proposed;
        markModified();
        $("#ai-change-modal")?.classList.add("hidden");
        await saveActiveFileAs();
      }
      return;
    }
    if (action === "apply") {
      if (state.activeTab?.tab_id !== preview.tabId || await hashText($("#editor").value) !== preview.originalHash) {
        $("#ai-change-warning").textContent = "The active file changed after this AI request. Regenerate the suggestion before applying.";
        $("#ai-change-warning").classList.add("error");
        return;
      }
      await saveActiveRecovery();
      const editor = $("#editor");
      editor.focus();
      editor.select();
      editor.setRangeText(preview.proposed, 0, editor.value.length, "end");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      $("#ai-change-modal")?.classList.add("hidden");
      state.aiChangePreview = null;
      toast("AI changes applied", "Review and save the modified file.");
    }
  }));
  $$("[data-file-warning]").forEach((button) => button.addEventListener("click", async () => {
    const path = state.pendingLargeFile;
    state.pendingLargeFile = "";
    $("#file-warning-modal")?.classList.add("hidden");
    if (button.dataset.fileWarning === "open" && path) {
      const result = await callBridge("open_file", path, true);
      if (handleOpenFileResult(result, path)) {
        $("#highlight-layer")?.classList.add("hidden");
        toast("Large file mode", "Syntax highlighting is disabled for performance.");
      }
    }
  }));
  $$("[data-external-change]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.externalChange;
    const pending = state.pendingExternalChange;
    $("#external-change-modal")?.classList.add("hidden");
    if (!pending) return;
    if (action === "reload" && !pending.deleted) {
      const result = await callBridge("read_file_snapshot", pending.path);
      if (result?.ok) {
        $("#editor").value = result.content;
        state.modified = false;
        state.activeTab.content = result.content;
        state.activeTab.is_modified = false;
        state.fileVersions.set(pending.path, result.metadata?.mtime);
        updateHighlighting(); renderTabs(); setApplicationStatus("Reloaded");
      }
    } else if (action === "keep") {
      if (pending.mtime) state.fileVersions.set(pending.path, pending.mtime);
    } else if (action === "save-as") await saveActiveFileAs();
    else if (action === "close" && state.activeTab) await closeEditorTab(state.activeTab);
    state.pendingExternalChange = null;
  }));
  $("#creation-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitCreation();
  });
  $$("[data-creation-action=cancel]").forEach((button) => button.addEventListener("click", closeCreationModal));

  $("#reduce-motion-setting")?.addEventListener("change", (event) => {
    document.body.classList.toggle("reduce-motion", event.target.checked);
    try { localStorage.setItem("nexcore.reduceMotion", String(event.target.checked)); } catch {}
  });

  $$("[data-modal-action]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.modalAction;
    if (action === "cancel") {
      state.pendingTabClose = null;
      state.pendingWorkspaceAction = "";
      return closeUnsavedModal();
    }
    if (state.pendingTabClose) {
      const tab = state.pendingTabClose;
      if (action === "save") {
        if (state.activeTab?.tab_id !== tab.tab_id) await activateEditorTab(tab);
        if (!(await saveActiveFile())) return;
      }
      state.pendingTabClose = null;
      closeUnsavedModal();
      await closeEditorTab(tab, true);
      return;
    }
    if (action === "save" && !(await saveAllModified())) return;
    if (action === "discard") await discardAllModified();
    const pendingWorkspaceAction = state.pendingWorkspaceAction;
    closeUnsavedModal();
    await completeProtectedAction(pendingWorkspaceAction);
  }));

  document.addEventListener("keydown", (event) => {
    const key = event.key.toLowerCase();
    const activeModal = [...$$(".modal-backdrop:not(.hidden)")].at(-1);
    if (activeModal && event.key === "Tab") {
      const focusable = [...activeModal.querySelectorAll("button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)")];
      if (focusable.length) {
        const index = focusable.indexOf(document.activeElement);
        const next = event.shiftKey ? (index <= 0 ? focusable.length - 1 : index - 1) : (index >= focusable.length - 1 ? 0 : index + 1);
        event.preventDefault(); focusable[next].focus();
      }
      return;
    }
    if (event.key === "Escape") {
      if (!$("#creation-modal")?.classList.contains("hidden")) closeCreationModal();
      else if (!$("#settings-modal")?.classList.contains("hidden")) $("#settings-modal")?.classList.add("hidden");
      else if (!$("#shortcuts-modal")?.classList.contains("hidden")) $("#shortcuts-modal")?.classList.add("hidden");
      else if (!$("#recovery-modal")?.classList.contains("hidden")) $("#recovery-modal")?.classList.add("hidden");
      else if (!$("#workspace-restore-modal")?.classList.contains("hidden")) $("#workspace-restore-modal")?.classList.add("hidden");
      else if (!$("#file-warning-modal")?.classList.contains("hidden")) $("#file-warning-modal")?.classList.add("hidden");
      else if (!$("#external-change-modal")?.classList.contains("hidden")) $("#external-change-modal")?.classList.add("hidden");
      else if (!$("#ai-change-modal")?.classList.contains("hidden")) $("#ai-change-modal")?.classList.add("hidden");
      else if (state.searchOpen) closeProjectSearch();
      else if (!$("#editor-context-menu")?.classList.contains("hidden")) $("#editor-context-menu")?.classList.add("hidden");
      else if (!$("#editor-findbar")?.classList.contains("hidden")) closeFindBar();
      else if (!$("#open-files-menu")?.classList.contains("hidden")) closeOpenFilesMenu();
      else if ($("#ai-drawer")?.classList.contains("open")) toggleAI(false);
      else if (!$("#unsaved-modal")?.classList.contains("hidden")) closeUnsavedModal();
      else closeOverlays();
      return;
    }
    if (event.target.closest(".modal-backdrop") && ["INPUT", "TEXTAREA"].includes(event.target.tagName)) return;
    if (event.ctrlKey && key === "p") { event.preventDefault(); focusSearch(); return; }
    if (event.ctrlKey && key === ",") { event.preventDefault(); openSettings(); return; }
    if (event.ctrlKey && event.shiftKey && key === "f") { event.preventDefault(); openProjectSearch(); return; }
    if (event.ctrlKey && !event.shiftKey && key === "n") { event.preventDefault(); newFileFromWelcome(); return; }
    if (state.currentView === "welcome") {
      if (event.ctrlKey && event.shiftKey && key === "o") { event.preventDefault(); openFolderFromWelcome(); }
      else if (event.ctrlKey && key === "o") { event.preventDefault(); openFileFromWelcome(); }
      return;
    }
    if (event.ctrlKey && event.shiftKey && key === "h") { event.preventDefault(); returnToWelcome(); }
    else if (event.ctrlKey && event.shiftKey && key === "n") { event.preventDefault(); openCreationModal("folder"); }
    else if (event.ctrlKey && event.shiftKey && key === "o") { event.preventDefault(); openFolder(); }
    else if (event.ctrlKey && event.shiftKey && key === "s") { event.preventDefault(); saveActiveFileAs(); }
    else if (event.ctrlKey && key === "o") { event.preventDefault(); openFile(); }
    else if (event.ctrlKey && key === "s") { event.preventDefault(); saveActiveFile(); }
    else if (event.ctrlKey && key === "f") { event.preventDefault(); openFindBar(false); }
    else if (event.ctrlKey && key === "h") { event.preventDefault(); openFindBar(true); }
    else if (event.ctrlKey && key === "w") { event.preventDefault(); if (state.activeTab) closeEditorTab(state.activeTab); }
    else if (event.ctrlKey && key === "tab") {
      event.preventDefault();
      if (state.openTabs.length) {
        const index = Math.max(0, state.openTabs.findIndex((tab) => tab.tab_id === state.activeTab?.tab_id));
        const direction = event.shiftKey ? -1 : 1;
        activateEditorTab(state.openTabs[(index + direction + state.openTabs.length) % state.openTabs.length]);
      }
    }
    else if (event.ctrlKey && key === "b") { event.preventDefault(); toggleExplorer(); }
    else if (event.ctrlKey && key === "j") { event.preventDefault(); toggleTerminal(); }
    else if (event.ctrlKey && event.shiftKey && key === "a") { event.preventDefault(); toggleAI(); }
    else if (event.shiftKey && event.key === "F5") { event.preventDefault(); callBridge("stop_run"); }
    else if (event.key === "F5") { event.preventDefault(); runActiveFile(); }
  });

  window.addEventListener("resize", () => { updateMinimap(); setTerminalHeight(state.terminalHeight, false); updateCaretFeedback(); }, { passive: true });
  window.addEventListener("blur", () => { if (state.autosave.mode === "window") autosaveActiveFile(); });
}

async function initializeBridge() {
  const bridge = api();
  if (!bridge) {
    setBridgeStatus("connecting");
    return false;
  }
  setBridgeStatus("connected");
  resolveBridgeWaiters(bridge);
  const info = await callBridge("app_info");
  if (!info || typeof info !== "object") {
    setBridgeStatus("error", "Backend connection failed");
    return false;
  }
  state.appInfo = info;
  const aiStatus = await callBridge("ai_status");
  setAIAvailability(aiStatus);
  $("#status-python").textContent = `Python ${info.python_version || "—"}`;
  if ($("#status-python-workspace")) $("#status-python-workspace").textContent = `Python ${info.python_version || "—"}`;
  if ($("#status-branch")) $("#status-branch").textContent = info.git_branch || "No repository";
  if ($("#ai-greeting")) $("#ai-greeting").textContent = info.user_name ? `Hello, ${info.user_name}` : "Hello";
  $("#status-platform").textContent = info.platform || "Desktop";
  if ($("#status-platform-workspace")) $("#status-platform-workspace").textContent = info.platform || "Desktop";
  if ($("#status-app-workspace")) $("#status-app-workspace").textContent = info.app_name || "NexCore Orbital Engine";
  const settingsResult = await callBridge("get_settings");
  if (settingsResult?.ok) applySettings(settingsResult.settings);
  const backendWorkspace = await callBridge("load_workspace_state");
  if (backendWorkspace?.ok && backendWorkspace.state && Object.keys(backendWorkspace.state).length) {
    state.persistedWorkspace = backendWorkspace.state;
  }
  const restoreMode = state.settings?.workspace?.restore_on_startup || "never";
  const savedRoot = state.persistedWorkspace?.rootPath;
  const savedRootState = savedRoot ? await callBridge("validate_path", savedRoot, "directory") : null;
  const hasValidPreviousWorkspace = Boolean(savedRootState?.ok && savedRootState.valid);
  if (restoreMode === "always" && hasValidPreviousWorkspace) {
    await restorePersistedPaths();
  } else if (restoreMode === "ask" && hasValidPreviousWorkspace) {
    const modal = $("#workspace-restore-modal");
    modal?.classList.remove("hidden");
    modal?.setAttribute("aria-hidden", "false");
  } else {
    showWelcomeView();
  }
  const recovery = await callBridge("list_recovery_snapshots");
  if (recovery?.ok && recovery.records?.length) {
    state.recoveryRecords = recovery.records;
    renderRecoveryRecords();
    $("#recovery-modal")?.classList.remove("hidden");
  }
  $("#about-version").textContent = `${info.name || "NexCore"} ${info.version || "0.1.0"} · ${info.shell || "pywebview"}`;
  if (info.startup_file) {
    const result = await callBridge("open_file", info.startup_file);
    if (result?.ok && isValidTab(result.tab)) {
      loadRealTab(result.tab);
      addRecentItem(result.tab.path, "file");
      showWorkspaceView(true);
    } else {
      toast("Startup file unavailable", friendlyError(result?.error), true);
    }
  }
  return true;
}

function initializeInterface() {
  setView("welcome");
  setBridgeStatus("connecting");
  loadRecentItems();
  $("#editor").value = "";
  updateHighlighting();
  clearTerminal();
  updateFolderContext();
  wireEvents();
  restoreWorkspaceSettings();
  updateCaretFeedback();
  try {
    const reduce = localStorage.getItem("nexcore.reduceMotion") === "true";
    $("#reduce-motion-setting").checked = reduce;
    document.body.classList.toggle("reduce-motion", reduce);
  } catch {}
  window.addEventListener("pywebviewready", initializeBridge);
  window.setInterval(checkExternalFileState, 5000);
  window.setInterval(() => {
    if (state.currentView === "workspace" && state.bridgeReady) refreshGitStatus();
  }, 30000);
  window.setInterval(() => {
    if (!api() && state.bridgeReady) setBridgeStatus("error", "NexCore Engine is unavailable");
  }, 4000);
  if (api()) initializeBridge();
  else window.setTimeout(() => {
    if (!api() && state.bridgeStatus === "connecting") {
      setBridgeStatus("error", "Desktop backend unavailable — launch with python main.py");
    }
  }, 3500);
}

initializeInterface();
