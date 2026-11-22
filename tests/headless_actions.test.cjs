'use strict';
// Offline only: all QQNT sessions/services are stubs. No runtime imports or IO.
const test = require('node:test');
const assert = require('node:assert/strict');
const { createActionDispatcher, ActionError } = require('../src/qsick/headless_actions.cjs');

const U = 'u_verifiedAlice';
const profile = (uin = '123', uid = U) => ({ coreInfo: { uin, uid, nick: 'Alice', remark: 'A' }, baseInfo: { sex: 2, age: 23 } });
const member = (uin = '123', uid = U) => ({ uin, uid, nick: 'Alice', cardName: 'Card', role: 3, joinTime: '1234', lastSpeakTime: '2345', shutUpTime: 0, memberRealLevel: 8 });
const group = { groupCode: '456', groupName: 'Group', memberCount: 2, maxMember: 200 };
function fixture(services = {}, extra = {}) {
  const session = Object.fromEntries(Object.entries(services).map(([name, value]) => [`get${name}Service`, () => value]));
  return createActionDispatcher({ getSession: () => session, getIdentity: () => ({ uin: '123', nickname: 'Alice' }), timeoutMs: 80, ...extra });
}
function eventGroup(overrides = {}) {
  let next = 0;
  const listeners = new Map();
  const removed = [];
  const svc = {
    listeners, removed,
    addKernelGroupListener(listener) { listeners.set(++next, listener); return next; },
    removeKernelGroupListener(handle) { removed.push(handle); listeners.delete(handle); },
    emit(event, ...args) { for (const listener of listeners.values()) listener[event]?.(...args); },
    ...overrides,
  };
  return svc;
}
const rejects = (promise, code) => assert.rejects(promise, e => e instanceof ActionError && e.code === code);

test('login returns OneBot fields and Python uin alias without requiring a session', async () => {
  const d = fixture({}, { getSession: () => null });
  assert.deepEqual(await d.execute('get_login_info'), { user_id: 123, uin: 123, nickname: 'Alice' });
  await rejects(fixture({}, { getIdentity: () => ({ uin: '0' }) }).execute('login.info'), 'NOT_READY');
  await rejects(fixture({}, { getIdentity: () => ({ uin: 9007199254740992 }) }).execute('login.info'), 'INVALID_RESULT');
});

test('group list requires successful ACK and full correlated event, ignoring deltas', async () => {
  const svc = eventGroup({ getGroupList(force) {
    assert.equal(force, true);
    this.emit('onGroupListUpdate', 2, [{ ...group, groupName: 'delta' }]);
    this.emit('onGroupListUpdate', 1, [group]); // synchronous event before ACK
    return Promise.resolve({ result: '0' });
  } });
  const d = fixture({ Group: svc });
  assert.deepEqual(await d.execute('get_group_list', { no_cache: true }), [{ group_id: 456, group_name: 'Group', member_count: 2, max_member_count: 200 }]);
  assert.equal(svc.listeners.size, 0);
  assert.equal(svc.removed.length, 1);
});

test('empty full group event is real success, never infer empty list from ACK', async () => {
  const empty = eventGroup({ getGroupList() { this.emit('onGroupListUpdate', 0, []); return { result: 0 }; } });
  assert.deepEqual(await fixture({ Group: empty }).execute('group.list'), []);
  const ackOnly = eventGroup({ getGroupList() { return { result: 0 }; } });
  await rejects(fixture({ Group: ackOnly }, { timeoutMs: 8 }).execute('group.list'), 'TIMEOUT');
  assert.equal(ackOnly.listeners.size, 0);
  const deltaOnly = eventGroup({ getGroupList() { this.emit('onGroupListUpdate', 3, []); return { result: 0 }; } });
  await rejects(fixture({ Group: deltaOnly }, { timeoutMs: 8 }).execute('group.list'), 'TIMEOUT');
  assert.equal(deltaOnly.listeners.size, 0);
});

test('group-list failure/malformed events clean up listeners and do not fake success', async () => {
  for (const response of [{ result: -1 }, { result: '12' }, {}]) {
    const svc = eventGroup({ getGroupList() { this.emit('onGroupListUpdate', 1, []); return response; } });
    await rejects(fixture({ Group: svc }).execute('group.list'), ownResult(response) ? 'SERVICE_ERROR' : 'INVALID_RESULT');
    assert.equal(svc.listeners.size, 0);
  }
  const svc = eventGroup({ getGroupList() { this.emit('onGroupListUpdate', 1, undefined); return { result: 0 }; } });
  await rejects(fixture({ Group: svc }).execute('group.list'), 'INVALID_RESULT');
  assert.equal(svc.listeners.size, 0);
  const throwing = eventGroup({ getGroupList() { throw new Error('stub failure'); } });
  await rejects(fixture({ Group: throwing }).execute('group.list'), 'SERVICE_ERROR');
  assert.equal(throwing.listeners.size, 0);
});
function ownResult(o) { return Object.hasOwn(o, 'result'); }

test('missing event APIs are unsupported and request method is never called', async () => {
  let called = false;
  const d = fixture({ Group: { getGroupList() { called = true; return { result: 0 }; } } });
  await rejects(d.execute('group.list'), 'UNSUPPORTED');
  assert.equal(called, false);
  assert.equal(d.capabilities().operations['group.list'].supported, false);
});

test('group.info correlates groupCode, passes KDATACARD=2 and normalizes detail fields', async () => {
  const svc = eventGroup({ getGroupDetailInfo(code, source) {
    assert.equal(code, '456'); assert.equal(source, 2);
    this.emit('onGroupDetailInfoChange', { ...group, groupCode: '999' });
    this.emit('onGroupDetailInfoChange', { groupCode: code, groupName: 'Detail', memberNum: 4, maxMemberNum: 500 });
    return { result: 0 };
  } });
  assert.deepEqual(await fixture({ Group: svc }).execute('get_group_info', { group_id: 456 }), {
    group_id: 456, group_name: 'Detail', member_count: 4, max_member_count: 500,
  });
  assert.equal(svc.listeners.size, 0);
});

test('concurrent group detail requests do not accept the other group event', async () => {
  const svc = eventGroup({ getGroupDetailInfo(code) {
    queueMicrotask(() => this.emit('onGroupDetailInfoChange', { ...group, groupCode: code }));
    return { result: 0 };
  } });
  const d = fixture({ Group: svc });
  const [a, b] = await Promise.all([d.execute('group.info', { group_id: 456 }), d.execute('group.info', { group_id: 789 })]);
  assert.equal(a.group_id, 456); assert.equal(b.group_id, 789); assert.equal(svc.listeners.size, 0);
});

test('member list/info uses errCode and complete result.infos, with correct OneBot role/time', async () => {
  const calls = [];
  const d = fixture({ Group: { getAllMemberList(...args) {
    calls.push(args); return { errCode: '0', result: { finish: true, ids: [{ uid: U, index: 0 }], infos: new Map([[U, member()]]) } };
  } } });
  const rows = await d.execute('get_group_member_list', { group_id: '456', no_cache: true });
  assert.equal(rows[0].user_id, 123); assert.equal(rows[0].uin, 123); assert.equal(rows[0].role, 'admin');
  assert.equal(rows[0].join_time, 1234); assert.equal(rows[0].last_sent_time, 2345); assert.equal(rows[0].level, '8');
  assert.deepEqual(calls[0], ['456', true]);
  assert.deepEqual(await d.execute('get_group_member_info', { group_id: 456, user_id: 123 }), rows[0]);
  await rejects(d.execute('group.member', { group_id: 456, user_id: 124 }), 'NOT_FOUND');
});

