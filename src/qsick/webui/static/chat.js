/* QQNT-like workspace; account-scoped history, persistent selection and drafts. */
(function () {
  "use strict";
  const root = document.getElementById("nt-chat");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const timeline = $("chat-log"), list = $("chat-groups"), input = $("chat-input");
  const state = { accounts: [], account: null, conversations: [], current: null, filter: "all", epoch: 0, privateSend: false,
    messages: new Map(), pending: new Map(), outboxes: new Map(), before: null, more: false, signature: "", sending: false, loading: false, busy: false, attachments: [], replyTo: null };
  const read = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch (_) {} };
  const uin = () => String(state.account && state.account.qq || "");
  const convKey = c => c ? c.chat_type + ":" + String(c.id) : "";
  const draftKey = () => "qsick.chat.draft." + uin() + "." + convKey(state.current);
  function outbox(account, conversation) {
    const key = account + "." + convKey(conversation);
    if (!state.outboxes.has(key)) state.outboxes.set(key, new Map());
    return state.outboxes.get(key);
  }
  const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text != null) el.textContent = text; return el; };
  function notice(text) { $("chat-notice").textContent = text || ""; $("chat-notice").hidden = !text; }
  async function api(url, options) {
    const response = await fetch(url, Object.assign({ cache: "no-store" }, options));
    // 服务端出错时可能返回 HTML 或纯文本而不是 JSON。直接 response.json()
    // 会让用户看到 "Unexpected token 'I'" 这种与真实原因无关的报错，真正的
    // 失败信息（"Internal Server Error"）反而被吞掉了。
    const raw = await response.text();
    let body = null;
    try { body = raw ? JSON.parse(raw) : null; } catch (_) { body = null; }
    if (!response.ok || (body && body.ok === false)) {
      const text = (body && (body.error || body.message))
        || raw.trim().slice(0, 300)
        || ("请求失败（" + response.status + "）");
      throw new Error(text);
    }
    if (!body) throw new Error("服务端没有返回 JSON（" + response.status + "）");
    return body;
  }
  function avatar(id, label, group) {
    const el = node("span", "nt-avatar" + (group ? " nt-avatar-group" : ""), group ? "群" : String(label || id || "?").slice(0, 1));
    if (/^[1-9]\d+$/.test(String(id))) {
      const img = document.createElement("img"); img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
      img.src = group ? "https://p.qlogo.cn/gh/" + id + "/" + id + "/100" : "https://q1.qlogo.cn/g?b=qq&nk=" + id + "&s=100";
      img.addEventListener("error", () => img.remove(), { once: true }); el.appendChild(img);
    }
    return el;
  }
  function shortTime(time) {
    if (!time) return "";
    const date = new Date(time * 1000), today = new Date();
    return date.toDateString() === today.toDateString() ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
  }
  function stamp(time) { return new Date(time * 1000).toLocaleString("zh-CN", { hour12: false }); }
  const FACE_PLACEHOLDER = /\[表情:([^\[\]#]{1,32})(?:#([0-9A-Za-z_\-]{1,20}))?\]/g;
  const faceMarkers = new Map();   // placeholder -> 完整标记

  function saveDraft() {
    if (!uin() || !state.current) return;
    store(draftKey(), input.value);
    // 占位符本身不带 id，重新打开页面后就无法还原成标记；把映射一起存下来，
    // 否则草稿里的表情会变成一串认不出的方括号文字。
    store(draftKey() + ".faces", JSON.stringify(Array.from(faceMarkers.entries())));
  }
  function loadFaceMarkers() {
    try {
      const raw = JSON.parse(read(draftKey() + ".faces") || "[]");
      if (Array.isArray(raw)) raw.forEach(pair => {
        if (Array.isArray(pair) && typeof pair[0] === "string" && typeof pair[1] === "string") {
          faceMarkers.set(pair[0], pair[1]);
        }
      });
    } catch (_) { /* 损坏的草稿不该让输入框失效 */ }
  }
  // 每 3 秒轮询都会走到这里（loadHistory → updateComposer，loadAccounts →
  // chooseAccount → updateComposer）。对 <textarea> 反复赋同一个 disabled 值是
  // 有副作用的：只要这次赋值把 disabled 设为 true，浏览器会立刻让元素失焦、
  // 光标消失，用户必须再点一次才能继续输入——表现就是"输入到一半突然停"。
  // 所以只在取值真的变化时才碰它；而且用户正在输入时不因为一次状态抖动
  // 把光标踢掉（下一轮轮询若确实该禁用会补上）。
  // 模板里 textarea 初始就是 disabled，缓存初值必须与之一致，否则第一次
  // updateComposer 还会再写一次（同样会打断"刚聚焦"的输入）。
  let composerLocked = true;
  function updateComposer() {
    const allowed = !!(state.account && state.account.logged_in && state.current && (state.current.chat_type === "group" || state.privateSend) && !state.loading);
    const locked = !allowed;
    // 输入中遇到"短暂"状态抖动（轮询期间 loading 为真）不要抢焦点；但如果
    // 会话/账号真的没了，就必须立即禁用，否则输入框看起来可用、发出去才报错。
    const hardLock = !state.current || !state.account || !state.account.logged_in;
    const typing = document.activeElement === input;
    if (composerLocked !== locked && (!typing || hardLock)) {
      composerLocked = locked;
      input.disabled = locked;
    }
    input.placeholder = !state.current ? "选择一个会话后开始聊天" : state.current.chat_type === "private" && !state.privateSend ? "当前通道不支持私聊发送" : !state.account.logged_in ? "账号未就绪，请先登录" : "输入消息…";
    $("chat-send").disabled = locked || (!input.value.trim() && !state.attachments.length) || state.sending;
    $("chat-send").textContent = state.sending ? "发送中…" : "发送 ↵";
    $("chat-length").textContent = input.value.length + " / 10000";
    $("emoji-toggle").disabled = locked;
  }
  function nearBottom() { return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 90; }
  function bottom() { timeline.scrollTop = timeline.scrollHeight; $("chat-latest").hidden = true; }
  function renderConversations() {
    const query = $("conversation-search").value.trim().toLowerCase();
    const items = state.conversations.filter(c => (state.filter === "all" || c.chat_type === state.filter) && [c.title, c.id, c.preview].join(" ").toLowerCase().includes(query));
    $("chat-count").textContent = String(state.conversations.length);
    // 轮询每 6 秒调一次；无条件重建会让每个会话头像 <img> 重新发起请求并触发
    // 一次布局。列表没变化时直接跳过——这是"输入时页面突然抖一下"的来源之一。
    const signature = JSON.stringify([
      state.filter, query, convKey(state.current),
      items.map(c => [c.id, c.chat_type, c.title, c.preview, c.last_time, c.unread]),
    ]);
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    list.replaceChildren();
    items.forEach(c => {
      const button = node("button", "nt-conversation" + (convKey(c) === convKey(state.current) ? " active" : ""));
      button.type = "button"; button.setAttribute("role", "listitem"); button.setAttribute("aria-current", convKey(c) === convKey(state.current) ? "true" : "false");
      button.appendChild(avatar(c.id, c.title, c.chat_type === "group"));
      const copy = node("span", "nt-conversation-copy"), line = node("span", "nt-conversation-line");
      line.append(node("strong", "", c.title), node("time", "", shortTime(c.last_time)));
      copy.append(line, node("span", "nt-conversation-preview", c.preview || "暂无消息")); button.appendChild(copy);
      button.addEventListener("click", () => chooseConversation(c)); list.appendChild(button);
    });
    if (!items.length) list.appendChild(node("p", "nt-empty-list", query ? "没有匹配的会话" : "暂无会话记录\n账号收到消息后会显示在这里"));
  }
  // ---------------------------------------------------------------- rendering

  // Minimal, safe markdown: headings, lists, bold/italic/code runs. Everything is
  // built from text nodes, so message content can never inject markup.
  function inlineMarkdown(text) {
    const host = document.createDocumentFragment();
    const pattern = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_)/g;
    let last = 0, match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) host.appendChild(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (token.startsWith("`")) host.appendChild(node("code", "nt-md-code", token.slice(1, -1)));
      else if (token.startsWith("**") || token.startsWith("__")) host.appendChild(node("strong", "", token.slice(2, -2)));
      else host.appendChild(node("em", "", token.slice(1, -1)));
      last = match.index + token.length;
    }
    if (last < text.length) host.appendChild(document.createTextNode(text.slice(last)));
    return host;
  }

  function markdownImage(url, alt) {
    const link = document.createElement("a");
    link.className = "nt-image nt-md-image";
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
    const img = document.createElement("img");
    img.alt = alt || "图片"; img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = url;
    const size = /#(\d+)px\s+#(\d+)px/.exec(alt || "");
    if (size) {
      const max = 220, scale = Math.min(1, max / Math.max(Number(size[1]), Number(size[2])));
      img.width = Math.round(Number(size[1]) * scale);
      img.height = Math.round(Number(size[2]) * scale);
    }
    img.addEventListener("error", () => { link.replaceWith(node("span", "nt-media-fallback", "[图片]")); }, { once: true });
    link.appendChild(img);
    return link;
  }

  // QQ bot markdown embeds images and mentions as markdown/inline links.
  function markdownInline(text) {
    const host = document.createDocumentFragment();
    const pattern = /!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\((mqqapi:[^)\s]+|https?:\/\/[^)\s]+)\)/g;
    let last = 0, match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) host.appendChild(inlineMarkdown(text.slice(last, match.index)));
      if (match[1] !== undefined) {
        host.appendChild(markdownImage(match[2], match[1]));
      } else {
        // mqqapi: mention links are rendered as an @ label; http links stay clickable.
        const label = match[3].replace(/\\/g, "");
        const href = match[4];
        if (href.startsWith("mqqapi://markdown/mention")) {
          host.appendChild(node("span", "nt-at", label.startsWith("@") ? label : "@" + label.replace(/^@/, "")));
        } else {
          const link = document.createElement("a");
          link.className = "nt-md-link"; link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
          link.textContent = label;
          host.appendChild(link);
        }
      }
      last = match.index + match[0].length;
    }
    if (last < text.length) host.appendChild(inlineMarkdown(text.slice(last)));
    return host;
  }

  function markdownNodes(content) {
    const wrap = node("div", "nt-markdown");
    String(content || "").split(/\r?\n/).forEach(raw => {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { wrap.appendChild(node("div", "nt-md-gap")); return; }
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const level = Math.min(heading[1].length + 2, 6);
        const block = node("div", "nt-md-h nt-md-h" + heading[1].length);
        block.appendChild(markdownInline(heading[2]));
        block.dataset.level = String(level);
        wrap.appendChild(block); return;
      }
      const bullet = /^\s{0,3}[-*+]\s+(.*)$/.exec(line);
      if (bullet) {
        const item = node("div", "nt-md-li");
        item.appendChild(markdownInline(bullet[1]));
        wrap.appendChild(item); return;
      }
      const ordered = /^\s{0,3}(\d+)[.)]\s+(.*)$/.exec(line);
      if (ordered) {
        const item = node("div", "nt-md-li nt-md-ol");
        item.dataset.index = ordered[1];
        item.appendChild(markdownInline(ordered[2]));
        wrap.appendChild(item); return;
      }
      if (/^\s{0,3}([-*_])\s*\1\s*\1/.test(line)) { wrap.appendChild(node("div", "nt-md-hr")); return; }
      const quote = /^\s{0,3}>\s?(.*)$/.exec(line);
      if (quote) {
        const block = node("div", "nt-md-quote");
        block.appendChild(markdownInline(quote[1]));
        wrap.appendChild(block); return;
      }
      const para = node("div", "nt-md-line");
      para.appendChild(markdownInline(line));
      wrap.appendChild(para);
    });
    return wrap;
  }

  function renderSegment(segment, message) {
    const type = segment.type;
    if (type === "text") return node("span", "nt-text", segment.text);
    if (type === "at") {
      const label = "@" + (segment.name ? String(segment.name).replace(/^@/, "") : segment.user_id === "all" ? "全体成员" : segment.user_id);
      return node("span", "nt-at", label);
    }
    if (type === "reply") {
      const quoteInfo = segment.quote || { found: false };
      const card = node("button", "nt-quote" + (quoteInfo.found ? "" : " missing"));
      card.type = "button";
      card.dataset.jumpTo = quoteInfo.found ? String(quoteInfo.message_id) : "";
      const head = node("span", "nt-quote-head", quoteInfo.found ? (quoteInfo.sender || "引用消息") : "无法定位引用");
      const summary = node("span", "nt-quote-body", quoteInfo.found ? (quoteInfo.text || "[消息]") : "该消息不在当前记录中");
      card.append(head, summary);
      card.title = quoteInfo.found ? "点击跳转到被引用的消息" : "无法定位引用";
      card.addEventListener("click", event => {
        event.stopPropagation();
        if (!quoteInfo.found) {
          notice("无法定位引用：该消息不在本地消息记录中");
          // Polling clears the notice bar, so keep a visible toast as well.
          if (window.qsickToast) window.qsickToast("无法定位引用：该消息不在本地消息记录中", "err");
          return;
        }
        jumpToMessage(String(quoteInfo.message_id));
      });
      return card;
    }
    if (type === "markdown") return markdownNodes(segment.content);
    if (type === "image") {
      // A market/sticker face arrives as an image in some clients; keep the two
      // visually distinct instead of rendering every picture the same way.
      const isSticker = segment.market_face === true;
      if (segment.url) {
        const link = document.createElement("a");
        link.className = isSticker ? "nt-sticker" : "nt-image";
        link.href = segment.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        const img = document.createElement("img");
        img.alt = segment.summary || (isSticker ? "表情包" : "图片");
        img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = segment.url;
        if (!isSticker && segment.width && segment.height && segment.width > 0 && segment.height > 0) {
          const max = 220, scale = Math.min(1, max / Math.max(segment.width, segment.height));
          img.width = Math.round(segment.width * scale); img.height = Math.round(segment.height * scale);
        }
        img.addEventListener("error", () => {
          link.replaceWith(retryableImage(segment, message));
        }, { once: true });
        link.appendChild(img); return link;
      }
      return retryableImage(segment, message);
    }
    if (type === "bag" || type === "sticker") {
      return node("span", "nt-media-fallback", "[" + (segment.summary || "表情包") + "]");
    }
    if (type === "face") return renderFace(segment);
    if (type === "mface") {
      // Sticker: rendered inline, never as a photo bubble.
      if (segment.url) {
        const link = document.createElement("a");
        link.className = "nt-sticker"; link.href = segment.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        const img = document.createElement("img");
        img.className = "nt-mface"; img.alt = segment.summary || "表情包";
        img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = segment.url;
        img.addEventListener("error", () => {
          link.replaceWith(node("span", "nt-media-fallback", "[" + (segment.summary || "表情包") + "]"));
        }, { once: true });
        link.appendChild(img); return link;
      }
      return node("span", "nt-media-fallback", "[" + (segment.summary || "表情包") + "]");
    }
    const label = "[" + (segment.summary || segment.original_type || type) + "]";
    const fallback = node("span", "nt-media-fallback", label);
    if (segment.url) { fallback.classList.add("link"); fallback.title = segment.url; }
    return fallback;
  }

  // ------------------------------------------------------------------ lottie
  // QQ ships its face motion as Lottie JSON, so the real animation plays here
  // instead of a frozen first frame. Players are pooled per container and torn
  // down when the node leaves the document, because a long history would
  // otherwise keep hundreds of rAF loops alive.
  const lottiePlayers = new WeakMap();
  const lottieHosts = new Set();
  const lottiePending = new WeakSet();

  function destroyAllLottie() {
    // Called before a repaint replaces the timeline: the old nodes are detached,
    // so their players would otherwise keep animating forever.
    lottieHosts.forEach(host => stopLottie(host));
    lottieHosts.clear();
  }

  function lottieAvailable() {
    return typeof window.lottie === "object" && typeof window.lottie.loadAnimation === "function";
  }

  function stopLottie(host) {
    const player = lottiePlayers.get(host);
    if (!player) return;
    try { player.destroy(); } catch (_) { /* already gone */ }
    lottiePlayers.delete(host);
    lottieHosts.delete(host);
  }

  function playLottie(host, url, options) {
    if (!lottieAvailable() || !host || lottiePending.has(host)) return false;
    const opts = options || {};
    if (state.hidden || document.hidden) return false;
    lottiePending.add(host);
    // The static PNG stays underneath: if the JSON 404s or the player refuses
    // the document, the face is still visible instead of collapsing to nothing.
    const player = window.lottie.loadAnimation({
      container: host,
      renderer: "svg",
      loop: opts.loop !== false,
      autoplay: true,
      path: url,
      rendererSettings: { preserveAspectRatio: "xMidYMid meet", progressiveLoad: true },
    });
    lottiePlayers.set(host, player);
    lottieHosts.add(host);
    player.addEventListener("DOMLoaded", () => {
      lottiePending.delete(host);
      host.classList.add("nt-lottie-ready");
    });
    player.addEventListener("data_failed", () => {
      lottiePending.delete(host);
      stopLottie(host);
      host.replaceChildren();
    });
    return true;
  }

  // One shared observer pauses off-screen animations, exactly like the client
  // stops drawing faces that scrolled away.
  const lottieObserver = typeof IntersectionObserver === "function"
    ? new IntersectionObserver(entries => {
      entries.forEach(entry => {
        const player = lottiePlayers.get(entry.target);
        if (!player) return;
        if (entry.isIntersecting) player.play(); else player.pause();
      });
    }, { root: null, rootMargin: "200px" })
    : null;

  function faceElement(segment) {
    // Show QQ's own face art. When the library has no image for this id, say so
    // plainly instead of substituting a look-alike emoji.
    const src = segment.face_url || (segment.url && /^https?:\/\//i.test(segment.url) ? segment.url : "");
    const missing = () => {
      const notice = node("span", "nt-media-fallback nt-face-missing", "[QQ版本过低，无法显示对应表情]");
      notice.title = "该表情在本地表情库中没有资源（ID " + (segment.id || "?") + "）";
      return notice;
    };
    if (!src && !segment.face_animation) return missing();
    const wrap = node("span", "nt-face-wrap");
    const img = document.createElement("img");
    img.className = "nt-face"; img.alt = segment.name || "表情";
    // An animated face (APNG/gif/webp) must not be lazy-deferred, otherwise it
    // sits on the first frame until it scrolls into view.
    const animated = segment.face_animated === true || /\.(gif|webp)(\?|$)/i.test(src);
    if (!animated) img.loading = "lazy";
    if (animated) img.classList.add("nt-face-animated");
    img.referrerPolicy = "no-referrer"; img.src = src;
    img.title = segment.name || "表情";
    wrap.appendChild(img);
    if (segment.face_animation) {
      // Overlay the Lottie on the same box so the swap is invisible.
      const layer = node("span", "nt-face-lottie");
      layer.setAttribute("aria-hidden", "true");
      wrap.appendChild(layer);
      // Delay the fetch until the bubble is on screen; a busy history must not
      // download every Lottie document at once.
      const kick = () => {
        if (!layer.isConnected) return;
        if (!playLottie(layer, segment.face_animation)) return;
        if (lottieObserver) lottieObserver.observe(layer);
      };
      if (typeof IntersectionObserver === "function") {
        const once = new IntersectionObserver(entries => {
          if (!entries.some(entry => entry.isIntersecting)) return;
          once.disconnect();
          kick();
        }, { rootMargin: "300px" });
        once.observe(layer);
      } else { kick(); }
    } else {
      img.addEventListener("error", () => { wrap.replaceWith(missing()); }, { once: true });
    }
    return wrap;
  }

  function renderFace(segment) {
    return faceElement(segment);
  }

  function highlight(element) {
    element.classList.add("nt-flash");
    setTimeout(() => element.classList.remove("nt-flash"), 1600);
  }

  async function jumpToMessage(messageId) {
    const selector = '.nt-message[data-message-id="' + String(messageId).replace(/"/g, "") + '"]';
    let target = timeline.querySelector(selector);
    if (!target) {
      // Not in the loaded page: page back through history until found or exhausted.
      let guard = 0;
      while (!target && state.more && guard < 20) {
        guard += 1;
        await loadHistory(true);
        target = timeline.querySelector(selector);
      }
    }
    if (!target) {
      notice("无法定位引用：该消息不在本地消息记录中");
      if (window.qsickToast) window.qsickToast("无法定位引用：该消息不在本地消息记录中", "err");
      return;
    }
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    highlight(target);
  }

  /**
   * A picture that could not be shown: clicking asks the host to fetch it again.
   * QQ only downloads media on demand, so a placeholder is often recoverable.
   */
  function retryableImage(segment, message) {
    const label = "[" + (segment.summary || "图片") + "]";
    const button = node("button", "nt-media-fallback nt-media-retry", label);
    button.type = "button";
    button.title = "点击尝试下载这张图片";
    button.addEventListener("click", async event => {
      event.stopPropagation();
      if (!message || !message.message_id) return;
      button.disabled = true;
      button.textContent = "正在下载…";
      try {
        const result = await postJson("/api/chat/download-media", {
          account: uin(),
          message_id: String(message.message_id),
          element_id: String(segment.element_id || ""),
          chat_type: message.message_type || (state.current ? state.current.chat_type : ""),
          chat_id: state.current ? String(state.current.id) : "",
        });
        if (result.url) {
          const img = document.createElement("img");
          img.className = "nt-image"; img.alt = segment.summary || "图片";
          img.src = result.url;
          img.addEventListener("error", () => {
            button.disabled = false; button.textContent = label;
            button.title = "下载成功但无法显示，可能已过期";
            notice("图片已下载但无法显示：" + (result.url || ""));
          }, { once: true });
          button.replaceWith(img);
          return;
        }
        // 失败必须看得见。以前只写进 title，用户不悬停就完全不知道发生了什么，
        // 界面上那个 [图片] 按钮毫无变化，看起来像点击没反应。
        const why = result.error || "宿主没有返回图片";
        button.textContent = label;
        button.title = "仍未取得图片：" + why;
        button.classList.add("nt-media-failed");
        notice("获取图片失败：" + why);
      } catch (error) {
        button.textContent = label;
        button.title = "下载失败：" + error.message;
        button.classList.add("nt-media-failed");
        notice("获取图片失败：" + error.message);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  function bubbleFor(message) {
    const bubble = node("div", "nt-bubble");
    const segments = Array.isArray(message.segments) && message.segments.length
      ? message.segments
      : [{ type: "text", text: message.text || "" }];
    segments.forEach(segment => bubble.appendChild(renderSegment(segment, message)));
    return bubble;
  }

  function authorOf(message) {
    if (message.outgoing) return "我";
    // Never show a bare QQ number when a card or nickname exists.
    return message.sender && !/^\d+$/.test(String(message.sender))
      ? message.sender
      : (message.card || message.sender || message.sender_uin);
  }

  function renderMessages(forceBottom, prepend) {
    const messages = Array.from(state.messages.values()).sort((a, b) => a.time - b.time || (BigInt(a.message_id) < BigInt(b.message_id) ? -1 : 1));
    const signature = JSON.stringify([messages, Array.from(state.pending.values()), state.more]);
    if (signature === state.signature) return;
    state.signature = signature;
    const oldTop = timeline.scrollTop, oldHeight = timeline.scrollHeight, follow = forceBottom || nearBottom();
    const fragment = document.createDocumentFragment();
    if (state.more) {
      const more = node("button", "nt-load-more", "加载更早的消息"); more.type = "button";
      more.addEventListener("click", () => loadHistory(true)); fragment.appendChild(more);
    }
    let previousTime = 0;
    messages.forEach(m => {
      if (!previousTime || m.time - previousTime > 300) fragment.appendChild(node("div", "nt-time-separator", stamp(m.time)));
      previousTime = m.time;
      const row = node("article", "nt-message" + (m.outgoing ? " outgoing" : "")); row.dataset.messageId = String(m.message_id);
      const peerAvatar = avatar(m.sender_uin, authorOf(m));
      peerAvatar.dataset.senderUin = String(m.sender_uin);
      peerAvatar.dataset.senderName = authorOf(m);
      if (!m.outgoing) peerAvatar.classList.add("nt-peer-avatar-trigger");
      row.appendChild(peerAvatar);
      const body = node("div", "nt-message-body");
      body.appendChild(node("div", "nt-message-author", authorOf(m)));
      const bubble = bubbleFor(m); bubble.title = stamp(m.time); body.appendChild(bubble);
      const copy = node("button", "nt-message-copy", "复制"); copy.type = "button";
      copy.setAttribute("aria-label", "复制这条消息"); copy.addEventListener("click", () => copyText(m.text || "")); body.appendChild(copy);
      row.appendChild(body); fragment.appendChild(row);
    });
    state.pending.forEach((m, key) => {
      const row = node("article", "nt-message outgoing nt-local-message"); row.appendChild(avatar(uin(), "我"));
      const body = node("div", "nt-message-body"); body.append(node("div", "nt-message-author", "我"), node("div", "nt-bubble", m.text));
      body.appendChild(node("span", "nt-delivery " + (m.error ? "failed" : ""), m.error ? "发送失败：" + m.error : m.done ? "已提交，等待 QQ 消息回执" : "正在发送…"));
      if (m.error) {
        const retry = node("button", "nt-retry", "放回输入框"); retry.type = "button";
        retry.addEventListener("click", () => { input.value = m.text; state.pending.delete(key); saveDraft(); updateComposer(); renderMessages(false); input.focus(); }); body.appendChild(retry);
      }
      row.appendChild(body); fragment.appendChild(row);
    });
    if (!messages.length && !state.pending.size) fragment.appendChild(node("div", "nt-history-empty", "暂无消息记录"));
    destroyAllLottie();
    timeline.replaceChildren(fragment);
    if (prepend) timeline.scrollTop = oldTop + timeline.scrollHeight - oldHeight;
    else if (follow) bottom();
    else { timeline.scrollTop = oldTop; $("chat-latest").hidden = false; }
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); $("chat-status").textContent = "已复制"; }
    catch (_) { $("chat-status").textContent = "无法访问剪贴板，请手动选择消息复制"; }
  }
  async function loadHistory(older, initial) {
    if (!state.current || !uin() || state.loading) return;
    const epoch = state.epoch, conversation = state.current, account = uin();
    const params = new URLSearchParams({ account, chat_type: conversation.chat_type, chat_id: conversation.id, limit: "80" });
    if (older && state.before) params.set("before", state.before);
    // 轮询走增量：只问"比已知最新一条更新的消息"。
    //
    // 以前每 3 秒整页重拉 80 条再整体重建 DOM，聊天栏因此一直"慢半拍"：
    // 后端要读一整页，前端要重排一整屏。现在没有新消息时几乎零开销。
    const incremental = !older && !initial && state.messages.size > 0;
    if (incremental) {
      const newest = Math.max(...Array.from(state.messages.keys(), id => Number(id) || 0));
      if (newest > 0) {
        params.set("after", String(newest));
        params.set("limit", "50");
      }
    }
    state.loading = true;
    try {
      const data = await api("/api/chat/history?" + params);
      if (epoch !== state.epoch) return;
      const rows = data.messages || [];
      if (incremental) {
        // 增量返回为空就什么都不做，连重渲染都不触发。
        if (rows.length) {
          rows.forEach(m => state.messages.set(String(m.message_id), m));
          rows.forEach(m => {
            state.pending.forEach((p, key) => { if (p.id && String(p.id) === String(m.message_id)) state.pending.delete(key); });
          });
          renderMessages(true, false);
        }
        return;
      }
      rows.forEach(m => {
        state.messages.set(String(m.message_id), m);
        state.pending.forEach((p, key) => { if (p.id && String(p.id) === String(m.message_id)) state.pending.delete(key); });
      });
      if (older || initial) { state.more = !!data.has_more; state.before = data.next_before; }
      renderMessages(!!initial, !!older);
      if (state.current.chat_type === "private") notice(state.privateSend ? "私聊发送需要已验证的 QQ / UID 映射；无法解析时会明确报错。" : "当前仅展示私聊记录，此通道不支持私聊发送。");
      else notice(state.account.logged_in ? "" : "账号尚未就绪，可以查看历史消息；登录后可发送。");
    } catch (error) { if (epoch === state.epoch) notice(error.message); }
    finally { if (epoch === state.epoch) { state.loading = false; updateComposer(); } }
  }
  function chooseConversation(c) {
    root.classList.add("nt-has-conversation");
    if (convKey(state.current) === convKey(c)) return;
    saveDraft(); state.current = c; state.epoch++; state.loading = false;
    state.messages.clear(); state.pending = outbox(uin(), c); state.more = false; state.before = null; state.signature = "";
    store("qsick.chat.conversation." + uin(), convKey(c));
    $("chat-title").textContent = c.title; $("chat-subtitle").textContent = (c.chat_type === "group" ? "群号 " : "QQ ") + c.id;
    const oldAvatar = $("chat-peer-avatar"), nextAvatar = avatar(c.id, c.title, c.chat_type === "group"); nextAvatar.id = "chat-peer-avatar"; oldAvatar.replaceWith(nextAvatar);
    faceMarkers.clear(); loadFaceMarkers();
    input.value = read(draftKey()) || ""; $("chat-status").textContent = "";
    timeline.replaceChildren(node("div", "nt-history-empty", "正在加载消息…")); renderConversations(); updateComposer(); loadHistory(false, true);
  }
  async function loadConversations() {
    if (!uin()) return;
    const account = uin(), epoch = state.epoch;
    try {
      const data = await api("/api/chat/conversations?account=" + encodeURIComponent(account));
      if (account !== uin() || epoch !== state.epoch) return;
      state.conversations = data.conversations || [];
      state.privateSend = data.send_private_supported === true;
      updateComposer(); renderConversations();
      if (!state.current && state.conversations.length) {
        const key = read("qsick.chat.conversation." + account);
        chooseConversation(state.conversations.find(c => convKey(c) === key) || state.conversations[0]);
      }
    } catch (error) { if (account === uin()) notice(error.message); }
  }
  function chooseAccount(account) {
    const previous = uin(), changed = !state.account || state.account.id !== account.id || previous !== String(account.qq || "");
    if (changed) { $("account-menu").hidden = true; $("account-toggle").setAttribute("aria-expanded", "false"); }
    saveDraft(); state.account = account;
    $("account-label").textContent = account.qq || account.id;
    // 轮询每 12 秒走一次 loadAccounts → chooseAccount。以前无条件 replaceWith
    // 换掉头像节点：即便账号没变，DOM 也会被整体替换一次，正在输入的 textarea
    // 会因此重排并丢掉选区/焦点。只在头像外观真的变了才替换。
    const label = String(account.qq || account.id);
    const old = $("account-avatar");
    if (!old || old.dataset.avatarFor !== label) {
      const next = avatar(account.qq, label); next.id = "account-avatar"; next.classList.add("nt-avatar-small");
      next.dataset.avatarFor = label;
      old.replaceWith(next);
    }
    store("qsick.chat.account", label);
    if (!changed) { updateComposer(); return; }
    state.epoch++; state.current = null; state.privateSend = false; state.conversations = []; state.messages.clear(); state.pending = new Map(); state.loading = false;
    input.value = ""; timeline.replaceChildren(node("div", "nt-history-empty", "请选择会话"));
    $("chat-title").textContent = "选择一个会话"; $("chat-subtitle").textContent = "从左侧继续聊天";
    $("chat-latest").hidden = true; notice(""); updateComposer(); renderConversations();
    if (uin()) loadConversations(); else notice("此实例尚未登录 QQ，请先到“账户管理”登录。");
  }
  async function loadAccounts() {
    try {
      const data = await api("/api/accounts"); state.accounts = data.accounts || [];
      const menu = $("account-menu");
      // 菜单只在账号列表真的变化时重建。每 12 秒无条件 replaceChildren 会让
      // 里面的 <img> 重新拉头像：网络请求、重排、以及正在输入的输入框丢焦点。
      const signature = JSON.stringify(state.accounts.map(a => [a.id, a.qq, !!a.logged_in]));
      if (menu.dataset.signature !== signature) {
        menu.dataset.signature = signature;
        menu.replaceChildren();
        state.accounts.forEach(account => {
          const item = node("button", "nt-account-option"); item.type = "button";
          item.appendChild(avatar(account.qq, account.id));
          const copy = node("span"); copy.append(node("strong", "", account.qq || account.id), node("small", "", account.id + " · " + (account.logged_in ? "会话就绪" : "未就绪")));
          item.appendChild(copy); item.addEventListener("click", () => chooseAccount(account)); menu.appendChild(item);
        });
      }
      if (!state.accounts.length) {
        saveDraft(); state.account = null; state.current = null; state.epoch++; state.loading = false;
        state.conversations = []; state.messages.clear(); state.pending = new Map(); input.value = "";
        $("account-label").textContent = "暂无账号"; menu.appendChild(node("p", "nt-empty-list", "请先在账户管理创建实例"));
        timeline.replaceChildren(node("div", "nt-history-empty", "请先在账户管理登录一个 QQ 账号")); renderConversations(); updateComposer(); return;
      }
      const remembered = state.account ? String(state.account.qq || state.account.id) : read("qsick.chat.account");
      const selected = state.accounts.find(a => String(a.qq || a.id) === remembered) || state.accounts.find(a => a.logged_in) || state.accounts[0];
      chooseAccount(selected);
    } catch (error) { notice(error.message); }
  }
  const MAX_UPLOAD = 100 * 1024 * 1024;
  const isImageFile = file => /^image\/(png|jpeg|jpg|gif|webp|bmp)$/i.test(file.type || "");

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function renderAttachments() {
    const host = $("chat-attachments");
    host.replaceChildren();
    state.attachments.forEach((item, index) => {
      const chip = node("div", "nt-attachment" + (item.error ? " failed" : ""));
      if (item.kind === "image" && item.previewUrl) {
        const img = document.createElement("img");
        img.src = item.previewUrl; img.alt = "";
        chip.appendChild(img);
      } else {
        chip.appendChild(node("span", "nt-attachment-icon", item.kind === "image" ? "图" : "文"));
      }
      const meta = node("span", "nt-attachment-meta");
      meta.append(node("strong", "", item.file.name || "文件"), node("small", "", humanSize(item.file.size) + (item.error ? " · 发送失败" : "")));
      chip.appendChild(meta);
      const remove = node("button", "nt-attachment-remove", "×");
      remove.type = "button"; remove.title = "移除"; remove.setAttribute("aria-label", "移除附件");
      remove.disabled = state.sending;
      remove.addEventListener("click", () => {
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        state.attachments.splice(index, 1);
        renderAttachments(); updateComposer();
      });
      chip.appendChild(remove);
      host.appendChild(chip);
    });
    host.hidden = !state.attachments.length;
    updateComposer();
  }

  function addFiles(files, options) {
    if (!state.current) { notice("请先选择一个会话再添加图片或文件"); return; }
    // extras.picSubType = 1 表示"这是表情，不是图片"。收藏表情靠它才能以表情
    // 身份发出；不带就是普通图片。
    const picSubType = options && Number.isInteger(options.picSubType) ? options.picSubType : undefined;
    let rejected = 0;
    Array.from(files || []).forEach(file => {
      if (!file || !file.size) { rejected += 1; return; }
      if (file.size > MAX_UPLOAD) { rejected += 1; return; }
      const isImage = isImageFile(file);
      const item = { file, kind: isImage ? "image" : "file", previewUrl: isImage ? URL.createObjectURL(file) : "" };
      // 表情只对图片有意义；文件不能冒充表情。
      if (isImage && picSubType !== undefined) item.picSubType = picSubType;
      state.attachments.push(item);
    });
    if (rejected) notice(rejected + " 个文件被忽略（为空或超过 100 MB）");
    else notice("");
    renderAttachments();
  }

  async function sendAttachments(epoch, account, c, text) {
    const queue = state.attachments.slice();
    let okCount = 0;
    for (const item of queue) {
      if (epoch !== state.epoch) break;
      $("chat-status").textContent = "正在发送 " + (item.file.name || "文件") + "…";
      const form = new FormData();
      form.append("account", account);
      form.append("chat_type", c.chat_type);
      form.append("chat_id", String(c.id));
      form.append("kind", item.kind);
      // 表情与图片在 QQ 侧是两个不同的 picSubType；发错了接收端就只显示一张图。
      if (Number.isInteger(item.picSubType)) form.append("pic_sub_type", String(item.picSubType));
      if (text && !okCount) form.append("text", text);
      if (state.replyTo && !okCount) form.append("reply_to", state.replyTo.message_id);
      form.append("file", item.file, item.file.name || "upload.bin");
      try {
        const response = await fetch("/api/chat/send-media", { method: "POST", body: form, cache: "no-store" });
        // 上传失败时服务端可能返回 HTML 或纯文本（例如裸 500）。直接 .json()
        // 会抛 "Unexpected token 'I'"，把真正的失败原因盖掉。
        const raw = await response.text();
        let result = null;
        try { result = raw ? JSON.parse(raw) : null; } catch (_) { result = null; }
        if (!response.ok || !result || result.status !== "ok") {
          const why = (result && (result.error || result.message || result.wording))
            || raw.trim().slice(0, 300)
            || ("HTTP " + response.status);
          throw new Error(why);
        }
        okCount += 1;
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        state.attachments = state.attachments.filter(other => other !== item);
        renderAttachments();
      } catch (error) {
        item.error = error.message;
        renderAttachments();
        if (epoch === state.epoch) {
          // The caption travels with the first attachment; put it back so a retry
          // does not silently drop text the user wrote.
          if (!okCount && text && !input.value.trim()) {
            input.value = text; saveDraft(); updateComposer();
          }
          $("chat-status").textContent = "发送失败：" + error.message;
        }
        return okCount;
      }
    }
    if (okCount) {
      $("chat-status").textContent = okCount > 1 ? "已发送 " + okCount + " 个附件" : "已提交发送请求";
      if (epoch === state.epoch) {
        // A reply only belongs to the message it was composed for; drop it once
        // that message has been sent.
        state.replyTo = null; renderReplyStrip(); updateComposer();
        renderMessages(true); loadHistory(false);
      }
    }
    return okCount;
  }

  async function send(event) {
    event.preventDefault();
    const hasText = !!input.value.trim(), hasFiles = state.attachments.length > 0;
    if (state.sending || input.disabled || !state.current || (!hasText && !hasFiles)) return;
    const epoch = state.epoch, text = input.value, account = uin(), c = state.current, key = String(Date.now()), pending = state.pending;
    // 输入框里是给人看的表情占位符，发出去的必须是完整标记。两者分开保存：
    // text 用于回填输入框与本地回显，wire 才是真正发出去的正文。
    const wire = expandFaces(text);
    state.sending = true;
    if (hasFiles) {
      updateComposer();
      try { await sendAttachments(epoch, account, c, wire); }
      finally {
        state.sending = false;
        if (epoch === state.epoch && !state.attachments.length) { input.value = ""; saveDraft(); }
        updateComposer(); renderAttachments();
      }
      return;
    }
    pending.set(key, { text }); input.value = ""; saveDraft(); updateComposer(); renderMessages(true);
    try {
      const body = { account, chat_type: c.chat_type, chat_id: c.id, text: wire };
      if (state.replyTo) body.reply_to = state.replyTo.message_id;
      const result = await api("/api/chat/send-text", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (result.status !== "ok") throw new Error(result.message || result.wording || "QQ 未接受消息");
      pending.set(key, { text, done: true, id: result.data && (result.data.message_id_str || result.data.message_id) });
      state.replyTo = null; renderReplyStrip();
      if (epoch === state.epoch) $("chat-status").textContent = "已提交发送请求";
    } catch (error) {
      pending.set(key, { text, error: error.message });
      if (epoch === state.epoch) {
        $("chat-status").textContent = "发送失败，内容已放回输入框";
      }
      // The input was cleared optimistically, so a failure must restore it or the
      // user silently loses what they typed despite the message claiming otherwise.
      if (epoch === state.epoch && !input.value.trim()) {
        input.value = text; saveDraft(); updateComposer();
      }
    } finally {
      state.sending = false; updateComposer();
      if (epoch === state.epoch) { renderMessages(true); loadHistory(false); input.focus(); }
    }
  }
  // Paste: images and files copied in the OS land here as clipboard items.
  input.addEventListener("paste", event => {
    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind !== "file") continue;
      const file = item.getAsFile();
      if (file) files.push(file);
    }
    if (!files.length) return; // plain text paste keeps native behaviour
    event.preventDefault();
    addFiles(files);
  });

  // Drag & drop anywhere over the chat pane.
  let dragDepth = 0;
  const showDrop = shown => { $("chat-drop").hidden = !shown; root.classList.toggle("nt-dropping", shown); };
  root.addEventListener("dragenter", event => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
    event.preventDefault(); dragDepth += 1; showDrop(true);
  });
  root.addEventListener("dragover", event => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
    event.preventDefault(); event.dataTransfer.dropEffect = "copy";
  });
  root.addEventListener("dragleave", event => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) showDrop(false);
  });
  root.addEventListener("drop", event => {
    if (!event.dataTransfer || !event.dataTransfer.files || !event.dataTransfer.files.length) return;
    event.preventDefault(); dragDepth = 0; showDrop(false);
    addFiles(event.dataTransfer.files);
    input.focus();
  });
  window.addEventListener("beforeunload", () => state.attachments.forEach(item => { if (item.previewUrl) URL.revokeObjectURL(item.previewUrl); }));

  // ------------------------------------------------------------ context menu
  const ctx = node("div", "nt-ctxmenu");
  ctx.hidden = true;
  document.body.appendChild(ctx);

  function closeCtx() { ctx.hidden = true; ctx.replaceChildren(); }
  function openCtx(event, items) {
    event.preventDefault();
    ctx.replaceChildren();
    items.forEach(item => {
      if (item === "-") { ctx.appendChild(node("div", "nt-ctx-sep")); return; }
      const entry = node("button", "nt-ctx-item" + (item.danger ? " danger" : ""), item.label);
      entry.type = "button";
      if (item.disabled) { entry.disabled = true; entry.title = item.hint || ""; }
      else entry.addEventListener("click", () => { closeCtx(); item.run(); });
      ctx.appendChild(entry);
    });
    ctx.hidden = false;
    const rect = ctx.getBoundingClientRect();
    const left = Math.min(event.clientX, window.innerWidth - rect.width - 8);
    const top = Math.min(event.clientY, window.innerHeight - rect.height - 8);
    ctx.style.left = Math.max(6, left) + "px";
    ctx.style.top = Math.max(6, top) + "px";
  }
  document.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target || !target.closest(".nt-ctxmenu")) closeCtx();
  });
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeCtx(); });
  window.addEventListener("scroll", closeCtx, true);

  async function postJson(url, body) {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store" });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.ok === false || result.status === "failed") {
      throw new Error(result.error || result.message || result.wording || ("HTTP " + response.status));
    }
    return result;
  }

  // Quote uses a real OneBot v11 `reply` segment, not a text banner: QQ renders
  // it as a native quote and the receiver can jump to the original.
  function quoteInto(message) {
    state.replyTo = { message_id: String(message.message_id), sender: authorOf(message), text: message.text || "" };
    renderReplyStrip();
    updateComposer(); input.focus();
  }

  function renderReplyStrip() {
    const strip = $("chat-reply");
    strip.replaceChildren();
    if (!state.replyTo) { strip.hidden = true; return; }
    strip.hidden = false;
    strip.append(
      node("span", "nt-reply-label", "回复 " + (state.replyTo.sender || "")),
      node("span", "nt-reply-text", (state.replyTo.text || "[消息]").slice(0, 80)),
    );
    const cancel = node("button", "nt-reply-cancel", "×");
    cancel.type = "button"; cancel.title = "取消引用"; cancel.setAttribute("aria-label", "取消引用");
    cancel.addEventListener("click", () => { state.replyTo = null; renderReplyStrip(); updateComposer(); });
    strip.appendChild(cancel);
  }

  // Message right-click.
  timeline.addEventListener("contextmenu", event => {
    // Avatars have their own menu; let that handler own the event.
    if (event.target.closest(".nt-peer-avatar-trigger")) return;
    const row = event.target.closest(".nt-message");
    if (!row || !row.dataset.messageId) return;
    const message = state.messages.get(String(row.dataset.messageId));
    if (!message) return;
    const items = [];
    const text = message.text || "";
    if (message.outgoing) {
      items.push({ label: "撤回", run: async () => {
        try {
          await postJson("/api/chat/recall", { account: uin(), message_id: String(message.message_id) });
          $("chat-status").textContent = "已撤回";
          loadHistory(false);
        } catch (error) { $("chat-status").textContent = "撤回失败：" + error.message; }
      } });
    } else {
      // QSICK cannot read the bot's own group role reliably, so the entry is
      // always offered and QQ decides; a rejection is reported verbatim.
      items.push({ label: "群撤回（需要管理权限）", run: async () => {
        try {
          await postJson("/api/chat/recall", { account: uin(), message_id: String(message.message_id) });
          $("chat-status").textContent = "已撤回该成员消息";
          loadHistory(false);
        } catch (error) { $("chat-status").textContent = "撤回失败：" + error.message; }
      } });
    }
    items.push({ label: "复制", run: () => copyText(text) });
    items.push({ label: "引用", run: () => quoteInto(message) });
    if (!message.outgoing) {
      const name = authorOf(message);
      items.push({ label: "在输入框 @ " + name, run: () => {
        input.value = (input.value || "") + "@" + name + " ";
        saveDraft(); updateComposer(); input.focus();
      } });
    }
    items.push("-");
    items.push({ label: "删除（消息记录）", danger: true, run: async () => {
      try {
        await postJson("/api/chat/delete-message", { account: uin(), message_id: String(message.message_id) });
        state.messages.delete(String(message.message_id));
        state.signature = "";
        renderMessages(false);
      } catch (error) { $("chat-status").textContent = "删除失败：" + error.message; }
    } });
    openCtx(event, items);
  });

  // Avatar right-click -> mention that member in the composer.
  timeline.addEventListener("contextmenu", event => {
    const avatarNode = event.target.closest(".nt-peer-avatar-trigger");
    if (!avatarNode) return;
    event.preventDefault();
    const name = avatarNode.dataset.senderName || avatarNode.dataset.senderUin || "";
    const peerUin = String(avatarNode.dataset.senderUin || "");
    openCtx(event, [
      { label: "@ " + name, run: () => {
        input.value = (input.value || "") + "@" + name + " ";
        saveDraft(); updateComposer(); input.focus();
      } },
      { label: "点赞 × 10", disabled: !/^[1-9]\d*$/.test(peerUin), run: async () => {
        $("chat-status").textContent = "正在为 " + name + " 点赞…";
        try {
          await postJson("/api/chat/like", { account: uin(), user_id: peerUin, times: 10 });
          $("chat-status").textContent = "已为 " + name + " 点赞 × 10";
          if (window.qsickToast) window.qsickToast("已点赞 × 10", "ok");
        } catch (error) {
          $("chat-status").textContent = "点赞失败：" + error.message;
          if (window.qsickToast) window.qsickToast("点赞失败：" + error.message, "err");
        }
      } },
      { label: "复制 QQ 号", run: () => copyText(peerUin || "") },
    ]);
  });

  // Conversation right-click.
  list.addEventListener("contextmenu", event => {
    const button = event.target.closest(".nt-conversation");
    if (!button) return;
    const index = Array.from(list.children).indexOf(button);
    const items = state.conversations.filter(c => (state.filter === "all" || c.chat_type === state.filter));
    const conversation = items[index];
    if (!conversation) return;
    const base = { account: uin(), chat_type: conversation.chat_type, chat_id: String(conversation.id) };
    const refresh = async () => { await loadConversations(); };
    openCtx(event, [
      { label: conversation.pinned ? "取消置顶" : "置顶", run: async () => {
        try { await postJson("/api/chat/pin", { ...base, pinned: !conversation.pinned }); await refresh(); }
        catch (error) { notice("置顶失败：" + error.message); }
      } },
      { label: "复制" + (conversation.chat_type === "group" ? "群号" : "QQ 号"), run: () => copyText(String(conversation.id)) },
      "-",
      { label: "从消息列表移除", run: async () => {
        try {
          await postJson("/api/chat/hide", { ...base, hidden: true, last_message_id: conversation.last_message_id });
          if (convKey(state.current) === convKey(conversation)) {
            // Bump epoch so an in-flight history request cannot repaint it.
            state.epoch++; state.current = null; state.messages.clear(); state.signature = "";
            timeline.replaceChildren(node("div", "nt-history-empty", "请选择会话"));
            $("chat-title").textContent = "选择一个会话";
          }
          await refresh();
        } catch (error) { notice("移除失败：" + error.message); }
      } },
      { label: "删除历史消息记录", danger: true, run: async () => {
        if (!confirm("删除该会话在 QSICK 中的全部本地记录？QQ 端不受影响。")) return;
        try {
          await postJson("/api/chat/delete-conversation", base);
          // Stale responses must not resurrect deleted messages.
          state.epoch++; state.messages.clear(); state.signature = "";
          state.current = null;
          timeline.replaceChildren(node("div", "nt-history-empty", "本地记录已删除"));
          $("chat-title").textContent = "选择一个会话";
          await refresh();
        } catch (error) { notice("删除失败：" + error.message); }
      } },
    ]);
  });

  $("chat-form").addEventListener("submit", send);
  input.addEventListener("input", () => { saveDraft(); updateComposer(); });
  // Send on Enter, or on Ctrl+Enter, depending on the user's choice. The other
  // combination inserts a newline, which is why neither is blocked outright.
  const SEND_MODE_KEY = "qsick.chat.sendMode";
  function sendMode() {
    const stored = read(SEND_MODE_KEY);
    return stored === "ctrl-enter" ? "ctrl-enter" : "enter";
  }
  function renderSendMode() {
    const mode = sendMode();
    const menu = $("chat-send-menu");
    if (!menu) return;
    menu.querySelectorAll("[data-mode]").forEach(item => {
      const active = item.dataset.mode === mode;
      item.classList.toggle("active", active);
      item.setAttribute("aria-checked", String(active));
    });
    // QQNT shows the active shortcut on the button itself.
    $("chat-send-mode").title = mode === "ctrl-enter" ? "按 Ctrl + Enter 发送" : "按 Enter 发送";
  }
  function setSendMode(mode) {
    store(SEND_MODE_KEY, mode === "ctrl-enter" ? "ctrl-enter" : "enter");
    renderSendMode();
    $("chat-send-menu").hidden = true;
    $("chat-send-mode").setAttribute("aria-expanded", "false");
  }
  $("chat-send-mode").addEventListener("click", event => {
    event.stopPropagation();
    const menu = $("chat-send-menu");
    menu.hidden = !menu.hidden;
    $("chat-send-mode").setAttribute("aria-expanded", String(!menu.hidden));
  });
  $("chat-send-menu").addEventListener("click", event => {
    const item = event.target.closest("[data-mode]");
    if (item) { event.stopPropagation(); setSendMode(item.dataset.mode); }
  });
  document.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target || !target.closest(".nt-send-group")) {
      $("chat-send-menu").hidden = true;
      $("chat-send-mode").setAttribute("aria-expanded", "false");
    }
  });
  renderSendMode();

  input.addEventListener("keydown", event => {
    if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
    const mode = sendMode();
    const wantsSend = mode === "enter"
      ? !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey
      : event.ctrlKey && !event.shiftKey && !event.metaKey && !event.altKey;
    if (wantsSend) { event.preventDefault(); $("chat-form").requestSubmit(); }
  });
  $("account-toggle").addEventListener("click", () => { const menu = $("account-menu"); menu.hidden = !menu.hidden; $("account-toggle").setAttribute("aria-expanded", String(!menu.hidden)); });
  document.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target || !target.closest(".nt-account-wrap")) { $("account-menu").hidden = true; $("account-toggle").setAttribute("aria-expanded", "false"); }
    if (!target || !target.closest(".nt-tools")) $("emoji-panel").hidden = true;
  });
  $("conversation-search").addEventListener("input", renderConversations);
  root.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    root.querySelectorAll("[data-filter]").forEach(other => { other.classList.toggle("active", other === button); other.setAttribute("aria-pressed", String(other === button)); }); renderConversations();
  }));
  $("chat-refresh").addEventListener("click", async () => { await loadAccounts(); await loadConversations(); await loadHistory(false); });
  $("chat-latest").addEventListener("click", bottom);
  timeline.addEventListener("scroll", () => { if (nearBottom()) $("chat-latest").hidden = true; });
  $("chat-back").addEventListener("click", () => root.classList.remove("nt-has-conversation"));
  // ------------------------------------------------------------ emoji picker
  // Faces are QQ's own art, grouped exactly like the QQ client: 小黄脸 / emoji
  // 表情 / 超级表情, plus whatever this browser used recently.
  const RECENT_KEY = "qsick.chat.recentFaces";
  let faceGroups = [];
  let activeFaceGroup = "";
  // 收藏表情 come from the account's own emoji list, so they are fetched on
  // demand instead of on every picker open.
  let favoriteFaces = null;
  let favoritesLoading = false;

  async function loadFavorites() {
    if (favoriteFaces !== null || favoritesLoading) return favoriteFaces;
    favoritesLoading = true;
    try {
      const data = await api("/api/chat/favorites");
      favoriteFaces = Array.isArray(data.faces) ? data.faces : [];
    } catch (error) {
      favoriteFaces = [];
      notice("收藏表情加载失败：" + error.message);
    } finally { favoritesLoading = false; }
    return favoriteFaces;
  }

  async function preloadFavoriteBlob(face) {
    // The picker shows a same-origin media URL; sending needs a Blob, so fetch it
    // once and keep the promise so repeated clicks do not re-download.
    if (face.blobPromise) return face.blobPromise;
    if (!face.url) return Promise.reject(new Error("这个收藏表情没有可用的图片地址"));
    face.blobPromise = (async () => {
      const response = await fetch(face.url, { credentials: "same-origin" });
      if (!response.ok) {
        // 服务端已经会把原因写在 JSON 里；读出来比 "HTTP 502" 有用得多。
        let detail = "HTTP " + response.status;
        try {
          const body = await response.clone().json();
          if (body && (body.error || body.message)) detail = body.error || body.message;
        } catch (_) { /* 不是 JSON 就用状态码 */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      // 服务端把非图片挡在 415，但代理转发也可能拿到一个 HTML 错误页。
      if (blob.type && !blob.type.startsWith("image/")) {
        throw new Error("返回的不是图片（" + blob.type + "）");
      }
      return blob;
    })().catch(error => {
      // 失败不要缓存，否则用户重试会一直拿到同一个失败的 Promise。
      face.blobPromise = null;
      throw error;
    });
    return face.blobPromise;
  }

  function favoriteButton(face) {
    const button = node("button", "nt-emoji-face");
    button.type = "button";
    button.title = face.desc || "收藏表情";
    button.setAttribute("aria-label", button.title);
    if (face.url) {
      const img = document.createElement("img");
      img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = face.url; img.alt = button.title;
      img.addEventListener("error", () => { button.replaceWith(node("span", "nt-emoji-missing", "?")); }, { once: true });
      button.appendChild(img);
    } else {
      button.appendChild(node("span", "nt-emoji-missing", "?"));
    }
    button.addEventListener("click", async event => {
      event.stopPropagation();
      if (input.disabled) return;
      $("emoji-panel").hidden = true;
      try {
        const blob = await preloadFavoriteBlob(face);
        const ext = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg");
        const file = new File([blob], "favorite-" + (face.md5 || face.res_id || "face") + "." + ext,
          { type: blob.type || "image/png" });
        // 收藏表情要以"表情"身份发出（picSubType=1），否则对方收到的是一张图片，
        // 动图也不会被当表情渲染。
        addFiles([file], { picSubType: 1 });
      } catch (error) {
        notice("收藏表情无法读取：" + error.message);
      }
    });
    return button;
  }

  function recentFaces() {
    try { const raw = JSON.parse(read(RECENT_KEY) || "[]"); return Array.isArray(raw) ? raw.slice(0, 24) : []; }
    catch (_) { return []; }
  }
  function rememberFace(id) {
    const list = [id, ...recentFaces().filter(item => item !== id)].slice(0, 24);
    store(RECENT_KEY, JSON.stringify(list));
  }
  // Build the EXTemotions marker for a picked face.
  //
  // OneBot v11 的 [CQ:face,id=N] 只有一个数字：既没有"是否动态"，也没有"文件类型"，
  // 超级表情因此在两端被猜，猜错就画成别的表情。EXTemotions 把这些写成对象字段。
  // 标记形态用规范附录 B 的兜底写法 [CQ:ext_emotion,json=...]，这样纯 v11 消费方
  // 会跳过不认识的段，而不会把整条消息判废。
  //
  // 注意：这个标记**不直接放进输入框**。base64 有几百个字符，塞进 textarea 会
  // 让用户面对一屏乱码。输入框里放一个占位符（见 markerFor），发送前再换回来。
  function emotionMarker(face) {
    const emotion = {
      schema: "EXTemotions",
      version: "1.0",
      type: face.animated || face.format === "animated" ? "super" : "face",
      id: String(face.id),
      moveable: face.animated === true || face.format === "animated",
      fileType: face.file_type || "png",
    };
    if (emotion.type === "super") emotion.uid = "super:" + emotion.id;
    else emotion.uid = "face:" + emotion.id;
    if (face.name) emotion.name = face.name;
    if (face.pack_id) emotion.pack = { id: String(face.pack_id) };
    if (face.sticker_id) {
      emotion.pack = emotion.pack || {};
      emotion.pack.index = Number(face.sticker_id);
    }
    if (face.sticker_type !== undefined && face.sticker_type !== null) {
      emotion.qq = emotion.qq || {};
      emotion.qq.aniStickerType = Number(face.sticker_type);
    }
    // base64url，无填充（RFC 4648 §5）；服务端按同一规则解回。
    const json = JSON.stringify(emotion);
    const bytes = new TextEncoder().encode(json);
    let binary = "";
    bytes.forEach(byte => { binary += String.fromCharCode(byte); });
    const encoded = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    return "[CQ:ext_emotion,json=" + encoded + "]";
  }

  // 输入框里的占位符：人脸可读，且不会与用户真的打出来的文字混淆。
  //   [表情:流泪]        —— 普通黄脸
  //   [表情:流泪#364]    —— 同一名字出现在多个表情上时带 id 消歧
  // 发送前 markerFor 会把它换回完整标记。

  function placeholderFor(face, marker) {
    const name = String(face.name || face.id || "");
    let text = "[表情:" + name + "]";
    if (faceMarkers.has(text) && faceMarkers.get(text) !== marker) {
      text = "[表情:" + name + "#" + face.id + "]";
    }
    faceMarkers.set(text, marker);
    return text;
  }

  // 把输入框正文展开成服务端要的标记串。认不出的占位符原样保留 —— 用户可能
  // 真的手打了一个方括号，不该被吞掉。
  function expandFaces(text) {
    return String(text || "").replace(FACE_PLACEHOLDER, (whole) => faceMarkers.get(whole) || whole);
  }

  function faceImageButton(face) {
    const id = face.id;
    const label = face.name || "表情";
    const button = node("button", "nt-emoji-face");
    button.type = "button";
    button.title = label || ("表情 " + id);
    // 有些动态表情没有贴纸三元组，QQ 的动效通道发不出去。在面板上就标出来，
    // 否则用户点了、写了字、按了发送，才收到一条拒绝。
    if (face.animated && face.sendable_as_animated === false) {
      button.classList.add("nt-emoji-unsendable");
      button.title = (label || ("表情 " + id)) + "（这个动态表情缺少动效标识，无法发送）";
    }
    button.setAttribute("aria-label", button.title);
    const img = document.createElement("img");
    img.loading = "lazy"; img.referrerPolicy = "no-referrer";
    img.src = "/api/chat/face?id=" + encodeURIComponent(id);
    img.alt = button.title;
    img.addEventListener("error", () => { button.replaceWith(node("span", "nt-emoji-missing", "?")); }, { once: true });
    button.appendChild(img);
    // No player is created here on purpose: QQ animates faces in the transcript,
    // not while you browse the picker. A still PNG is enough to pick from.
    button.addEventListener("click", event => {
      event.stopPropagation();
      if (input.disabled) return;
      // 输入框里放可读的占位符，完整标记留在 faceMarkers 里，发送前才展开。
      // 缺贴纸三元组的动态表情也允许插入：服务端会把它降级成静态发送，
      // 挡住点击等于让用户永远发不出这个表情，比降级更糟。
      const marker = emotionMarker(face);
      const placeholder = placeholderFor(face, marker);
      input.setRangeText(placeholder, input.selectionStart, input.selectionEnd, "end");
      rememberFace(id); saveDraft(); updateComposer(); input.focus();
      $("emoji-panel").hidden = true;
    });
    return button;
  }

  function renderFaceTabs() {
    const tabs = $("emoji-tabs");
    tabs.replaceChildren();
    // 收藏 sits after QQ's own columns, matching where the client puts it.
    const columns = faceGroups.concat([{ key: "favorites", label: "收藏" }]);
    columns.forEach(group => {
      const tab = node("button", "nt-emoji-tab" + (group.key === activeFaceGroup ? " active" : ""), group.label);
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(group.key === activeFaceGroup));
      tab.addEventListener("click", event => {
        // The tab strip is rebuilt below; without stopping propagation the
        // detached node makes the document handler think the click was outside
        // the composer and it collapses the panel.
        event.stopPropagation();
        activeFaceGroup = group.key; renderFaceTabs(); renderFaceGrid();
        if (group.key === "favorites") {
          loadFavorites().then(() => { if (activeFaceGroup === "favorites") renderFaceGrid(); });
        }
      });
      tabs.appendChild(tab);
    });
  }

  function renderFaceGrid() {
    const grid = $("emoji-grid");
    grid.replaceChildren();
    if (activeFaceGroup === "favorites") {
      // Rendered separately: these are pictures, not QQ face ids.
      if (favoritesLoading || favoriteFaces === null) {
        grid.appendChild(node("p", "nt-emoji-empty", "正在读取收藏表情…"));
        return;
      }
      if (!favoriteFaces.length) {
        grid.appendChild(node("p", "nt-emoji-empty", "这个账号还没有收藏表情"));
        return;
      }
      favoriteFaces.forEach(face => grid.appendChild(favoriteButton(face)));
      return;
    }
    const group = faceGroups.find(item => item.key === activeFaceGroup) || faceGroups[0];
    if (!group) { grid.appendChild(node("p", "nt-emoji-empty", "暂无可用表情")); return; }
    group.faces.forEach(item => grid.appendChild(faceImageButton(item)));
  }

  async function loadFaceCatalog() {
    try {
      const data = await api("/api/chat/faces");
      const groups = (data.groups || []).filter(group => group.faces && group.faces.length);
      const recent = recentFaces();
      faceGroups = [{ key: "recent", label: "最近", faces: recent.map(id => ({ id })) }, ...groups];
      activeFaceGroup = recent.length ? "recent" : (groups[0] ? groups[0].key : "recent");
      renderFaceTabs(); renderFaceGrid();
    } catch (error) {
      $("emoji-grid").replaceChildren(node("p", "nt-emoji-empty", "表情加载失败：" + error.message));
    }
  }

  document.getElementById("emoji-panel").addEventListener("click", event => event.stopPropagation());
  $("emoji-toggle").addEventListener("click", () => {
    const panel = $("emoji-panel");
    panel.hidden = !panel.hidden;
    $("emoji-toggle").setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) {
      // Refresh so 最近 reflects what was just used.
      const recent = recentFaces();
      const entry = faceGroups.find(item => item.key === "recent");
      if (entry) { entry.faces = recent.map(id => ({ id })); }
      if (!faceGroups.length) loadFaceCatalog();
      else { if (!recent.length && activeFaceGroup === "recent") activeFaceGroup = faceGroups[1] ? faceGroups[1].key : "recent"; renderFaceTabs(); renderFaceGrid(); }
    }
  });
  window.addEventListener("beforeunload", saveDraft);
  let rounds = 0;
  const timer = setInterval(async () => {
    if (document.hidden || state.busy) return;
    state.busy = true;
    try { if (++rounds % 4 === 0) await loadAccounts(); if (rounds % 2 === 0) await loadConversations(); await loadHistory(false); }
    finally { state.busy = false; }
  }, 3000);
  window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
  loadAccounts(); updateComposer();
})();
