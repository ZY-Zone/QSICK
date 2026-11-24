// System notices hidden inside received messages: group name changes, mutes, joins, quits, pokes.
//
// ## Why they live here and not in a listener
//
// QQ NT has no "group member changed" callback. What it does have is a message whose only element is a
// *gray tip* (`elementType` 8): a system line the client renders as "某某 撤回了一条消息" or "某某 被设为
// 管理员" in the conversation. Every implementation that emits these events reads them out of those
// elements — this is the same route, not a workaround.
//
// The other half of these events (a member being added or removed, an admin being appointed, friend and
// join requests) arrives on a *packet push* (`trpc.msg.olpush.OlPushService.MsgPush`, decoded per
// `contentHead.type`). That channel is not wired up in QSICK, so those notices are absent rather than
// guessed at — see docs/headless-events.md.
//
// ## What is read, and from where
//
// Nothing here is inferred. Each field mirrors a structure that a working client parses:
//
//     grayTipElement.subElementType 4  (group)  -> .groupElement.type
//         5  group name changed   .groupName, .memberUid
//         8  mute                 .shutUp.duration, .shutUp.member.uid, .shutUp.admin.uid
//         1  a member was added   .memberUid, .adminUid
//         2  group dissolved      (10 also: dissolved or recycled)
//         3  a member left        .memberUid
//     grayTipElement.subElementType 17 (json)   -> .jsonGrayTipElement.busiId
//         "1061"  a poke          .jsonStr.items[] each with .uid
//
// A field that is absent produces no event. A *uid* that cannot be resolved to a UIN also produces no
// event: OneBot's `user_id` is a QQ number, and writing 0 there would be inventing a user.
//
// ## What is deliberately left out
//
// The same gray tips also carry two more things, and both need a lookup this module cannot do:
//
//   * "somebody joined the group" (busiId 51) names nobody. The joiner is only identifiable by
//     diffing the group's member list before and after, which is an async service call.
//   * A message reaction (xmlElement templId 10382) keeps the reacting account and the face id inside
//     an XML document. Reading them needs a parser and a URL shape neither is confirmed offline.
//
// Emitting either with a stand-in value would put a wrong account in a notice, which is worse than a
// missing notice. They are tracked in docs/headless-events.md instead.
"use strict";

//: `grayTipElement.subElementType`.
const GRAY_SUBTYPE = Object.freeze({
  REVOKE: 1,
  PROCLAMATION: 2,
  GROUP: 4,
  GROUP_NOTIFY: 8,
  XML: 12,
  LOCAL_MSG: 13,
  WALLET: 16,
  JSON: 17,
});

//: `grayTipElement.groupElement.type`.
const GROUP_TYPE = Object.freeze({
  MEMBER_ADD: 1,
  DISBANDED: 2,
  QUIT: 3,
  CREATED: 4,
  NAME_MODIFIED: 5,
  BLOCK: 6,
  UNBLOCK: 7,
  SHUTUP: 8,
  BE_RECYCLED: 9,
  DISBAND_OR_RECYCLED: 10,
});

//: `jsonGrayTipElement.busiId` for a poke. The only json tip that identifies both parties.
const BUSI_POKE = "1061";

const INTEGER = /^-?\d+$/;

function digits(value) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) return String(value);
  if (typeof value === "string" && INTEGER.test(value) && Number(value) > 0) return String(Number(value));
  return "";
}

function parseJson(text) {
  if (typeof text !== "string" || !text) return null;
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (_) {
    return null;
  }
}

/**
 * Build an extractor.
 *
 * `resolveUin(uid)` maps a QQ uid (`u_...`) to its UIN, or returns "" when unknown. `selfUin` is the
 * logged-in account, used to tell "I was added" apart from "somebody else was".
 */
