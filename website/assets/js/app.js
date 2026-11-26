/* =========================================================
   QSICK · 站点交互
   原生 JS，无构建步骤。所有动效均尊重 prefers-reduced-motion。
   ========================================================= */
(function () {
  "use strict";

  const doc = document;
  const root = doc.documentElement;
  const $ = (sel, ctx) => (ctx || doc).querySelector(sel);
  const $$ = (sel, ctx) => Array.from((ctx || doc).querySelectorAll(sel));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const finePointer = window.matchMedia("(pointer: fine)").matches;

  /* ---------- 主题 ---------- */
  const themeMeta = $('meta[name="theme-color"]');
  const themeToggle = $("#themeToggle");
  const THEME_KEY = "qsick-theme";

  function applyTheme(theme) {
    root.dataset.theme = theme;
    if (themeMeta) themeMeta.setAttribute("content", theme === "dark" ? "#050b18" : "#f3f7ff");
    if (themeToggle) themeToggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  }
  function initTheme() {
    let saved = null;
    try {
      saved = localStorage.getItem(THEME_KEY);
    } catch (e) {}
    applyTheme(saved === "dark" ? "dark" : "light");
    if (themeToggle) {
      themeToggle.addEventListener("click", () => {
        const next = root.dataset.theme === "dark" ? "light" : "dark";
        applyTheme(next);
        try {
          localStorage.setItem(THEME_KEY, next);
        } catch (e) {}
        toast(next === "dark" ? "已切换到深色模式" : "已切换到浅色模式", false);
      });
    }
  }
  initTheme();

  /* ---------- Toast ---------- */
  let toastTimer = 0;
  const toastEl = $("#toast");
  const toastText = $("#toastText");
  function toast(message, check) {
    if (!toastEl || !toastText) return;
    toastText.textContent = message;
    const icon = toastEl.querySelector("use");
    if (icon) icon.setAttribute("href", check === false ? "#i-info" : "#i-check-circle");
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2200);
  }

  /* ---------- 滚动进度 / 页头 / 回顶 ---------- */
  const header = $("#siteHeader");
  const progress = $("#scrollProgress");
  const toTop = $("#toTop");
  let ticking = false;

  function onScrollFrame() {
    const y = window.scrollY || root.scrollTop;
    if (header) header.classList.toggle("scrolled", y > 14);
    if (progress) {
      const max = doc.documentElement.scrollHeight - window.innerHeight;
      progress.style.transform = "scaleX(" + (max > 0 ? Math.min(y / max, 1) : 0) + ")";
    }
    if (toTop) toTop.classList.toggle("show", y > 720);
    spotActiveNav(y);
    ticking = false;
  }
  function onScroll() {
    if (!ticking) {
      ticking = true;
      requestAnimationFrame(onScrollFrame);
    }
  }
  window.addEventListener("scroll", onScroll, { passive: true });

  if (toTop) {
    toTop.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  /* ---------- 首页导航高亮 ---------- */
  const navLinks = $$(".nav-links .nav-link");
  const spySections = [];
  if (doc.body.dataset.page === "home") {
    ["features", "architecture", "quickstart", "docs", "community"].forEach((id) => {
      const el = doc.getElementById(id);
      if (!el) return;
      const link = navLinks.find((a) => (a.getAttribute("href") || "").includes("#" + id));
      if (link) spySections.push({ id, el, link });
    });
  } else {
    const page = doc.body.dataset.page;
    navLinks.forEach((a) => {
      const href = a.getAttribute("href") || "";
      if (page === "docs" && href.startsWith("docs")) a.classList.add("active");
      if (page === "about" && href.startsWith("about")) a.classList.add("active");
    });
  }
  function spotActiveNav(y) {
    if (!spySections.length) return;
    let current = null;
    const probe = y + window.innerHeight * 0.32;
    spySections.forEach((item) => {
      if (item.el.offsetTop <= probe) current = item;
    });
    spySections.forEach((item) => item.link.classList.toggle("active", item === current));
  }

  // 初始化状态（必须在 spySections 初始化之后调用）
  onScrollFrame();

  /* ---------- 移动端菜单 ---------- */
  const burger = $("#navBurger");
  const mobileNav = $("#mobileNav");
  function closeMobileNav() {
    if (!mobileNav) return;
    mobileNav.classList.remove("open");
    if (burger) {
      burger.setAttribute("aria-expanded", "false");
      const use = burger.querySelector("use");
      if (use) use.setAttribute("href", "#i-menu");
    }
  }
  if (burger && mobileNav) {
    burger.addEventListener("click", () => {
      const open = !mobileNav.classList.contains("open");
      mobileNav.classList.toggle("open", open);
      burger.setAttribute("aria-expanded", open ? "true" : "false");
      const use = burger.querySelector("use");
      if (use) use.setAttribute("href", open ? "#i-x" : "#i-menu");
    });
    $$("a", mobileNav).forEach((a) => a.addEventListener("click", closeMobileNav));
    doc.addEventListener("click", (e) => {
      if (!mobileNav.contains(e.target) && !burger.contains(e.target)) closeMobileNav();
    });
  }

  /* ---------- 滚动揭示 ---------- */
  const revealItems = $$("[data-reveal]");
  if (revealItems.length) {
    if (reduceMotion || !("IntersectionObserver" in window)) {
      revealItems.forEach((el) => el.classList.add("in"));
    } else {
      const revealIO = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              entry.target.classList.add("in");
              revealIO.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.12, rootMargin: "0px 0px -6% 0px" }
      );
      revealItems.forEach((el) => revealIO.observe(el));
    }
  }

  /* ---------- 数字滚动 ---------- */
  function animateCount(el) {
    const target = parseInt(el.getAttribute("data-count"), 10);
    if (isNaN(target)) return;
    if (reduceMotion) {
      el.textContent = String(target);
      return;
    }
    const duration = 1500;
    const start = performance.now();
    const from = 0;
    function frame(now) {
      const t = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = String(Math.round(from + (target - from) * eased));
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }
  const countEls = $$("[data-count]");
  if (countEls.length) {
    if (!("IntersectionObserver" in window)) {
      countEls.forEach(animateCount);
    } else {
      const countIO = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              animateCount(entry.target);
              countIO.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.4 }
      );
      countEls.forEach((el) => countIO.observe(el));
    }
  }

  /* ---------- 光标光晕 ---------- */
  const glow = $("#cursorGlow");
  if (glow && finePointer && !reduceMotion) {
    let gx = window.innerWidth / 2;
    let gy = window.innerHeight * 0.3;
    let tx = gx;
    let ty = gy;
    let glowFrame = null;
    doc.body.classList.add("has-cursor");
    doc.addEventListener(
      "pointermove",
      (e) => {
        tx = e.clientX;
        ty = e.clientY;
        if (!glowFrame) {
          glowFrame = requestAnimationFrame(function loop() {
            gx += (tx - gx) * 0.12;
            gy += (ty - gy) * 0.12;
            glow.style.transform = "translate3d(" + gx.toFixed(1) + "px," + gy.toFixed(1) + "px,0)";
            const done = Math.abs(tx - gx) < 0.4 && Math.abs(ty - gy) < 0.4;
            glowFrame = done ? null : requestAnimationFrame(loop);
          });
        }
      },
      { passive: true }
    );
    doc.addEventListener("mouseleave", () => glow.classList.remove("opacity"));
  }

  /* ---------- 卡片光斑 ---------- */
  const spotCards = $$(".feature-card, .doc-card, .step, .arch-node");
  spotCards.forEach((card) => {
    card.addEventListener(
      "pointermove",
      (e) => {
        const rect = card.getBoundingClientRect();
        card.style.setProperty("--mx", ((e.clientX - rect.left) / rect.width) * 100 + "%");
        card.style.setProperty("--my", ((e.clientY - rect.top) / rect.height) * 100 + "%");
      },
      { passive: true }
    );
  });

  /* ---------- 3D 倾斜 ---------- */
  const tiltEls = $$(".tilt");
  if (finePointer && !reduceMotion) {
    tiltEls.forEach((el) => {
      let raf = null;
      el.addEventListener("pointermove", (e) => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          const rect = el.getBoundingClientRect();
          const px = (e.clientX - rect.left) / rect.width - 0.5;
          const py = (e.clientY - rect.top) / rect.height - 0.5;
          el.style.transform =
            "rotateX(" + (-py * 7).toFixed(2) + "deg) rotateY(" + (px * 9).toFixed(2) + "deg) translateY(-2px)";
          raf = null;
        });
      });
      el.addEventListener("pointerleave", () => {
        el.style.transform = "";
      });
    });
  }

  /* ---------- 终端 ---------- */
  const termTabs = $$(".term-tab");
  const termPanels = $$(".term-panel");
  const TERM_COPY = {
    win: "start.bat\r\nstart.bat --check",
    src: "uv sync --extra dev\r\nuv pip install -e . --no-deps\r\nuv run qsick --mock",
  };
  let activeTerm = "win";
  termTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const key = tab.getAttribute("data-term");
      activeTerm = key;
      termTabs.forEach((t) => {
        const on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      termPanels.forEach((p) => p.classList.toggle("active", p.getAttribute("data-panel") === key));
    });
  });
  const copyTermBtn = $("[data-copy-term]");
  if (copyTermBtn) {
    copyTermBtn.addEventListener("click", () => {
      copyText(TERM_COPY[activeTerm] || "", "命令已复制");
    });
  }

  /* ---------- 复制 ---------- */
  function fallbackCopy(text) {
    const ta = doc.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    doc.body.appendChild(ta);
    ta.select();
    try {
      doc.execCommand("copy");
    } catch (e) {}
    doc.body.removeChild(ta);
  }
  function copyText(text, message) {
    if (!text) return;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        () => toast(message || "已复制"),
        () => {
          fallbackCopy(text);
          toast(message || "已复制");
        }
      );
    } else {
      fallbackCopy(text);
      toast(message || "已复制");
    }
  }
  $$(".copy-code").forEach((btn) => {
    btn.addEventListener("click", () => {
      const block = btn.closest(".code-block");
      const code = block ? block.querySelector("pre") : null;
      copyText(code ? code.innerText : "", "代码已复制");
    });
  });

  /* ---------- 搜索 ---------- */
  const SEARCH_INDEX = [
    // 首页
    { title: "特性", desc: "小内核，大能力：原生注入、多协议、插件系统与多实例", href: "index.html#features", tag: "首页" },
    { title: "架构", desc: "一条消息的旅程：QQ.exe → qsick 内核 → 协议端", href: "index.html#architecture", tag: "首页" },
    { title: "快速开始", desc: "三步让 QQ 开口说话：start.bat、运行时与 WebUI", href: "index.html#quickstart", tag: "首页" },
    { title: "文档入口", desc: "架构 / 协议 / 插件 / Payload / 实例 / WebUI", href: "index.html#docs", tag: "首页" },
    { title: "生态与入口", desc: "OneBot v11、Milky、maxStore、WebUI 与插件市场", href: "index.html#ecosystem", tag: "首页" },
    { title: "社区", desc: "QQ 交流群、GitHub 仓库与组织", href: "index.html#community", tag: "首页" },
    // 文档
    { title: "快速开始 · 安装", desc: "start.bat 一键启动、uv 依赖、mock 模式与 WebUI 地址", href: "docs.html#quickstart", tag: "文档" },
    { title: "架构 · 多实例", desc: "实例中心、消息归属、各实例独立的库与网络配置", href: "docs.html#architecture", tag: "文档" },
    { title: "架构 · 消息流水线", desc: "事件先进库再分发，动作必须经过 runtime.invoke", href: "docs.html#architecture", tag: "文档" },
    { title: "架构 · 动作模块", desc: "一个动作一个模块，注册表与分发", href: "docs.html#architecture", tag: "文档" },
    { title: "协议层", desc: "onebotv11 / milky / maxStore 三条流水线与新增协议", href: "docs.html#protocols", tag: "文档" },
    { title: "OneBot v11", desc: "默认聊天协议，事件与动作转换恒等", href: "docs.html#protocols", tag: "协议" },
    { title: "Milky", desc: "POST /api/{api} 与 GET /event，65/65 端点归类", href: "docs.html#protocols", tag: "协议" },
    { title: "maxStore", desc: "原始消息证据库：只写消息缓存，保留全部原始 payload", href: "docs.html#protocols", tag: "协议" },
    { title: "插件 API", desc: "basic.json 清单、权限、生命周期钩子与配置界面", href: "docs.html#plugins", tag: "文档" },
    { title: "插件市场", desc: "搜索、安装、更新远程插件；受 allow_remote_install 约束", href: "docs.html#plugins", tag: "插件" },
    { title: "Payload ABI", desc: "帧协议、op 契约、钩子偏移表、注入与管道认证", href: "docs.html#payload", tag: "文档" },
    { title: "命名管道", desc: "\\\\.\\pipe\\qsick.<pid>.ctl / .evt 与 32 字节帧头", href: "docs.html#payload", tag: "协议" },
    { title: "实例", desc: "headless / pipe 两类实例、模板硬链接与启动链路", href: "docs.html#instances", tag: "文档" },
    { title: "WebUI · 仪表盘", desc: "CPU / 内存 / 网络配置 / 消息数，手写 SVG 图表", href: "docs.html#webui", tag: "文档" },
    { title: "WebUI · 实时日志", desc: "SSE 日志流、级别过滤、暂停与下载", href: "docs.html#webui", tag: "文档" },
    { title: "WebUI · 动作测试器", desc: "242 个动作的分类过滤与 JSON 测试", href: "docs.html#webui", tag: "文档" },
    // 关于
    { title: "关于 QSICK", desc: "项目目标、设计取舍与实现边界", href: "about.html", tag: "关于" },
    { title: "免责声明", desc: "注入风险、账号安全与使用协议", href: "about.html#disclaimer", tag: "关于" },
    { title: "许可证", desc: "AGPL-3.0-or-later 与专有 payload 组件", href: "about.html#license", tag: "关于" },
    { title: "致谢与社区", desc: "参考项目、社区与反馈渠道", href: "about.html#thanks", tag: "关于" },
    // 外链
    { title: "GitHub 仓库", desc: "ZY-Zone / QSICK--QQ_NT-Bridge", href: "https://github.com/ZY-Zone/QSICK--QQ_NT-Bridge", tag: "外部" },
    { title: "QQ 交流群", desc: "问题反馈与更新通知", href: "https://qm.qq.com/q/IDnnkW9eSs", tag: "外部" },
    { title: "插件市场", desc: "QSICK 远程插件市场", href: "http://sickcc.online/QSICK/plugins-market", tag: "外部" },
  ];

  const overlay = $("#searchOverlay");
  const searchBtn = $("#searchOpen");
  const searchInput = $("#searchInput");
  const searchResults = $("#searchResults");
  let searchActiveIndex = 0;
  let searchItems = [];

  // 没有内置弹层的页面（docs / about）由脚本补齐
  let ov = overlay;
  if (!ov) {
    ov = doc.createElement("div");
    ov.className = "search-overlay";
    ov.id = "searchOverlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-label", "搜索文档");
    ov.innerHTML =
      '<div class="search-panel">' +
      '<div class="search-input-row">' +
      '<svg aria-hidden="true"><use href="#i-search"/></svg>' +
      '<input id="searchInput" type="text" placeholder="搜索功能、文档、命令…" autocomplete="off" spellcheck="false" aria-label="搜索关键词">' +
      '<span class="search-esc">ESC</span></div>' +
      '<div class="search-results" id="searchResults"></div>' +
      '<div class="search-hint"><span><kbd>↑</kbd><kbd>↓</kbd> 选择 · <kbd>Enter</kbd> 打开</span><span>QSICK docs</span></div>' +
      "</div>";
    doc.body.appendChild(ov);
  }
  const sInput = $("#searchInput");
  const sResults = $("#searchResults");

  function iconForTag(tag) {
    if (tag === "外部") return "#i-external";
    if (tag === "协议" || tag === "插件") return "#i-network";
    if (tag === "关于") return "#i-info";
    if (tag === "首页") return "#i-sparkles";
    return "#i-book";
  }

  function renderSearch(query) {
    if (!sResults) return;
    const q = (query || "").trim().toLowerCase();
    let list;
    if (!q) {
      list = SEARCH_INDEX.slice(0, 8);
    } else {
      list = SEARCH_INDEX.filter((item) => {
        const hay = (item.title + " " + item.desc + " " + item.tag).toLowerCase();
        return hay.includes(q);
      }).slice(0, 12);
    }
    searchItems = list;
    searchActiveIndex = 0;
    if (!list.length) {
      sResults.innerHTML = '<div class="search-empty">没有找到与「' + escapeHtml(query) + '」相关的结果<br><span style="opacity:.7">试试「架构」「插件」「WebUI」「快速开始」</span></div>';
      return;
    }
    let html = '<div class="search-group-label">' + (q ? "搜索结果" : "推荐阅读") + "</div>";
    list.forEach((item, i) => {
      html +=
        '<a class="search-result' + (i === 0 ? " active" : "") + '" href="' + item.href + '" data-index="' + i + '">' +
        '<span class="sr-icon"><svg aria-hidden="true"><use href="' + iconForTag(item.tag) + '"/></svg></span>' +
        "<span><b>" + escapeHtml(item.title) + "</b><small>" + escapeHtml(item.desc) + "</small></span>" +
        '<span class="sr-tag">' + escapeHtml(item.tag) + "</span></a>";
    });
    sResults.innerHTML = html;
    $$(".search-result", sResults).forEach((el) => {
      el.addEventListener("click", closeSearch);
    });
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  function highlightResult(index) {
    const items = $$(".search-result", sResults);
    if (!items.length) return;
    searchActiveIndex = (index + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle("active", i === searchActiveIndex));
    const active = items[searchActiveIndex];
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function openSearch() {
    ov.classList.add("open");
    doc.body.classList.add("no-scroll");
    if (sInput) {
      sInput.value = "";
      renderSearch("");
      setTimeout(() => sInput.focus(), 60);
    }
  }
  function closeSearch() {
    ov.classList.remove("open");
    doc.body.classList.remove("no-scroll");
  }
  if (searchBtn) searchBtn.addEventListener("click", openSearch);
  if (sInput) {
    sInput.addEventListener("input", () => renderSearch(sInput.value));
    sInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        highlightResult(searchActiveIndex + 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        highlightResult(searchActiveIndex - 1);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const active = $$(".search-result", sResults)[searchActiveIndex];
        if (active) {
          active.click();
          window.location.href = active.getAttribute("href");
        }
      }
    });
  }
  if (ov) {
    ov.addEventListener("click", (e) => {
      if (e.target === ov) closeSearch();
    });
  }
  doc.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "");
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (ov.classList.contains("open")) closeSearch();
      else openSearch();
    } else if (e.key === "Escape") {
      if (ov.classList.contains("open")) closeSearch();
      closeMobileNav();
    } else if (e.key === "/" && !typing && !ov.classList.contains("open")) {
      e.preventDefault();
      openSearch();
    }
  });

  /* ---------- 文档侧栏高亮 ---------- */
  const sideLinks = $$(".side-link");
  const docBlocks = $$(".doc-block[id]");
  if (sideLinks.length && docBlocks.length) {
    const sideIO = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          sideLinks.forEach((link) => {
            link.classList.toggle("active", link.getAttribute("href") === "#" + entry.target.id);
          });
        });
      },
      { rootMargin: "-18% 0px -62% 0px", threshold: 0 }
    );
    docBlocks.forEach((b) => sideIO.observe(b));
  }

  /* ---------- 进入动画 ---------- */
  if (!reduceMotion) {
    const main = doc.querySelector("main");
    if (main) main.classList.add("page-enter");
  }

  /* ---------- 外链安全 ---------- */
  $$('a[target="_blank"]').forEach((a) => {
    if (!a.rel) a.rel = "noopener";
  });
})();
