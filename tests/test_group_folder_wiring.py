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
async def test_create_folder_requires_real_id(data, ok, name_key, monkeypatch):
    """No ID from the service: resolve it from the listing, never invent one.

    Measured live: the folder was created while the call returned no ID, so the old
    "fail immediately" contract told the caller nothing was created and a retry produced a
    duplicate. The action layer now snapshots the listing before creating, and after a
    creation without an ID it looks the folder up by name. When it never shows up the call
    still fails -- a fabricated ID would be worse than a failure.
    """
    from qsick.onebot.actions.group_file import create_group_file_folder as mod

    monkeypatch.setattr(mod, "LOOKUP_ATTEMPTS", 2)
    monkeypatch.setattr(mod, "LOOKUP_DELAY", 0)

    calls = []

    async def call(op, params, **kwargs):
        calls.append((op, params))
        if op == "get_group_files":
            return SimpleNamespace(ok=True, data={"files": []})
        return SimpleNamespace(ok=True, data=data)

    result = await build_registry().dispatch(
        "create_group_file_folder", {"group_id": 123, name_key: "display"},
        SimpleNamespace(call=call))
    assert (result["status"] == "ok") is ok, result
    # The listing is read before the creation, so a pre-existing folder cannot be mistaken for ours.
    assert calls[0][0] == "get_group_files"
    assert calls[1] == ("group.folder.create", {"group_id": 123, "folder_name": "display"})
    if ok:
        assert result["data"] == {"folder_id": "native-folder"}
        assert len(calls) == 2
    else:
        assert {op for op, _ in calls[2:]} == {"get_group_files"}
        assert "did not appear in the group file listing" in result["message"]


async def test_a_created_folder_is_identified_from_the_listing(monkeypatch):
    """The measured case: the service returns no ID but the folder exists afterwards."""
    from qsick.onebot.actions.group_file import create_group_file_folder as mod

    monkeypatch.setattr(mod, "LOOKUP_ATTEMPTS", 2)
    monkeypatch.setattr(mod, "LOOKUP_DELAY", 0)

    listings = [
        {"files": [{"is_folder": False, "file_id": "/a", "file_name": "display"}]},
        {"files": [
            {"is_folder": False, "file_id": "/a", "file_name": "display"},
            {"is_folder": True, "file_id": "/folder-1", "file_name": "display"},
        ]},
    ]
    seen = []

    async def call(op, params, **kwargs):
        if op == "get_group_files":
            seen.append(op)
            return SimpleNamespace(ok=True, data=listings[min(len(seen) - 1, 1)])
        return SimpleNamespace(ok=True, data={})

    result = await build_registry().dispatch(
        "create_group_file_folder", {"group_id": 123, "name": "display"},
        SimpleNamespace(call=call))
    assert result["status"] == "ok", result
    # The folder entry appeared only after the creation, so it is ours; not the same-named file.
    assert result["data"] == {"folder_id": "/folder-1"}


async def test_a_preexisting_folder_of_the_same_name_is_not_claimed(monkeypatch):
    """If the name was taken before the call, an ID-less success cannot be attributed to us."""
    from qsick.onebot.actions.group_file import create_group_file_folder as mod

    monkeypatch.setattr(mod, "LOOKUP_ATTEMPTS", 2)
    monkeypatch.setattr(mod, "LOOKUP_DELAY", 0)
    listing = {"files": [{"is_folder": True, "file_id": "/old", "file_name": "display"}]}

    async def call(op, params, **kwargs):
        if op == "get_group_files":
            return SimpleNamespace(ok=True, data=listing)
        return SimpleNamespace(ok=True, data={})

    result = await build_registry().dispatch(
        "create_group_file_folder", {"group_id": 123, "name": "display"},
        SimpleNamespace(call=call))
    assert result["status"] == "failed", result
    assert "already exists" in result["message"]


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
    // No ID in a confirmed creation is not a failure any more: the folder exists, the action layer
    // resolves the ID from the listing. Returning '' keeps "created" and "identified" separate.
    [{result:0, folder_name:'display'}, ''],
    [{result:0, folderId:''}, ''],
    [{result:0, folderId:{}}, ''],
    // Several different IDs, or an ambiguous pair, is still a refusal: guessing which is new is worse.
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
    if (expected !== null) {
      const result = await dispatcher.execute('group.folder.create', {group_id:123, folder_name:'display'});
      assert.equal(result.folder_id, expected);
    } else {
      await assert.rejects(dispatcher.execute('group.folder.create', {group_id:123, folder_name:'display'}),
        error => /several different folderIds/.test(error.message));
    }
    assert.equal(calls, 1);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("MODULE", json.dumps(str(module)))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
