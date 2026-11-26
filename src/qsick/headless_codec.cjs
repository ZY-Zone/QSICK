'use strict';

/**
 * Pure QQNT <-> QSICK boundary. No native imports, I/O, downloads or file probing.
 * Contracts: protocol/{message,elements}.py; QQNT types/{msg,element}.ts and
 * NodeIKernelMsgService.sendMsg/recallMsg. This is an independent implementation.
 *
 * normalizeMessage(raw, account, context?) is synchronous and returns a payload
 * or null. context.onSkip({code, reason, chatType}) explains rejected messages.
 * context.resolveUin(uid) may synchronously return a decimal UIN (never Promise).
 * A function supplied as context is shorthand for resolveUin. Missing sender
 * UIN is represented by '0'; an unresolved conversation peer is never accepted.
 *
 * buildSendElements(elements, context?) is async and all-or-nothing. Its only
 * optional external operation is context.resolveUid(uin, context), returning
 * a UID string or Promise<string>. context.peer (if supplied) is a native Peer.
 * Reply data must include message_id, seq, sender_uin, or context.messageRefs
 * must be an account-scoped Map<string, payload.native> for the exact message ID.
 * Cached replies require context.peer matching native.peer chatType + peerUid;
 * guildId is ignored for C2C/group because hosts may use it for correlation.
 * Outgoing media/XML/forwards require native preparation and
 * deliberately fail. Only legacy faces 0..221 are supported without a catalog.
 *
 * All decimal IDs are strings. Unsafe numeric IDs are rejected, not repaired.
 * Native references are in payload.native; payload.raw is a JSON-safe snapshot.
 * Map snapshots use {$map:[[key,value],...]} to preserve key types/collisions;
 * cycles use {$circular:true}. No Map is silently serialized to {}.
 */

class CodecError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'CodecError';
    this.code = code;
    Object.assign(this, details);
  }
}

// 'xml' and 'multi_forward' are distinct native element types, not two spellings of one thing:
//
//   xml             -> elementType 10, arkElement.bytesData         (Ark / structured card)
//   multi_forward   -> elementType 16, multiForwardMsgElement.xmlContent
//
// The second is what an incoming merged forward looks like when the client sends the XML form.
// Both are carried verbatim: the codec does not parse, validate or synthesise their contents,
// because constructing a forward card is a decision for the caller, not for a transport.
const SUPPORTED_SEND_TYPES = Object.freeze(['text', 'at', 'face', 'json', 'xml', 'multi_forward', 'reply', 'image', 'file']);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string' ? value : '';

//: How deep and how wide the raw snapshot goes. These objects reach into kernel state; the limits exist so a
//: pathological element cannot turn into an unbounded walk on every received message.
const RAW_MAX_DEPTH = 4;
const RAW_MAX_KEYS = 64;

/**
 * Field names of `value`, including the ones a NAPI wrapper keeps on its prototype.
 *
 * QQ hands these decoders native objects whose values are getters on the prototype, so `Object.keys` reports
 * nothing. Reading `id` off a picture element worked only because the decoder asked for it by name -- everything
 * else was invisible, which is why an element's `raw` arrived empty and could not be inspected later.
 */
function rawKeys(value) {
  const names = [];
  let target = value;
  for (let level = 0; level < RAW_MAX_DEPTH && target && target !== Object.prototype; level += 1) {
    for (const name of Object.getOwnPropertyNames(target)) {
      if (name === 'constructor' || names.includes(name)) continue;
      names.push(name);
      if (names.length >= RAW_MAX_KEYS) return names;
    }
    target = Object.getPrototypeOf(target);
  }
  return names;
}

function toJsonSafe(value, seen = new WeakSet(), depth = 0) {
  if (typeof value === 'bigint') return value.toString();
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'function') return undefined;
  if (typeof value !== 'object') return null;
  if (seen.has(value)) return { $circular: true };
  if (depth >= RAW_MAX_DEPTH) return { $depth: true };
  seen.add(value);
  let result;
  if (value instanceof Map) {
    result = { $map: Array.from(value, ([key, item]) => [toJsonSafe(key, seen, depth + 1), toJsonSafe(item, seen, depth + 1)]) };
  } else if (value instanceof Set) {
    result = { $set: Array.from(value, item => toJsonSafe(item, seen, depth + 1)) };
  } else if (Array.isArray(value)) {
    result = Array.from(value, item => toJsonSafe(item, seen, depth + 1));
  } else {
    result = {};
    for (const key of rawKeys(value)) {
      let item;
      try {
        item = value[key];
      } catch (error) {
        // A getter that throws is worth one marker, not the whole element.
        result[key] = { $error: String((error && error.message) || error) };
        continue;
      }
      if (typeof item === 'function') continue;
      const safe = toJsonSafe(item, seen, depth + 1);
      if (safe !== undefined) result[key] = safe;
    }
  }
  seen.delete(value);
  return result;
}