test('member list never accepts incomplete, missing, or failed results as empty lists', async () => {
  for (const [response, code] of [
    [{ errCode: 1, result: { infos: new Map(), finish: true } }, 'SERVICE_ERROR'],
    [{ result: { infos: new Map(), finish: true } }, 'INVALID_RESULT'],
    [{ errCode: 0, result: { infos: new Map(), finish: false } }, 'INVALID_RESULT'],
    [{ errCode: 0, result: { infos: [], finish: true } }, 'INVALID_RESULT'],
    [{ errCode: 0 }, 'INVALID_RESULT'],
    [{ errCode: 0, result: { finish: true, ids: [{ uid: U }], infos: new Map() } }, 'INVALID_RESULT'],
    [{ errCode: 0, result: { finish: true, ids: [], infos: new Map([[U, member()]]) } }, 'INVALID_RESULT'],
    [{ errCode: 0, result: { finish: true, ids: [{ uid: U }], infos: new Map([[U, member('123', 'u_other')]]) } }, 'INVALID_RESULT'],
  ]) await rejects(fixture({ Group: { getAllMemberList: () => response } }).execute('group.members', { group_id: 456 }), code);
});

test('friend list uses cached categories, deduplicates UIDs, and returns profile facts', async () => {
  const d = fixture({
    Buddy: { hasBuddyList: () => true, getBuddyListFromCache(type) { assert.equal(type, 0); return [{ buddyUids: [U] }, { buddyUids: [U] }]; } },
    Profile: { getCoreAndBaseInfo(from, ids) { assert.equal(from, 'qsick'); assert.deepEqual(ids, [U]); return new Map([[U, profile()]]); } },
  });
  assert.deepEqual(await d.execute('get_friend_list'), [{ user_id: 123, uin: 123, uid: U, nickname: 'Alice', remark: 'A', sex: 'female', age: 23 }]);
  assert.equal(await d.resolveUid(123), U); // cached profile fact, no native lookup needed
  await rejects(d.execute('friend.list', { no_cache: true }), 'UNSUPPORTED');
});

test('friend list distinguishes uninitialized cache, actual empty cache, and missing profiles', async () => {
  await rejects(fixture({ Buddy: { hasBuddyList: () => false, getBuddyListFromCache: () => [] }, Profile: { getCoreAndBaseInfo: () => new Map() } }).execute('friend.list'), 'NOT_READY');
  assert.deepEqual(await fixture({ Buddy: { hasBuddyList: () => true, getBuddyListFromCache: () => [] }, Profile: { getCoreAndBaseInfo: () => new Map() } }).execute('friend.list'), []);
  await rejects(fixture({ Buddy: { getBuddyListFromCache: () => [{ buddyUids: [U] }] }, Profile: { getCoreAndBaseInfo: () => new Map() } }).execute('friend.list'), 'INVALID_RESULT');
  await rejects(fixture({ Buddy: { getBuddyList: () => ({ result: 0 }) } }).execute('friend.list'), 'UNSUPPORTED');
  await rejects(fixture({ Buddy: { getBuddyListFromCache: () => [] }, Profile: { getCoreAndBaseInfo: () => new Map() } }).execute('friend.list'), 'UNSUPPORTED');
});

test('profile aliases validate ACK and requested UIN, cache only verified facts', async () => {
  const d = fixture({ Profile: { getUserDetailInfoByUin(uin) { assert.equal(uin, '123'); return { result: '0', detail: { uin, uid: U, simpleInfo: profile() } }; } } });
  const info = await d.execute('get_stranger_info', { user_id: '123' });
  assert.equal(info.sex, 'female'); assert.equal(await d.resolveUid(123), U);
  assert.deepEqual(await d.execute('user.profile', { user_id: 123 }), info);
  await rejects(fixture({ Profile: { getUserDetailInfoByUin: () => ({ result: 0, detail: { uin: '124', uid: U, simpleInfo: profile() } }) } }).execute('friend.info', { user_id: 123 }), 'INVALID_RESULT');
  await rejects(fixture({ Profile: { getUserDetailInfoByUin: () => ({ detail: { uin: '123', uid: U, simpleInfo: profile() } }) } }).execute('friend.info', { user_id: 123 }), 'INVALID_RESULT');
});

test('UID lookup uses synchronous cached profile facts and never fabricates identifiers', async () => {
  const calls = [];
  const d = fixture({ Profile: { getUidByUin(...args) { calls.push(args); return new Map([['123', U]]); } } });
  assert.equal(await d.resolveUid('123'), U);
  assert.equal(await d.resolveUid(123), U);
  assert.deepEqual(calls, [['qsick', ['123']]]);
  await rejects(d.resolveUid(124), 'UNSUPPORTED');
  await rejects(fixture({ Profile: { getUidByUin: () => ({ 123: U }) } }).resolveUid(123), 'INVALID_RESULT');
  await rejects(fixture({ Profile: { getUidByUin: () => new Map([['123', '123']]) } }).resolveUid(123), 'INVALID_RESULT');
  await rejects(fixture({}).resolveUid(123), 'UNSUPPORTED');
});

test('changing sessions clears cached UID facts', async () => {
  let current = { getProfileService: () => ({ getUidByUin: () => new Map([['123', U]]) }) };
  const d = fixture({}, { getSession: () => current });
  assert.equal(await d.resolveUid(123), U);
  current = { getProfileService: () => ({ getUidByUin: () => new Map() }) };
  await rejects(d.resolveUid(123), 'UNSUPPORTED');
});

test('management dispatch uses exact verified signatures and zero-status success only', async () => {
  const calls = [];
  const capture = name => (...args) => { calls.push([name, ...args]); return { result: '0', errMsg: '' }; };
  const d = fixture({ Group: {
    setMemberShutUp: capture('mute'), setGroupShutUp: capture('all'), modifyGroupName: capture('name'),
    modifyGroupRemark: capture('remark'), quitGroupV2: capture('leave'),
  }, Profile: { getUidByUin: () => new Map([['123', U]]) } });
  assert.deepEqual(await d.execute('set_group_ban', { group_id: 456, user_id: 123, duration: 0 }), {});
  await d.execute('group.mute', { group_id: 456, user_id: 123, duration: 2592000 });
  await d.execute('set_group_whole_ban', { group_id: 456, enable: false });
  await d.execute('set_group_name', { group_id: 456, group_name: 'New' });
  await d.execute('set_group_remark', { group_id: 456, remark: '' });
  await d.execute('set_group_leave', { group_id: 456 });
  assert.deepEqual(calls, [
    ['mute', '456', [{ uid: U, timeStamp: 0 }]], ['mute', '456', [{ uid: U, timeStamp: 2592000 }]],
    ['all', '456', false], ['name', '456', 'New', false], ['remark', '456', ''],
    ['leave', { groupCode: '456', needDeleteLocalMsg: false }],
  ]);
});

test('void mutations without readback and unverified destructive contracts never invoke methods', async () => {
  let called = 0;
  const unexpected = () => { called++; };
  const d = fixture({ Group: { kickMember: unexpected, kickMemberV2: unexpected, modifyMemberRole: unexpected, modifyMemberCardName: unexpected, quitGroup: unexpected, quitGroupV2: unexpected, destroyGroup: unexpected }, Buddy: { setBuddyRemark: unexpected, delBuddy: unexpected } });
  for (const op of ['set_group_kick', 'set_group_card', 'set_group_admin', 'set_group_special_title', 'set_friend_remark', 'delete_friend']) {
    await rejects(d.execute(op, { group_id: 456, user_id: 123 }), 'UNSUPPORTED');
  }
  await rejects(d.execute('group.leave', { group_id: 456, is_dismiss: true }), 'UNSUPPORTED');
  assert.equal(called, 0);
});

function completeMembers(rows) {
  return { errCode: 0, result: { finish: true, ids: rows.map((m, index) => ({ uid: m.uid, index })), infos: new Map(rows.map(m => [m.uid, m])) } };
}

test('kick uses kickMemberV2 status plus verified absence and writes once', async () => {
  let rows = [member()], writes = 0;
  const calls = [];
  const d = fixture({ Group: {
    getAllMemberList: () => completeMembers(rows),
    kickMemberV2(args) { writes++; calls.push(args); rows = []; return { result: 0 }; },
  } });
  assert.deepEqual(await d.execute('set_group_kick', { group_id: 456, user_id: 123 }), {});
  assert.equal(writes, 1);
  assert.deepEqual(calls[0], {
    groupCode: '456', kickFlag: 0,
    kickList: [{ optFlag: 0, optOperate: 0, optMemberUid: U, optBytesMsg: '' }],
    kickListUids: [U], kickMsg: '',
  });
});

