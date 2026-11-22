'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const codec = require('../src/qsick/headless_codec.cjs');
const { createMessageSender } = require('../src/qsick/headless_sender.cjs');
function fixture(behavior, timeoutMs = 30) {
  let sender, args, sent;
  const session = {getMSFService:()=>({getServerTime:()=> '1700000000'}), getMsgService:()=>({
    generateMsgUniqueId:()=> '9007199254740993',
    sendMsg:(...values)=> { args=values; return behavior(values, sender); }
  })};
  sender=createMessageSender({getSession:()=>session,getIdentity:()=>({uin:'10001'}),codec,
    resolveUid:async()=> 'u_peer',messageRefs:new Map(),onSent:raw=>{sent=raw;},timeoutMs});
  return {sender,args:()=>args,sent:()=>sent};
}
const params = (type='group') => ({chat:{type,id:'20001'},elements:[{type:'text',data:{text:'  hello\n'}}]});
function receipt(args, fields={}) {return {chatType:args[1].chatType,peerUid:args[1].peerUid,guildId:args[1].guildId,senderUin:'10001',msgId:'9007199254740995',sendStatus:2,...fields};}
test('native ACK plus correlated message yields exact real ID, not correlation ID',async()=>{
  const f=fixture((args,sender)=>{sender.observe([receipt(args)]);return {result:0};});
  const result=await f.sender.send(params());
  assert.equal(f.args()[0],'0');
  assert.equal(f.args()[1].guildId,'9007199254740993');
  assert.equal(f.args()[2][0].textElement.content,'  hello\n');
  assert.equal(result.message_id,'9007199254740995');
  assert.equal(f.sent().msgId,result.message_id);
  assert.equal(f.sender.pendingCount(),0);
});
test('private send resolves actual UID before issuing send',async()=>{
  const f=fixture((args,sender)=>{sender.observe(receipt(args));return {result:'0'};});
  await f.sender.send(params('private')); assert.equal(f.args()[1].peerUid,'u_peer'); assert.equal(f.args()[1].chatType,1);
});
test('ACK alone is not success',async()=>{
  const f=fixture(()=>({result:0}));
  await assert.rejects(f.sender.send(params()),e=>e.code==='TIMEOUT'); assert.equal(f.sender.pendingCount(),0);
});
test('hung ACK has bounded timeout',async()=>{
  const f=fixture(()=>new Promise(()=>{})); await assert.rejects(f.sender.send(params()),e=>e.code==='TIMEOUT');
});
test('unrelated peer receipt cannot complete sending',async()=>{
  const f=fixture((args,sender)=>{sender.observe(receipt(args,{peerUid:'99999'}));return {result:0};});
  await assert.rejects(f.sender.send(params()),e=>e.code==='TIMEOUT');
});
test('native failure and malformed ACK do not report success',async()=>{
  for (const ack of [{result:1,errMsg:'blocked'},{}]) {
    const f=fixture(()=>ack); await assert.rejects(f.sender.send(params()),e=>e.code==='SERVICE_ERROR');
  }
});
test('failed receipt is explicit and pending state cleaned',async()=>{
  const f=fixture((args,sender)=>{sender.observe(receipt(args,{sendStatus:0}));return {result:0};});
  await assert.rejects(f.sender.send(params()),e=>e.code==='SERVICE_ERROR'); assert.equal(f.sender.pendingCount(),0);
});
test('unsupported mixed element fails before invoking native send',async()=>{
  const f=fixture(()=>({result:0}));const p=params();p.elements.push({type:'image',data:{file:'/secret'}});
  await assert.rejects(f.sender.send(p));assert.equal(f.args(),undefined);
});
test('session close rejects pending request',async()=>{
  const f=fixture((args,sender)=>{setImmediate(()=>sender.close());return {result:0};});
  await assert.rejects(f.sender.send(params()),e=>e.code==='NOT_READY');
});


test('a timed-out send does not delete the staged media file', async () => {
  const fs = require('node:fs');
  const fsp = require('node:fs/promises');
  const os = require('node:os');
  const p = require('node:path');
  const root = await fsp.mkdtemp(p.join(os.tmpdir(), 'qsick-sender-media-'));
  const source = p.join(root, 'pic.png');
  await fsp.writeFile(source, Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,0,0,0,13]));
  let staged = null;
  const session = {
    getMSFService: () => ({ getServerTime: () => '1700000000' }),
    getMsgService: () => ({
      generateMsgUniqueId: () => '9007199254740993',
      getRichMediaFilePathForGuild(arg) {
        staged = p.join(root, arg.md5HexStr + '-' + arg.fileName);
        return staged;
      },
      // Resolve the ACK but never emit a receipt, so only the deadline fires.
      sendMsg: () => Promise.resolve({ result: 0 }),
    }),
  };
  const sender = createMessageSender({
    getSession: () => session, getIdentity: () => ({ uin: '10001' }),
    codec, resolveUid: async () => 'u_peer', messageRefs: new Map(),
    onSent: () => {}, timeoutMs: 40,
  });
  // Media sends get a 2-minute grace deadline; unref it isn't possible here, so the
  // assertion is wrapped and the sender is closed to release its timers.
  const started = Date.now();
  await assert.rejects(sender.send({
    chat: { type: 'group', id: '500' },
    elements: [{ type: 'image', data: { path: source } }],
  }), e => e.code === 'TIMEOUT');
  sender.close();
  assert.ok(Date.now() - started < 5000, 'send took ' + (Date.now() - started) + 'ms, deadline was not honoured');
  // A message QQ may still be uploading must keep its staged copy.
  assert.equal(fs.existsSync(staged), true, 'staged media was deleted on timeout');
  await fsp.rm(root, { recursive: true, force: true });
});
