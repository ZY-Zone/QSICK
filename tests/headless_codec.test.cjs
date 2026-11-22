'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeMessage, normalizeElement, buildSendElements, toJsonSafe, CodecError, SUPPORTED_SEND_TYPES } = require('../src/qsick/headless_codec.cjs');

const SNOWFLAKE = '18446744073709551615';
const ACCOUNT = '90071992547409931234';
const nativeText = (content = 'hello', extra = {}) => ({ elementType: 1, elementId: '999', textElement: { content, atType: 0, atUid: '', atNtUid: '', atTinyId: '', ...extra } });
const incoming = (extra = {}) => ({ msgId: SNOWFLAKE, msgSeq: '90071992547409939999', msgTime: '1700000000', msgRandom: '18446744073709551614', clientSeq: '90071992547409939998', chatType: 2, peerUid: '12345', peerUin: '12345', senderUid: 'u_sender', senderUin: '90071992547409939997', sendNickName: 'Nick', sendMemberName: 'Card', elements: [nativeText()], ...extra });
const outgoing = (type, data) => ({ type, data });
const hasCode = code => error => error instanceof CodecError && error.code === code;

// All fixtures are synthetic. These tests never load native QQ, invoke a sender,
// inspect a filesystem path from a message, fetch URLs, or change configuration.

test('stable CommonJS API and outgoing capability list', () => {
  assert.deepEqual(SUPPORTED_SEND_TYPES, ['text', 'at', 'face', 'json', 'reply', 'image', 'file']);
  assert.ok(Object.isFrozen(SUPPORTED_SEND_TYPES));
});

test('normalizes group identity with every snowflake preserved as decimal strings', () => {
  const source = incoming();
  const result = normalizeMessage(source, ACCOUNT);
  assert.equal(result.account, ACCOUNT);
  assert.equal(result.message_id, SNOWFLAKE);
  assert.equal(result.seq, source.msgSeq);
  assert.equal(result.time, '1700000000');
  assert.deepEqual(result.chat, { type: 'group', id: '12345' });
  assert.deepEqual(result.sender, { uin: source.senderUin, uid: 'u_sender', nickname: 'Nick', card: 'Card', role: 'member' });
  assert.deepEqual(result.native.peer, { chatType: 2, peerUid: '12345' });
  assert.equal(result.native.msgId, SNOWFLAKE);
  assert.equal(result.native.msgRandom, source.msgRandom);
  assert.equal(result.native.clientSeq, source.clientSeq);
  assert.deepEqual(result.raw, source);
  assert.notEqual(result.raw, source);
  assert.doesNotThrow(() => JSON.stringify(result));
});

test('BigInt IDs, safe numeric IDs, account object and decimal leading zeros', () => {
  const value = normalizeMessage(incoming({ msgId: BigInt(SNOWFLAKE), msgSeq: 17n, senderUin: 5, peerUid: 23n, peerUin: 23n }), { uin: 7n });
  assert.equal(value.message_id, SNOWFLAKE);
  assert.equal(value.seq, '17');
  assert.equal(value.sender.uin, '5');
  assert.equal(value.chat.id, '23');
  assert.equal(value.account, '7');
  assert.equal(normalizeMessage(incoming({ msgId: '000123' }), 'profile-A').message_id, '000123');
});

test('private conversations use peer, not sender (including self-sent messages)', () => {
  const value = normalizeMessage(incoming({ chatType: 1, peerUid: 'u_friend', peerUin: '123', senderUin: ACCOUNT, senderUid: 'u_self', guildId: '0' }), ACCOUNT);
  assert.deepEqual(value.chat, { type: 'private', id: '123' });
  assert.equal(value.sender.uin, ACCOUNT);
  assert.deepEqual(value.native.peer, { chatType: 1, peerUid: 'u_friend', guildId: '0' });
});

test('synchronous incoming resolver resolves private peers and missing senders', () => {
  const calls = [];
  const value = normalizeMessage(incoming({ chatType: 1, peerUid: 'u_friend', peerUin: '0', senderUin: '' }), ACCOUNT, uid => { calls.push(uid); return uid === 'u_friend' ? '123' : '456'; });
  assert.equal(value.chat.id, '123');
  assert.equal(value.sender.uin, '456');
  assert.deepEqual(calls, ['u_friend', 'u_sender']);
  assert.equal(normalizeMessage(incoming({ senderUin: '' }), ACCOUNT).sender.uin, '0');
});

