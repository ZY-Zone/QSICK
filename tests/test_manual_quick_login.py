"""No live QQ/services/process control: manual-login route and metadata tests."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.headless import Instance, HeadlessManager
from qsick.webui.app import create_app, templates


def test_last_account_is_not_a_login_claim(tmp_path):
    (tmp_path/'data').mkdir()
    (tmp_path/'data'/'login.json').write_text(json.dumps({'uin':'10001','uid':'u_saved'}),encoding='utf-8')
    data=Instance('C1',tmp_path).to_dict()
    assert data['can_quick_login'] is True
    assert data['last_qq']=='10001'
    assert data['logged_in'] is False
    assert data['running'] is False


def test_row_has_manual_login_button_and_no_history_disables(tmp_path):
    account=Instance('C1',tmp_path).to_dict()
    html=templates.env.get_template('partials/accounts_table.html').render(accounts=[account])
    assert 'data-action="quick-login"' in html
    row=html.split('data-action="quick-login"',1)[1].split('>',1)[0]
    assert 'disabled' in row


async def test_route_offloads_manual_login_and_preserves_errors(tmp_path,monkeypatch):
    config=RuntimeConfig(data_dir=str(tmp_path))
    config.headless_root=str(tmp_path/'instances')
    runtime=SimpleNamespace(config=config)
    called=Mock(return_value={'account':{'id':'C1','phase':'initializing'},'accepted':True,'already_logged_in':False})
    monkeypatch.setattr(HeadlessManager,'quick_login',called)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),base_url='http://ui') as client:
        response=await client.post('/api/accounts/C1/quick-login')
        assert response.status_code==200 and response.json()['accepted'] is True
        assert response.json()['account']['phase']=='initializing'
        called.assert_called_once_with('C1',5754)
        called.side_effect=ValueError('no history')
        assert (await client.post('/api/accounts/C1/quick-login')).status_code==400
        called.side_effect=RuntimeError('expired')
        response=await client.post('/api/accounts/C1/quick-login')
        assert response.status_code==409 and response.json()['error']=='expired'
        config.webui.access_token='secret'
        called.reset_mock()
        assert (await client.post('/api/accounts/C1/quick-login')).status_code==401
        called.assert_not_called()


def test_manager_manual_request_is_instance_scoped_and_starts_without_auto_login(tmp_path,monkeypatch):
    manager=HeadlessManager(tmp_path/'instances'); manager.create(); manager.create()
    folder=tmp_path/'instances'/'C1'
    (folder/'data'/'login.json').write_text(json.dumps({'uin':'10002','uid':'u_peer'}),encoding='utf-8')
    (folder/'config.json').write_text(json.dumps({'port':19101,'bridgeToken':'token-C1'}),encoding='utf-8')
    started=Mock(return_value={})
    monkeypatch.setattr(manager,'start',started)
    requests=[]
    class Reply:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return b'{"ok":true,"accepted":true}'
    class Opener:
        def open(self,request,timeout): requests.append(request);return Reply()
    monkeypatch.setattr('qsick.headless.urllib.request.build_opener',lambda *_:Opener())
    result=manager.quick_login('C1',5754)
    started.assert_called_once_with('C1',5754,auto_login=False)
    assert requests[0].full_url=='http://127.0.0.1:19101/quick-login'
    assert requests[0].get_header('Authorization')=='Bearer token-C1'
    assert result['accepted'] is True and result['account']['id']=='C1'
    with pytest.raises(KeyError): manager.quick_login('../C0')


def test_kicked_instance_reports_a_real_status_not_a_generic_error(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "status.json").write_text(json.dumps({
        "phase": "kicked", "pid": 999, "updated_at": 9_999_999_999,
        "error": "账号在另一台设备登录", "security_kicked_type": 3,
    }), encoding="utf-8")
    data = Instance("C1", tmp_path).to_dict()
    assert data["kicked"] is True
    assert data["security_kicked"] is True
    assert data["kick_reason"] == "账号在另一台设备登录"
    assert data["logged_in"] is False


def test_auto_relogin_defaults_to_off_and_persists(tmp_path):
    manager = HeadlessManager(tmp_path / "instances")
    manager.create()
    assert manager.instances()[0]["auto_relogin"] is False
    updated = manager.set_auto_relogin("C0", True)
    assert updated["auto_relogin"] is True
    assert json.loads((tmp_path / "instances" / "C0" / "instance.json").read_text(encoding="utf-8"))["autoRelogin"] is True
    assert manager.set_auto_relogin("C0", False)["auto_relogin"] is False


def test_auto_relogin_switch_gates_recovery(tmp_path, monkeypatch):
    from qsick.headless import HeadlessManager
    manager = HeadlessManager(tmp_path / "instances")
    manager.create()
    started = Mock(return_value={})
    monkeypatch.setattr(manager, "start", started)
    # Off by default: recovery must refuse rather than restart anything.
    with pytest.raises(ValueError):
        manager.recover_kicked("C0")
    started.assert_not_called()


async def test_auto_relogin_endpoint_validates_and_persists(tmp_path, monkeypatch):
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.headless_root = str(tmp_path / "instances")
    runtime = SimpleNamespace(config=config)
    manager = HeadlessManager(config.headless_root)
    manager.create()
    monkeypatch.setattr(HeadlessManager, "set_auto_relogin",
                        lambda self, iid, enabled, delay=None: {
                            "id": iid, "auto_relogin": enabled, "auto_relogin_delay": delay or 600})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://ui") as client:
        ok = await client.post("/api/accounts/C0/auto-relogin", json={"enabled": True})
        assert ok.status_code == 200 and ok.json()["account"]["auto_relogin"] is True
        assert (await client.post("/api/accounts/C0/auto-relogin", json={"enabled": "yes"})).status_code == 400
        # 时延只接受预设档位：任意数字会让重登节拍失控。
        assert (await client.post("/api/accounts/C0/auto-relogin",
                                  json={"enabled": True, "delay": 7})).status_code == 400
        picked = await client.post("/api/accounts/C0/auto-relogin",
                                   json={"enabled": True, "delay": 1800})
        assert picked.status_code == 200