function decimal(value, positive = false) {
  let result;
  if (typeof value === 'bigint') result = value.toString();
  else if (typeof value === 'number' && Number.isSafeInteger(value)) result = String(value);
  else if (typeof value === 'string') result = value;
  else return null;
  if (!/^\d+$/.test(result) || (positive && !/[1-9]/.test(result))) return null;
  return result;
}

function requireDecimal(value, name, positive = false) {
  const result = decimal(value, positive);
  if (result === null) throw new CodecError('INVALID_ID', `${name} must be an exact ${positive ? 'positive ' : ''}decimal string, bigint, or safe integer`);
  return result;
}

function contextOf(context) {
  return typeof context === 'function' ? { resolveUin: context } : (object(context) ? context : {});
}

function resolvedUin(uid, context) {
  if (!uid || typeof context.resolveUin !== 'function') return null;
  let value;
  try { value = context.resolveUin(uid); } catch { return null; }
  if (value && typeof value.then === 'function') {
    // Avoid an unhandled rejection from a mistakenly async callback.
    if (typeof value.catch === 'function') value.catch(() => {});
    throw new CodecError('ASYNC_RESOLVER', 'normalizeMessage/normalizeElement resolveUin must be synchronous');
  }
  return decimal(value, true);
}

// QQ sometimes delivers a system face as markup inside a text element, e.g.
// <faceType=1,faceId="102",ext="eyJ0ZXh0Ijoi5bem5ZO85ZO8In0=">. The ext field is
// base64 JSON carrying the face name ("左哼哼"). Returning null means the text
// holds no such markup and should stay a single text segment.
// --- protobuf writers for the two face common-elements ---------------------
// Field numbers come from NapCat's QBigFaceExtra / QSmallFaceExtra and
// SnowLuma's QFaceExtra. Written by hand so the send path needs no proto
// runtime; both messages are tiny and flat.

function pushVarint(out, value) {
  let n = BigInt(value);
  while (n > 0x7fn) {
    out.push(Number((n & 0x7fn) | 0x80n));
    n >>= 7n;
  }
  out.push(Number(n));
}

function pushTag(out, field, wire) { pushVarint(out, (field << 3) | wire); }

function pushInt(out, field, value) {
  if (value === undefined || value === null || value === '') return;
  const n = Number(value);
  if (!Number.isFinite(n)) return;
  pushTag(out, field, 0);
  pushVarint(out, Math.trunc(n));
}

function pushBytes(out, field, value) {
  const str = typeof value === 'string' ? value : '';
  if (!str) return;
  const bytes = Buffer.from(str, 'utf8');
  pushTag(out, field, 2);
  pushVarint(out, bytes.length);
  out.push(...bytes);
}

/** QFaceExtra / QBigFaceExtra: the animated ("超级") face payload. */
function encodeQFaceExtra({ packId, stickerId, qsid, stickerType, preview }) {
  const out = [];
  pushBytes(out, 1, packId);       // AniStickerPackId
  pushBytes(out, 2, stickerId);    // AniStickerId
  pushInt(out, 3, qsid);           // faceId
  pushInt(out, 4, 1);              // sourceType
  pushInt(out, 5, stickerType);    // AniStickerType
  pushBytes(out, 7, preview);      // preview
  pushInt(out, 9, 1);              // randomType
  return Buffer.from(out).toString('base64');
}

/** QSmallFaceExtra: ids >= 260 that are not animated stickers. */
function encodeQSmallFaceExtra({ faceId }) {
  const out = [];
  pushInt(out, 1, faceId);
  pushBytes(out, 2, '');
  pushBytes(out, 3, '');
  return Buffer.from(out).toString('base64');
}

// QQ sometimes delivers a system face as markup inside a text element, e.g.
// <faceType=1,faceId="102",ext="eyJ0ZXh0Ijoi5bem5ZO85ZO8In0=">. The ext field is
// base64 JSON carrying the face name ("左哼哼"). Returning null means the text
// holds no such markup and should stay a single text segment.
const TEXT_FACE = /<faceType=(\d+),faceId="(\d+)"(?:,ext="([^"]*)")?>/g;

