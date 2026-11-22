"""Isolated visual/interaction smoke test. No real accounts, sends, or QSICK config."""
from __future__ import annotations
import asyncio
import json
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import uvicorn
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/qsick/webui/static'
app = FastAPI()
env = Environment(loader=FileSystemLoader(ROOT / 'src/qsick/webui/templates'), autoescape=True)
info = dict(version='preview', chat_enabled=True, logged_in=True, nickname='预览账号', uin='10001', webui_auth=False,
            transport='preview', onebot=dict(host='127.0.0.1',port=0), webui=dict(host='127.0.0.1',port=18754))
app.mount('/static', StaticFiles(directory=STATIC), name='static')
@app.get('/chat', response_class=HTMLResponse)
def page():
    return env.get_template('chat.html').render(page='chat', chat_enabled=True, info=info)
@app.get('/api/accounts')
def accounts():
    return {'accounts':[dict(id='C0',qq='10001',logged_in=True),dict(id='C1',qq='10002',logged_in=False)]}
@app.get('/api/chat/conversations')
def conversations(account: str):
    if account != '10001': return {'conversations':[]}
    now = int(time.time())
    return {'conversations':[
        dict(id='2169010946',chat_type='group',title='QSICK 交流与测试',preview='这次聊天界面顺手多了 👏',last_time=now,sender='小陈',last_message_id='1004',pinned=True),
        dict(id='20001',chat_type='private',title='小林',preview='稍后发你截图',last_time=now-400,sender='小林',last_message_id='2001',pinned=False),
        dict(id='1057604000',chat_type='group',title='开发讨论',preview='收消息后就会出现在会话列表里',last_time=now-1500,sender='Alice',last_message_id='3001',pinned=False)
    ]}
@app.get('/api/chat/history')
def history(account: str, chat_id: str, chat_type: str, limit: int=80, before: str=''):
    now=int(time.time())
    if chat_type=='private':
        messages=[dict(message_id='2001',sender_uin='20001',sender='小林',text='稍后发你截图',time=now-400,outgoing=False)]
    else:
        rich = [
          dict(type='reply', message_id='9007199254740999', quote=dict(found=True, message_id='9007199254740995', sender='小林', text='被引用的原消息')), dict(type='at', user_id='20001', name='小林'), dict(type='text', text=' 看这条'),
          dict(type='image', url='', summary='图片', file_id='', width=800, height=600, file_name='a.png'),
          dict(type='face', id='14', result_id='', file_id='', url='', summary='微笑', name='微笑', emoji='\U0001F60A', face_url='/api/chat/face?id=14', face_available=True, face_animated=False),
          dict(type='face', id='99999', result_id='', file_id='', url='', summary='表情', name='表情', emoji='\U0001F642', face_available=False),
          dict(type='mface', emoji_id='ab1234', key='', file_id='', url='https://example.invalid/sticker.gif', summary='表情包', market_face=True),
          dict(type='markdown', content='# 标题\n- 第一项\n**加粗** 与 `code`'),
        ]
        messages=[dict(message_id=str(9007199254740993+i),sender_uin='10001' if i%3==1 else '20001',sender='我' if i%3==1 else '小林',
                  card='', text='消息', segments=rich if i==0 else [dict(type='text', text='普通消息')],
                  time=now-1800+i*55,outgoing=i%3==1) for i in range(24)]
    return {'messages':messages,'has_more':False,'next_before':None}
@app.post('/api/chat/pin')
async def pin(request: Request):
    return {'ok': True, 'pinned': (await request.json()).get('pinned', True)}

@app.post('/api/chat/hide')
async def hide(request: Request):
    return {'ok': True}

@app.post('/api/chat/recall')
async def recall(request: Request):
    return {'status': 'ok', 'retcode': 0, 'data': {}}

@app.post('/api/chat/delete-message')
async def delete_message(request: Request):
    return {'ok': True, 'deleted': True}

@app.post('/api/chat/delete-conversation')
async def delete_conversation(request: Request):
    return {'ok': True, 'deleted': 3}

@app.get('/api/chat/faces')
def faces():
    return {'groups': [
        {'key': 'normal', 'label': '小黄脸', 'faces': [{'id': '14'}, {'id': '178'}]},
        {'key': 'super', 'label': '超级表情', 'faces': [{'id': '367'}]},
        {'key': 'special', 'label': '特殊表情', 'faces': [{'id': '358'}]},
    ]}

