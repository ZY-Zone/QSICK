"""Offline regressions for public DLL entity/receipt fields at the Python boundary."""
from types import SimpleNamespace
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from qsick.onebot.actions.base import member_json, message_json_payload, send_message
from qsick.onebot.actions.friend.get_stranger_info import _handle as stranger
from qsick.onebot.actions.friend.get_friend_list import _handle as friends
from qsick.onebot.actions.group.get_group_info import _handle as group
from qsick.onebot.actions.group.get_group_list import _handle as groups
from qsick.onebot.actions.group.get_group_member_info import _handle as member
from qsick.onebot.actions.group.get_group_member_list import _handle as members


def context(data):
    async def call(*args, **kwargs):
        return SimpleNamespace(ok=True, data=data)
    return SimpleNamespace(call=call, config=SimpleNamespace(onebot=SimpleNamespace(cache_seconds=0)))


@pytest.mark.parametrize('handler,params,wrapped', [
    (stranger, {'user_id': 7}, None), (friends, {}, 'friends'),
    (group, {'group_id': 8}, None), (groups, {}, 'groups'),
    (member, {'group_id': 8, 'user_id': 7}, None), (members, {'group_id': 8}, 'members'),
])
async def test_entity_public_fields_survive_without_credentials(handler, params, wrapped):
    data = {'user_id': 7, 'uin': '7', 'uid': 'u_real', 'qid': 'real-qid',
            'nickname': 'Name', 'group_id': 8, 'group_name': 'Group', 'card': 'Card',
            'title': 'Title', 'shut_up_timestamp': 1730000000, 'category_id': 3,
            'personal_note': 'Bio', 'group_create_time': 1700000000,
            'token': 'secret', 'credentials': {'skey': 'secret'},
            'raw': {'token': 'secret'}, 'nickname_extra': {'token': 'secret'}}
    result = await handler(context({wrapped: [data]} if wrapped else data), **params)
    if wrapped:
        result = result[0]
    for key in ('uid', 'qid', 'card', 'title', 'group_name', 'personal_note', 'category_id', 'group_create_time'):
        assert result[key] == data[key]
    assert not {'token', 'credentials', 'raw', 'nickname_extra'} & result.keys()
    assert data['token'] == 'secret'  # no mutation of cache/native records


def test_member_mute_alias_and_missing_public_values():
    result = member_json(8, {'uin': 7, 'uid': 'u_real', 'shut_up_timestamp': 123})
    assert result['shut_up_end_time'] == 123
    assert result['level'] == '0'  # OneBot compatibility
    assert 'qid' not in result and 'title' not in result
    assert 'shut_up_end_time' not in member_json(8, {'uin': 7})


async def test_send_receipt_preserves_only_actual_sequence_and_time():
    result = await send_message(context({'message_id': '9007199254740995', 'message_seq': '53',
                                        'time': '1700000000', 'token': 'secret'}),
                                {'type': 'group', 'id': 8}, 'hello', False)
    assert result == {'message_id': '9007199254740995', 'message_id_str': '9007199254740995',
                      'message_seq': '53', 'time': '1700000000'}
    assert await send_message(context({'message_id': 12}), {'type': 'group', 'id': 8}, 'hi', False) == {
        'message_id': 12, 'message_id_str': '12'}


def test_message_sender_public_identity_survives():
    result = message_json_payload(None, {'message_id': '12', 'seq': '53', 'time': 1700000000,
        'chat': {'type': 'group', 'id': 8},
        'sender': {'uin': 7, 'uid': 'u_real', 'nickname': 'Name', 'card': 'Card', 'token': 'secret'},
        'elements': [{'type': 'text', 'data': {'text': 'hi'}}]})
    assert result['sender']['uid'] == 'u_real'
    assert result['sender']['user_id'] == 7
    assert 'token' not in result['sender']
    assert result['message_seq'] == 53


def test_native_sender_uses_correlated_record_sequence_and_time():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    root = Path(__file__).resolve().parents[1]
    script = r"""
const {createMessageSender} = require('./src/qsick/headless_sender.cjs');
const codec = require('./src/qsick/headless_codec.cjs');
let sender;
const session = {getMSFService:()=>({getServerTime:()=>999}),getMsgService:()=>({
 generateMsgUniqueId:()=> '9999', sendMsg:(_id,peer)=> {
 sender.observe({chatType:2,peerUid:peer.peerUid,guildId:peer.guildId,senderUin:'7',
 msgId:'9007199254740995',msgSeq:'9007199254740997',msgTime:1700000000,sendStatus:2});
 return {result:0};
}})};
sender=createMessageSender({getSession:()=>session,getIdentity:()=>({uin:'7'}),codec,
 resolveUid:async()=> 'u_real',messageRefs:new Map()});
sender.send({chat:{type:'group',id:8},elements:[{type:'text',data:{text:'hi'}}]})
 .then(result=>{console.log(JSON.stringify(result));sender.close();})
 .catch(error=>{console.error(error);process.exitCode=1;sender.close();});
"""
    result = subprocess.run([node, '-e', script], cwd=root, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'message_id': '9007199254740995',
        'message_id_str': '9007199254740995', 'message_seq': '9007199254740997', 'time': '1700000000'}
