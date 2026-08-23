const body = document.body;
const navigationShell = document.querySelector("#navigation-shell");
const sidebar = document.querySelector("#paper-sidebar");
const toggle = document.querySelector(".sidebar-toggle");
const themeToggle = document.querySelector(".theme-toggle");
const themeIcon = themeToggle?.querySelector("[data-theme-icon]");
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
const themeStorageKey = "arxiv-theme";

function storedTheme() {
  try {
    const theme = window.localStorage.getItem(themeStorageKey);
    return theme === "light" || theme === "dark" ? theme : null;
  } catch (error) {
    return null;
  }
}

let followsSystemTheme = storedTheme() === null;

function updateThemeControl(theme) {
  const isDark = theme === "dark";
  if (themeIcon) themeIcon.textContent = isDark ? "☀" : "☾";
  themeToggle?.setAttribute("aria-label", `Switch to ${isDark ? "light" : "dark"} theme`);
  themeToggle?.setAttribute("title", `Switch to ${isDark ? "light" : "dark"} theme`);
  themeToggle?.setAttribute("aria-pressed", String(isDark));
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  updateThemeControl(theme);
  if (!persist) return;
  try {
    window.localStorage.setItem(themeStorageKey, theme);
  } catch (error) {
    // The current page can still switch themes when storage is unavailable.
  }
}

themeToggle?.addEventListener("click", () => {
  const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  followsSystemTheme = false;
  applyTheme(nextTheme, true);
});

themeMedia.addEventListener("change", (event) => {
  if (followsSystemTheme) applyTheme(event.matches ? "dark" : "light");
});

updateThemeControl(document.documentElement.dataset.theme || (themeMedia.matches ? "dark" : "light"));

function setSidebar(open) {
  body.classList.toggle("sidebar-open", open);
  toggle?.setAttribute("aria-expanded", String(open));
}

function setYearExpanded(yearArchive, expanded) {
  if (!yearArchive) return;
  const yearToggle = yearArchive.querySelector(".archive-year-toggle");
  const content = yearArchive.querySelector(".archive-year-content");
  yearArchive.dataset.expanded = String(expanded);
  yearToggle?.setAttribute("aria-expanded", String(expanded));
  content?.setAttribute("aria-hidden", String(!expanded));
}

function revealMonthTab(tab) {
  const tablist = tab?.parentElement;
  if (!tablist) return;
  const tabRect = tab.getBoundingClientRect();
  const tablistRect = tablist.getBoundingClientRect();
  if (tabRect.left < tablistRect.left) {
    tablist.scrollLeft -= tablistRect.left - tabRect.left;
  } else if (tabRect.right > tablistRect.right) {
    tablist.scrollLeft += tabRect.right - tablistRect.right;
  }
}

function selectMonth(tab, { updateHash = false, focus = false } = {}) {
  const targetId = tab?.dataset.monthTarget;
  const yearArchive = tab?.closest("[data-archive-year]");
  if (!targetId || !yearArchive || tab.disabled) return;

  yearArchive.querySelectorAll("[data-month-target]").forEach((candidate) => {
    const selected = candidate === tab;
    candidate.setAttribute("aria-selected", String(selected));
    candidate.tabIndex = selected ? 0 : -1;
  });
  yearArchive.querySelectorAll(".archive-month-panel").forEach((panel) => {
    const active = panel.id === targetId;
    panel.dataset.active = String(active);
    panel.setAttribute("aria-hidden", String(!active));
  });
  setYearExpanded(yearArchive, true);
  revealMonthTab(tab);

  if (updateHash) window.history.replaceState(null, "", `#${targetId}`);
  if (focus) tab.focus();
}

document.querySelectorAll("[data-archive-year]").forEach((yearArchive) => {
  const yearToggle = yearArchive.querySelector(".archive-year-toggle");
  setYearExpanded(yearArchive, yearArchive.dataset.expanded === "true");
  yearToggle?.addEventListener("click", () => {
    setYearExpanded(yearArchive, yearArchive.dataset.expanded !== "true");
  });

  const tabs = [...yearArchive.querySelectorAll("[data-month-target]")];
  const selectedTab = tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
  window.requestAnimationFrame(() => revealMonthTab(selectedTab));
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => selectMonth(tab, { updateHash: true }));
    tab.addEventListener("keydown", (event) => {
      const currentIndex = tabs.indexOf(tab);
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
      if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      selectMonth(tabs[nextIndex], { updateHash: true, focus: true });
    });
  });
});

