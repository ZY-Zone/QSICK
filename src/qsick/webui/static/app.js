/* QSICK console: theme, toasts, live logs, action tester. No framework. */
(function () {
  "use strict";

  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };

  function toast(message, kind) {
    var host = $("#toasts");
    if (!host) return;
    var node = document.createElement("div");
    node.className = "toast " + (kind || "");
    node.textContent = message;
    host.appendChild(node);
    setTimeout(function () { node.remove(); }, 3200);
  }
  window.qsickToast = toast;

  window.qsickCopy = function (id) {
    var node = typeof id === "string" ? document.getElementById(id) : id;
    if (!node) return false;
    var text = node.value !== undefined ? node.value : node.textContent;
    if (!text) return false;
    navigator.clipboard.writeText(text).then(
      function () { toast("已复制", "ok"); },
      function () { toast("复制失败（浏览器权限）", "err"); }
    );
    return false;
  };

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ------------------------------------------------------------------- logs
  function initLogs() {
    var view = $("#logs");
    if (!view) return;

    var levelSelect = $("#log-level");
    var categorySelect = $("#log-category");
    var search = $("#log-search");
    var pause = $("#log-pause");
    var autoscroll = $("#log-autoscroll");
    var counter = $("#log-count");
    var ranks = { TRACE: 0, DEBUG: 1, INFO: 2, WARN: 3, ERROR: 4 };
    var lines = [];

    try {
      var savedLevel = localStorage.getItem("qsick.log.level");
      if (savedLevel && Object.prototype.hasOwnProperty.call(ranks, savedLevel)) {
        levelSelect.value = savedLevel;
      }
    } catch (_) {}

    function matches(line) {
      var level = line.dataset.level || "INFO";
      if ((ranks[level] || 0) < (ranks[levelSelect.value] || 2)) return false;
      var category = categorySelect.value;
      if (category && line.dataset.category !== category) return false;
      var needle = (search.value || "").trim().toLowerCase();
      if (needle && (line.textContent || "").toLowerCase().indexOf(needle) === -1) return false;
      return true;
    }

    function apply() {
      var shown = 0;
      lines.forEach(function (line) {
        var visible = matches(line);
        line.classList.toggle("hidden", !visible);
        if (visible) shown += 1;
      });
      if (counter) counter.textContent = String(shown);
      if (autoscroll.checked) view.scrollTop = view.scrollHeight;
    }

    function add(record) {
      var line = document.createElement("div");
      line.className = "logline lvl-" + record.level;
      line.dataset.level = record.level;
      line.dataset.category = record.category;
      var fields = record.fields && Object.keys(record.fields).length
        ? " " + Object.keys(record.fields).map(function (key) {
            return key + "=" + JSON.stringify(record.fields[key]);
          }).join(" ")
        : "";
      line.innerHTML =
        '<span class="t">' + escapeHtml(record.time) + "</span>" +
        '<span class="l">' + escapeHtml(record.level) + "</span>" +
        '<span class="c" title="' + escapeHtml(record.category) + '">' + escapeHtml(record.category) + "</span>" +
        '<span class="m">' + escapeHtml(record.message) + '<span class="f">' + escapeHtml(fields) + "</span></span>";
      view.appendChild(line);
      lines.push(line);
      if (!categorySelect.querySelector('option[value="' + CSS.escape(record.category) + '"]')) {
        var option = document.createElement("option");
        option.value = record.category;
        option.textContent = record.category;
        categorySelect.appendChild(option);
      }
      while (lines.length > 4000) {
        var first = lines.shift();
        if (first && first.parentNode) first.parentNode.removeChild(first);
      }
    }

    var seed = document.getElementById("log-seed");
    if (seed) {
      try { JSON.parse(seed.textContent).forEach(add); } catch (error) { /* ignore */ }
    }
    apply();

    levelSelect.addEventListener("change", function () {
      try { localStorage.setItem("qsick.log.level", levelSelect.value); } catch (_) {}
      apply();
    });
    categorySelect.addEventListener("change", apply);
    search.addEventListener("input", apply);
    $("#log-clear").addEventListener("click", function () {
      if (!confirm("清空服务端日志缓冲？")) return;
      fetch("/api/logs/clear", { method: "POST" }).then(function () {
        view.innerHTML = "";
        lines = [];
        apply();
        toast("日志已清空", "ok");
      });
    });
    $("#log-copy").addEventListener("click", function () {
      var text = lines.filter(function (line) { return !line.classList.contains("hidden"); })
        .map(function (line) { return line.textContent; }).join("\n");
      navigator.clipboard.writeText(text).then(function () { toast("已复制当前日志", "ok"); });
    });

    var source = new EventSource("/api/logs/stream");
    source.onmessage = function (event) {
      if (pause.checked) return;
      try {
        add(JSON.parse(event.data));
        apply();
      } catch (error) { /* ignore malformed frame */ }
    };
    source.onerror = function () {
      var dot = $("#log-dot");
      if (dot) { dot.className = "dot warn"; }
    };
  }

  // ---------------------------------------------------------------- actions
  var currentSpec = null;

  function initActions() {
    var list = $("#action-list");
    if (!list) return;
    var search = $("#action-search");
    var category = $("#action-category");
    var tabs = $("#action-tabs");
    var wireFilter = "all";

    function filter() {
      var needle = (search.value || "").trim().toLowerCase();
      $$(".actionrow", list).forEach(function (row) {
        var ok = true;
        if (wireFilter !== "all" && row.dataset.wire !== wireFilter) ok = false;
        if (ok && category.value && row.dataset.category !== category.value) ok = false;
        if (ok && needle && row.dataset.name.toLowerCase().indexOf(needle) === -1) ok = false;
        row.classList.toggle("hidden", !ok);
      });
    }

    search.addEventListener("input", filter);
    category.addEventListener("change", filter);
    tabs.addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) return;
      wireFilter = button.dataset.wire;
      $$("button", tabs).forEach(function (item) { item.classList.toggle("active", item === button); });
      filter();
    });

    list.addEventListener("click", function (event) {
      var row = event.target.closest(".actionrow");
      if (!row) return;
      $$(".actionrow", list).forEach(function (item) { item.classList.remove("selected"); });
      row.classList.add("selected");
      fetch("/api/actions/" + encodeURIComponent(row.dataset.name))
        .then(function (response) { return response.json(); })
        .then(function (spec) { selectSpec(spec, row.dataset.wire); })
        .catch(function () { toast("无法加载动作详情", "err"); });
    });
  }

  function selectSpec(spec, wire) {
    currentSpec = spec;
    $("#tester-title").textContent = spec.name;
    $("#tester-hint").textContent = (spec.summary || spec.category) + " · " + spec.status;
    var badge = $("#tester-badge");
    badge.textContent = wire === "core" ? "core" : "wire pending";
    badge.className = "badge " + (wire === "core" ? "accent" : "warn");
    $("#tester-params").value = JSON.stringify(placeholder(spec), null, 2);
    $("#tester-run").disabled = spec.status !== "implemented";
    $("#tester-result").textContent = "—";
    $("#tester-result").className = "result";
  }

  function placeholder(spec) {
    var out = {};
    (spec.params || []).forEach(function (param) {
      out[param.name] = param.default !== null && param.default !== undefined ? param.default : sample(param.type);
    });
    return out;
  }

  function sample(type) {
    if (type === "int" || type === "float") return 0;
    if (type === "bool") return false;
    if (type === "list") return [];
    if (type === "dict") return {};
    if (type === "message") return [{ type: "text", data: { text: "hello" } }];
    return "";
  }

  window.qsickFillRequired = function () {
    if (!currentSpec) return false;
    var out = {};
    (currentSpec.params || []).forEach(function (param) {
      if (param.required) out[param.name] = sample(param.type);
    });
    $("#tester-params").value = JSON.stringify(out, null, 2);
    return false;
  };

  window.qsickRunAction = function (event) {
    event.preventDefault();
    if (!currentSpec) return false;
    var params;
    try {
      params = JSON.parse($("#tester-params").value || "{}");
    } catch (error) {
      $("#tester-result").textContent = "参数不是合法 JSON：" + error.message;
      $("#tester-result").className = "result err";
      return false;
    }
    var out = $("#tester-result");
    out.textContent = "…";
    out.className = "result";
    fetch("/api/actions/" + encodeURIComponent(currentSpec.name), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params)
    }).then(function (response) { return response.text(); }).then(function (text) {
      var parsed;
      try { parsed = JSON.parse(text); } catch (error) { parsed = text; }
      out.textContent = typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2);
      out.className = "result " + (parsed && parsed.status === "ok" ? "ok" : "err");
    }).catch(function (error) {
      out.textContent = String(error);
      out.className = "result err";
    });
    return false;
  };

  // ------------------------------------------------------------- status fallback
  function initStatusFallback() {
    if (window.htmx) return;
    var target = $("#status");
    if (!target) return;
    setInterval(function () {
      fetch("/partials/status").then(function (response) {
        return response.ok ? response.text() : null;
      }).then(function (html) {
        if (html) target.innerHTML = html;
      }).catch(function () { /* ignore */ });
    }, 2000);
  }

  // ---------------------------------------------------------- custom select menus
  var selectMenu = null;

  /** 事件目标所在的 <select>，管不到就返回 null。
   *
   * 只认单选：多选下拉交互完全不同，硬套这套菜单会得到一个选不出第二项的框。
   * 点在下拉的 <option> 上时事件目标是 option，所以统一往上看 closest。
   *
   * 带 ``data-native-select`` 的跳过：那是对话框里的下拉。<dialog showModal()>
   * 处于浏览器 top layer，层级高于任何 z-index，而这份菜单是挂在 body 上的普通
   * 元素——硬接管只会得到一个被对话框盖住的菜单。那些地方用系统弹出层反而正确。
   */
  function selectTarget(target) {
    if (!target || target.tagName !== "SELECT") {
      target = target && target.closest ? target.closest("select") : null;
    }
    if (!target || target.multiple) return null;
    if (target.dataset && target.dataset.nativeSelect !== undefined) return null;
    return target;
  }

  function closeSelectMenu() {
    if (selectMenu) selectMenu.hidden = true;
  }

  function openSelectMenu(select) {
    if (!selectMenu) return;
    // 就地重建内容，所以"换一个 select"不需要先关再开——同一份菜单挪位置、
    // 换选项即可。这样也不会出现两个下拉同时可见。
    selectMenu.innerHTML = "";
    Array.prototype.forEach.call(select.options, function (option) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "dd-option" + (option.selected ? " active" : "");
      item.textContent = option.textContent;
      // 原生 option 上的 disabled 要延续下来：「无可用账号」这类占位项在系统
      // 菜单里点不动，换了自己的菜单也不该能点。
      if (option.disabled) {
        item.disabled = true;
        item.setAttribute("disabled", "");
      }
      if (option.title) item.title = option.title;
      item.addEventListener("click", function () {
        if (option.disabled) return;
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        closeSelectMenu();
      });
      selectMenu.appendChild(item);
    });
    var rect = select.getBoundingClientRect();
    selectMenu.style.width = Math.max(rect.width, 180) + "px";
    selectMenu.style.left = rect.left + "px";
    selectMenu.style.top = (rect.bottom + 4) + "px";
    selectMenu.hidden = false;
    if (rect.bottom + selectMenu.offsetHeight + 8 > window.innerHeight) {
      selectMenu.style.top = Math.max(8, rect.top - selectMenu.offsetHeight - 4) + "px";
    }
  }

  // 接管所有原生 <select>，换成自己的菜单。
  //
  // 这里用**事件委托**而不是逐个绑定：早期版本在 DOMContentLoaded 时扫一遍
  // querySelectorAll("select") 就完事，结果有两类下拉一直是系统原生样式——
  //   1. 之后才插进页面的 <select>（比如插件配置页、动态渲染的表单行）；
  //   2. 启动时是 disabled、后来才启用的（「快捷发送」的账号框就是这样：
  //      账号列表拉回来之前它被设成 disabled，之后再 false）。
  // 委托挂在 document 上，这两类都会走同一条路径，不需要谁记得回来注册一次。
  function initSelectMenus() {
    if (selectMenu) return;
    selectMenu = document.createElement("div");
    selectMenu.className = "dd-menu";
    selectMenu.hidden = true;
    document.body.appendChild(selectMenu);

    document.addEventListener("mousedown", function (event) {
      var select = selectTarget(event.target);
      if (!select) return;
      // 阻止原生弹出层；disabled 的也拦下来，免得一个灰掉的下拉还能点出系统菜单。
      event.preventDefault();
      if (select.disabled) return;
      openSelectMenu(select);
    });

    document.addEventListener("keydown", function (event) {
      var select = selectTarget(event.target);
      if (!select || select.disabled) return;
      if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
        event.preventDefault();
        openSelectMenu(select);
      }
    });

    document.addEventListener("mousedown", function (event) {
      if (!selectMenu || selectMenu.hidden) return;
      if (selectMenu.contains(event.target)) return;
      // 点在某个 select 上时不要关：上一个监听器刚刚为它打开了菜单（或在切换
      // 到它）。这里若继续关，就会把刚打开的菜单立刻收起来——两个监听器都在
      // document 上，顺序触发，看起来像"点了没反应"。
      if (selectTarget(event.target)) return;
      closeSelectMenu();
    });
    window.addEventListener("resize", closeSelectMenu);
    window.addEventListener("scroll", closeSelectMenu, true);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initLogs();
    initActions();
    initStatusFallback();
    initSelectMenus();
  });
})();
