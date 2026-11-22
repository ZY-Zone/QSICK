"""Synthetic bridge regression: never imports/starts QQ or the application runtime."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.protocol.message import Message
from qsick.protocol.elements import Element
from qsick.storage import SqliteStore, MemoryStore
from qsick.transport.headless import HeadlessTransport, _wire_value
from qsick.webui.app import create_app


@pytest.mark.parametrize('op,key', [('group.list','groups'),('friend.list','friends'),('group.members','members'),('group.shut_list','members'),('friend.categories','categories')])
async def test_list_envelope_and_empty_list_are_preserved(op, key, monkeypatch):
    transport = HeadlessTransport()
    monkeypatch.setattr(transport, '_send_sync', lambda body, timeout: {'ok': True, 'data': []})
    result = await transport.send(op)
    assert result.ok and result.data == {key: []}


async def test_rpc_preserves_all_segments_whitespace_and_string_ids(monkeypatch):
    transport = HeadlessTransport()
    request = {}
    def native(body, timeout):
        request.update(body)
        return {'ok': True, 'data': {'message_id': '9007199254740993'}}
    monkeypatch.setattr(transport, '_send_sync', native)
    params = {'chat': {'type':'group','id':123}, 'elements':[
        {'type':'text','data':{'text':'  x\n'}}, {'type':'face','data':{'id':'1'}}]}
    result = await transport.send('message.send', params)
    assert request['params'] == params
    assert result.data['message_id'] == '9007199254740993'


@pytest.mark.parametrize('code,status', [('UNSUPPORTED',1502),('INVALID_PARAMS',1400),('NOT_READY',1501),('TIMEOUT',1501)])
async def test_host_failure_codes_are_not_success(code, status, monkeypatch):
    transport = HeadlessTransport()
    monkeypatch.setattr(transport, '_send_sync', lambda body, timeout: {'ok':False,'code':code,'error':'test failure'})
    result = await transport.send('group.list')
    assert not result.ok and result.status == status and result.message == 'test failure'


def test_rpc_converts_unsafe_js_numbers_without_mutation():
    original = {'message_id': 18446744073709551615, 'enabled': True, 'elements': [{'data': {'message_id': 9007199254740993}}]}
    converted = _wire_value(original)
    assert converted['message_id'] == '18446744073709551615'
    assert converted['elements'][0]['data']['message_id'] == '9007199254740993'
    assert converted['enabled'] is True
    assert isinstance(original['message_id'], int)


def test_event_kinds_and_large_ids_are_preserved():
    transport = HeadlessTransport()
    transport.push({'kind':'message_sent','payload':{'message_id':'18446744073709551615','chat':{'type':'group','id':'123'},'elements':[]}})
    event = transport._queue.get_nowait()
    assert event.cmd == 'message.sent'
    assert event.json()['message_id'] == '18446744073709551615'
    transport.push({'kind':'notice','cmd':'notice.group_recall','payload':{'message_id':'18446744073709551615'}})
    assert transport._queue.get_nowait().cmd == 'notice.group_recall'
    with pytest.raises(ValueError): transport.push({'kind':'invented','payload':{}})
    with pytest.raises(ValueError): transport.push({'kind':'message','payload':{'message_id':1,'chat':{'type':'guild'}}})


def test_exact_uint64_and_native_reference_survive_sqlite(tmp_path):
    store = SqliteStore(tmp_path / 'isolated.db')
    native = {'msgId':'18446744073709551615','peer':{'chatType':2,'peerUid':'123'},'msgSeq':'42'}
    message = Message.from_payload({'message_id':'18446744073709551615','seq':'42','chat':{'type':'group','id':'123'},
        'sender':{'uin':'456'},'account':'789','elements':[{'type':'image','data':{'file_id':'abc'}}],'native':native})
    store.put(message)
    loaded = store.get(18446744073709551615)
    assert loaded and loaded.message_id == 18446744073709551615 and loaded.native == native
    store.close()
    store = SqliteStore(tmp_path / 'isolated.db')
    assert store.get(18446744073709551615).native == native
    store.close()


def test_memory_search_uses_messages_not_ids():
    store = MemoryStore()
    store.put(Message(1,1,1,'group',123,456,elements=[Element('text',{'text':'find me'})]))
    assert store.search('find')[0].message_id == 1
    assert store.count_matching('find') == 1


async def test_ingress_requires_per_instance_token(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.headless_root = str(tmp_path/'hosts')
    host_dir = tmp_path/'hosts'/'C0'; host_dir.mkdir(parents=True)
    (host_dir/'config.json').write_text(json.dumps({'bridgeToken':'secret'}),encoding='utf-8')
    transport = HeadlessTransport()
    runtime = SimpleNamespace(config=config, transport=transport)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),base_url='http://test') as web:
        headers={'X-QSICK-Instance':'C0'}
        data={'kind':'login','payload':{'logged_in':True,'uin':'123'}}
        assert (await web.post('/api/headless/event',json=data,headers=headers)).status_code == 401
        headers['Authorization']='Bearer secret'
        assert (await web.post('/api/headless/event',json=data,headers=headers)).status_code == 200
        headers['X-QSICK-Instance']='../C0'
        assert (await web.post('/api/headless/event',json=data,headers=headers)).status_code == 409
        headers['X-QSICK-Instance']='C0'
        assert (await web.post('/api/headless/event',json={'kind':'oops','payload':{}},headers=headers)).status_code == 400


async def test_rkey_request_maps_to_the_oidb_action(monkeypatch):
    transport = HeadlessTransport()
    seen = {}
    def rpc(body, timeout):
        seen.update(body)
        return {'ok': True, 'data': {'rkeys': [{'rkey': 'ABC', 'ttl': 60, 'type': 20}]}}
    monkeypatch.setattr(transport, '_send_sync', rpc)
    result = await transport.send('rkey', {})
    assert seen['op'] == 'system.rkey'
    assert result.ok and result.data['rkeys'][0]['rkey'] == 'ABC'


async def test_rkey_static_entries_are_not_double_wrapped(monkeypatch):
    """A dict payload must survive the list-envelope conversion untouched."""
    transport = HeadlessTransport()
    payload = {'rkeys': [{'rkey': 'ONE', 'ttl': 60, 'type': 10},
                         {'rkey': 'TWO', 'ttl': 60, 'type': 20}]}
    monkeypatch.setattr(transport, '_send_sync', lambda body, timeout: {'ok': True, 'data': payload})
    result = await transport.send('rkey', {})
    assert result.ok
    assert [item['rkey'] for item in result.data['rkeys']] == ['ONE', 'TWO']
