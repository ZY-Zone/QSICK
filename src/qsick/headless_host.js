// QSICK headless host: loads QQ's wrapper.node through our own QQNT.dll shim,
// initializes the engine + login service, and serves QR/status to QSICK.
"use strict";

const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const codec = require("./headless_codec.cjs");
const { createActionDispatcher, ActionError } = require("./headless_actions.cjs");
const { createMessageSender } = require("./headless_sender.cjs");
const { createQuickLogin } = require("./headless_login.cjs");
const { createMediaPreparer } = require("./headless_media.cjs");
const { createNoticeExtractor } = require("./headless_notices.cjs");

const configPath = process.argv[process.argv.indexOf("--config") + 1];
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
// appDir is the layer that actually holds wrapper.node. For a full client that
// is runtime/versions/<ver>/resources/app; for the older flattened layout it is
// the runtime root. Falling back keeps hand-made runtimes and pipe instances
// (which ship a flattened template) working unchanged.
const runtime = config.appDir || config.runtime;
const dataDir = path.join(config.dataDir, "qq");
fs.mkdirSync(dataDir, { recursive: true });
process.chdir(runtime);

const status = {
  id: config.id,
  phase: "starting",
  pid: process.pid,
  qr: "",
  uin: "",
  uid: "",
  nickname: "",
  error: "",
  updated_at: Math.floor(Date.now() / 1000)
};

function postCore(kind, payload) {
  if (!config.corePort) return;
  fetch("http://127.0.0.1:" + config.corePort + "/api/headless/event", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + config.bridgeToken,
      "X-QSICK-Instance": config.id },
    body: JSON.stringify(codec.toJsonSafe({ kind: kind, cmd: kind === "notice" ? payload.cmd : undefined, payload: payload }))
  }).then(function (response) {
    if (!response.ok) debug("core rejected " + kind + ": " + response.status);
  }).catch(function (error) {
    debug("core post failed: " + String(error && error.message ? error.message : error));
  });
}

const messageRefs = new Map();
const mediaPaths = new Map(); // "messageId:elementId" -> local file path
// emitMessage may see the same message twice (onRecvMsg then onMsgInfoListUpdate).
const mediaScheduled = new Set();
const media = createMediaPreparer({ getSession: () => (status.phase === "ready" ? activeSession : null) });
const uinByUid = new Map();
const uidByUin = new Map();
const sentSeen = new Set();
let nativeMessageListener = null;
let nativeMessageService = null;
let nativeMessageListenerId = null;

// System notices arrive as messages whose only element is a gray tip, so the extractor reads the same raw
// message the codec does. See headless_notices.cjs for which structures it trusts.
const notices = createNoticeExtractor({
  resolveUin: uid => uinByUid.get(uid),
  debug: message => debug(message),
  selfUin: () => activeUin,
});