function splitTextFaces(content) {
  if (typeof content !== 'string' || content.indexOf('<faceType=') < 0) return null;
  const pieces = [];
  let cursor = 0;
  TEXT_FACE.lastIndex = 0;
  for (let match = TEXT_FACE.exec(content); match; match = TEXT_FACE.exec(content)) {
    if (match.index > cursor) pieces.push({ type: 'text', data: { text: content.slice(cursor, match.index) } });
    const faceType = decimal(match[1]);
    const faceId = decimal(match[2]);
    // Unknown ids are left as literal text rather than dropped silently.
    if (faceId === null) { pieces.push({ type: 'text', data: { text: match[0] } }); cursor = TEXT_FACE.lastIndex; continue; }
    const data = { id: faceId };
    // <faceType=5> is a poke; <faceType=1> is an ordinary system face.
    if (faceType === '5') pieces.push({ type: 'poke', data: { id: faceId } });
    else {
      const label = decodeFaceExt(match[3]);
      if (label) data.text = label;
      pieces.push({ type: 'face', data });
    }
    cursor = TEXT_FACE.lastIndex;
  }
  if (!pieces.length) return null;
  if (cursor < content.length) pieces.push({ type: 'text', data: { text: content.slice(cursor) } });
  return pieces;
}

function decodeFaceExt(value) {
  if (typeof value !== 'string' || !value) return '';
  try {
    const raw = Buffer.from(value, 'base64').toString('utf8');
    const parsed = JSON.parse(raw);
    return object(parsed) && typeof parsed.text === 'string' ? parsed.text : '';
  } catch {
    return '';
  }
}

function unknown(raw, reason) {
  const original = object(raw) ? raw.elementType : undefined;
  const data = { original_type: original === undefined ? 'unknown' : String(original), raw: toJsonSafe(raw) };
  if (reason) data.reason = reason;
  return { type: 'unknown', data };
}

function normalized(type, data, raw) {
  // Always present: an element with `raw: {}` says "QQ gave nothing", which is different from
  // "nobody looked".
  const snapshot = toJsonSafe(raw);
  return { type, data: toJsonSafe(data), raw: snapshot && typeof snapshot === 'object' ? snapshot : {} };
}

function mediaData(body, raw, kind) {
  const hash = body.md5HexStr || body.videoMd5 || body.fileMd5;
  const data = {
    file_id: text(body.fileUuid) || text(hash) || decimal(body.fileId) || decimal(raw.elementId) || '',
    file_name: text(body.fileName),
    file_size: decimal(body.fileSize) || '',
  };
  if (body.filePath) data.path = text(body.filePath);
  if (hash) data.md5 = text(hash);
  if (body.fileSubId !== undefined) data.file_sub_id = text(body.fileSubId);
  // The URL a received file already carries.
  //
  // `downloadUrl` is a confirmed field of the file element (reports 13/15: `kElemTypeFile` /
  // `kElemTypeOnlineFile`), but it has never been observed populated on this build -- the napi
  // hooks that would capture it returned nothing. So it is copied when present and simply absent
  // otherwise: a caller may use it, but nothing here pretends a private file always arrives with
  // one. That is why `private.file.download_url` still has to ask the server.
  const downloadUrl = text(body.downloadUrl) || text(body.download_url);
  if (downloadUrl) data.download_url = downloadUrl;
  // Element IDs are required to correlate late media downloads back to this element.
  if (raw.elementId !== undefined && raw.elementId !== null && raw.elementId !== '') data.element_id = text(raw.elementId);
  if (kind === 'image') {
    data.width = body.picWidth;
    data.height = body.picHeight;
    data.summary = text(body.summary);
    data.sub_type = body.picSubType;
    if (body.originImageUrl) data.url = text(body.originImageUrl);
    if (body.sourcePath) data.source_path = text(body.sourcePath);
  }
  if (kind === 'record') data.duration = body.duration;
  if (kind === 'video') {
    data.duration = body.fileTime;
    data.width = body.fileWidth;
    data.height = body.fileHeight;
  }
  if (body.thumbPath !== undefined) data.thumbnails = body.thumbPath;
  if (body.picThumbPath !== undefined) data.thumbnails = body.picThumbPath;
  return Object.fromEntries(Object.entries(data).filter(([, value]) => value !== undefined));
}