test('kick never reports success without confirmed removal', async () => {
  // Target stays present: ACK alone must time out, not claim success.
  let writes = 0;
  const stays = fixture({ Group: { getAllMemberList: () => completeMembers([member()]), kickMemberV2() { writes++; return { result: 0 }; } } }, { timeoutMs: 8 });
  await rejects(stays.execute('group.kick', { group_id: 456, user_id: 123 }), 'TIMEOUT');
  assert.equal(writes, 1);
  // Missing target: no native kick is issued at all.
  const absent = fixture({ Group: { getAllMemberList: () => completeMembers([]), kickMemberV2() { writes++; } } });
  await rejects(absent.execute('group.kick', { group_id: 456, user_id: 123 }), 'NOT_FOUND');
  assert.equal(writes, 1);
  // Nonzero native status is a service error even if the target later vanishes.
  const failed = fixture({ Group: { getAllMemberList: () => completeMembers([member()]), kickMemberV2() { return { result: -1, errMsg: 'denied' }; } } });
  await rejects(failed.execute('group.kick', { group_id: 456, user_id: 123 }), 'SERVICE_ERROR');
});

test('ocr requires a native GeneralCallResult and returns recognised text', async () => {
  const calls = [];
  const d = fixture({ NodeMisc: { wantWinScreenOCR(path) { calls.push(path); return { result: 0, text: 'hello' }; } } });
  assert.deepEqual(await d.execute('ocr_image', { image_path: 'C:/tmp/a.png' }), { text: 'hello' });
  assert.deepEqual(calls, ['C:/tmp/a.png']);
  // Success without a text field yields empty text, never a fabricated string.
  const bare = fixture({ NodeMisc: { wantWinScreenOCR: () => ({ result: 0 }) } });
  assert.deepEqual(await bare.execute('system.ocr', { image_path: '/a.png' }), { text: '' });
  const failed = fixture({ NodeMisc: { wantWinScreenOCR: () => ({ result: 7, errMsg: 'no' }) } });
  await rejects(failed.execute('system.ocr', { image_path: '/a.png' }), 'SERVICE_ERROR');
  const missing = fixture({ NodeMisc: { wantWinScreenOCR: () => ({}) } });
  await rejects(missing.execute('system.ocr', { image_path: '/a.png' }), 'INVALID_RESULT');
  let touched = false;
  await rejects(fixture({ NodeMisc: { wantWinScreenOCR() { touched = true; } } }).execute('system.ocr', {}), 'INVALID_PARAMS');
  assert.equal(touched, false);
});

test('reaction list uses the cached native reference and reports paging', async () => {
  const refs = new Map([['9007199254740993', { msgId: '9007199254740993', msgSeq: '77', peer: { chatType: 2, peerUid: '456' } }]]);
  const calls = [];
  const d = fixture({ Msg: { getMsgEmojiLikesList(...args) {
    calls.push(args);
    return { errCode: 0, errMsg: '', emojiLikesList: [{ tinyId: 't1', nickName: 'A', headUrl: 'h' }], cookie: 'c2', isLastPage: false, isFirstPage: true };
  } } }, { messageRefs: refs });
  const out = await d.execute('get_msg_reaction', { message_id: '9007199254740993', emoji_id: '128077', count: 5 });
  // emoji_id longer than 3 characters is a server-side emoji (type '2').
  assert.deepEqual(calls[0], [{ chatType: 2, peerUid: '456', guildId: '' }, '77', '128077', '2', '', false, 5]);
  assert.equal(out.users[0].tiny_id, 't1');
  assert.equal(out.is_last_page, false);
  assert.equal(out.is_first_page, true);
});

test('reaction list refuses unknown messages and native failures', async () => {
  const empty = fixture({ Msg: { getMsgEmojiLikesList: () => ({ errCode: 0, emojiLikesList: [] }) } }, { messageRefs: new Map() });
  await rejects(empty.execute('message.reactions', { message_id: '1' }), 'NOT_FOUND');
  const refs = new Map([['1', { msgId: '1', msgSeq: '2', peer: { chatType: 9, peerUid: 'x' } }]]);
  await rejects(fixture({ Msg: { getMsgEmojiLikesList: () => ({}) } }, { messageRefs: refs }).execute('message.reactions', { message_id: '1' }), 'UNSUPPORTED');
  const groupRefs = new Map([['1', { msgId: '1', msgSeq: '2', peer: { chatType: 2, peerUid: '456' } }]]);
  await rejects(fixture({ Msg: { getMsgEmojiLikesList: () => ({ errCode: 12, errMsg: 'nope' }) } }, { messageRefs: groupRefs }).execute('message.reactions', { message_id: '1' }), 'SERVICE_ERROR');
  await rejects(fixture({ Msg: { getMsgEmojiLikesList: () => ({ errCode: 0 }) } }, { messageRefs: groupRefs }).execute('message.reactions', { message_id: '1' }), 'INVALID_RESULT');
});

test('recent contacts use native order, typed result and skip non-C2C chats', async () => {
  const calls = [];
  const d = fixture({ RecentContact: { getRecentContactListSnapShot(count) {
    calls.push(count);
    return { result: 0, info: { errCode: 0, errMsg: '', sortedContactList: ['u_a', '456'],
      changedList: [
        { peerUid: '456', peerUin: '456', chatType: 2, msgId: '11', msgTime: '1700000000', sendNickName: 'Group' },
        { peerUid: 'u_a', peerUin: '123', chatType: 1, msgId: '12', msgTime: '1700000100', peerName: 'Alice', remark: 'A' },
        { peerUid: 'u_guild', chatType: 4, msgId: '13', msgTime: '1700000200' },
      ] } };
  } } });
  const rows = await d.execute('recent.list', { count: 5 });
  assert.equal(calls[0], 5);
  assert.deepEqual(rows.map(r => r.peer_uid), ['u_a', '456']);
  assert.equal(rows[0].chat_type, 'private');
  assert.equal(rows[0].last_message_id, '12');
  assert.equal(rows[0].last_time, 1700000100);
  assert.equal(rows[1].chat_type, 'group');
});

test('recent contacts reject failure and malformed snapshots instead of returning empty', async () => {
  const make = info => fixture({ RecentContact: { getRecentContactListSnapShot: () => info } });
  await rejects(make({ result: 0, info: { errCode: 3, errMsg: 'bad' } }).execute('recent.list', {}), 'SERVICE_ERROR');
  await rejects(make({ result: 0 }).execute('recent.list', {}), 'INVALID_RESULT');
  await rejects(make({ result: 0, info: { errCode: 0, sortedContactList: [] } }).execute('recent.list', {}), 'INVALID_RESULT');
  await rejects(make({ result: -1 }).execute('recent.list', {}), 'SERVICE_ERROR');
});

test('media download confirms only the matching element and reports its path', async () => {
  let listener;
  const calls = [];
  const svc = {
    addKernelMsgListener(l) { listener = l; return 7; },
    removeKernelMsgListener(handle) { calls.push(['remove', handle]); },
    downloadRichMedia(arg) { calls.push(['download', arg]); },
  };
  const d = fixture({ Msg: svc }, { timeoutMs: 500 });
  const pending = d.execute('get_image', {
    message_id: '9007199254740993', element_id: 'elem-1', chat: { type: 'group', id: 456 }, file_path: '/tmp/out.png',
  });
  await new Promise(r => setImmediate(r));
  listener.onRichMediaDownloadComplete({ msgId: '1', msgElementId: 'other', filePath: '/wrong' });
  listener.onRichMediaDownloadComplete({ msgId: '9007199254740993', msgElementId: 'elem-1', fileErrCode: 0, filePath: '/tmp/out.png', totalSize: 12 });
  assert.deepEqual(await pending, { file_path: '/tmp/out.png', file_size: 12 });
  assert.deepEqual(calls[0][1], {
    fileModelId: '', downSourceType: 0, triggerType: 0, msgId: '9007199254740993', chatType: 2,
    peerUid: '456', elementId: 'elem-1', thumbSize: 0, downloadType: 1, filePath: '/tmp/out.png', downloadSourceType: 0,
  });
  assert.deepEqual(calls[1], ['remove', 7]);
});

