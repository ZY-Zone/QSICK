// System notices hidden inside received messages: group name changes, mutes, joins, quits, pokes,
// title changes, file uploads, emoji reactions and essence-message changes.
//
// ## Why they live here and not in a listener
//
// QQ NT has no "group member changed" callback. What it does have is a message whose only element is a
// *gray tip* (`elementType` 8): a system line the client renders as "某某 撤回了一条消息" or "某某 被设为
// 管理员" in the conversation. Every implementation that emits these events reads them out of those
// elements — this is the same route, not a workaround.
//
// A group file upload is the same idea one level down: the "upload" itself is a chat message carrying a
// file element (`elementType` 3), so it is read from there rather than from a notification.
//
// The remaining events — a member being removed by force, an admin being appointed, friend and join
// requests — only exist as *packet pushes* (`trpc.msg.olpush.OlPushService.MsgPush`). Their payloads are
// decoded by `src/qsick/olpush.py`; the wire to receive them is not installed yet, so they are absent
// rather than guessed at — see docs/headless-events.md.
//
// ## What is read, and from where
//
// Every field mirrors a structure a working client parses (NapCat's `api/group.ts` + `api/msg.ts`,
// cross-checked against Lagrange's `Internal/Logic/Processors/*` and `Internal/Packets/Notify/*`):
//
//     elementType 3 (fileElement)              -> a group file upload
//     grayTipElement.subElementType 4  (group) -> .groupElement.type
//         5  group name changed   .groupName, .memberUid
//         8  mute                 .shutUp.duration, .shutUp.member.uid, .shutUp.admin.uid
//         1  a member was added   .memberUid, .adminUid
//         2  group dissolved      (10 also: dissolved or recycled)
//         3  a member left        .memberUid
//     grayTipElement.subElementType 17 (json)  -> .jsonGrayTipElement.busiId
//         "1061"  a poke          .jsonStr.items[] each with .uid
//         tail "头衔"             .items[1].param[0] = uid, .items[3].txt = the title
//         tail is an essence line .items[0].jp is a URL carrying groupCode + msgSeq
//     grayTipElement.subElementType 12 (xml), .templId "10382"
//         <gtip><qq jp="uin"/><url msgseq="..."/><face id="..."/></gtip>
//
// A field that is absent produces no event. A *uid* that cannot be resolved to a UIN also produces no
// event: OneBot's `user_id` is a QQ number, and writing 0 there would be inventing a user.
//
// ## What is deliberately left out
//
// "Somebody joined the group" (busiId 51) names nobody. The joiner is only identifiable by diffing the
// group's member list before and after, which is an async service call this module cannot make. Emitting
// it with a stand-in value would put a wrong account in a notice, which is worse than a missing notice.
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

//: `xmlElement.templId` for "somebody reacted to your message".
const TEMPL_REACTION = "10382";

//: The tail text that identifies a title-change tip. The tipless alternative — a busiId — does not exist
//: for this one, so the text is the only handle.
const TITLE_TAIL = "头衔";

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
 * The value of one attribute on the first `<tag ...>` in an XML string.
 *
 * A full parser is not needed for any of the fields here: each one is a single attribute on a
 * single tag, and the tags are distinguishable by name (`gtip`, `qq`, `url`, `face`). Written as an
 * attribute reader rather than a regex per field so a missing tag returns "" instead of `undefined`.
 */