function createNoticeExtractor(options) {
  const opts = options || {};
  const resolveUin = typeof opts.resolveUin === "function" ? opts.resolveUin : () => "";
  const debug = typeof opts.debug === "function" ? opts.debug : () => {};
  const selfUin = () => String((typeof opts.selfUin === "function" ? opts.selfUin() : opts.selfUin) || "");

  // The message that carries a gray tip is itself the record of who did it: its sender is the operator
  // for every event that has no explicit one. Seeding from it means a mute by an admin whose uid was
  // never seen before still resolves -- the pair travels on the very message being parsed.
  function uinOf(uid, seeded) {
    const key = String(uid || "");
    if (!key) return "";
    const fromMessage = seeded.get(key);
    if (fromMessage) return fromMessage;
    const known = digits(resolveUin(key));
    if (known) return known;
    // Some payloads put a UIN where a uid would normally be; accept it then, and only then.
    return digits(key);
  }

  function seed(raw) {
    const pairs = new Map();
    for (const [uidKey, uinKey] of [["senderUid", "senderUin"], ["peerUid", "peerUin"]]) {
      const uid = String(raw[uidKey] || "");
      const uin = digits(raw[uinKey]);
      if (uid && uin && uid !== uin) pairs.set(uid, uin);
    }
    return pairs;
  }

  /** Every notice this message carries. Never throws: a malformed element is one lost event. */
  function inspect(raw, mapped) {
    const found = [];
    if (!raw || typeof raw !== "object" || !Array.isArray(raw.elements)) return found;
    const isGroup = Boolean(mapped && mapped.chat && mapped.chat.type === "group");
    const groupId = isGroup ? digits(mapped.chat.id) : "";
    const seeded = seed(raw);
    const context = {
      groupId,
      isGroup,
      seeded,
      senderUid: String(raw.senderUid || ""),
      senderUin: digits(raw.senderUin),
    };
    for (const element of raw.elements) {
      try {
        readElement(element, context, found);
      } catch (error) {
        debug("notice element failed: " + String(error && error.message ? error.message : error));
      }
    }
    return found;
  }

  function readElement(element, context, found) {
    if (!element || typeof element !== "object") return;
    if (String(element.elementType) !== "8") return;
    const tip = element.grayTipElement;
    if (!tip || typeof tip !== "object") return;
    const sub = Number(tip.subElementType);
    if (sub === GRAY_SUBTYPE.GROUP && tip.groupElement) readGroupElement(tip.groupElement, context, found);
    else if (sub === GRAY_SUBTYPE.JSON && tip.jsonGrayTipElement) readJsonElement(tip.jsonGrayTipElement, context, found);
  }

  // ---------------------------------------------------------------- group tips

  function readGroupElement(body, context, found) {
    const type = Number(body && body.type);
    if (!context.groupId) return;               // a group-only structure outside a group: nothing to report
    const operator = uinOf(body.memberUid || body.adminUid, context.seeded) || context.senderUin;

    if (type === GROUP_TYPE.NAME_MODIFIED) {
      const name = typeof body.groupName === "string" ? body.groupName : "";
      if (!name) return;
      found.push({
        cmd: "notice.group_name",
        payload: { group_id: context.groupId, group_name: name, operator_id: operator, user_id: operator },
      });
      return;
    }

    if (type === GROUP_TYPE.SHUTUP) {
      const shut = body.shutUp || {};
      const duration = Number(shut.duration);
      if (!Number.isFinite(duration)) return;
      const member = shut.member && shut.member.uid ? uinOf(shut.member.uid, context.seeded) : "";
      const admin = shut.admin && shut.admin.uid ? uinOf(shut.admin.uid, context.seeded) : "";
      const actor = admin || operator;
      // duration 0 lifts a mute; a whole-group mute carries no member at all.
      const subType = duration > 0 ? "ban" : "lift_ban";
      if (member) {
        found.push({
          cmd: "notice.group_ban",
          payload: { group_id: context.groupId, sub_type: subType, user_id: member,
                     operator_id: actor, duration: duration > 0 ? duration : 0 },
        });
      } else {
        found.push({
          cmd: "notice.group_whole_ban",
          payload: { group_id: context.groupId, sub_type: subType, operator_id: actor },
        });
      }
      return;
    }

    if (type === GROUP_TYPE.MEMBER_ADD) {
      const member = uinOf(body.memberUid, context.seeded);
      const admin = body.adminUid ? uinOf(body.adminUid, context.seeded) : "";
      if (!member) return;
      const me = selfUin();
      // OneBot splits "I was added" into approve/invite. Only self-joins can be told apart this way:
      // QQ does not say who invited somebody else on this tip.
      if (me && member === me) {
        found.push({
          cmd: "notice.group_increase",
          payload: { group_id: context.groupId, user_id: member, operator_id: admin || 0,
                     sub_type: admin ? "approve" : "invite" },
        });
      }
      return;
    }

    if (type === GROUP_TYPE.QUIT) {
      const member = uinOf(body.memberUid, context.seeded) || context.senderUin;
      if (!member) return;
      found.push({
        cmd: "notice.group_decrease",
        payload: { group_id: context.groupId, user_id: member, operator_id: member, sub_type: "leave" },
      });
      return;
    }

    if (type === GROUP_TYPE.DISBANDED || type === GROUP_TYPE.DISBAND_OR_RECYCLED) {
      found.push({
        cmd: "notice.group_disband",
        payload: { group_id: context.groupId, operator_id: operator },
      });
    }
    // CREATED / BLOCK / UNBLOCK / BE_RECYCLED have no OneBot v11 event. Reported as nothing rather
    // than folded into a neighbouring type.
  }

  // ----------------------------------------------------------------- json tips

  function readJsonElement(body, context, found) {
    const busi = String((body && body.busiId) || "");
    if (busi !== BUSI_POKE) return;
    const parsed = parseJson(body && body.jsonStr);
    if (!parsed) return;
    const items = Array.isArray(parsed.items) ? parsed.items : [];
    // A poke names two parties. Taking the first two uids in order is what makes it interpretable;
    // with any other count the direction is unknowable, so it is skipped.
    const named = items.filter((item) => item && String(item.uid || ""));
    if (named.length !== 2) return;
    const from = uinOf(named[0].uid, context.seeded);
    const to = uinOf(named[1].uid, context.seeded);
    if (!from || !to) return;
    const payload = { sub_type: "poke", user_id: from, target_id: to };
    if (context.groupId) payload.group_id = context.groupId;
    found.push({ cmd: "notice.notify", payload });
  }

  /** True when every element is a system tip — such a message is a notice, not a chat message. */
  function isSystemOnly(raw) {
    if (!raw || !Array.isArray(raw.elements) || !raw.elements.length) return false;
    return raw.elements.every((element) => {
      if (!element || typeof element !== "object") return false;
      return String(element.elementType) === "8";
    });
  }

  return { inspect, isSystemOnly };
}

module.exports = { createNoticeExtractor, GRAY_SUBTYPE, GROUP_TYPE, BUSI_POKE };