function normalizeElement(raw, suppliedContext) {
  const context = contextOf(suppliedContext);
  if (!object(raw)) return unknown(raw, 'Element is not an object');
  const type = decimal(raw.elementType);
  const field = { '1': 'textElement', '2': 'picElement', '3': 'fileElement', '4': 'pttElement', '5': 'videoElement', '6': 'faceElement', '7': 'replyElement', '10': 'arkElement', '11': 'marketFaceElement', '13': 'structLongMsgElement', '14': 'markdownElement', '16': 'multiForwardMsgElement', '17': 'inlineKeyboardElement' }[type];
  if (!field || !object(raw[field])) return unknown(raw, field ? `Missing ${field}` : 'Unsupported native element type');
  const body = raw[field];
  if (type === '1') {
    if (typeof body.content !== 'string') return unknown(raw, 'Text content is not a string');
    const atType = decimal(body.atType ?? 0);
    if (atType === '0') {
      // A QQ system face sometimes arrives inside a plain text element as
      // <faceType=1,faceId="102",ext="...">, measured live. Rendering that as
      // literal text is what made faces look wrong, so split it out first.
      const parsed = splitTextFaces(body.content);
      if (parsed) return parsed.map(item => normalized(item.type, item.data, raw));
      return normalized('text', { text: body.content }, raw);
    }
    if (atType === '1') return normalized('at', { user_id: 'all', name: body.content }, raw);
    if (atType !== '2' && atType !== '4') return unknown(raw, 'Unsupported mention subtype');
    const uid = text(body.atNtUid) || (text(body.atUid).startsWith('u_') ? body.atUid : '');
    const uin = decimal(body.atUid, true) || resolvedUin(uid, context);
    // Do not turn an unresolved user mention into an accidental @all.
    if (!uin) return unknown(raw, 'User mention has no exact UIN; supply synchronous resolveUin');
    return normalized('at', { user_id: uin, uid, name: body.content }, raw);
  }
  const media = { '2': 'image', '3': 'file', '4': 'record', '5': 'video' }[type];
  if (media) return normalized(media, mediaData(body, raw, media), raw);
  if (type === '6') {
    const id = decimal(body.faceIndex);
    if (id === null) return unknown(raw, 'Face index is invalid');
    if (decimal(body.faceType) === '5') {
      const pokeType = decimal(body.pokeType);
      if (pokeType === null) return unknown(raw, 'Poke type is missing or invalid');
      return normalized('poke', { type: pokeType, id }, raw);
    }
    if (id === '358' || id === '359') {
      const result = decimal(body.resultId);
      // Measured on a live account: QQ frequently sends dice/RPS with no
      // resultId at all. Dropping those turned a real message into UNKNOWN, so
      // show the face itself and only report a result when QQ actually gave one.
      if (result === null) return normalized('face', { id }, raw);
      return normalized(id === '358' ? 'dice' : 'rps', { result }, raw);
    }
    const data = { id };
    if (body.resultId !== undefined) data.result_id = text(body.resultId) || decimal(body.resultId) || '';
    // Preserve what makes a face an EXTemotion.
    //
    // faceType distinguishes a still face (1) from an animated sticker (3). Dropping it made every
    // incoming sticker look like a plain face, so a consumer had no way to tell whether the picture
    // moves -- which is exactly the distinction the EXTemotions protocol exists to carry, and the
    // one plain v11 cannot express with `[CQ:face,id=N]`.
    //
    // The sticker triple is kept alongside it: pack id, sticker id and sticker type identify which
    // image this actually is, and none of them is recoverable from the face index alone.
    const faceType = decimal(body.faceType);
    if (faceType !== null) {
      data.face_type = Number(faceType);
      // 3 is AniSticke (animated), 4 is Lottie. Anything else is a still face.
      if (faceType === '3' || faceType === '4') data.format = 'animated';
      else if (faceType === '1' || faceType === '2') data.format = 'static';
    }
    const stickerId = text(body.stickerId);
    const packId = text(body.packId);
    const stickerType = decimal(body.stickerType);
    if (stickerId) data.sticker_id = stickerId;
    if (packId) data.pack_id = packId;
    if (stickerType !== null) data.sticker_type = Number(stickerType);
    // An animated element without an explicit format still moves; say so rather than leaving the
    // consumer to infer it from face_type.
    if (data.format === undefined && (stickerId || packId)) data.format = 'animated';
    return normalized('face', data, raw);
  }
  if (type === '7') {
    const id = decimal(body.replayMsgId, true) || decimal(body.sourceMsgIdInRecords, true);
    const seq = decimal(body.replayMsgSeq);
    if (!id && !seq) return unknown(raw, 'Reply has neither exact message ID nor sequence');
    const data = { seq: seq || '', sender_uin: decimal(body.senderUin) || '', sender_uid: text(body.senderUidStr) };
    if (id) data.message_id = id;
    if (body.replyMsgTime !== undefined) data.time = decimal(body.replyMsgTime) || '';
    if (body.replyMsgClientSeq !== undefined) data.client_seq = decimal(body.replyMsgClientSeq) || '';
    return normalized('reply', data, raw);
  }
  if (type === '10') {
    if (typeof body.bytesData !== 'string') return unknown(raw, 'Ark bytesData is not a string');
    const value = body.bytesData;
    if (value.trimStart().startsWith('<')) return normalized('xml', { data: value }, raw);
    let parsed;
    try { parsed = JSON.parse(value); } catch { return unknown(raw, 'Ark bytesData is neither JSON nor XML'); }
    if (object(parsed) && parsed.app === 'com.tencent.multimsg' && object(parsed.meta?.detail) && typeof parsed.meta.detail.resid === 'string') {
      return normalized('forward', { res_id: parsed.meta.detail.resid, summary: text(parsed.meta.detail.summary), content: [], data: value }, raw);
    }
    return normalized('json', { data: value }, raw);
  }
  if (type === '11') {
    const packageId = decimal(body.emojiPackageId);
    if (packageId === null || typeof body.emojiId !== 'string' || !body.emojiId || typeof body.key !== 'string' || typeof body.faceName !== 'string') return unknown(raw, 'Market face requires exact package ID, emojiId, key and faceName');
    return normalized('mface', { emoji_id: body.emojiId, emoji_package_id: packageId, key: body.key, summary: body.faceName }, raw);
  }
  if (type === '14') {
    if (typeof body.content !== 'string') return unknown(raw, 'Markdown content is not a string');
    // Extension metadata stays in raw; rendering markdown must never execute it.
    return normalized('markdown', { content: body.content }, raw);
  }
  if (type === '17') {
    const appId = decimal(body.botAppid);
    if (appId === null || !Array.isArray(body.rows) || !body.rows.every(row => object(row) && Array.isArray(row.buttons) && row.buttons.every(object))) return unknown(raw, 'Inline keyboard requires exact botAppid and rows of button objects');
    return normalized('inline_keyboard', { bot_appid: appId, rows: body.rows }, raw);
  }
  if (type === '13' || type === '16') {
    if (typeof body.xmlContent !== 'string') return unknown(raw, 'Forward/long-message XML content is missing');
    if (typeof body.resId === 'string' && body.resId) {
      return normalized('forward', { res_id: body.resId, summary: '', content: [], xml: body.xmlContent, file_name: text(body.fileName) }, raw);
    }
    return normalized('xml', { data: body.xmlContent }, raw);
  }
  return unknown(raw);
}

