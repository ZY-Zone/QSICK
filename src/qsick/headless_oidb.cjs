'use strict';
/*
 * Minimal protobuf writer + OIDB transport for QQ's kernel services.
 *
 * No native hook is involved: QQ exposes `sendSsoCmdReqByContend(cmd, buf)` on
 * the message service, which sends an arbitrary SSO packet and resolves with
 * `{ rspbuffer }`. That single method is enough to speak OIDB.
 *
 * Contracts (NapCat): napcat-core/packet/client/nativeClient.ts sendPacket,
 * packet/transformer/oidb/oidbBase.ts for the OIDB envelope, and
 * packet/transformer/proto/oidb/Oidb.0x9067_202.ts for FetchRkey.
 */

class ProtoWriter {
  constructor() { this.parts = []; }

  _varint(value) {
    let v = BigInt(value);
    if (v < 0n) v = BigInt.asUintN(64, v);
    const out = [];
    while (v > 0x7fn) { out.push(Number((v & 0x7fn)) | 0x80); v >>= 7n; }
    out.push(Number(v));
    return Buffer.from(out);
  }

  varint(field, value) {
    this.parts.push(this._varint((BigInt(field) << 3n) | 0n), this._varint(value));
    return this;
  }

  bytes(field, value) {
    const buf = Buffer.isBuffer(value) ? value : Buffer.from(String(value), 'utf8');
    this.parts.push(this._varint((BigInt(field) << 3n) | 2n), this._varint(buf.length), buf);
    return this;
  }

  message(field, writer) {
    return this.bytes(field, writer.toBuffer());
  }

  toBuffer() { return Buffer.concat(this.parts); }
}

/** Read a varint returning [value, nextOffset]. */
function readVarint(buf, offset) {
  let result = 0n, shift = 0n, pos = offset;
  for (;;) {
    if (pos >= buf.length) throw new Error('varint truncated');
    const byte = buf[pos++];
    result |= BigInt(byte & 0x7f) << shift;
    if ((byte & 0x80) === 0) break;
    shift += 7n;
    if (shift > 70n) throw new Error('varint too long');
  }
  return [result, pos];
}

/** Decode a protobuf message into {field: [values]} with buffers for bytes. */
function decode(buf) {
  const out = new Map();
  let pos = 0;
  while (pos < buf.length) {
    let key;
    [key, pos] = readVarint(buf, pos);
    const field = Number(key >> 3n);
    const wire = Number(key & 7n);
    let value;
    if (wire === 0) {
      [value, pos] = readVarint(buf, pos);
    } else if (wire === 2) {
      let length;
      [length, pos] = readVarint(buf, pos);
      const size = Number(length);
      if (pos + size > buf.length) throw new Error('length-delimited truncated');
      value = buf.subarray(pos, pos + size);
      pos += size;
    } else if (wire === 5) {
      value = buf.readUInt32LE(pos); pos += 4;
    } else if (wire === 1) {
      value = buf.readBigUInt64LE(pos); pos += 8;
    } else {
      throw new Error('unsupported wire type ' + wire);
    }
    if (!out.has(field)) out.set(field, []);
    out.get(field).push(value);
  }
  return out;
}

const first = (map, field) => (map.get(field) || [])[0];
const all = (map, field) => map.get(field) || [];

/** OIDB envelope: {command, subCommand, body, isReserved}. */
function encodeOidb(cmd, subCmd, body, reserved) {
  return new ProtoWriter()
    .varint(1, cmd)
    .varint(2, subCmd)
    .bytes(4, body)      // body is field 4
    .varint(12, reserved === undefined ? 1 : reserved)
    .toBuffer();
}

function decodeOidb(buf) {
  // Field map per OidbSvcTrpcTcpBase: 1 command, 2 subCommand, 3 errorCode,
  // 4 body, 5 errorMsg, 12 isReserved. Swapping 3 and 4 silently turns every
  // response body into a number, so the mapping is asserted in tests.
  const map = decode(buf);
  const command = Number(first(map, 1) ?? 0n);
  const subCommand = Number(first(map, 2) ?? 0n);
  const errorCode = Number(first(map, 3) ?? 0n);
  const errorMsg = first(map, 5);
  return {
    command,
    subCommand,
    errorCode,
    errorMsg: errorMsg ? errorMsg.toString('utf8') : '',
    body: first(map, 4) ?? Buffer.alloc(0),
    raw: map,
  };
}

/** QQ routes OIDB by command string; the 0x-prefixed form is the documented one. */
function commandName(cmd, subCmd, style) {
  const hex = cmd.toString(16).toUpperCase();
  // Measured against a live QQ session: "OidbSvc.0x9067_202" routes and returns
  // result=0 with a body, while "OidbSvcTrpcTcp.0x9067_202" is not routed
  // (result=236 访问接口未配置路由) or fails to parse the request.
  const prefix = style || process.env.QSICK_OIDB_STYLE || 'plain';
  if (prefix === 'trpc') return 'OidbSvcTrpcTcp.0x' + hex + '_' + subCmd;
  if (prefix === 'plain') return 'OidbSvc.0x' + hex + '_' + subCmd;
  if (prefix === 'lower') return 'OidbSvcTrpcTcp.0x' + hex.toLowerCase() + '_' + subCmd;
  return 'OidbSvcTrpcTcp.0x' + hex + '_' + subCmd;
}