test('all unsupported chat types return null with explicit reason, never relabeled', () => {
  for (const chatType of [0, 3, 4, 5, 6, 9, 99, 100, 101, 103, 134, 'group', null, undefined]) {
    const reasons = [];
    assert.equal(normalizeMessage(incoming({ chatType }), ACCOUNT, { onSkip: reason => reasons.push(reason) }), null);
    assert.equal(reasons.length, 1);
    assert.equal(reasons[0].code, 'UNSUPPORTED_CHAT_TYPE');
    assert.match(reasons[0].reason, /Only QQNT C2C/);
  }
});

test('malformed message, account, peer and unsafe IDs are rejected explicitly', () => {
  for (const raw of [null, false, 3, [], 'message']) assert.equal(normalizeMessage(raw, ACCOUNT), null);
  for (const id of [Number(SNOWFLAKE), -1, 0, '0', '1e19', ' 12', '12.0', '', null, {}, NaN, Infinity]) assert.equal(normalizeMessage(incoming({ msgId: id }), ACCOUNT), null);
  for (const account of [null, '', undefined, {}, Number(SNOWFLAKE)]) assert.equal(normalizeMessage(incoming(), account), null);
  for (const extra of [
    { peerUid: '', peerUin: '0' }, { peerUid: 'u_group', peerUin: '123' },
    { peerUid: '2', peerUin: '3' }, { peerUid: Number(SNOWFLAKE), peerUin: '123' },
    { peerUid: 'u_friend', peerUin: '0', chatType: 1 },
    { peerUid: '', peerUin: '123', chatType: 1 },
    { peerUid: '123', peerUin: '123', chatType: 1 },
    ...['msgSeq', 'msgTime', 'msgRandom', 'clientSeq', 'senderUin', 'peerUin'].map(key => ({ [key]: Number(SNOWFLAKE) })),
  ]) {
    const reasons = [];
    assert.equal(normalizeMessage(incoming(extra), ACCOUNT, { onSkip: reason => reasons.push(reason) }), null, JSON.stringify(extra));
    assert.ok(reasons[0].reason);
  }
});

test('failed incoming resolver preserves mixed content and refuses unresolved peer', () => {
  const context = { resolveUin: () => { throw Error('cache unavailable'); } };
  const raw = incoming({ senderUin: '', elements: [nativeText('before'), nativeText('@user', { atType: 2, atNtUid: 'u_missing' }), nativeText('after')] });
  const value = normalizeMessage(raw, ACCOUNT, context);
  assert.equal(value.sender.uin, '0');
  assert.deepEqual(value.elements.map(element => element.type), ['text', 'unknown', 'text']);
  assert.equal(normalizeMessage(incoming({ chatType: 1, peerUid: 'u_missing', peerUin: '' }), ACCOUNT, context), null);
});

test('async incoming resolver is a declared contract violation', () => {
  assert.throws(() => normalizeMessage(incoming({ chatType: 1, peerUid: 'u_peer', peerUin: '' }), ACCOUNT, async () => '1'), hasCode('ASYNC_RESOLVER'));
});

test('text content including empty/Unicode is never trimmed or split', () => {
  for (const content of ['', ' a\n文 😀 ', '@ordinary text']) assert.deepEqual(normalizeElement(nativeText(content)).data, { text: content });
});

test('all and user mentions use real atUid/atNtUid shapes without numeric coercion', () => {
  assert.deepEqual(normalizeElement(nativeText('@全体成员', { atType: 1, atUid: 'all', atNtUid: 'all' })).data, { user_id: 'all', name: '@全体成员' });
  const user = normalizeElement(nativeText('@Nick', { atType: 2, atUid: SNOWFLAKE, atNtUid: 'u_target' }));
  assert.equal(user.type, 'at');
  assert.deepEqual(user.data, { user_id: SNOWFLAKE, uid: 'u_target', name: '@Nick' });
  assert.equal(normalizeElement(nativeText('@me', { atType: 4, atUid: '42' })).data.user_id, '42');
  const resolved = normalizeElement(nativeText('@user', { atType: 2, atUid: '0', atNtUid: 'u_target' }), () => SNOWFLAKE);
  assert.equal(resolved.data.user_id, SNOWFLAKE);
  for (const body of [{ atType: 2, atUid: '', atNtUid: 'u_missing' }, { atType: 8 }, { atType: 2, atUid: Number(SNOWFLAKE) }]) {
    const result = normalizeElement(nativeText('@something', body));
    assert.equal(result.type, 'unknown');
    assert.notEqual(result.data.user_id, 'all');
  }
});

