'use strict';
/*
 * Media preparation for outgoing picture/file elements.
 *
 * QQ does not expose a separate upload RPC for chat media: the native layer
 * uploads when `sendMsg` runs. All we must do is hand QQ a well-formed element
 * whose sourcePath points at a readable local file inside its rich-media tree.
 * Contract reference: napcat-onebot/api/file.ts createValidSendPicElement /
 * createValidSendFileElement, and napcat-core/apis/file.ts uploadFile.
 *
 * This module performs real file I/O and native calls; callers must not pass
 * paths they cannot read.
 */
const fs = require('fs');
const fsp = require('fs/promises');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { ActionError } = require('./headless_actions.cjs');

const ElementType = Object.freeze({ PIC: 2, FILE: 3 });
const PicType = Object.freeze({ GIF: 2000, JPEG: 1000 });
const PicSubType = Object.freeze({ NORMAL: 0, STICKER: 1, HOT: 2 });
const PIC_SUB_TYPES = Object.freeze(Object.values(PicSubType));
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;
const HEAD_BYTES = 512 * 1024;

const fail = (code, message, details = {}) => { throw new ActionError(code, message, details); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const isPathLike = value => typeof value === 'string' && /[\\/]/.test(value);

function stripFileUri(value) {
  if (typeof value !== 'string') return value;
  if (value.startsWith('file://')) {
    let rest = value.slice('file://'.length);
    if (/^\/[A-Za-z]:[\\/]/.test(rest)) rest = rest.slice(1);
    return decodeURIComponent(rest);
  }
  return value;
}

/** Read image dimensions from header bytes. Returns null when unrecognised. */
function imageSize(head) {
  if (!Buffer.isBuffer(head) || head.length < 10) return null;
  // PNG: 89 50 4E 47 ... IHDR width/height at 16..24, big endian.
  if (head.length >= 24 && head[0] === 0x89 && head.toString('ascii', 1, 4) === 'PNG') {
    return { width: head.readUInt32BE(16), height: head.readUInt32BE(20) };
  }
  // GIF: 'GIF8' + width/height little endian at 6..10.
  if (head.toString('ascii', 0, 4) === 'GIF8') {
    return { width: head.readUInt16LE(6), height: head.readUInt16LE(8) };
  }
  // WebP: RIFF....WEBP then VP8 / VP8L / VP8X chunk.
  if (head.length >= 30 && head.toString('ascii', 0, 4) === 'RIFF' && head.toString('ascii', 8, 12) === 'WEBP') {
    const chunk = head.toString('ascii', 12, 16);
    if (chunk === 'VP8 ') return { width: head.readUInt16LE(26) & 0x3fff, height: head.readUInt16LE(28) & 0x3fff };
    if (chunk === 'VP8L') {
      const bits = head.readUInt32LE(21);
      return { width: (bits & 0x3fff) + 1, height: ((bits >> 14) & 0x3fff) + 1 };
    }
    if (chunk === 'VP8X') {
      const width = 1 + (head[24] | (head[25] << 8) | (head[26] << 16));
      const height = 1 + (head[27] | (head[28] << 8) | (head[29] << 16));
      return { width, height };
    }
  }
  // JPEG: walk segments to a start-of-frame marker.
  if (head.length >= 4 && head[0] === 0xff && head[1] === 0xd8) {
    let offset = 2;
    while (offset + 9 < head.length) {
      if (head[offset] !== 0xff) { offset++; continue; }
      const marker = head[offset + 1];
      if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { offset += 2; continue; }
      const length = head.readUInt16BE(offset + 2);
      if (length < 2) return null;
      const isSof = marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc;
      if (isSof) return { height: head.readUInt16BE(offset + 5), width: head.readUInt16BE(offset + 7) };
      offset += 2 + length;
    }
  }
  return null;
}

function picTypeFor(file) {
  const lower = file.toLowerCase();
  if (lower.endsWith('.gif')) return PicType.GIF;
  return PicType.JPEG;
}

function createMediaPreparer({ getSession, maxBytes = DEFAULT_MAX_BYTES } = {}) {
  if (typeof getSession !== 'function') throw new TypeError('getSession is required');
  const cleanup = new Set();
  let tempRoot = null;

  function tempDir() {
    if (!tempRoot) {
      tempRoot = path.join(os.tmpdir(), 'qsick-media-' + process.pid);
      fs.mkdirSync(tempRoot, { recursive: true });
    }
    return tempRoot;
  }

  /** Resolve an element's file reference to a readable absolute local path. */
  async function resolveLocalFile(data, index) {
    if (!object(data)) fail('INVALID_PARAMS', 'media element requires a data object', { index });
    let reference = data.path ?? data.file_path ?? data.file_id ?? data.file;
    if (typeof reference !== 'string' || !reference) {
      if (typeof data.base64 === 'string' && data.base64) {
        const payload = data.base64.startsWith('base64://') ? data.base64.slice('base64://'.length) : data.base64;
        const bytes = Buffer.from(payload, 'base64');
        if (!bytes.length) fail('INVALID_PARAMS', 'base64 payload decoded to zero bytes', { index });
        const target = path.join(tempDir(), crypto.randomUUID() + (typeof data.name === 'string' && path.extname(data.name) ? path.extname(data.name) : ''));
        await fsp.writeFile(target, bytes);
        cleanup.add(target);
        return { file: target, name: typeof data.name === 'string' ? data.name : path.basename(target), size: bytes.length };
      }
      fail('INVALID_PARAMS', 'media element requires path, file_id, file or base64', { index });
    }
    // An inline `base64://` payload arrives in `file`, not in `base64` -- that is the form OneBot clients
    // send (Yunzai's image segments, for one) and NapCat accepts it. It is data, not a path, so it has to be
    // decoded before anything stats it. Previously this fell through and produced
    // "Media file is not readable: <cwd>\base64:\9j\4AAQSkZJRg..." -- the decoder was never reached because
    // the string was non-empty and looked like a relative path.
    if (reference.startsWith('base64://')) {
      const bytes = Buffer.from(reference.slice('base64://'.length), 'base64');
      if (!bytes.length) fail('INVALID_PARAMS', 'base64 payload decoded to zero bytes', { index });
      const suffix = typeof data.name === 'string' && path.extname(data.name) ? path.extname(data.name) : '';
      const target = path.join(tempDir(), crypto.randomUUID() + suffix);
      await fsp.writeFile(target, bytes);
      cleanup.add(target);
      return { file: target, name: typeof data.name === 'string' ? data.name : path.basename(target), size: bytes.length };
    }
    const absolute = path.resolve(stripFileUri(reference));
    let stat;
    try { stat = await fsp.stat(absolute); }
    catch (_) { fail('NOT_FOUND', 'Media file is not readable: ' + absolute, { index, path: absolute }); }
    if (!stat.isFile()) fail('INVALID_PARAMS', 'Media reference is not a regular file: ' + absolute, { index, path: absolute });
    if (stat.size === 0) fail('INVALID_PARAMS', 'Media file is empty: ' + absolute, { index, path: absolute });
    if (stat.size > maxBytes) fail('INVALID_PARAMS', `Media file exceeds ${maxBytes} bytes`, { index, size: stat.size });
    const name = typeof data.name === 'string' && data.name ? data.name : path.basename(absolute);
    return { file: absolute, name, size: stat.size };
  }

  function md5Of(file) {
    return new Promise((resolve, reject) => {
      const hash = crypto.createHash('md5');
      const stream = fs.createReadStream(file);
      stream.on('data', chunk => hash.update(chunk));
      stream.on('error', reject);
      stream.on('end', () => resolve(hash.digest('hex')));
    });
  }

  async function readHead(file) {
    const handle = await fsp.open(file, 'r');
    try {
      const buffer = Buffer.alloc(HEAD_BYTES);
      const { bytesRead } = await handle.read(buffer, 0, HEAD_BYTES, 0);
      return buffer.subarray(0, bytesRead);
    } finally { await handle.close(); }
  }

  function msgService() {
    const session = getSession();
    if (!session) fail('NOT_READY', 'QQ message session is not ready');
    const service = session.getMsgService && session.getMsgService();
    if (!service || typeof service.getRichMediaFilePathForGuild !== 'function') {
      fail('UNSUPPORTED', 'QQNT rich-media path API is unavailable on this build');
    }
    return service;
  }

  /** Stage a copy inside QQ's own media tree (required for group transfers). */
  async function stage(service, source, md5, name, elementType, elementSubType) {
    const mediaPath = service.getRichMediaFilePathForGuild({
      md5HexStr: md5, fileName: name, elementType, elementSubType,
      thumbSize: 0, needCreate: true, downloadType: 1, file_uuid: ''
    });
    if (typeof mediaPath !== 'string' || !mediaPath) fail('INVALID_RESULT', 'QQNT did not return a rich-media path');
    await fsp.mkdir(path.dirname(mediaPath), { recursive: true });
    await fsp.copyFile(source, mediaPath);
    cleanup.add(mediaPath);
    return mediaPath;
  }

  async function prepareImage(data, context = {}, index = 0) {
    const resolved = await resolveLocalFile(data, index);
    const service = msgService();
    const md5 = await md5Of(resolved.file);
    const head = await readHead(resolved.file);
    const size = imageSize(head);
    // picSubType 决定接收端把它当"图片"还是"表情"：
    //   0 = 普通图片，1 = 表情（收藏表情/表情包），2 = 热图（QQ 侧另有含义）
    // 以前这里写死 NORMAL，所以收藏表情发出去是一张图片，动图也不再是表情——
    // 接收端没有任何依据把它渲染成表情。调用方明确要发表情时才允许改。
    //
    // 两个字段名都认：OneBot 标准段里叫 `sub_type`，WebUI 上传表单历史上用
    // `pic_sub_type`。只认其中一个，就等于其中一条链路悄悄退回普通图片。
    const requested = Number(data.sub_type !== undefined ? data.sub_type : data.pic_sub_type);
    const subType = PIC_SUB_TYPES.includes(requested) ? requested : PicSubType.NORMAL;
    const staged = await stage(service, resolved.file, md5, resolved.name, ElementType.PIC, subType);
    const picElement = {
      md5HexStr: md5,
      fileSize: String(resolved.size),
      picWidth: size ? size.width : 0,
      picHeight: size ? size.height : 0,
      fileName: resolved.name,
      sourcePath: staged,
      original: true,
      picType: picTypeFor(resolved.file),
      picSubType: subType,
      fileUuid: '',
      fileSubId: '',
      thumbFileSize: 0,
      summary: typeof data.summary === 'string' ? data.summary : ''
    };
    return { elementType: ElementType.PIC, elementId: '', picElement };
  }

  async function prepareFile(data, context = {}, index = 0) {
    const resolved = await resolveLocalFile(data, index);
    const service = msgService();
    const group = !context.peer || Number(context.peer.chatType) === 2;
    // Group files live in QQ's rich-media tree; C2C files are sent from the
    // original path (mirrors uploadFile(..., uploadGroupFile=false)).
    const sourcePath = group
      ? await stage(service, resolved.file, await md5Of(resolved.file), resolved.name, ElementType.FILE, 0)
      : resolved.file;
    return {
      elementType: ElementType.FILE,
      elementId: '',
      fileElement: {
        fileName: typeof data.name === 'string' && data.name ? data.name : resolved.name,
        folderId: typeof data.folder_id === 'string' ? data.folder_id : '',
        filePath: sourcePath,
        fileSize: String(resolved.size)
      }
    };
  }

  /**
   * Download a received picture/file into QQ's own media tree so the WebUI can
   * serve it from disk. QQ does not cache received media in headless mode, so
   * without this every image element is only a file id and a dead URL.
   * Contract: napcat-core/apis/file.ts downloadMedia -> downloadRichMedia +
   * onRichMediaDownloadComplete correlated on msgId + elementId.
   */
  async function downloadReceived(native, element) {
    const session = getSession();
    if (!session) return null;
    const msg = session.getMsgService && session.getMsgService();
    if (!msg || typeof msg.downloadRichMedia !== 'function' || typeof msg.addKernelMsgListener !== 'function') return null;
    const peer = native && native.peer;
    if (!peer) return null;
    const msgId = String(native.msgId || '');
    const elementId = String(element.id || '');
    if (!msgId || !elementId) return null;
    // A copy may already exist on disk (QQ downloads lazily); reuse it instead of
    // triggering another transfer.
    if (typeof element.sourcePath === 'string' && element.sourcePath) {
      try { if (fs.existsSync(element.sourcePath) && fs.statSync(element.sourcePath).size > 0) return element.sourcePath; }
      catch (_) {}
    }
    if (typeof element.thumbPath === 'string' && element.thumbPath) {
      try { if (fs.existsSync(element.thumbPath) && fs.statSync(element.thumbPath).size > 0) return element.thumbPath; }
      catch (_) {}
    }
    let handle;
    let settle;
    const done = new Promise(resolve => {
      const timer = setTimeout(() => { cleanup(); resolve(null); }, 30000);
      const cleanup = () => {
        clearTimeout(timer);
        try { if (handle !== undefined) msg.removeKernelMsgListener(handle); } catch (_) {}
      };
      settle = value => { cleanup(); resolve(value); };
      handle = msg.addKernelMsgListener(new Proxy({
        onRichMediaDownloadComplete(info) {
          if (!object(info)) return;
          if (String(info.msgId) !== msgId || String(info.msgElementId) !== elementId) return;
          if (info.fileErrCode !== undefined && Number(info.fileErrCode) !== 0) { settle(null); return; }
          settle(typeof info.filePath === 'string' && info.filePath ? info.filePath : null);
        }
      }, { get: (target, prop) => target[prop] || function () {} }));
    });
    try {
      msg.downloadRichMedia({
        fileModelId: '0', downSourceType: 0, downloadSourceType: 0, triggerType: 1,
        msgId, chatType: Number(peer.chatType), peerUid: String(peer.peerUid),
        elementId, thumbSize: 0, downloadType: 1, filePath: ''
      });
    } catch (_) { return null; }
    return done;
  }

  /** Remove staged copies and decoded base64 temporaries. Best effort. */
  async function release() {
    const paths = [...cleanup];
    cleanup.clear();
    await Promise.all(paths.map(target => fsp.rm(target, { force: true }).catch(() => {})));
    if (tempRoot) await fsp.rm(tempRoot, { recursive: true, force: true }).catch(() => {});
    tempRoot = null;
  }

  return { prepareImage, prepareFile, downloadReceived, release, pending: () => cleanup.size };
}

module.exports = { createMediaPreparer, imageSize, stripFileUri, ElementType, PicType, PicSubType, DEFAULT_MAX_BYTES };
