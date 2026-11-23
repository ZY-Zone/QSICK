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
  // 内联脚本要用转义函数，而它在本文件的作用域里，够不着 —— 导出一份。
  window.qsickEscape = escapeHtml;

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

    // 分类 → 分组。分组筛选要按组匹配，靠这张表在本地判断，不必为每次筛选回服务器。
    var categoryGroups = {};
    try {
      var groupSeed = document.getElementById("log-groups");
      if (groupSeed) categoryGroups = JSON.parse(groupSeed.textContent) || {};
    } catch (_) {}

    /** 还原上次选的分类筛选。
     *
     * 之前只有级别会被记住，分类每次刷新都回到"全部"——刷一下就丢筛选条件，
     * 而刷新恰恰是排查问题时最常做的事。
     *
     * 记下的值可能已经不存在了（插件被卸载、分类改名），所以只有对应 <option>
     * 真的在列表里才套用，否则留空。设一个不存在的 value 会让 select 显示成空白，
     * 看起来像坏了。
     */
    function restoreCategory() {
      var saved = "";
      try { saved = localStorage.getItem("qsick.log.category") || ""; } catch (_) {}
      if (!saved) return;
      var exists = false;
      for (var i = 0; i < categorySelect.options.length; i += 1) {
        if (categorySelect.options[i].value === saved) { exists = true; break; }
      }
      if (exists) categorySelect.value = saved;
      else { try { localStorage.removeItem("qsick.log.category"); } catch (_) {} }
    }
    restoreCategory();

    function matches(line) {
      var level = line.dataset.level || "INFO";
      if ((ranks[level] || 0) < (ranks[levelSelect.value] || 2)) return false;
      var wanted = categorySelect.value;
      if (wanted) {
        if (wanted.indexOf("group:") === 0) {
          if (categoryGroups[line.dataset.category] !== wanted.slice(6)) return false;
        } else if (line.dataset.category !== wanted) {
          return false;
        }
      }
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
      var group = categoryGroups[record.category];
      if (group) line.dataset.group = group;
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
    categorySelect.addEventListener("change", function () {
      try { localStorage.setItem("qsick.log.category", categorySelect.value); } catch (_) {}
      apply();
    });
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

  // ----------------------------------------------------------------- charts
  //
  // 三张图都画在 <svg> 里，用 viewBox 定坐标、preserveAspectRatio="none" 拉满容器。
  // 不引入图表库：需要的只有折线、柱子和饼，而一个 40KB 的库换不来这四十行 SVG。
  //
  // 坐标一律用"作图坐标系"（0..640 × 0..190），再由 viewBox 缩放到实际宽度——
  // 这样数据点不需要知道容器多大，窗口缩放时也不用重算。

  var CHART_H = 190;
  var CHART_PAD = { top: 14, right: 12, bottom: 22, left: 34 };

  /** 把 <svg> 的坐标系设成它当前的实际像素宽度，并返回作图区尺寸。
   *
   * 原来的写法是固定 viewBox="0 0 640 190" 加 preserveAspectRatio="none"：
   * 图形拉满容器没问题，但**文字也被跟着横向拉伸**。1440px 宽的页面上，
   * 640 宽的坐标系被拉到 ~660px，刻度字号被压扁到认不出——截图里
   * "20.0%" 看起来像 "2.0%"，就是这么来的。
   *
   * 现在每次渲染按 clientWidth 重设 viewBox，1 单位 = 1 像素，文字不再变形；
   * 窗口尺寸变化时重画一次即可（见下面的 resize 监听）。
   */
  function prepare(svg) {
    var width = Math.max(280, Math.round(svg.clientWidth || svg.parentNode.clientWidth || 640));
    svg.setAttribute("viewBox", "0 0 " + width + " " + CHART_H);
    return {
      width: width,
      left: CHART_PAD.left,
      right: width - CHART_PAD.right,
      top: CHART_PAD.top,
      bottom: CHART_H - CHART_PAD.bottom,
    };
  }

  // 记住上一次的数据：窗口缩放后要用同一份数据重画，不必等下一次轮询。
  var lastCharts = null;

  /** 折线路径：点集 → "M x y L x y …"。 */
  function linePath(points, toX, toY) {
    if (!points.length) return "";
    return points.map(function (point, index) {
      return (index ? "L" : "M") + toX(point[0]).toFixed(1) + " " + toY(point[1]).toFixed(1);
    }).join(" ");
  }

  function svgEl(name, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    return node;
  }

  function clearSvg(svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
  }

  /** 把任意上限抬到一个"好读"的整数，返回 {top, step}。
   *
   * 直接按 4 等分会得到 0 / 176 / 353 / 529 这种刻度：数值没错，但没人这么读图。
   * 从 1-2-5 数列里挑步长，刻度就落在 0 / 200 / 400 上。
   */
  function niceAxis(rawMax) {
    var target = Math.max(1e-6, rawMax) / 4;   // 四条网格线 → 五档
    var magnitude = Math.pow(10, Math.floor(Math.log10(target)));
    var candidates = [1, 2, 2.5, 5, 10];
    var step = magnitude;
    for (var index = 0; index < candidates.length; index += 1) {
      step = candidates[index] * magnitude;
      if (step >= target) break;
    }
    var top = step * 4;
    // 上限被抬过数据的真实最大值才画得下：数据 100 配 step 25 正好，数据 101 需要 step 50。
    while (top < rawMax) { step *= 2; top = step * 4; }
    return { top: top, step: step };
  }

  /** 网格与纵轴刻度。`scale` 来自 niceAxis，`unit` 只用于文字。 */
  function drawGrid(svg, box, scale, unit) {
    for (var step = 0; step <= 4; step += 1) {
      var value = step * scale.step;
      var y = box.bottom - (value / scale.top) * (box.bottom - box.top);
      svg.appendChild(svgEl("line", {
        x1: box.left, x2: box.right, y1: y, y2: y,
        class: step === 0 ? "chart-axis" : "chart-grid",
      }));
      var label = svgEl("text", {
        x: box.left - 6, y: y + 3, class: "chart-tick", "text-anchor": "end",
      });
      label.textContent = (scale.step < 1 ? value.toFixed(1) : String(Math.round(value))) + unit;
      svg.appendChild(label);
    }
  }

  function drawCpu(charts) {
    var svg = $("#cpu-chart");
    if (!svg) return;
    clearSvg(svg);
    var box = prepare(svg);
    var cpu = charts.cpu || {};
    var points = cpu.points || [];
    var fit = cpu.fit || [];
    var average = Number(cpu.average || 0);
    var peak = Number(cpu.peak || 0);

    // 纵轴顶取"数据和均值里较高的那个"再留一点余量，且至少 10%：
    // 一条贴着实线走的空载曲线看不出形状，而 0 刻度对百分比没有意义。
    var scale = niceAxis(Math.min(100, Math.max(10, peak, average) * 1.15));
    drawGrid(svg, box, scale, "%");

    function toY(value) { return box.bottom - (value / scale.top) * (box.bottom - box.top); }

    if (!points.length) {
      var empty = svgEl("text", {
        x: box.width / 2, y: CHART_H / 2, class: "chart-empty", "text-anchor": "middle",
      });
      empty.textContent = charts.available ? "正在采样…" : "本平台不支持采样";
      svg.appendChild(empty);
      return;
    }

    var first = points[0][0];
    var last = points[points.length - 1][0];
    var span = last - first || 1;
    function toX(stamp) { return box.left + ((stamp - first) / span) * (box.right - box.left); }
    function pathOf(series) {
      return series.map(function (point, index) {
        return (index ? "L" : "M") + toX(point[0]).toFixed(1) + " " + toY(point[1]).toFixed(1);
      }).join(" ");
    }

    // 均值线：虚线，画在数据下面，免得盖住真实曲线。
    svg.appendChild(svgEl("line", {
      x1: box.left, x2: box.right, y1: toY(average), y2: toY(average), class: "chart-avg",
    }));

    // 面积：给折线一点体积感，半透明让它不抢眼。
    var line = pathOf(points);
    if (line) {
      svg.appendChild(svgEl("path", {
        d: line + " L" + toX(last).toFixed(1) + " " + box.bottom +
           " L" + toX(first).toFixed(1) + " " + box.bottom + " Z",
        class: "chart-area",
      }));
      svg.appendChild(svgEl("path", { d: line, class: "chart-line" }));
    }
    if (fit.length) {
      svg.appendChild(svgEl("path", { d: pathOf(fit), class: "chart-fit" }));
    }

    // 横轴两端各标一个时间：30 分钟的窗口用"距今多久"比用钟点更好读。
    [[first, "start", Math.round((last - first) / 60) + " 分钟前"],
     [last, "end", "现在"]].forEach(function (entry) {
      var label = svgEl("text", {
        x: entry[1] === "end" ? box.right : box.left, y: CHART_H - 6, class: "chart-tick",
        "text-anchor": entry[1] === "end" ? "end" : "start",
      });
      label.textContent = entry[2];
      svg.appendChild(label);
    });

    var now = $("#cpu-now"), avg = $("#cpu-avg"), pk = $("#cpu-peak");
    if (now) now.textContent = Number(cpu.current || 0).toFixed(1) + "%";
    if (avg) avg.textContent = average.toFixed(1) + "%";
    if (pk) pk.textContent = peak.toFixed(1) + "%";
  }

  /** 内存饼图：qsick 自身、每个 QQ 主进程、其他已用、空闲。 */
  function drawMemory(charts) {
    var svg = $("#mem-chart");
    if (!svg) return;
    clearSvg(svg);
    var memory = charts.memory || {};
    var total = Number(memory.total || 0);
    var legend = $("#mem-legend");
    if (legend) legend.innerHTML = "";
    var totalLabel = $("#mem-total");
    if (totalLabel) {
      totalLabel.textContent = total ? (total / 1073741824).toFixed(1) + " GB 物理内存" : "";
    }
    if (!total) return;

    // 超过四五个 QQ 进程时颜色会重复，所以按固定色板循环取；
    // 图例文字里带了 pid，重复颜色也能分清是哪一块。
    var palette = ["--sky", "--ok", "--warn", "--err", "--fuchsia", "--teal"];
    var slices = [{ label: "QSICK", value: Number(memory.self || 0), color: "--sky" }];
    (memory.qq || []).forEach(function (item, index) {
      var name = item.uin ? "QQ (" + item.uin + ")" : "QQ " + item.pid;
      slices.push({
        label: name, value: Number(item.bytes || 0),
        color: palette[(index + 1) % palette.length],
        sub: item.processes > 1 ? item.processes + " 个进程" : "",
      });
    });
    slices.push({ label: "其他", value: Number(memory.other || 0), color: "--faint" });
    slices.push({ label: "空闲", value: Number(memory.available || 0), color: "--pie-free" });

    var usable = slices.filter(function (slice) { return slice.value > 0; });
    var centre = 90, radius = 74, thickness = 26;
    var start = -Math.PI / 2;
    usable.forEach(function (slice) {
      var sweep = (slice.value / total) * Math.PI * 2;
      // 整圆用一个 ring，不用 arc：起止点重合的 A 命令什么都不画。
      if (sweep >= Math.PI * 2 - 1e-6) {
        svg.appendChild(svgEl("circle", {
          cx: centre, cy: centre, r: radius - thickness / 2,
          fill: "none", stroke: "var(" + slice.color + ")", "stroke-width": thickness,
        }));
      } else {
        var outer = radius, inner = radius - thickness;
        var mid = radius - thickness / 2;
        var x1 = centre + outer * Math.cos(start), y1 = centre + outer * Math.sin(start);
        var x2 = centre + outer * Math.cos(start + sweep), y2 = centre + outer * Math.sin(start + sweep);
        var x3 = centre + inner * Math.cos(start + sweep), y3 = centre + inner * Math.sin(start + sweep);
        var x4 = centre + inner * Math.cos(start), y4 = centre + inner * Math.sin(start);
        var large = sweep > Math.PI ? 1 : 0;
        svg.appendChild(svgEl("path", {
          d: "M" + x1.toFixed(1) + " " + y1.toFixed(1) +
             " A" + outer + " " + outer + " 0 " + large + " 1 " + x2.toFixed(1) + " " + y2.toFixed(1) +
             " L" + x3.toFixed(1) + " " + y3.toFixed(1) +
             " A" + inner + " " + inner + " 0 " + large + " 0 " + x4.toFixed(1) + " " + y4.toFixed(1) + " Z",
          fill: "var(" + slice.color + ")",
        }));
      }
      start += sweep;
    });

    var used = svgEl("text", { x: centre, y: centre - 2, class: "pie-centre", "text-anchor": "middle" });
    used.textContent = Math.round(((total - Number(memory.available || 0)) / total) * 100) + "%";
    svg.appendChild(used);
    var caption = svgEl("text", { x: centre, y: centre + 14, class: "pie-caption", "text-anchor": "middle" });
    caption.textContent = "已用";
    svg.appendChild(caption);

    if (!legend) return;
    usable.forEach(function (slice) {
      var row = document.createElement("li");
      var share = (slice.value / total) * 100;
      row.innerHTML =
        '<i class="sw" style="background:var(' + slice.color + ')"></i>' +
        '<span class="pie-name">' + escapeHtml(slice.label) + "</span>" +
        '<span class="pie-size">' + formatBytes(slice.value) + "</span>" +
        '<span class="pie-share">' + share.toFixed(share < 1 ? 2 : 1) + "%</span>";
      if (slice.sub) row.title = slice.sub;
      legend.appendChild(row);
    });
  }

  function formatBytes(bytes) {
    var value = Number(bytes || 0);
    if (value >= 1073741824) return (value / 1073741824).toFixed(2) + " GB";
    return Math.round(value / 1048576) + " MB";
  }

  /** 消息数柱状图：每根柱子按来源账号分段，段色固定跟着账号走。

   * 颜色按账号在 `accounts` 里的次序分配，而那个次序由服务端按总量排好——
   * 不按账号数字大小，否则量最大的账号可能拿到最不显眼的颜色。
   * 同一个账号在每次重画、每次刷新后颜色都一样，图例才读得下去。
   */
  var ACCOUNT_COLORS = [
    "--sky", "--ok", "--warn", "--err", "--violet", "--teal", "--pink", "--faint",
  ];

  function drawMessages(charts) {
    var svg = $("#msg-chart");
    if (!svg) return;
    clearSvg(svg);
    var box = prepare(svg);
    var data = charts.messages || {};
    var buckets = data.buckets || [];
    var accounts = data.accounts || [];
    var peak = Number(data.peak || 0);
    var average = Number(data.average || 0);

    var total = $("#msg-total"), pk = $("#msg-peak"), avg = $("#msg-avg");
    if (total) total.textContent = String(data.total || 0);
    if (pk) pk.textContent = String(peak);
    if (avg) avg.textContent = average.toFixed(1);

    var colorOf = {};
    accounts.forEach(function (item, index) {
      colorOf[item.account] = ACCOUNT_COLORS[index % ACCOUNT_COLORS.length];
    });

    // 图例：账号 → 颜色 → 总量。放在标题行右侧，不占图的面积。
    var legend = $("#msg-accounts");
    if (legend) {
      legend.innerHTML = "";
      accounts.forEach(function (item) {
        var chip = document.createElement("span");
        chip.className = "chip-line";
        chip.innerHTML =
          '<i class="sw" style="background:var(' + colorOf[item.account] + ')"></i>' +
          escapeHtml(item.account) + " <b>" + item.count + "</b>";
        legend.appendChild(chip);
      });
    }

    var scale = niceAxis(Math.max(1, peak * 1.15));
    drawGrid(svg, box, scale, "");

    if (!buckets.length) return;

    var slot = (box.right - box.left) / buckets.length;
    var barWidth = Math.max(3, slot * 0.62);
    var previousDay = "";
    buckets.forEach(function (bucket, index) {
      var x = box.left + slot * index + (slot - barWidth) / 2;
      var bottom = box.bottom;
      var totalHeight = 0;
      var parts = bucket.accounts || {};

      // 从下往上堆：段序与 `accounts` 一致，所以量最大的账号总是贴着轴，最容易被看见。
      accounts.forEach(function (item) {
        var count = Number(parts[item.account] || 0);
        if (!count) return;
        var height = (count / scale.top) * (box.bottom - box.top);
        totalHeight += height;
        var bar = svgEl("rect", {
          x: x.toFixed(1), y: (bottom - height).toFixed(1), width: barWidth.toFixed(1),
          height: Math.max(1, height).toFixed(1),
          fill: "var(" + colorOf[item.account] + ")",
          class: "chart-seg",
        });
        var caption = svgEl("title");
        caption.textContent = bucketLabel(bucket) + " · " + item.account + " · " + count + " 条";
        bar.appendChild(caption);
        svg.appendChild(bar);
        bottom -= height;
      });

      if (!totalHeight) {
        // 空桶留一条一像素的底：空桶和"这一格没数据"必须能分辨。
        svg.appendChild(svgEl("rect", {
          x: x.toFixed(1), y: (box.bottom - 1).toFixed(1), width: barWidth.toFixed(1), height: "1",
          class: "chart-bar empty",
        }));
      }

      // 只在本地日期发生变化的那一格标日期。原来判的是 `getHours() === 0`，
      // 但桶是从 epoch 按 12 小时切出来的，在 UTC+8 落在 08:00 / 20:00 —— 永远
      // 不是午夜，于是整条轴上只剩最左边一个日期。
      var date = new Date(bucket.start * 1000);
      var dayKey = date.getFullYear() + "-" + date.getMonth() + "-" + date.getDate();
      var newDay = index === 0 || dayKey !== previousDay;
      previousDay = dayKey;
      if (newDay) {
        var label = svgEl("text", {
          x: (x + barWidth / 2).toFixed(1), y: CHART_H - 6, class: "chart-tick", "text-anchor": "middle",
        });
        label.textContent = (date.getMonth() + 1) + "/" + date.getDate();
        svg.appendChild(label);
      }
    });

    // 均值线画在最上面：这是一张计数图，趋势比单根柱子重要。
    var avgY = box.bottom - (average / scale.top) * (box.bottom - box.top);
    svg.appendChild(svgEl("line", {
      x1: box.left, x2: box.right, y1: avgY.toFixed(1), y2: avgY.toFixed(1), class: "chart-avg",
    }));
  }

  function bucketLabel(bucket) {
    var start = new Date(bucket.start * 1000);
    var end = new Date(bucket.end * 1000);
    function pad(value) { return value < 10 ? "0" + value : String(value); }
    return (start.getMonth() + 1) + "/" + start.getDate() + " " + pad(start.getHours()) + ":00" +
      " – " + (end.getMonth() + 1) + "/" + end.getDate() + " " + pad(end.getHours()) + ":00";
  }

  function renderCharts(charts) {
    if (!charts) return;
    lastCharts = charts;
    drawCpu(charts);
    drawMemory(charts);
    drawMessages(charts);
  }

  function redrawCharts() {
    if (lastCharts) renderCharts(lastCharts);
  }

  function initCharts() {
    var seed = document.getElementById("chart-seed");
    if (!seed) return;
    try { renderCharts(JSON.parse(seed.textContent)); } catch (error) { /* 首次渲染失败不该阻断轮询 */ }

    // 五秒一次。状态卡片是两秒，但它们重画的是文字；这几张图每次要重建上百个
    // SVG 节点，没必要跟着那个节奏跑。
    setInterval(function () {
      fetch("/api/charts", { headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) { if (data) renderCharts(data); })
        .catch(function () { /* 网络抖动忽略，下次再试 */ });
    }, 5000);

    // 坐标系是按像素定的，所以宽度一变就得重画。去抖到 150ms：拖窗口会连发几十个
    // resize，每次都重建三张 SVG 没有意义。
    var resizeTimer = 0;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(redrawCharts, 150);
    });
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
    menuAnchor = null;
  }

  //: 打开菜单时锚点 <select> 的位置。滚动监听用它判断"下拉真的被滚走了没有"。
  var menuAnchor = null;

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
    menuAnchor = { select: select, left: rect.left, top: rect.top, width: rect.width };
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
    // 滚动时收起菜单——判据是"锚点有没有真的被滚走"。
    //
    // 这里原来是无条件 `addEventListener("scroll", closeSelectMenu, true)`：捕获阶段，
    // 页面上任何元素滚动都会触发。日志页正好是最坏情况：日志视图每来一行就自动
    // 滚到底，而菜单本身 `max-height: 260px; overflow: auto`，选项一多也有自己的
    // 滚动条。于是滚轮一转、或者新日志一到，scroll 事件就来了，菜单立刻收起——
    // 用户看到的现象正是"滚轮和侧条都用不了，一转就缩回去"。
    //
    // 现在只有两种情况会收起：
    //   1. 滚动发生在菜单之外，且锚点 <select> 的位置确实变了（页面真的滚了）；
    //   2. 视口尺寸变了（由 resize 处理）。
    // 菜单内部滚动、日志视图自动滚动，都不移动锚点，因此不再打断选择。
    window.addEventListener("scroll", function (event) {
      if (!selectMenu || selectMenu.hidden) return;
      if (selectMenu.contains(event.target)) return;
      if (!menuAnchor) { closeSelectMenu(); return; }
      var rect = menuAnchor.select.getBoundingClientRect();
      if (Math.abs(rect.left - menuAnchor.left) < 1 && Math.abs(rect.top - menuAnchor.top) < 1) return;
      closeSelectMenu();
    }, true);
  }

  // ------------------------------------------------------------- theme & chrome
  //
  // 主题、光标、减少动效都存在 localStorage，并且由 base.html 里的一小段内联脚本
  // 在首帧**之前**应用。这里只负责三件事：系统主题变化时跟随、设置页切换时生效、
  // 以及首次引导条的显示。

  /** 把 localStorage 里的选择应用到 <html> 上。
   *
   * 内联脚本已经做过一次（避免闪白），这里是可重复调用的版本：设置页切换主题时
   * 直接调用它，不需要刷新。
   */
  function applyTheme() {
    var choice = "system";
    try { choice = localStorage.getItem("qsick.theme") || "system"; } catch (_) {}
    var dark = choice === "dark" ||
      (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themeChoice = choice;
  }
  window.qsickApplyTheme = applyTheme;

  function initSystemTheme() {
    // 只在"跟随系统"时响应系统切换。选了明确的浅色/深色就该固定住，
    // 否则用户的选择会被操作系统的日落切换悄悄推翻。
    var media = window.matchMedia("(prefers-color-scheme: dark)");
    var listener = function () {
      if (document.documentElement.dataset.themeChoice === "system") applyTheme();
    };
    if (media.addEventListener) media.addEventListener("change", listener);
    else if (media.addListener) media.addListener(listener);
    // 设置页可能在别的标签页改过偏好
    window.addEventListener("storage", function (event) {
      if (event.key === "qsick.theme" || event.key === null) applyTheme();
    });
  }

  function initSetupBanner() {
    var banner = document.getElementById("setup-banner");
    if (!banner) return;
    // 模板按 cookie 决定要不要渲染；这里控制"这一刻是否显示"。
    // 默认 hidden 而不是默认显示：脚本没跑起来时不该一直挡着内容。
    banner.hidden = false;

    var dismiss = document.getElementById("setup-dismiss");
    if (!dismiss) return;
    dismiss.addEventListener("click", function () {
      banner.hidden = true;
      fetch("/api/security/setup-dismissed", { method: "POST" }).catch(function () {});
    });
  }

  /** 点当前所在的那一项，不刷新。
   *
   * 导航与二级导航里，当前页对应的链接是 `.active`。点它没有任何新东西可看，却会整页重载：
   * 日志页的滚动位置、日志缓冲、图表的采样历史全部回到起点。跳过它比"刷新一下也没坏处"更对——
   * 排查问题时最常做的动作就是不假思索地点一下当前页。
   *
   * 只拦纯左键、没有修饰键的点击：Ctrl+点击、中键新开标签页这些是用户明确要"再来一份"，
   * 拦下来反而破坏了浏览器的常规行为。
   */
  function initNavNoop() {
    document.addEventListener("click", function (event) {
      if (event.defaultPrevented) return;
      if (event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      var link = event.target.closest ? event.target.closest("a") : null;
      if (!link) return;
      // 只管家里的两个导航条，别把正文里的普通链接也管住。
      if (!link.closest(".nav") && !link.closest(".subnav")) return;
      if (!link.classList.contains("active")) return;
      event.preventDefault();
    });
  }

  // ------------------------------------------------------------- 缓存清理
  //
  // 饼图与消息数图共用同一套 SVG 手法（像素坐标系、1-2-5 步长），一致性比新写法更值钱。
  // 数据来自 /api/cache：清理后服务端会把新的尺寸一起返回，页面不必自己猜剩下的量。

  var CACHE_COLORS = ["--sky", "--ok", "--violet", "--amber", "--pink", "--teal", "--faint"];

  function cacheColor(index) {
    return CACHE_COLORS[index % CACHE_COLORS.length];
  }

  function drawCachePie(items) {
    var svg = $("#cache-chart");
    if (!svg) return;
    clearSvg(svg);

    var total = items.reduce(function (sum, item) { return sum + Number(item.bytes || 0); }, 0);
    var totalLabel = $("#cache-total");
    if (totalLabel) totalLabel.textContent = humanBytes(total);

    var legend = $("#cache-legend");
    if (legend) legend.innerHTML = "";

    if (!total) {
      // 全空时留一圈浅色，比画一个"什么都没有"的空白 SVG 更容易理解。
      svg.appendChild(svgEl("circle", {
        cx: 90, cy: 90, r: 58, fill: "none", stroke: "var(--line)", "stroke-width": 24,
      }));
      var hint = svgEl("text", {
        x: 90, y: 95, class: "chart-tick", "text-anchor": "middle", "font-size": "12",
      });
      hint.textContent = "无缓存";
      svg.appendChild(hint);
      return;
    }

    var radius = 58;
    var circumference = 2 * Math.PI * radius;
    var offset = 0;

    items.forEach(function (item, index) {
      var share = Number(item.bytes || 0) / total;
      if (share <= 0) return;
      var length = share * circumference;
      var arc = svgEl("circle", {
        cx: 90, cy: 90, r: radius, fill: "none",
        stroke: "var(" + cacheColor(index) + ")",
        "stroke-width": 24,
        "stroke-dasharray": length.toFixed(2) + " " + (circumference - length).toFixed(2),
        "stroke-dashoffset": (-offset).toFixed(2),
        // 从 12 点方向开始，和内存占用那张图一致。
        transform: "rotate(-90 90 90)",
        class: "cache-arc",
      });
      var caption = svgEl("title");
      caption.textContent = item.label + " · " + item.human + " · " + (share * 100).toFixed(1) + "%";
      arc.appendChild(caption);
      svg.appendChild(arc);
      offset += length;

      if (legend) {
        var row = document.createElement("li");
        row.dataset.id = item.id;
        row.innerHTML =
          '<i class="sw" style="background:var(' + cacheColor(index) + ')"></i>' +
          '<span class="pie-name">' + escapeHtml(item.label) + '</span>' +
          '<span class="pie-size">' + escapeHtml(item.human) + '</span>' +
          '<span class="pie-share">' + (share * 100).toFixed(1) + '%</span>';
        legend.appendChild(row);
      }
    });

    // 中心不写总数：卡片标题右侧已经有一个，"合计"两个字只是把同一个数字说两遍。
    // 环形图中间留空反而更像一个仪表，视线会落到弧段上。
  }

  function humanBytes(size) {
    var value = Number(size) || 0;
    if (value < 1024) return Math.round(value) + " B";
    var units = ["KB", "MB", "GB"];
    for (var index = 0; index < units.length; index += 1) {
      value /= 1024;
      if (value < 1024 || index === units.length - 1) {
        return (units[index] === "KB" ? Math.round(value) : value.toFixed(1)) + " " + units[index];
      }
    }
    return Math.round(value) + " B";
  }

  function renderCachePanel(items) {
    drawCachePie(items);
    // 表格里的尺寸也要跟着刷新，否则清完还显示旧数字。
    var list = $("#cache-list");
    if (!list) return;
    items.forEach(function (item) {
      var row = list.querySelector('.cache-row[data-id="' + item.id + '"]');
      if (!row) return;
      var size = row.querySelector("[data-size]");
      if (size) size.textContent = item.human;
      row.classList.toggle("is-empty", !Number(item.bytes));
      var dot = row.querySelector(".cache-dot");
      if (dot) dot.style.background = "var(" + cacheColor(items.indexOf(item)) + ")";
    });
  }

  function initCachePanel() {
    var seed = document.getElementById("cache-seed");
    if (!seed) return;
    var items;
    try { items = JSON.parse(seed.textContent || "[]"); } catch (_) { items = []; }
    renderCachePanel(items);

    async function clear(id, label) {
      if (!confirm("清理「" + label + "」？这会立刻释放空间，且不可撤销。")) return;
      try {
        var response = await fetch("/api/cache/clear", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: id }),
        });
        var result = await response.json();
        if (result.ok) {
          renderCachePanel(result.items || []);
          if (typeof window.qsickToast === "function") {
            window.qsickToast("已释放 " + result.freed_human, "ok");
          }
        } else if (typeof window.qsickToast === "function") {
          window.qsickToast(result.error || "清理失败", "err");
        }
      } catch (error) {
        if (typeof window.qsickToast === "function") window.qsickToast("清理失败：" + error.message, "err");
      }
    }

    document.querySelectorAll(".cache-clear").forEach(function (button) {
      button.addEventListener("click", function () {
        var row = button.closest(".cache-row");
        var name = row ? row.querySelector("strong").textContent : button.dataset.id;
        clear(button.dataset.id, name);
      });
    });

    var all = document.getElementById("cache-clear-all");
    if (all) {
      all.addEventListener("click", function () {
        if (!confirm("清理全部缓存？这会立刻释放空间，且不可撤销。")) return;
        clear("all", "全部缓存");
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    initNavNoop();
    initLogs();
    initActions();
    initStatusFallback();
    initCharts();
    initSelectMenus();
    initCachePanel();
    initSystemTheme();
    initSetupBanner();
  });
})();