function rememberRawMessage(raw) {
  for (const pair of [[raw.senderUid, raw.senderUin], [raw.peerUid, raw.peerUin]]) {
    const [uid, uin] = pair;
    if (typeof uin === "number" && !Number.isSafeInteger(uin)) continue;
    if (typeof uid === "string" && /^u_[A-Za-z0-9_-]+$/.test(uid) && /^[1-9]\d*$/.test(String(uin || ""))) {
      uinByUid.set(uid, String(uin)); uidByUin.set(String(uin), uid);
    }
  }
}
// Received pictures are not cached by QQ in headless mode, so their element only
// carries a file id and a dead download URL. Downloading them into QQ's own media
// tree is what makes them viewable in the WebUI.
function scheduleMediaFetch(raw, mapped) {
  const key = String(mapped.message_id);
  if (mediaScheduled.has(key)) return;
  const elements = raw && Array.isArray(raw.elements) ? raw.elements : [];
  if (!elements.length || !mapped || !mapped.native) return;
  const wanted = [];
  elements.forEach(function (element) {
    const type = String(element && element.elementType);
    if (type !== "2" && type !== "3" && type !== "4" && type !== "5") return;
    const field = { "2": "picElement", "3": "fileElement", "4": "pttElement", "5": "videoElement" }[type];
    const body = element[field];
    if (!body || !element.elementId) return;
    wanted.push({ elementId: String(element.elementId), field, body, type });
  });
  if (!wanted.length) {
    debug("media fetch skipped: no downloadable elements in " + String(mapped.message_id));
    return;
  }
  // 只有真的排了下载才记账：早退分支也记的话，第一次拿不到元素，后续重试就全被
  // 自己的去重挡住——表现正是"有时候自动下载不生效"。
  if (mediaScheduled.size > 10000) mediaScheduled.delete(mediaScheduled.values().next().value);
  mediaScheduled.add(key);
  debug("media fetch start " + String(mapped.message_id) + " elements=" + wanted.length);
  Promise.all(wanted.map(function (item) {
    return media.downloadReceived(mapped.native, { id: item.elementId, sourcePath: item.body.sourcePath, thumbPath: item.body.thumbPath })
      .then(function (filePath) {
        if (!filePath) { debug("media fetch returned no path for " + item.elementId); return; }
        debug("media fetch ok " + item.elementId + " -> " + filePath);
        mediaPaths.set(String(mapped.message_id) + ":" + item.elementId, filePath);
        // Re-publish the message with the resolved local path so late UI polls
        // and the SSE stream converge on the same data.
        postCore("message_media", {
          message_id: mapped.message_id,
          account: mapped.account,
          chat: mapped.chat,
          element_id: item.elementId,
          path: filePath
        });
      })
      .catch(function (error) { debug("media fetch failed: " + (error && error.message)); });
  })).catch(function () {});
}

function emitMessage(raw, uin, sent) {
  if (!raw || typeof raw !== "object") return;
  rememberRawMessage(raw);
  const mapped = codec.normalizeMessage(raw, String(uin), {
    resolveUin: uid => uinByUid.get(uid),
    onSkip: reason => debug("message skipped: " + JSON.stringify(codec.toJsonSafe(reason)))
  });
  if (!mapped) return;
  // A message that carries nothing but a system tip is a notice, not a chat message. Posting it as a
  // message put "[未知消息]" in front of clients for every group rename and every mute; the notice below
  // is the honest representation of the same event.
  const systemOnly = notices.isSystemOnly(raw);
  for (const notice of notices.inspect(raw, mapped)) {
    // postCore wants the cmd inside the payload for kind "notice" -- it forwards `payload.cmd` as the
    // envelope's cmd, and the Python side derives `notice_type` from it.
    postCore("notice", Object.assign({ cmd: notice.cmd }, notice.payload));
  }
  if (systemOnly) {
    debug("system tip carried no chat content; posted as notice only");
    return;
  }
  // 自己发出的消息同样要知道图片在哪：从手机发出的图在电脑侧只是"已发送"，
  // 本地并没有副本，聊天栏因此显示 [图片]。以前这里写成 ``if (!sent)``，等于
  // 假定"自己发的图一定已经有本地文件"——只有从 WebUI 发出去的那条成立。
  scheduleMediaFetch(raw, mapped);
  messageRefs.set(mapped.message_id, mapped.native);
  while (messageRefs.size > 10000) messageRefs.delete(messageRefs.keys().next().value);
  if (sent) {
    if (sentSeen.has(mapped.message_id)) return;
    sentSeen.add(mapped.message_id);
    if (sentSeen.size > 10000) sentSeen.delete(sentSeen.values().next().value);
  }
  postCore(sent ? "message_sent" : "message", mapped);
}
function markReady() {
  if (!messageListenerAttached) return;
  if (sessionInitTimer) { clearTimeout(sessionInitTimer); sessionInitTimer = null; }
  rememberLogin(activeUin, activeUid);
  publish({ phase: "ready", error: "" });
  fs.writeFileSync(path.join(config.dataDir, "session.json"), JSON.stringify({ uin: activeUin, uid: activeUid, ready: true }));
  postCore("login", { logged_in: true, online: true, uin: activeUin, nickname: status.nickname || "" });
}