function normalizeMessage(raw, account, suppliedContext) {
  const context = contextOf(suppliedContext);
  const skip = (code, reason) => {
    if (typeof context.onSkip === 'function') context.onSkip({ code, reason, chatType: toJsonSafe(raw?.chatType) });
    return null;
  };
  if (!object(raw)) return skip('INVALID_MESSAGE', 'Native message must be an object');
  const chatType = decimal(raw.chatType);
  if (chatType !== '1' && chatType !== '2') return skip('UNSUPPORTED_CHAT_TYPE', 'Only QQNT C2C (1) and group (2) have supported peer semantics; temporary, guild, notification and other chats are not relabeled');
  const messageId = decimal(raw.msgId, true);
  if (!messageId) return skip('INVALID_MESSAGE_ID', 'msgId must be an exact positive decimal ID; unsafe Number snowflakes cannot be recovered');
  const accountId = object(account) ? (account.uin ?? account.account) : account;
  const accountString = typeof accountId === 'string' ? accountId : decimal(accountId, true);
  if (!accountString) return skip('INVALID_ACCOUNT', 'An explicit nonempty account identity is required');
  for (const key of ['msgSeq', 'msgTime', 'msgRandom', 'clientSeq', 'senderUin', 'peerUin']) {
    if (raw[key] !== undefined && raw[key] !== null && raw[key] !== '' && decimal(raw[key]) === null) {
      return skip('INVALID_NATIVE_ID', `${key} must be an exact decimal value; unsafe or malformed IDs cannot be recovered`);
    }
  }
  const uid = text(raw.peerUid);
  if (chatType === '1' && !/^u_\S+$/.test(uid)) return skip('INVALID_PEER', 'Private native peerUid must be a QQNT u_ UID, not a fabricated UIN peer');
  if (chatType === '2' && raw.peerUid !== undefined && raw.peerUid !== '' && decimal(raw.peerUid, true) === null) return skip('INVALID_PEER', 'Group native peerUid must be an exact positive decimal group ID');
  let peerId = decimal(raw.peerUin, true);
  if (chatType === '2') peerId ||= decimal(raw.peerUid, true);
  else peerId ||= resolvedUin(uid, context);
  if (!peerId) return skip('INVALID_PEER', 'Conversation has no exact positive UIN/group ID; UID-only private peers require synchronous resolveUin');
  if (chatType === '2' && decimal(raw.peerUid, true) && decimal(raw.peerUin, true) && raw.peerUid.toString() !== raw.peerUin.toString()) {
    return skip('INVALID_PEER', 'Group peerUid and peerUin disagree');
  }
  const senderUid = text(raw.senderUid);
  const senderUin = decimal(raw.senderUin, true) || resolvedUin(senderUid, context) || '0';
  const peer = { chatType: chatType === '1' ? 1 : 2, peerUid: uid || peerId };
  if (raw.guildId) peer.guildId = text(raw.guildId);
  const native = { peer, msgId: messageId, msgSeq: decimal(raw.msgSeq) || '0', msgTime: decimal(raw.msgTime) || '0', msgRandom: decimal(raw.msgRandom) || '0', clientSeq: decimal(raw.clientSeq) || '0', senderUid, senderUin, peerUin: peerId };
  let elements;
  if (Array.isArray(raw.elements)) {
    // One native element may expand into several segments (a text element that
    // embeds <faceType=..> markup), so flatten without dropping order.
    elements = [];
    for (const item of raw.elements) {
      const normalizedItem = normalizeElement(item, context);
      if (Array.isArray(normalizedItem)) elements.push(...normalizedItem);
      else elements.push(normalizedItem);
    }
  }
  else elements = [unknown(raw.elements, 'Native elements must be an array')];
  return {
    account: accountString,
    message_id: messageId,
    seq: native.msgSeq,
    time: native.msgTime,
    chat: { type: chatType === '2' ? 'group' : 'private', id: peerId },
    sender: { uin: senderUin, uid: senderUid, nickname: text(raw.sendNickName) || text(raw.sendRemarkName), card: text(raw.sendMemberName), role: 'member' },
    elements,
    native,
    raw: toJsonSafe(raw),
  };
}

