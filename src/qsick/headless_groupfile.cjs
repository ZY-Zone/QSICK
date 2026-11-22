// Group file download over OIDB, following Lagrange's implementation.
//
// ## Where this comes from
//
// Lagrange (Lagrange.Core) implements group file download as an OIDB call rather than through any
// kernel service:
//
//     Service("OidbSvcTrpcTcp.0x6d6_2")
//     DownloadFileReq { groupCode:1, appId:2, busId:3, fileId:4, ... }
//     response.DownloadFileRsp { retCode:1, retMsg:2, downloadDns:5, downloadUrl:6, ... }
//
//     url = `https://${downloadDns}/ftn_handler/${hex(downloadUrl)}/?fname=`
//
// The upload counterpart is the same command with sub-service 0, delete is 3.
//
// This project already had the OIDB transport (headless_oidb.cjs: encodeOidb / sendOidb over
// sendSsoCmdReqByContend) and simply had no group-file message builders, which is why this looked
// like a missing capability. It is a missing packet, not a missing channel.
//
// ## Field numbers
//
// Taken from Lagrange's ProtoMember attributes, not guessed. A wrong field number is the kind of bug
// that returns retCode 0 with an empty url, so they are transcribed rather than inferred.
//
// ## What this does and does not do
//
// It obtains a download URL. Downloading the bytes is left to the caller: the URL is a plain HTTPS
// endpoint that needs no cookies, and doing the transfer here would put file I/O in the injected
// context for no benefit.

'use strict';

// ProtoWriter builds the packets; sendOidb is deliberately not used here because it decodes from the
// lossy string form. sendOidbRaw below is the binary-safe equivalent.
const { ProtoWriter } = require('./headless_oidb.cjs');

// OIDB 0x6d6 is the group file system command; the sub-service selects the operation.
//
// Sub-service numbers and the envelope field for each operation come from NapCat's Oidb.0x6D6.ts,
// which is the most reliable source here because this project runs in the same environment:
//
//     D6D6ReqBody { 1: file(upload), 3: download, 4: delete, 5: rename, 6: move }
//     sub-service  0               2           3         4           5
//
// Lagrange's numbers agree for upload/download/delete; NapCat is the one that pins rename and move.
const CMD = 0x6d6;
const SUB_UPLOAD = 0;
const SUB_DOWNLOAD = 2;
const SUB_DELETE = 3;
const SUB_RENAME = 4;
const SUB_MOVE = 5;

// Envelope field index for each operation's sub-message.
const FIELD_UPLOAD = 1;
const FIELD_DOWNLOAD = 3;
const FIELD_DELETE = 4;
const FIELD_RENAME = 5;
const FIELD_MOVE = 6;

// Constants the client sends for the group file system. They appear verbatim in Lagrange's builder
// and are not derived from anything available locally, so they are named and explained rather than
// inlined as magic numbers.
const APP_ID = 7;      // 群文件 application id
const BUS_ID = 102;    // 群文件 bus id

// DownloadFileReqBody, wrapped in D6D6ReqBody.
//
// The wrapping is the part that is easy to get wrong and it was: an earlier version encoded the
// download fields directly as the OIDB body, and the server answered
//
//     OidbSvc.0x6d6_2  result=0, body: "[oidb] invalid pkg and decode req failed"
//
// with errorCode 129 -- routed, but undecodable. The command's body is a oneof-style envelope:
//
//     D6D6ReqBody { 1: UploadFileReq, 2: ResendFileReq, 3: DownloadFileReq, 4: DeleteFileReq, ... }
//
// so the download fields live one level down, under field 3. Lagrange and NapCat both build it that
// way (`download: { groupUin, appId, busId, fileId }`), and the field numbers below come from
// Lagrange's ProtoMember attributes.
function encodeDownloadFileReq(groupCode, fileId, options = {}) {
  const inner = new ProtoWriter()
    .varint(1, groupCode)
    .varint(2, APP_ID)
    .varint(3, BUS_ID)
    // A string field is length-delimited UTF-8 on wire type 2, which is what bytes() writes.
    .bytes(4, Buffer.from(String(fileId), 'utf8'));
  // Optional fields that follow in the same message. Left at their defaults unless asked for:
  // sending a non-zero urlType changes which URL the server hands back.
  if (options.thumbnail) inner.varint(5, 1);
  if (options.urlType !== undefined) inner.varint(6, Number(options.urlType));
  if (options.preview) inner.varint(7, 1);
  if (options.src !== undefined) inner.varint(8, Number(options.src));
  return new ProtoWriter().bytes(3, inner.toBuffer()).toBuffer();
}