function attachMessageListener(session, uin) {
  if (!session || messageListenerAttached) return messageListenerAttached;
  try {
    var service = session.getMsgService();
    debug("msg service=" + typeof service + " add=" + (service ? typeof service.addKernelMsgListener : "n/a"));
    if (!service) { debug("no msg service"); return false; }
    if (typeof service.addKernelMsgListener !== "function") { debug("no addKernelMsgListener"); return false; }
    var listener = new Proxy({
      onRecvMsg: function (messages) {
        for (const raw of Array.isArray(messages) ? messages : []) {
          try { emitMessage(raw, uin, String(raw.senderUin) === String(uin)); }
          catch (error) { debug("message mapping failed: " + error.message); }
        }
      },
      onAddSendMsg: function (raw) { sender.observe(raw); },
      onKickedOffLine: function (info) {
        // QQ tells us exactly why the session died, including whether Tencent
        // security kicked it. Surface that instead of a generic failure.
        const detail = info && typeof info === "object" ? info : {};
        const securityType = Number(detail.securityKickedType || 0);
        const kickedType = Number(detail.kickedType || 0);
        const message = String(detail.tipsDesc || detail.tipsTitle || "").trim();
        debug("kicked offline type=" + kickedType + " security=" + securityType + " tips=" + message);
        publish({
          phase: "kicked",
          qr: "",
          kicked_type: kickedType,
          security_kicked_type: securityType,
          same_device: Boolean(detail.sameDevice),
          error: message || (securityType ? "登录被安全策略强制下线" : "账号已在其他设备登录")
        });
        postCore("login", { logged_in: false, online: false, uin: activeUin, nickname: "", kicked: true,
          security_kicked_type: securityType, kicked_type: kickedType, reason: message });
      },
      onMsgInfoListUpdate: function (messages) {
        sender.observe(messages);
        for (const raw of Array.isArray(messages) ? messages : []) {
          if (String(raw.senderUin) === String(uin) && [2, 3, "2", "3"].includes(raw.sendStatus)) emitMessage(raw, uin, true);
          if (raw.recallTime && String(raw.recallTime) !== "0") {
            const mapped = codec.normalizeMessage(raw, String(uin), { resolveUin: uid => uinByUid.get(uid) });
            if (mapped) postCore("notice", { cmd: mapped.chat.type === "group" ? "notice.group_recall" : "notice.friend_recall",
              self_id: String(uin), message_id: mapped.message_id, user_id: mapped.sender.uin,
              ...(mapped.chat.type === "group" ? { group_id: mapped.chat.id } : {}) });
          }
        }
      }
    }, { get: function (target, prop) { return target[prop] || function () {}; } });
    nativeMessageListenerId = service.addKernelMsgListener(listener);
    nativeMessageListener = listener;
    nativeMessageService = service;
    messageListenerAttached = true;
    try { login.setRemerberPwd(true); } catch (_) {}
    debug("message listener attached");
    markReady();
    return true;
  } catch (error) {
    debug("message listener failed: " + String(error && error.message ? error.message : error));
    return false;
  }
}

function attachMessageListenerWithRetry(session, uin, attempt) {
  if (session !== activeSession || String(uin) !== String(activeUin) || status.phase === "error") return;
  if (messageListenerAttached) return;
  if (attachMessageListener(session, uin)) return;
  if (attempt >= 15) return;
  setTimeout(function () { attachMessageListenerWithRetry(session, uin, attempt + 1); }, 1000);
}

function debug(message) {
  try { fs.appendFileSync(path.join(config.dataDir, "host-debug.log"), new Date().toISOString() + " " + message + "\n"); } catch (_) {}
}

function publish(patch) {
  Object.assign(status, patch, { updated_at: Math.floor(Date.now() / 1000) });
  fs.writeFileSync(path.join(config.dataDir, "status.json"), JSON.stringify(status));
}

function fail(error) {
  if (sessionInitTimer) { clearTimeout(sessionInitTimer); sessionInitTimer = null; }
  sender.close();
  publish({ phase: "error", error: String(error && error.message ? error.message : error) });
  postCore("login", { logged_in: false, online: false, uin: activeUin || "0", nickname: "" });
}

