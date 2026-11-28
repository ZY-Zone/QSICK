(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = { items: [], filter: "pending", q: "", editing: null };
  var labels = { pending: "待审核", approved: "已上架", rejected: "已拒绝", removed: "已下架" };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
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
        if (response.status === 401) {
          showLogin();
          throw new Error(body.error || "请先登录");
        }
        if (!response.ok) throw new Error(body.error || response.statusText);
        return body;
      });
    });
  }

  function setLoginStatus(text, isError) {
    var node = $("loginStatus");
    node.hidden = !text;
    node.textContent = text || "";
    node.classList.toggle("err", !!isError);
  }

  function showLogin() {
    $("loginView").hidden = false;
    $("dashboard").hidden = true;
    $("logout").hidden = true;
  }

  function showDashboard() {
    $("loginView").hidden = true;
    $("dashboard").hidden = false;
    $("logout").hidden = false;
  }

  function formatDate(value) {
    if (!value) return "—";
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value).slice(0, 10);
    return date.toISOString().slice(0, 10);
  }

  function statusBadge(status) {
    return el("span", "a-status " + status, labels[status] || status);
  }

  function iconFor(item) {
    var box = el("div", "plugin-icon");
    box.textContent = (item.name || item.id || "?").trim().slice(0, 1).toUpperCase();
    if (item.icon_url) {
      var image = document.createElement("img");
      image.src = item.icon_url;
      image.alt = "";
      image.onerror = function () { image.remove(); };
      box.appendChild(image);
    }
    return box;
  }

  function actionButton(label, action, item, primary) {
    var button = el("button", "btn " + (primary ? "btn-primary" : "btn-glass"), label);
    button.type = "button";
    button.addEventListener("click", function () {
      if (action === "delete" && !confirm("删除插件 " + item.id + "？归档文件也会删除。")) return;
      api("admin/plugins/" + encodeURIComponent(item.id) + "/" + action, { method: "POST" })
        .then(function () {
          toast(action === "delete" ? "已删除" : "操作完成");
          load();
        })
        .catch(function (error) { toast(error.message, true); });
    });
    return button;
  }

  function editButton(item) {
    var button = el("button", "btn btn-glass", "编辑");
    button.type = "button";
    button.addEventListener("click", function () { openEdit(item); });
    return button;
  }

  function row(item) {
    var box = el("div", "admin-row");
    box.appendChild(iconFor(item));
    var main = el("div", "admin-row-main");
    var head = el("div", "admin-row-head");
    head.appendChild(el("strong", "", item.name));
    head.appendChild(el("code", "", item.id));
    head.appendChild(statusBadge(item.status));
    main.appendChild(head);
    main.appendChild(el("p", "admin-row-desc", item.description || "（暂无简介）"));
    main.appendChild(el("div", "admin-row-meta",
      "v" + (item.version || "?") + " · " + formatDate(item.updated_at) + " 更新" +
      (item.archive_size ? " · " + (item.archive_size / 1024).toFixed(1) + " KiB" : "")));
    box.appendChild(main);

    var actions = el("div", "admin-row-actions");
    if (item.status !== "approved") actions.appendChild(actionButton("通过", "approve", item, true));
    if (item.status === "approved") actions.appendChild(actionButton("下架", "unpublish", item, false));
    if (item.status === "pending") actions.appendChild(actionButton("拒绝", "reject", item, false));
    actions.appendChild(editButton(item));
    actions.appendChild(actionButton("删除", "delete", item, false));
    box.appendChild(actions);
    return box;
  }

  function render() {
    var query = state.q.toLowerCase();
    var items = state.items.filter(function (item) {
      if (state.filter && item.status !== state.filter) return false;
      if (!query) return true;
      return (item.name + " " + item.id + " " + (item.description || "")).toLowerCase().indexOf(query) >= 0;
    });
    var list = $("adminList");
    list.innerHTML = "";
    $("adminEmpty").hidden = items.length !== 0;
    items.forEach(function (item) { list.appendChild(row(item)); });
    var counts = { pending: 0, approved: 0, removed: 0, rejected: 0 };
    state.items.forEach(function (item) { if (counts[item.status] !== undefined) counts[item.status]++; });
    $("statPending").textContent = counts.pending;
    $("statApproved").textContent = counts.approved;
    $("statRemoved").textContent = counts.removed;
    $("statRejected").textContent = counts.rejected;
    $("adminSub").textContent = state.items.length + " 个插件 · 管理操作立即生效";
    document.querySelectorAll(".a-tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.getAttribute("data-status") === state.filter);
      var status = tab.getAttribute("data-status");
      if (status && counts[status] !== undefined) tab.textContent = labels[status] + " " + counts[status];
      else if (!status) tab.textContent = "全部 " + state.items.length;
    });
  }

  function load() {
    api("admin/plugins").then(function (body) {
      state.items = body.items || [];
      showDashboard();
      render();
    }).catch(function (error) {
      if (error.message !== "请先登录") toast(error.message, true);
    });
  }

  function openEdit(item) {
    state.editing = item;
    $("edit-title") && ($("edit-title").textContent = "编辑插件");
    $("editMeta").textContent = item.id + " · 当前状态：" + (labels[item.status] || item.status);
    $("edit-name").value = item.name || "";
    $("edit-version").value = item.version || "";
    $("edit-repo").value = item.repo_url || "";
    $("edit-icon").value = item.icon_url || "";
    $("edit-min").value = item.qsick_min || "";
    $("edit-max").value = item.qsick_max || "";
    $("edit-desc").value = item.description || "";
    $("editStatus").hidden = true;
    $("edit").showModal();
  }

  function saveEdit() {
    if (!state.editing) return;
    var payload = {
      name: $("edit-name").value.trim(),
      version: $("edit-version").value.trim(),
      repo_url: $("edit-repo").value.trim(),
      icon_url: $("edit-icon").value.trim(),
      qsick_min: $("edit-min").value.trim(),
      qsick_max: $("edit-max").value.trim(),
      description: $("edit-desc").value
    };
    api("admin/plugins/" + encodeURIComponent(state.editing.id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (body) {
      state.editing = body.plugin;
      $("edit").close();
      toast("已保存");
      load();
    }).catch(function (error) {
      var node = $("editStatus");
      node.hidden = false;
      node.textContent = error.message;
      node.classList.add("err");
    });
  }

  $("loginForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var password = $("password").value;
    if (!password) { setLoginStatus("请输入密码", true); return; }
    $("loginSubmit").disabled = true;
    api("admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password })
    }).then(function () {
      $("password").value = "";
      setLoginStatus("");
      load();
    }).catch(function (error) {
      setLoginStatus(error.message, true);
    }).finally(function () {
      $("loginSubmit").disabled = false;
    });
  });

  $("passwordToggle").addEventListener("click", function () {
    var input = $("password");
    var visible = input.type === "text";
    input.type = visible ? "password" : "text";
    $("passwordToggle").textContent = visible ? "显示" : "隐藏";
  });

  $("logout").addEventListener("click", function () {
    api("admin/logout", { method: "POST" }).then(function () {
      state.items = [];
      showLogin();
      toast("已退出管理");
    });
  });

  document.querySelectorAll(".a-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      state.filter = tab.getAttribute("data-status");
      render();
    });
  });

  $("refresh").addEventListener("click", load);
  var searchTimer = null;
  $("adminSearch").addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      state.q = $("adminSearch").value.trim();
      render();
    }, 200);
  });

  $("editClose").addEventListener("click", function () { $("edit").close(); });
  $("editCancel").addEventListener("click", function () { $("edit").close(); });
  $("editSave").addEventListener("click", saveEdit);

  api("admin/session").then(function (body) {
    if (body.authed) load(); else showLogin();
  }).catch(function () { showLogin(); });
})();