function revealHashTarget({ scroll = false } = {}) {
  const id = decodeURIComponent(window.location.hash.slice(1));
  if (!id) return;
  const target = document.getElementById(id);
  if (!target) return;

  const monthPanel = target.matches(".archive-month-panel")
    ? target
    : target.closest(".archive-month-panel");
  if (monthPanel) {
    const tab = document.querySelector(`[data-month-target="${monthPanel.id}"]`);
    selectMonth(tab);
  }
  setYearExpanded(target.closest("[data-archive-year]"), true);

  let parent = target;
  while (parent) {
    if (parent.tagName === "DETAILS") parent.open = true;
    parent = parent.parentElement;
  }
  if (scroll) {
    window.requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }
}

toggle?.addEventListener("click", () => setSidebar(!body.classList.contains("sidebar-open")));
document.querySelector("[data-sidebar-close]")?.addEventListener("click", () => setSidebar(false));
navigationShell?.addEventListener("click", (event) => {
  if (event.target.closest("a")) setSidebar(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSidebar(false);
});

document.querySelectorAll("[data-sidebar-action]").forEach((button) => {
  button.addEventListener("click", () => {
    const shouldOpen = button.dataset.sidebarAction === "expand";
    sidebar.querySelectorAll("details").forEach((details) => { details.open = shouldOpen; });
  });
});

window.addEventListener("hashchange", () => revealHashTarget({ scroll: true }));
revealHashTarget({ scroll: true });

const summaryDialog = document.querySelector("#summary-dialog");
const summaryDialogTitle = summaryDialog?.querySelector("#summary-dialog-title");
const summaryDialogContent = summaryDialog?.querySelector("[data-summary-content]");
const summaryDocumentCache = new Map();
let summaryTrigger = null;

function setSummaryDialogContent(markup, { error = false } = {}) {
  if (!summaryDialogContent) return;
  summaryDialogContent.classList.toggle("has-error", error);
  summaryDialogContent.innerHTML = markup;
}

async function loadSummaryDocument(link) {
  const sourceUrl = new URL(link.dataset.summaryUrl || link.href, window.location.href);
  sourceUrl.hash = "";
  const cacheKey = sourceUrl.href;
  if (!summaryDocumentCache.has(cacheKey)) {
    const request = fetch(cacheKey, { headers: { Accept: "text/html" } })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((documentText) => new DOMParser().parseFromString(documentText, "text/html"));
    summaryDocumentCache.set(cacheKey, request);
    request.catch(() => summaryDocumentCache.delete(cacheKey));
  }
  return summaryDocumentCache.get(cacheKey);
}

async function openSummaryDialog(link) {
  if (!summaryDialog?.showModal || !summaryDialogContent) return;
  summaryTrigger = link;
  const paperTitle = link.closest("tr")?.querySelector(".paper-title")?.textContent?.trim();
  if (summaryDialogTitle) summaryDialogTitle.textContent = paperTitle || "论文要点";
  setSummaryDialogContent('<p class="muted">正在加载…</p>');
  summaryDialog.showModal();

  try {
    const parsed = await loadSummaryDocument(link);
    const articleId = link.dataset.summaryId || new URL(link.href).hash.slice(1);
    const article = parsed.getElementById(articleId);
    if (!article) throw new Error("Summary content is missing");
    setSummaryDialogContent(article.innerHTML);
  } catch (error) {
    setSummaryDialogContent(
      '<p>要点加载失败，请稍后重试，或使用链接直接打开总结页面。</p>',
      { error: true },
    );
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-summary-url]");
  if (!link || !summaryDialog?.showModal) return;
  event.preventDefault();
  openSummaryDialog(link);
});

summaryDialog?.querySelector("[data-summary-close]")?.addEventListener("click", () => {
  summaryDialog.close();
});

summaryDialog?.addEventListener("click", (event) => {
  if (event.target === summaryDialog) summaryDialog.close();
});

summaryDialog?.addEventListener("close", () => {
  summaryTrigger?.focus();
  summaryTrigger = null;
});