function attributeOf(xml, tag, attribute) {
  const text = String(xml || "");
  if (!text) return "";
  const open = text.match(new RegExp(`<${tag}\\b([^>]*)>`, "i"));
  if (!open) return "";
  const pair = open[1].match(new RegExp(`\\b${attribute}\\s*=\\s*"([^"]*)"`, "i"))
    || open[1].match(new RegExp(`\\b${attribute}\\s*=\\s*'([^']*)'`, "i"));
  return pair ? pair[1] : "";
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
    const kind = String(element.elementType);
    // A group file upload is not a tip at all: it is a chat message carrying a file element. Translating
    // it here is what makes `notice.group_upload` exist — QQ never emits a separate notification for it.
    if (kind === "3") {
      readFileElement(element, context, found);
      return;
    }
    if (kind !== "8") return;
    const tip = element.grayTipElement;
    if (!tip || typeof tip !== "object") return;
    const sub = Number(tip.subElementType);
    if (sub === GRAY_SUBTYPE.GROUP && tip.groupElement) readGroupElement(tip.groupElement, context, found);
    else if (sub === GRAY_SUBTYPE.JSON && tip.jsonGrayTipElement) readJsonElement(tip.jsonGrayTipElement, context, found);
    else if (sub === GRAY_SUBTYPE.XML && tip.xmlElement) readXmlElement(tip.xmlElement, context, found);
  }

  // ---------------------------------------------------------------- file upload

  function readFileElement(element, context, found) {
    const body = element.fileElement;
    if (!body || typeof body !== "object") return;
    if (!context.groupId) return;                 // a private file transfer is not a group upload
    const name = String(body.fileName || "").trim();
    if (!name) return;
    const size = Number(body.fileSize);
    // `fileBizId` is the sub-type the client shows; OneBot carries it as `busid`.
    const busid = Number(body.fileBizId);
    found.push({
      cmd: "notice.group_upload",
      payload: {
        group_id: context.groupId,
        user_id: context.senderUin || uinOf(context.senderUid, context.seeded),
        file: {
          // OneBot's `id` is an opaque handle for later downloads. QQ's own uuid is what a client can
          // act on, so it is used as-is rather than wrapped in a request-shaped identifier.
          id: String(body.fileUuid || body.fileMd5 || ""),
          name,
          size: Number.isFinite(size) ? size : 0,
          busid: Number.isFinite(busid) ? busid : 0,
        },
      },
    });
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
    const parsed = parseJson(body && body.jsonStr);
    if (!parsed) return;
    const items = Array.isArray(parsed.items) ? parsed.items : [];
    const tail = items.length ? String((items[items.length - 1] || {}).txt || "") : "";

    if (busi === BUSI_POKE) {
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
      return;
    }

    if (!context.groupId) return;

    // A title change reads its two facts out of fixed slots: the member's uid in `items[1].param[0]`,
    // the new title in `items[3].txt`. Matching on the tail text is the only way to tell this tip apart
    // — it carries no busiId of its own.
    if (tail === TITLE_TAIL) {
      const param = items[1] && Array.isArray(items[1].param) ? items[1].param : [];
      const member = uinOf(param[0], context.seeded);
      const title = items[3] ? String(items[3].txt || "") : "";
      if (!member) return;
      found.push({
        cmd: "notice.group_title",
        // An empty string is a real value here: it means the title was cleared.
        payload: { group_id: context.groupId, user_id: member, title },
      });
      return;
    }

    // An essence change links the pinned message through a URL on the first item, not through a field.
    const jump = items[0] ? String(items[0].jp || "") : "";
    if (!jump) return;
    const seq = /[?&]msgSeq=(\d+)/i.exec(jump) || /[?&]msgseq=(\d+)/i.exec(jump);
    const group = /[?&]groupCode=(\d+)/i.exec(jump);
    if (!seq) return;
    if (group && group[1] !== context.groupId) return;
    found.push({
      cmd: "notice.group_essence",
      payload: {
        group_id: context.groupId,
        message_seq: seq[1],
        // The tip says "a message was made an essence"; which one is in the URL, who did it is the
        // sender of the tip itself. Neither field is invented.
        operator_id: context.senderUin || uinOf(context.senderUid, context.seeded),
        sub_type: "add",
      },
    });
  }

  // ------------------------------------------------------------------ xml tips

  function readXmlElement(body, context, found) {
    if (String((body && body.templId) || "") !== TEMPL_REACTION) return;
    if (!context.groupId) return;
    const xml = String((body && body.content) || "");
    if (!xml) return;
    // <gtip><qq jp="<uin>"/><url msgseq="<seq>"/><face id="<face id>"/></gtip>
    const member = digits(attributeOf(xml, "qq", "jp")) || uinOf(attributeOf(xml, "qq", "jp"), context.seeded);
    const seq = digits(attributeOf(xml, "url", "msgseq"));
    const faceId = String(attributeOf(xml, "face", "id") || "");
    if (!member || !seq) return;
    found.push({
      cmd: "notice.group_msg_emoji_like",
      payload: {
        group_id: context.groupId,
        user_id: member,
        message_seq: seq,
        // A reaction is added or removed; a tip only exists for the addition of the first one.
        is_add: true,
        likes: faceId ? [{ emoji_id: faceId, count: 1 }] : [],
      },
    });
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

module.exports = { createNoticeExtractor, GRAY_SUBTYPE, GROUP_TYPE, BUSI_POKE, TEMPL_REACTION };
