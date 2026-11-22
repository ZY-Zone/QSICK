'use strict';
// Evaluate the real host with fake native services, in-memory files and no sockets.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const source = fs.readFileSync(path.join(__dirname,'../src/qsick/headless_host.js'),'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture({ready=true}={}) {
  const config={id:'C0',runtime:'/fake/runtime',dataDir:'/fake/C0/data',port:9100,corePort:5754,bridgeToken:'secret',version:'test',appid:'test',qua:'test'};
  const files=new Map([['/fake/C0/config.json',JSON.stringify(config)]]), events=[];
  let loginListener,msgListener,handler;
  const msg={ addKernelMsgListener(listener){ msgListener=listener;return 'listener'; },removeKernelMsgListener(){},
    generateMsgUniqueId(){return '9007199254740993';},
    sendMsg(id,peer,elements){
      assert.equal(id,'0'); assert.equal(elements[0].textElement.content,'hello');
      msgListener.onMsgInfoListUpdate([{chatType:2,peerUid:peer.peerUid,peerUin:peer.peerUid,senderUid:'u_self',senderUin:'10001',guildId:peer.guildId,msgId:'9007199254740995',msgSeq:'1',msgTime:'1700000000',sendStatus:2,elements}]);
      return Promise.resolve({result:0});
    }
  };
  const session={init(cfg,depends,dispatcher,listener){wrapperCalls.sessionConfig=cfg;if(ready) listener.onSessionInitComplete();},getMsgService(){return ready?msg:undefined;},getMSFService:()=>({getServerTime:()=> '1700000000'})};
  const wrapperCalls={globalPaths:[],loginConfig:{},sessionConfig:{}};
  let qrRequests=0;
  const pendingTimers=[];
  const wrapper={NodeIQQNTStartupSessionWrapper:{create:()=>({start(){}})},NodeIQQNTWrapperSession:{getNTWrapperSession:()=>session},
    NodeQQNTWrapperUtil:{getNTUserDataInfoConfig:()=>'/fake/qq'},NodeIQQNTWrapperEngine:{get:()=>({initWithDeskTopConfig(cfg){wrapperCalls.globalPaths.push(cfg.global_path_config.desktopGlobalPath);}})},
    NodeIKernelLoginService:{get:()=>({initConfig(cfg){Object.assign(wrapperCalls.loginConfig,cfg);},getMachineGuid:()=> '',quickLoginWithUin:async uin=>({result:uin==='10001'?'0':'3'}),getQRCodePicture:()=>{qrRequests++;return true;},setRemerberPwd(){},addKernelLoginListener(listener){loginListener=listener;return 1;},connect:()=>true})}
  };
  const process=new EventEmitter();process.argv=['node','host.js','--config','/fake/C0/config.json'];process.pid=123;process.platform='win32';process.chdir=()=>{};process.kill=()=>{};process.exit=()=>{};
  const context={console,Buffer,Map,Set,JSON,Date,Promise,Object,String,Number,Boolean,Array,BigInt,Error,URL,process,
    setTimeout:(fn,ms)=>{pendingTimers.push({fn,ms:ms||0});return pendingTimers.length;},
    clearTimeout:(id)=>{if(id)pendingTimers[id-1]=null;},
    setInterval:()=>0,
    fetch:async(url,options)=>{events.push(JSON.parse(options.body));return {ok:true};},
    require(name){
      if(name==='fs')return {mkdirSync(){},readFileSync(name){if(!files.has(name))throw new Error('missing');return files.get(name);},writeFileSync(name,value){files.set(name,value);},appendFileSync(){},createReadStream(){throw new Error('not used');}};
      if(name==='path')return path.posix;
      if(name==='os')return {release:()=> 'test',hostname:()=> 'fixture',platform:()=> 'win32'};
      if(name==='http')return {createServer(fn){handler=fn;return {listen(){},close(){}};}};
      if(name.endsWith('wrapper.node'))return wrapper;
      if(name.startsWith('./headless_'))return require(path.join(__dirname,'../src/qsick',name));
      return require(name);
    }
  };
  vm.runInNewContext(source,context,{filename:'headless_host.js'});
  if(!loginListener){const err=files.get('/fake/C0/data/host-debug.log')||'(no debug log)';throw new Error('login listener never registered; debug=\n'+err);}
  async function request(url,body,token='secret',method='POST') {
    const req=new EventEmitter();req.url=url;req.method=method;req.headers={authorization:'Bearer '+token};
    let status,result;
    const res={writeHead(code){status=code;},end(data){result=JSON.parse(data);}};
    const completion=handler(req,res);
    queueMicrotask(()=>{if(body!==undefined)req.emit('data',Buffer.from(JSON.stringify(body)));req.emit('end');});
    await completion;return {status,result};
  }
  async function login(){loginListener.onQRCodeLoginSucceed({uin:'10001',uid:'u_self'});await flush();}
  function runTimers(minMs) {
    const due = pendingTimers.filter(t => t && t.ms >= minMs);
    due.forEach(t => { const i = pendingTimers.indexOf(t); pendingTimers[i] = null; });
    due.forEach(t => { try { t.fn(); } catch (_) {} });
    return due.length;
  }
  return {request,login,events,files,wrapperCalls,qrRequests:()=>qrRequests,runTimers,
    pushQr(data){loginListener.onQRCodeGetPicture(data);},
    receive(raw){msgListener.onRecvMsg([raw]);}};
}
test('manual quick login authenticates and uses last identity without ready marker',async()=>{
  const f=fixture();
  assert.equal((await f.request('/quick-login',{},'wrong')).status,401);
  assert.equal((await f.request('/quick-login',{})).result.code,'NO_HISTORY');
  f.files.set('/fake/C0/data/login.json',JSON.stringify({uin:'10001',uid:'u_self'}));
  const response=await f.request('/quick-login',{});
  assert.equal(response.status,200);assert.equal(response.result.accepted,true);
  assert.equal(response.result.phase,'ready');
  assert.equal((await f.request('/quick-login',{})).result.already_logged_in,true);
});