function requireString(value, name, nonempty = false) {
  if (typeof value !== 'string' || (nonempty && !value)) throw new CodecError('INVALID_ELEMENT', `${name} must be ${nonempty ? 'a nonempty' : 'a'} string`);
  return value;
}

function prepareSend(element, index, context) {
  if (!object(element) || !object(element.data)) throw new CodecError('INVALID_ELEMENT', 'Each outgoing element requires an object data payload', { index });
  const { type, data } = element;
  // The receive side yields a 'forward' segment carrying `xml` when the incoming element was
  // elementType 16. Sending that segment back must reproduce the same element, so 'forward' with an
  // xml field is treated as 'multi_forward' rather than being rejected as an unknown type -- which
  // would make a round trip of read-then-forward impossible.
  const effective = (type === 'forward' && typeof (data.xml ?? data.xmlContent) === 'string')
    ? 'multi_forward'
    : type;
  if (!SUPPORTED_SEND_TYPES.includes(effective)) throw new CodecError('UNSUPPORTED_ELEMENT', `Outgoing ${String(type)} is unsupported: native media/upload preparation is outside this pure codec`, { index, elementType: type });
  const kind = effective;
  const result = { elementId: '' };
  if (kind === 'text' || kind === 'at') {
    result.elementType = 1;
    result.textElement = { content: '', atType: 0, atUid: '', atTinyId: '', atNtUid: '' };
    if (kind === 'text') result.textElement.content = requireString(data.text, 'text');
    else {
      if (context.peer && decimal(context.peer.chatType) !== '2') throw new CodecError('INVALID_PEER', 'Outgoing mentions are supported only in group chats', { index });
      const target = data.user_id ?? data.uin ?? data.qq;
      if (target === 'all') Object.assign(result.textElement, { content: '@全体成员', atType: 1, atUid: 'all', atNtUid: 'all' });
      else {
        const uin = requireDecimal(target, 'mention user_id', true);
        const uid = data.uid ?? data.at_nt_uid;
        if (uid !== undefined && (typeof uid !== 'string' || !/^u_\S+$/.test(uid))) throw new CodecError('INVALID_UID', 'Mention uid must be a QQNT u_ UID', { index });
        Object.assign(result.textElement, { content: `@${typeof data.name === 'string' ? data.name.replace(/^@/, '') : uin}`, atType: 2, atUid: uin, atNtUid: uid || '' });
      }
    }
  } else if (kind === 'face') {
    const id = requireDecimal(data.id ?? data.face_id, 'face id', true);
    // A face index is a small integer; anything near 2^53 is a message ID that
    // leaked into the wrong field.
    if (BigInt(id) > 100000n) throw new CodecError('INVALID_ID', 'face id is out of the supported range', { index });
    if (data.result_id !== undefined || data.resultId !== undefined) {
      throw new CodecError('UNSUPPORTED_FACE', 'Dice/rps results cannot be sent as ordinary faces', { index });
    }
    result.elementType = 6;
    // This goes through QQNT's native sendMsg, whose element table has no
    // commonElem entry (NapCat's ElementType jumps 6=FACE then 7=REPLY; the
    // commonElem form belongs to its proto transport). So the face must stay a
    // faceElement, and the animated case is carried by faceType.
    //
    // NapCat's FaceType enum: OldFace=1, Normal=2, AniSticke=3, Lottie=4, Poke=5.
    // Animated ("超级") faces need AniSticke, otherwise QQ treats them as an
    // ordinary face and silently remaps the id (SnowLuma documents 424 -> 168).
    //
    // The EXTemotion protocol states this explicitly in `format`, so the encoding
    // follows the declaration rather than guessing from the sticker triple.
    const format = text(data.format);
    // The sticker Type is what the native element carries; `sticker_kind` was an extra gate that
    // nobody sets, so any caller supplying the documented triple (sticker_id, sticker_type,
    // pack_id) was rejected as incomplete. Presence of the three fields is the real condition.
    const stickerType = data.sticker_type === undefined ? undefined : Number(data.sticker_type);
    const hasTriple = stickerType !== undefined && Number.isFinite(stickerType)
      && data.sticker_id !== undefined && data.pack_id !== undefined;
    if (format === 'animated' && !hasTriple) {
      // An animated declaration must actually be sendable as a sticker; silently
      // downgrading it to a static face is how the wrong picture gets drawn.
      throw new CodecError('INVALID_FACE', 'animated face requires sticker_id, sticker_type and pack_id', { index });
    }
    if (format && format !== 'animated' && format !== 'static') {
      throw new CodecError('INVALID_FACE', `unsupported face format ${JSON.stringify(format)}`, { index });
    }
    const animated = format === 'animated' || (format === '' && hasTriple);
    result.faceElement = animated
      ? {
          faceIndex: Number(id),
          faceType: 3,
          faceText: typeof data.text === 'string' ? data.text : '',
          stickerId: data.sticker_id === undefined ? '' : String(data.sticker_id),
          stickerType,
          packId: data.pack_id === undefined ? '' : String(data.pack_id),
          sourceType: 1,
        }
      : { faceIndex: Number(id), faceType: 1, faceText: typeof data.text === 'string' ? data.text : '' };
  } else if (kind === 'json') {
    let bytesData;
    if (typeof data.data === 'string') {
      try { JSON.parse(data.data); } catch { throw new CodecError('INVALID_JSON', 'Ark data must contain valid JSON', { index }); }
      bytesData = data.data;
    } else if (object(data.data) || Array.isArray(data.data)) bytesData = JSON.stringify(toJsonSafe(data.data));
    else throw new CodecError('INVALID_JSON', 'Ark data must be a JSON string or object', { index });
    result.elementType = 10;
    result.arkElement = { bytesData, linkInfo: null, subElementType: null };
  } else if (kind === 'xml') {
    // Plain XML card: elementType 10 with an XML payload rather than JSON.
    //
    // Which of the two the native layer expects is decided by the payload, not the element type --
    // receiving code already treats a bytesData starting with '<' as XML. So the same shape is used
    // here and the content is passed through untouched.
    const raw = data.data ?? data.xml ?? data.content;
    if (typeof raw !== 'string' || !raw.trim()) {
      throw new CodecError('INVALID_XML', 'xml data must be a non-empty string', { index });
    }
    if (!raw.trimStart().startsWith('<')) {
      throw new CodecError('INVALID_XML', 'xml data must start with "<"', { index });
    }
    result.elementType = 10;
    result.arkElement = { bytesData: raw, linkInfo: null, subElementType: null };
  } else if (kind === 'multi_forward') {
    // Merged forward in the XML form: elementType 16, multiForwardMsgElement.xmlContent.
    //
    // This is transport only. The XML is supplied by the caller and forwarded byte for byte: the
    // codec does not compose a card, does not invent senders, and does not fill in a resid. Building
    // one is the caller's business -- and where that content comes from is theirs to answer for.
    const raw = data.xml ?? data.xmlContent ?? data.data ?? data.content;
    if (typeof raw !== 'string' || !raw.trim()) {
      throw new CodecError('INVALID_FORWARD_XML', 'multi_forward requires XML content', { index });
    }
    if (!raw.trimStart().startsWith('<')) {
      throw new CodecError('INVALID_FORWARD_XML', 'multi_forward content must start with "<"', { index });
    }
    result.elementType = 16;
    result.multiForwardMsgElement = { xmlContent: raw };
  } else if (kind === 'image' || kind === 'file') {
    // Media needs real file access (size, md5, image dimensions) and native
    // rich-media staging, so the pure codec cannot build it alone. The host
    // injects an async preparer through context; without one this is unsupported
    // rather than silently dropping the segment.
    result.__media = type;
    result.__mediaData = data;
  } else if (kind === 'reply') {
    const id = requireDecimal(data.message_id ?? data.id, 'reply message_id', true);
    let reference = data;
    if (context.messageRefs instanceof Map && context.messageRefs.has(id)) {
      const native = context.messageRefs.get(id);
      const peer = context.peer;
      const nativeType = decimal(native?.peer?.chatType);
      if (!object(native) || !object(peer) || !['1', '2'].includes(nativeType) ||
          nativeType !== decimal(peer.chatType) || native.peer.peerUid !== peer.peerUid ||
          decimal(native.msgId, true) !== id) {
        throw new CodecError('REPLY_PEER_MISMATCH', 'Cached reply must have the exact message ID and belong to the current native peer', { index });
      }
      reference = { seq: native.msgSeq, sender_uin: native.senderUin, sender_uid: native.senderUid || undefined, time: native.msgTime, client_seq: native.clientSeq };
    }
    result.elementType = 7;
    // A reply carrying only `id` is the ordinary OneBot shape, and it must not fail the whole send.
    //
    // The client's own element takes `+(... ?? 0)` for both the sequence and the sender and never rejects a bare
    // id (NapCat `PacketMsgReplyElement`); requiring them here turned "quote an old message" into a hard error
    // once the reference was not in the local cache -- which is exactly what happens for a message received
    // before this process started, or one belonging to another account.
    result.replyElement = {
      replayMsgId: id,
      replayMsgSeq: decimal(reference.seq) || '0',
      senderUin: decimal(reference.sender_uin) || '0',
    };
    if (reference.sender_uid !== undefined) result.replyElement.senderUidStr = requireString(reference.sender_uid, 'reply sender_uid', true);
    if (reference.time !== undefined) result.replyElement.replyMsgTime = requireDecimal(reference.time, 'reply time');
    if (reference.client_seq !== undefined) result.replyElement.replyMsgClientSeq = requireDecimal(reference.client_seq, 'reply client_seq');
  }
  return result;
}

