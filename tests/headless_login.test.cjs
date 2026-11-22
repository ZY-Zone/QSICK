'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {createQuickLogin}=require('../src/qsick/headless_login.cjs');
function setup(result={result:'0'}, options={}) {
 const state={phase:'connected'}, calls=[];
 const controller=createQuickLogin({
   getLogin:()=>({
     quickLoginWithUin:uin=>{calls.push(uin);return typeof result==='function'?result():Promise.resolve(result);},
     getMsfStatus:()=>0,
     getLoginList:async()=>({LocalLoginInfoList:[{uin:'10001',isQuickLogin:true}]})
   }),
   getState:()=>state,getLastAccount:()=>({uin:'10001',uid:'u_last'}),
   initialize:async(uin,uid)=>{assert.equal(uin,'10001');assert.equal(uid,'u_last');state.phase='initializing';},
   publish:patch=>Object.assign(state,patch),timeoutMs:25,...options
 });
 return {controller,state,calls};
}
test('manual login uses last account even without previous ready marker',async()=>{
 const f=setup();const r=await f.controller.attempt();assert.equal(r.accepted,true);assert.equal(r.phase,'initializing');assert.deepEqual(f.calls,['10001']);assert.notEqual(f.state.phase,'ready');
});
test('already ready is a no-op without native login',async()=>{
 const f=setup();f.state.phase='ready';assert.equal((await f.controller.attempt()).already_logged_in,true);assert.equal(f.calls.length,0);
});
test('failed cached credentials return native error, never create QR or ready',async()=>{
 const f=setup({result:'3',loginErrorInfo:{errMsg:'expired'}});await assert.rejects(f.controller.attempt(),e=>e.code==='LOGIN_REJECTED'&&e.message==='expired');assert.equal(f.state.phase,'login_failed');
});
test('missing account history does not invoke native method',async()=>{
 const f=setup(null,{getLastAccount:()=>null});await assert.rejects(f.controller.attempt(),e=>e.code==='NO_HISTORY');assert.equal(f.calls.length,0);
});
test('parallel attempts are rejected and timeout cannot later initialize',async()=>{
 let resolve;let initialized=0;
 const f=setup(()=>new Promise(r=>{resolve=r;}),{initialize:async()=>{initialized++;}});
 const first=f.controller.attempt();await assert.rejects(f.controller.attempt(),e=>e.code==='BUSY');
 await assert.rejects(first,e=>e.code==='TIMEOUT');resolve({result:0});await new Promise(r=>setImmediate(r));assert.equal(initialized,0);assert.equal(f.state.phase,'login_failed');
});
test('a new QR login supersedes pending quick login without overwriting its state',async()=>{
 let resolve;
 const f=setup(()=>new Promise(r=>{resolve=r;}),{getLogin:()=>({
   quickLoginWithUin:()=>new Promise(r=>{resolve=r;}),
   getMsfStatus:()=>0,
   getLoginList:async()=>({LocalLoginInfoList:[{uin:'10001'}]})
 })});
 const attempt=f.controller.attempt();
 // Let the controller reach the native call before the QR flow wins the race.
 for(let i=0;i<5;i++) await Promise.resolve();
 f.controller.invalidate();f.state.phase='ready';
 await assert.rejects(attempt,e=>e.code==='SUPERSEDED');
 resolve({result:0});assert.equal(f.state.phase,'ready');
});
test('a mismatching returned account cannot initialize session',async()=>{
 const f=setup({result:0,uin:'20002'});await assert.rejects(f.controller.attempt(),e=>e.code==='ACCOUNT_MISMATCH');
});
test('failed session init remains a failure, not a login success',async()=>{
 const f=setup({result:0},{initialize:async()=>{throw new Error('init failed');}});
 await assert.rejects(f.controller.attempt(),/init failed/);assert.equal(f.state.phase,'login_failed');
});