test('real host rejects unauthenticated action and unknown operations',async()=>{
  const f=fixture();
  assert.equal((await f.request('/action',{op:'login.info',params:{}},'wrong')).status,401);
  assert.equal((await f.request('/action',{op:'login.info',params:{}})).result.code,'NOT_READY');
  const r=await f.request('/action',{op:'invented',params:{}});assert.equal(r.status,501);assert.equal(r.result.code,'UNSUPPORTED');
});
test('ready session emits login only after listener and retains ready file',async()=>{
  const f=fixture();assert.equal(f.events.length,0);await f.login();
  assert.ok(f.events.some(e=>e.kind==='login' && e.payload.logged_in));
  const saved=JSON.parse(f.files.get('/fake/C0/data/session.json'));assert.equal(saved.ready,true);
  const result=await f.request('/action',{op:'login.info',params:{}});assert.equal(result.result.ok,true);assert.equal(result.result.data.uin,10001);
});
test('real host accepts structured elements and waits for confirmed send',async()=>{
  const f=fixture();await f.login();
  const r=await f.request('/action',{op:'message.send',params:{chat:{type:'group',id:'123'},elements:[{type:'text',data:{text:'hello'}}]}});
  assert.equal(r.result.ok,true);assert.equal(r.result.data.message_id,'9007199254740995');
  assert.ok(f.events.some(e=>e.kind==='message_sent' && e.payload.message_id==='9007199254740995'));
});
test('incoming nontext media is forwarded and no ID precision is lost',async()=>{
  const f=fixture();await f.login();
  f.receive({chatType:2,peerUid:'123',peerUin:'123',senderUid:'u_peer',senderUin:'234',msgId:'9007199254740997',msgTime:'1700000000',msgSeq:'2',elements:[{elementType:2,picElement:{md5HexStr:'abc',fileName:'test.png',fileSize:'123'}}]});
  await flush();const event=f.events.find(e=>e.kind==='message');
  assert.equal(event.payload.message_id,'9007199254740997');assert.equal(event.payload.elements[0].type,'image');
});


test('login state is stored under the global path QQ actually reads', async () => {
  // The ticket lives in "<root>/nt_qq/global" on Windows; sending commonPath to
  // the data root is exactly why quick login always reported an expired login.
  const f = fixture();
  const globals = f.wrapperCalls.globalPaths;
  assert.ok(globals.length > 0, 'engine init should receive a global path');
  for (const value of globals) {
    assert.match(value, /nt_qq[\\/]global$/, 'unexpected global path: ' + value);
  }
  // Session config is only written when a login actually initialises a session.
  await f.login();
  const common = f.wrapperCalls.loginConfig.commonPath;
  assert.match(common, /nt_qq[\\/]global$/, 'login commonPath must be the global dir: ' + common);
  // Account data must stay inside the per-instance directory, isolated from the
  // shared global login store.
  const accountPath = f.wrapperCalls.sessionConfig.desktopPathConfig.account_path;
  assert.ok(accountPath.startsWith('/fake/C0/data'), 'account path escaped the instance: ' + accountPath);
  assert.ok(!accountPath.endsWith('global'), 'account data must not share the global dir: ' + accountPath);
});


test('a QR produced during a quick-login attempt is still published', async () => {
  // Guarding the picture callback on quickLogin.isBusy() blanked the panel while
  // the (usually failing) cached-login attempt was in flight.
  const f = fixture();
  f.files.set('/fake/C0/data/login.json', JSON.stringify({ uin: '10001', uid: 'u_self' }));
  const picture = { pngBase64QrcodeData: 'data:image/png;base64,AAAA' };
  f.pushQr(picture);
  const status = JSON.parse(f.files.get('/fake/C0/data/status.json'));
  assert.equal(status.phase, 'waiting_qrcode');
  assert.equal(status.qr, picture.pngBase64QrcodeData);
});

test('a QR is suppressed once the session is genuinely ready', async () => {
  const f = fixture();
  await f.login();
  f.pushQr({ pngBase64QrcodeData: 'data:image/png;base64,BBBB' });
  const status = JSON.parse(f.files.get('/fake/C0/data/status.json'));
  assert.notEqual(status.phase, 'waiting_qrcode');
  assert.notEqual(status.qr, 'data:image/png;base64,BBBB');
});

test('connecting always requests a QR picture even without onLoginConnected', async () => {
  const f = fixture();
  // The native listener may never fire; the startup fallback must still ask.
  f.runTimers(2000);
  assert.ok(f.qrRequests() >= 1, 'connect should ask QQ for a QR code');
});