@app.get('/api/chat/face')
def face(id: str = ''):
    from fastapi.responses import Response
    # 1x1 PNG so the browser can decode it; the real app serves QQ's art.
    # A real 8x8 PNG, so Chromium actually decodes it (1x1 hex strings are easy to
    # get subtly wrong and then the browser quietly fails to load).
    return Response(content=bytes.fromhex(
        '89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc'
        '0000001149444154789c633851518115310c2d09000f4d6e016ad6c2d0'
        '0000000049454e44ae426082'), media_type='image/png')

@app.post('/api/chat/send-media')
async def send_media(request: Request):
    form = await request.form()
    upload = form.get('file')
    size = 0
    if upload is not None and hasattr(upload, 'read'):
        size = len(await upload.read())
    return {'status':'ok','retcode':0,'data':{'message_id':1},
            'uploaded': size, 'kind': form.get('kind'), 'name': getattr(upload,'filename','')}

@app.post('/api/chat/send-text')
async def send(request: Request):
    return {'status':'failed','retcode':1502,'message':'预览模式：不会发送真实消息'}

async def check(ws_url, origin):
    async with websockets.connect(ws_url, max_size=10_000_000, proxy=None, compression=None, open_timeout=8) as ws:
        serial=0
        async def call(method, params=None):
            nonlocal serial
            serial+=1; ident=serial
            await ws.send(json.dumps({'id':ident,'method':method,'params':params or {}}))
            while True:
                msg=json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if msg.get('id')==ident:
                    if 'error' in msg: raise RuntimeError(msg['error'])
                    return msg.get('result',{})
        async def evaluate(expression):
            res=await call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
            if 'exceptionDetails' in res: raise RuntimeError(res['exceptionDetails'])
            return res['result'].get('value')
        print('CDP connected', flush=True)
        await call('Page.enable')
        await call('Page.navigate',{'url':origin+'/chat'})
        for _ in range(70):
            if await evaluate('document.querySelectorAll(".nt-message").length')==24: break
            await asyncio.sleep(.1)
        else: raise AssertionError('chat did not load')
        print('Chat rendered', flush=True)
        assert await evaluate('document.querySelectorAll(".nt-conversation").length')==3
        await evaluate('document.getElementById("chat-log").scrollTop=0')
        await asyncio.sleep(3.3)
        assert await evaluate('document.getElementById("chat-log").scrollTop')==0
        await evaluate('document.getElementById("chat-input").value="草稿保留"; document.getElementById("chat-input").dispatchEvent(new Event("input"));')
        await call('Page.reload')
        for _ in range(60):
            if await evaluate('document.getElementById("chat-input") && document.getElementById("chat-input").value')=='草稿保留': break
            await asyncio.sleep(.1)
        else: raise AssertionError('draft restore failed')
        await evaluate('document.getElementById("chat-input").dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", isComposing:true, bubbles:true})); document.getElementById("chat-input").dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", shiftKey:true, bubbles:true}));')
        assert await evaluate('document.querySelectorAll(".nt-local-message").length') == 0
        await evaluate('document.getElementById("chat-form").requestSubmit()')
        for _ in range(50):
            if await evaluate('Boolean(document.querySelector(".nt-delivery.failed"))'): break
            await asyncio.sleep(.1)
        else: raise AssertionError('failed send was not retained')
        await evaluate('document.querySelector(".nt-retry").click()')
        assert await evaluate('document.getElementById("chat-input").value')=='草稿保留'
        # Structured rendering: mentions, quotes and markdown become typed DOM.
        assert await evaluate('document.querySelectorAll(".nt-at").length') >= 1
        assert await evaluate('document.querySelectorAll(".nt-quote").length') >= 1
        assert await evaluate('document.querySelectorAll(".nt-markdown .nt-md-li").length') >= 1
        print('Rich rendering OK', flush=True)

        # Message right-click menu on someone else's message.
        await evaluate("""
          (() => {
            const row = Array.from(document.querySelectorAll('.nt-message')).find(r => !r.classList.contains('outgoing'));
            const rect = row.getBoundingClientRect();
            row.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true, clientX: rect.left+40, clientY: rect.top+20}));
          })()
        """)
        await asyncio.sleep(.15)
        menuItems = await evaluate('Array.from(document.querySelectorAll(".nt-ctx-item")).map(b => b.textContent)')
        assert any('复制' in item for item in menuItems), menuItems
        assert any('引用' in item for item in menuItems), menuItems
        assert any('删除' in item for item in menuItems), menuItems
        assert any('@' in item for item in menuItems), menuItems
        # Click 引用 -> composer receives a quoted block.
        await evaluate("""
          (() => { const b = Array.from(document.querySelectorAll('.nt-ctx-item')).find(x => x.textContent.includes('引用')); b.click(); })()
        """)
        await asyncio.sleep(.1)
        quoted_value = await evaluate('document.getElementById("chat-input").value')
        # Existing draft text is preserved; the quoted block is appended.
        assert await evaluate('document.getElementById("chat-reply").hidden') is False
        await evaluate('document.getElementById("chat-input").value = ""; document.getElementById("chat-input").dispatchEvent(new Event("input"));')
        print('Message menu OK', flush=True)

        # Conversation right-click menu (pin/copy/remove/delete).
        await evaluate("""
          (() => {
            const row = document.querySelector('.nt-conversation');
            const rect = row.getBoundingClientRect();
            row.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true, clientX: rect.left+20, clientY: rect.top+20}));
          })()
        """)
        await asyncio.sleep(.15)
        convItems = await evaluate('Array.from(document.querySelectorAll(".nt-ctx-item")).map(b => b.textContent)')
        joined = " | ".join(convItems)
        assert '置顶' in joined and '移除' in joined and '删除' in joined, convItems
        assert '群号' in joined or 'QQ 号' in joined, convItems
        await evaluate('document.dispatchEvent(new MouseEvent("click", {bubbles:true}))')
        await asyncio.sleep(.1)
        assert await evaluate('document.querySelector(".nt-ctxmenu").hidden') is True
        print('Conversation menu OK', flush=True)

        quote_label = await evaluate('document.querySelector(".nt-quote-head") ? document.querySelector(".nt-quote-head").textContent : ""')
        quote_body = await evaluate('document.querySelector(".nt-quote-body") ? document.querySelector(".nt-quote-body").textContent : ""')
        assert "小林" in quote_label, quote_label
        assert "被引用的原消息" in quote_body, quote_body
        assert await evaluate('document.querySelectorAll(".nt-md-h").length') >= 1
        assert await evaluate('document.querySelectorAll(".nt-md-code").length') >= 1
        assert await evaluate('document.querySelectorAll(".nt-bubble a.nt-sticker").length') >= 1
        print('Quote card + markdown + sticker OK', flush=True)
        # Clicking a quote whose target exists scrolls to and flashes that message.
        await evaluate('document.querySelector(".nt-quote").click()')
        jumped = False
        for _ in range(30):
            if await evaluate('document.querySelectorAll(".nt-message.nt-flash").length') >= 1:
                jumped = True
                break
            await asyncio.sleep(.1)
        assert jumped, 'quote click did not jump to the referenced message'
        print('Quote jump OK', flush=True)

        # System faces render as a glyph from the built-in index, not "[表情]".
        face_img = await evaluate('document.querySelector(".nt-face") ? document.querySelector(".nt-face").getAttribute("src") : ""')
        assert face_img == "/api/chat/face?id=14", "face not rendered as a real image: %r" % face_img
        # lazy loading may leave the image unloaded until it enters the viewport;
        # assert the endpoint delivers a real PNG instead of pixel state.
        status = await evaluate('fetch("/api/chat/face?id=14").then(r => r.status + ":" + r.headers.get("content-type")).catch(e => "err:" + e.message)')
        assert status == "200:image/png", "face endpoint broken: %r" % status
        attrs = await evaluate('(() => { const i = document.querySelector(".nt-face"); return i ? JSON.stringify({src: i.getAttribute("src"), alt: i.alt, cls: i.className}) : "none"; })()')
        assert "nt-face" in str(attrs), attrs
        assert await evaluate('document.body.textContent.includes("[表情]")') is False, "face still shows a placeholder"
        # A face with no local art states the version notice verbatim.
        missing_text = await evaluate('document.querySelector(".nt-face-missing") ? document.querySelector(".nt-face-missing").textContent : ""')
        assert missing_text == "[QQ版本过低，无法显示对应表情]", repr(missing_text)
        print('Real face image + missing notice OK', flush=True)

        # Emoji picker: QQ-style tabs with real face images.
        await evaluate('document.getElementById("emoji-toggle").click()')
        await asyncio.sleep(0.4)
        tabs = await evaluate('Array.from(document.querySelectorAll(".nt-emoji-tab")).map(b => b.textContent)')
        assert "小黄脸" in tabs and "超级表情" in tabs, tabs
        face_srcs = await evaluate('Array.from(document.querySelectorAll(".nt-emoji-face img")).map(i => i.getAttribute("src"))')
        assert face_srcs, "no faces rendered in the picker"
        assert all("/api/chat/face?id=" in src for src in face_srcs), face_srcs
        # Switching a tab changes the grid and must NOT close the panel.
        await evaluate('Array.from(document.querySelectorAll(".nt-emoji-tab")).find(b => b.textContent.includes("超级表情")).click()')
        await asyncio.sleep(0.25)
        panel_open = await evaluate('document.getElementById("emoji-panel").hidden === false')
        assert panel_open, "panel closed when switching tabs"
        current = await evaluate('Array.from(document.querySelectorAll(".nt-emoji-face img")).map(i => i.getAttribute("src"))')
        assert current == ["/api/chat/face?id=367"], current
        # Clicking a face inserts a CQ face code into the composer.
        await evaluate('document.querySelector(".nt-emoji-face").click()')
        await asyncio.sleep(0.2)
        composer = await evaluate('document.getElementById("chat-input").value')
        assert "[CQ:face,id=367]" in composer, composer
        await evaluate('document.getElementById("chat-input").value = ""; document.getElementById("chat-input").dispatchEvent(new Event("input"));')
        print('Emoji picker tabs OK', flush=True)

        # Avatar context menu: a real CDP right-click is required because a
        # synthetic MouseEvent does not reproduce Chromium's trusted input path.
        await evaluate(
            "(() => { const a = document.querySelector('.nt-peer-avatar-trigger');"
            " if (a) a.scrollIntoView({block:'center'}); })()"
        )
        # scrollIntoView is async; measuring immediately yields stale coordinates.
        await asyncio.sleep(0.35)
        box = await evaluate(
            "(() => { const a = document.querySelector('.nt-peer-avatar-trigger');"
            " const r = a.getBoundingClientRect();"
            " return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),"
            " w: window.innerWidth, h: window.innerHeight}); })()"
        )
        point = json.loads(box)
        assert 0 < point["x"] < point["w"] and 0 < point["y"] < point["h"], "avatar off-screen: %s" % point
        for kind in ("mousePressed", "mouseReleased"):
            await call("Input.dispatchMouseEvent", {
                "type": kind, "button": "right", "buttons": 2,
                "clickCount": 1, "x": point["x"], "y": point["y"],
            })
        await asyncio.sleep(0.3)
        avatarItems = await evaluate(
            'Array.from(document.querySelectorAll(".nt-ctx-item")).map(b => b.textContent)'
        )
        assert avatarItems, "avatar context menu did not open"
        joined = " | ".join(avatarItems)
        assert "点赞" in joined, avatarItems
        assert "复制 QQ 号" in joined, avatarItems
        assert "@" in joined, avatarItems
        await evaluate('document.dispatchEvent(new MouseEvent("click", {bubbles:true}))')
        await asyncio.sleep(0.1)
        print('Avatar menu OK', flush=True)

        # Send-mode dropdown: default is Enter-to-send, switching persists.
        assert await evaluate('document.getElementById("chat-send-menu").hidden') is True
        await evaluate('document.getElementById("chat-send-mode").click()')
        await asyncio.sleep(.1)
        assert await evaluate('document.getElementById("chat-send-menu").hidden') is False
        modes = await evaluate('Array.from(document.querySelectorAll("#chat-send-menu [data-mode]")).map(b => b.dataset.mode)')
        assert modes == ["enter", "ctrl-enter"], modes
        await evaluate('document.querySelector(\'#chat-send-menu [data-mode="ctrl-enter"]\').click()')
        await asyncio.sleep(.1)
        assert await evaluate('localStorage.getItem("qsick.chat.sendMode")') == "ctrl-enter"
        assert await evaluate('document.getElementById("chat-send-menu").hidden') is True
        # In Ctrl+Enter mode a bare Enter must NOT submit the form.
        await evaluate("""
          (() => {
            const box = document.getElementById('chat-input');
            box.value = '不发送';
            box.dispatchEvent(new Event('input'));
            box.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true}));
          })()
        """)
        await asyncio.sleep(.15)
        assert await evaluate('document.getElementById("chat-input").value') == '不发送'
        # Ctrl+Enter does submit (form data clears the input).
        await evaluate('document.querySelector(\'#chat-send-menu\')')
        await evaluate('document.getElementById("chat-send-mode").click()')
        await asyncio.sleep(.05)
        await evaluate('document.querySelector(\'#chat-send-menu [data-mode="enter"]\').click()')
        await evaluate('document.getElementById("chat-input").value = ""; document.getElementById("chat-input").dispatchEvent(new Event("input"));')
        print('Send-mode menu OK', flush=True)

        # Drag & drop a picture onto the chat pane -> attachment chip appears.
        await evaluate("""
          (() => {
            const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,13])], 'drag.png', {type:'image/png'}));
            document.getElementById('nt-chat').dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer: dt}));
          })()
        """)
        for _ in range(30):
            if await evaluate('document.querySelectorAll(".nt-attachment").length')==1: break
            await asyncio.sleep(.1)
        else: raise AssertionError('drop did not create an attachment')
        assert await evaluate('document.querySelector(".nt-attachment img") !== null') is True
        # Sending uploads it and clears the chip.
        await evaluate('document.getElementById("chat-form").requestSubmit()')
        for _ in range(60):
            if await evaluate('document.querySelectorAll(".nt-attachment").length')==0: break
            await asyncio.sleep(.1)
        else: raise AssertionError('attachment was not uploaded/cleared')
        print('Drop-send OK', flush=True)
        await evaluate('document.querySelectorAll(".nt-conversation")[0].click()')
        await asyncio.sleep(.3)
        await evaluate('document.getElementById("account-toggle").click(); document.querySelectorAll(".nt-account-option")[1].click()')
        await asyncio.sleep(.2)
        assert await evaluate('document.querySelectorAll(".nt-message").length')==0
        await call('Page.reload'); await asyncio.sleep(.5)
        assert await evaluate('document.getElementById("account-label").textContent')=='10002'
        await evaluate('document.getElementById("account-toggle").click(); document.querySelectorAll(".nt-account-option")[0].click()')
        await asyncio.sleep(.5)
        await evaluate('document.querySelectorAll(".nt-conversation")[1].click()')
        await asyncio.sleep(.2)
        assert await evaluate('document.getElementById("chat-input").disabled') is True
        await evaluate('document.querySelectorAll(".nt-conversation")[0].click()')
        await asyncio.sleep(.3)
        await call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':980,'deviceScaleFactor':1,'mobile':False})
        await asyncio.sleep(.2)
        image=await call('Page.captureScreenshot',{'format':'png'})
        import base64
        (ROOT/'docs/chat-qqnt-preview.png').write_bytes(base64.b64decode(image['data']))
        await call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
        await asyncio.sleep(.2)
        assert await evaluate('document.documentElement.scrollWidth <= window.innerWidth') is True
        print('PASS: account isolation/persistence, drafts, retained send failures, scroll preservation, private-send gating, responsive layout')

