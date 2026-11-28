(function () {
  "use strict";

  var state = { q: "", sort: "published", order: "desc", client: "", page: 1, per: 24, pages: 1, total: 0 };
  var grid = document.getElementById("grid");
  var empty = document.getElementById("empty");
  var stats = document.getElementById("stats");
  var pageinfo = document.getElementById("pageinfo");
  var toastBox = document.getElementById("toast");

  function $(id) { return document.getElementById(id); }

  function toast(text) {
    toastBox.textContent = text;
    toastBox.hidden = false;
    clearTimeout(toastBox._timer);
    toastBox._timer = setTimeout(function () { toastBox.hidden = true; }, 2800);
  }

  function voter() {
    var key = "qsick-market-voter";
    var value = localStorage.getItem(key);
    if (!value) {
      value = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(key, value);
    }
    return value;
  }

  function api(path, options) {
    return fetch("api/" + path, options).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.error || response.statusText);
        return body;
      });
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatDate(value) {
    if (!value) return "—";
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value).slice(0, 10);
    return date.toISOString().slice(0, 10);
  }

  function formatBytes(value) {
    var size = Number(value) || 0;
    if (!size) return "—";
    if (size < 1024) return size + " B";
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KiB";
    return (size / (1024 * 1024)).toFixed(2) + " MiB";
  }

  function fact(label, value, code) {
    var item = el("div", "fact");
    item.appendChild(el("b", "", label));
    var node = code ? el("code", "", value) : el("span", "", value);
    item.appendChild(node);
    return item;
  }

  function card(item) {
    var article = el("article", "card");
    var head = el("div", "card-head");
    var icon = el("div", "icon");
    if (item.icon_url) {
      var img = document.createElement("img");
      img.src = item.icon_url;
      img.alt = "";
      img.loading = "lazy";
      img.onerror = function () { img.remove(); };
      icon.appendChild(img);
    } else {
      icon.textContent = (item.name || "?").slice(0, 1).toUpperCase();
    }
    var title = el("div", "title");
    title.appendChild(el("strong", "", item.name));
    title.appendChild(el("code", "", item.id));
    head.appendChild(icon);
    head.appendChild(title);
    head.appendChild(el("span", "badge " + (item.matches_client ? "ok" : "warn"),
      item.matches_client ? "匹配当前客户端" : "版本不匹配"));
    article.appendChild(head);

    article.appendChild(el("div", "desc", item.description || "（暂无简介）"));

    var meta = el("div", "meta");
    meta.appendChild(el("span", "", "v" + (item.version || "?")));
    meta.appendChild(el("span", "", "★ " + item.stars));
    meta.appendChild(el("span", "", "👁 " + item.views));
    meta.appendChild(el("span", "", "♥ " + item.likes));
    meta.appendChild(el("span", "", "更新 " + formatDate(item.updated_at)));
    if (item.archive_size) meta.appendChild(el("span", "", "包 " + formatBytes(item.archive_size)));
    article.appendChild(meta);

    var actions = el("div", "actions");
    var like = el("button", "ghost like", "点赞");
    like.addEventListener("click", function () {
      api("plugins/" + encodeURIComponent(item.id) + "/like", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voter: voter() })
      }).then(function (result) {
        item.likes = result.likes;
        like.textContent = result.liked ? "已赞 " + result.likes : "点赞";
        like.classList.toggle("liked", result.liked);
        toast(result.liked ? "已点赞" : "已取消点赞");
      }).catch(function (error) { toast(String(error.message || error)); });
    });
    var detail = el("button", "", "查看 / 安装");
    detail.addEventListener("click", function () { showDetail(item); });
    actions.appendChild(like);
    actions.appendChild(detail);
    article.appendChild(actions);
    return article;
  }

  function render() {
    empty.hidden = state.total !== 0;
    grid.hidden = state.total === 0;
    stats.textContent = state.total + " 个插件" + (state.client ? " · 客户端 " + state.client : "");
    pageinfo.textContent = "第 " + state.page + " / " + state.pages + " 页";
    $("prev").disabled = state.page <= 1;
    $("next").disabled = state.page >= state.pages;
  }

  function load() {
    var params = new URLSearchParams({
      q: state.q,
      sort: state.sort,
      order: state.order,
      client: state.client,
      page: String(state.page),
      per: String(state.per)
    });
    api("plugins?" + params.toString()).then(function (data) {
      state.total = data.total;
      state.pages = data.pages;
      state.page = data.page;
      grid.innerHTML = "";
      data.items.forEach(function (item) { grid.appendChild(card(item)); });
      render();
    }).catch(function (error) {
      grid.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML = "";
      empty.appendChild(el("strong", "", "加载失败"));
      empty.appendChild(el("p", "", String(error.message || error)));
      stats.textContent = "—";
    });
  }

  function showDetail(item) {
    var dialog = $("detail");
    $("detail-title").textContent = item.name + " · 安装";
    $("detail-desc").textContent = item.description || "";
    var facts = $("detail-facts");
    facts.innerHTML = "";
    facts.appendChild(fact("插件 ID", item.id, true));
    facts.appendChild(fact("版本", item.version || "—"));
    facts.appendChild(fact("兼容", [item.qsick_min, item.qsick_max].filter(Boolean).join(" ~ ") || "未声明"));
    facts.appendChild(fact("归档大小", formatBytes(item.archive_size)));
    facts.appendChild(fact("更新时间", formatDate(item.updated_at)));
    facts.appendChild(fact("SHA-256", item.archive_sha256 || "—", true));
    var input = $("detail-url");
    input.value = item.archive_url || item.repo_url || "";
    var download = $("detail-download");
    download.hidden = !item.archive_url;
    if (item.archive_url) download.href = item.archive_url; else download.removeAttribute("href");
    var repo = $("detail-repo");
    repo.hidden = !item.repo_url;
    if (item.repo_url) repo.href = item.repo_url; else repo.removeAttribute("href");
    dialog.showModal();
    $("detail-copy").onclick = function () {
      if (!input.value) return;
      navigator.clipboard.writeText(input.value).then(function () { toast("安装地址已复制"); });
    };
    $("detail-close").onclick = function () { dialog.close(); };
  }

  /* ------------------------------------------------------------------ 上传 */
  var uploadDialog = $("upload");
  var uploadStatus = $("upload-status");

  function setUploadStatus(text, isError) {
    uploadStatus.hidden = !text;
    uploadStatus.textContent = text || "";
    uploadStatus.classList.toggle("err", !!isError);
  }

  function openUpload() {
    ["upload-token", "upload-icon", "upload-repo", "upload-min", "upload-max"].forEach(function (id) {
      $(id).value = "";
    });
    $("upload-file").value = "";
    setUploadStatus("");
    uploadDialog.showModal();
  }

  function upload() {
    var token = $("upload-token").value.trim();
    var file = $("upload-file").files[0];
    if (!token) { setUploadStatus("请填写管理 Token", true); return; }
    if (!file) { setUploadStatus("请选择 .zip 插件包", true); return; }
    if (!/\.zip$/i.test(file.name)) { setUploadStatus("插件包必须是 .zip", true); return; }
    if (file.size > 32 * 1024 * 1024) { setUploadStatus("插件包不能超过 32 MB", true); return; }
    var form = new FormData();
    form.append("file", file);
    [
      ["icon_url", "upload-icon"],
      ["repo_url", "upload-repo"],
      ["qsick_min", "upload-min"],
      ["qsick_max", "upload-max"]
    ].forEach(function (pair) {
      var value = $(pair[1]).value.trim();
      if (value) form.append(pair[0], value);
    });
    var submit = $("upload-submit");
    submit.disabled = true;
    setUploadStatus("上传中…");
    fetch("api/plugins/upload", {
      method: "POST",
      headers: { "X-Market-Token": token },
      body: form
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.error || response.statusText);
        return body;
      });
    }).then(function (body) {
      uploadDialog.close();
      toast("上传成功：" + body.plugin.name);
      state.page = 1;
      load();
      showDetail(body.plugin);
    }).catch(function (error) {
      setUploadStatus(String(error.message || error), true);
    }).finally(function () {
      submit.disabled = false;
    });
  }

  /* --------------------------------------------------------------- 筛选 / 分页 */
  var search = $("search");
  var timer = null;
  search.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      state.q = search.value.trim();
      state.page = 1;
      load();
    }, 250);
  });
  $("sort").addEventListener("change", function (event) {
    state.sort = event.target.value;
    state.page = 1;
    load();
  });
  $("client").addEventListener("change", function (event) {
    state.client = event.target.value.trim();
    load();
  });
  $("order").addEventListener("click", function (event) {
    state.order = state.order === "desc" ? "asc" : "desc";
    event.target.textContent = state.order === "desc" ? "↓ 倒序" : "↑ 正序";
    state.page = 1;
    load();
  });
  $("reload").addEventListener("click", load);
  $("prev").addEventListener("click", function () {
    if (state.page > 1) { state.page -= 1; load(); }
  });
  $("next").addEventListener("click", function () {
    if (state.page < state.pages) { state.page += 1; load(); }
  });

  $("upload-open").addEventListener("click", openUpload);
  $("upload-close").addEventListener("click", function () { uploadDialog.close(); });
  $("upload-submit").addEventListener("click", upload);

  load();
})();