/** FetchRkey (0x9067 sub 202) request body. */
function encodeFetchRkey() {
  const head = new ProtoWriter()
    .message(1, new ProtoWriter().varint(1, 1).varint(2, 202))   // Common{requestId,command}
    .message(2, new ProtoWriter().varint(101, 2).varint(102, 1).varint(200, 0)) // Scene
    .message(3, new ProtoWriter().varint(1, 2));                 // Client{agentType}
  const key = new ProtoWriter().varint(1, 10).varint(1, 20).varint(1, 2);
  return new ProtoWriter().message(1, head).message(4, key).toBuffer();
}

/** Parse FetchRkey response into [{rkey, ttl, time, type}]. */
function decodeFetchRkeyResponse(body) {
  const root = decode(body);
  const data = first(root, 4);
  if (!data) return [];
  const list = all(decode(data), 1);
  return list.map(entry => {
    const item = decode(entry);
    const rkey = first(item, 1);
    return {
      rkey: rkey ? rkey.toString('utf8') : '',
      ttl: Number(first(item, 2) ?? 0n),
      time: Number(first(item, 4) ?? 0n),
      type: Number(first(item, 5) ?? 0n),
    };
  }).filter(item => item.rkey);
}

/**
 * Send one OIDB request through QQ's own SSO channel.
 * `getMsgService` must return the live kernel message service.
 */
async function sendOidb(getMsgService, cmd, subCmd, body, timeoutMs = 10000, style = '') {
  const service = getMsgService();
  if (!service || typeof service.sendSsoCmdReqByContend !== 'function') {
    throw new Error('sendSsoCmdReqByContend is unavailable on this QQ build');
  }
  const packet = encodeOidb(cmd, subCmd, body);
  // Which naming style routes is a property of the command, not a global fact: 0x9067 answers to
  // the plain style and rejects the trpc one, while 0x6D6 goes the other way. So the caller can name
  // one, and the resolved name is returned rather than assumed.
  const name = commandName(cmd, subCmd, style || undefined);
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`OIDB ${name} timed out after ${timeoutMs}ms`)), timeoutMs);
    if (typeof timer.unref === 'function') timer.unref();
  });
  try {
    const response = await Promise.race([
      Promise.resolve().then(() => service.sendSsoCmdReqByContend(name, packet)),
      timeout,
    ]);
    if (response && typeof response === 'object' && response.result !== undefined
        && response.result !== 0 && !response.rspbuffer && !response.rsp) {
      // QQ answers with its own envelope: {result, errMsg, rsp}.
      throw new Error(`OIDB ${name} rejected: result=${response.result} ${response.errMsg || ''}`.trim());
    }
    // QQ answers with {result, errMsg, rsp}; other builds may name it rspbuffer.
    const buffer = response && (response.rspbuffer || response.rsp);
    if (!buffer || (typeof buffer !== 'string' && !Buffer.isBuffer(buffer))) {
      // Surface the actual shape: guessing cost a whole round already.
      let shape;
      try {
        shape = response === null ? 'null' : typeof response === 'object'
          ? JSON.stringify(Object.keys(response)) + ' ' + JSON.stringify(response).slice(0, 300)
          : String(response).slice(0, 200);
      } catch (_) { shape = '<unprintable>'; }
      throw new Error(`OIDB ${name} returned no response buffer: ${shape}`);
    }
    // The resolved name travels with the answer: "which command string actually worked" is exactly
    // what a caller probing an unmapped command needs to know.
    return { ...decodeOidb(Buffer.from(buffer, typeof buffer === 'string' ? 'latin1' : undefined)), name };
  } finally { clearTimeout(timer); }
}

/** Send a raw command and report QQ's own envelope, for transport probing. */
async function probeCommand(getMsgService, name, payload) {
  const service = getMsgService();
  if (!service || typeof service.sendSsoCmdReqByContend !== 'function') return { error: 'no SSO method' };
  try {
    const response = await Promise.resolve().then(() => service.sendSsoCmdReqByContend(name, payload));
    if (!response || typeof response !== 'object') return { raw: String(response).slice(0, 120) };
    const value = response.rspbuffer || response.rsp;
    return {
      result: response.result,
      errMsg: response.errMsg,
      hasRsp: Boolean(value),
      rspType: value === undefined ? 'undefined' : (Buffer.isBuffer(value) ? 'Buffer' : typeof value),
      rspLen: value === undefined ? 0 : (Buffer.isBuffer(value) ? value.length : String(value).length),
      rspHead: value === undefined ? '' : (Buffer.isBuffer(value) ? value.toString('hex') : String(value).slice(0, 24)),
    };
  } catch (error) { return { error: String((error && error.message) || error) }; }
}

module.exports = {
  commandName, probeCommand, ProtoWriter, decode, encodeOidb, decodeOidb,
  encodeFetchRkey, decodeFetchRkeyResponse, sendOidb,
};