let wrapper = null;
let login = null;
let listenerId = null;
let savedLogin = null;
let quickLoginAttempted = false;
let messageListenerAttached = false;
let activeSession = null;
let activeUin = "";
let activeUid = "";
let wrapperSession = null;
let startupSession = null;
let sessionInitTimer = null;

function machineGuid() {
  let value = "";
  try { value = String(login.getMachineGuid() || ""); } catch (_) {}
  if (value.length >= 32) {
    value = value.slice(0, 8) + "-" + value.slice(8, 12) + "-" + value.slice(12, 16) + "-" +
      value.slice(16, 20) + "-" + value.slice(20);
  }
  return value;
}

function persistAccount(uin, uid) {
  const account = { id: config.id, qq: uin, uid, port: config.port };
  try {
    fs.writeFileSync(path.join(path.dirname(config.dataDir), "instance.json"), JSON.stringify(account, null, 2));
  } catch (_) {}
}

function rememberLogin(uin, uid) {
  savedLogin = { uin: String(uin || ""), uid: String(uid || ""), at: Date.now() };
  fs.writeFileSync(path.join(config.dataDir, "login.json"), JSON.stringify(savedLogin));
  persistAccount(savedLogin.uin, savedLogin.uid);
}

async function initSession(uin, uid) {
  debug("initSession " + uin + " " + uid);
  publish({ phase: "initializing", error: "" });
  if (sessionInitTimer) clearTimeout(sessionInitTimer);
  sessionInitTimer = setTimeout(function () {
    if (!messageListenerAttached) {
      publish({ phase: "error", error: "session init timed out before the message service became ready" });
    }
  }, 20000);
  const startup = startupSession || wrapper.NodeIQQNTStartupSessionWrapper.create();
  const session = wrapperSession || wrapper.NodeIQQNTWrapperSession.getNTWrapperSession("nt_1");
  sender.close();
  try { if (nativeMessageService && nativeMessageListenerId !== undefined) nativeMessageService.removeKernelMsgListener(nativeMessageListenerId); } catch (_) {}
  nativeMessageService = null; nativeMessageListener = null; nativeMessageListenerId = null;
  messageRefs.clear(); uinByUid.clear(); uidByUin.clear(); sentSeen.clear();
  messageListenerAttached = false;
  activeSession = session;
  activeUin = uin;
  activeUid = uid;
  const platform = 3;
  const cfg = {
    selfUin: uin,
    selfUid: uid,
    // NapCat keeps account data next to the global store (dataPath), with the
    // global directory at <dataPath>/nt_qq/global. Splitting them made QQ look
    // for a ticket in one tree while writing it into another.
    desktopPathConfig: { account_path: path.join(dataDir, "qq") },
    clientVer: config.version,
    a2: "",
    d2: "",
    d2Key: "",
    machineId: "",
    platform,
    platVer: os.release(),
    appid: String(config.appid),
    rdeliveryConfig: {
      appKey: "", systemId: 0, appId: "", logicEnvironment: "", platform,
      language: "", sdkVersion: "", userId: "", appVersion: "", osVersion: "", bundleId: "",
      serverUrl: "", fixedAfterHitKeys: [""]
    },
    defaultFileDownloadPath: path.join(dataDir, "download"),
    deviceInfo: {
      guid: machineGuid(), buildVer: config.version, localId: 2052,
      devName: os.hostname(), devType: "Windows", vendorName: "",
      osVer: os.release(), vendorOsName: "Windows", setMute: false, vendorType: 0
    },
    deviceConfig: '{"appearance":{"isSplitViewMode":true},"msg":{}}'
  };
  const depends = { onMSFStatusChange() {}, onMSFSsoError() {}, getGroupCode() {} };
  const dispatcher = { dispatchRequest() {}, dispatchCall() {}, dispatchCallWithJson() {} };
  const listener = {
    onSessionInitComplete() {
      debug("session init complete");
      attachMessageListenerWithRetry(session, uin, 0);
      markReady();
    },
    onOpentelemetryInit() {},
    onNTSessionCreate() {},
    onGProSessionCreate() {},
    onUserOnlineResult() {},
    onGetSelfTinyId() {}
  };
  fs.writeFileSync(path.join(config.dataDir, "session.json"), JSON.stringify({ uin, uid, ready: false }));
  session.init(cfg, depends, dispatcher, listener);
  debug("session methods=" + Object.getOwnPropertyNames(Object.getPrototypeOf(session)).filter(function (name) {
    return name.toLowerCase().indexOf("msg") >= 0;
  }).join(","));
  // Do not rely on callback ordering in wrapper.node; retry once the service is live.
  setTimeout(function () { attachMessageListenerWithRetry(session, uin, 0); }, 1500);
  try {
    if (startup && typeof startup.start === "function") startup.start();
  } catch (error) {
    debug("startup.start failed: " + String(error && error.message ? error.message : error));
  }
  // Readiness is published only when the message listener is attached.
}