test('media download surfaces native errors and never invents a path', async () => {
  let listener;
  const make = () => fixture({ Msg: {
    addKernelMsgListener(l) { listener = l; return 1; }, removeKernelMsgListener() {}, downloadRichMedia() {},
  } }, { timeoutMs: 400 });
  const failed = make();
  const p1 = failed.execute('get_record', { message_id: '5', element_id: 'e', chat: { type: 'group', id: 1 }, file_path: '/x' });
  await new Promise(r => setImmediate(r));
  listener.onRichMediaDownloadComplete({ msgId: '5', msgElementId: 'e', fileErrCode: 3, fileErrMsg: 'gone' });
  await rejects(p1, 'SERVICE_ERROR');
  const missing = make();
  const p2 = missing.execute('get_record', { message_id: '5', element_id: 'e', chat: { type: 'group', id: 1 }, file_path: '/x' });
  await new Promise(r => setImmediate(r));
  listener.onRichMediaDownloadComplete({ msgId: '5', msgElementId: 'e', fileErrCode: 0 });
  await rejects(p2, 'INVALID_RESULT');
  const timeout = make();
  await rejects(timeout.execute('get_record', { message_id: '5', element_id: 'e', chat: { type: 'group', id: 1 }, file_path: '/x' }), 'TIMEOUT');
});

test('media download validates required identifiers before native calls', async () => {
  let called = false;
  const d = fixture({ Msg: { addKernelMsgListener() { called = true; }, removeKernelMsgListener() {}, downloadRichMedia() { called = true; } } });
  for (const p of [{}, { message_id: '5' }, { message_id: '5', element_id: 'e' },
    { message_id: '5', element_id: 'e', chat: { type: 'guild', id: 1 } }]) {
    await rejects(d.execute('media.download', p), 'INVALID_PARAMS');
  }
  assert.equal(called, false);
});

test('friend.like reports real successful counts and never fakes zero', async () => {
  const calls = [];
  const d = fixture({
    ProfileLike: { setBuddyProfileLike(arg) { calls.push(arg); return { result: 0, succCounts: 3 }; } },
    Profile: { getUidByUin() { return new Map([['123', U]]); } },
  });
  assert.deepEqual(await d.execute('send_like', { user_id: 123, times: 3 }), { succ_counts: 3 });
  assert.deepEqual(calls[0], { friendUid: U, sourceId: 71, doLikeCount: 3, doLikeTollCount: 0 });
  const zero = fixture({ ProfileLike: { setBuddyProfileLike: () => ({ result: 0, succCounts: 0 }) },
    Profile: { getUidByUin: () => new Map([['123', U]]) } });
  await rejects(zero.execute('send_like', { user_id: 123 }), 'SERVICE_ERROR');
  const failed = fixture({ ProfileLike: { setBuddyProfileLike: () => ({ result: -1, errMsg: 'no' }) },
    Profile: { getUidByUin: () => new Map([['123', U]]) } });
  await rejects(failed.execute('send_like', { user_id: 123 }), 'SERVICE_ERROR');
});

test('clean cache requires explicit keys and a typed result', async () => {
  const calls = [];
  const d = fixture({ StorageClean: { clearCacheDataByKeys(keys) { calls.push(keys); return { result: 0 }; } } });
  assert.deepEqual(await d.execute('clean_cache', { keys: ['tmp', 'hotUpdate'] }), { keys: ['tmp', 'hotUpdate'], result: 0 });
  assert.deepEqual(calls[0], ['tmp', 'hotUpdate']);
  for (const keys of [[], undefined, [''], 'tmp', [1]]) {
    await rejects(d.execute('clean_cache', { keys }), 'INVALID_PARAMS');
  }
  assert.equal(calls.length, 1);
  await rejects(fixture({ StorageClean: { clearCacheDataByKeys: () => ({ result: 5 }) } }).execute('clean_cache', { keys: ['tmp'] }), 'SERVICE_ERROR');
});

test('forward retrieval returns standard node array and rejects empty results', async () => {
  const textElement = { elementType: 1, textElement: { content: 'inside' } };
  const calls = [];
  const d = fixture({
    Msg: { getMultiMsg(peer, root, current) { calls.push([peer, root, current]); return { result: 0, msgList: [{
      chatType: 2, peerUid: '456', peerUin: '456', senderUid: 'u_src', senderUin: '789',
      msgId: '9007199254740993', msgSeq: '1', msgTime: '1700000000', elements: [textElement],
    }] }; } },
  }, { codec: require('../src/qsick/headless_codec.cjs') });
  const out = await d.execute('get_forward_msg', { id: 'resid_abc123456', chat: { type: 'group', id: 456 } });
  assert.equal(out.message.length, 1);
  assert.equal(out.message[0].type, 'node');
  assert.equal(out.message[0].data.user_id, 789);
  assert.equal(out.message[0].data.nickname, '');
  assert.equal(out.message[0].data.content[0].type, 'text');
  assert.deepEqual(calls[0][0], { chatType: 2, peerUid: '456', guildId: '' });
  // Both ids equal for a top-level forward.
  assert.equal(calls[0][1], 'resid_abc123456');
  assert.equal(calls[0][2], 'resid_abc123456');
  const empty = fixture({ Msg: { getMultiMsg: () => ({ result: 0, msgList: [] }) } },
    { codec: require('../src/qsick/headless_codec.cjs') });
  await rejects(empty.execute('get_forward_msg', { id: 'resid_abc123456', chat: { type: 'group', id: 456 } }), 'NOT_FOUND');
  const ackOnly = fixture({ Msg: { getMultiMsg: () => ({ result: 0 }) } },
    { codec: require('../src/qsick/headless_codec.cjs') });
  await rejects(ackOnly.execute('get_forward_msg', { id: 'resid_abc123456', chat: { type: 'group', id: 456 } }), 'INVALID_RESULT');
});

test('card/admin void setters require exact forced member readback and invoke writer once', async () => {
  let row = member();
  const writes = [];
  const reads = [];
  const svc = {
    getAllMemberList(groupId, force) { reads.push([groupId, force]); return completeMembers([row]); },
    modifyMemberCardName(...args) { writes.push(args); row = { ...row, cardName: args[2] }; },
    modifyMemberRole(...args) { writes.push(args); row = { ...row, role: args[2] }; },
  };
  const d = fixture({ Group: svc });
  assert.deepEqual(await d.execute('set_group_card', { group_id: 456, user_id: 123, card: 'New Card' }), {});
  assert.deepEqual(await d.execute('set_group_admin', { group_id: 456, user_id: 123, enable: false }), {});
  assert.deepEqual(await d.execute('group.admin', { group_id: 456, user_id: 123, enable: true }), {});
  assert.deepEqual(writes, [['456', U, 'New Card'], ['456', U, 2], ['456', U, 3]]);
  assert.equal(reads.length, 6); assert.ok(reads.every(r => r[0] === '456' && r[1] === true));
  assert.equal(d.capabilities().operations['group.card'].supported, true);
  assert.match(d.capabilities().operations['group.admin'].condition, /readback/);
});

test('already matching card/admin states succeed as verified no-ops without writing', async () => {
  let writes = 0;
  const d = fixture({ Group: { getAllMemberList: () => completeMembers([member()]), modifyMemberRole() { writes++; }, modifyMemberCardName() { writes++; } } });
  await d.execute('group.card', { group_id: 456, user_id: 123, card: 'Card' });
  await d.execute('group.admin', { group_id: 456, user_id: 123, enable: true });
  assert.equal(writes, 0);
});

test('state confirmation retries only reads until a matching state is observed', async () => {
  let writes = 0, reads = 0;
  const d = fixture({ Group: {
    modifyMemberCardName() { writes++; },
    getAllMemberList() { reads++; return completeMembers([{ ...member(), cardName: reads >= 3 ? 'Confirmed' : 'Card' }]); },
  } }, { timeoutMs: 200 });
  assert.deepEqual(await d.execute('group.card', { group_id: 456, user_id: 123, card: 'Confirmed' }), {});
  assert.equal(writes, 1); assert.equal(reads, 3);
});

