"""Registry -> Python wrappers -> bridge envelopes. No real session/server."""
from types import SimpleNamespace

import pytest
from qsick.onebot.actions import build_registry
from qsick.transport.headless import HeadlessTransport


@pytest.mark.parametrize('name,params,op,native,expected', [
    ('get_group_admin_list',{'group_id':'123'},'group.admins',[{'user_id':456,'role':'admin'}],[{'user_id':456,'role':'admin'}]),
    ('get_group_member_count',{'group_id':'123'},'group.member_count',{'group_id':123,'member_count':5},{'group_id':123,'member_count':5}),
    ('get_group_share_ark',{'group_id':'123'},'group.share_ark',{'ark_json':'{"x":1}'},{'ark_json':'{"x":1}'}),
    ('get_group_share_link',{'group_id':123},'get_group_share_link',{'url':'https://example.invalid/inert'},{'url':'https://example.invalid/inert'}),
    ('get_friend_status_list',{},'friend.statuses',[{'user_id':456,'status':10,'ext_status':0}],[{'user_id':456,'status':10,'ext_status':0}]),
    ('get_friend_relation_list',{},'friend.relations',[{'user_id':456,'relation_flags':{'isBlock':False}}],[{'user_id':456,'relation_flags':{'isBlock':False}}]),
])
async def test_new_query_routes_roundtrip(name,params,op,native,expected,monkeypatch):
    transport=HeadlessTransport()
    calls=[]
    def rpc(body,timeout):
        calls.append(body)
        return {'ok':True,'data':native}
    monkeypatch.setattr(transport,'_send_sync',rpc)
    ctx=SimpleNamespace(call=transport.send)
    result=await build_registry().dispatch(name,params,ctx)
    assert result['status']=='ok',result
    assert result['data']==expected
    assert calls[0]['op']==op
    if 'group_id' in params: assert calls[0]['params']['group_id']==123


async def test_new_required_group_params_are_validated_before_rpc():
    async def must_not_call(*args,**kwargs): raise AssertionError('unexpected call')
    registry=build_registry()
    for name in ('get_group_admin_list','get_group_member_count','get_group_share_ark'):
        response=await registry.dispatch(name,{},SimpleNamespace(call=must_not_call))
        assert response['retcode']==1400
