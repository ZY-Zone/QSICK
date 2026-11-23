/* 光标包：ZIP 里的 cursor.json + 图片，全部类型都由包提供。
 *
 * ## 为什么不再有内置样式
 *
 * 内置的三套箭头需要"跟随元素"来绘制，而跟随元素无法覆盖每一个类型——于是某些位置上
 * 自绘箭头和系统光标会同时出现，也就是"两个指针叠在一起"。光标包走的是 CSS `cursor`，
 * 浏览器原生支持全部类型，不会有第二个指针。
 *
 * ## 不叠加的保证
 *
 * 应用时先写一条通配规则 `body, body *` → 包的初始箭头，再按类型逐条覆盖。
 * 包里没有定义的类型**继承初始箭头**，而不是退回系统光标——退回系统光标正是叠加的来源。
 *
 * ## cursor.json
 *
 *     {
 *       "name": "我的光标包",
 *       "description": "不超过十二个字",
 *       "cursors": { "default": "arrow.png", "pointer": "hand.png", ... },
 *       "hotspots": { "default": [1, 1] }
 *     }
 */
(function () {
  "use strict";

  var STORAGE_CHOICE = "qsick.cursor";
  var STORAGE_PACK = "qsick.cursor.pack";
  var MAX_PACK_BYTES = 3.2 * 1024 * 1024;
  var MAX_DESCRIPTION = 12;

  //: 需要覆盖的状态。
  //:
  //: 只有四种。光标包要求作者为每个状态画一张图并标热点，而界面里根本不会出现的状态
  //: 就是白做的工——更糟的是，做了却看不到会让人以为功能坏了。所以清单只留那些
  //: "不需要额外标记就能出现在屏幕上的"：
  //:   default     — 页面本身
  //:   pointer     — 链接、按钮、下拉、开关
  //:   text        — 输入框、文本域
  //:   not-allowed — 禁用的控件
  //: 加回一种只需在 TYPES 与 TYPE_SELECTORS 各加一行，并在 cursor_types.py 里同步。
  var TYPES = ["default", "pointer", "text", "not-allowed"];

  //: 每个状态的匹配规则。`default` 不在这里——它是通配兜底。
  var TYPE_SELECTORS = {
    pointer: 'a, button, select, summary, label, .dd-option, .cursor-option, ' +
             'input[type="checkbox"], input[type="radio"], input[type="submit"], ' +
             '[role="button"], .nav a, .subnav a',
    text: 'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), ' +
          'textarea',
    "not-allowed": ':disabled, [aria-disabled="true"], .is-disabled'
  };

  function read(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }

  function readPack() {
    var raw = read(STORAGE_PACK);
    if (!raw) return null;
    try {
      var pack = JSON.parse(raw);
      if (!pack || !pack.cursors || !pack.cursors.default) return null;
      return pack;
    } catch (_) { return null; }
  }

  // ------------------------------------------------------------------ ZIP

  function readZipDirectory(buffer) {
    var view = new DataView(buffer);
    var size = buffer.byteLength;
    var eocd = -1;
    for (var offset = size - 22; offset >= Math.max(0, size - 22 - 65535); offset -= 1) {
      if (view.getUint32(offset, true) === 0x06054b50) { eocd = offset; break; }
    }
    if (eocd < 0) throw new Error("不是有效的 ZIP 文件");
    var count = view.getUint16(eocd + 10, true);
    var start = view.getUint32(eocd + 16, true);
    var entries = [];
    var pointer = start;
    for (var index = 0; index < count; index += 1) {
      if (view.getUint32(pointer, true) !== 0x02014b50) throw new Error("ZIP 中央目录损坏");
      var nameLength = view.getUint16(pointer + 28, true);
      var extraLength = view.getUint16(pointer + 30, true);
      var commentLength = view.getUint16(pointer + 32, true);
      entries.push({
        name: new TextDecoder("utf-8").decode(new Uint8Array(buffer, pointer + 46, nameLength)),
        method: view.getUint16(pointer + 10, true),
        compressedSize: view.getUint32(pointer + 20, true),
        offset: view.getUint32(pointer + 42, true)
      });
      pointer += 46 + nameLength + extraLength + commentLength;
    }
    return entries;
  }

  async function extractEntry(buffer, entry) {
    var view = new DataView(buffer);
    var nameLength = view.getUint16(entry.offset + 26, true);
    var extraLength = view.getUint16(entry.offset + 28, true);
    var dataStart = entry.offset + 30 + nameLength + extraLength;
    var raw = buffer.slice(dataStart, dataStart + entry.compressedSize);
    if (entry.method === 0) return raw;
    if (entry.method !== 8) throw new Error("只支持 store 与 deflate 压缩（" + entry.name + "）");
    if (typeof DecompressionStream === "undefined") throw new Error("浏览器不支持解压 deflate");
    var stream = new Blob([raw]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return await new Response(stream).arrayBuffer();
  }

  function mimeOf(name) {
    var lower = name.toLowerCase();
    if (lower.endsWith(".svg")) return "image/svg+xml";
    if (lower.endsWith(".png")) return "image/png";
    if (lower.endsWith(".webp")) return "image/webp";
    if (lower.endsWith(".ico") || lower.endsWith(".cur")) return "image/x-icon";
    if (lower.endsWith(".gif")) return "image/gif";
    return "";
  }

  function toDataUrl(buffer, mime) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    var chunk = 0x8000;
    for (var index = 0; index < bytes.length; index += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunk));
    }
    return "data:" + mime + ";base64," + btoa(binary);
  }

  /** 先解码一遍再采用：一张坏图会让 `cursor: url()` 静默退回系统箭头，也就是叠加。 */
  function canDecode(source) {
    return new Promise(function (resolve) {
      var image = new Image();
      image.onload = function () { resolve(image.naturalWidth > 0); };
      image.onerror = function () { resolve(false); };
      image.src = source;
    });
  }

  async function readCursorPack(file) {
    var buffer = await file.arrayBuffer();
    var entries = readZipDirectory(buffer);
    var manifestEntry = entries.find(function (entry) { return /(^|\/)cursor\.json$/i.test(entry.name); });
    if (!manifestEntry) throw new Error("包里没有 cursor.json");

    var manifest;
    try {
      manifest = JSON.parse(new TextDecoder("utf-8").decode(await extractEntry(buffer, manifestEntry)));
    } catch (_) {
      throw new Error("cursor.json 不是有效的 JSON");
    }
    if (!manifest || typeof manifest !== "object") throw new Error("cursor.json 顶层必须是对象");
    if (typeof manifest.name !== "string" || !manifest.name.trim()) {
      throw new Error("cursor.json 缺少 name");
    }
    if (typeof manifest.description !== "string" || !manifest.description.trim()) {
      throw new Error("cursor.json 缺少 description");
    }
    if (Array.from(manifest.description.trim()).length > MAX_DESCRIPTION) {
      throw new Error("description 不能超过 " + MAX_DESCRIPTION + " 个字");
    }
    if (!manifest.cursors || typeof manifest.cursors !== "object") {
      throw new Error("cursor.json 缺少 cursors 字段");
    }

    var base = manifestEntry.name.indexOf("/") >= 0
      ? manifestEntry.name.slice(0, manifestEntry.name.lastIndexOf("/") + 1) : "";

    var cursors = {};
    var hotspots = {};
    var warnings = [];
    for (var index = 0; index < TYPES.length; index += 1) {
      var type = TYPES[index];
      var fileName = manifest.cursors[type];
      if (!fileName) continue;
      var wanted = (base + fileName).toLowerCase();
      var entry = entries.find(function (item) { return item.name.toLowerCase() === wanted; })
        || entries.find(function (item) { return item.name.toLowerCase().endsWith("/" + wanted); });
      if (!entry) { warnings.push("找不到 " + fileName); continue; }
      var mime = mimeOf(entry.name);
      if (!mime) { warnings.push(fileName + " 不是支持的图片格式"); continue; }
      var source = toDataUrl(await extractEntry(buffer, entry), mime);
      if (!(await canDecode(source))) { warnings.push(fileName + " 无法解码"); continue; }
      cursors[type] = source;
      var spot = manifest.hotspots && manifest.hotspots[type];
      if (Array.isArray(spot) && spot.length === 2) {
        hotspots[type] = [Number(spot[0]) || 0, Number(spot[1]) || 0];
      }
    }
    if (!cursors.default) throw new Error("cursors.default 必须指向一张可以解码的图片");
    return {
      name: String(manifest.name).trim(),
      description: String(manifest.description).trim(),
      cursors: cursors,
      hotspots: hotspots,
      warnings: warnings
    };
  }

  // ------------------------------------------------------------------ 应用

  function applyPack(pack) {
    var existing = document.getElementById("qsick-cursor-pack");
    if (existing) existing.remove();
    if (!pack || !pack.cursors || !pack.cursors.default) return;

    var defaultSpot = (pack.hotspots && pack.hotspots.default) || [0, 0];
    var rules = [
      // 通配兜底：先让每一个元素都有包里的初始箭头。它同时是"没定义的类型"的归宿。
      'body, body * { cursor: url("' + pack.cursors.default + '") ' +
      defaultSpot[0] + " " + defaultSpot[1] + ", default !important; }"
    ];

    Object.keys(TYPE_SELECTORS).forEach(function (type) {
      var source = pack.cursors[type];
      if (!source) return;                 // 没定义：继承通配规则里的初始箭头，不退回系统光标
      var spot = (pack.hotspots && pack.hotspots[type]) || defaultSpot;
      // 回退关键字也写 default，而不是同名的系统光标：图片万一失效，至少还是一片箭头，
      // 不会出现"箭头 + I 形"两个指针。
      rules.push(TYPE_SELECTORS[type] + ' { cursor: url("' + source + '") ' +
                 spot[0] + " " + spot[1] + ", default !important; }");
    });

    var style = document.createElement("style");
    style.id = "qsick-cursor-pack";
    style.textContent = rules.join("\n");
    document.head.appendChild(style);
  }

  /** 把导入的包渲染成第一张卡片旁边的那张，和「系统默认」完全同构。 */
  function renderPackCard(pack) {
    var picker = document.getElementById("cursor-picker");
    if (!picker) return;
    var old = picker.querySelector('[data-cursor="custom"]');
    if (old) old.remove();
    if (!pack) return;

    var card = document.createElement("button");
    card.type = "button";
    card.className = "cursor-option";
    card.dataset.cursor = "custom";

    var preview = document.createElement("img");
    preview.className = "cursor-pack-preview";
    preview.src = pack.cursors.default;
    preview.alt = "";
    card.appendChild(preview);

    var name = document.createElement("strong");
    name.textContent = pack.name;
    card.appendChild(name);

    var description = document.createElement("small");
    description.textContent = pack.description;
    card.appendChild(description);

    picker.appendChild(card);
    var choice = read(STORAGE_CHOICE) || "default";
    picker.querySelectorAll(".cursor-option").forEach(function (button) {
      button.classList.toggle("active", button.dataset.cursor === choice);
    });
  }

  function install() {
    var pack = readPack();
    var choice = read(STORAGE_CHOICE) || "default";
    var active = choice === "custom" && pack;

    if (active) {
      document.documentElement.dataset.cursor = "custom";
      applyPack(pack);
    } else {
      document.documentElement.removeAttribute("data-cursor");
      applyPack(null);
    }
    renderPackCard(pack);
    return active;
  }

  // ------------------------------------------------------------------ 对外

  window.qsickSetCursor = function (choice) {
    try { localStorage.setItem(STORAGE_CHOICE, choice); } catch (_) {}
    install();
  };

  window.qsickImportCursorPack = function (file) {
    return new Promise(function (resolve) {
      if (!file) { resolve({ ok: false, error: "没有选择文件" }); return; }
      if (!/\.zip$/i.test(file.name)) { resolve({ ok: false, error: "请选择 .zip 文件" }); return; }
      if (file.size > MAX_PACK_BYTES) {
        resolve({ ok: false, error: "包超过 3.2MB，请压缩图片后再打包" });
        return;
      }
      readCursorPack(file).then(function (pack) {
        try {
          localStorage.setItem(STORAGE_PACK, JSON.stringify(pack));
          localStorage.setItem(STORAGE_CHOICE, "custom");
        } catch (_) {
          resolve({ ok: false, error: "浏览器存储已满，无法保存这个光标包" });
          return;
        }
        install();
        resolve({
          ok: true, warnings: pack.warnings, name: pack.name,
          description: pack.description, types: Object.keys(pack.cursors)
        });
      }).catch(function (error) {
        resolve({ ok: false, error: error.message || String(error) });
      });
    });
  };

  window.qsickClearCursorPack = function () {
    try {
      localStorage.removeItem(STORAGE_PACK);
      localStorage.setItem(STORAGE_CHOICE, "default");
    } catch (_) {}
    install();
  };

  window.qsickCursorPackInfo = function () {
    var pack = readPack();
    if (!pack) return null;
    return {
      name: pack.name,
      description: pack.description,
      types: Object.keys(pack.cursors),
      active: (read(STORAGE_CHOICE) || "default") === "custom"
    };
  };

  window.qsickCursorTypes = TYPES.slice();

  function bootstrap() { install(); }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
