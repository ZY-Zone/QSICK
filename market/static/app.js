(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };
  var state = {
    q: "", sort: "published", order: "desc", client: "",
    page: 1, per: 24, pages: 1, total: 0,
    allItems: [], items: [], loading: false,
  };
  var uploadFile = null;
  var uploading = false;

  function icon(name) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + name);
    svg.appendChild(use);
    return svg;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function toast(text, isError) {
    var box = $("toast");
    box.textContent = text;
    box.classList.toggle("err", !!isError);
    box.classList.add("show");
    clearTimeout(box._timer);
    box._timer = setTimeout(function () { box.classList.remove("show"); }, 3000);
  }

  function api(path, options) {
    return fetch("api/" + path, options).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.error || response.statusText);
        return body;
      });
    });
  }

  function formatBytes(value) {
    var size = Number(value) || 0;
    if (!size) return "—";
    if (size < 1024) return size + " B";
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KiB";
    return (size / (1024 * 1024)).toFixed(2) + " MiB";
  }

  function formatDate(value) {
    if (!value) return "—";
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value).slice(0, 10);
    var diff = Date.now() - date.getTime();
    if (diff >= 0 && diff < 86400000) return "今天";
    if (diff >= 0 && diff < 86400000 * 7) return Math.floor(diff / 86400000) + " 天前";
    return date.toISOString().slice(0, 10);
  }

  function formatNumber(value) {
    var number = Number(value) || 0;
    return number.toLocaleString("zh-CN");
  }

  function hueOf(text) {
    var hash = 0;
    for (var i = 0; i < String(text || "").length; i++) hash = (hash * 31 + String(text).charCodeAt(i)) % 360;
    return hash;
  }

  function pluginIcon(item, className) {
    var box = el("div", className || "plugin-icon");
    var hue = hueOf(item.id);
    box.style.background = "linear-gradient(135deg, hsl(" + hue + " 90% 92%), hsl(" + ((hue + 28) % 360) + " 90% 98%))";
    box.style.color = "hsl(" + hue + " 55% 40%)";
    var fallback = (item.name || item.id || "?").trim().slice(0, 1).toUpperCase();
    box.textContent = fallback;
    if (item.icon_url) {
      var image = document.createElement("img");
      image.src = item.icon_url;
      image.alt = "";
      image.loading = "lazy";
      image.onerror = function () { image.remove(); };
      box.appendChild(image);
    }
    return box;
  }

  function badge(text, kind) {
    return el("span", "badge " + (kind || ""), text);
  }

  function stat(label, value, code) {
    var box = el("div", "fact");
    box.appendChild(el("b", "", label));
    box.appendChild(code ? el("code", "", value) : el("span", "", value));
    return box;
  }

  function setEmpty(message, filtered) {
    $("empty").hidden = false;
    $("emptyText").textContent = message;
    $("emptyClear").hidden = !filtered;
  }

  function clearEmpty() {
    $("empty").hidden = true;
    $("emptyClear").hidden = true;
  }

  /* --------------------------------------------------------------- 列表与卡片 */
  function card(item, index) {
    var article = el("article", "card");
    article.style.animationDelay = Math.min(index * 26, 260) + "ms";

    var head = el("div", "card-head");
    head.appendChild(pluginIcon(item, "plugin-icon"));
    var title = el("div", "card-title");
    title.appendChild(el("strong", "", item.name || item.id));
    title.appendChild(el("code", "", item.id));
    head.appendChild(title);
    var source = item.archive_url && item.archive_url.indexOf("/files/") >= 0;
    var status = state.client
      ? badge(item.matches_client ? "兼容当前版本" : "版本不匹配", item.matches_client ? "ok" : "warn")
      : badge(source ? "站内归档" : "仓库插件", source ? "ok" : "");
    head.appendChild(status);
    article.appendChild(head);

    article.appendChild(el("p", "card-desc", item.description || "这个插件还没有写简介。"));
    var meta = el("div", "card-meta");
    meta.appendChild(el("span", "", "v" + (item.version || "?")));
    meta.appendChild(el("span", "", formatDate(item.updated_at) + "更新"));
    meta.appendChild(el("span", "", "★ " + formatNumber(item.stars)));
    meta.appendChild(el("span", "", "👁 " + formatNumber(item.views)));
    meta.appendChild(el("span", "", "♥ " + formatNumber(item.likes)));
    if (item.archive_size) meta.appendChild(el("span", "", formatBytes(item.archive_size)));
    article.appendChild(meta);

    var actions = el("div", "card-actions");
    var like = el("button", "like-btn" + (item.liked ? " liked" : ""));
    like.type = "button";
    like.appendChild(icon("i-heart"));
    like.appendChild(document.createTextNode(String(item.likes || 0)));
    like.title = "点赞";
    like.addEventListener("click", function (event) {
      event.stopPropagation();
      toggleLike(item, like);
    });
    var view = el("button", "btn btn-primary view-btn", "查看 / 安装");
    view.type = "button";
    view.appendChild(icon("i-download"));
    view.addEventListener("click", function () { showDetail(item); });
    actions.appendChild(like);
    actions.appendChild(view);
    article.appendChild(actions);
    return article;
  }

  function toggleLike(item, button) {
    var voter = localStorage.getItem("qsick-market-voter");
    if (!voter) {
      voter = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem("qsick-market-voter", voter);
    }
    api("plugins/" + encodeURIComponent(item.id) + "/like", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voter: voter })
    }).then(function (result) {
      var delta = result.likes - (item.likes || 0);
      item.likes = result.likes;
      button.classList.toggle("liked", result.liked);
      button.lastChild.textContent = String(result.likes);
      toast(result.liked ? "已点赞" : "已取消点赞");
      adjustLikes(delta);
    }).catch(function (error) {
      toast(String(error.message || error), true);
    });
  }

  function adjustLikes(delta) {
    var node = $("statLikes");
    var value = Math.max(0, Number(String(node.textContent).replace(/[^\d]/g, "")) + delta);
    node.textContent = formatNumber(value);
  }

  function renderGrid(items) {
    var grid = $("grid");
    grid.innerHTML = "";
    if (!items.length) {
      grid.hidden = true;
      var filtered = !!(state.q || state.client);
      setEmpty(filtered ? "没有找到匹配的插件，试试其他关键词或清除筛选。" : "还没有插件。发布第一个插件，让这里热闹起来。", filtered);
      return;
    }
    grid.hidden = false;
    clearEmpty();
    items.forEach(function (item, index) { grid.appendChild(card(item, index)); });
  }

  function renderPager() {
    $("pageinfo").textContent = state.pages > 0 ? "第 " + state.page + " / " + state.pages + " 页" : "—";
    $("prev").disabled = state.page <= 1;
    $("next").disabled = state.page >= state.pages;
    document.querySelector(".pager").style.display = state.total > 0 ? "flex" : "none";
  }

  function updateHero() {
    var items = state.allItems;
    var views = 0, likes = 0;
    items.forEach(function (item) { views += Number(item.views) || 0; likes += Number(item.likes) || 0; });
    animateNumber($("statTotal"), items.length);
    animateNumber($("statViews"), views);
    animateNumber($("statLikes"), likes);
    $("listSub").textContent = state.total + " 个插件" +
      (state.q ? " · 搜索「" + state.q + "」" : "") +
      (state.client ? " · 客户端 " + state.client : "");
  }

  function animateNumber(node, target) {
    var start = Number(String(node.textContent).replace(/[^\d]/g, "")) || 0;
    if (!target) { node.textContent = "0"; return; }
    var started = performance.now();
    var duration = 520;
    function step(now) {
      var t = Math.min(1, (now - started) / duration);
      var eased = 1 - Math.pow(1 - t, 3);
      node.textContent = formatNumber(Math.round(start + (target - start) * eased));
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function setLoading(loading) {
    state.loading = loading;
    $("skeleton").hidden = !loading;
    if (loading) {
      $("grid").innerHTML = "";
      $("grid").hidden = true;
      clearEmpty();
    }
  }

  function load() {
    if (state.loading) return;
    setLoading(true);
    var params = new URLSearchParams({
      q: state.q, sort: state.sort, order: state.order, client: state.client,
      page: String(state.page), per: String(state.per)
    });
    var listPromise = api("plugins?" + params.toString());
    var allPromise = api("plugins/all").catch(function () { return null; });
    Promise.all([listPromise, allPromise]).then(function (results) {
      var data = results[0];
      var all = results[1];
      state.total = data.total;
      state.pages = data.pages;
      state.page = data.page;
      state.items = data.items;
      state.allItems = all && Array.isArray(all.items) ? all.items : [];
      if (!state.allItems.length && data.total > 0) state.allItems = data.items;
      updateHero();
      renderGrid(data.items);
      renderPager();
      setLoading(false);
    }).catch(function (error) {
      setLoading(false);
      $("grid").hidden = true;
      setEmpty("加载失败：" + String(error.message || error), true);
      $("emptyClear").hidden = true;
      $("statTotal").textContent = "—";
      $("statViews").textContent = "—";
      $("statLikes").textContent = "—";
      $("listSub").textContent = "插件列表暂时不可用";
      $("pageinfo").textContent = "—";
    });
  }

  /* ---------------------------------------------------------------- 详情弹窗 */
  function fillDetailIcon(item) {
    var box = $("detail-icon");
    box.innerHTML = "";
    var iconNode = pluginIcon(item, "modal-icon");
    box.replaceWith(iconNode);
    iconNode.id = "detail-icon";
  }

  function showDetail(item) {
    $("detail-title").textContent = item.name || item.id;
    $("detail-id").textContent = item.id;
    $("detail-desc").textContent = item.description || "这个插件还没有写简介。";
    fillDetailIcon(item);

    var badges = $("detail-badges");
    badges.innerHTML = "";
    badges.appendChild(badge("v" + (item.version || "?")));
    if (item.archive_url && item.archive_url.indexOf("/files/") >= 0) badges.appendChild(badge("站内归档", "ok"));
    else badges.appendChild(badge("外部仓库"));
    if (state.client) badges.appendChild(badge(item.matches_client ? "兼容当前版本" : "版本不匹配", item.matches_client ? "ok" : "warn"));

    var facts = $("detail-facts");
    facts.innerHTML = "";
    facts.appendChild(stat("兼容 QSICK", [item.qsick_min, item.qsick_max].filter(Boolean).join(" ~ ") || "未声明"));
    facts.appendChild(stat("最后更新", formatDate(item.updated_at)));
    facts.appendChild(stat("归档大小", formatBytes(item.archive_size)));
    facts.appendChild(stat("SHA-256", item.archive_sha256 || "—", true));

    var stats = $("detail-stats");
    stats.innerHTML = "";
    [["★", item.stars], ["浏览", item.views], ["点赞", item.likes], ["发布", formatDate(item.published_at)]].forEach(function (pair) {
      var span = el("span");
      span.appendChild(el("b", "", String(pair[1])));
      span.appendChild(document.createTextNode(" " + pair[0]));
      stats.appendChild(span);
    });

    var urlInput = $("detail-url");
    urlInput.value = item.archive_url || item.repo_url || "";
    var download = $("detail-download");
    download.hidden = !item.archive_url;
    if (item.archive_url) download.href = item.archive_url; else download.removeAttribute("href");
    var repo = $("detail-repo");
    repo.hidden = !item.repo_url;
    if (item.repo_url) repo.href = item.repo_url; else repo.removeAttribute("href");

    var dialog = $("detail");
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }

  function copyText(text, message) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast(message); }).catch(function () { legacyCopy(text, message); });
    } else {
      legacyCopy(text, message);
    }
  }

  function legacyCopy(text, message) {
    var area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); toast(message); } catch (e) { toast("复制失败", true); }
    document.body.removeChild(area);
  }

  /* ---------------------------------------------------------------- 上传弹窗 */
  function setFile(file) {
    if (!file) {
      uploadFile = null;
      $("file-chip").hidden = true;
      $("dropzone").hidden = false;
      $("upload-status").hidden = true;
      return;
    }
    if (!/\.zip$/i.test(file.name)) { toast("只接受 .zip 插件包", true); return; }
    if (file.size > 32 * 1024 * 1024) { toast("插件包不能超过 32 MB", true); return; }
    uploadFile = file;
    $("file-name").textContent = file.name;
    $("file-size").textContent = formatBytes(file.size) + " · zip";
    $("file-chip").hidden = false;
    $("dropzone").hidden = true;
    $("upload-status").hidden = true;
  }

  function openUpload() {
    uploadFile = null;
    ["upload-icon", "upload-repo", "upload-min", "upload-max"].forEach(function (id) { $(id).value = ""; });
    $("upload-file").value = "";
    $("file-chip").hidden = true;
    $("dropzone").hidden = false;
    $("upload-status").hidden = true;
    $("upload-progress").hidden = true;
    $("progress-bar").style.width = "0%";
    $("upload-submit").disabled = false;
    $("upload").showModal();
  }

  function uploadStatus(text, isError) {
    var node = $("upload-status");
    node.hidden = !text;
    node.textContent = text || "";
    node.classList.toggle("err", !!isError);
  }

  function submitUpload() {
    if (uploading) return;
    if (!uploadFile) { uploadStatus("请选择 .zip 插件包", true); return; }
    var form = new FormData();
    form.append("file", uploadFile);
    [["upload-icon", "icon_url"], ["upload-repo", "repo_url"], ["upload-min", "qsick_min"], ["upload-max", "qsick_max"]].forEach(function (pair) {
      var value = $(pair[0]).value.trim();
      if (value) form.append(pair[1], value);
    });
    uploading = true;
    $("upload-submit").disabled = true;
    $("upload-progress").hidden = false;
    uploadStatus("");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "api/plugins/upload");
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      var percent = Math.round((event.loaded / event.total) * 100);
      $("progress-bar").style.width = percent + "%";
      $("progress-text").textContent = percent + "% · 正在上传";
    };
    xhr.onload = function () {
      uploading = false;
      $("upload-submit").disabled = false;
      var body;
      try { body = JSON.parse(xhr.responseText); } catch (e) { body = { error: "服务器返回了无法解析的响应" }; }
      if (xhr.status < 200 || xhr.status >= 300) {
        uploadStatus(String(body.error || ("上传失败（HTTP " + xhr.status + "）")), true);
        $("upload-progress").hidden = true;
        return;
      }
      $("progress-bar").style.width = "100%";
      $("progress-text").textContent = "已完成";
      var plugin = body.plugin;
      var name = plugin ? plugin.name : (uploadFile ? uploadFile.name : "插件");
      $("upload").close();
      toast("已提交审核：" + name + "，通过后会出现在市场");
      uploadFile = null;
      state.page = 1;
      state.q = "";
      $("search").value = "";
      load();
    };
    xhr.onerror = function () {
      uploading = false;
      $("upload-submit").disabled = false;
      $("upload-progress").hidden = true;
      uploadStatus("网络错误，上传没有完成", true);
    };
    xhr.send(form);
  }

  /* ------------------------------------------------------------------ 事件 */
  $("themeToggle").addEventListener("click", function () {
    var root = document.documentElement;
    var next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("qsick-market-theme", next); } catch (e) {}
  });

  var searchTimer = null;
  $("search").addEventListener("input", function (event) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      state.q = event.target.value.trim();
      state.page = 1;
      load();
    }, 260);
  });
  var sortTrigger = $("sortTrigger");
  var sortPop = $("sortPop");
  function closeSortMenu() {
    sortPop.classList.remove("open");
    sortTrigger.setAttribute("aria-expanded", "false");
  }
  sortTrigger.addEventListener("click", function (event) {
    event.stopPropagation();
    var open = sortPop.classList.toggle("open");
    sortTrigger.setAttribute("aria-expanded", open ? "true" : "false");
  });
  $$(".menu-item", sortPop).forEach(function (item) {
    item.addEventListener("click", function () {
      state.sort = item.getAttribute("data-value");
      $("sortLabel").textContent = item.textContent;
      $$(".menu-item", sortPop).forEach(function (other) { other.classList.toggle("active", other === item); });
      closeSortMenu();
      state.page = 1;
      load();
    });
  });
  document.addEventListener("click", function (event) {
    if (!sortPop.contains(event.target) && event.target !== sortTrigger && !sortTrigger.contains(event.target)) closeSortMenu();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeSortMenu();
  });
  $("order").addEventListener("click", function () {
    state.order = state.order === "desc" ? "asc" : "desc";
    $("order").textContent = state.order === "desc" ? "↓" : "↑";
    $("order").title = state.order === "desc" ? "当前倒序，点击切换正序" : "当前正序，点击切换倒序";
    state.page = 1;
    load();
  });
  var clientTimer = null;
  $("client").addEventListener("input", function () {
    clearTimeout(clientTimer);
    clientTimer = setTimeout(function () {
      state.client = $("client").value.trim();
      state.page = 1;
      load();
    }, 320);
  });
  $("reload").addEventListener("click", load);
  $("prev").addEventListener("click", function () { if (state.page > 1) { state.page -= 1; load(); } });
  $("next").addEventListener("click", function () { if (state.page < state.pages) { state.page += 1; load(); } });
  $("emptyClear").addEventListener("click", function () {
    state.q = "";
    state.client = "";
    $("search").value = "";
    $("client").value = "";
    state.page = 1;
    load();
  });

  $$("[data-open-upload]").forEach(function (button) { button.addEventListener("click", openUpload); });
  $("upload-close").addEventListener("click", function () { $("upload").close(); });
  $("upload-cancel").addEventListener("click", function () { $("upload").close(); });
  $("upload-submit").addEventListener("click", submitUpload);

  var dropzone = $("dropzone");
  dropzone.addEventListener("click", function () { $("upload-file").click(); });
  dropzone.addEventListener("keydown", function (event) {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("upload-file").click(); }
  });
  $("upload-file").addEventListener("change", function () { setFile($("upload-file").files[0]); });
  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) { event.preventDefault(); dropzone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) { event.preventDefault(); dropzone.classList.remove("dragover"); });
  });
  dropzone.addEventListener("drop", function (event) { setFile(event.dataTransfer.files[0]); });
  $("file-clear").addEventListener("click", function () { setFile(null); });

  $("detail-close").addEventListener("click", function () { $("detail").close(); });
  $("detail-copy").addEventListener("click", function () { copyText($("detail-url").value, "安装地址已复制"); });

  [$("detail"), $("upload")].forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target !== dialog) return;
      var rect = dialog.getBoundingClientRect();
      var inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) dialog.close();
    });
  });

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && (event.key === "k" || event.key === "K")) {
      event.preventDefault();
      $("search").focus();
      $("search").select();
    }
  });

  load();
})();
