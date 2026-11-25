"use strict";

// Pipe envelopes deliberately differ from headless_host's HTTP envelope.
const { createNoticeExtractor } = require("./headless_notices.cjs");

function createPipeNoticeBridge(options = {}) {
  const uidByUin = options.uidByUin || new Map();
  const resolveUin = uid => {
    for (const [uin, knownUid] of uidByUin) if (knownUid === uid) return String(uin);
    return "";
  };
  const extractor = createNoticeExtractor({ resolveUin, selfUin: options.selfUin });
  function processMessage(raw, kind, codec, uin) {
    const uidPairs = {};
    for (const [number, uid] of Array.from(uidByUin).slice(-500)) {
      if (/^u_[A-Za-z0-9_-]+$/.test(uid) && /^[1-9]\d*$/.test(String(number))) uidPairs[uid] = String(number);
    }
    let mapped = null;
    try { mapped = codec.normalizeMessage(raw, uin, { resolveUin }); } catch (_) {}
    const at = Date.now();
    const events = extractor.inspect(raw, mapped).map(notice => ({
      type: "notice", uin, at, cmd: notice.cmd,
      payload: Object.assign({}, notice.payload, { uin_by_uid: uidPairs }),
    }));
    // Sent callbacks are also RPC completion evidence, even for unusual native elements.
    if (kind === "sent" || !extractor.isSystemOnly(raw)) {
      events.push({ type: kind || "message", uin, at, protocol: mapped, message: raw });
    }
    return { mapped, events };
  }
  return { processMessage };
}

module.exports = { createPipeNoticeBridge };
