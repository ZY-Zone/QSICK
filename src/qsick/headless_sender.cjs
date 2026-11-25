'use strict';
// sendMsg ACK is not a delivered message. Complete only on a correlated QQ record.
const { ActionError } = require('./headless_actions.cjs');
const { createMediaPreparer } = require('./headless_media.cjs');
function createMessageSender({ getSession, getIdentity, codec, resolveUid, messageRefs, onSent, timeoutMs = 15000 }) {
  const pending = new Map();
  //: Correlation ticket -> {message_id} for sends already answered this episode, so a later FAILED record for the
  //: same ticket can be reported. Bounded: this is diagnostic, not state.
  const settled = new Map();
  const timers = new Set();
  // Media (picture/file) needs file access, md5 and rich-media staging, so the
  // host injects these preparers into the otherwise pure codec.
  const media = createMediaPreparer({ getSession });
  const fail = (code, message) => { throw new ActionError(code, message); };
  const decimal = (v, label) => {
    if (typeof v === 'number' && !Number.isSafeInteger(v)) fail('INVALID_PARAMS', label + ' lost precision');
    const s = String(v == null ? '' : v);
    if (!/^[1-9]\d*$/.test(s)) fail('INVALID_PARAMS', label + ' must be a positive decimal ID');
    return s;
  };
  function observe(records) {
    for (const raw of Array.isArray(records) ? records : [records]) {
      if (!raw || typeof raw !== 'object') continue;
      const ticket = String(raw.guildId || '');
      const job = pending.get(ticket);
      if (!job) {
        // A record for a send we already answered. QQ can revise a record in place: the first write may say
        // SUCCESS and a later one FAILED (the usual shape when an upload is rejected after the record exists).
        // That cannot retract the answer we already gave, so record it loudly -- otherwise "the send returned ok
        // but the peer never got it" has no trace anywhere, which is exactly how it was reported.
        if (ticket && (raw.sendStatus === 0 || raw.sendStatus === '0') && settled.has(ticket)) {
          const known = settled.get(ticket);
          console.error('[qsick] message ' + known.message_id + ' was confirmed sent, then QQ reported it FAILED'
            + ' (peer ' + String(raw.peerUid || '') + '); the consumer already saw success');
        }
        continue;
      }
      if (Number(raw.chatType) !== job.peer.chatType || String(raw.peerUid) !== job.peer.peerUid) continue;
      if (String(raw.senderUin || job.uin) !== job.uin) continue;
      if (raw.sendStatus === 0 || raw.sendStatus === '0') {
        job.reject(new ActionError('SERVICE_ERROR', 'QQ reported that message sending failed')); continue;
      }
      if (raw.sendStatus !== 2 && raw.sendStatus !== '2' && raw.sendStatus !== 3 && raw.sendStatus !== '3') continue;
      try {
        const mid = decimal(raw.msgId, 'returned message ID');
        if (onSent) onSent(raw);
        // Remember the confirmation briefly, keyed by the correlation ticket, so a later FAILED for the same
        // record is attributable instead of silently ignored.
        settled.set(ticket, { message_id: mid });
        while (settled.size > 200) settled.delete(settled.keys().next().value);
        const receipt = { message_id: mid, message_id_str: mid };
        // Only expose exact values from the correlated success record, never the
        // correlation ticket, a local clock, or zero placeholders for absent data.
        for (const [nativeKey, publicKey] of [['msgSeq', 'message_seq'], ['msgTime', 'time']]) {
          const value = raw[nativeKey];
          if ((typeof value === 'string' && /^(0|[1-9]\d*)$/.test(value)) ||
              (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) ||
              (typeof value === 'bigint' && value >= 0n)) receipt[publicKey] = String(value);
        }
        job.resolve(receipt);
      } catch (error) { job.reject(error); }
    }
  }
  async function send(params) {
    if (!params || !params.chat || !['group', 'private'].includes(params.chat.type)) fail('INVALID_PARAMS', 'chat must specify group/private');
    if (!Array.isArray(params.elements) || !params.elements.length) fail('INVALID_PARAMS', 'elements must be a non-empty array');
    const target = decimal(params.chat.id, 'chat.id');
    const session = getSession();
    if (!session) fail('NOT_READY', 'QQ message session is not ready');
    const service = session.getMsgService && session.getMsgService();
    const msf = session.getMSFService && session.getMSFService();
    if (!service || typeof service.sendMsg !== 'function' || typeof service.generateMsgUniqueId !== 'function' || !msf || typeof msf.getServerTime !== 'function') fail('UNSUPPORTED', 'Required message send APIs are unavailable');
    const chatType = params.chat.type === 'group' ? 2 : 1;
    const expiresAt = Date.now() + timeoutMs;
    let preparationTimer;
    const prepared = await Promise.race([
      (async () => {
        const peerUid = chatType === 2 ? target : await resolveUid(target);
        if (chatType === 1 && (typeof peerUid !== 'string' || !/^u_[A-Za-z0-9_-]+$/.test(peerUid))) fail('UNSUPPORTED', 'No verified private-peer UID mapping');
        const peer = { chatType, peerUid: String(peerUid), guildId: '' };
        let elements;
        try { elements = await codec.buildSendElements(params.elements, {
          resolveUid, chatType, peer, messageRefs,
          prepareImage: media.prepareImage, prepareFile: media.prepareFile
        }); }
        catch (error) {
          if (error instanceof codec.CodecError) throw new ActionError(String(error.code).startsWith('UNSUPPORTED') || error.code === 'UID_RESOLUTION_REQUIRED' ? 'UNSUPPORTED' : 'INVALID_PARAMS', error.message);
          throw error;
        }
        return { peer, elements };
      })(),
      new Promise((_, reject) => { preparationTimer = setTimeout(() => reject(new ActionError('TIMEOUT', 'Message preparation timed out; no send issued')), timeoutMs); })
    ]).finally(() => clearTimeout(preparationTimer));
    if (Date.now() >= expiresAt) fail('TIMEOUT', 'Message preparation deadline exhausted; no send issued');
    let { peer, elements } = prepared;
    // Uploading media happens inside QQ during sendMsg and is slower than a text
    // send, so allow extra grace — but never less than the caller asked for, and
    // capped so a misconfiguration cannot pin a request for minutes.
    const hasMedia = elements.some(e => e && (e.elementType === 2 || e.elementType === 3));
    const mediaGrace = hasMedia ? Math.max(timeoutMs, Math.min(120000, timeoutMs * 4)) : 0;
    const expires = expiresAt + mediaGrace;
    const identity = getIdentity();
    const uin = decimal(identity.uin, 'account');
    if (getSession() !== session || String(getIdentity().uin) !== uin) fail('NOT_READY', 'Session changed while preparing message');
    const ticket = decimal(service.generateMsgUniqueId(chatType, msf.getServerTime()), 'correlation ID');
    if (pending.has(ticket)) fail('INVALID_RESULT', 'QQ returned a duplicate correlation ID');
    peer.guildId = ticket;
    let timer, resolveReceipt, rejectReceipt;
    const receipt = new Promise((resolve, reject) => { resolveReceipt = resolve; rejectReceipt = reject; });
    receipt.catch(() => {}); // receipt may precede native ACK rejection
    pending.set(ticket, { peer, uin, resolve: resolveReceipt, reject: rejectReceipt });
    let timedOut = false;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        reject(new ActionError('TIMEOUT', 'QQ send confirmation timed out; delivery is unknown, do not retry blindly'));
      }, Math.max(1, expires - Date.now()));
      // A pending send must not keep the process alive on its own.
      if (typeof timer.unref === 'function') timer.unref();
      timers.add(timer);
    });
    try {
      // "0" asks QQ to allocate the native message; guildId is only correlation.
      const ackPromise = Promise.resolve().then(() => service.sendMsg('0', peer, elements, new Map())).then(ack => {
        if (!ack || (ack.result !== 0 && ack.result !== '0')) fail('SERVICE_ERROR', 'QQ rejected sendMsg: ' + String(ack && (ack.errMsg || ack.result)));
      });
      const results = await Promise.race([Promise.all([ackPromise, receipt]), deadline]);
      return results[1];
    } finally {
      clearTimeout(timer);
      timers.delete(timer);
      pending.delete(ticket);
      // Releasing removes the staged copy from QQ's media tree. On a timeout the
      // send may still be in flight natively, so deleting the file could break a
      // message QQ is about to upload; keep it and let QQ's own cache prune it.
      if (!timedOut) await media.release();
    }
  }
  function close() {
    for (const job of pending.values()) job.reject(new ActionError('NOT_READY', 'QQ session closed during send'));
    pending.clear();
    settled.clear();
    // Long media deadlines would otherwise keep the host process alive.
    for (const handle of timers) clearTimeout(handle);
    timers.clear();
    media.release().catch(() => {});
  }
  return { send, observe, close, pendingCount: () => pending.size, mediaPending: () => media.pending() };
}
module.exports = { createMessageSender };