function savedLoginInfo(requireReady = true) {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(config.dataDir, "login.json"), "utf8"));
    if (!value || !/^[1-9]\d*$/.test(String(value.uin || "")) || !/^u_[A-Za-z0-9_-]+$/.test(String(value.uid || ""))) return null;
    if (requireReady) {
      const session = JSON.parse(fs.readFileSync(path.join(config.dataDir, "session.json"), "utf8"));
      if (!session || !session.ready || String(session.uin) !== String(value.uin)) return null;
    }
    return value;
  } catch (_) { return null; }
}

let qrRetryTimer = null;
let qrRequestedAt = 0;

function requestQr(message, options) {
  // 自动重连也会来这里刷二维码。如果无脑把 phase 改成 waiting_qrcode，
  // 被踢下线的状态和原因就会被冲掉——用户只看到"未登录"，完全不知道自己
  // 是被踢的。默认保留；只有用户主动要求登录时才翻篇。
  const manual = Boolean(options && options.manual);
  if (manual || status.phase !== "kicked") {
    publish({ phase: "waiting_qrcode", error: message || "" });
  } else {
    debug("QR requested after a kick: keeping the kicked phase visible");
  }
  if (!login) return;
  qrRequestedAt = Date.now();
  let accepted = false;
  try { accepted = login.getQRCodePicture() !== false; } catch (error) { fail(error); return; }
  debug("QR requested accepted=" + accepted);
  // QQ sometimes stays silent after a picture was already produced once. Retry
  // a few times so the panel cannot sit permanently blank.
  if (qrRetryTimer) clearTimeout(qrRetryTimer);
  let attempt = 0;
  const retry = function () {
    if (status.qr_at && status.qr_at >= qrRequestedAt && status.qr) return;
    if (attempt >= 4 || status.phase === "ready" || status.phase === "initializing") return;
    attempt += 1;
    debug("QR retry " + attempt);
    try { login.getQRCodePicture(); } catch (_) {}
    qrRetryTimer = setTimeout(retry, 1500);
  };
  qrRetryTimer = setTimeout(retry, 1500);
}

function tryQuickLogin() {
  if (!savedLogin || quickLoginAttempted || !login) return false;
  quickLoginAttempted = true;
  quickLogin.attempt().catch(function (error) {
    debug("automatic quick login failed [" + (error.code || "ERROR") + "]: " + error.message);
    if (error.code !== "SUPERSEDED" && status.phase !== "ready") requestQr(error.message);
  });
  return true;
}

/**
 * Resolve the directory QQ keeps login state in.
 *
 * This is the one detail that decides whether quick login can ever work: QQ
 * stores its reusable login ticket under the GLOBAL path, which on Windows is
 * "<data path>/nt_qq/global" — not the data root itself. Pointing commonPath and
 * desktopGlobalPath at the root (as QSICK used to do) leaves the ticket nowhere
 * to live, so every restart reports "登录态已失效" and demands a fresh QR scan.
 * Reference: NapCat packages/napcat-shell/data-path.ts getDataPaths().
 */