test('unconfirmed void writes time out and never retry the writer', async () => {
  let writes = 0;
  const d = fixture({ Group: { getAllMemberList: () => completeMembers([member()]), modifyMemberCardName() { writes++; } } }, { timeoutMs: 8 });
  await rejects(d.execute('group.card', { group_id: 456, user_id: 123, card: 'Not Applied' }), 'TIMEOUT');
  assert.equal(writes, 1);
});

test('missing target or unsafe pre-read blocks mutation rather than guessing UID/role', async () => {
  let writes = 0;
  const writer = () => { writes++; };
  for (const [response, code, op, params] of [
    [completeMembers([]), 'NOT_FOUND', 'group.card', { card: 'New' }],
    [completeMembers([{ ...member(), cardName: undefined }]), 'INVALID_RESULT', 'group.card', { card: '' }],
    [completeMembers([{ ...member(), role: 1 }]), 'INVALID_RESULT', 'group.admin', { enable: false }],
    [completeMembers([{ ...member(), role: 4 }]), 'UNSUPPORTED', 'group.admin', { enable: false }],
    [{ errCode: 0, result: { finish: false, ids: [], infos: new Map() } }, 'INVALID_RESULT', 'group.card', { card: 'New' }],
  ]) await rejects(fixture({ Group: { getAllMemberList: () => response, modifyMemberCardName: writer, modifyMemberRole: writer } }).execute(op, { group_id: 456, user_id: 123, ...params }), code);
  assert.equal(writes, 0);
});

test('failed explicit writer result and mismatched readback never become success', async () => {
  for (const ack of [{ result: -1 }, { result: '3' }]) {
    let reads = 0;
    const d = fixture({ Group: { getAllMemberList() { reads++; return completeMembers([member()]); }, modifyMemberCardName: () => ack } });
    await rejects(d.execute('group.card', { group_id: 456, user_id: 123, card: 'New' }), 'SERVICE_ERROR');
    assert.equal(reads, 1);
  }
  let reads = 0;
  const changedUid = fixture({ Group: { getAllMemberList() { return completeMembers([reads++ ? { ...member('123', 'u_other'), cardName: 'New' } : member()]); }, modifyMemberCardName() {} } });
  await rejects(changedUid.execute('group.card', { group_id: 456, user_id: 123, card: 'New' }), 'INVALID_RESULT');
});

test('late pre-read completion cannot issue a mutation after action timeout', async () => {
  let finish, writes = 0;
  const d = fixture({ Group: { getAllMemberList: () => new Promise(resolve => { finish = resolve; }), modifyMemberCardName() { writes++; } } }, { timeoutMs: 8 });
  await rejects(d.execute('group.card', { group_id: 456, user_id: 123, card: 'New' }), 'TIMEOUT');
  finish(completeMembers([member()]));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(writes, 0);
});

test('session replacement during pre-read prevents any void mutation', async () => {
  let finish, writes = 0;
  let current = { getGroupService: () => ({ getAllMemberList: () => new Promise(resolve => { finish = resolve; }), modifyMemberCardName() { writes++; } }) };
  const d = fixture({}, { getSession: () => current });
  const pending = d.execute('group.card', { group_id: 456, user_id: 123, card: 'New' });
  await new Promise(resolve => setImmediate(resolve));
  current = {};
  finish(completeMembers([member()]));
  await rejects(pending, 'NOT_READY');
  assert.equal(writes, 0);
});

test('post-write failed/incomplete reads and vanished target do not confirm success', async () => {
  for (const [post, code] of [
    [{ errCode: -1 }, 'SERVICE_ERROR'],
    [{ errCode: 0, result: { finish: false, ids: [], infos: new Map() } }, 'INVALID_RESULT'],
    [completeMembers([]), 'TIMEOUT'],
  ]) {
    let writes = 0, reads = 0;
    const d = fixture({ Group: { getAllMemberList: () => reads++ ? post : completeMembers([member()]), modifyMemberCardName() { writes++; } } }, { timeoutMs: 8 });
    await rejects(d.execute('group.card', { group_id: 456, user_id: 123, card: 'New' }), code);
    assert.equal(writes, 1);
  }
});

test('card/admin parameters reject bad input before read or write', async () => {
  let called = 0;
  const unexpected = () => { called++; };
  const d = fixture({ Group: { getAllMemberList: unexpected, modifyMemberCardName: unexpected, modifyMemberRole: unexpected } });
  for (const card of [null, 1, false, 'bad\0name']) await rejects(d.execute('group.card', { group_id: 456, user_id: 123, card }), 'INVALID_PARAMS');
  for (const enable of [null, 1, 'false']) await rejects(d.execute('group.admin', { group_id: 456, user_id: 123, enable }), 'INVALID_PARAMS');
  assert.equal(called, 0);
});

test('strict service results reject missing, malformed and nonzero numeric/string statuses', async () => {
  for (const result of [undefined, null, {}, { result: false }, { result: '' }, { result: '00' }, { result: '0.0' }, { result: ' 0' }, { result: NaN }, { result: {} }]) {
    await rejects(fixture({ Group: { setGroupShutUp: () => result } }).execute('group.whole_ban', { group_id: 456 }), 'INVALID_RESULT');
  }
  for (const code of [-1, 5, '-1', '7']) {
    const d = fixture({ Group: { setGroupShutUp: () => ({ result: code, errMsg: 'denied' }) } });
    await assert.rejects(d.execute('group.whole_ban', { group_id: 456 }), e => e.code === 'SERVICE_ERROR' && e.details.serviceCode === code && e.message === 'denied');
  }
});

test('unsafe IDs, noncanonical strings, booleans, durations and names fail before mutation', async () => {
  let called = 0;
  const native = () => { called++; return { result: 0 }; };
  const d = fixture({ Group: { setGroupShutUp: native, setMemberShutUp: native, modifyGroupName: native }, Profile: { getUidByUin: native } });
  for (const group_id of [0, -1, 1.5, NaN, Infinity, 9007199254740992, true, null, '01', ' 123', '1e3', '123x', '9007199254740992', {}, []]) {
    await rejects(d.execute('group.whole_ban', { group_id }), 'INVALID_PARAMS');
  }
  for (const enable of [0, 1, 'true', 'false', null]) await rejects(d.execute('group.whole_ban', { group_id: 456, enable }), 'INVALID_PARAMS');
  for (const duration of [-1, 2592001, 0.1, '60', false, null]) await rejects(d.execute('group.mute', { group_id: 456, user_id: 123, duration }), 'INVALID_PARAMS');
  for (const group_name of ['', ' ', 12, null, 'bad\0name']) await rejects(d.execute('group.name', { group_id: 456, group_name }), 'INVALID_PARAMS');
  assert.equal(called, 0);
});

test('recall uses cached native peer/id, preserves uint64 decimal strings, never guesses peer', async () => {
  const msgId = '18446744073709551615';
  const refs = new Map([[msgId, { msgId, peer: { chatType: 2, peerUid: '456' } }]]);
  const calls = [];
  const d = fixture({ Msg: { recallMsg(...args) { calls.push(args); return { result: 0 }; } } }, { messageRefs: refs });
  assert.deepEqual(await d.execute('delete_msg', { message_id: msgId }), {});
  assert.deepEqual(calls, [[{ chatType: 2, peerUid: '456', guildId: '' }, [msgId]]]);
  await rejects(d.execute('message.recall', { message_id: '111' }), 'NOT_FOUND');
  await rejects(d.execute('message.recall', { message_id: 9007199254740992 }), 'INVALID_PARAMS');
  await rejects(d.execute('message.recall', { message_id: '18446744073709551616' }), 'INVALID_PARAMS');
  refs.set('12', { msgId: '13', peer: { chatType: 2, peerUid: '456' } });
  await rejects(d.execute('message.recall', { message_id: '12' }), 'INVALID_RESULT');
  assert.equal(calls.length, 1);
});

test('private recall uses actual UID and status failures propagate', async () => {
  const refs = new Map([['99', { msgId: '99', peer: { chatType: 1, peerUid: U, guildId: '' } }]]);
  const d = fixture({ Msg: { recallMsg(peer) { assert.equal(peer.peerUid, U); return { result: '1' }; } } }, { messageRefs: refs });
  await rejects(d.execute('message.recall', { message_id: 99 }), 'SERVICE_ERROR');
});

