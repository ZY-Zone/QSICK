// Private (C2C) file download URL: OIDB 0xE37_800.
//
// ## What is known, and what is not
//
// Report 15 (§五.2 and §十一 note ②) states the private-file URL comes from a different command than
// the group one:
//
//     | 项        | 群文件       | 私聊文件            |
//     | OIDB 命令 | 0x6D6_2      | 0xE37_800           |
//     | URL 来源  | 字段 5 + 6   | result.server/port/url |
//     | scheme    | https://     | http://（明文）      |
//     | 额外参数  | —            | &isthumb=0          |
//     | 需要 md5  | 否           | 是                  |
//
// and the same report lists "私聊文件 0xE37_800 的完整协议" as **not covered** ("本次未展开"). So the
// command number, the md5 requirement and the *shape of the response* are known; the request field
// numbers are not.
//
// ## What has now been measured (2026-11-26, QQ 9.9.33-52230)
//
// The command is real and routed. Through `oidb.call` (`OidbSvc.0xE37_800`, plain style):
//
//   body (uin, appId=7, busId=102, uuid)                 -> errorCode 253 "[oidb] invalid pkg and decode req failed"
//   + md5 as field 5 (string or raw bytes)               -> 253
//   peer uid instead of uin, in several arrangements     -> 154 "[oidb] time out"
//   uuid alone                                           -> 154
//
// So 253 means "the packet does not parse" and 154 means "parsed, but the request did not complete".
// The uid-based bodies get past the parser, which is the most useful single fact here: it says the
// peer must be named by **uid**, and that what is still missing is a field the server needs to
// finish, not the envelope.
//
// The control that makes those numbers meaningful: `0x6D6_2` (group file download, known to work
// in the product) returns errorCode 0 with a 204-byte body through the same `oidb.call` channel when
// given a real group file id, and 253 when given a fake one. So a 253 here is a body problem and
// the channel itself is sound.
//
// Consequently this module still does not return a URL. What it should return once the layout is
// known: the response's host/port/path fields, joined with `&isthumb=0` over plain HTTP (report 15
// §五.2). The server points at its own iwiki page in the error text
// (`pageId=4011875093`), which is where the remaining field numbers live.
//
// ## Why this module returns raw bytes instead of a parsed URL
//
// Writing a parser for an undocumented body would mean inventing field numbers and then reporting
// whatever they happened to land on. A wrong URL handed to a caller is worse than a clear "unknown",
// because the failure moves to the download instead of staying here. So the probe:
//
//   1. builds the request from the one layout that is documented in substance -- (uin, file uuid,
//      md5, business/app id) -- and
//   2. returns the response as hex plus a best-effort field listing,
//
// which is exactly what a next session needs to finish the mapping. When the shape is confirmed the
// parser goes here and the op starts answering with a URL.

'use strict';

const APP_ID = 7;
const BUS_ID = 102;
const CMD_NUMBER = 0xe37;
const SUB_COMMAND = 800;

// Minimal protobuf writer: only the two wire types this request needs.
class Writer {
  constructor() {
    this.parts = [];
  }

  varint(field, value) {
    const bytes = [];
    let v = Number(value);
    if (!Number.isSafeInteger(v) || v < 0) throw new TypeError(`varint field ${field} is not a safe integer`);
    while (v >= 0x80) {
      bytes.push((v & 0x7f) | 0x80);
      v = Math.floor(v / 128);
    }
    bytes.push(v);
    this.parts.push(Buffer.from([(field << 3) | 0, ...bytes]));
    return this;
  }

  bytes(field, buffer) {
    const body = Buffer.isBuffer(buffer) ? buffer : Buffer.from(String(buffer), 'utf8');
    this.varintLength((field << 3) | 2, body.length);
    this.parts.push(body);
    return this;
  }

  varintLength(tag, length) {
    const bytes = [];
    let v = Number(length);
    while (v >= 0x80) {
      bytes.push((v & 0x7f) | 0x80);
      v = Math.floor(v / 128);
    }
    bytes.push(v);
    this.parts.push(Buffer.from([tag, ...bytes]));
  }

  toBuffer() {
    return Buffer.concat(this.parts);
  }
}

// Reads enough protobuf to list what came back: field number, wire type and a printable value.
//
// Deliberately tolerant. The point is to learn the real layout from one real response, so an
// unexpected wire type is reported rather than thrown -- refusing to describe a response is what
// would leave the next session as blind as this one.
function describe(buffer) {
  const fields = [];
  let offset = 0;
  const readVarint = () => {
    let result = 0;
    let shift = 1;
    while (offset < buffer.length) {
      const byte = buffer[offset++];
      result += (byte & 0x7f) * shift;
      if ((byte & 0x80) === 0) return result;
      shift *= 128;
      if (shift > Number.MAX_SAFE_INTEGER) break;
    }
    throw new RangeError('varint ran past the end of the buffer');
  };
  while (offset < buffer.length) {
    let tag;
    try {
      tag = readVarint();
    } catch (error) {
      fields.push({ error: String((error && error.message) || error) });
      break;
    }
    const field = Math.floor(tag / 8);
    const wire = tag % 8;
    try {
      if (wire === 0) {
        fields.push({ field, wire, value: readVarint() });
      } else if (wire === 2) {
        const length = readVarint();
        const slice = buffer.subarray(offset, offset + length);
        offset += length;
        const asText = slice.toString('utf8');
        // A host or path is printable ASCII; binary is summarised as hex so nothing is invented.
        const printable = /^[\x20-\x7e]*$/.test(asText);
        fields.push({
          field,
          wire,
          length,
          text: printable ? asText : undefined,
          hex: printable ? undefined : slice.toString('hex').slice(0, 120),
        });
      } else if (wire === 5) {
        fields.push({ field, wire, value: buffer.readUInt32LE(offset) });
        offset += 4;
      } else if (wire === 1) {
        fields.push({ field, wire, hex: buffer.subarray(offset, offset + 8).toString('hex') });
        offset += 8;
      } else {
        fields.push({ field, wire, unsupported: true });
        break;
      }
    } catch (error) {
      fields.push({ field, wire, error: String((error && error.message) || error) });
      break;
    }
  }
  return fields;
}