test('face preserves index and result ID', () => {
  assert.deepEqual(normalizeElement({ elementType: 6, faceElement: { faceIndex: 14, faceType: 1, resultId: '6' } }).data, { id: '14', result_id: '6' });
});

test('dice/rps report native resultId verbatim, and survive a missing one', () => {
  for (const [faceIndex, type] of [[358, 'dice'], [359, 'rps']]) {
    for (const resultId of ['0', '1', '3', '6', SNOWFLAKE, 2n]) {
      const value = normalizeElement({ elementType: 6, faceElement: { faceIndex, faceType: 3, resultId } });
      assert.equal(value.type, type);
      assert.deepEqual(value.data, { result: String(resultId) });
    }
    // Measured live: QQ sends dice/RPS with no resultId. Those are still real
    // messages, so they render as the face rather than vanishing into UNKNOWN.
    for (const resultId of [undefined, null, '']) {
      const value = normalizeElement({ elementType: 6, faceElement: { faceIndex, faceType: 3, resultId } });
      assert.equal(value.type, 'face');
      assert.deepEqual(value.data, { id: String(faceIndex) });
    }
    // A malformed value is still surfaced verbatim rather than guessed at.
    for (const resultId of ['random', -1, Number(SNOWFLAKE)]) {
      const raw = { elementType: 6, faceElement: { faceIndex, faceType: 3, resultId } };
      assert.equal(normalizeElement(raw).type, 'face');
    }
  }
});

test('poke requires native FaceType.Poke and actual pokeType, not gray-tip inference', () => {
  const raw = { elementType: 6, faceElement: { faceIndex: 358, faceType: 5, pokeType: 126 } };
  const value = normalizeElement(raw);
  assert.equal(value.type, 'poke');
  assert.deepEqual(value.data, { type: '126', id: '358' });
  assert.equal(normalizeElement({ elementType: 6, faceElement: { faceIndex: 1, faceType: 5 } }).type, 'unknown');
  assert.equal(normalizeElement({ elementType: 8, grayTipElement: { subElementType: 17, jsonGrayTipElement: { jsonStr: '{"poke":true}' } } }).type, 'unknown');
});

test('market face maps verified QQNT fields to QSICK mface without fabricated media URL', () => {
  const raw = { elementType: 11, marketFaceElement: { emojiPackageId: 123, emojiId: 'opaque-emoji-id', key: 'native-key', faceName: '[动画]' } };
  const value = normalizeElement(raw);
  assert.equal(value.type, 'mface');
  assert.deepEqual(value.data, { emoji_id: 'opaque-emoji-id', emoji_package_id: '123', key: 'native-key', summary: '[动画]' });
  assert.deepEqual(value.raw, raw);
  assert.equal(value.data.url, undefined);
  for (const patch of [{ emojiPackageId: Number(SNOWFLAKE) }, { emojiId: '' }, { key: undefined }, { faceName: null }]) {
    assert.equal(normalizeElement({ ...raw, marketFaceElement: { ...raw.marketFaceElement, ...patch } }).type, 'unknown');
  }
});

test('markdown content remains inert text and preserves opaque extension metadata', () => {
  const content = '[link](file:///C:/secret)\n<script>ignored</script>';
  const raw = { elementType: 14, markdownElement: { content, mdExtInfo: { flashTransferInfo: { filesetId: SNOWFLAKE } } } };
  const value = normalizeElement(raw);
  assert.equal(value.type, 'markdown');
  assert.deepEqual(value.data, { content });
  assert.deepEqual(value.raw, raw);
  assert.equal(normalizeElement({ elementType: 14, markdownElement: { content: '' } }).type, 'markdown');
  assert.equal(normalizeElement({ elementType: 14, markdownElement: {} }).type, 'unknown');
});

test('inline keyboard preserves rows/buttons and decimal bot_appid for QSICK', () => {
  const rows = [{ buttons: [{ id: SNOWFLAKE, label: 'Click', data: 'https://untrusted.invalid/action', style: 1, type: 2, permissionType: 0, extension: new Map([[1, 2n]]) }] }];
  const raw = { elementType: 17, inlineKeyboardElement: { botAppid: BigInt(SNOWFLAKE), rows } };
  const value = normalizeElement(raw);
  assert.equal(value.type, 'inline_keyboard');
  assert.deepEqual(value.data, { bot_appid: SNOWFLAKE, rows: toJsonSafe(rows) });
  for (const patch of [{ botAppid: Number(SNOWFLAKE) }, { rows: null }, { rows: [{}] }, { rows: [{ buttons: [null] }] }]) {
    assert.equal(normalizeElement({ ...raw, inlineKeyboardElement: { ...raw.inlineKeyboardElement, ...patch } }).type, 'unknown');
  }
});

