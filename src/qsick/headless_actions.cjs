'use strict';

/* Independent adapter against LOCAL NapCatQQ-4.18.42 contracts (not its implementation):
 * napcat-core/services/{NodeIKernel{Group,Buddy,Profile,Msg}Service,common}.ts,
 * napcat-core/listeners/NodeIKernelGroupListener.ts, types/{group,user}.ts.
 * GeneralCallResult requires result=0; getAllMemberList uses errCode=0 and
 * result.infos; cache/profile-map methods return payloads, not status envelopes.
 * getGroupList/getGroupDetailInfo ACKs are NOT data: correlated events are required.
 * Void card/admin APIs require forced complete state readback; their return alone
 * never establishes success. Kick/destroy remain unsupported and are NOT called.
 * Additional query contracts (all are observations, never mutation confirmations):
 * - group.admins: normalized role=3 members, owner only with include_owner=true;
 *   group.member_count: {group_id, member_count} from the same complete member set.
 * - friend.statuses / friend.relations: one row per unique cached buddy UID; require
 *   initialized cache and ALL typed status/relation fields used below. Missing data
 *   rejects the WHOLE result, not an empty/partial list; no forced refresh available.
 *   Status codes stay numeric: no unverified mapping to online/offline booleans.
 * - group.share_link: ACK+nonempty url; group.share_ark: ACK+JSON-object arkJson.
 *   These only retrieve share content, never send it. No buddy-share alias: the
 *   local UID signature conflicts with its UIN-based OneBot caller.
 * No runtime imports, IO, network, fabricated UIDs or automatic destructive retries.
 */

class ActionError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'ActionError';
    this.code = code;
    this.details = details;
  }
  toJSON() { return { code: this.code, error: this.message, details: this.details }; }
}
const fail = (code, message, details) => { throw new ActionError(code, message, details); };
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const invalid = message => fail('INVALID_PARAMS', message);
const badResult = message => fail('INVALID_RESULT', message);

