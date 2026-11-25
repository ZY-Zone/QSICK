"""Offline folder creation regressions; no QQ client or network required."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from qsick.onebot.actions import build_registry


@pytest.mark.parametrize("data,ok", [
    ({"folder_id": "native-folder", "folder_name": "display"}, True),
    ({"folder_name": "display"}, False),
    ({"folder_id": ""}, False),
    ({"folder_id": None}, False),
    ({"folder_id": {}}, False),
])
@pytest.mark.parametrize("name_key", ["name", "folder_name"])
async def test_create_folder_requires_real_id(data, ok, name_key):
    calls = []

    async def call(op, params, **kwargs):
        calls.append((op, params))
        return SimpleNamespace(ok=True, data=data)

    result = await build_registry().dispatch(
        "create_group_file_folder", {"group_id": 123, name_key: "display"},
        SimpleNamespace(call=call))
    assert (result["status"] == "ok") is ok, result
    assert calls == [("group.folder.create", {"group_id": 123, "folder_name": "display"})]
    if ok:
        assert result["data"] == {"folder_id": "native-folder"}
    else:
        assert "reliable folder_id" in result["message"]


def test_native_create_folder_response_ids():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    module = Path(__file__).resolve().parents[1] / "src/qsick/headless_actions.cjs"
    script = r"""
const assert = require('node:assert/strict');
const { createActionDispatcher } = require(MODULE);
(async () => {
  const cases = [
    [{result:0, folderId:'native-id'}, 'native-id'],
    [{result:0, resultWithGroupItem:{result:0, groupItem:[{folderInfo:{folderId:'nested-id',folderName:'display'}}]}}, 'nested-id'],
    [{result:0, resultWithGroupItem:{folderId:'nested-direct'}}, 'nested-direct'],
    [{result:0, folder_name:'display'}, null],
    [{result:0, folderId:''}, null],
    [{result:0, folderId:{}}, null],
    [{result:0, resultWithGroupItem:{groupItem:[{folderInfo:{folderId:'a'}},{folderInfo:{folderId:'b'}}]}}, null],
    [{result:0, folderId:'a', resultWithGroupItem:{folderId:'b'}}, null],
  ];
  for (const [response, expected] of cases) {
    let calls = 0;
    const rm = {createGroupFolder(group, name) {
      ++calls;
      assert.equal(group, '123'); assert.equal(name, 'display');
      return response;
    }};
    const dispatcher = createActionDispatcher({getSession:()=>({getRichMediaService:()=>rm}), getIdentity:()=>({uin:'123'})});
    if (expected) {
      const result = await dispatcher.execute('group.folder.create', {group_id:123, folder_name:'display'});
      assert.equal(result.folder_id, expected);
    } else {
      await assert.rejects(dispatcher.execute('group.folder.create', {group_id:123, folder_name:'display'}),
        error => error.code === 'INVALID_RESULT' && /may already exist/.test(error.message));
    }
    assert.equal(calls, 1);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("MODULE", json.dumps(str(module)))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