test('unverified location and contact schemas are retained without invented coordinates or IDs', () => {
  const raw = { elementType: 28, shareLocationElement: { text: 'Somewhere', ext: '{"latitude":1,"longitude":2}' } };
  const value = normalizeElement(raw);
  assert.equal(value.type, 'unknown');
  assert.deepEqual(value.data.raw, raw);
  // No standalone native contact type/schema is verified; opaque server Ark
  // payloads remain JSON rather than interpreting invented meta field names.
  const bytesData = '{"app":"com.tencent.contact","meta":{"uin":"18446744073709551615"}}';
  const contact = normalizeElement({ elementType: 10, arkElement: { bytesData } });
  assert.equal(contact.type, 'json');
  assert.equal(contact.data.data, bytesData);
});

const mediaFixtures = [
  [2, 'picElement', 'image', { fileUuid: 'picture-uuid', fileName: 'x.png', fileSize: SNOWFLAKE, filePath: 'C:\\untrusted\\x.png', sourcePath: 'C:\\untrusted\\source', md5HexStr: 'image-md5', picWidth: 800, picHeight: 600, picSubType: 1, summary: '[图片]', originImageUrl: 'https://untrusted.invalid/x', thumbPath: new Map([[1, 'C:\\untrusted\\thumb']]) }],
  [3, 'fileElement', 'file', { fileUuid: 'file-uuid', fileName: 'x.zip', fileSize: SNOWFLAKE, fileMd5: 'file-md5', filePath: 'C:\\untrusted\\x.zip', fileSubId: 'sub', picThumbPath: new Map([[2, 'thumb']]) }],
  [4, 'pttElement', 'record', { fileUuid: 'voice-uuid', fileId: 23, fileName: 'voice.amr', fileSize: '1024', filePath: 'C:\\untrusted\\voice', md5HexStr: 'ptt-md5', duration: 4 }],
  [5, 'videoElement', 'video', { fileUuid: 'video-uuid', fileName: 'movie.mp4', fileSize: SNOWFLAKE, filePath: 'C:\\untrusted\\movie', videoMd5: 'video-md5', fileTime: 9, fileWidth: 1920, fileHeight: 1080, thumbPath: new Map([[0, 'thumb']]) }],
];
for (const [elementType, field, type, body] of mediaFixtures) {
  test(`maps ${type} exact native fields without reading paths or fetching URLs`, () => {
    const raw = { elementType, elementId: SNOWFLAKE, [field]: body };
    const value = normalizeElement(raw);
    assert.equal(value.type, type);
    assert.equal(value.data.file_id, body.fileUuid);
    assert.equal(value.data.file_name, body.fileName);
    assert.equal(value.data.file_size, body.fileSize);
    assert.equal(value.data.path, body.filePath);
    assert.deepEqual(value.raw, toJsonSafe(raw));
    if (type === 'image') { assert.equal(value.data.url, body.originImageUrl); assert.equal(value.data.sub_type, 1); assert.equal(value.data.width, 800); }
    if (type === 'record') assert.equal(value.data.duration, 4);
    if (type === 'video') assert.equal(value.data.duration, 9);
    if (body.thumbPath || body.picThumbPath) assert.ok(value.data.thumbnails.$map);
    assert.doesNotThrow(() => JSON.stringify(value));
  });
}

test('media fallback identifiers remain strings and non-text-only messages survive', () => {
  assert.equal(normalizeElement({ elementType: 4, elementId: '4', pttElement: { fileId: 23 } }).data.file_id, '23');
  assert.equal(normalizeElement({ elementType: 2, elementId: SNOWFLAKE, picElement: { md5HexStr: 'hash' } }).data.file_id, 'hash');
  assert.equal(normalizeElement({ elementType: 3, elementId: SNOWFLAKE, fileElement: {} }).data.file_id, SNOWFLAKE);
  const value = normalizeMessage(incoming({ elements: [{ elementType: 5, videoElement: { fileUuid: 'uuid' } }] }), ACCOUNT);
  assert.equal(value.elements.length, 1);
  assert.equal(value.elements[0].type, 'video');
});