test('quick login is refused when QQ has no saved ticket for the account', async () => {
  let called = false;
  const state = { phase: 'connected' };
  const controller = createQuickLogin({
    getLogin: () => ({
      quickLoginWithUin: () => { called = true; return Promise.resolve({ result: '0' }); },
      getMsfStatus: () => 0,
      getLoginList: async () => ({ LocalLoginInfoList: [] }),
    }),
    getState: () => state, getLastAccount: () => ({ uin: '10001', uid: 'u_last' }),
    initialize: async () => {}, publish: patch => Object.assign(state, patch), timeoutMs: 50,
  });
  await assert.rejects(controller.attempt(), e => e.code === 'NO_TICKET');
  assert.equal(called, false, 'must not dial QQ without a ticket');
});

test('a listed ticket is allowed through to QQ', async () => {
  const state = { phase: 'connected' };
  const seen = [];
  const controller = createQuickLogin({
    getLogin: () => ({
      quickLoginWithUin: uin => { seen.push(uin); return Promise.resolve({ result: '0' }); },
      getMsfStatus: () => 1,
      getLoginList: async () => ({ LocalLoginInfoList: [{ uin: '10001' }, { uin: '20002' }] }),
    }),
    getState: () => state, getLastAccount: () => ({ uin: '10001', uid: 'u_last' }),
    initialize: async () => { state.phase = 'initializing'; }, publish: patch => Object.assign(state, patch), timeoutMs: 50,
  });
  const result = await controller.attempt();
  assert.equal(result.accepted, true);
  assert.deepEqual(seen, ['10001']);
});

test('a result of 0 with an error message is still a failure', async () => {
  const state = { phase: 'connected' };
  const controller = createQuickLogin({
    getLogin: () => ({ quickLoginWithUin: () => Promise.resolve({ result: '0', loginErrorInfo: { errMsg: '需要重新验证' } }),
      getMsfStatus: () => 0, getLoginList: async () => ({ LocalLoginInfoList: [{ uin: '10001' }] }) }),
    getState: () => state, getLastAccount: () => ({ uin: '10001', uid: 'u_last' }),
    initialize: async () => {}, publish: patch => Object.assign(state, patch), timeoutMs: 50,
  });
  await assert.rejects(controller.attempt(), e => e.code === 'LOGIN_REJECTED' && e.message === '需要重新验证');
});

test('quick login waits for the network layer instead of failing early', async () => {
  const state = { phase: 'connected' };
  let status = 3;
  setTimeout(() => { status = 0; }, 120);
  const controller = createQuickLogin({
    getLogin: () => ({ quickLoginWithUin: () => Promise.resolve({ result: '0' }),
      getMsfStatus: () => status, getLoginList: async () => ({ LocalLoginInfoList: [{ uin: '10001' }] }) }),
    getState: () => state, getLastAccount: () => ({ uin: '10001', uid: 'u_last' }),
    initialize: async () => { state.phase = 'initializing'; }, publish: patch => Object.assign(state, patch), timeoutMs: 1000,
  });
  assert.equal((await controller.attempt()).accepted, true);
});

test('unknown login list does not block a legitimate attempt', async () => {
  const state = { phase: 'connected' };
  const controller = createQuickLogin({
    getLogin: () => ({ quickLoginWithUin: () => Promise.resolve({ result: '0' }), getMsfStatus: () => 0 }),
    getState: () => state, getLastAccount: () => ({ uin: '10001', uid: 'u_last' }),
    initialize: async () => { state.phase = 'initializing'; }, publish: patch => Object.assign(state, patch), timeoutMs: 50,
  });
  assert.equal((await controller.attempt()).accepted, true);
});


test('a missing ticket does not leave the controller permanently busy', async () => {
  const state = { phase: 'connected' };
  const controller = createQuickLogin({
    getLogin: () => ({ quickLoginWithUin: () => Promise.resolve({ result: '0' }), getMsfStatus: () => 0,
      getLoginList: async () => ({ LocalLoginInfoList: [] }) }),
    getState: () => state, getLastAccount: () => ({ uin: '10001', uid: 'u_last' }),
    initialize: async () => {}, publish: patch => Object.assign(state, patch), timeoutMs: 50,
  });
  await assert.rejects(controller.attempt(), e => e.code === 'NO_TICKET');
  assert.equal(controller.isBusy(), false, 'controller stayed busy after NO_TICKET');
  // A second attempt must report the real cause, not a permanent BUSY.
  await assert.rejects(controller.attempt(), e => e.code === 'NO_TICKET');
});
