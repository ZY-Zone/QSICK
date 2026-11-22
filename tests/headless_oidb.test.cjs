'use strict';
// Offline OIDB tests: pure encode/decode plus a stubbed SSO channel.
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  ProtoWriter, decode, encodeOidb, decodeOidb,
  encodeFetchRkey, decodeFetchRkeyResponse, sendOidb,
} = require('../src/qsick/headless_oidb.cjs');

test('varint round-trips small and 64-bit values', () => {
  const buf = new ProtoWriter().varint(1, 0).varint(2, 300).varint(3, 18446744073709551615n).toBuffer();
  const map = decode(buf);
  assert.equal(Number(map.get(1)[0]), 0);
  assert.equal(Number(map.get(2)[0]), 300);
  assert.equal(map.get(3)[0], 18446744073709551615n);
});

test('bytes and nested messages decode back', () => {
  const inner = new ProtoWriter().varint(1, 7).bytes(2, 'hi');
  const buf = new ProtoWriter().bytes(1, 'abc').message(2, inner).toBuffer();
  const map = decode(buf);
  assert.equal(map.get(1)[0].toString('utf8'), 'abc');
  const nested = decode(map.get(2)[0]);
  assert.equal(Number(nested.get(1)[0]), 7);
  assert.equal(nested.get(2)[0].toString('utf8'), 'hi');
});

test('OIDB envelope carries command, sub-command and body', () => {
  const body = Buffer.from('payload');
  const encoded = encodeOidb(0x9067, 202, body);
  const parsed = decodeOidb(encoded);
  assert.deepEqual(parsed.body, body);
  // Field map: 1=command, 2=subCommand, 3=errorCode, 4=body, 12=isReserved.
  const outer = decode(encoded);
  assert.equal(Number(outer.get(1)[0]), 0x9067);
  assert.equal(Number(outer.get(2)[0]), 202);
  assert.equal(Number(outer.get(12)[0]), 1);
  assert.equal(outer.get(3), undefined);
});

test('OIDB parse surfaces a non-zero error code', () => {
  const buf = new ProtoWriter().varint(3, 12).bytes(5, 'denied').toBuffer();
  const parsed = decodeOidb(buf);
  assert.equal(parsed.errorCode, 12);
  assert.equal(parsed.errorMsg, 'denied');
});

test('OIDB body is field 4, never confused with the error code', () => {
  const body = Buffer.from('real-body');
  const buf = new ProtoWriter().varint(1, 0x9067).varint(2, 202).varint(3, 0).bytes(4, body).toBuffer();
  const parsed = decodeOidb(buf);
  assert.equal(parsed.errorCode, 0);
  assert.deepEqual(parsed.body, body);
  assert.equal(parsed.command, 0x9067);
  assert.equal(parsed.subCommand, 202);
});

test('FetchRkey request encodes the documented head and key list', () => {
  const body = encodeFetchRkey();
  const map = decode(body);
  const head = decode(map.get(1)[0]);
  const common = decode(head.get(1)[0]);
  assert.equal(Number(common.get(1)[0]), 1);     // requestId
  assert.equal(Number(common.get(2)[0]), 202);   // command
  const scene = decode(head.get(2)[0]);
  assert.equal(Number(scene.get(101)[0]), 2);    // requestType
  assert.equal(Number(scene.get(102)[0]), 1);    // businessType
  const client = decode(head.get(3)[0]);
  assert.equal(Number(client.get(1)[0]), 2);     // agentType
  const keyReq = decode(map.get(4)[0]);
  const keys = keyReq.get(1).map(Number);
  assert.deepEqual(keys, [10, 20, 2]);
});

test('FetchRkey response decodes the rkey list', () => {
  const entry = new ProtoWriter()
    .bytes(1, 'GROUP_RKEY_VALUE')
    .varint(2, 3600)
    .varint(4, 1700000000)
    .varint(5, 20);
  const data = new ProtoWriter().message(1, entry);
  const body = new ProtoWriter().message(4, data).toBuffer();
  const list = decodeFetchRkeyResponse(body);
  assert.equal(list.length, 1);
  assert.equal(list[0].rkey, 'GROUP_RKEY_VALUE');
  assert.equal(list[0].ttl, 3600);
  assert.equal(list[0].type, 20);
});

test('FetchRkey response with no list yields an empty array', () => {
  assert.deepEqual(decodeFetchRkeyResponse(new ProtoWriter().varint(1, 0).toBuffer()), []);
});

test('sendOidb refuses a service without the SSO method', async () => {
  await assert.rejects(sendOidb(() => ({}), 0x9067, 202, Buffer.alloc(0)), /unavailable/);
  await assert.rejects(sendOidb(() => null, 0x9067, 202, Buffer.alloc(0)), /unavailable/);
});

test('sendOidb sends the right command name and decodes the reply', async () => {
  const calls = [];
  const entry = new ProtoWriter().bytes(1, 'PRIVATE_RKEY').varint(2, 60).varint(5, 10);
  const data = new ProtoWriter().message(1, entry);
  const replyBody = new ProtoWriter().message(4, data).toBuffer();
  // Real OIDB response: 3 = errorCode, 4 = body, 5 = errorMsg.
  const envelope = new ProtoWriter().varint(1, 0x9067).varint(2, 202).varint(3, 0).bytes(4, replyBody).toBuffer();
  const service = {
    sendSsoCmdReqByContend(cmd, packet) {
      calls.push({ cmd, packet });
      return Promise.resolve({ rspbuffer: envelope });
    },
  };
  const result = await sendOidb(() => service, 0x9067, 202, encodeFetchRkey());
  assert.equal(calls.length, 1);
  // Measured against the live client: the trpc-prefixed name is rejected with
  // "result=236 访问接口未配置路由", while this one actually routes.
  assert.equal(calls[0].cmd, 'OidbSvc.0x9067_202');
  assert.deepEqual(calls[0].packet, encodeOidb(0x9067, 202, encodeFetchRkey()));
  const list = decodeFetchRkeyResponse(result.body);
  assert.equal(list[0].rkey, 'PRIVATE_RKEY');
});

test('sendOidb times out instead of hanging forever', async () => {
  const service = { sendSsoCmdReqByContend: () => new Promise(() => {}) };
  // Unref'd timer keeps the process alive only for the assertion window.
  await assert.rejects(sendOidb(() => service, 0x9067, 202, Buffer.alloc(0), 50), /timed out/);
});

test('sendOidb rejects a response without a buffer', async () => {
  const service = { sendSsoCmdReqByContend: () => Promise.resolve({}) };
  await assert.rejects(sendOidb(() => service, 0x9067, 202, Buffer.alloc(0)), /no response buffer/);
});