// Parses D6D6RspBody, whose field 3 is the DownloadFileRsp body.
function unwrapDownloadFileRsp(body) {
  const outer = decodeProto(body);
  const inner = outer.get(3);
  return inner === undefined ? null : Buffer.from(inner);
}

// DeleteFileReqBody. Field 5 is the file id here, not 4 -- each sub-message numbers its own fields and
// they do not line up across operations. Reading NapCat's per-operation tables is what keeps this
// straight; assuming the download layout caused a wrong packet in an earlier draft of the upload path.
function encodeDeleteFileReq(groupCode, fileId, options = {}) {
  const inner = new ProtoWriter()
    .varint(1, groupCode)
    .varint(3, BUS_ID)
    .bytes(5, Buffer.from(String(fileId), 'utf8'));
  if (options.parentFolderId !== undefined) {
    inner.bytes(4, Buffer.from(String(options.parentFolderId), 'utf8'));
  }
  return new ProtoWriter().bytes(FIELD_DELETE, inner.toBuffer()).toBuffer();
}

// RenameFileReqBody. No appId; parent folder is field 5, new name is field 6.
function encodeRenameFileReq(groupCode, fileId, newName, parentFolder) {
  const inner = new ProtoWriter()
    .varint(1, groupCode)
    .varint(3, BUS_ID)
    .bytes(4, Buffer.from(String(fileId), 'utf8'))
    .bytes(5, Buffer.from(String(parentFolder === undefined ? '/' : parentFolder), 'utf8'))
    .bytes(6, Buffer.from(String(newName), 'utf8'));
  return new ProtoWriter().bytes(FIELD_RENAME, inner.toBuffer()).toBuffer();
}

// MoveFileReqBody. appId 5 here while download uses 7 and upload uses 4 -- the app id is per operation,
// not a constant, which is why it is a parameter rather than a shared one.
function encodeMoveFileReq(groupCode, fileId, parentDirectory, targetDirectory) {
  const inner = new ProtoWriter()
    .varint(1, groupCode)
    .varint(2, 5)
    .varint(3, BUS_ID)
    .bytes(4, Buffer.from(String(fileId), 'utf8'))
    .bytes(5, Buffer.from(String(parentDirectory === undefined ? '/' : parentDirectory), 'utf8'))
    .bytes(6, Buffer.from(String(targetDirectory === undefined ? '/' : targetDirectory), 'utf8'));
  return new ProtoWriter().bytes(FIELD_MOVE, inner.toBuffer()).toBuffer();
}

// Parses DownloadFileRspBody. Only the fields a caller needs are read; the checksums are kept too
// because a caller verifying a download needs them, and dropping them would make verification
// impossible without a second call.
function decodeDownloadFileRsp(buffer) {
  const fields = decodeProto(buffer);
  const text = (n) => {
    const value = fields.get(n);
    return value === undefined ? '' : Buffer.from(value).toString('utf8');
  };
  const bytes = (n) => {
    const value = fields.get(n);
    return value === undefined ? Buffer.alloc(0) : Buffer.from(value);
  };
  const num = (n) => {
    const value = fields.get(n);
    return value === undefined ? 0 : Number(value);
  };
  return {
    retCode: num(1),
    retMsg: text(2),
    clientWording: text(3),
    downloadIp: text(4),
    downloadDns: text(5),
    downloadUrl: bytes(6),
    sha: bytes(7),
    sha3: bytes(8),
    md5: bytes(9),
    cookieVal: bytes(10),
    saveFileName: text(11),
    previewPort: num(12),
    downloadDnsHttps: text(13),
    previewPortHttps: num(14),
  };
}