function resolveQQRoot(wrapper) {
  // QQ's own helper points at the REAL client data directory (for example
  // {QQ 文件目录}\Tencent Files). Using it would make every QSICK instance
  // share — and fight over — the live QQ profile, so instances are pinned to
  // their own folder instead. QSICK_DATA_ROOT only overrides for testing.
  if (config.dataRoot) return String(config.dataRoot);
  return path.join(dataDir, "qq");
}

function resolveGlobalPath(wrapper) {
  const platform = os.platform();
  const root = resolveQQRoot(wrapper);
  // Keep each instance isolated while matching QQ's own layout.
  const globalDir = platform === "linux" || platform === "darwin"
    ? path.join(root, "global")
    : path.join(root, "nt_qq", "global");
  try { fs.mkdirSync(globalDir, { recursive: true }); } catch (_) {}
  return globalDir;
}

function loadWrapper() {
  wrapper = require(path.join(runtime, "wrapper.node"));
  savedLogin = savedLoginInfo();
  try {
    startupSession = wrapper.NodeIQQNTStartupSessionWrapper.create();
    wrapperSession = wrapper.NodeIQQNTWrapperSession.getNTWrapperSession("nt_1");
    debug("preinit session=" + typeof wrapperSession);
  } catch (error) {
    debug("preinit session failed: " + String(error && error.message ? error.message : error));
  }
  debug("loadWrapper saved=" + JSON.stringify(savedLogin));
  const dataPath = resolveGlobalPath(wrapper);
  let clientDir = "";
  try { clientDir = String(wrapper.NodeQQNTWrapperUtil.getNTUserDataInfoConfig() || ""); } catch (_) {}
  debug("dataPath global=" + dataPath + " (client dir ignored: " + (clientDir || "<none>") + ")");
  const adapter = new Proxy({}, { get: () => function () {} });
  wrapper.NodeIQQNTWrapperEngine.get().initWithDeskTopConfig(
    {
      base_path_prefix: "",
      platform_type: config.platformType || 3,
      app_type: 4,
      app_version: config.version,
      os_version: os.release(),
      use_xlog: false,
      qua: config.qua,
      // Login tickets live under the global path, so both the engine and the
      // login service must agree on it; account data still stays per instance.
      global_path_config: { desktopGlobalPath: dataPath },
      thumb_config: { maxSide: 324, minSide: 48, longLimit: 6, density: 2 }
    },
    adapter
  );
  login = wrapper.NodeIKernelLoginService.get();
  login.initConfig({
    machineId: "",
    appid: String(config.appid),
    platVer: os.release(),
    commonPath: dataPath,
    clientVer: config.version,
    hostName: os.hostname(),
    externalVersion: false
  });
  const listener = {
    onQRCodeGetPicture: (data) => {
      // Never drop a QR that QQ actually produced. A quick-login attempt may be
      // in flight, but if QQ hands us a picture the user must be able to see it
      // (the attempt may fail, and a blank panel is unusable). Only a session
      // that is already initialising or ready makes a QR meaningless.
      if (status.phase === "initializing" || status.phase === "ready") return;
      if (qrRetryTimer) { clearTimeout(qrRetryTimer); qrRetryTimer = null; }
      publish({ phase: "waiting_qrcode", qr: data.pngBase64QrcodeData || "", error: "", qr_at: Date.now() });
    },
    onQRCodeSessionUserScaned: () => publish({ phase: "scanned" }),
    onQRCodeSessionFailed: (errType, errCode) => {
      debug("QR session failed " + errType + "/" + errCode);
      publish({ phase: "qrcode_failed", qr: "", error: "二维码已失效，正在重新获取" });
      // A dead code is useless; fetch a fresh one instead of leaving a blank box.
      if (status.phase !== "ready" && status.phase !== "initializing") setTimeout(function () { requestQr(""); }, 800);
    },
    onLoginFailed: (...args) => publish({ phase: "login_failed", error: JSON.stringify(args) }),
    onLoginDisConnected: () => {
      // Only meaningful once we had a session; before that it is just startup.
      if (status.phase !== "ready" && status.phase !== "initializing") return;
      debug("login service disconnected");
      // 被踢之后 QQ 也会走到这里。如果直接把 phase 改成 offline，用户就再也
      // 看不到"被踢下线"和原因了——而那正是他需要知道的事。保留 kicked，
      // 只补一句连接已断开，等下次真正登录成功再翻篇。
      if (status.phase === "kicked") {
        debug("disconnect after kick: keeping the kicked phase");
        postCore("login", { logged_in: false, online: false, uin: activeUin, nickname: "", disconnected: true });
        return;
      }
      publish({ phase: "offline", qr: "", error: "登录连接已断开" });
      postCore("login", { logged_in: false, online: false, uin: activeUin, nickname: "", disconnected: true });
    },
    onLoginConnected: () => {
      debug("onLoginConnected");
      if (quickLogin.isBusy() || status.phase === "ready" || status.phase === "initializing") return;
      publish({ phase: "connected" });
      if (config.autoLogin === false) { requestQr(""); return; }
      // Start the attempt, but always ask for a QR as well: if the ticket turns
      // out to be missing the user needs a scannable code immediately rather
      // than a panel that only fills in after the attempt gives up.
      if (tryQuickLogin()) { requestQr(""); return; }
      requestQr("");
    },
    onQRCodeLoginSucceed: (result) => {
      quickLogin.invalidate();
      const uin = String(result.uin || "");
      const uid = String(result.uid || "");
      publish({ phase: "initializing", uin: uin, uid: uid, error: "" });
      initSession(uin, uid).catch(fail);
    },
    onUserLoggedIn: (uin) => {
      if (status.phase === "ready" || quickLogin.isBusy() || status.phase === "initializing") return;
      publish({ phase: "logged_in", uin: String(uin || "") });
    }
  };
  listenerId = login.addKernelLoginListener(listener);
  const connected = login.connect();
  if (!connected) throw new Error("login service connect() returned false");
  // onLoginConnected is not guaranteed on every build; without it the panel would
  // stay blank forever. Ask for a picture shortly after connecting regardless.
  setTimeout(function () {
    if (status.phase === "ready" || status.phase === "initializing") return;
    if (status.qr) return;
    debug("no QR yet after connect; requesting");
    requestQr("");
  }, 2500);
}

