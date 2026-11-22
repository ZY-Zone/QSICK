(function () {
  "use strict";

  var state = { q: "", sort: "published", order: "desc", client: "", page: 1, per: 24, pages: 1, total: 0 };
  var grid = document.getElementById("grid");
  var empty = document.getElementById("empty");
  var stats = document.getElementById("stats");
  var pageinfo = document.getElementById("pageinfo");
  var toastBox = document.getElementById("toast");

  function toast(text) {
    toastBox.textContent = text;
    toastBox.hidden = false;
    clearTimeout(toastBox._timer);
    toastBox._timer = setTimeout(function () { toastBox.hidden = true; }, 2600);
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
    meta.appendChild(el("span", "", "发布 " + formatDate(item.published_at)));
    meta.appendChild(el("span", "", "更新 " + formatDate(item.updated_at)));
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
    var repo = el("a", "ghost", "仓库");
    repo.href = item.repo_url;
    repo.target = "_blank";
    repo.rel = "noreferrer noopener";
    var install = el("button", "", "安装 / 更新");
    install.addEventListener("click", function () { showInstall(item); });
    actions.appendChild(like);
    actions.appendChild(repo);
    actions.appendChild(install);
    article.appendChild(actions);
    return article;
  }

  function render() {
    grid.innerHTML = "";
    empty.hidden = state.total !== 0;
    grid.hidden = state.total === 0;
    stats.textContent = state.total + " 个插件" + (state.client ? " · 客户端 " + state.client : "");
    pageinfo.textContent = "第 " + state.page + " / " + state.pages + " 页";
    document.getElementById("prev").disabled = state.page <= 1;
    document.getElementById("next").disabled = state.page >= state.pages;
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

  function showInstall(item) {
    var dialog = document.getElementById("detail");
    document.getElementById("detail-title").textContent = "安装 / 更新 " + item.name;
    document.getElementById("detail-desc").textContent = item.description || "";
    document.getElementById("detail-repo").href = item.repo_url;
    var input = document.getElementById("detail-url");
    input.value = item.archive_url || item.repo_url;
    dialog.showModal();
    document.getElementById("detail-copy").onclick = function () {
      navigator.clipboard.writeText(input.value).then(function () { toast("下载地址已复制"); });
    };
    document.getElementById("detail-close").onclick = function () { dialog.close(); };
  }

  var search = document.getElementById("search");
  var timer = null;
  search.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      state.q = search.value.trim();
      state.page = 1;
      load();
    }, 250);
  });
  document.getElementById("sort").addEventListener("change", function (event) {
    state.sort = event.target.value;
    state.page = 1;
    load();
  });
  document.getElementById("client").addEventListener("change", function (event) {
    state.client = event.target.value.trim();
    load();
  });
  document.getElementById("order").addEventListener("click", function (event) {
    state.order = state.order === "desc" ? "asc" : "desc";
    event.target.textContent = state.order === "desc" ? "↓ 倒序" : "↑ 正序";
    state.page = 1;
    load();
  });
  document.getElementById("reload").addEventListener("click", load);
  document.getElementById("prev").addEventListener("click", function () {
    if (state.page > 1) { state.page -= 1; load(); }
  });
  document.getElementById("next").addEventListener("click", function () {
    if (state.page < state.pages) { state.page += 1; load(); }
  });

  load();
})();