test('reply uses replayMsgId/replayMsgSeq (native spelling), source ID fallback, and metadata', () => {
  const value = normalizeElement({ elementType: 7, replyElement: { replayMsgId: SNOWFLAKE, replayMsgSeq: '999', senderUin: '321', senderUidStr: 'u_reply', replyMsgTime: '111', replyMsgClientSeq: '222' } });
  assert.deepEqual(value.data, { message_id: SNOWFLAKE, seq: '999', sender_uin: '321', sender_uid: 'u_reply', time: '111', client_seq: '222' });
  assert.equal(normalizeElement({ elementType: 7, replyElement: { sourceMsgIdInRecords: SNOWFLAKE } }).data.message_id, SNOWFLAKE);
  const seqOnly = normalizeElement({ elementType: 7, replyElement: { replayMsgSeq: '23' } });
  assert.equal(seqOnly.type, 'reply');
  assert.equal(seqOnly.data.message_id, undefined);
});

test('ark JSON and XML preserve original text without external entity processing', () => {
  for (const data of [' {"hello":1,"id":18446744073709551615} ', '[1,2]', 'null']) {
    const value = normalizeElement({ elementType: 10, arkElement: { bytesData: data } });
    assert.equal(value.type, 'json');
    assert.equal(value.data.data, data);
  }
  const xml = '<!DOCTYPE x SYSTEM "file:///C:/secret"><msg>&x;</msg>';
  assert.deepEqual(normalizeElement({ elementType: 10, arkElement: { bytesData: xml } }).data, { data: xml });
  assert.equal(normalizeElement({ elementType: 10, arkElement: { bytesData: xml } }).type, 'xml');
});

test('native multi-forward and struct-long-message fields are retained without fetching records', () => {
  for (const [elementType, key] of [[16, 'multiForwardMsgElement'], [13, 'structLongMsgElement']]) {
    const value = normalizeElement({ elementType, [key]: { resId: 'opaque-resource', xmlContent: '<msg/>', fileName: 'forward.xml' } });
    assert.deepEqual(value.data, { res_id: 'opaque-resource', summary: '', content: [], xml: '<msg/>', file_name: 'forward.xml' });
    assert.equal(value.type, 'forward');
    assert.equal(normalizeElement({ elementType, [key]: { xmlContent: '<msg/>', resId: '' } }).type, 'xml');
  }
  const ark = JSON.stringify({ app: 'com.tencent.multimsg', meta: { detail: { resid: 'ark-resource', summary: '2 messages' } } });
  const value = normalizeElement({ elementType: 10, arkElement: { bytesData: ark } });
  assert.deepEqual(value.data, { res_id: 'ark-resource', summary: '2 messages', content: [], data: ark });
});

test('unknown/malformed elements are retained one-for-one in mixed messages', () => {
  const entries = [nativeText('before'), { elementType: 999, payload: { huge: BigInt(SNOWFLAKE), map: new Map([[1, 2n]]) } }, null, 'bad', 4, { elementType: 1 }, { elementType: 1, textElement: { content: 5 } }, { elementType: 6, faceElement: { faceIndex: Number(SNOWFLAKE) } }, { elementType: 7, replyElement: {} }, { elementType: 10, arkElement: { bytesData: 'garbage' } }, { elementType: 13, structLongMsgElement: {} }, nativeText('after')];
  const value = normalizeMessage(incoming({ elements: entries }), ACCOUNT);
  assert.equal(value.elements.length, entries.length);
  assert.equal(value.elements[0].type, 'text');
  assert.equal(value.elements.at(-1).type, 'text');
  for (let index = 1; index < entries.length - 1; index++) {
    assert.equal(value.elements[index].type, 'unknown');
    assert.deepEqual(value.elements[index].data.raw, toJsonSafe(entries[index]));
  }
  assert.equal(value.elements[1].data.original_type, '999');
  for (const elements of [null, undefined, {}, 'text']) {
    const result = normalizeMessage(incoming({ elements }), ACCOUNT);
    assert.equal(result.elements.length, 1);
    assert.equal(result.elements[0].type, 'unknown');
  }
  assert.deepEqual(normalizeMessage(incoming({ elements: [] }), ACCOUNT).elements, []);
});