function id(value, name, wide = false) {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || value <= 0) invalid(`${name} must be a positive safe integer or decimal string`);
    value = String(value);
  }
  if (typeof value !== 'string' || !/^[1-9]\d*$/.test(value) || value.length > (wide ? 20 : 16)) {
    invalid(`${name} must be a positive canonical decimal ID`);
  }
  const max = wide ? 18446744073709551615n : BigInt(Number.MAX_SAFE_INTEGER);
  if (BigInt(value) > max) invalid(`${name} exceeds the supported exact integer range`);
  return value;
}
function uid(value) {
  if (typeof value !== 'string' || !/^u_[A-Za-z0-9_-]{1,128}$/.test(value)) badResult('Service returned an invalid UID');
  return value;
}
function bool(p, key, fallback = false) {
  const v = p[key] === undefined ? fallback : p[key];
  if (typeof v !== 'boolean') invalid(`${key} must be boolean`);
  return v;
}
function integer(v, name, min, max) {
  if (!Number.isSafeInteger(v) || v < min || v > max) invalid(`${name} must be an integer in [${min}, ${max}]`);
  return v;
}
function text(v, name, allowEmpty = true) {
  if (typeof v !== 'string' || (!allowEmpty && !v.trim()) || v.includes('\0')) invalid(`${name} must be ${allowEmpty ? 'a' : 'a non-empty'} string without NUL`);
  return v;
}
// Last path segment, for deriving a file name when the caller supplies only a path.
//
// Splits on both separators because a path may arrive in either form regardless of the platform the
// caller builds it on, and queries/trailing separators are stripped so a URL-ish path still yields a
// usable name.
function basenameOf(value) {
  const cleaned = String(value || '').replace(/[?#].*$/, '').replace(/[\\/]+$/, '');
  const parts = cleaned.split(/[\\/]/);
  return parts.length ? parts[parts.length - 1] : '';
}
// Some QQNT service calls resolve to a plain object with no integer status. Treating that as a
// failure made a working call look broken: the kick path reported "Service result is not an integer
// status" for a kick that had been issued. Callers that can confirm the effect objectively (the
// member list) read the status from here and let the effect decide.
function summarizeAck(ack) {
  if (!object(ack)) return { code: undefined, fields: [] };
  for (const field of ['result', 'errCode', 'code']) {
    if (!own(ack, field)) continue;
    const value = ack[field];
    if (value === 0 || value === '0') return { code: 0, field, fields: Object.getOwnPropertyNames(ack).slice(0, 12) };
    if (typeof value === 'number' && Number.isSafeInteger(value)) return { code: value, field, fields: Object.getOwnPropertyNames(ack).slice(0, 12) };
    if (typeof value === 'string' && /^-?(?:0|[1-9]\d*)$/.test(value)) return { code: Number(value), field, fields: Object.getOwnPropertyNames(ack).slice(0, 12) };
  }
  // No usable status: report which fields did arrive, so the next investigation starts with facts.
  return { code: undefined, fields: Object.getOwnPropertyNames(ack).slice(0, 12) };
}
function resultCode(result, field = 'result') {
  if (!object(result) || !own(result, field)) badResult(`Service response is missing ${field}`);  const code = result[field];
  if (code === 0 || code === '0') return result;
  if ((typeof code !== 'number' || !Number.isSafeInteger(code)) &&
      (typeof code !== 'string' || !/^-?(?:0|[1-9]\d*)$/.test(code))) {
    badResult(`Service ${field} is not an integer status`);
  }
  fail('SERVICE_ERROR', typeof result.errMsg === 'string' && result.errMsg ? result.errMsg : `QQNT returned ${field}=${code}`, { serviceCode: code, field });
}
function resultInteger(value, name, fallback = 0) {
  if (value === undefined) return fallback;
  if (typeof value === 'string' && /^(0|[1-9]\d*)$/.test(value)) value = Number(value);
  if (!Number.isSafeInteger(value) || value < 0) badResult(`Invalid ${name} in service data`);
  return value;
}
function resultId(v, name) {
  try { return Number(id(v, name)); }
  catch (e) { if (e instanceof ActionError) badResult(`Invalid ${name} in service data`); throw e; }
}
const sex = value => value === 1 ? 'male' : value === 2 ? 'female' : 'unknown';

const ALIASES = Object.freeze({
  get_login_info: 'login.info', get_group_list: 'group.list', get_friend_list: 'friend.list',
  get_group_info: 'group.info', get_group_member_list: 'group.members', get_group_member_info: 'group.member',
  'group.member.list': 'group.members', 'group.member.info': 'group.member',
  get_stranger_info: 'friend.info', 'user.info': 'friend.info', 'user.profile': 'friend.info', 'profile.info': 'friend.info',
  delete_msg: 'message.recall', set_group_ban: 'group.mute', set_group_whole_ban: 'group.whole_ban',
  'group.mute_all': 'group.whole_ban', set_group_kick: 'group.kick', set_group_name: 'group.name',
  set_group_card: 'group.card', set_group_admin: 'group.admin', set_group_leave: 'group.leave',
  set_group_remark: 'group.remark', set_group_special_title: 'group.special_title',
  set_friend_remark: 'friend.remark', delete_friend: 'friend.delete',
  get_friends_with_category: 'friend.categories', get_group_at_all_remain: 'group.at_all_remain',
  get_group_shut_list: 'group.shut_list', nc_get_user_status: 'user.status',
  get_group_ignored_notifies: 'group.ignored_notifies', get_essence_msg_list: 'group.essence.list',
  set_essence_msg: 'group.essence.set', delete_essence_msg: 'group.essence.remove',
  del_essence_msg: 'group.essence.remove', get_group_latest_essence: 'group.essence.latest',
  get_guild_list: 'guild.list', get_guild_service_profile: 'guild.profile',
  get_group_admin_list: 'group.admins', get_group_member_count: 'group.member_count',
  get_group_share_link: 'group.share_link', get_group_share_ark: 'group.share_ark',
  get_friend_status_list: 'friend.statuses', get_friend_relation_list: 'friend.relations',
  get_group_admin_permissions: 'group.admin_permissions', get_group_admin_settings: 'group.admin_settings',
  get_not_joined_group_info: 'group.not_joined_info', get_online_clients: 'online.clients',
  _get_model_show: 'model.show', get_online_friend_list: 'friend.online',
  send_like: 'friend.like', clean_cache: 'cache.clean', get_forward_msg: 'forward.get',
  get_image: 'media.download', get_record: 'media.download',
  get_recent_contact: 'recent.list', get_recent_contact_list: 'recent.list',
  get_msg_reaction: 'message.reactions', get_msg_emoji_likes: 'message.reactions',
  ocr_image: 'system.ocr', '.ocr_image': 'system.ocr',
  // Both forward sends map to the same kernel call; the destination contact is what differs.
  send_group_forward_msg: 'forward.send', send_private_forward_msg: 'forward.send',
  send_forward_msg: 'forward.send',
  upload_forward_msg: 'forward.send', forward_group_single_msg: 'forward.send',
  // Group files. Names follow OneBot/NapCat so existing callers keep working.
  get_group_root_files: 'group.file.list', get_group_files_by_folder: 'group.file.list',
  get_group_file_system_info: 'group.file.count', get_group_file_count: 'group.file.count',
  // Delete goes through the OIDB path, not the kernel one.
  //
  // RichMediaService.deleteGroupFile exists in the method list but is not callable on this client -- the
  // kernel variant fails with "Required Group service methods are unavailable". The OIDB variant is the
  // one NapCat uses anyway, so the alias points at it and callers get a working delete under the plain
  // OneBot name.
  get_group_file_info: 'group.file.info', delete_group_file: 'group.file.delete.oidb',
  get_group_file_url: 'group.file.download', get_group_file_download_url: 'group.file.download',
  // NapCat / OneBot name for creating a group file. It is the action the reference implementation uses,
  // so accepting it directly is the point of the op.
  upload_group_file: 'group.file.upload', upload_group_file_to_folder: 'group.file.upload',
  create_group_file_folder: 'group.folder.create', delete_group_folder: 'group.folder.delete',
  rename_group_file: 'group.file.rename.oidb', move_group_file: 'group.file.move.oidb',
  // NapCat's name for kicking off a transfer of a group file to the local client. It is the kernel
  // transGroupFile call, which the RichMedia service exposes directly.
  trans_group_file: 'group.file.trans', transfer_group_file: 'group.file.trans',
  search_group_file: 'group.file.search',
  // The dotted names the existing OneBot adapters call.
  //
  // Three adapters (get_group_root_files, get_group_files_by_folder, create_group_file_folder) pass
  // these instead of the flat names above, so without them those adapters were dead even though the
  // capability existed. Both spellings must resolve to the same implementation.
  'group.files.root': 'group.file.list',
  'group.files.folder': 'group.file.list',
  'group.files.create_folder': 'group.folder.create',
  // Folder-level names map to the folder ops that exist.
  delete_group_file_folder: 'group.folder.delete',
  rename_group_file_folder: 'group.folder.rename',
  // Qzone adapter names.
  //
  // All 29 of the existing OneBot Qzone adapters name a flat op (`get_qzone_feeds`,
  // `like_qzone`, ...) rather than one of the dotted qzone.* names. Mapping them means a caller
  // gets the measured 4001 explanation instead of "unknown op" -- the conclusion is the same
  // either way, but the reason is what makes it actionable.
  get_qzone_feeds: 'qzone.feeds', get_qzone_home_feeds: 'qzone.feeds',
  get_qzone_feed_detail: 'qzone.detail', get_qzone_msg_list: 'qzone.feeds',
  get_qzone_msg_detail: 'qzone.detail',
  get_qzone_public_feeds: 'qzone.feeds', get_qzone_public_feed_detail: 'qzone.detail',
  get_qzone_public_comments: 'qzone.comments', get_qzone_public_replies: 'qzone.replies',
  get_qzone_public_capabilities: 'qzone.capabilities',
  get_qzone_profile: 'qzone.detail', get_qzone_share_ark: 'qzone.detail',
  like_qzone: 'qzone.like', unlike_qzone: 'qzone.unlike',
  comment_qzone: 'qzone.add_comment', comment_qzone_public_feed: 'qzone.add_comment',
  reply_qzone_public_feed: 'qzone.add_comment',
  delete_qzone_msg: 'qzone.delete_comment', delete_qzone_public_comment: 'qzone.delete_comment',
  delete_qzone_public_reply: 'qzone.delete_comment',
  forward_qzone_public_feed: 'qzone.forward',
  set_qzone_public_like: 'qzone.like', set_qzone_public_comment_like: 'qzone.like',
  set_qzone_public_upvote: 'qzone.like', set_qzone_public_follow: 'qzone.detail',
  send_qzone_msg: 'qzone.forward', set_qzone_ban: 'qzone.detail',
  set_qzone_msg_right: 'qzone.detail', set_qzone_profile: 'qzone.detail',
});
const UNRESOLVED = Object.freeze({
  'group.special_title': 'No verified title-setting service signature',
  'friend.remark': 'setBuddyRemark returns void; cached profile remark readback does not establish server-confirmed completion',
  'friend.delete': 'delBuddy returns unknown; no verified completion result contract',
  'group.ignored_notifies': 'General notify pages do not verify ignored-only filtering or complete ignored-list retrieval',
  'guild.list': 'Local guild list action is an empty void stub; no verified service payload contract',
  'guild.profile': 'Local guild profile action is an empty void stub; no verified service payload contract',
  'group.admin_permissions': 'Member roles do not define granular permissions; native privilege flag meanings are not verified',
  'group.admin_settings': 'No verified native admin-settings result and flag semantics',
  'group.not_joined_info': 'getGroupInfoForJoinGroup returns unknown; joined-list absence does not provide non-member group details',
  'online.clients': 'Local OneBot action returns an empty placeholder; no verified complete native device-list contract',
  'model.show': 'Local OneBot model action returns a hardcoded placeholder, not native model data',
  'friend.online': 'UserStatus defines numeric codes but no online/offline enum; use friend.statuses for exact cached codes',
  // Cookies and credentials are not kernel calls.
  //
  // NapCat builds them by issuing an HTTPS request to a QQ endpoint with the client's own credentials
  // (`UserApi.getCookies` -> `RequestUtil.HttpsGetCookies`), then deriving bkn from skey/p_skey. That needs a
  // request layer carrying the right login headers, which this payload does not have -- so returning anything
  // here would be a guess, and a wrong bkn silently breaks every qzone/web feature that consumes it.
  'cookies': 'Cookies require a signed HTTPS call to a QQ endpoint (NapCat RequestUtil.HttpsGetCookies); no verified in-process equivalent',
  'credentials': 'Credentials are derived from those same cookies; unavailable for the same reason',
  // Group honors are in the same boat, which is not obvious from the action name.
  //
  // NapCat's `getGroupHonorInfo` (napcat-core/apis/webapi.ts) builds every list from
  // `UserApi.getCookies('qun.qq.com')` plus HTTP requests to the honor endpoint. It is not a kernel call, so
  // there is nothing to forward -- it fails for the same reason `cookies` does.
  'group.honor': 'Group honors are fetched over HTTP with qun.qq.com cookies (NapCat WebApi.getGroupHonorInfo); no kernel equivalent',
  'get_group_system_msg': 'Requires the group-notify pull chain (NapCat uses getGroupNotifiesUnreadCount plus a notifies fetch); not implemented',
  'get_profile_like': 'Profile-like reads go through NodeIKernelProfileLikeService, whose response shape has not been measured on this client',
  'media.url': 'Resolving a bare file id to a URL needs the media cache the receive path builds; pass message_id + element_id + chat to use media.download instead. Private files go through `private.file.download_url`, group files through `group.file.download` -- both are separate commands, not this one',
  // Group notices are the third member of the cookies family, despite living under group.*.
  //
  // NapCat reads them with `RequestUtil.HttpGetJson` against the qun.qq.com notice endpoint
  // (napcat-core/apis/webapi.ts `getGroupNotice`), and writes them the same way. No kernel method involved.
  '_get_group_notice': 'Group notices are fetched over HTTP with qun.qq.com cookies (NapCat WebApi.getGroupNotice); no kernel equivalent',
  '_send_group_notice': 'Setting a group notice uses the same HTTP endpoint as reading one; unavailable for the same reason',
});
const REQUIREMENTS = Object.freeze({
  'group.list': ['Group', 'getGroupList', 'addKernelGroupListener', 'removeKernelGroupListener'],
  'group.info': ['Group', 'getGroupDetailInfo', 'addKernelGroupListener', 'removeKernelGroupListener'],
  'group.members': ['Group', 'getAllMemberList'], 'group.member': ['Group', 'getAllMemberList'],
  'group.admins': ['Group', 'getAllMemberList'], 'group.member_count': ['Group', 'getAllMemberList'],
  'group.share_link': ['Group', 'getJoinGroupLink'], 'group.share_ark': ['Group', 'getGroupRecommendContactArkJson'],
  'friend.info': ['Profile', 'getUserDetailInfoByUin'],
  'group.mute': ['Group', 'setMemberShutUp'], 'group.whole_ban': ['Group', 'setGroupShutUp'],
  'group.name': ['Group', 'modifyGroupName'], 'group.remark': ['Group', 'modifyGroupRemark'],
  'group.card': ['Group', 'modifyMemberCardName', 'getAllMemberList'],
  'group.admin': ['Group', 'modifyMemberRole', 'getAllMemberList'],
  'group.kick': ['Group', 'kickMemberV2', 'getAllMemberList'],
  'group.leave': ['Group', 'quitGroupV2'], 'message.recall': ['Msg', 'recallMsg'],
  'group.at_all_remain': ['Group', 'getGroupRemainAtTimes'],
  'group.shut_list': ['Group', 'getGroupShutUpMemberList', 'addKernelGroupListener', 'removeKernelGroupListener'],
  'user.status': ['Profile', 'getCoreAndBaseInfo'],
  'friend.like': ['ProfileLike', 'setBuddyProfileLike'],
  'cache.clean': ['StorageClean', 'clearCacheDataByKeys'],
  'forward.get': ['Msg', 'getMultiMsg'],
  // Message history.
  //
  // getLatestDbMsgs is what every other read depends on -- forward.send resolves msgIds through it,
  // and the essence set/remove path needs msgSeq and msgRandom from its records. It had no op, so
  // the dispatcher could not reach it at all while the tool scripts used it constantly.
  'message.history': ['Msg', 'getLatestDbMsgs'],
  // Reading a message back by id, and paging a conversation's history.
  //
  // NapCat does both through the same kernel service: `message.get` is `getMsgsByMsgId(peer, [msgId])` after
  // resolving the peer from its msgId index, and history is `getMsgsIncludeSelf(peer, msgId, count, order)` or
  // `getAioFirstViewLatestMsgs(peer, count)` when no anchor is given (napcat-core/apis/msg.ts:23,204).
  //
  // The peer lookup for `message.get` comes from `messageRefs`, which already stores the native record per
  // received message -- the raw record carries `chatType` and `peerUid`, so no separate index is needed.
  'message.get': ['Msg', 'getMsgsByMsgId'],
  'history.group': ['Msg', 'getMsgsIncludeSelf', 'getAioFirstViewLatestMsgs'],
  'history.private': ['Msg', 'getMsgsIncludeSelf', 'getAioFirstViewLatestMsgs'],
  // Group files live on RichMediaService, not GroupService.
  //
  // GroupService has 243 methods and only two mention files, both about sharing a group card. The
  // real location is confirmed by measurement and by NapCat's own wrapper: the method is on
  // RichMediaService while the event is delivered through MsgService's ordinary listener, so the
  // service name here is 'RichMedia' but the listener functions come from 'Msg'.
  'group.file.list': ['RichMedia', 'Msg', 'getGroupFileList'],
  'group.file.count': ['RichMedia', 'batchGetGroupFileCount'],
  'group.file.info': ['RichMedia', 'getGroupFileInfo'],
  'group.file.delete': ['RichMedia', 'deleteGroupFile'],
  // OIDB variants, matching NapCat's packet layout byte for byte. NapCat drives delete/rename/move
  // over OIDB 0x6D6 sub-services 3/4/5 rather than through kernel services, so a caller checking
  // parity with NapCat wants these; the kernel ones above remain the convenient default.
  'group.file.delete.oidb': ['Msg', 'sendSsoCmdReqByContend'],
  'group.file.rename.oidb': ['Msg', 'sendSsoCmdReqByContend'],
  'group.file.move.oidb': ['Msg', 'sendSsoCmdReqByContend'],
  'group.file.delete.kernel': ['RichMedia', 'deleteGroupFile'],
  // Kick off a transfer of a group file to the local client.
  //
  // NapCat routes this through the kernel (GroupApi.transGroupFile -> RichMediaService.transGroupFile)
  // rather than OIDB, so there is no packet to build. It is the operation behind NapCat's
  // trans_group_file action, and it is separate from group.file.download: that one yields a URL, this
  // one asks the client to fetch the file into its own transfer pipeline.
  'group.file.trans': ['RichMedia', 'transGroupFile'],
  'group.folder.create': ['RichMedia', 'createGroupFolder'],
  'group.folder.delete': ['RichMedia', 'deleteGroupFolder'],
  'group.folder.rename': ['RichMedia', 'renameGroupFolder'],
  'group.file.rename': ['RichMedia', 'renameGroupFile'],
  'group.file.move': ['RichMedia', 'moveGroupFile'],
  'group.file.search': ['RichMedia', 'searchGroupFile'],
  // Download URL comes from OIDB 0x6d6_2, not from any service.
  //
  // That was the missing part: this project already had the OIDB transport, and the capability looked
  // absent only because no message builder existed. See headless_groupfile.cjs for the field numbers,
  // which are transcribed from Lagrange's ProtoMember attributes rather than guessed.
  'group.file.download': ['Msg', 'sendSsoCmdReqByContend'],
  // Private (C2C) file download URL. `0xE37_800` per report 15; its request layout is not
  // reverse-engineered, so this op probes and reports rather than pretending to answer.
  // A generic OIDB call, for the operations QQ exposes only as packets.
  //
  // Several capabilities have no kernel service method at all -- setting a member's title, adding a
  // reaction, sending a nudge, approving a join request -- and the only path to them is an OIDB
  // command number plus a body. Building each one separately would mean N copies of the same
  // envelope handling; this exposes the transport and lets a caller probe or use it directly.
  //
  // It returns QQ's own status and the raw body: a caller that knows the layout can parse it, and a
  // caller that does not yet learns it from the answer. Nothing is interpreted here.
  'oidb.call': ['Msg', 'sendSsoCmdReqByContend'],
  // Upload rides on a message send: the client uploads the bytes for a FILE element itself.
  //
  // The same call serves C2C. A private file is a FILE element sent to `chatType: 1` instead of a
  // group, and the client uploads the bytes the same way -- QQ has no separate "upload to friend"
  // endpoint to call. `upload_private_file` differs only in the destination.
  'group.file.upload': ['Msg', 'sendMsg', 'addKernelMsgListener', 'removeKernelMsgListener'],
  'upload_private_file': ['Msg', 'sendMsg', 'addKernelMsgListener', 'removeKernelMsgListener'],
  // Essence messages. Set/unset go through the kernel and work; listing does not, on this account.
  //
  // Measured: addGroupEssence and removeGroupEssence both return errCode 0 "success" with the
  // parameter object {groupCode, msgId, msgSeq, msgRandom}. The listing entry points return errCode 5
  // on a 3-person group, a 59-person group and a 126-person group alike -- an account-level
  // restriction, not an empty list -- and the two-argument form never settles at all. There is no
  // cookie getter in the session either, so NapCat's HTTP fallback (qun.qq.com with cookies) is not
  // reachable from here.
  //
  // So the list op is registered but reports why it cannot answer, instead of pretending.
  'group.essence.set': ['Group', 'addGroupEssence'],
  'group.essence.remove': ['Group', 'removeGroupEssence'],
  'group.essence.list': ['Group', 'fetchGroupEssenceListV2'],
  'group.essence.latest': ['Group', 'getGroupLatestEssenceList'],
  // Qzone. Reachable, and the earlier note here said it was not.
  //
  // The old reading was "the RPC endpoint refuses every request with retcode 4001 before it looks at
  // credentials", based on identical 4001 with a valid p_skey, with only a uin, and with no cookie. The
  // control was sound but measured the wrong thing: those requests carried no `x-oidb` header, and the
  // gateway is addressed by it. Without it there is no command to authorize, so 4001 answered "which
  // command is this?".
  //
  // Re-measured with the header: retcode 0, and both GetFriendFeeds (0x97e5/0) and GetShuoshuoList
  // (0x9a97/1) return real data. So these ops stay registered and unimplemented, not blocked -- see
  // the qzone.* handler below and docs/qzone-findings.json.
  'qzone.capabilities': ['__http__', 'qzone'],
  'qzone.feeds': ['__http__', 'qzone'],
  'qzone.detail': ['__http__', 'qzone'],
  'qzone.comments': ['__http__', 'qzone'],
  'qzone.replies': ['__http__', 'qzone'],
  'qzone.like': ['__http__', 'qzone'],
  'qzone.unlike': ['__http__', 'qzone'],
  'qzone.add_comment': ['__http__', 'qzone'],
  'qzone.delete_comment': ['__http__', 'qzone'],
  'qzone.forward': ['__http__', 'qzone'],
  // Merged forward, sent as a snapshot.
  //
  // The entry point is multiForwardMsgWithComment, not multiForwardMsg. That is measured, not
  // inferred: report 12/13 intercepted 42 real forward calls and found 21 to
  // multiForwardMsgWithComment and zero to multiForwardMsg, despite the latter having a complete
  // implementation. Calling the wrong one is why every argument shape I tried returned
  // {result: 5, errMsg: ''} -- a constant error that never varied with the arguments, because it
  // was never looking at them.
  'forward.send': ['Msg', 'multiForwardMsgWithComment'],
  'media.download': ['Msg', 'downloadRichMedia', 'addKernelMsgListener', 'removeKernelMsgListener'],
  'recent.list': ['RecentContact', 'getRecentContactListSnapShot'],
  'message.reactions': ['Msg', 'getMsgEmojiLikesList'],
  'system.ocr': ['NodeMisc', 'wantWinScreenOCR'],
  // Qzone credentials: a p_skey for qzone.qq.com plus the g_tk derived from it. The HTTP itself is done in
  // Python -- this op exists only because the ticket lives behind a kernel service inside the session.
  'qzone.credentials': ['TipOff', 'getPskey'],
  'system.rkey': ['Msg', 'sendSsoCmdReqByContend'],
  'system.oidb_probe': ['Msg', 'sendSsoCmdReqByContend'],
  'emoji.favorites': ['Msg', 'fetchFavEmojiList'],
  'emoji.favorite.add': ['Msg', 'addFavEmoji'],
  'emoji.favorite.remove': ['Msg', 'deleteFavEmoji'],
});

function createActionDispatcher(options = {}) {
  if (typeof options.getSession !== 'function' || typeof options.getIdentity !== 'function') {
    throw new TypeError('getSession and getIdentity functions are required');
  }
  const timeoutMs = options.timeoutMs === undefined ? 5000 : integer(options.timeoutMs, 'timeoutMs', 1, 120000);
  const uidByUin = new Map();
  let cachedSession;
  // Session identity changes invalidate all facts: never reuse mappings across accounts.
  function session() {
    const s = options.getSession();
    if (!s || typeof s !== 'object' || typeof s.then === 'function') fail('NOT_READY', 'QQNT session is not available');
    if (s !== cachedSession) { uidByUin.clear(); cachedSession = s; }
    return s;
  }
  function service(s, type, methods = []) {
    const getter = s[`get${type}Service`];
    const svc = typeof getter === 'function' ? getter.call(s) : null;
    if (!svc || methods.some(m => typeof svc[m] !== 'function')) {
      fail('UNSUPPORTED', `Required ${type} service methods are unavailable`, { service: type, methods });
    }
    return svc;
  }
  function remember(uin, userUid) {
    if (uin === undefined || userUid === undefined) return;
    const key = id(uin, 'uin');
    // A profile that carries no uid is not an error.
    //
    // This is a cache of "uin -> uid facts", so a record without a usable uid simply contributes no fact.
    // Throwing here instead made `get_stranger_info` fail outright on this client: its
    // `getUserDetailInfoByUin` returns `detail.uid` but leaves `detail.simpleInfo.coreInfo.uid` empty, and the
    // caller only needs the uin, nickname, sex and age. Measured: the whole action answered
    // "Service returned an invalid UID" for a perfectly ordinary QQ number.
    const raw = typeof userUid === 'string' ? userUid.trim() : '';
    if (!/^u_[A-Za-z0-9_-]{1,128}$/.test(raw)) return;
    const fact = uid(raw);
    if (uidByUin.has(key) && uidByUin.get(key) !== fact) badResult('Conflicting cached UID facts');
    uidByUin.set(key, fact);
  }
  async function bounded(fn, op) {
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(fn),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new ActionError('TIMEOUT', `${op} timed out; completion is unknown and must not be blindly retried`, { op, timeoutMs })), timeoutMs); }),
      ]);
    } finally { clearTimeout(timer); }
  }
  async function resolveUid(userId) {
    const uin = id(userId, 'user_id');
    const s = session();
    if (uidByUin.has(uin)) return uidByUin.get(uin);
    if (typeof options.resolveUid === 'function') {
      // Injectors must return verified cached facts only; no synthetic u_<uin> mapping.
      const resolved = await bounded(() => options.resolveUid(uin), 'resolveUid');
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during UID resolution');
      if (resolved !== undefined && resolved !== null) { remember(uin, resolved); return uidByUin.get(uin); }
    }
    const profile = service(s, 'Profile', ['getUidByUin']);
    const facts = profile.getUidByUin('qsick', [uin]); // synchronous cache-map contract
    if (!(facts instanceof Map)) badResult('getUidByUin must return a Map');
    if (!facts.has(uin) || !facts.get(uin)) fail('UNSUPPORTED', 'No verified cached UID mapping for user', { user_id: uin });
    remember(uin, facts.get(uin));
    return uidByUin.get(uin);
  }
  function normalizeGroup(g) {
    if (!object(g) || typeof g.groupName !== 'string') badResult('Malformed group record');
    return { group_id: resultId(g.groupCode, 'groupCode'), group_name: g.groupName,
      member_count: resultInteger(g.memberCount ?? g.memberNum, 'member_count'),
      max_member_count: resultInteger(g.maxMember ?? g.maxMemberNum, 'max_member_count') };
  }
  function normalizeUser(info, expectedUid) {
    if (!object(info) || !object(info.coreInfo) || !object(info.baseInfo)) badResult('Malformed profile record');
    const c = info.coreInfo;
    const userId = resultId(c.uin, 'uin');
    if (expectedUid && c.uid && c.uid !== expectedUid) badResult('Profile UID does not match requested UID');
    // `coreInfo.uid` is empty on some responses (see `remember`); the caller's `expectedUid` is the same fact
    // when it has one, so prefer whichever is present rather than reporting an empty uid.
    const effectiveUid = (typeof c.uid === 'string' && c.uid) || (typeof expectedUid === 'string' ? expectedUid : '');
    remember(c.uin, effectiveUid);
    return { user_id: userId, uin: userId, uid: effectiveUid, nickname: typeof c.nick === 'string' ? c.nick : '',
      remark: typeof c.remark === 'string' ? c.remark : '', sex: sex(info.baseInfo.sex), age: resultInteger(info.baseInfo.age, 'age') };
  }
  function normalizeMember(m, groupId) {
    if (!object(m) || typeof m.nick !== 'string' || ![1, 2, 3, 4].includes(m.role)) badResult('Malformed member record');
    const userId = resultId(m.uin, 'uin');
    // Member UID/UIN data is returned to callers, not used as a global buddy/profile fact.
    return { group_id: Number(groupId), user_id: userId, uin: userId, uid: uid(m.uid), nickname: m.nick,
      card: typeof m.cardName === 'string' ? m.cardName : '', role: m.role === 4 ? 'owner' : m.role === 3 ? 'admin' : 'member',
      sex: sex(m.sex), age: resultInteger(m.age, 'age'),
      join_time: resultInteger(m.joinTime, 'joinTime'), last_sent_time: resultInteger(m.lastSpeakTime, 'lastSpeakTime'),
      level: String(resultInteger(m.memberRealLevel, 'memberRealLevel')), title: m.memberSpecialTitle ?? '',
      shut_up_timestamp: resultInteger(m.shutUpTime, 'shutUpTime') };
  }
  function normalizeStatus(info, user) {
    const status = info.status;
    if (status === null || status === undefined) fail('UNSUPPORTED', 'No cached status exists for this user');
    if (!object(status) || status.uid !== user.uid || String(status.uin) !== String(user.uin) || status.status === undefined || status.extStatus === undefined) badResult('Status identity or fields are invalid');
    return { status: resultInteger(status.status, 'status'), ext_status: resultInteger(status.extStatus, 'extStatus') };
  }
  function normalizeRelations(info) {
    const flags = info.relationFlags;
    if (flags === null || flags === undefined) fail('UNSUPPORTED', 'No cached relationship flags exist for this friend');
    if (!object(flags)) badResult('Relationship flags must be an object');
    const out = {};
    // Preserve exact typed native names, especially isBlock versus isBlocked;
    // neither is relabeled as a verified server-side mutual-friend relation.
    for (const key of ['isBlock', 'isBlocked', 'isMsgDisturb', 'isSpecialCareOpen', 'isSpecialCareZone']) {
      if (typeof flags[key] !== 'boolean') badResult(`Missing or invalid relationship flag ${key}`);
      out[key] = flags[key];
    }
    return out;
  }
  async function groupEvent(svc, method, args, eventName, select) {
    // Register first so synchronous/early callbacks cannot be lost. ACK success and
    // a matching event are BOTH required. Mutating list deltas are not full lists.
    let handle, timer, accept, rejectEvent;
    let closed = false;
    const event = new Promise((resolve, reject) => { accept = resolve; rejectEvent = reject; });
    const listener = { [eventName]: (...eventArgs) => {
      if (closed) return;
      try { const match = select(...eventArgs); if (match !== undefined) accept(match); }
      catch (error) { rejectEvent(error); }
    } };
    try {
      handle = svc.addKernelGroupListener(listener);
      // Listener handle validation.
      //
      // The kernel returns this handle as a decimal *string* (measured: "12"), not a number.
      // Requiring Number.isSafeInteger here rejected every listener registration -- which is why
      // group.list, group.members and every other event-delivered action failed through the
      // dispatcher while the tool scripts, which only checked for non-null, worked fine.
      //
      // A number is still accepted in case a client build returns one. What is rejected is anything
      // that is not a usable token: null, undefined, an empty string, or a non-numeric value.
      const handleText = (typeof handle === 'number') ? String(handle)
        : (typeof handle === 'string' ? handle.trim() : '');
      if (!(handleText !== '' && /^\d+$/.test(handleText))) {
        badResult(`Group listener registration did not return a valid handle (got ${JSON.stringify(handle)})`);
      }
      const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(new ActionError('TIMEOUT', `No complete ${eventName} response before timeout`, { timeoutMs })), timeoutMs); });
      const ack = Promise.resolve().then(() => svc[method](...args)).then(resultCode);
      const [, payload] = await Promise.race([Promise.all([ack, event]), deadline]);
      return payload;
    } finally {
      closed = true;
      clearTimeout(timer);
      if (handle !== undefined && handle !== null && handle !== '') svc.removeKernelGroupListener(handle);
    }
  }
  // Group-file calls: method on RichMediaService, result through MsgService's listener.
  //
  // The split is not guesswork. Measured on a live client: RichMediaService has no listener methods
  // at all, and getGroupFileList returns a plain number (a request id) rather than data. NapCat's
  // own wrapper shows the intended pairing --
  //
  //     callNormalEventV2('NodeIKernelRichMediaService/getGroupFileList',
  //                       'NodeKernelMsgListener/onGroupFileInfoUpdate', [GroupCode, params], ...)
  //
  // -- and a direct test reproduced it: registering on onGroupFileInfoUpdate and calling with
  // {folderId:'', startIndex:0} yielded {retCode:0, item:[], allFileCount:0}.
  //
  // So this registers on MsgService, calls the RichMedia method, and waits for the event.
  async function richMediaEvent(rm, msg, method, args, eventName, select) {
    let handle, timer, accept, rejectEvent;
    let closed = false;
    const event = new Promise((resolve, reject) => { accept = resolve; rejectEvent = reject; });
    const listener = { [eventName]: (...eventArgs) => {
      if (closed) return;
      try { const match = select(...eventArgs); if (match !== undefined) accept(match); }
      catch (error) { rejectEvent(error); }
    } };
    try {
      handle = msg.addKernelMsgListener(listener);
      const handleText = (typeof handle === 'number') ? String(handle)
        : (typeof handle === 'string' ? handle.trim() : '');
      if (!(handleText !== '' && /^\d+$/.test(handleText))) {
        badResult(`Message listener registration did not return a valid handle (got ${JSON.stringify(handle)})`);
      }
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new ActionError('TIMEOUT',
          `No complete ${eventName} response before timeout`, { timeoutMs })), timeoutMs);
      });
      // The method's return value is a request id, not an ack with a code, so it is not checked for
      // success -- the event carries retCode and that is what decides.
      const called = Promise.resolve().then(() => rm[method](...args));
      const [, payload] = await Promise.race([Promise.all([called, event]), deadline]);
      return payload;
    } finally {
      closed = true;
      clearTimeout(timer);
      if (handle !== undefined && handle !== null && handle !== '') msg.removeKernelMsgListener(handle);
    }
  }
  // Numbers often arrive as decimal strings ("0", "1794938767"). Returning a string where a number is
  // documented would leak into JSON output and break comparisons, so convert when possible and keep
  // undefined when the field is absent -- 0 and "missing" are different facts.
  function numberOrUndefined(value) {
    if (value === undefined || value === null || value === '') return undefined;
    const n = Number(value);
    return Number.isFinite(n) ? n : undefined;
  }
  // Normalises one native file entry.
  //
  // Measured shape: an entry is not flat. It is
  //
  //   { peerId, type, folderInfo: {...} | null, fileInfo: {...} | null }
  //
  // with the meaningful fields one level down. Reading item.fileName directly -- which is what an
  // earlier version did -- yields empty strings for every field while the listing itself looks
  // successful, so a folder can be created and then appear to not exist.
  //
  // Unknown fields are kept under `raw`: busId and the folder id are needed by delete/move/rename,
  // and a later client build may add fields this mapping does not know about.
  function normalizeFileEntry(item, groupId) {
    if (!object(item)) return null;
    const folder = object(item.folderInfo) ? item.folderInfo : null;
    const file = object(item.fileInfo) ? item.fileInfo : null;
    const body = folder || file;
    if (!body) return null;
    const isFolder = folder !== null;
    const entry = {
      group_id: Number(groupId),
      is_folder: isFolder,
      file_id: String(isFolder ? (folder.folderId ?? '') : (file.fileId ?? '')),
      file_name: String(isFolder ? (folder.folderName ?? '') : (file.fileName ?? '')),
      parent_folder_id: String(isFolder ? (folder.parentFolderId ?? '') : (file.parentId ?? '')),
      upload_time: numberOrUndefined(isFolder ? folder.createTime : file.uploadTime),
      modify_time: numberOrUndefined(isFolder ? folder.modifyTime : file.modifyTime),
      uploader: numberOrUndefined(isFolder ? folder.createUin : file.uploaderUin),
      uploader_name: String(isFolder ? (folder.creatorName ?? '') : (file.uploaderName ?? '')),
      file_size: numberOrUndefined(isFolder ? folder.usedSpace : file.fileSize),
      download_times: isFolder ? undefined : numberOrUndefined(file.downloadTimes),
      dead_time: isFolder ? undefined : numberOrUndefined(file.deadTime),
      // Only files carry this, and delete/rename/move all require it.
      bus_id: isFolder ? undefined : numberOrUndefined(file.busId),
      // A folder reports how many files sit inside it.
      total_file_count: isFolder ? numberOrUndefined(folder.totalFileCount) : undefined,
    };
    entry.raw = item;
    return entry;
  }
  // Group member lists, cached per group.
  //
  // `getAllMemberList` is a full re-fetch of every member and takes tens of seconds on a real group --
  // measured 38 s for 59 members and 34 s for 125, and a repeat call was no faster (45 s). A bot consumer
  // asks for this on every message (permission checks, card lookups), so serving it straight from the kernel
  // made every command take tens of seconds.
  //
  // NapCat keeps exactly this cache (`GroupApi.groupMemberCache`) and only refetches when it is missing or
  // the caller passes `no_cache`; the O(1) serve is the whole reason its commands are responsive.
  //
  // Freshness: `onGroupMemberListUpdate` bumps a version token in `global.__qsickMemberVersion` (written by
  // the injected listener), and a different token invalidates the entry immediately. That is stricter than
  // NapCat, whose cache lives for the whole process regardless of membership changes.
  const memberCache = new Map();
  const memberInflight = new Map();
  // 60 s, deliberately the same as the Python layer's action cache (`runtime._ACTION_CACHE_TTL`).
  //
  // Two caches at different horizons would be worse than one: a membership change would be invisible for
  // whichever is longer. At 60 s the worst-case staleness is a minute -- tolerable for permission checks, and
  // still one real fetch per group per minute instead of one per message.
  const MEMBER_TTL_MS = 60 * 1000;
  // How long past the TTL a list may still be *served* while a refresh runs in the background.
  //
  // Without this, every 60 s one caller pays the full native fetch (measured 14-15 s on a 59- and a
  // 125-member group). Stale-while-revalidate moves that cost off the caller entirely: the only wait left is
  // the very first fetch for a group this process has never seen.
  //
  // A version change still invalidates outright, so a membership change is never served stale.
  const MEMBER_SERVE_MS = 10 * 60 * 1000;
  function memberVersion(groupId) {
    try {
      const table = global.__qsickMemberVersion;
      return table && table[groupId] ? String(table[groupId]) : '';
    } catch (_) { return ''; }
  }
  function cachedMembers(groupId, force) {
    const entry = memberCache.get(groupId);
    if (!entry) return null;
    if (force) return null;
    // A membership change invalidates immediately, whatever the age.
    if (entry.version !== memberVersion(groupId)) return null;
    if (Date.now() - entry.at > MEMBER_TTL_MS) return null;
    return entry.list;
  }
  async function fetchMembers(s, groupId, force, requireCard, requireRole) {
    // One fetch per group at a time: a burst of parallel lookups must not each pay the full cost.
    const running = memberInflight.get(groupId);
    if (running && !force) return running;
    const task = (async () => {
      const svc = service(s, 'Group', ['getAllMemberList']);
      const response = resultCode(await svc.getAllMemberList(groupId, force), 'errCode');
      if (!object(response.result) || response.result.finish !== true || !(response.result.infos instanceof Map) || !Array.isArray(response.result.ids)) badResult('Member response is incomplete or missing result.infos Map / ids');
      const { ids, infos } = response.result;
      const seen = new Set();
      for (const entry of ids) {
        if (!object(entry) || !infos.has(entry.uid) || seen.has(entry.uid)) badResult('Member IDs do not match complete member records');
        seen.add(entry.uid);
      }
      if (seen.size !== infos.size) badResult('Member ID count does not match member records');
      const seenUins = new Set();
      const list = Array.from(infos, ([key, m]) => {
        if (!object(m) || m.uid !== key) badResult('Member UID does not match map key');
        const uin = resultId(m.uin, 'uin');
        if (seenUins.has(uin)) badResult('Multiple member records match the same UIN');
        seenUins.add(uin);
        if (requireCard && typeof m.cardName !== 'string') badResult('Card readback is missing cardName');
        if (requireRole && ![2, 3, 4].includes(m.role)) badResult('Role readback is not a member/admin/owner role');
        return normalizeMember(m, groupId);
      });
      memberCache.set(groupId, { at: Date.now(), version: memberVersion(groupId), list });
      return list;
    })();
    memberInflight.set(groupId, task);
    try { return await task; }
    finally {
      // Only clear our own entry: a forced refresh may have started a second one meanwhile.
      if (memberInflight.get(groupId) === task) memberInflight.delete(groupId);
    }
  }
  async function members(s, groupId, force, requireCard = false, requireRole = false) {
    if (!force) {
      const hit = cachedMembers(groupId, false);
      if (hit) return hit;
      const entry = memberCache.get(groupId);
      // Stale but usable: serve it and refresh in the background, so the caller never pays the fetch.
      // Errors are swallowed on purpose -- the refresh is an optimisation, and its failure must not surface as
      // a failure of a call that was already answered.
      if (entry && entry.version === memberVersion(groupId) && Date.now() - entry.at <= MEMBER_SERVE_MS) {
        fetchMembers(s, groupId, false, false, false).catch(() => {});
        return entry.list;
      }
    }
    return fetchMembers(s, groupId, force, requireCard, requireRole);
  }
  async function confirmKick(s, svc, groupId, userId, deadline) {
    const guard = () => {
      if (Date.now() >= deadline) fail('TIMEOUT', 'group.kick confirmation deadline exceeded; completion is unknown, do not blindly retry', { op: 'group.kick' });
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during kick confirmation');
    };
    const readTarget = async () => {
      guard();
      const rows = await members(s, groupId, true);
      guard();
      return rows.filter(m => String(m.user_id) === userId);
    };
    const before = await readTarget();
    if (before.length > 1) badResult('Multiple member records match the target UIN');
    if (!before.length) fail('NOT_FOUND', 'Target is not in the complete member list; kick not issued');
    const target = before[0];
    guard();
    // kickMemberV2 is the only kick path that returns an inspectable status;
    // the legacy Promise<void> overload gives no objective signal. kickFlag and
    // optFlag meanings are undocumented, so the plain flag (0) is used and the
    // absence of "refuse forever" is stated rather than guessed.
    const ack = await svc.kickMemberV2({
      groupCode: groupId,
      kickFlag: 0,
      kickList: [{ optFlag: 0, optOperate: 0, optMemberUid: target.uid, optBytesMsg: '' }],
      kickListUids: [target.uid],
      kickMsg: ''
    });
    // Measured on this build: the ack carries no integer `result` at all -- `resultCode` therefore
    // reported "Service result is not an integer status" for a kick that had in fact been issued,
    // which turned a working call into a reported failure. The objective signal is the member
    // list, so a missing status is not treated as an error: the confirmation poll below decides,
    // and the raw ack is attached to whatever it decides.
    const ackStatus = summarizeAck(ack);
    // Validate the field the status was actually read from. `resultCode(ack)` used to be called with
    // the default field name, so an ack carrying `errCode` but no `result` still failed with
    // "Service result is not an integer status" -- the same dead end, one layer down.
    if (ackStatus.code !== undefined) resultCode(ack, ackStatus.field);
    for (;;) {
      const after = await readTarget();
      if (!after.length) return ackStatus.code === undefined ? { ack_fields: ackStatus.fields } : {};
      if (after.length > 1) badResult('Multiple member records match the target UIN');
      if (after[0].uid !== target.uid) badResult('Member identity changed during kick confirmation');
      guard();
      await new Promise(resolve => setTimeout(resolve, Math.min(50, Math.max(1, deadline - Date.now()))));
    }
  }
  async function confirmMemberState(s, svc, op, groupId, userId, requested, deadline) {
    const guard = () => {
      if (Date.now() >= deadline) fail('TIMEOUT', `${op} state confirmation deadline exceeded; completion is unknown, do not blindly retry`, { op });
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during state confirmation');
    };
    const isCard = op === 'group.card';
    const readTarget = async () => {
      guard();
      const rows = await members(s, groupId, true, isCard, !isCard);
      guard();
      const found = rows.filter(m => String(m.user_id) === userId);
      if (found.length > 1) badResult('Multiple member records match the target UIN');
      return found[0];
    };
    const matches = member => member && (isCard ? member.card === requested : member.role === (requested === 3 ? 'admin' : 'member'));
    const before = await readTarget();
    if (!before) fail('NOT_FOUND', 'Target is not in the complete member list; mutation not issued');
    if (matches(before)) return {}; // Desired state already verified; no write required.
    if (!isCard && before.role === 'owner') fail('UNSUPPORTED', 'Owner role cannot be changed using the admin/member setter');
    guard();
    // Exactly one writer invocation. Undefined is NOT an acknowledgment. If a
    // build returns an explicit status envelope, reject its failure as well.
    const ack = await (isCard
      ? svc.modifyMemberCardName(groupId, before.uid, requested)
      : svc.modifyMemberRole(groupId, before.uid, requested));
    if (ack !== undefined) resultCode(ack);
    for (;;) {
      const after = await readTarget();
      if (after && after.uid !== before.uid) badResult('Member identity changed during confirmation');
      if (matches(after)) return {};
      guard();
      // Retry only observational reads, never the writer. The outer action
      // deadline also bounds a native read that never resolves.
      await new Promise(resolve => setTimeout(resolve, Math.min(50, Math.max(1, deadline - Date.now()))));
    }
  }
  async function executeInner(op, p, deadline = Date.now() + timeoutMs) {
    if (own(UNRESOLVED, op)) fail('UNSUPPORTED', UNRESOLVED[op], { op });
    if (op === 'login.info') {
      const identity = await options.getIdentity();
      if (!object(identity) || identity.logged_in === false || !identity.uin || String(identity.uin) === '0') fail('NOT_READY', 'Login identity is not available');
      // An account is the confirmation. `stage` is reported alongside it and is not a gate.
      //
      // An earlier version also required the injected side to report `ready` (its message listener attached).
      // That looked stricter, but the stage it derives from is rewritten by the 20 s QR refresh and by every
      // retry, so a session that was serving perfectly well reported "not confirmed" in between -- measured as
      // a session alternating between 已登录 and 未登录 every few seconds, and only when more than one session
      // was running, because the extra load made the rewrites overlap.
      //
      // The uin is the honest signal: it exists only after QQ has authenticated this session, and it does not
      // flicker. Anything that genuinely needs the listener waits for the events it is missing anyway.
      const userId = resultId(identity.uin, 'uin');
      return { user_id: userId, uin: userId, nickname: typeof identity.nickname === 'string' ? identity.nickname : typeof identity.nick === 'string' ? identity.nick : '' };
    }
    const friendListOp = ['friend.list', 'friend.categories', 'friend.statuses', 'friend.relations'].includes(op);
    if (!own(REQUIREMENTS, op) && !friendListOp && op !== 'message.send') fail('UNSUPPORTED', `Unsupported operation: ${op}`, { op });
    const s = session();
    if (op === 'message.send') {
      if (typeof options.sendMessage !== 'function') fail('UNSUPPORTED', 'message.send requires an injected verified send handler');
      const out = await options.sendMessage(p, { session: s, resolveUid, messageRefs: options.messageRefs, codec: options.codec });
      if (out === undefined || out === null) badResult('Injected send handler returned no confirmation');
      return out;
    }
    if (friendListOp) {
      const completeFacts = op === 'friend.statuses' || op === 'friend.relations';
      const force = bool(p, 'no_cache');
      if (force) fail('UNSUPPORTED', 'friend.list is cache-backed; forced refresh has no version-independent signature');
      const buddy = service(s, 'Buddy', completeFacts ? ['getBuddyListFromCache', 'hasBuddyList'] : ['getBuddyListFromCache']);
      const profile = service(s, 'Profile', ['getCoreAndBaseInfo']);
      if (typeof buddy.hasBuddyList === 'function' && buddy.hasBuddyList() !== true) fail('NOT_READY', 'Buddy cache has not been initialized');
      // The native binding asserts argc == 2 even though NapCat's own TypeScript declaration shows a
      // single `reqType` parameter. Calling with one argument throws
      //
      //     assertion (argc == 2) failed:
      //     NodeIKernelBuddyService::getBuddyListFromCache needs 2 arguments
      //
      // which is what broke get_friend_list for a real client. The second argument is not a result
      // filter: measured on a live client, `0`, `null`, `false`, `{}`, `[]` and a paging object all
      // return the same populated list (16 friends across 2 categories), so the binding only counts
      // arguments and ignores the value. An empty object is passed because it is the least surprising
      // thing to hand a parameter whose meaning is unknown.
      const categories = await buddy.getBuddyListFromCache(0, {});
      if (!Array.isArray(categories)) badResult('Buddy cache did not return categories');
      const uids = new Set();
      for (const c of categories) {
        if (!object(c) || !Array.isArray(c.buddyUids)) badResult('Malformed buddy category');
        for (const u of c.buddyUids) uids.add(uid(u));
      }
      if (!uids.size && typeof buddy.hasBuddyList !== 'function') fail('UNSUPPORTED', 'Cannot distinguish an uninitialized buddy cache from an empty friend list');
      // Argument order is (callfrom, uids), not (uids). Getting it backwards is worse than an error:
      // measured on a live client, `getCoreAndBaseInfo(uids, 'qsick')` returns an **empty Map** rather
      // than throwing, so a wrong order silently produces friends with no profile data.
      //
      // The first argument is a caller label the client uses for logging; any non-empty string works
      // and it appears in diagnostics, so this project's own name is used.
      const profiles = uids.size ? await profile.getCoreAndBaseInfo('qsick', [...uids]) : new Map();
      if (!(profiles instanceof Map)) badResult('Profile lookup did not return a Map');
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during friend lookup');
      const users = new Map([...uids].map(u => [u, normalizeUser(profiles.get(u), u)]));
      if (completeFacts) return [...users].map(([u, user]) => ({ ...user,
        ...(op === 'friend.statuses' ? normalizeStatus(profiles.get(u), user) : { relation_flags: normalizeRelations(profiles.get(u)) }) }));
      if (op === 'friend.list') return [...users.values()];
      return categories.map(c => {
        if (typeof c.categroyName !== 'string' || c.categoryId === undefined || c.categroyMbCount === undefined) badResult('Category metadata is incomplete');
        // Preserve category membership/order, including empty categories; do not
        // silently filter missing profiles or replace server counts with guesses.
        return { categoryId: resultInteger(c.categoryId, 'categoryId'),
          categorySortId: resultInteger(c.categorySortId, 'categorySortId'), categoryName: c.categroyName,
          categoryMbCount: resultInteger(c.categroyMbCount, 'categoryMbCount'),
          onlineCount: resultInteger(c.onlineCount, 'onlineCount'), buddyList: [...new Set(c.buddyUids)].map(u => users.get(u)) };
      });
    }
    if (op === 'friend.info') {
      const uin = id(p.user_id, 'user_id');
      bool(p, 'no_cache');
      const svc = service(s, 'Profile', ['getUserDetailInfoByUin']);
      const r = resultCode(await svc.getUserDetailInfoByUin(uin));
      if (!object(r.detail) || String(r.detail.uin) !== uin) badResult('Profile response does not match requested UIN');
      const out = normalizeUser(r.detail.simpleInfo, r.detail.uid);
      if (String(out.uin) !== uin) badResult('Profile core UIN mismatch');
      return out;
    }
    if (op === 'user.status') {
      const uin = id(p.user_id, 'user_id');
      if (bool(p, 'no_cache')) fail('UNSUPPORTED', 'User status is a cached profile fact; forced status refresh is not verified');
      const userUid = await resolveUid(uin);
      const svc = service(s, 'Profile', ['getCoreAndBaseInfo']);
      const profiles = await svc.getCoreAndBaseInfo('qsick', [userUid]);
      if (!(profiles instanceof Map)) badResult('Profile lookup did not return a Map');
      const info = profiles.get(userUid);
      const user = normalizeUser(info, userUid);
      if (String(user.uin) !== uin) badResult('Status profile UIN mismatch');
      return normalizeStatus(info, user);
    }
    // Read one message back by id, and page a conversation's history.
    //
    // Both were missing, which left `get_msg` (for anything not already in the local store), `get_group_msg_history`
    // and `get_friend_msg_history` answering "Unsupported operation". A consumer that quotes a message or asks
    // for context therefore failed with nothing useful to fall back on.
    //
    // Shapes follow NapCat: `message.get` resolves the peer from the msgId index and returns the message
    // itself, while history returns `{ messages: [...] }`. Both come back normalized, so the Python side runs
    // them through the same `message_json_payload` that received messages use.
    if (op === 'message.get') {
      const msgId = id(p.message_id, 'message_id', true);
      const ref = options.messageRefs instanceof Map ? options.messageRefs.get(msgId) : undefined;
      // No record means no peer, and a message cannot be fetched without one. NapCat answers "娑堟伅涓嶅瓨鍦?
      // here for the same reason: the id was never seen in this session.
      if (!ref) fail('NOT_FOUND', '娑堟伅涓嶅瓨鍦? this message id was never seen in this session', { message_id: msgId });
      if (!object(ref.peer) || ![1, 2].includes(ref.peer.chatType)) badResult('Invalid native message reference');
      const peer = {
        chatType: ref.peer.chatType,
        peerUid: ref.peer.chatType === 2 ? id(ref.peer.peerUid, 'peerUid') : uid(ref.peer.peerUid),
        guildId: ref.peer.guildId ?? ''
      };
      const svc = service(s, 'Msg', ['getMsgsByMsgId']);
      const response = resultCode(await svc.getMsgsByMsgId(peer, [String(ref.msgId ?? msgId)]));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed while reading the message');
      const list = Array.isArray(response.msgList) ? response.msgList : [];
      if (!list.length) fail('NOT_FOUND', '娑堟伅涓嶅瓨鍦ㄦ垨宸茶鎾ゅ洖');
      const mapped = options.codec.normalizeMessage(list[0], String(options.getIdentity().uin || ''), {});
      if (!mapped) badResult('Message could not be normalized');
      return mapped;
    }
    if (op === 'history.group' || op === 'history.private') {
      const isGroup = op === 'history.group';
      const peerId = isGroup ? id(p.group_id, 'group_id', true) : id(p.user_id, 'user_id', true);
      const count = Math.max(1, Math.min(100, Number(p.count) || 20));
      const reverse = bool(p, 'reverse_order') || bool(p, 'reverseOrder');
      // `message_seq` is the anchor. NapCat treats "absent", "0" and "" alike: all mean "latest".
      const rawSeq = p.message_seq;
      const hasSeq = rawSeq !== undefined && rawSeq !== null
        && !(String(rawSeq) === '' || String(rawSeq) === '0');
      let peer;
      if (isGroup) {
        peer = { chatType: 2, peerUid: peerId, guildId: '' };
      } else {
        // A private conversation is addressed by the peer's uid, not its uin. Use the dispatcher's own
        // `resolveUid`: it checks cached facts and then the native `Profile.getUidByUin` cache. Calling
        // `options.resolveUid` directly only sees locally observed pairs and returns undefined for anyone we
        // have not exchanged a message with yet -- which surfaced as "Service returned an invalid UID".
        peer = { chatType: 1, peerUid: await resolveUid(peerId), guildId: '' };
      }
      const svc = service(s, 'Msg', hasSeq
        ? ['getMsgsIncludeSelf']
        : ['getAioFirstViewLatestMsgs']);
      const response = resultCode(hasSeq
        ? await svc.getMsgsIncludeSelf(peer, String(rawSeq), count, reverse)
        : await svc.getAioFirstViewLatestMsgs(peer, count));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed while reading history');
      let list = Array.isArray(response.msgList) ? response.msgList : [];
      // `getMsgsIncludeSelf` takes an order argument; `getAioFirstViewLatestMsgs` does not, so the anchorless
      // path has to reverse the page itself. Without this `reverse_order` was accepted and silently ignored --
      // measured: [1308, 1309, 1310] whichever way it was set.
      if (reverse && !hasSeq) list = list.slice().reverse();
      const selfUin = String(options.getIdentity().uin || '');
      const messages = [];
      for (const raw of list) {
        try {
          const mapped = options.codec.normalizeMessage(raw, selfUin, {});
          if (!mapped) continue;
          messages.push(mapped);
          // Index what we just read.
          //
          // `get_msg` resolves a message's peer from this map, and without indexing history results it could
          // only answer for messages that arrived while this process was running -- so quoting anything older
          // returned "娑堟伅涓嶅瓨鍦?. NapCat's MessageUnique accumulates the same way, from every message it maps.
          try {
            if (mapped.native && options.messageRefs instanceof Map) {
              options.messageRefs.set(mapped.message_id, mapped.native);
              while (options.messageRefs.size > 10000) {
                options.messageRefs.delete(options.messageRefs.keys().next().value);
              }
            }
          } catch (_) { /* indexing is best-effort */ }
        } catch (_) { /* one unreadable record must not drop the page */ }
      }
      return { messages };
    }
    if (op === 'message.recall') {
      const msgId = id(p.message_id, 'message_id', true);
      const ref = options.messageRefs instanceof Map ? options.messageRefs.get(msgId) : undefined;
      if (!ref) fail('NOT_FOUND', 'No native message reference for recall', { message_id: msgId });
      if (!object(ref.peer) || String(ref.msgId) !== msgId || ![1, 2].includes(ref.peer.chatType)) badResult('Invalid native message reference');
      const peer = { chatType: ref.peer.chatType, peerUid: ref.peer.chatType === 2 ? id(ref.peer.peerUid, 'peerUid') : uid(ref.peer.peerUid), guildId: ref.peer.guildId ?? '' };
      const svc = service(s, 'Msg', ['recallMsg']);
      resultCode(await svc.recallMsg(peer, [msgId]));
      return {};
    }
    if (op === 'group.list') {
      const force = bool(p, 'no_cache');
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      return groupEvent(svc, 'getGroupList', [force], 'onGroupListUpdate', (type, list) => {
        if (type !== 0 && type !== 1) return undefined;
        if (!Array.isArray(list)) badResult('Full group-list event is missing its list');
        return list.map(normalizeGroup);
      });
    }
    // Ops that do NOT require a group_id.
    //
    // Every op below falls through to `id(p.group_id, 'group_id')`, which throws when the field is absent.
    // That is right for the group-scoped ops -- they genuinely need one -- but wrong for anything that can
    // also address a private chat, and wrong for the ops that take no target at all.
    //
    // Measured: `send_private_forward_msg` ({user_id, messages:[node鈥}) failed with "group_id must be a
    // positive canonical decimal ID" even though the handler below resolves `user_id` into a private
    // destination perfectly well. It never got the chance: this check ran first.
    const NO_GROUP_ID_OPS = new Set([
      'friend.like', 'cache.clean', 'forward.get', 'media.download', 'recent.list',
      'message.reactions', 'system.ocr', 'system.rkey', 'system.oidb_probe',
      // A raw OIDB call addresses nothing by itself: the command and its body are the whole request,
      // and the target (if any) is encoded inside `body_hex`. Without this exemption it failed with
      // "group_id must be a positive canonical decimal ID" before its own branch was reached.
      'oidb.call',
      // Same reasoning for the private-file ops: they are addressed by `user_id`, and one of them
      // takes no target at all.
      'upload_private_file', 'private.file.download_url',
      'emoji.favorites', 'emoji.favorite.add', 'emoji.favorite.remove',
      // Destination is resolved by the handler from group_id OR user_id.
      'forward.send',
      // Same: its handler reads `group_id` and `user_id` as alternatives and rejects only when both are
      // absent. Without this exemption the private half was unreachable -- measured, `{user_id, count}`
      // failed with the group_id message while `{group_id, count}` worked.
      'message.history',
    ]);
    // qzone.* is exempt by prefix: those ops are HTTP calls keyed by the account, not by a group. Without the
    // exemption the handler below was unreachable -- measured, qzone.feeds answered "group_id must be a positive
    // canonical decimal ID" and the reason the op exists for was never printed.
    const groupId = (NO_GROUP_ID_OPS.has(op) || op.startsWith('qzone.')) ? '' : id(p.group_id, 'group_id');
    if (op === 'group.share_link') {
      const needShortUrl = bool(p, 'need_short_url', true);
      const srcId = integer(p.src_id === undefined ? 73 : p.src_id, 'src_id', 0, 2147483647);
      const additionalParam = text(p.additional_param === undefined ? '' : p.additional_param, 'additional_param');
      const svc = service(s, 'Group', ['getJoinGroupLink']);
      const response = resultCode(await svc.getJoinGroupLink({ groupCode: groupId, srcId, needShortUrl, additionalParam }));
      if (typeof response.url !== 'string' || !response.url.trim() || response.url.includes('\0')) badResult('Group share link is missing or invalid');
      return { url: response.url }; // Native data only; never fetch/open the URL.
    }
    if (op === 'group.share_ark') {
      const svc = service(s, 'Group', ['getGroupRecommendContactArkJson']);
      const response = resultCode(await svc.getGroupRecommendContactArkJson(groupId));
      if (typeof response.arkJson !== 'string') badResult('Group share card is missing arkJson');
      let card;
      try { card = JSON.parse(response.arkJson); } catch { badResult('Group share card is not JSON'); }
      if (!object(card)) badResult('Group share card must be a JSON object');
      return { ark_json: response.arkJson }; // Preserve original JSON; no send side effect.
    }
    if (op === 'group.card' || op === 'group.admin') {
      const userId = id(p.user_id, 'user_id');
      const requested = op === 'group.card' ? text(p.card === undefined ? '' : p.card, 'card') : (bool(p, 'enable', true) ? 3 : 2);
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      return confirmMemberState(s, svc, op, groupId, userId, requested, deadline);
    }
    if (op === 'group.kick') {
      const userId = id(p.user_id, 'user_id');
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      return confirmKick(s, svc, groupId, userId, deadline);
    }
    if (op === 'group.at_all_remain') {
      const svc = service(s, 'Group', ['getGroupRemainAtTimes']);
      const response = resultCode(await svc.getGroupRemainAtTimes(groupId), 'errCode');
      const at = response.atInfo;
      if (!object(at) || typeof at.canAtAll !== 'boolean' || at.RemainAtAllCountForUin === undefined || at.RemainAtAllCountForGroup === undefined) badResult('At-all quota response is incomplete');
      return { can_at_all: at.canAtAll,
        remain_at_all_count_for_group: resultInteger(at.RemainAtAllCountForGroup, 'RemainAtAllCountForGroup'),
        remain_at_all_count_for_uin: resultInteger(at.RemainAtAllCountForUin, 'RemainAtAllCountForUin') };
    }
    if (op === 'oidb.call') {
      // Send one OIDB command and hand back QQ's status plus the raw body.
      //
      // The envelope, the command-name style (`OidbSvcTrpcTcp.0x….sub` routes where the plain style
      // does not, per headless_oidb.cjs) and the decode all live in that module already; this is the
      // thin seam that makes them reachable from an action instead of only from rkey handling.
      const command = integer(p.command, 'command', 1, 0x7fffffff);
      const subCommand = integer(p.sub_command === undefined ? 0 : p.sub_command,
        'sub_command', 0, 0xffff);
      const bodyHex = text(p.body_hex === undefined ? '' : String(p.body_hex));
      if (bodyHex && !/^[0-9a-fA-F]*$/.test(bodyHex)) invalid('body_hex must be hexadecimal');
      if (bodyHex.length % 2 !== 0) invalid('body_hex must contain whole bytes');
      let oidb;
      try {
        oidb = typeof options.loadModule === 'function'
          ? options.loadModule('headless_oidb.cjs')
          : require('./headless_oidb.cjs');
      } catch (error) {
        fail('UNSUPPORTED', `OIDB module unavailable: ${String((error && error.message) || error).slice(0, 160)}`);
      }
      const body = Buffer.from(bodyHex, 'hex');
      const style = text(p.style === undefined ? '' : String(p.style));
      if (style && !['plain', 'trpc', 'lower'].includes(style)) {
        invalid('style must be one of plain / trpc / lower');
      }
      let decoded;
      try {
        decoded = await oidb.sendOidb(() => s.getMsgService(), command, subCommand, body,
          p.timeout_ms === undefined ? 10000 : integer(p.timeout_ms, 'timeout_ms', 1000, 60000),
          style);
      } catch (error) {
        // A refusal here is QQ's answer, not a bug in the caller: report it as upstream so the
        // difference between "the command is not routed" and "the payload was wrong" survives.
        fail('UPSTREAM', String((error && error.message) || error).slice(0, 300), {
          command, sub_command: subCommand,
        });
      }
      const responseBody = decoded && (decoded.body || decoded.payload);
      const buffer = Buffer.isBuffer(responseBody) ? responseBody
        : Buffer.from(responseBody || []);
      return {
        command: Number(command),
        sub_command: Number(subCommand),
        // The name that actually worked, reported by the transport rather than reconstructed here.
        resolved: decoded && decoded.name ? String(decoded.name) : '',
        error_code: decoded && decoded.errorCode !== undefined ? Number(decoded.errorCode) : 0,
        error_message: decoded && typeof decoded.errorMsg === 'string' ? decoded.errorMsg : '',
        body_hex: buffer.toString('hex'),
        body_size: buffer.length,
      };
    }
    if (op === 'system.oidb_probe') {
      const svc = service(s, 'Msg', ['sendSsoCmdReqByContend']);
      const oidb = require('./headless_oidb.cjs');
      const names = Array.isArray(p.names) ? p.names.filter(n => typeof n === 'string').slice(0, 12) : [];
      if (!names.length) invalid('names must be a non-empty array of command strings');
      const payload = Buffer.from(typeof p.payload_hex === 'string' && p.payload_hex ? p.payload_hex : '', 'hex');
      const results = [];
      for (const name of names) results.push({ name, ...(await oidb.probeCommand(() => svc, name, payload)) });
      return { results };
    }
    if (op === 'qzone.credentials') {
      // The ticket, and the derived g_tk, for the HTTP client in Python.
      //
      // Only `qzone.qq.com` matters: it is the one domain that answers (44 chars), and every measured Qzone call
      // works with it. The others are asked for and reported so the answer shows what was available rather than
      // implying the list was refused.
      const svc = service(s, 'TipOff', REQUIREMENTS[op].slice(1));
      const domains = ['qzone.qq.com', 'qun.qq.com', 'h5.qzone.qq.com', 'user.qzone.qq.com'];
      const answer = await svc.getPskey(domains, true);
      const map = answer && answer.domainPskeyMap;
      const keys = {};
      for (const domain of domains) {
        let value = '';
        try {
          value = map ? (typeof map.get === 'function' ? map.get(domain) : map[domain]) : '';
        } catch (error) { value = ''; }
        keys[domain] = value ? String(value) : '';
      }
      const pskey = keys['qzone.qq.com'];
      if (!pskey) {
        fail('UPSTREAM', 'getPskey returned no key for qzone.qq.com, so Qzone cannot be called from this session',
             { domains: Object.fromEntries(Object.entries(keys).map(([k, v]) => [k, v ? v.length : 0])) });
      }
      // hash33, the same accumulation Qzone's own pages use for g_tk.
      let hash = 5381;
      for (let index = 0; index < pskey.length; index += 1) {
        hash += (hash << 5) + pskey.charCodeAt(index);
      }
      const gtk = hash & 0x7fffffff;
      const boot = global.__qsickLoginBoot || {};
      const uin = String(boot.uin || '');
      return {
        uin: uin || String(account || ''),
        pskey,
        g_tk: gtk,
        // Lengths, not values: enough to see which domains the client will hand over without copying tickets
        // around in logs.
        available: Object.fromEntries(Object.entries(keys).map(([name, value]) => [name, value ? value.length : 0])),
      };
    }
    if (op === 'system.rkey') {
      const svc = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      const oidb = require('./headless_oidb.cjs');
      const result = await oidb.sendOidb(() => svc, 0x9067, 202, oidb.encodeFetchRkey(), Math.max(3000, timeoutMs));
      if (result.errorCode !== 0) {
        fail('SERVICE_ERROR',
          (result.errorMsg || ('OIDB returned errorCode=' + result.errorCode))
            + ' | command=' + result.command + ' sub=' + result.subCommand
            + ' bodyHex=' + result.body.toString('hex').slice(0, 120),
          { serviceCode: result.errorCode });
      }
      const list = oidb.decodeFetchRkeyResponse(result.body);
      if (!list.length) fail('INVALID_RESULT', 'OIDB returned no rkey entries');
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during rkey fetch');
      return { rkeys: list.map(item => ({ rkey: item.rkey, ttl: item.ttl, type: item.type, time: item.time })) };
    }
    if (op === 'emoji.favorites') {
      const count = integer(p.count === undefined ? 48 : p.count, 'count', 1, 500);
      const svc = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      // Signature verified from the native declaration: (startId, count,
      // backward, forceRefresh). An empty start id means "from the beginning".
      const response = resultCode(await svc.fetchFavEmojiList('', count, true, true));
      if (!object(response)) badResult('Favourite-emoji response is malformed');
      if (!Array.isArray(response.emojiInfoList)) badResult('Favourite-emoji response is missing its list');
      const items = response.emojiInfoList.map(row => {
        if (!object(row)) badResult('Favourite-emoji row is malformed');
        const pick = key => (typeof row[key] === 'string' ? row[key] : '');
        const resId = pick('resId') || pick('eId');
        if (!resId) return undefined; // Without an id the row cannot be deleted or sent.
        return {
          res_id: resId,
          emoji_id: row.emoId === undefined || row.emoId === null ? '' : String(row.emoId),
          md5: pick('md5'),
          url: pick('url'),
          path: pick('emoPath'),
          original_path: pick('emoOriginalPath'),
          thumb_path: pick('thumbPath'),
          desc: pick('desc') || pick('modifyWord') || pick('ocrWord'),
          is_mark_face: row.isMarkFace === true,
          is_apng: row.isAPNG === true,
          expose_num: resultInteger(row.exposeNum, 'exposeNum'),
          click_num: resultInteger(row.clickNum, 'clickNum'),
        };
      }).filter(Boolean);
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during favourite-emoji lookup');
      return { emoticons: items };
    }
    if (op === 'emoji.favorite.add') {
      const emojiPath = text(p.file, 'file', false);
      if (emojiPath.includes('\0')) invalid('file must not contain NUL');
      const svc = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      let info;
      try {
        info = require('fs').statSync(emojiPath);
      } catch (error) {
        fail('NOT_FOUND', 'Favourite-emoji source file is not readable: ' + error.message);
      }
      if (!info.isFile() || info.size === 0) fail('INVALID_RESULT', 'Favourite-emoji source must be a non-empty file');
      // QQ keys the upload on the file hash, so it must be computed, never guessed.
      const md5 = typeof p.md5 === 'string' && /^[0-9a-fA-F]{32}$/.test(p.md5)
        ? p.md5.toLowerCase()
        : require('crypto').createHash('md5').update(require('fs').readFileSync(emojiPath)).digest('hex');
      const payload = {
        emojiId: p.emoji_id === undefined || p.emoji_id === null ? '' : String(p.emoji_id),
        packageId: p.package_id === undefined ? 0 : integer(p.package_id, 'package_id', 0, 2147483647),
        emojiPath,
        fileSize: String(p.file_size === undefined ? info.size : integer(p.file_size, 'file_size', 0, 2147483647)),
        fileName: typeof p.file_name === 'string' && p.file_name
          ? p.file_name
          : require('path').basename(emojiPath),
        md5,
        isMarkFace: bool(p, 'is_mark_face', false),
        isOrigin: bool(p, 'is_origin', true),
      };
      // The native method returns unknown, so completion cannot be asserted here;
      // callers verify by re-reading the list.
      await svc.addFavEmoji(payload);
      if (session() !== s) fail('NOT_READY', 'QQNT session changed while adding a favourite emoji');
      return { submitted: true, md5, res_id: '', verify: 're-read emoji.favorites to confirm' };
    }
    if (op === 'emoji.favorite.remove') {
      const raw = Array.isArray(p.res_ids) ? p.res_ids : (p.res_id === undefined ? [] : [p.res_id]);
      const ids = raw.map((value, index) => text(value, 'res_ids[' + index + ']', false)).filter(Boolean);
      if (!ids.length) invalid('res_ids must list at least one resource id');
      const svc = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      // deleteFavEmoji takes the string resource ids returned by emoji.favorites,
      // never the MD5 or the numeric emoji id.
      await svc.deleteFavEmoji(ids);
      if (session() !== s) fail('NOT_READY', 'QQNT session changed while removing a favourite emoji');
      return { submitted: true, res_ids: ids, verify: 're-read emoji.favorites to confirm' };
    }
    if (op === 'system.ocr') {
      const imagePath = text(p.image_path, 'image_path', false);
      if (imagePath.includes('\0')) invalid('image_path must not contain NUL');
      const misc = service(s, 'NodeMisc', REQUIREMENTS[op].slice(1));
      // Returns GeneralCallResult in the verified native declaration.
      const response = resultCode(await misc.wantWinScreenOCR(imagePath));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during OCR');
      const result = response.result === 0 || response.result === '0' ? (response.text ?? response.ocrText) : undefined;
      return { text: typeof result === 'string' ? result : '' };
    }
    if (op === 'message.reactions') {
      const ref = options.messageRefs instanceof Map ? options.messageRefs.get(String(p.message_id)) : undefined;
      if (!object(ref) || !object(ref.peer) || typeof ref.msgSeq !== 'string') {
        fail('NOT_FOUND', 'No cached native reference for this message; reaction list requires a received message');
      }
      const chatType = Number(ref.peer.chatType);
      if (chatType !== 1 && chatType !== 2) fail('UNSUPPORTED', 'Reaction list supports only C2C and group messages');
      const emojiId = text(p.emoji_id === undefined ? '128077' : String(p.emoji_id), 'emoji_id', false);
      const emojiType = p.emoji_type === undefined ? (emojiId.length > 3 ? '2' : '1') : text(String(p.emoji_type), 'emoji_type', false);
      const count = integer(p.count === undefined ? 20 : p.count, 'count', 1, 100);
      const peer = { chatType, peerUid: String(ref.peer.peerUid), guildId: '' };
      const msg = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      const response = await msg.getMsgEmojiLikesList(peer, ref.msgSeq, emojiId, emojiType,
        p.cookie === undefined ? '' : text(p.cookie, 'cookie'), false, count);
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during reaction lookup');
      if (!object(response)) badResult('Reaction response is missing its payload');
      if (Number(response.errCode) !== 0) fail('SERVICE_ERROR', String(response.errMsg || 'Reaction lookup failed'), { serviceCode: response.errCode });
      if (!Array.isArray(response.emojiLikesList)) badResult('Reaction response is missing its list');
      return {
        users: response.emojiLikesList.map(row => {
          if (!object(row) || typeof row.tinyId !== 'string') badResult('Reaction row is missing tinyId');
          return { tiny_id: row.tinyId, nickname: typeof row.nickName === 'string' ? row.nickName : '', head_url: typeof row.headUrl === 'string' ? row.headUrl : '' };
        }),
        cookie: typeof response.cookie === 'string' ? response.cookie : '',
        is_last_page: response.isLastPage === true,
        is_first_page: response.isFirstPage === true,
      };
    }
    if (op === 'recent.list') {
      const count = integer(p.count === undefined ? 50 : p.count, 'count', 1, 500);
      const svc = service(s, 'RecentContact', REQUIREMENTS[op].slice(1));
      const response = resultCode(await svc.getRecentContactListSnapShot(count));
      if (!object(response.info)) badResult('Recent-contact snapshot is missing its info payload');
      const info = response.info;
      if (Number(info.errCode) !== 0) fail('SERVICE_ERROR', String(info.errMsg || 'Recent-contact snapshot failed'), { serviceCode: info.errCode });
      if (!Array.isArray(info.changedList)) badResult('Recent-contact snapshot is missing its contact list');
      if (!Array.isArray(info.sortedContactList)) badResult('Recent-contact snapshot is missing sort order');
      const byUid = new Map();
      for (const row of info.changedList) {
        if (!object(row) || typeof row.peerUid !== 'string' || !row.peerUid) badResult('Recent-contact row is missing peerUid');
        if (byUid.has(row.peerUid)) badResult('Duplicate recent-contact row');
        const chatType = Number(row.chatType);
        if (chatType !== 1 && chatType !== 2) continue; // Other chat kinds are not C2C/group.
        byUid.set(row.peerUid, {
          chat_type: chatType === 2 ? 'group' : 'private',
          peer_uid: row.peerUid,
          peer_uin: row.peerUin === undefined ? '' : String(row.peerUin),
          nickname: typeof row.sendNickName === 'string' ? row.sendNickName : (typeof row.peerName === 'string' ? row.peerName : ''),
          card: typeof row.sendMemberName === 'string' ? row.sendMemberName : '',
          remark: typeof row.remark === 'string' ? row.remark : '',
          last_message_id: row.msgId === undefined ? '' : String(row.msgId),
          last_time: resultInteger(row.msgTime, 'msgTime'),
        });
      }
      // Preserve the native ordering, dropping entries we cannot describe.
      const rows = [];
      for (const uid of info.sortedContactList) {
        const row = byUid.get(uid);
        if (row) { rows.push(row); byUid.delete(uid); }
      }
      for (const row of byUid.values()) rows.push(row);
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during recent-contact lookup');
      return rows;
    }
    if (op === 'media.download') {
      const messageId = text(p.message_id, 'message_id', false);
      const elementId = text(p.element_id, 'element_id', false);
      const chat = p.chat;
      if (!object(chat) || !['group', 'private'].includes(chat.type)) invalid('chat must specify group/private');
      const chatType = chat.type === 'group' ? 2 : 1;
      const chatId = id(chat.id, 'chat.id');
      const peerUid = chatType === 2 ? chatId : await resolveUid(chatId);
      if (chatType === 1 && (typeof peerUid !== 'string' || !/^u_[A-Za-z0-9_-]+$/.test(peerUid))) {
        fail('UNSUPPORTED', 'No verified private-peer UID mapping for this media download');
      }
      const filePath = text(p.file_path, 'file_path', false);
      const msg = service(s, 'Msg', ['downloadRichMedia', 'addKernelMsgListener', 'removeKernelMsgListener']);
      // Completion is correlated by msgId + elementId; it is not request-scoped,
      // so the listener is removed as soon as the matching record arrives.
      let handle;
      let settle;
      const done = new Promise((resolve, reject) => {
        let timer;
        const cleanup = () => { clearTimeout(timer); try { if (handle !== undefined) msg.removeKernelMsgListener(handle); } catch (_) {} };
        settle = { resolve: value => { cleanup(); resolve(value); }, reject: error => { cleanup(); reject(error); } };
        timer = setTimeout(() => settle.reject(new ActionError('TIMEOUT', 'Rich-media download did not confirm; file may be unavailable')), Math.max(1, deadline - Date.now()));
        handle = msg.addKernelMsgListener(new Proxy({
          onRichMediaDownloadComplete(info) {
            if (!object(info)) return;
            if (String(info.msgId) !== String(messageId) || String(info.msgElementId) !== String(elementId)) return;
            if (info.fileErrCode !== undefined && Number(info.fileErrCode) !== 0) {
              settle.reject(new ActionError('SERVICE_ERROR', String(info.fileErrMsg || 'Rich-media download failed'), { fileErrCode: info.fileErrCode }));
              return;
            }
            if (typeof info.filePath !== 'string' || !info.filePath) {
              settle.reject(new ActionError('INVALID_RESULT', 'Download completed without a local file path'));
              return;
            }
            settle.resolve({ file_path: info.filePath, file_size: resultInteger(info.totalSize, 'totalSize') });
          }
        }, { get: (target, prop) => target[prop] || function () {} }));
      });
      done.catch(() => {}); // avoid an unhandled rejection if the call throws first
      msg.downloadRichMedia({
        fileModelId: p.file_model_id === undefined ? '' : text(p.file_model_id, 'file_model_id'),
        downSourceType: integer(p.down_source_type === undefined ? 0 : p.down_source_type, 'down_source_type', 0, 100),
        triggerType: integer(p.trigger_type === undefined ? 0 : p.trigger_type, 'trigger_type', 0, 100),
        msgId: messageId, chatType, peerUid: String(peerUid), elementId,
        thumbSize: integer(p.thumb_size === undefined ? 0 : p.thumb_size, 'thumb_size', 0, 65535),
        downloadType: integer(p.download_type === undefined ? 1 : p.download_type, 'download_type', 0, 100),
        filePath, downloadSourceType: integer(p.download_source_type === undefined ? 0 : p.download_source_type, 'download_source_type', 0, 100)
      });
      return await done;
    }
    if (op === 'friend.like') {
      const userId = id(p.user_id, 'user_id');
      const times = integer(p.times === undefined ? 1 : p.times, 'times', 1, 20);
      const userUid = await resolveUid(userId);
      if (Date.now() >= deadline) fail('TIMEOUT', 'UID resolution exhausted action deadline; like was not issued');
      const like = service(s, 'ProfileLike', REQUIREMENTS[op].slice(1));
      // Wrapper-verified shape (apis/user.ts like()): friendUid + sourceId 71.
      const response = resultCode(await like.setBuddyProfileLike({
        friendUid: userUid, sourceId: 71, doLikeCount: times, doLikeTollCount: 0
      }));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during like');
      const succ = resultInteger(response.succCounts, 'succCounts');
      if (succ <= 0) fail('SERVICE_ERROR', 'QQ accepted the request but reported no successful like', { succCounts: succ });
      return { succ_counts: succ };
    }
    if (op === 'cache.clean') {
      const keys = p.keys;
      if (!Array.isArray(keys) || !keys.length || !keys.every(k => typeof k === 'string' && k)) {
        invalid('keys must be a non-empty array of cache key strings');
      }
      const clean = service(s, 'StorageClean', REQUIREMENTS[op].slice(1));
      const response = resultCode(await clean.clearCacheDataByKeys([...keys]));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during cache clean');
      return { keys: [...keys], result: response.result };
    }
    if (op === 'forward.get') {
      const resId = p.res_id === undefined ? p.id : p.res_id;
      if (typeof resId !== 'string' || !/^[A-Za-z0-9_+/=-]{8,4096}$/.test(resId)) invalid('res_id must be a forward resource ID string');
      const rootId = p.root_msg_id === undefined ? resId : text(p.root_msg_id, 'root_msg_id', false);
      const chat = p.chat;
      if (!object(chat) || !['group', 'private'].includes(chat.type)) invalid('chat must specify group/private');
      const chatType = chat.type === 'group' ? 2 : 1;
      const chatId = id(chat.id, 'chat.id');
      const peerUid = chatType === 2 ? chatId : await resolveUid(chatId);
      if (chatType === 1 && (typeof peerUid !== 'string' || !/^u_[A-Za-z0-9_-]+$/.test(peerUid))) {
        fail('UNSUPPORTED', 'No verified private-peer UID mapping for this forward');
      }
      const msg = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      const peer = { chatType, peerUid: String(peerUid), guildId: '' };
      // arg2 is the outermost root, arg3 the multimsg element at this depth.
      const response = resultCode(await msg.getMultiMsg(peer, rootId, resId));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during forward retrieval');
      if (!Array.isArray(response.msgList)) badResult('Forward response is missing its message list');
      if (!options.codec || typeof options.codec.normalizeMessage !== 'function') fail('UNSUPPORTED', 'Message codec is unavailable');
      const nodes = [];
      for (const raw of response.msgList) {
        const mapped = options.codec.normalizeMessage(raw, String(options.getIdentity().uin || ''), {
          resolveUin: uid => options.resolveUin ? undefined : undefined
        });
        if (!mapped) continue;
        nodes.push({ type: 'node', data: {
          user_id: Number(mapped.sender.uin) || 0,
          nickname: mapped.sender.nickname || '',
          time: Number(mapped.time) || 0,
          content: mapped.elements
        } });
      }
      if (!nodes.length) fail('NOT_FOUND', 'Forward contains no readable messages');
      return { message: nodes };
    }
    if (op === 'forward.send') {
      // Sends a merged forward.
      //
      // The client's own call shape, from the intercepted captures:
      //
      //   multiForwardMsgWithComment(
      //     msgInfos,          [{msgId, senderShowName}]
      //     srcContact,        {chatType, peerUid, guildId}
      //     dstContact,        {chatType, peerUid, guildId}
      //     commentElements,   [] always
      //     msgAttributeInfos  {} always
      //   )
      //
      // msgInfos carries only two fields per message. The native layer resolves the bodies from the
      // local store by msgId, so elements must NOT be supplied -- passing a full message record is
      // accepted by the shape check and then ignored, which is a confusing way to fail.
      const svc = service(s, 'Msg', REQUIREMENTS[op].slice(1));

      // Two caller shapes reach this op:
      //
      //   OneBot v11    {group_id|user_id, messages: [node...]}   node = {type:'node', data:{...}}
      //   internal      {chat: {type,id}, source: {type,id}, messages: [{message_id, name}]}
      //
      // OneBot is the one that matters for protocol work, so it is handled directly. Its nodes carry
      // the message content rather than message ids, which the merged-forward call cannot accept --
      // it resolves bodies from the local store by id. So a OneBot node list is rejected with the
      // reason spelled out, instead of silently producing an empty card.
      const dstChat = p.chat
        || (p.group_id !== undefined
          ? { type: 'group', id: p.group_id }
          : (p.user_id !== undefined ? { type: 'private', id: p.user_id } : null));
      if (!object(dstChat) || !['group', 'private'].includes(dstChat.type)) {
        invalid('chat must specify group or private');
      }
      const dstType = dstChat.type === 'group' ? 2 : 1;
      const dstId = id(dstChat.id, 'chat.id');
      const dstPeer = dstType === 2 ? dstId : await resolveUid(dstId);
      if (dstType === 1 && (typeof dstPeer !== 'string' || !/^u_[A-Za-z0-9_-]+$/.test(dstPeer))) {
        fail('UNSUPPORTED', 'No verified private-peer UID mapping for this forward target');
      }

      const srcChat = p.source;
      let srcContact;
      let msgInfos;

      // A `source` only applies to the internal shape, where messages are bare `{message_id, name}`.
      //
      // OneBot callers build `node` segments instead, and a node's content has to be materialized before it
      // has an id -- so `source` cannot describe it. Testing for node segments rather than for `source`
      // keeps a caller that sends both from being routed into a branch that would reject every entry.
      const looksLikeNodes = Array.isArray(p.messages)
        && p.messages.some(n => object(n) && n.type === 'node');
      if (object(srcChat) && Array.isArray(p.messages) && !looksLikeNodes) {
        // Explicit source conversation: messages are identified by msgId within it.
        const srcType = srcChat.type === 'group' ? 2 : 1;
        const srcId = id(srcChat.id, 'source.id');
        const srcPeer = srcType === 2 ? srcId : await resolveUid(srcId);
        srcContact = { chatType: srcType, peerUid: String(srcPeer), guildId: '' };
        msgInfos = p.messages.map(item => {
          if (!object(item) || item.message_id === undefined) {
            invalid('each message needs message_id');
          }
          const m = String(item.message_id);
          if (!/^\d{1,20}$/.test(m)) invalid('message_id must be a decimal string');
          const name = typeof item.name === 'string' ? item.name : '';
          return { msgId: m, senderShowName: name.slice(0, 64) };
        });
      } else if (Array.isArray(p.messages)) {
        // OneBot node list.
        //
        // A node carries either a message id (`data.id`) or message *content* (`data.content`). The
        // merged-forward call resolves bodies from the local store by msgId, so content has to become a real
        // message before it can be forwarded. NapCat does exactly that: it sends each node's content to
        // itself -- a C2C message to its own uid -- and then forwards the resulting ids with itself as the
        // source conversation (napcat-onebot/action/msg/SendMsg.ts, `handleForwardedNodes`).
        //
        // This used to be a flat refusal, which made every go-cqhttp-shaped `send_group_forward_msg` /
        // `send_private_forward_msg` fail. That is the shape real plugins send -- Yunzai's update plugin
        // builds nodes from content alone -- so the feature was simply unusable.
        const selfUin = String(options.getIdentity().uin || '');
        const selfUid = String(options.getIdentity().uid || '');
        if (!selfUid) fail('NOT_READY', 'The account has no uid yet; cannot materialize forward nodes');
        const infos = [];
        for (const node of p.messages) {
          if (!object(node) || node.type !== 'node' || !object(node.data)) {
            invalid('each entry must be a node with a data object');
          }
          const nd = node.data;
          const name = typeof nd.name === 'string' ? nd.name.slice(0, 64) : '';
          const nodeId = nd.id !== undefined && nd.id !== null ? nd.id : nd.message_id;
          if (nodeId !== undefined && nodeId !== null && String(nodeId) !== '') {
            // An id refers to a message this session has seen; the native record carries both the real
            // msgId and the peer it came from.
            const ref = options.messageRefs instanceof Map ? options.messageRefs.get(String(nodeId)) : undefined;
            if (!ref || !object(ref.peer)) {
              fail('NOT_FOUND', '转发消息失败，未找到消息: ' + String(nodeId), { id: String(nodeId) });
            }
            infos.push({ msgId: String(ref.msgId ?? nodeId), senderShowName: name });
            continue;
          }
          if (!Array.isArray(nd.content) || !nd.content.length) {
            invalid('a node needs either data.id or a non-empty data.content');
          }
          if (typeof options.sendMessage !== 'function') {
            fail('UNSUPPORTED', 'materializing forward nodes needs an injected verified send handler');
          }
          // Sent to self so it lands in the local store with a msgId the forward call can resolve. This goes
          // through the sender rather than `sendMsg` directly because the raw ack carries no message id --
          // only the receipt does.
          const staged = await options.sendMessage(
            { chat: { type: 'private', id: selfUin }, elements: nd.content },
            { session: s, resolveUid, messageRefs: options.messageRefs });
          const stagedId = String((staged && (staged.message_id_str || staged.message_id)) || '');
          if (!stagedId) fail('INVALID_RESULT', '转发消息失败，生成节点为空');
          infos.push({ msgId: stagedId, senderShowName: name || String(options.getIdentity().nickname || '') });
        }
        msgInfos = infos;
        // Every node was materialized as a message to ourselves, so that is the source conversation.
        srcContact = { chatType: 1, peerUid: selfUid, guildId: '' };
      } else {
        invalid('messages must be an array of nodes');
      }

      if (!msgInfos.length) fail('INVALID', 'forward needs at least one message');
      // The client renders a summary; an oversized list is refused here rather than after upload.
      if (msgInfos.length > 200) invalid('forward is limited to 200 messages');

      const dstContact = { chatType: dstType, peerUid: String(dstPeer), guildId: '' };
      // The last argument is a Map, not a plain object.
      //
      // NapCat's measured call (napcat-core/apis/msg.ts `multiForwardMsg`):
      //
      //     multiForwardMsgWithComment(msgInfos, srcPeer, destPeer, [], new Map())
      //
      // Passing `{}` there is a type mismatch the native binding does not reject cleanly: measured on this
      // client the call never returned at all, which blocked the JS thread and with it every later action
      // (`loop-ticks.txt` stops advancing and the only way out is a restart).
      const startedAt = Math.floor(Date.now() / 1000);
      const ack = resultCode(await svc.multiForwardMsgWithComment(
        msgInfos, srcContact, dstContact, [], new Map()));
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during forward send');
      // The ack carries no message id; the card is published asynchronously.
      //
      // NapCat gets the id by waiting on `NodeIKernelMsgListener/onMsgInfoListUpdate` for a message from this
      // peer. The dispatcher has no event bus, so it asks the same service for the newest message instead and
      // accepts it only if it appeared after the call started. Bounded on purpose: a client that cannot answer
      // still gets `message_id: 0` rather than a stalled action.
      let messageId = 0;
      const resId = ack && ack.resId ? String(ack.resId) : '';
      // Reuse `svc`: it is already this session's Msg service. Asking `service()` for a second one re-validates
      // and can throw, which would abandon the lookup entirely -- measured as a 0.3 s return with no id even
      // though the very same method answers fine when the history action calls it.
      for (let attempt = 0; attempt < 4 && !messageId; attempt += 1) {
        try {
          if (attempt) await new Promise(resolve => setTimeout(resolve, 400));
          const latest = await svc.getAioFirstViewLatestMsgs(dstContact, 1);
          const list = object(latest) && Array.isArray(latest.msgList) ? latest.msgList : [];
          const newest = list[0];
          if (!object(newest)) continue;
          const sentAt = Number(newest.msgTime) || 0;
          // Only accept a message that appeared after this call started; otherwise it is the previous card.
          if (sentAt >= startedAt) messageId = resultId(newest.msgId, 'msgId');
        } catch (_) { /* keep trying, the forward itself already succeeded */ }
      }
      return { message_id: messageId, res_id: resId };
    }
    if (op === 'group.file.list') {
      // Lists one folder. The root is "/", not "" -- measured: an empty folderId returns an empty
      // item list with retCode 0, so the call looks successful while showing nothing. A folder
      // created moments earlier only appeared once the id was "/".
      const groupId = id(p.group_id, 'group_id');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(2));
      const msg = service(s, 'Msg', ['addKernelMsgListener', 'removeKernelMsgListener']);
      const folderId = p.folder_id === undefined || p.folder_id === '' ? '/' : String(p.folder_id);
      const start = p.start_index === undefined ? 0 : Number(p.start_index);
      const count = p.file_count === undefined ? 50 : Number(p.file_count);
      if (!Number.isFinite(start) || start < 0) invalid('start_index must be >= 0');
      if (!Number.isFinite(count) || count <= 0 || count > 200) invalid('file_count must be 1..200');
      const response = await richMediaEvent(rm, msg, 'getGroupFileList',
        [groupId, { folderId, startIndex: start, fileCount: count }],
        'onGroupFileInfoUpdate',
        (payload) => {
          if (!object(payload)) return undefined;
          // The event fires per page; only a finished page answers this request.
          if (payload.isEnd !== true) return undefined;
          return payload;
        });
      const code = Number(response.retCode);
      if (code !== 0) fail('UPSTREAM', `Group file listing failed: retCode=${code} ${response.retMsg || ''}`.trim());
      const items = Array.isArray(response.item) ? response.item : [];
      return {
        group_id: Number(groupId),
        folder_id: folderId,
        files: items.map(item => normalizeFileEntry(item, groupId)).filter(Boolean),
        all_file_count: response.allFileCount === undefined ? items.length : Number(response.allFileCount),
        next_index: response.nextIndex === undefined ? 0 : Number(response.nextIndex),
      };
    }
    if (op === 'group.file.count') {
      const groupId = id(p.group_id, 'group_id');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.batchGetGroupFileCount([groupId]));
      const counts = Array.isArray(response.groupFileCounts) ? response.groupFileCounts : [];
      return { group_id: Number(groupId), file_count: counts.length ? Number(counts[0]) : 0 };
    }
    if (op === 'group.file.info') {
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.getGroupFileInfo(groupId, fileId));
      // The info payload nests the entry; keep both so a caller can see the whole thing.
      return { group_id: Number(groupId), file_id: fileId, info: response };
    }
    if (op === 'group.file.trans') {
      // Asks the client to transfer a group file into its own pipeline.
      //
      // The kernel signature is (groupCode, fileId), and the reply nests a result object:
      // { transGroupFileResult: { result: { retCode, retMsg } } }. Reading retCode out of that shape
      // rather than treating a returned object as success is the difference between reporting the
      // server's answer and reporting that a call returned.
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      const rm = service(s, 'RichMedia', ['transGroupFile']);
      const raw = await rm.transGroupFile(groupId, fileId);
      if (!object(raw)) badResult('transGroupFile response is not an object');
      const nested = object(raw.transGroupFileResult) ? raw.transGroupFileResult : {};
      const result = object(nested.result) ? nested.result : nested;
      const code = Number(result.retCode === undefined ? result.result : result.retCode);
      if (Number.isFinite(code) && code !== 0) {
        fail('UPSTREAM', `transGroupFile failed: retCode=${code} ${result.retMsg || result.errMsg || ''}`.trim());
      }
      return { group_id: Number(groupId), file_id: fileId, ok: true, raw };
    }
    if (op === 'group.file.delete.oidb'
        || op === 'group.file.rename.oidb'
        || op === 'group.file.move.oidb') {
      // OIDB variants of delete / rename / move, matching NapCat's packet layout.
      //
      // The kernel methods (RichMediaService.deleteGroupFile and friends) already work and stay as the
      // default. These exist because NapCat drives the same operations over OIDB 0x6D6 with
      // sub-services 3/4/5, and a caller that wants byte-for-byte parity with NapCat needs that path.
      // The send path also needs the native hook, since the reply carries binary fields.
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      let groupfile;
      try {
        groupfile = typeof options.loadModule === 'function'
          ? options.loadModule('headless_groupfile.cjs')
          : require('./headless_groupfile.cjs');
      } catch (error) {
        fail('UNSUPPORTED', `group file module unavailable: ${String(error && error.message || error).slice(0, 160)}`);
      }
      const sendOptions = {
        require: (typeof require === 'function') ? require : options.loadModule.require,
      };
      try {
        let result;
        if (op === 'group.file.delete.oidb') {
          result = await groupfile.deleteGroupFile(
            () => s.getMsgService(), groupId, fileId, sendOptions);
          return { group_id: Number(groupId), file_id: fileId, ok: true, via: 'oidb' };
        }
        if (op === 'group.file.rename.oidb') {
          const name = text(p.file_name ?? p.name);
          if (!name) invalid('file_name is required');
          const parent = p.folder_id === undefined ? '/' : String(p.folder_id);
          result = await groupfile.renameGroupFile(
            () => s.getMsgService(), groupId, fileId, name, parent, sendOptions);
          return { group_id: Number(groupId), file_id: fileId, file_name: name, ok: true, via: 'oidb' };
        }
        const from = p.folder_id === undefined ? '/' : String(p.folder_id);
        const to = p.target_folder_id === undefined ? '/' : String(p.target_folder_id);
        result = await groupfile.moveGroupFile(
          () => s.getMsgService(), groupId, fileId, from, to, sendOptions);
        return { group_id: Number(groupId), file_id: fileId, from_folder: from, to_folder: to,
                 ok: true, via: 'oidb' };
      } catch (error) {
        const code = error && error.code;
        const message = String((error && error.message) || error);
        if (code === 'NO_BUFFER') fail('UNSUPPORTED', message.slice(0, 260));
        if (code === 'NO_BODY' || code === 'UPSTREAM' || code === 'TRANSPORT') {
          fail('UPSTREAM', message.slice(0, 300));
        }
        throw error;
      }
    }
    if (op === 'group.file.delete.kernel') {
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      // busId identifies which storage a file sits in; it is required by the kernel, not optional.
      const busId = p.bus_id === undefined ? undefined : Number(p.bus_id);
      if (busId === undefined || !Number.isFinite(busId)) {
        invalid('bus_id is required to delete a group file; read it from group.file.list');
      }
      const rm = service(s, 'RichMedia', ['deleteGroupFile']);
      const response = resultCode(await rm.deleteGroupFile(groupId, fileId, busId));
      return { group_id: Number(groupId), file_id: fileId, result: response, via: 'kernel' };
    }
    if (op === 'group.folder.create') {
      const groupId = id(p.group_id, 'group_id');
      const name = text(p.folder_name ?? p.name);
      if (!name) invalid('folder_name is required');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.createGroupFolder(groupId, name));
      // NapCat's RichMedia declaration returns resultWithGroupItem.groupItem, not the name as an ID.
      // Read only explicit native folder IDs from this creation response. A name-only list lookup
      // cannot distinguish an existing folder or concurrent creation; never create again to retry.
      const ids = new Set();
      const addId = value => { if (typeof value === 'string' && value.trim()) ids.add(value); };
      addId(response.folderId);
      const created = response.resultWithGroupItem;
      if (object(created)) {
        addId(created.folderId);
        if (Array.isArray(created.groupItem)) {
          for (const item of created.groupItem) {
            if (object(item) && object(item.folderInfo)) addId(item.folderInfo.folderId);
          }
        }
      }
      // A creation that the service confirmed but whose response names no folder ID is still a
      // creation: `resultCode` above would have thrown otherwise. Failing here turned a folder that
      // now exists into "nothing was created", and a caller that retried made a second one. The ID
      // is left empty for the action layer to resolve, which checks the listing before and after so
      // that a pre-existing folder of the same name cannot be mistaken for this one.
      if (ids.size > 1) {
        badResult('Folder creation returned several different folderIds; refusing to guess which one is new');
      }
      if (ids.size === 0) {
        return { group_id: Number(groupId), folder_id: '', folder_name: name,
                 result: response, folder_id_unavailable: true };
      }
      return { group_id: Number(groupId), folder_id: [...ids][0], folder_name: name, result: response };
    }
    if (op === 'group.folder.delete') {
      const groupId = id(p.group_id, 'group_id');
      const folderId = text(p.folder_id);
      if (!folderId) invalid('folder_id is required');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.deleteGroupFolder(groupId, folderId));
      return { group_id: Number(groupId), folder_id: folderId, result: response };
    }
    if (op === 'group.folder.rename') {
      const groupId = id(p.group_id, 'group_id');
      const folderId = text(p.folder_id);
      const name = text(p.folder_name ?? p.name);
      if (!folderId) invalid('folder_id is required');
      if (!name) invalid('folder_name is required');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.renameGroupFolder(groupId, folderId, name));
      return { group_id: Number(groupId), folder_id: folderId, folder_name: name, result: response };
    }
    if (op === 'group.file.rename') {
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      const name = text(p.file_name ?? p.name);
      if (!fileId) invalid('file_id is required');
      if (!name) invalid('file_name is required');
      // Optional, defaulting to bus 102, for the same reason as move: NapCat marks it optional.
      const busId = p.bus_id === undefined || p.bus_id === null ? 102 : Number(p.bus_id);
      if (!Number.isFinite(busId)) invalid('bus_id must be a number');
      // Rename takes five arguments; the fifth is the parent folder, empty for the root.
      //
      // Root is '/', not '' -- the same distinction that made group.file.list return nothing when it
      // was passed an empty folder id. An absent parent_folder_id means "the root", so it maps to '/'.
      const parent = p.parent_folder_id === undefined || p.parent_folder_id === ''
        ? '/' : String(p.parent_folder_id);
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.renameGroupFile(groupId, fileId, name, busId, parent));
      return { group_id: Number(groupId), file_id: fileId, file_name: name, result: response };
    }
    if (op === 'group.file.move') {
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      // busId identifies which storage the file sits in. NapCat treats it as optional everywhere it
      // appears (`bus_id?` / `busid?`), so requiring it rejected requests both clients consider valid.
      // Group files live on bus 102 -- the constant both implementations send when they do fill it in.
      const busId = p.bus_id === undefined || p.bus_id === null ? 102 : Number(p.bus_id);
      if (!Number.isFinite(busId)) invalid('bus_id must be a number');
      // Move also takes five; the last two are the source and destination folders.
      const from = p.folder_id === undefined ? '' : String(p.folder_id);
      const to = p.target_folder_id === undefined ? '' : String(p.target_folder_id);
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const response = resultCode(await rm.moveGroupFile(groupId, fileId, busId, from, to));
      return { group_id: Number(groupId), file_id: fileId, from_folder: from, to_folder: to, result: response };
    }
    if (op === 'group.file.upload' || op === 'upload_private_file') {
      // Creates a group file, or sends a file to a friend, by sending a FILE message element.
      //
      // This is the route NapCat's own upload_group_file action takes, and it is the one that works.
      // The alternative -- building the BDH highway transfer by hand -- needs a `serviceTicket` from
      // HttpConn.0x6ff_501, and that request returns an empty buffer unless `ver` is encoded at field
      // 15 *and* the client's BDH config has already been refreshed. Both conditions were painful to
      // find and neither is needed here: the client uploads the bytes itself when asked to send a file
      // element, so the ticket, the chunk framing, the retries and the progress reporting all stay
      // inside the client where they cannot drift.
      //
      // The element needs exactly four fields. Supplying a hash or a file uuid is neither required nor
      // expected -- the client fills those in during send and replaces the element with the server's
      // identifiers. `folderId` may be empty for the group root, and C2C has no folders at all.
      const isPrivate = op === 'upload_private_file' || p.group_id === undefined;
      let groupId = 0;
      let peer;
      if (isPrivate) {
        const userId = id(p.user_id, 'user_id');
        // A C2C peer is addressed by uid, not by account number, so the mapping has to be known.
        // Guessing `u_<uin>` is wrong on this build; report the gap instead of sending into nothing.
        const peerUid = await resolveUid(userId);
        if (typeof peerUid !== 'string' || !/^u_[A-Za-z0-9_-]+$/.test(peerUid)) {
          fail('UNSUPPORTED', 'No verified peer UID for this account; a private file needs it');
        }
        peer = { chatType: 1, peerUid, guildId: '' };
      } else {
        groupId = id(p.group_id, 'group_id');
        peer = { chatType: 2, peerUid: String(groupId), guildId: '' };
      }
      // A private upload must never be handed to the group element fields by accident.
      const sourcePath = text(p.file_path ?? p.path ?? p.file);
      if (!sourcePath) invalid('file_path is required');
      const fileName = text(p.name ?? p.file_name ?? '') || basenameOf(sourcePath);
      if (!fileName) invalid('name is required when file_path has no basename');
      const folderId = isPrivate ? '' : (p.folder_id === undefined || p.folder_id === null
        ? '' : String(p.folder_id));

      // Verify the file is readable here, in the injected process. A path that exists on the caller's
      // side may not be visible from QQ's process, and the resulting failure (an empty size) would be
      // reported much later as a generic transfer error.
      let size = 0;
      try {
        const fs = require('fs');
        size = fs.statSync(sourcePath).size;
      } catch (error) {
        fail('INVALID', `file_path is not readable from the client process: ${String(
          (error && error.message) || error).slice(0, 160)}`);
      }
      if (!size) invalid('file is empty; QQ rejects zero-byte uploads');

      const msg = service(s, 'Msg', ['sendMsg', 'addKernelMsgListener', 'removeKernelMsgListener']);

      // Watch for the outcome. A successful file send lands as a message; the file id is what a caller
      // needs, and it only exists once the client has uploaded and the server has assigned it.
      //
      // The wait is deliberately shorter than the op's own deadline. An earlier version waited 25s,
      // which is longer than the action lets it run: the upload itself succeeded and the file appeared
      // in the group, but the op had already been killed, so a successful operation was reported as a
      // timeout. Losing the file id is a lesser problem than losing the whole result -- a caller can
      // still confirm the upload by listing the group.
      const confirmation = new Promise((resolve) => {
        let handle = null;
        const timer = setTimeout(() => finish(null), 8000);
        function finish(value) {
          clearTimeout(timer);
          try { if (handle !== null) msg.removeKernelMsgListener(handle); } catch (error) { /* gone */ }
          resolve(value);
        }
        try {
          handle = msg.addKernelMsgListener({
            onMsgInfoListUpdate: (payload) => {
              // The callback receives a list; scan it for our file element rather than assuming the
              // first entry is ours -- other traffic can arrive in the same batch.
              let list = payload;
              if (list && !Array.isArray(list) && Array.isArray(list[0])) list = list[0];
              if (!Array.isArray(list)) return;
              for (const item of list) {
                if (!object(item)) continue;
                const elements = Array.isArray(item.elements) ? item.elements : [];
                for (const element of elements) {
                  if (!object(element)) continue;
                  if (!object(element.fileElement)) continue;
                  const uuid = text(element.fileElement.fileUuid);
                  if (uuid) { finish({ file_id: uuid, msg_id: text(item.msgId) }); return; }
                }
              }
            },
          });
        } catch (error) {
          finish(null);
        }
      });

      const element = {
        elementType: 3, // FILE
        elementId: '',
        fileElement: {
          fileName,
          folderId,
          filePath: sourcePath,
          fileSize: String(size),
        },
      };
      const ack = await msg.sendMsg('0', peer, [element], new Map());
      if (object(ack) && Number(ack.result) !== 0 && Number(ack.result) !== -1) {
        fail('UPSTREAM', `QQ rejected the file send: ${text(ack.errMsg) || text(ack.result)}`);
      }
      // A -1 here is not decisive: the ack and the message list race, and the file can still land.
      const result = await confirmation;

      // The upload itself is what this op does, and by the time sendMsg returns the client has taken
      // the file and started transferring. The file id is extra -- it arrives on a message the client
      // may report after the action deadline, so treating its absence as failure reported a completed
      // upload as a timeout in roughly one run in three.
      //
      // Reporting success on the send, with the id when it arrives, means a caller who needs the id can
      // wait for it or look it up in the listing, while a caller who just wanted the file uploaded is
      // not made to care.
      const accepted = Boolean(result) || !(object(ack) && Number(ack.result) < 0);
      if (!accepted) {
        fail('UPSTREAM', `QQ did not accept the file send: ${text(ack && ack.errMsg) || 'no reason given'}`);
      }
      // The result shape follows the destination: a group upload reports its folder, a private one
      // has no folder and reports the recipient instead, so a caller is not handed a `folder_id`
      // that means nothing.
      const location = isPrivate
        ? { user_id: Number(p.user_id), chat_type: 'private' }
        : { group_id: Number(groupId), folder_id: folderId || '/', chat_type: 'group' };
      return {
        ...location,
        file_name: fileName,
        file_size: size,
        file_id: result ? result.file_id : '',
        msg_id: result ? result.msg_id : '',
        ok: true,
        file_id_pending: !result,
        note: result ? '' : 'upload accepted; the file id was not reported in time -- '
          + (isPrivate ? 'read it from the sent message' : 'list the group to read it'),
      };
    }
    if (op === 'private.file.download_url') {
      // A private file's download URL, or an honest account of why it is not available yet.
      //
      // Two sources, tried in order:
      //
      //   1. The received message element, if it carried `downloadUrl`. That is free and exact --
      //      but it has not been observed populated on this build, so it cannot be the only path.
      //   2. OIDB 0xE37_800 (report 15). The command and the md5 requirement are known; the request
      //      layout is not, which is why `headless_privatefile.cjs` returns the raw response.
      //
      // What is never done here: inventing a URL by templating a host and a path. A wrong URL moves
      // the failure to the download, where it is much harder to diagnose.
      const userId = id(p.user_id, 'user_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      const fileHash = text(p.file_hash ?? p.file_md5 ?? '');
      const known = text(p.download_url ?? '');
      if (known) {
        return { user_id: Number(userId), file_id: fileId, url: known, via: 'message-element' };
      }
      let privatefile;
      try {
        privatefile = typeof options.loadModule === 'function'
          ? options.loadModule('headless_privatefile.cjs')
          : require('./headless_privatefile.cjs');
      } catch (error) {
        fail('UNSUPPORTED', `private file module unavailable: ${String((error && error.message) || error).slice(0, 160)}`);
      }
      const probe = await privatefile.probePrivateFileUrl(() => s.getMsgService(), {
        peerUin: Number(userId),
        fileUuid: fileId,
        fileHash,
        isSelfSend: p.is_self_send === true || p.is_self_send === 'true',
      });
      // `routed` is the only success-shaped outcome, and even then the URL is not being returned:
      // the layout is unmapped, so the caller gets the evidence instead of a guess.
      fail('UNSUPPORTED',
        `private file URL (OIDB 0x${privatefile.CMD_NUMBER.toString(16)}_${privatefile.SUB_COMMAND}): `
        + `${probe.note}${probe.routed ? ` raw=${probe.raw_hex}` : ''}`.slice(0, 480),
        { command: probe.command, routed: probe.routed, fields: probe.fields, attempts: probe.attempts });
    }
    if (op === 'group.file.download') {
      // Returns a download URL for one group file.
      //
      // Obtained over OIDB 0x6d6_2 rather than through a kernel service -- see the header of
      // headless_groupfile.cjs. The URL is a plain HTTPS endpoint needing no cookies, so the caller
      // does the transfer; doing it here would put file I/O inside the injected context for no gain.
      const groupId = id(p.group_id, 'group_id');
      const fileId = text(p.file_id);
      if (!fileId) invalid('file_id is required');
      // The module is loaded lazily so a client without the OIDB path still gets everything else.
      //
      // Which loader to use depends on where this file is running: loaded as a module it has its own
      // require, but the injected copy is eval'd and has no require in scope. `options.loadModule`
      // is the hook the caller can supply; when absent, this file's own require works (as it does
      // for headless_oidb.cjs above).
      let groupfile;
      try {
        groupfile = typeof options.loadModule === 'function'
          ? options.loadModule('headless_groupfile.cjs')
          : require('./headless_groupfile.cjs');
      } catch (error) {
        fail('UNSUPPORTED', `group file download module unavailable: ${String(error && error.message || error).slice(0, 160)}`);
      }
      let result;
      try {
        result = await groupfile.getGroupFileDownloadUrl(
          () => s.getMsgService(), groupId, fileId, {
            thumbnail: p.thumbnail === true || p.thumbnail === 'true',
            urlType: p.url_type === undefined ? undefined : Number(p.url_type),
            preview: p.preview === true || p.preview === 'true',
            src: p.src === undefined ? undefined : Number(p.src),
            // The name the download is saved under. Passed by the caller so the file keeps the name the message
            // showed, rather than whatever the server picked at upload time.
            fileName: typeof p.file_name === 'string' ? p.file_name : undefined,
            timeoutMs: p.timeout_ms === undefined ? 10000 : Number(p.timeout_ms),
            // The hook setup needs fs/os/process.dlopen. This file's own require is used when it is
            // loaded as a module; the injected copy supplies one through loadModule's module object.
            require: (typeof require === 'function') ? require
              : (typeof options.loadModule === 'function' ? options.loadModule.require : undefined),
          });
      } catch (error) {
        // Report the specific stage that failed, because they mean different things.
        //
        // NO_BUFFER means the hook is not installed, so the client only offers the lossy string form of
        // the reply and binary fields cannot be read. That is a setup problem with a known fix, not a
        // rejected request. NO_BODY with the hook active would mean the envelope came back without a
        // download result, which would be a server-side answer worth investigating separately.
        const code = error && error.code;
        const message = String((error && error.message) || error);
        if (code === 'NO_BUFFER') {
          fail('UNSUPPORTED',
            'Group file download needs the rspbuffer path: ' + message.slice(0, 260)
            + '. Install tooling/native hook (napi2native) to enable it.');
        }
        if (code === 'NO_BODY') {
          fail('UPSTREAM', 'Group file download reply had no DownloadFileRsp body: ' + message.slice(0, 200));
        }
        if (code === 'TRANSPORT' || code === 'UPSTREAM') {
          fail('UPSTREAM', message.slice(0, 300));
        }
        throw error;
      }
      if (!result.url) badResult('Group file download returned no URL');
      return {
        group_id: Number(groupId),
        file_id: fileId,
        url: result.url,
        file_name: result.file_name,
        md5: result.md5,
        sha1: result.sha1,
        host: result.host,
      };
    }
    if (op === 'group.file.search') {
      const groupId = id(p.group_id, 'group_id');
      const keyword = text(p.keyword ?? p.word);
      if (!keyword) invalid('keyword is required');
      const rm = service(s, 'RichMedia', REQUIREMENTS[op].slice(1));
      const msg = service(s, 'Msg', ['addKernelMsgListener', 'removeKernelMsgListener']);
      // Search reports through the same file-list event, so it reuses the same wait.
      const response = await richMediaEvent(rm, msg, 'searchGroupFile',
        [groupId, { keyword, startIndex: 0, fileCount: p.file_count === undefined ? 50 : Number(p.file_count) }],
        'onGroupFileInfoUpdate',
        (payload) => (object(payload) && payload.isEnd === true ? payload : undefined));
      const code = Number(response.retCode);
      if (code !== 0) fail('UPSTREAM', `Group file search failed: retCode=${code} ${response.retMsg || ''}`.trim());
      const items = Array.isArray(response.item) ? response.item : [];
      return {
        group_id: Number(groupId),
        keyword,
        files: items.map(item => normalizeFileEntry(item, groupId)).filter(Boolean),
        all_file_count: response.allFileCount === undefined ? items.length : Number(response.allFileCount),
      };
    }
    if (op === 'message.history') {
      // Recent messages for a conversation, newest first as the kernel returns them.
      //
      // The raw records are passed through with only the fields that callers consistently need
      // hoisted to the top. Trimming to a schema would lose elements, msgMeta and the id triple
      // (msgId/msgSeq/msgRandom), and that triple is exactly what essence set/remove requires.
      const groupId = p.group_id === undefined ? undefined : id(p.group_id, 'group_id');
      const userId = p.user_id === undefined ? undefined : id(p.user_id, 'user_id');
      if (groupId === undefined && userId === undefined) invalid('group_id or user_id is required');
      let peer;
      if (groupId !== undefined) {
        peer = { chatType: 2, peerUid: String(groupId), guildId: '' };
      } else {
        const peerUid = await resolveUid(userId);
        if (typeof peerUid !== 'string' || !/^u_[A-Za-z0-9_-]+$/.test(peerUid)) {
          fail('UNSUPPORTED', 'No verified private-peer UID mapping for this conversation');
        }
        peer = { chatType: 1, peerUid, guildId: '' };
      }
      const count = p.count === undefined ? 20 : Number(p.count);
      if (!Number.isFinite(count) || count <= 0 || count > 200) invalid('count must be 1..200');
      const svc = service(s, 'Msg', REQUIREMENTS[op].slice(1));
      const response = await svc.getLatestDbMsgs(peer, count);
      if (!object(response)) badResult('Message history response is not an object');
      const list = Array.isArray(response.msgList) ? response.msgList : [];
      const messages = list.map(raw => {
        if (!object(raw)) return null;
        return {
          message_id: String(raw.msgId ?? ''),
          msg_seq: String(raw.msgSeq ?? ''),
          msg_random: String(raw.msgRandom ?? ''),
          time: numberOrUndefined(raw.msgTime),
          sender_uin: numberOrUndefined(raw.senderUin),
          sender_uid: String(raw.senderUid ?? ''),
          sender_name: String(raw.sendNickName || raw.sendMemberName || raw.sendRemarkName || ''),
          chat_type: numberOrUndefined(raw.chatType),
          peer_uid: String(raw.peerUid ?? ''),
          elements: Array.isArray(raw.elements) ? raw.elements : [],
          raw,
        };
      }).filter(Boolean);
      return { messages, count: messages.length };
    }
    if (op === 'group.essence.set' || op === 'group.essence.remove') {
      // Set or unset an essence message.
      //
      // Both take one params object, measured: {groupCode, msgId, msgSeq, msgRandom} returns
      // errCode 0 "success". msgSeq and msgRandom come from the message record; supplying only a
      // msgId is not enough, because the server identifies the message by all three.
      const groupId = id(p.group_id, 'group_id');
      const msgId = text(p.message_id ?? p.msg_id);
      if (!msgId) invalid('message_id is required');
      const msgSeq = p.msg_seq === undefined ? '' : String(p.msg_seq);
      const msgRandom = p.msg_random === undefined ? '' : String(p.msg_random);
      if (!msgSeq || !msgRandom) {
        invalid('msg_seq and msg_random are required: the server identifies an essence message by all three ids');
      }
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      const method = op === 'group.essence.set' ? 'addGroupEssence' : 'removeGroupEssence';
      const response = await svc[method]({
        groupCode: groupId, msgId, msgSeq, msgRandom,
      });
      if (!object(response)) badResult('Essence response is not an object');
      const code = Number(response.errCode);
      if (code !== 0) {
        fail('UPSTREAM', `Essence operation failed: errCode=${code} ${response.errMsg || ''}`.trim());
      }
      return { group_id: Number(groupId), message_id: msgId, ok: true };
    }
    if (op === 'group.essence.list' || op === 'group.essence.latest') {
      // Reads the essence list. Not available on this account.
      //
      // Recorded rather than guessed: errCode 5 comes back identically for a 3-person group, a
      // 59-person group and a 126-person group, so it is a restriction on the account rather than
      // "this group has no essence messages". The two-argument form (which wants a pskey) never
      // settles. NapCat avoids all of this by reading qun.qq.com over HTTP with cookies, and this
      // session exposes no cookie getter, so that route is closed too.
      //
      // The call is still attempted and its answer reported: if a future client build lifts the
      // restriction this starts working without a code change, and until then the caller gets the
      // real reason instead of a generic "unsupported".
      const groupId = id(p.group_id, 'group_id');
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      const method = op === 'group.essence.list' ? 'fetchGroupEssenceListV2' : 'getGroupLatestEssenceList';
      const response = await svc[method](groupId);
      if (!object(response)) badResult('Essence listing response is not an object');
      const code = Number(response.errCode);
      if (code === 5) {
        fail('UNSUPPORTED',
          'The client refuses essence listing for this account (errCode 5 on every group tested); '
          + 'set and remove work, listing does not. NapCat reads it over HTTP with cookies and this '
          + 'session exposes no cookie getter.');
      }
      if (code !== 0) {
        fail('UPSTREAM', `Essence listing failed: errCode=${code} ${response.errMsg || ''}`.trim());
      }
      const result = object(response.result) ? response.result : {};
      const items = Array.isArray(result.list) ? result.list
        : (Array.isArray(result.msgList) ? result.msgList : []);
      return {
        group_id: Number(groupId),
        items,
        is_end: result.isEnd === true,
        total_count: numberOrUndefined(result.totalCount),
      };
    }
    if (op.startsWith('qzone.')) {
      // Registered so the answer is specific rather than "unknown op".
      //
      // The transport is *known good*, and it was once believed not to be. The note here used to say the endpoint
      // answered `retcode 4001 not authorized` before checking credentials, which would have made Qzone unreachable
      // from this client. That reading came from a probe that omitted the `x-oidb` header: the gateway
      // `/http2rpc/gotrpc/v1/{service}/{method}` is addressed *by* that header, so without it there was no command to
      // authorize, and 4001 answered "which command is this?" rather than "are you allowed?".
      //
      // Measured since -- same session, same pskey, only that header differing:
      //
      //     no x-oidb                          -> retcode 4001
      //     x-oidb                             -> retcode 0
      //     FeedsReader/GetFriendFeeds  0x97e5/0 -> retcode 0, feed list
      //     FeedsReader/GetShuoshuoList 0x9a97/1 -> retcode 0, own posts
      //
      // What is missing is therefore not permission but the request builders: every route needs its own body shape,
      // plus a response normaliser. Until those exist this op says that, instead of claiming the client is refused.
      // See docs/qzone-findings.json; tools/probe_qzone_auth.py re-measures.
      fail('UNSUPPORTED',
        'Qzone is reachable from this client (pskey + g_tk + x-oidb all verified) '
        + 'but no request builders are implemented yet, so there is nothing to call. '
        + 'See docs/qzone-findings.json.');
    }
    if (op === 'group.shut_list') {
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      return groupEvent(svc, 'getGroupShutUpMemberList', [groupId], 'onShutUpMemberListChanged', (code, list) => {
        if (String(code) !== groupId) return undefined;
        if (!Array.isArray(list)) badResult('Mute-list event is missing its member list');
        return list.map(m => {
          if (!object(m) || m.shutUpTime === undefined) badResult('Mute-list member is missing shutUpTime');
          const normalized = normalizeMember(m, groupId);
          return { ...normalized, shut_up_time: normalized.shut_up_timestamp };
        });
      });
    }
    if (['group.members', 'group.member', 'group.admins', 'group.member_count'].includes(op)) {
      const force = bool(p, 'no_cache');
      const userId = op === 'group.member' ? id(p.user_id, 'user_id') : undefined;
      const includeOwner = op === 'group.admins' ? bool(p, 'include_owner') : false;
      const list = await members(s, groupId, force, false, op === 'group.admins' || op === 'group.member_count');
      if (session() !== s) fail('NOT_READY', 'QQNT session changed during member lookup');
      if (op === 'group.admins') return list.filter(m => m.role === 'admin' || (includeOwner && m.role === 'owner'));
      if (op === 'group.member_count') return { group_id: Number(groupId), member_count: list.length };
      if (!userId) return list;
      let member = list.find(m => String(m.user_id) === userId);
      if (!member && !force) {
        // A miss may just mean the cache predates a join, so refetch once before declaring it absent.
        // Serving a stale "no such member" would be a wrong answer, which is worse than a slow one; the
        // refetch only happens on a miss, so the common path stays cached.
        const fresh = await members(s, groupId, true);
        if (session() !== s) fail('NOT_READY', 'QQNT session changed during member lookup');
        member = fresh.find(m => String(m.user_id) === userId);
      }
      if (!member) fail('NOT_FOUND', 'Group member not found', { group_id: groupId, user_id: userId });
      return member;
    }
    if (op === 'group.info') {
      bool(p, 'no_cache');
      const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
      return groupEvent(svc, 'getGroupDetailInfo', [groupId, 2], 'onGroupDetailInfoChange', detail => {
        if (!object(detail) || String(detail.groupCode) !== groupId) return undefined;
        return normalizeGroup(detail);
      });
    }
    const svc = service(s, 'Group', REQUIREMENTS[op].slice(1));
    let response;
    if (op === 'group.mute') {
      const userId = id(p.user_id, 'user_id');
      const duration = integer(p.duration === undefined ? 1800 : p.duration, 'duration', 0, 2592000);
      const userUid = await resolveUid(userId);
      if (Date.now() >= deadline) fail('TIMEOUT', 'UID resolution exhausted action deadline; mute was not issued');
      if (session() !== s) fail('NOT_READY', 'QQNT session changed; mute was not issued');
      response = await svc.setMemberShutUp(groupId, [{ uid: userUid, timeStamp: duration }]);
    } else if (op === 'group.whole_ban') {
      response = await svc.setGroupShutUp(groupId, bool(p, 'enable', true));
    } else if (op === 'group.name') {
      response = await svc.modifyGroupName(groupId, text(p.group_name, 'group_name', false), false);
    } else if (op === 'group.remark') {
      response = await svc.modifyGroupRemark(groupId, text(p.remark === undefined ? '' : p.remark, 'remark'));
    } else if (op === 'group.leave') {
      if (bool(p, 'is_dismiss')) fail('UNSUPPORTED', 'destroyGroup is void; dismiss completion cannot be verified');
      // Never delete local messages as a side effect of the OneBot leave action.
      response = await svc.quitGroupV2({ groupCode: groupId, needDeleteLocalMsg: false });
    }
    resultCode(response);
    return {};
  }
  // Field spellings that mean the same thing, per operation.
  //
  // This mirrors the OneBot-side table in qsick/onebot/compat.py. It has to exist here as well because
  // this op endpoint is callable directly, without going through the OneBot action layer. Both tables
  // canonicalise onto the names the code below actually reads, so whichever path a request takes it
  // arrives with the same keys.
  //
  // Canonical name always wins when both are present -- NapCat's spelling takes precedence, matching
  // the OneBot side, and the losing spelling is removed rather than left behind for some later reader
  // to pick up by accident.
  const FIELD_ALIASES = Object.freeze({
    'group.file.move': {
      folder_id: ['current_parent_directory', 'parent_directory', 'from_folder'],
      target_folder_id: ['target_parent_directory', 'target_directory', 'to_folder'],
    },
    'group.file.move.oidb': {
      folder_id: ['current_parent_directory', 'parent_directory', 'from_folder'],
      target_folder_id: ['target_parent_directory', 'target_directory', 'to_folder'],
    },
    // The kernel rename reads parent_folder_id, the OIDB one reads folder_id. Both are filled so
    // either implementation works, which is why these two entries mirror each other rather than
    // sharing one alias list.
    'group.file.rename': {
      parent_folder_id: ['current_parent_directory', 'parent_directory', 'folder', 'folder_id'],
      folder_id: ['current_parent_directory', 'parent_directory', 'folder', 'folder_id'],
      file_name: ['new_name', 'name', 'new_file_name'],
    },
    'group.file.rename.oidb': {
      parent_folder_id: ['current_parent_directory', 'parent_directory', 'folder', 'folder_id'],
      folder_id: ['current_parent_directory', 'parent_directory', 'folder', 'folder_id'],
      file_name: ['new_name', 'name', 'new_file_name'],
    },
    'group.file.delete': {
      bus_id: ['busid', 'busId'],
    },
    'group.file.delete.oidb': {
      bus_id: ['busid', 'busId'],
    },
    'group.file.delete.kernel': {
      bus_id: ['busid', 'busId'],
    },
    'group.folder.create': {
      folder_name: ['name'],
    },
    'group.file.list': {
      folder_id: ['folder'],
      file_count: ['count', 'limit'],
    },
    'group.files.root': {
      file_count: ['count', 'limit'],
    },
    'group.files.folder': {
      folder_id: ['folder'],
      file_count: ['count', 'limit'],
    },
  });

  // Applies the alias table for one operation. Absent and null both count as "not supplied": clients
  // commonly serialise every schema field, and a null must not overwrite a real value sent under
  // another spelling.
  function normaliseParams(canonical, params) {
    const table = own(FIELD_ALIASES, canonical) ? FIELD_ALIASES[canonical] : null;
    if (!table) return params;
    const out = Object.assign({}, params);
    for (const key of Object.keys(table)) {
      const alternatives = table[key];
      const has = name => out[name] !== undefined && out[name] !== null;
      if (has(key)) {
        for (const alt of alternatives) if (alt !== key) delete out[alt];
        continue;
      }
      let winner = null;
      for (const alt of alternatives) {
        if (has(alt)) { winner = alt; break; }
      }
      if (winner === null) continue;
      out[key] = out[winner];
      for (const alt of alternatives) if (alt !== key) delete out[alt];
    }
    return out;
  }

  // OneBot send names.
  //
  // Not a rename: OneBot describes a target as `group_id` / `user_id` and the content as `message`, which may be
  // a plain string or an array of segments, while `message.send` takes `chat` and `elements`. Converting here --
  // before anything else looks at `op` -- keeps every caller (OneBot layer, tools, WebUI) on one path.
  const ONEBOT_SEND = Object.freeze({
    send_group_msg: 'group', send_group_message: 'group',
    send_private_msg: 'private', send_private_message: 'private'
  });

  function oneBotChat(op, params) {
    const kind = ONEBOT_SEND[op] || (String(params.message_type || '') === 'group' ? 'group' : 'private');
    const raw = kind === 'group'
      ? (params.group_id !== undefined ? params.group_id : params.group_code)
      : (params.user_id !== undefined ? params.user_id : params.qq);
    return { kind, id: raw };
  }

  function oneBotElements(message) {
    if (typeof message === 'string') return [{ type: 'text', data: { text: message } }];
    if (!Array.isArray(message)) return null;
    // Segments already have the `{type, data}` shape the codec wants, so they pass through. Only the `at` target
    // spelling differs between clients, and the codec accepts both `qq` and `uid`.
    return message.map((segment) => {
      if (typeof segment === 'string') return { type: 'text', data: { text: segment } };
      if (!object(segment)) return segment;
      const type = String(segment.type || '');
      const data = object(segment.data) ? segment.data : {};
      if (type === 'at' && data.qq !== undefined && data.uid === undefined) {
        return { type: 'at', data: { ...data, uid: String(data.qq) } };
      }
      return { type, data };
    });
  }

  function normalizeOneBotSend(op, params) {
    const isSendMsg = op === 'send_msg';
    if (!ONEBOT_SEND[op] && !isSendMsg) return null;
    const chat = oneBotChat(op, params);
    if (chat.id === undefined || chat.id === null || String(chat.id) === '') {
      invalid(`${op} requires ${chat.kind === 'group' ? 'group_id' : 'user_id'}`);
    }
    const elements = oneBotElements(params.message);
    if (!elements || !elements.length) invalid(`${op} requires a message`);
    const out = { chat: { type: chat.kind, id: String(chat.id) }, elements };
    // Auto-escape means "treat the text as plain text", which is already how a string message is encoded.
    return out;
  }

  async function execute(op, params = {}) {
    if (typeof op !== 'string' || !op) invalid('op must be a non-empty string');
    if (!object(params) || (Object.getPrototypeOf(params) !== Object.prototype && Object.getPrototypeOf(params) !== null)) invalid('params must be a plain object');
    const oneBotSend = normalizeOneBotSend(op, params);
    if (oneBotSend) { op = 'message.send'; params = oneBotSend; }
    const canonical = own(ALIASES, op) ? ALIASES[op] : op;
    // Fold client-specific field spellings before dispatch.
    //
    // The OneBot layer does this too, but only for requests that came through it. This op endpoint is
    // also called directly -- by tools and by the WebUI -- and an alias like rename_group_file resolves
    // to the OIDB op, which reads `file_name` where the alias's own caller sends `new_name`. Without
    // folding here the alias lookup succeeds and the op then reports a missing field, which reads like a
    // bug in the op rather than a naming difference.
    params = normaliseParams(canonical, params);
    const deadline = Date.now() + timeoutMs;
    try {
      // Event operations own their deadline/cleanup; do not race their cleanup.
      return canonical === 'group.list' || canonical === 'group.info' || canonical === 'group.shut_list' || canonical === 'message.send'
        ? await executeInner(canonical, params)
        : await bounded(() => executeInner(canonical, params, deadline), canonical);
    } catch (e) {
      if (e instanceof ActionError) throw e;
      throw new ActionError('SERVICE_ERROR', e instanceof Error ? e.message : String(e), { op: canonical });
    }
  }
  function capabilities() {
    const operations = {};
    let s;
    try { s = session(); } catch { /* not logged in */ }
    for (const [op, req] of Object.entries(REQUIREMENTS)) {
      try { if (!s) throw new Error('QQNT session unavailable'); service(s, req[0], req.slice(1)); operations[op] = { supported: true }; }
      catch (e) { operations[op] = { supported: false, reason: e.message }; }
    }
    operations['login.info'] = { supported: true, condition: 'requires current login identity' };
    try { service(s || {}, 'Buddy', ['getBuddyListFromCache']); service(s || {}, 'Profile', ['getCoreAndBaseInfo']); operations['friend.list'] = { supported: true, condition: 'initialized buddy/profile cache only' }; }
    catch (e) { operations['friend.list'] = { supported: false, reason: e.message }; }
    operations['friend.categories'] = { ...operations['friend.list'] };
    for (const op of ['friend.statuses', 'friend.relations']) {
      try {
        service(s || {}, 'Buddy', ['getBuddyListFromCache', 'hasBuddyList']);
        service(s || {}, 'Profile', ['getCoreAndBaseInfo']);
        operations[op] = { supported: true };
      } catch (e) { operations[op] = { supported: false, reason: e.message }; }
      operations[op].condition = 'complete initialized cache only; every buddy profile and requested fact required; no forced refresh';
    }
    operations['group.admins'].condition = 'complete member result only; role=admin, owner excluded unless include_owner=true';
    operations['group.member_count'].condition = 'count unique members in complete result, not max capacity or cached group metadata';
    operations['group.share_link'].condition = 'query only; zero-status ACK and nonempty native URL required';
    operations['group.share_ark'].condition = 'query only; zero-status ACK and native JSON-object arkJson required; does not send';
    operations['user.status'].condition = 'requires cached UID mapping and non-null profile status; no forced refresh';
    operations['message.send'] = { supported: typeof options.sendMessage === 'function', condition: 'requires injected send handler' };
    for (const [op, reason] of Object.entries(UNRESOLVED)) operations[op] = { supported: false, reason };
    operations['group.card'].condition = 'one writer call; success requires requested state in a forced complete member readback';
    operations['group.admin'].condition = 'one writer call; success requires requested role in a forced complete member readback';
    operations['group.mute'].condition = 'requires verified cached UID mapping';
    operations['group.leave'].condition = 'leave only; is_dismiss=true is unsupported';
    operations['message.recall'].condition = 'requires cached native message reference';
    return { operations, aliases: { ...ALIASES }, timeout_ms: timeoutMs };
  }
  return { execute, capabilities, resolveUid };
}

module.exports = { createActionDispatcher, ActionError };