// A minimal protobuf reader, kept local because the module this file imports exposes an encoder and
// a whole-packet decoder but not a per-message field reader.
function decodeProto(buffer) {
  const out = new Map();
  let offset = 0;
  const readVarint = () => {
    let result = 0n;
    let shift = 0n;
    while (offset < buffer.length) {
      const byte = buffer[offset++];
      result |= BigInt(byte & 0x7f) << shift;
      if ((byte & 0x80) === 0) break;
      shift += 7n;
    }
    return result;
  };
  while (offset < buffer.length) {
    const key = Number(readVarint());
    const fieldNumber = key >>> 3;
    const wireType = key & 0x7;
    if (wireType === 0) {
      out.set(fieldNumber, readVarint());
    } else if (wireType === 2) {
      const length = Number(readVarint());
      out.set(fieldNumber, buffer.subarray(offset, offset + length));
      offset += length;
    } else if (wireType === 5) {
      out.set(fieldNumber, buffer.subarray(offset, offset + 4));
      offset += 4;
    } else if (wireType === 1) {
      out.set(fieldNumber, buffer.subarray(offset, offset + 8));
      offset += 8;
    } else {
      break;
    }
  }
  return out;
}

// Builds the download URL the way Lagrange does: the response carries a DNS name and a hex-encoded
// path, and the two are joined here. Building it from the plain fields rather than trusting a
// ready-made url field is deliberate -- Lagrance's own code does this join, so that is the shape the
// server is known to support.
//
// `?fname=` is the name the file is saved under, so it belongs to the caller: the name in the message is more
// useful than whatever the server stored. It falls back to the response's `saveFileName` when nobody says.
function buildDownloadUrl(response, fileName) {
  if (!response.downloadUrl || response.downloadUrl.length === 0) return '';
  const host = response.downloadDnsHttps || response.downloadDns;
  if (!host) return '';
  const hex = Buffer.from(response.downloadUrl).toString('hex').toUpperCase();
  // Encoded: a name containing `&`, `#` or a space would otherwise cut the query short.
  const name = typeof fileName === 'string' && fileName ? fileName : (response.saveFileName || '');
  return `https://${host}/ftn_handler/${hex}/?fname=${encodeURIComponent(name)}`;
}

// Requests a download URL for one group file.
//
// `getMsgService` is the accessor headless_actions.cjs already uses; sendOidb takes it because
// sendSsoCmdReqByContend lives on the message service.
// Sends an OIDB command and returns the reply body as a Buffer, preferring rspbuffer.
//
// This exists instead of reusing headless_oidb.sendOidb because that one decodes from `rsp` -- the
// string form -- which is lossy for binary replies. `rspbuffer` only appears once the napi2native hook
// is installed, so the hook is installed on demand and the failure mode when it cannot be is reported
// plainly rather than producing a mangled parse.
// The native hook module.
//
// A native addon that patches the client's transport: it locates QQ's internal string allocator inside
// `wrapper.node`, then replaces the send and receive functions by RVA so binary replies survive.
//
// Deployed next to this file: headless.py copies it into the instance's `resources/app` along with the .cjs
// modules, so `__dirname` finds it. QSICK_NAPI2NATIVE still overrides.
//
const HOOK_MODULE = process.env.QSICK_NAPI2NATIVE
  || require('path').join(__dirname, 'inithook.node');

// RVAs for wrapper.node, keyed by QQ build. Taken from NapCat's napi2native.json, which ships the table
// for each supported version; 9.9.33-52230-x64 is the build this project targets.
const HOOK_RVA = {
  '9.9.33-52230-x64': { send: 'BF2B5F', recv: '23BB4DD' },
};

let hookState = { attempted: false, ok: false, reason: '' };

// Installs the native hook once per process.
//
// initHook can only be applied once -- a second call fails with "Failed to initialize send hook" --
// so the result is memoised. It patches two functions inside wrapper.node by RVA; if the RVA did not
// match this build the process would likely crash rather than fail cleanly, which is why the values
// are not guessed.
function ensureHook(requireFn) {
  if (hookState.attempted) return hookState;
  hookState = { attempted: true, ok: false, reason: '' };
  try {
    const fs = requireFn('fs');
    const rva = HOOK_RVA[process.arch === 'x64' ? '9.9.33-52230-x64' : '9.9.33-52230-x64'];
    if (!rva) {
      hookState.reason = `no hook offsets for this build (${process.platform}.${process.arch})`;
      return hookState;
    }
    if (!fs.existsSync(HOOK_MODULE)) {
      hookState.reason = `hook module missing: ${HOOK_MODULE}`;
      return hookState;
    }
    const bag = { exports: {} };
    process.dlopen(bag, HOOK_MODULE, requireFn('os').constants.dlopen.RTLD_LAZY);
    const api = bag.exports;
    if (!api || typeof api.initHook !== 'function') {
      hookState.reason = 'hook module exported no initHook';
      return hookState;
    }
    const ok = api.initHook(rva.send, rva.recv);
    hookState.ok = ok === true;
    if (!hookState.ok) hookState.reason = `initHook returned ${String(ok)}`;
  } catch (error) {
    hookState.reason = String((error && error.message) || error).slice(0, 200);
  }
  return hookState;
}