test('safe serialization handles maps, bigint, cycles, shared references, and prototype keys', () => {
  const shared = { id: 2n };
  const value = { big: BigInt(SNOWFLAKE), map: new Map([[1, 'number'], ['1', 'string'], ['__proto__', shared]]), set: new Set([2n]), a: shared, b: shared, nan: NaN, infinity: Infinity, missing: undefined };
  value.self = value;
  Object.defineProperty(value, '__proto__', { value: { injected: true }, enumerable: true });
  const safe = toJsonSafe(value);
  assert.equal(safe.big, SNOWFLAKE);
  assert.deepEqual(safe.map.$map, [[1, 'number'], ['1', 'string'], ['__proto__', { id: '2' }]]);
  assert.deepEqual(safe.self, { $circular: true });
  assert.deepEqual(safe.a, safe.b);
  assert.deepEqual(safe.set, { $set: ['2'] });
  assert.equal(safe.injected, undefined);
  assert.equal(Object.getPrototypeOf(safe), Object.prototype);
  assert.equal(safe.__proto__.injected, true);
  assert.doesNotThrow(() => JSON.stringify(safe));
});

test('outgoing text and @all use the full native text-element contract', async () => {
  const value = await buildSendElements([outgoing('text', { text: ' hello ' }), outgoing('at', { user_id: 'all' })]);
  assert.deepEqual(value, [
    { elementType: 1, elementId: '', textElement: { content: ' hello ', atType: 0, atUid: '', atTinyId: '', atNtUid: '' } },
    { elementType: 1, elementId: '', textElement: { content: '@全体成员', atType: 1, atUid: 'all', atTinyId: '', atNtUid: 'all' } },
  ]);
});

test('outgoing user mention resolves UID asynchronously without Number conversion', async () => {
  const calls = [];
  const context = { peer: { chatType: 2, peerUid: '123' }, resolveUid: async (uin, supplied) => { calls.push([uin, supplied]); return 'u_resolved'; } };
  const [value] = await buildSendElements([outgoing('at', { user_id: SNOWFLAKE, name: '@Nick' })], context);
  assert.equal(value.textElement.atUid, SNOWFLAKE);
  assert.equal(value.textElement.atNtUid, 'u_resolved');
  assert.equal(value.textElement.content, '@Nick');
  assert.deepEqual(calls, [[SNOWFLAKE, context]]);
  const [direct] = await buildSendElements([outgoing('at', { user_id: 123n, uid: 'u_direct' })], { resolveUid: () => { throw Error('must not be called'); } });
  assert.equal(direct.textElement.atUid, '123');
  assert.equal(direct.textElement.atNtUid, 'u_direct');
});

test('outgoing legacy face and valid ark JSON conversion', async () => {
  const [face, json] = await buildSendElements([outgoing('face', { id: '14' }), outgoing('json', { data: { id: BigInt(SNOWFLAKE), map: new Map([['x', 1n]]) } })]);
  assert.deepEqual(face, { elementId: '', elementType: 6, faceElement: { faceIndex: 14, faceType: 1, faceText: '' } });
  assert.equal(json.elementType, 10);
  assert.equal(json.arkElement.linkInfo, null);
  assert.equal(json.arkElement.subElementType, null);
  assert.deepEqual(JSON.parse(json.arkElement.bytesData), { id: SNOWFLAKE, map: { $map: [['x', '1']] } });
  const bytes = '{"id":18446744073709551615}';
  assert.equal((await buildSendElements([outgoing('json', { data: bytes })]))[0].arkElement.bytesData, bytes);
});

test('outgoing reply requires native ID, sequence and sender; preserves metadata', async () => {
  const [reply] = await buildSendElements([outgoing('reply', { message_id: SNOWFLAKE, seq: '90071992547409931', sender_uin: '90071992547409932', sender_uid: 'u_sender', time: '1700000000', client_seq: '9223372036854775807' })]);
  assert.deepEqual(reply.replyElement, { replayMsgId: SNOWFLAKE, replayMsgSeq: '90071992547409931', senderUin: '90071992547409932', senderUidStr: 'u_sender', replyMsgTime: '1700000000', replyMsgClientSeq: '9223372036854775807' });
  await assert.rejects(buildSendElements([outgoing('reply', { message_id: SNOWFLAKE })]), hasCode('INVALID_ID'));
});

test('bare reply ID enriches from exact account-scoped messageRefs and matching peer', async () => {
  const native = normalizeMessage(incoming(), ACCOUNT).native;
  const context = { peer: { ...native.peer, guildId: 'send-correlation-token' }, messageRefs: new Map([[SNOWFLAKE, native]]) };
  const [reply] = await buildSendElements([outgoing('reply', { message_id: SNOWFLAKE })], context);
  assert.deepEqual(reply.replyElement, { replayMsgId: SNOWFLAKE, replayMsgSeq: native.msgSeq, senderUin: native.senderUin, senderUidStr: native.senderUid, replyMsgTime: native.msgTime, replyMsgClientSeq: native.clientSeq });
  const [trusted] = await buildSendElements([outgoing('reply', { message_id: SNOWFLAKE, seq: '1', sender_uin: '2' })], context);
  assert.equal(trusted.replyElement.replayMsgSeq, native.msgSeq);
  assert.equal(trusted.replyElement.senderUin, native.senderUin);
});

