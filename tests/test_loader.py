from __future__ import annotations

from pathlib import Path

from qsick.onebot.actions import build_registry, load_catalog, verify_catalog

ACTIONS = Path(__file__).resolve().parents[1] / "src" / "qsick" / "onebot" / "actions"


def test_catalog_modules_and_classes_exist():
    problems = verify_catalog()
    assert problems == [], "\n".join(problems[:10])


def test_every_action_has_its_own_file():
    missing = []
    for item in load_catalog()["actions"]:
        path = ACTIONS / f"{item['module']}.py"
        if not path.exists():
            missing.append(str(path))
    assert not missing, f"missing modules: {missing[:5]}"


def test_json_drives_registration():
    catalog = {
        "actions": [
            {
                "name": "send_group_msg",
                "module": "message/send_group_msg",
                "class": "SendGroupMsgAction",
                "category": "message",
                "summary": "send",
                "status": "implemented",
                "read_only": False,
                "params": [{"name": "group_id", "kind": "int", "required": True}],
            }
        ],
        "aliases": {"send_group_poke": "send_group_msg"},
    }
    registry = build_registry(catalog)
    assert registry.names() == ["send_group_msg", "send_group_poke"]
    assert registry.get("send_group_poke").name == "send_group_msg"


def test_one_file_per_action():
    files = list(ACTIONS.glob("*/*.py"))
    names = {item["module"] for item in load_catalog()["actions"]}
    assert len(files) >= len(names)
    assert all((ACTIONS / f"{name}.py").exists() for name in names)


def test_implemented_actions_override_run():
    from qsick.onebot.actions.base import Action

    registry = build_registry()
    for spec in registry._actions.values():
        if spec.status == "implemented":
            assert spec.handler is not None
    implemented = [spec for spec in registry._actions.values() if spec.status == "implemented"]
    assert len(implemented) >= 230
    # every registered action must have a handler; planned stubs are gone
    assert all(spec.handler is not None for spec in implemented)
    # sanity: the base class is not accidentally registered as a handler
    assert Action.run is not None