function readJson(request) {
  return new Promise(function (resolve, reject) {
    const chunks = []; let size = 0;
    request.on("data", function (chunk) {
      size += chunk.length;
      if (size > 8 * 1024 * 1024) { reject(new ActionError("INVALID_PARAMS", "request body too large")); return; }
      chunks.push(chunk);
    });
    request.on("end", function () {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}")); }
      catch (_) { reject(new ActionError("INVALID_PARAMS", "invalid JSON")); }
    });
    request.on("error", reject);
  });
}
const actions = createActionDispatcher({
  getSession: () => status.phase === "ready" ? activeSession : null,
  getIdentity: () => ({ uin: activeUin, uid: activeUid, nickname: status.nickname || "", logged_in: status.phase === "ready" }),
  resolveUid: uin => uidByUin.get(String(uin)),
  messageRefs, codec,
  sendMessage: params => sender.send(params)
});
const sender = createMessageSender({
  getSession: () => status.phase === "ready" ? activeSession : null,
  getIdentity: () => ({ uin: activeUin }),
  resolveUid: uin => actions.resolveUid(uin), codec, messageRefs,
  onSent: raw => emitMessage(raw, activeUin, true)
});
const quickLogin = createQuickLogin({
  getLogin: () => login,
  getState: () => status,
  getLastAccount: () => savedLoginInfo(false),
  initialize: initSession,
  publish
});

function authorized(request) {
  const expected = Buffer.from("Bearer " + String(config.bridgeToken || ""));
  const actual = Buffer.from(String(request.headers.authorization || ""));
  return Boolean(config.bridgeToken) && actual.length === expected.length && crypto.timingSafeEqual(expected, actual);
}
function reply(response, statusCode, body) {
  response.writeHead(statusCode, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(codec.toJsonSafe(body)));
}