test('cached replies reject cross-peer, absent-peer, malformed, and mismatched-ID references', async () => {
  const native = normalizeMessage(incoming(), ACCOUNT).native;
  const segment = outgoing('reply', { message_id: SNOWFLAKE });
  for (const peer of [undefined, { chatType: 2, peerUid: 'other-group' }, { chatType: 1, peerUid: native.peer.peerUid }]) {
    await assert.rejects(buildSendElements([segment], { peer, messageRefs: new Map([[SNOWFLAKE, native]]) }), hasCode('REPLY_PEER_MISMATCH'));
  }
  for (const reference of [null, {}, { ...native, msgId: '1' }, { ...native, peer: { chatType: 99, peerUid: native.peer.peerUid } }]) {
    await assert.rejects(buildSendElements([segment], { peer: native.peer, messageRefs: new Map([[SNOWFLAKE, reference]]) }), hasCode('REPLY_PEER_MISMATCH'));
  }
  await assert.rejects(buildSendElements([segment], { peer: native.peer, messageRefs: new Map([[BigInt(SNOWFLAKE), native]]) }), hasCode('INVALID_ID'));
  await assert.rejects(buildSendElements([outgoing('json', { data: null })]), hasCode('INVALID_JSON'));
});

test('every unsupported outgoing element rejects entire mixed message before resolver calls', async () => {
  for (const type of ['image', 'record', 'voice', 'video', 'file', 'xml', 'forward', 'markdown', 'mface', 'dice', 'rps', 'poke', 'inline_keyboard', 'location', 'contact', 'unknown', 'invalid']) {
    let calls = 0;
    await assert.rejects(buildSendElements([outgoing('text', { text: 'before' }), outgoing('at', { user_id: '123' }), outgoing(type, { url: 'https://untrusted.invalid/file', path: 'C:\\untrusted' }), outgoing('text', { text: 'after' })], { resolveUid: async () => { calls++; return 'u_x'; } }), error => error.code === 'UNSUPPORTED_ELEMENT' && error.index === 2);
    assert.equal(calls, 0);
  }
});

test('outgoing malformed input, unsafe numbers, invalid JSON/faces and private mentions fail', async () => {
  for (const value of [undefined, null, {}, 'text', []]) await assert.rejects(buildSendElements(value), hasCode('INVALID_ELEMENTS'));
  for (const value of [null, {}, { type: 'text' }, outgoing('text', { text: 23 }), outgoing('at', { user_id: Number(SNOWFLAKE) }), outgoing('face', { id: '-1' }), outgoing('json', { data: '{' })]) await assert.rejects(buildSendElements([value]), CodecError);
  await assert.rejects(buildSendElements(new Array(2)), CodecError);
  // High ids are legitimate now (QQ has faces in the 300s and 500s); only values
  // that cannot be a face index are rejected.
  for (const id of [SNOWFLAKE, '0', '-5', 'abc']) await assert.rejects(buildSendElements([outgoing('face', { id })]), CodecError);
  await assert.rejects(buildSendElements([outgoing('face', { id: 14, result_id: '1' })]), hasCode('UNSUPPORTED_FACE'));
  // Ids >= 260 take the small-face commonElem instead of the legacy element, so
  // the three encodings are checked by their wire shape, not just the index.
  for (const id of [5, 222]) {
    const built = await buildSendElements([outgoing('face', { id })]);
    assert.equal(built[0].faceElement.faceIndex, id);
  }
  for (const id of [358, 478, 515]) {
    const built = await buildSendElements([outgoing('face', { id })]);
    assert.equal(built[0].faceElement.faceIndex, id);
  }
  await assert.rejects(buildSendElements([outgoing('at', { user_id: 'all' })], { peer: { chatType: 1 } }), hasCode('INVALID_PEER'));
  await assert.rejects(buildSendElements([outgoing('at', { user_id: '123' })]), hasCode('UID_RESOLUTION_REQUIRED'));
  for (const uid of ['', null, '123', 123, {}]) await assert.rejects(buildSendElements([outgoing('at', { user_id: '123' })], { resolveUid: async () => uid }), hasCode('INVALID_UID'));
  await assert.rejects(buildSendElements([outgoing('at', { user_id: '123', uid: 'not-a-uid' })]), hasCode('INVALID_UID'));
  await assert.rejects(buildSendElements([outgoing('at', { user_id: '123' })], { resolveUid: async () => { throw Error('resolver unavailable'); } }), /resolver unavailable/);
});