async function sendOidbRaw(getMsgService, cmd, subCmd, body, options) {
  const service = getMsgService();
  if (!service || typeof service.sendSsoCmdReqByContend !== 'function') {
    throw new Error('sendSsoCmdReqByContend is unavailable on this QQ build');
  }
  const requireFn = options.require || require;
  const hook = ensureHook(requireFn);
  const packet = encodeOidbPacket(cmd, subCmd, body);
  // Uppercase hex in the command name, matching both NapCat and what the server accepts.
  const name = 'OidbSvcTrpcTcp.0x' + cmd.toString(16).toUpperCase() + '_' + subCmd;

  const response = await Promise.resolve().then(() => service.sendSsoCmdReqByContend(name, packet));
  if (!response || typeof response !== 'object') {
    throw new Error(`OIDB ${name} returned no response object`);
  }
  if (Number(response.result) !== 0) {
    const error = new Error(
      `OIDB ${name} rejected: result=${response.result} ${response.errMsg || ''}`.trim());
    error.code = 'TRANSPORT';
    throw error;
  }
  if (!response.rspbuffer) {
    // Explain the specific cause rather than "no body": without the hook the client only offers the
    // string form, and that is a different problem from a wrong request.
    const error = new Error(hook.ok
      ? 'OIDB reply carried no rspbuffer even with the native hook installed'
      : `OIDB reply carried no rspbuffer; native hook unavailable: ${hookState.reason}`);
    error.code = 'NO_BUFFER';
    throw error;
  }
  // Decode the outer envelope here so callers get the body directly.
  const envelope = decodeOidbEnvelope(Buffer.from(response.rspbuffer));
  return envelope.body;
}

// The OIDB envelope: 1 command, 2 subCommand, 3 errorCode, 4 body, 5 errorMsg, 12 isReserved.
function encodeOidbPacket(cmd, subCmd, body) {
  return new ProtoWriter()
    .varint(1, cmd)
    .varint(2, subCmd)
    .bytes(4, body)
    .varint(12, 1)
    .toBuffer();
}

function decodeOidbEnvelope(buffer) {
  const fields = decodeProto(buffer);
  const get = (n) => fields.get(n);
  return {
    command: get(1) === undefined ? 0 : Number(get(1)),
    subCommand: get(2) === undefined ? 0 : Number(get(2)),
    errorCode: get(3) === undefined ? 0 : Number(get(3)),
    errorMsg: get(5) === undefined ? '' : Buffer.from(get(5)).toString('utf8'),
    body: get(4) === undefined ? Buffer.alloc(0) : Buffer.from(get(4)),
  };
}

// Requests a download URL for one group file.
//
// Two things had to be right, and both were wrong before:
//
// 1. Command name. `OidbSvcTrpcTcp.0x6D6_2` works; the older `OidbSvc.0x6D6_2` reaches a different
//    handler that rejects the packet. Both appear in the report's signature whitelist, which is why
//    the old name looked plausible.
//
// 2. Reading the reply. `sendSsoCmdReqByContend` returns its payload as a UTF-8 string, which destroys
//    every byte above 0x7F (measured: all collapse to U+FFFD; only 0xFD survives, by coincidence of
//    its low byte). A DownloadFileRsp is mostly binary, so the string form is useless. With NapCat's
//    napi2native hook installed the same call also returns `rspbuffer` as a real Buffer.
//
// With both fixed the server answers retCode 0 and the URL downloads: the first verified response was
// 906 bytes, downloadDns "njc-download.ftn.qq.com", a 64-byte path, and an HTTP HEAD on the resulting
// URL returned 200 with Content-Length 94765 -- exactly the file's size.
async function getGroupFileDownloadUrl(getMsgService, groupCode, fileId, options = {}) {
  const body = encodeDownloadFileReq(groupCode, fileId, options);
  const raw = await sendOidbRaw(getMsgService, CMD, SUB_DOWNLOAD, body, options);
  // The reply body is D6D6RspBody; the download result sits under field 3.
  const inner = unwrapDownloadFileRsp(raw);
  if (!inner) {
    const error = new Error('group file download: response carried no DownloadFileRsp body');
    error.code = 'NO_BODY';
    throw error;
  }
  const response = decodeDownloadFileRsp(inner);
  if (response.retCode !== 0) {
    const error = new Error(
      `group file download failed: retCode=${response.retCode} ${response.retMsg || ''}`.trim());
    error.code = 'UPSTREAM';
    error.retCode = response.retCode;
    throw error;
  }
  return {
    url: buildDownloadUrl(response, options.fileName),
    file_name: response.saveFileName,
    md5: response.md5.length ? response.md5.toString('hex') : '',
    sha1: response.sha.length ? response.sha.toString('hex') : '',
    host: response.downloadDnsHttps || response.downloadDns,
  };
}