const server = http.createServer(async (request, response) => {
  if (request.url !== "/health" && !authorized(request)) {
    reply(response, 401, { ok: false, code: "UNAUTHORIZED", error: "bridge authorization required" }); return;
  }
  if (request.url === "/capabilities" && request.method === "GET") {
    reply(response, 200, { ok: true, data: actions.capabilities() }); return;
  }
  if ((request.url === "/action" || request.url === "/send") && request.method === "POST") {
    try {
      const body = await readJson(request);
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new ActionError("INVALID_PARAMS", "object required");
      if (body.account && String(body.account) !== String(activeUin)) throw new ActionError("INVALID_PARAMS", "selected account does not match host session");
      const op = request.url === "/send" ? "message.send" : body.op;
      const params = request.url === "/send" ? { chat: body.chat, elements: [{ type: "text", data: { text: body.text } }] } : body.params;
      const data = op === "runtime.capabilities" ? actions.capabilities() : await actions.execute(op, params || {});
      reply(response, 200, { ok: true, data });
    } catch (error) {
      const code = error.code || "SERVICE_ERROR";
      debug("action failed [" + code + "]: " + error.message);
      reply(response, code === "INVALID_PARAMS" ? 400 : code === "UNSUPPORTED" ? 501 : 502,
        { ok: false, code, error: String(error.message || error), details: error.details || {} });
    }
    return;
  }
  if (request.url === "/quick-login" && request.method === "POST") {
    try {
      const result = await quickLogin.attempt();
      reply(response, 200, { ok: true, ...result });
    } catch (error) {
      debug("manual quick login failed [" + (error.code || "ERROR") + "]: " + error.message);
      reply(response, error.code === "BUSY" ? 409 : error.code === "NO_HISTORY" ? 404 : 502,
        { ok: false, code: error.code || "LOGIN_FAILED", error: String(error.message || error) });
    }
    return;
  }
  if (request.url === "/refresh" && request.method === "POST") {
    try {
      if (!login) throw new Error("login service is not ready");
      if (quickLogin.isBusy() || status.phase === "initializing" || status.phase === "ready") throw new Error("登录或会话已在运行，不应重复刷新二维码");
      const requested = login.getQRCodePicture();
      if (!requested) throw new Error("QQ rejected the QR refresh request");
      // 用户主动点"刷新二维码"= 他决定重新登录，此时才把被踢的状态翻篇。
      publish({ phase: "waiting_qrcode", error: "" });
      response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      response.end(JSON.stringify({ ok: true, status }));
    } catch (error) {
      response.writeHead(503, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      response.end(JSON.stringify({ ok: false, error: String(error && error.message ? error.message : error) }));
    }
    return;
  }
  if (request.url === "/status") {
    response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    response.end(JSON.stringify(status));
    return;
  }
  if (request.url === "/health") {
    response.writeHead(200, { "Content-Type": "text/plain" });
    response.end("ok");
    return;
  }
  response.writeHead(404);
  response.end("not found");
});

try {
  loadWrapper();
  server.listen(config.port, "127.0.0.1");
  // Keep the phase emitted by connect()/login callbacks, including synchronous ones.
  setInterval(() => publish({}), 3000);
  if (config.parentPid) {
    setInterval(function () {
      try {
        process.kill(Number(config.parentPid), 0);
      } catch (_) {
        debug("parent process is gone, exiting");
        process.exit(0);
      }
    }, 2000);
  }
} catch (error) {
  fail(error);
  setTimeout(() => process.exit(1), 100);
}

process.on("SIGTERM", () => {
  try { if (login && listenerId !== undefined) login.removeKernelLoginListener(listenerId); } catch (_) {}
  sender.close();
  try { if (nativeMessageService && nativeMessageListenerId !== undefined) nativeMessageService.removeKernelMsgListener(nativeMessageListenerId); } catch (_) {}
  postCore("login", { logged_in: false, online: false, uin: activeUin, nickname: "" });
  try { server.close(); } catch (_) {}
  publish({ phase: "stopped" });
  process.exit(0);
});
process.on("SIGINT", () => process.emit("SIGTERM"));
process.on("uncaughtException", fail);
process.on("unhandledRejection", fail);