test('message.send is optional injected handler, with context and no invented confirmation', async () => {
  await rejects(fixture({}).execute('message.send', {}), 'UNSUPPORTED');
  const d = fixture({}, { sendMessage: async (p, context) => { assert.deepEqual(p, { elements: [] }); assert.equal(typeof context.resolveUid, 'function'); return { message_id: '123456' }; } });
  assert.deepEqual(await d.execute('message.send', { elements: [] }), { message_id: '123456' });
  await rejects(fixture({}, { sendMessage: () => undefined }).execute('message.send'), 'INVALID_RESULT');
});

test('timeouts are bounded, invalid timeout options reject, and native errors become structured errors', async () => {
  const never = () => new Promise(() => {});
  await rejects(fixture({ Group: { setGroupShutUp: never } }, { timeoutMs: 8 }).execute('group.whole_ban', { group_id: 456 }), 'TIMEOUT');
  for (const timeoutMs of [0, -1, 120001, NaN, Infinity, '10', 1.5]) assert.throws(() => fixture({}, { timeoutMs }), e => e.code === 'INVALID_PARAMS');
  await rejects(fixture({ Group: { setGroupShutUp() { throw new Error('Native stub failure'); } } }).execute('group.whole_ban', { group_id: 456 }), 'SERVICE_ERROR');
});

test('late UID resolution cannot issue mute after timeout or session replacement', async () => {
  let resolve;
  let called = 0;
  const d = fixture({ Group: { setMemberShutUp() { called++; return { result: 0 }; } } }, {
    timeoutMs: 8, resolveUid: () => new Promise(done => { resolve = done; }),
  });
  await rejects(d.execute('group.mute', { group_id: 456, user_id: 123 }), 'TIMEOUT');
  resolve(U);
  await new Promise(done => setImmediate(done));
  assert.equal(called, 0);

  let resolveOther;
  let s = { getGroupService: () => ({ setMemberShutUp() { called++; return { result: 0 }; } }) };
  const replaced = fixture({}, { getSession: () => s, resolveUid: () => new Promise(done => { resolveOther = done; }) });
  const pending = replaced.execute('group.mute', { group_id: 456, user_id: 123 });
  await new Promise(done => setImmediate(done));
  s = {};
  resolveOther(U);
  await rejects(pending, 'NOT_READY');
  assert.equal(called, 0);
});

test('wrong-group events and never-resolving ACKs time out and detach listeners', async () => {
  const wrong = eventGroup({ getGroupDetailInfo() { this.emit('onGroupDetailInfoChange', { ...group, groupCode: '999' }); return { result: 0 }; } });
  await rejects(fixture({ Group: wrong }, { timeoutMs: 8 }).execute('group.info', { group_id: 456 }), 'TIMEOUT');
  assert.equal(wrong.listeners.size, 0);
  const noAck = eventGroup({ getGroupList() { this.emit('onGroupListUpdate', 1, []); return new Promise(() => {}); } });
  await rejects(fixture({ Group: noAck }, { timeoutMs: 8 }).execute('group.list'), 'TIMEOUT');
  assert.equal(noAck.listeners.size, 0);
});

test('at-all quotas use errCode and native field casing without invented defaults', async () => {
  const response = { errCode: '0', atInfo: { canAtAll: false, RemainAtAllCountForUin: 0, RemainAtAllCountForGroup: 12 } };
  const d = fixture({ Group: { getGroupRemainAtTimes(code) { assert.equal(code, '456'); return response; } } });
  assert.deepEqual(await d.execute('get_group_at_all_remain', { group_id: 456 }), { can_at_all: false, remain_at_all_count_for_group: 12, remain_at_all_count_for_uin: 0 });
  for (const [bad, code] of [
    [{ result: 0, atInfo: response.atInfo }, 'INVALID_RESULT'], [{ errCode: '2', atInfo: response.atInfo }, 'SERVICE_ERROR'],
    [{ errCode: 0 }, 'INVALID_RESULT'], [{ errCode: 0, atInfo: {} }, 'INVALID_RESULT'],
    [{ errCode: 0, atInfo: { ...response.atInfo, canAtAll: 1 } }, 'INVALID_RESULT'],
    [{ errCode: 0, atInfo: { ...response.atInfo, RemainAtAllCountForUin: -1 } }, 'INVALID_RESULT'],
  ]) await rejects(fixture({ Group: { getGroupRemainAtTimes: () => bad } }).execute('group.at_all_remain', { group_id: 456 }), code);
});

test('mute-list read correlates group code and preserves normalized duration fields', async () => {
  const svc = eventGroup({ getGroupShutUpMemberList(code) {
    assert.equal(code, '456');
    this.emit('onShutUpMemberListChanged', '999', []);
    this.emit('onShutUpMemberListChanged', code, [{ ...member(), shutUpTime: 4567 }]);
    return { result: '0' };
  } });
  const rows = await fixture({ Group: svc }).execute('get_group_shut_list', { group_id: 456 });
  assert.equal(rows[0].user_id, 123); assert.equal(rows[0].nickname, 'Alice');
  assert.equal(rows[0].shut_up_time, 4567); assert.equal(rows[0].shut_up_timestamp, 4567);
  assert.equal(svc.listeners.size, 0);
});

test('mute-list read never turns absent/malformed/failure events into an empty success', async () => {
  for (const [eventRows, result, code] of [
    [undefined, { result: 0 }, 'INVALID_RESULT'], [[{}], { result: 0 }, 'INVALID_RESULT'],
    [[], { result: -1 }, 'SERVICE_ERROR'], [[], {}, 'INVALID_RESULT'],
  ]) {
    const svc = eventGroup({ getGroupShutUpMemberList(groupId) { this.emit('onShutUpMemberListChanged', groupId, eventRows); return result; } });
    await rejects(fixture({ Group: svc }).execute('group.shut_list', { group_id: 456 }), code);
    assert.equal(svc.listeners.size, 0);
  }
  const svc = eventGroup({ getGroupShutUpMemberList() { this.emit('onShutUpMemberListChanged', '999', []); return { result: 0 }; } });
  await rejects(fixture({ Group: svc }, { timeoutMs: 8 }).execute('group.shut_list', { group_id: 456 }), 'TIMEOUT');
  assert.equal(svc.listeners.size, 0);
  const empty = eventGroup({ getGroupShutUpMemberList(code) { this.emit('onShutUpMemberListChanged', code, []); return { result: 0 }; } });
  assert.deepEqual(await fixture({ Group: empty }).execute('group.shut_list', { group_id: 456 }), []);
});

test('friend categories preserve empty groups and exact category names/counts', async () => {
  const categories = [
    { categoryId: 0, categorySortId: 1, categroyName: 'Friends', categroyMbCount: 1, onlineCount: 1, buddyUids: [U] },
    { categoryId: 2, categorySortId: 2, categroyName: 'Empty', categroyMbCount: 0, onlineCount: 0, buddyUids: [] },
  ];
  const d = fixture({ Buddy: { hasBuddyList: () => true, getBuddyListFromCache: () => categories }, Profile: { getCoreAndBaseInfo: () => new Map([[U, profile()]]) } });
  const rows = await d.execute('get_friends_with_category');
  assert.deepEqual(rows.map(r => [r.categoryId, r.categoryName, r.categoryMbCount, r.buddyList.length]), [[0, 'Friends', 1, 1], [2, 'Empty', 0, 0]]);
  assert.equal(rows[0].buddyList[0].user_id, 123);
  assert.equal(d.capabilities().operations['friend.categories'].supported, true);
  categories[0].buddyUids = [];
  assert.equal((await d.execute('friend.categories'))[0].buddyList.length, 0);
  categories[0].categoryId = undefined;
  await rejects(d.execute('friend.categories'), 'INVALID_RESULT');
  await rejects(d.execute('friend.categories', { no_cache: true }), 'UNSUPPORTED');
});