// Decodes the shared reply used by delete / rename / move: D6D6RspBody whose envelope field holds a
// { 1: retCode, 2: retMsg, 3: clientWording } message.
function decodeCommonRsp(body, fieldIndex) {
  const outer = decodeProto(body);
  const inner = outer.get(fieldIndex);
  if (inner === undefined) return null;
  const fields = decodeProto(Buffer.from(inner));
  const text = (n) => {
    const value = fields.get(n);
    return value === undefined ? '' : Buffer.from(value).toString('utf8');
  };
  const num = (n) => {
    const value = fields.get(n);
    return value === undefined ? 0 : Number(value);
  };
  return { retCode: num(1), retMsg: text(2), clientWording: text(3) };
}

// Runs one of the mutating operations and reports its result.
//
// All three share a shape: build the envelope with the operation's field index, send, then read the
// same three response fields. Keeping them on one path means a fix to the envelope or to error
// handling lands once rather than three times.
async function runFileOperation(getMsgService, subService, fieldIndex, packet, options, label) {
  const body = await sendOidbRaw(getMsgService, CMD, subService, packet, options);
  const response = decodeCommonRsp(body, fieldIndex);
  if (!response) {
    const error = new Error(`group file ${label}: reply carried no result body`);
    error.code = 'NO_BODY';
    throw error;
  }
  if (response.retCode !== 0) {
    const error = new Error(
      `group file ${label} failed: retCode=${response.retCode} `
      + `${response.clientWording || response.retMsg || ''}`.trim());
    error.code = 'UPSTREAM';
    error.retCode = response.retCode;
    throw error;
  }
  return { ok: true, ret_code: response.retCode, wording: response.clientWording || '' };
}

// Deletes a group file. Verified against the live server: this is the same OIDB command as download,
// with sub-service 3 and the file id at field 5 of the delete sub-message.
async function deleteGroupFile(getMsgService, groupCode, fileId, options = {}) {
  const packet = encodeDeleteFileReq(groupCode, fileId, options);
  return runFileOperation(getMsgService, SUB_DELETE, FIELD_DELETE, packet, options, 'delete');
}

// Renames a group file. sub-service 4.
async function renameGroupFile(getMsgService, groupCode, fileId, newName, parentFolder, options = {}) {
  const packet = encodeRenameFileReq(groupCode, fileId, newName, parentFolder);
  return runFileOperation(getMsgService, SUB_RENAME, FIELD_RENAME, packet, options, 'rename');
}

// Moves a group file between folders. sub-service 5.
async function moveGroupFile(getMsgService, groupCode, fileId, parentDirectory, targetDirectory, options = {}) {
  const packet = encodeMoveFileReq(groupCode, fileId, parentDirectory, targetDirectory);
  return runFileOperation(getMsgService, SUB_MOVE, FIELD_MOVE, packet, options, 'move');
}

module.exports = {
  CMD, SUB_UPLOAD, SUB_DOWNLOAD, SUB_DELETE, SUB_RENAME, SUB_MOVE,
  encodeDownloadFileReq, decodeDownloadFileRsp, unwrapDownloadFileRsp,
  encodeDeleteFileReq, encodeRenameFileReq, encodeMoveFileReq, decodeCommonRsp,
  buildDownloadUrl, getGroupFileDownloadUrl,
  deleteGroupFile, renameGroupFile, moveGroupFile,
  // Exposed for tests and for callers that want the raw envelope.
  sendOidbRaw, decodeOidbEnvelope, encodeOidbPacket, ensureHook, hookState: () => hookState,
};