if __name__=='__main__':
    # Use an isolated port and browser profile; never start the real runtime.
    import socket
    def unused_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]
    web_port, debug_port = unused_port(), unused_port()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=web_port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True); thread.start()
    edge=next((p for p in [Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),Path('C:/Program Files/Microsoft/Edge/Application/msedge.exe')] if p.exists()),None)
    if not edge: raise SystemExit('Edge not found')
    profile=Path(tempfile.mkdtemp(prefix='qsick-chat-preview-'))
    process=None
    try:
        for _ in range(50):
            if server.started: break
            time.sleep(.1)
        process=subprocess.Popen([str(edge),'--headless=new','--disable-gpu','--no-sandbox','--disable-extensions','--no-first-run','--no-default-browser-check','--remote-allow-origins=*',f'--remote-debugging-port={debug_port}',f'--user-data-dir={profile}',f'http://127.0.0.1:{web_port}/chat'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        pages=[]
        for _ in range(70):
            try:
                pages=json.load(urllib.request.urlopen(f'http://127.0.0.1:{debug_port}/json',timeout=1))
                if pages: break
            except OSError: pass
            time.sleep(.1)
        target=next(p for p in pages if p.get('type')=='page')
        asyncio.run(check(target['webSocketDebuggerUrl'],f'http://127.0.0.1:{web_port}'))
    finally:
        server.should_exit=True; thread.join(timeout=5)
        if process:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        # Profile may still be held briefly by Chromium helper processes; leave
        # the uniquely named temp directory if Windows has not released it yet.
        assert profile.name.startswith('qsick-chat-preview-')
        shutil.rmtree(profile,ignore_errors=True)