test('user status reads verified cached SimpleInfo.status and never assumes offline', async () => {
  let status = { uid: U, uin: '123', status: 10, extStatus: 0 };
  const d = fixture({ Profile: { getUidByUin: () => new Map([['123', U]]), getCoreAndBaseInfo: () => new Map([[U, { ...profile(), status }]]) } });
  assert.deepEqual(await d.execute('nc_get_user_status', { user_id: 123 }), { status: 10, ext_status: 0 });
  assert.match(d.capabilities().operations['user.status'].condition, /cached/);
  status = null;
  await rejects(d.execute('user.status', { user_id: 123 }), 'UNSUPPORTED');
  status = { uid: U, uin: '124', status: 10, extStatus: 0 };
  await rejects(d.execute('user.status', { user_id: 123 }), 'INVALID_RESULT');
  status = { uid: U, uin: '123', status: 10 };
  await rejects(d.execute('user.status', { user_id: 123 }), 'INVALID_RESULT');
  await rejects(d.execute('user.status', { user_id: 123, no_cache: true }), 'UNSUPPORTED');
});

test('unverified read contracts are explicitly unsupported and never called', async () => {
  let called = 0;
  const unexpected = () => { called++; };
  const d = fixture({ Group: { getSingleScreenNotifies: unexpected, getGroupLatestEssenceList: unexpected, fetchGroupEssenceList: unexpected }, Guild: { getGuildList: unexpected, getGuildServiceProfile: unexpected } });
  for (const op of ['get_group_ignored_notifies', 'get_essence_msg_list', 'get_guild_list', 'get_guild_service_profile']) await rejects(d.execute(op), 'UNSUPPORTED');
  assert.equal(called, 0);
  assert.equal(d.capabilities().operations['group.ignored_notifies'].supported, false);
});

test('group admin list filters exact roles from complete members, including owner only on request', async () => {
  const rows = [member(), { ...member('124', 'u_owner'), role: 4 }, { ...member('125', 'u_member'), role: 2 }];
  const calls = [];
  const d = fixture({ Group: { getAllMemberList(...args) { calls.push(args); return completeMembers(rows); } } });
  assert.deepEqual((await d.execute('get_group_admin_list', { group_id: 456, no_cache: true })).map(m => m.user_id), [123]);
  assert.deepEqual((await d.execute('group.admins', { group_id: 456, include_owner: true })).map(m => [m.user_id, m.role]), [[123, 'admin'], [124, 'owner']]);
  assert.deepEqual(await d.execute('get_group_member_count', { group_id: 456 }), { group_id: 456, member_count: 3 });
  assert.deepEqual(calls, [['456', true], ['456', false], ['456', false]]);
  await rejects(d.execute('group.admins', { group_id: 456, include_owner: 'true' }), 'INVALID_PARAMS');
  assert.equal(calls.length, 3);
});

test('derived group lists/counts reject partial, duplicate, stranger or malformed members', async () => {
  for (const op of ['group.admins', 'group.member_count']) {
    for (const response of [
      { errCode: 0, result: { finish: false, ids: [], infos: new Map() } },
      completeMembers([member(), member('123', 'u_duplicate')]),
      completeMembers([{ ...member(), role: 1 }]),
      completeMembers([{ ...member(), role: 99 }]),
    ]) await rejects(fixture({ Group: { getAllMemberList: () => response } }).execute(op, { group_id: 456 }), 'INVALID_RESULT');
    await rejects(fixture({ Group: { getAllMemberList: () => ({ errCode: 4 }) } }).execute(op, { group_id: 456 }), 'SERVICE_ERROR');
  }
  const empty = fixture({ Group: { getAllMemberList: () => completeMembers([]) } });
  assert.deepEqual(await empty.execute('group.admins', { group_id: 456 }), []);
  assert.deepEqual(await empty.execute('group.member_count', { group_id: 456 }), { group_id: 456, member_count: 0 });
});

function factFixture(profiles, extraBuddy = {}, extra = {}) {
  return fixture({
    Buddy: { hasBuddyList: () => true, getBuddyListFromCache: () => [{ buddyUids: [...profiles.keys()] }, { buddyUids: [...profiles.keys()] }], ...extraBuddy },
    Profile: { getCoreAndBaseInfo: () => profiles },
  }, extra);
}
const relationFlags = { isBlock: false, isBlocked: true, isMsgDisturb: false, isSpecialCareOpen: true, isSpecialCareZone: false };

test('friend status and relationship lists return every unique cached buddy with exact native facts', async () => {
  const profiles = new Map([
    [U, { ...profile(), status: { uid: U, uin: '123', status: 10, extStatus: 1024 }, relationFlags }],
    ['u_bob', { ...profile('124', 'u_bob'), status: { uid: 'u_bob', uin: '124', status: 9876, extStatus: 0 }, relationFlags: { ...relationFlags, isBlocked: false } }],
  ]);
  const d = factFixture(profiles);
  const statuses = await d.execute('get_friend_status_list');
  assert.deepEqual(statuses.map(r => [r.user_id, r.status, r.ext_status]), [[123, 10, 1024], [124, 9876, 0]]);
  assert.ok(statuses.every(r => !Object.hasOwn(r, 'online')));
  const relations = await d.execute('get_friend_relation_list');
  assert.equal(relations.length, 2);
  assert.deepEqual(relations[0].relation_flags, relationFlags);
  assert.equal(relations[1].relation_flags.isBlocked, false);
  assert.match(d.capabilities().operations['friend.statuses'].condition, /complete/);
  assert.equal(await d.resolveUid(124), 'u_bob');
});

test('complete friend fact lists never silently drop missing statuses or relationship flags', async () => {
  for (const [op, field, good, bad] of [
    ['friend.statuses', 'status', { uid: U, uin: '123', status: 10, extStatus: 0 }, { uid: U, uin: '999', status: 10, extStatus: 0 }],
    ['friend.relations', 'relationFlags', relationFlags, { ...relationFlags, isBlocked: 0 }],
  ]) {
    const profiles = new Map([[U, { ...profile(), [field]: good }], ['u_bob', profile('124', 'u_bob')]]);
    await rejects(factFixture(profiles).execute(op), 'UNSUPPORTED');
    profiles.delete('u_bob');
    profiles.set(U, { ...profile(), [field]: bad });
    await rejects(factFixture(profiles).execute(op), 'INVALID_RESULT');
    await rejects(factFixture(profiles).execute(op, { no_cache: true }), 'UNSUPPORTED');
    await rejects(factFixture(profiles, { hasBuddyList: undefined }).execute(op), 'UNSUPPORTED');
    await rejects(factFixture(profiles, { hasBuddyList: () => false }).execute(op), 'NOT_READY');
    assert.deepEqual(await factFixture(new Map()).execute(op), []);
    await rejects(factFixture(new Map(), { getBuddyListFromCache: () => [{ buddyUids: [U] }] }).execute(op), 'INVALID_RESULT');
  }
});

test('friend relation list requires each documented boolean, not merely a non-null object', async () => {
  for (const key of Object.keys(relationFlags)) {
    const flags = { ...relationFlags }; delete flags[key];
    await rejects(factFixture(new Map([[U, { ...profile(), relationFlags: flags }]])).execute('friend.relations'), 'INVALID_RESULT');
  }
});

test('new complete queries reject session replacement while awaiting native data', async () => {
  for (const op of ['friend.statuses', 'group.admins', 'group.member_count']) {
    let finish;
    let s = {
      getGroupService: () => ({ getAllMemberList: () => new Promise(resolve => { finish = resolve; }) }),
      getBuddyService: () => ({ hasBuddyList: () => true, getBuddyListFromCache: () => [{ buddyUids: [U] }] }),
      getProfileService: () => ({ getCoreAndBaseInfo: () => new Promise(resolve => { finish = resolve; }) }),
    };
    const d = fixture({}, { getSession: () => s });
    const pending = d.execute(op, { group_id: 456 });
    await new Promise(resolve => setImmediate(resolve));
    s = {};
    finish(op === 'friend.statuses' ? new Map([[U, profile()]]) : completeMembers([member()]));
    await rejects(pending, 'NOT_READY');
  }
});