function encodeBody({ peerUin, fileUuid, fileHash, isSelfSend }) {
  // Field numbers are the open question, so the layout below is the one report 15 describes in
  // substance and is asserted as an *assumption* at the call site: a probe whose assumptions are
  // hidden reads like a specification.
  //
  //   1: peer uin        (who the conversation is with)
  //   2: app id          7
  //   3: business id     102
  //   4: file uuid       the identifier a received file element carries
  //   5: md5             required for C2C and not for groups (report 15)
  //   6: is self send    whether we sent it
  const inner = new Writer()
    .varint(1, peerUin)
    .varint(2, APP_ID)
    .varint(3, BUS_ID)
    .bytes(4, String(fileUuid));
  if (fileHash) inner.bytes(5, Buffer.from(String(fileHash), 'utf8'));
  if (isSelfSend) inner.varint(6, 1);
  return inner.toBuffer();
}

// OIDB envelope. Field map per OidbSvcTrpcTcpBase: 1 command, 2 subCommand, 4 body, 12 reserved.
function encodeEnvelope(body) {
  return new Writer()
    .varint(1, CMD_NUMBER)
    .varint(2, SUB_COMMAND)
    .bytes(4, body)
    .varint(12, 1)
    .toBuffer();
}

function commandNames() {
  // Measured once already for a different op (headless_oidb.cjs): the trpc style routes and the
  // plain style does not, on this build. Both are probed so the answer is a measurement, not a
  // recollection.
  return [
    `OidbSvcTrpcTcp.0x${CMD_NUMBER.toString(16)}_${SUB_COMMAND}`,
    `OidbSvc.0x${CMD_NUMBER.toString(16)}_${SUB_COMMAND}`,
  ];
}

/**
 * Ask the server for a private file's download URL and report what actually came back.
 *
 * Returns `{ routed, command, raw_hex, fields, note }`. `routed` is false when the client refused
 * the command outright -- that is a different answer from "routed but the body is unknown", and
 * conflating them is what makes a capability look broken when it is merely unmapped.
 */
async function probePrivateFileUrl(getMsgService, options = {}) {
  const service = getMsgService();
  if (!service || typeof service.sendSsoCmdReqByContend !== 'function') {
    return { routed: false, command: '', fields: [], note: 'sendSsoCmdReqByContend is unavailable' };
  }
  const body = encodeEnvelope(encodeBody({
    peerUin: Number(options.peerUin),
    fileUuid: options.fileUuid,
    fileHash: options.fileHash,
    isSelfSend: Boolean(options.isSelfSend),
  }));
  const attempts = [];
  for (const command of commandNames()) {
    let response;
    try {
      response = await service.sendSsoCmdReqByContend(command, body);
    } catch (error) {
      attempts.push({ command, routed: false, note: String((error && error.message) || error).slice(0, 200) });
      continue;
    }
    // The answer's payload may be `rspbuffer`, `rsp`, a string, or absent entirely. Only an actual
    // buffer-ish value is used: falling back to the response object itself made `Buffer.from` throw
    // "The first argument must be of type string or an instance of Buffer ... Received an instance of
    // Object", which surfaced as a generic SERVICE_ERROR and hid the real answer (`result: 236`).
    const candidate = response && (response.rspbuffer ?? response.rsp);
    let buffer = Buffer.alloc(0);
    if (Buffer.isBuffer(candidate)) buffer = candidate;
    else if (typeof candidate === 'string') buffer = Buffer.from(candidate, 'latin1');
    else if (candidate instanceof Uint8Array) buffer = Buffer.from(candidate);
    attempts.push({
      command,
      routed: buffer.length > 0,
      status: response && response.result !== undefined ? response.result : undefined,
      message: response && response.errMsg ? String(response.errMsg).slice(0, 200) : undefined,
      raw_hex: buffer.toString('hex').slice(0, 2000),
      fields: buffer.length ? describe(buffer) : [],
    });
    if (buffer.length) break; // a routed answer is the one worth reading
  }
  const hit = attempts.find(item => item.routed) || attempts[0] || { routed: false };
  return {
    routed: Boolean(hit.routed),
    command: hit.command || '',
    raw_hex: hit.raw_hex || '',
    fields: hit.fields || [],
    attempts,
    note: hit.routed
      ? 'routed; the body layout is not mapped yet -- read `fields` to finish it'
      : 'not routed on this build; the request layout may be wrong, not the capability',
  };
}

module.exports = {
  CMD_NUMBER,
  SUB_COMMAND,
  commandNames,
  encodeBody,
  encodeEnvelope,
  describe,
  probePrivateFileUrl,
};
