"""Headless deployment fixture; Popen and process control are completely stubbed."""
import json
import time
from types import SimpleNamespace
from pathlib import Path

from qsick.headless import HeadlessManager


def test_start_copies_complete_module_bundle_and_rotates_token(tmp_path, monkeypatch):
    manager = HeadlessManager(tmp_path/'hosts')
    manager.create()
    monkeypatch.setattr(manager, 'prepare_runtime', lambda: (tmp_path/'runtime','test','1','test'))
    monkeypatch.setattr(manager, 'stop', lambda *_: None)
    monkeypatch.setattr('qsick.headless.shutil.which', lambda _: 'fake-node')
    monkeypatch.setattr('qsick.headless.time.sleep', lambda _: None)
    captured=[]
    def popen(args, **kwargs):
        captured.append(args)
        config=json.loads(Path(args[-1]).read_text(encoding='utf-8'))
        Path(config['dataDir']).joinpath('status.json').write_text(json.dumps({'id':'C0','phase':'waiting_qrcode','pid':98765,'updated_at':time.time()}),encoding='utf-8')
        return SimpleNamespace(pid=98765)
    monkeypatch.setattr('qsick.headless.subprocess.Popen', popen)
    manager.start('C0',12345)
    folder=tmp_path/'hosts'/'C0'
    for name in ('host.js','headless_codec.cjs','headless_actions.cjs','headless_sender.cjs','headless_login.cjs','headless_media.cjs','headless_oidb.cjs'):
        assert (folder/name).is_file()
    config=json.loads((folder/'config.json').read_text(encoding='utf-8'))
    assert len(config['bridgeToken']) >= 32
    assert config['corePort']==12345
    assert len(captured)==1