async function buildSendElements(elements, suppliedContext) {
  const context = contextOf(suppliedContext);
  if (!Array.isArray(elements) || elements.length === 0) throw new CodecError('INVALID_ELEMENTS', 'Outgoing elements must be a nonempty array');
  // Validate the whole list before any resolver is called. Never filter segments.
  const prepared = Array.from(elements, (element, index) => {
    try { return prepareSend(element, index, context); } catch (error) { if (error instanceof CodecError) error.index ??= index; throw error; }
  });
  for (let index = 0; index < prepared.length; index++) {
    const media = prepared[index];
    if (!media || !media.__media) continue;
    const hook = media.__media === 'image' ? context.prepareImage : context.prepareFile;
    if (typeof hook !== 'function') {
      throw new CodecError('UNSUPPORTED_ELEMENT', `Outgoing ${media.__media} requires a native media preparer`, { index, elementType: media.__media });
    }
    const native = await hook(media.__mediaData, context, index);
    if (!object(native) || typeof native.elementType !== 'number') {
      throw new CodecError('INVALID_MEDIA', 'Media preparer returned an invalid native element', { index });
    }
    prepared[index] = native;
  }
  for (let index = 0; index < prepared.length; index++) {
    const mention = prepared[index].textElement;
    if (!mention || mention.atType !== 2 || mention.atNtUid) continue;
    if (typeof context.resolveUid !== 'function') throw new CodecError('UID_RESOLUTION_REQUIRED', 'User mention requires data.uid or context.resolveUid(uin, context)', { index });
    const uid = await context.resolveUid(mention.atUid, context);
    if (typeof uid !== 'string' || !/^u_\S+$/.test(uid)) throw new CodecError('INVALID_UID', 'resolveUid must return a QQNT u_ UID string', { index });
    mention.atNtUid = uid;
  }
  return prepared;
}

module.exports = { normalizeMessage, normalizeElement, buildSendElements, toJsonSafe, CodecError, SUPPORTED_SEND_TYPES };