test('group share queries call only typed native getters and preserve exact payloads', async () => {
  const calls = [];
  const arkJson = '{ "app": "com.tencent.structmsg", "meta": {} }';
  const d = fixture({ Group: {
    getJoinGroupLink(param) { calls.push(param); return { result: '0', url: 'https://qm.qq.com/q/stub-only' }; },
    getGroupRecommendContactArkJson(code) { calls.push(code); return { result: 0, arkJson }; },
  } });
  assert.deepEqual(await d.execute('get_group_share_link', { group_id: 456 }), { url: 'https://qm.qq.com/q/stub-only' });
  await d.execute('group.share_link', { group_id: 456, need_short_url: false, src_id: 12, additional_param: 'native-value' });
  assert.deepEqual(await d.execute('get_group_share_ark', { group_id: 456 }), { ark_json: arkJson });
  assert.deepEqual(calls, [
    { groupCode: '456', srcId: 73, needShortUrl: true, additionalParam: '' },
    { groupCode: '456', srcId: 12, needShortUrl: false, additionalParam: 'native-value' }, '456',
  ]);
});

test('share getters reject ACK-only/void/failure/malformed payloads rather than claiming success', async () => {
  for (const [op, method, field, values] of [
    ['group.share_link', 'getJoinGroupLink', 'url', [undefined, null, '', ' ', 1, 'bad\0url']],
    ['group.share_ark', 'getGroupRecommendContactArkJson', 'arkJson', [undefined, null, '', '[]', 'null', '1', 'broken']],
  ]) {
    for (const value of values) await rejects(fixture({ Group: { [method]: () => ({ result: 0, [field]: value }) } }).execute(op, { group_id: 456 }), 'INVALID_RESULT');
    for (const response of [undefined, {}, { result: true }]) await rejects(fixture({ Group: { [method]: () => response } }).execute(op, { group_id: 456 }), 'INVALID_RESULT');
    await rejects(fixture({ Group: { [method]: () => ({ result: '4' }) } }).execute(op, { group_id: 456 }), 'SERVICE_ERROR');
    await rejects(fixture({ Group: { [method]: () => new Promise(() => {}) } }, { timeoutMs: 8 }).execute(op, { group_id: 456 }), 'TIMEOUT');
  }
});

test('share link parameters fail before native invocation and no arbitrary share dispatch exists', async () => {
  let calls = 0;
  const d = fixture({ Group: { getJoinGroupLink() { calls++; } } });
  for (const params of [{ need_short_url: 'true' }, { src_id: -1 }, { src_id: 1.5 }, { src_id: '73' }, { additional_param: null }, { additional_param: 'bad\0param' }]) {
    await rejects(d.execute('group.share_link', { group_id: 456, ...params }), 'INVALID_PARAMS');
  }
  await rejects(d.execute('getGroupRecommendContactArkJson', { group_id: 456 }), 'UNSUPPORTED');
  assert.equal(calls, 0);
});

test('audited unsupported permission, model, online and non-member queries never call native placeholders', async () => {
  let calls = 0;
  const unexpected = () => { calls++; throw new Error('must not call'); };
  const d = fixture({ Group: { getGroupInfoForJoinGroup: unexpected, getAllGroupPrivilegeFlag: unexpected }, Profile: { getStatus: unexpected }, OnlineStatus: { getAggregationGroupModels: unexpected } });
  for (const op of ['get_group_admin_permissions', 'get_group_admin_settings', 'get_not_joined_group_info', '_get_model_show', 'get_online_clients', 'get_online_friend_list']) {
    await rejects(d.execute(op), 'UNSUPPORTED');
    const caps = d.capabilities();
    assert.equal(caps.operations[caps.aliases[op]].supported, false);
  }
  assert.equal(calls, 0);
});

test('parent login readiness guard survives dispatcher extensions', async () => {
  await rejects(fixture({}, { getIdentity: () => ({ uin: '123', logged_in: false }) }).execute('login.info'), 'NOT_READY');
});

test('capabilities expose unavailable contracts and aliases without invoking action methods', async () => {
  const d = fixture({ Group: { setGroupShutUp() { throw new Error('must not call'); } } });
  const caps = d.capabilities();
  assert.equal(caps.operations['group.whole_ban'].supported, true);
  assert.equal(caps.operations['group.card'].supported, false);
  assert.equal(caps.operations['group.kick'].supported, false);
  assert.equal(caps.aliases.set_group_whole_ban, 'group.whole_ban');
  assert.equal(caps.operations['message.send'].supported, false);
  await rejects(d.execute('__proto__'), 'UNSUPPORTED');
  await rejects(d.execute('raw.call', {}), 'UNSUPPORTED');
  await rejects(d.execute('group.list', []), 'INVALID_PARAMS');
  await rejects(d.execute('', {}), 'INVALID_PARAMS');
  await rejects(d.execute('group.list', null), 'INVALID_PARAMS');
});

test('favourite emoji lists rows with a usable resource id', async () => {
  const calls = [];
  const svc = {
    fetchFavEmojiList(...args) {
      calls.push(args);
      return Promise.resolve({ result: 0, emojiInfoList: [
        { resId: 'R1', emoId: 3, md5: 'ABC', url: 'https://p.qpic.cn/x', emoPath: 'C:/a.png', desc: '笑', isMarkFace: false, isAPNG: true },
        { emoId: 4, md5: 'DEF' }, // no resId: cannot be sent or deleted
      ] });
    },
  };
  const d = fixture({ Msg: svc });
  const out = await d.execute('emoji.favorites', { count: 10 });
  // Native signature is (startId, count, backward, forceRefresh).
  assert.deepEqual(calls[0], ['', 10, true, true]);
  assert.equal(out.emoticons.length, 1);
  assert.equal(out.emoticons[0].res_id, 'R1');
  assert.equal(out.emoticons[0].emoji_id, '3');
  assert.equal(out.emoticons[0].desc, '笑');
  assert.equal(out.emoticons[0].is_apng, true);
});

test('favourite emoji rejects malformed or failing responses', async () => {
  await rejects(fixture({ Msg: { fetchFavEmojiList: () => Promise.resolve({ result: 0 }) } })
    .execute('emoji.favorites', {}), 'INVALID_RESULT');
  await rejects(fixture({ Msg: { fetchFavEmojiList: () => Promise.resolve({ result: 0, emojiInfoList: {} }) } })
    .execute('emoji.favorites', {}), 'INVALID_RESULT');
  await rejects(fixture({ Msg: { fetchFavEmojiList: () => Promise.resolve({ result: -1 }) } })
    .execute('emoji.favorites', {}), 'SERVICE_ERROR');
  await rejects(fixture({ Msg: { fetchFavEmojiList: () => Promise.resolve({ result: 0, emojiInfoList: [null] }) } })
    .execute('emoji.favorites', {}), 'INVALID_RESULT');
});

test('favourite emoji add requires a real file and hashes it', async () => {
  const os = require('os'), fs = require('fs'), path = require('path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qsick-fav-'));
  const file = path.join(dir, 'face.png');
  fs.writeFileSync(file, Buffer.from('not really a png'));
  try {
    const seen = [];
    const d = fixture({ Msg: { addFavEmoji: arg => { seen.push(arg); return Promise.resolve(); } } });
    const out = await d.execute('emoji.favorite.add', { file });
    assert.equal(seen.length, 1);
    // The MD5 must be computed from the bytes, never accepted on trust.
    assert.equal(out.md5, require('crypto').createHash('md5').update(fs.readFileSync(file)).digest('hex'));
    assert.equal(seen[0].emojiPath, file);
    assert.equal(seen[0].fileName, 'face.png');
    assert.equal(seen[0].isOrigin, true);
    await rejects(fixture({ Msg: { addFavEmoji: () => Promise.resolve() } })
      .execute('emoji.favorite.add', { file: path.join(dir, 'missing.png') }), 'NOT_FOUND');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('favourite emoji removal sends the resource ids verbatim', async () => {
  const seen = [];
  const d = fixture({ Msg: { deleteFavEmoji: ids => { seen.push(ids); return Promise.resolve(); } } });
  await d.execute('emoji.favorite.remove', { res_ids: ['A', 'B'] });
  assert.deepEqual(seen[0], ['A', 'B']);
  await rejects(fixture({ Msg: { deleteFavEmoji: () => Promise.resolve() } })
    .execute('emoji.favorite.remove', { res_ids: [] }), 'INVALID_PARAMS');
});