test('conversion does not mutate raw or outgoing elements and retains ordering', async () => {
  const source = incoming();
  const before = structuredClone(source);
  normalizeMessage(source, ACCOUNT);
  assert.deepEqual(source, before);
  const input = [outgoing('text', { text: 'start' }), outgoing('at', { user_id: '123', uid: 'u_x' }), outgoing('face', { id: 14 }), outgoing('text', { text: 'end' })];
  const original = structuredClone(input);
  const result = await buildSendElements(input);
  assert.deepEqual(input, original);
  assert.deepEqual(result.map(element => element.elementType), [1, 1, 6, 1]);
  assert.equal(result.at(-1).textElement.content, 'end');
});


test('faces stay faceElement, with AniSticke for animated ones', async () => {
  // This goes through QQNT's native sendMsg, whose element table has no
  // commonElem entry. An earlier attempt used the commonElem form from
  // NapCat's proto transport and QQ rejected every face with result 5.
  const [classic] = await buildSendElements([outgoing('face', { id: '14', text: '微笑' })]);
  assert.equal(classic.elementType, 6);
  assert.equal(classic.faceElement.faceIndex, 14);
  assert.equal(classic.faceElement.faceType, 1);
  assert.equal(classic.commonElem, undefined);

  const [modern] = await buildSendElements([outgoing('face', { id: '478', text: '对的对的' })]);
  assert.equal(modern.faceElement.faceIndex, 478);
  assert.equal(modern.faceElement.faceType, 1);

  // NapCat's FaceType: OldFace=1, Normal=2, AniSticke=3. Animated faces must
  // carry AniSticke plus the pack/sticker triple, or QQ remaps the id.
  const [animated] = await buildSendElements([outgoing('face', {
    id: '424', text: '续标识', sticker_type: 1, sticker_id: '52', pack_id: '1',
  })]);
  assert.equal(animated.faceElement.faceIndex, 424);
  assert.equal(animated.faceElement.faceType, 3);
  assert.equal(animated.faceElement.stickerType, 1);
  assert.equal(animated.faceElement.stickerId, '52');
  assert.equal(animated.faceElement.packId, '1');
  assert.equal(animated.faceElement.sourceType, 1);
});

test('a face delivered as <faceType=..> markup becomes a real face segment', () => {
  // Measured live: QQ put this exact string in a text element's content. It used
  // to reach the reader as literal markup, which is why the face looked wrong.
  const raw = { elementType: 1, elementId: '7707098593803467595',
    textElement: { content: '<faceType=1,faceId="102",ext="eyJ0ZXh0Ijoi5bem5ZO85ZO8In0=">', atType: 0 } };
  const out = normalizeElement(raw, {});
  assert.equal(out.length, 1);
  assert.equal(out[0].type, 'face');
  assert.equal(out[0].data.id, '102');
  // ext is base64 JSON {"text":"左哼哼"}; the decoded label travels with the face.
  assert.equal(out[0].data.text, '左哼哼');
});

test('markup faces split the surrounding text and keep its order', () => {
  const raw = { elementType: 1, textElement: {
    content: '看<faceType=1,faceId="5">这个', atType: 0 } };
  const out = normalizeElement(raw, {});
  assert.deepEqual(out.map(item => [item.type, item.data.text || item.data.id]),
    [['text', '看'], ['face', '5'], ['text', '这个']]);
});

test('text without face markup stays one text segment', () => {
  for (const content of ['普通文本', 'a < b', '<faceType=1>', '']) {
    const out = normalizeElement({ elementType: 1, textElement: { content, atType: 0 } }, {});
    const list = Array.isArray(out) ? out : [out];
    assert.ok(list.every(item => item.type === 'text'), content);
    assert.equal(list.map(item => item.data.text).join(''), content);
  }
});

test('a faceType=5 markup becomes a poke rather than a face', () => {
  const out = normalizeElement({ elementType: 1, textElement: {
    content: '<faceType=5,faceId="358">', atType: 0 } }, {});
  assert.equal(out[0].type, 'poke');
  assert.equal(out[0].data.id, '358');
});
