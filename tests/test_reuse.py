import copy
import json
import sqlite3
from pathlib import Path

import pytest

from labeler import server
from labeler.backup import backup_workspace
from labeler.configuration import DEFAULT_CONFIG, validate_config
from labeler.manifest import read_manifest
from labeler.yolo import convert


def test_annotation_shortcuts_and_deferred_new_box_label() -> None:
    root = Path(__file__).parents[1]
    html = (root / "labeler" / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "labeler" / "static" / "app.js").read_text(encoding="utf-8")

    assert "<kbd>Q</kbd>新增 / 退出；<kbd>W</kbd>切换类别；<kbd>E</kbd>保存并下一张" in html
    assert 'aria-keyshortcuts="Q"' in html
    assert 'aria-keyshortcuts="W"' in html
    assert 'aria-keyshortcuts="E"' in html
    assert "if (!['q', 'w', 'e'].includes(key)) return" in script
    assert "if (key === 'q') toggleDrawMode()" in script
    assert "if (key === 'w') cycleBoxClass()" in script
    assert "if (key === 'e') saveCurrent(true)" in script
    assert "function selectBoxClass(classId)" in script
    assert "function cycleBoxClass()" in script
    assert "const isUnfinishedNewBox = index === state.activeBox" in script
    assert "if (!isUnfinishedNewBox)" in script


@pytest.fixture(autouse=True)
def reset_config():
    server.configure(DEFAULT_CONFIG)
    yield
    server.configure(DEFAULT_CONFIG)


def create_project(tmp_path, class_count=3):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["classes"] = [
        {"id": i, "name": f"part_{i}", "label_zh": f"零件 {i}", "color": "#1688ff"}
        for i in range(class_count)
    ]
    config["annotators"] = ["worker"]
    server.configure(config)
    images = tmp_path / "images"
    images.mkdir()
    (images / "fixture.png").write_bytes(b"fixture")
    source = tmp_path / "manifest.jsonl"
    source.write_text(
        json.dumps(
            {"candidate_id": "sample", "local_path": "fixture.png", "width": 100, "height": 80}
        ),
        encoding="utf-8",
    )
    password = tmp_path / "accounts.json"
    password.write_text(
        json.dumps({"worker": "worker-test-password", "admin": "admin-test-password"}),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    db = server.initialize_workspace(source, images, password, workspace)
    return config, source, images, password, workspace, db


@pytest.mark.parametrize("count", [1, 3, 12])
def test_configurable_classes_and_sql_views(tmp_path, count):
    _, _, _, _, _, db = create_project(tmp_path, count)
    box = {
        "class_id": count - 1,
        "class_name": f"part_{count - 1}",
        "x1": 10,
        "y1": 10,
        "x2": 50,
        "y2": 60,
    }
    assert server.validated_boxes([box], 100, 80) == [box]
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE annotations SET current_class='positive',boxes_json=?,task_state='completed'",
            (json.dumps([box]),),
        )
    assert server.summary(db, {"username": "admin", "role": "admin"})["annotated"] == 1
    box["class_name"] = "not_a_configured_class"
    with pytest.raises(ValueError):
        server.validated_boxes([box], 100, 80)


def test_contract_change_refuses_existing_workspace(tmp_path):
    config, source, images, password, workspace, db = create_project(tmp_path)
    config["classes"][0]["name"] = "replacement"
    server.configure(config)
    with pytest.raises(RuntimeError, match="contract differs"):
        server.initialize_workspace(source, images, password, workspace)
    with sqlite3.connect(db) as con:
        stored = json.loads(
            con.execute("SELECT value FROM metadata WHERE key='project_contract'").fetchone()[0]
        )
    assert stored["classes"][0]["name"] == "part_0"


def test_manifest_rejects_escape_and_duplicates(tmp_path):
    _, source, images, _, _, _ = create_project(tmp_path)
    row = json.loads(source.read_text())
    source.write_text(json.dumps(row) + "\n" + json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        read_manifest(source, images)
    row["local_path"] = "../accounts.json"
    source.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        read_manifest(source, images)


def test_online_backup_is_restorable(tmp_path):
    _, _, _, _, workspace, _ = create_project(tmp_path)
    output = tmp_path / "backup"
    report = backup_workspace(workspace, output)
    assert report["annotations"] == 1 and report["integrity_check"] == "ok"
    with sqlite3.connect(output / "annotations.current.sqlite") as con:
        assert con.execute("SELECT candidate_id FROM annotations").fetchone()[0] == "sample"
    with pytest.raises(FileExistsError):
        backup_workspace(workspace, output)


def test_yolo_positive_and_empty_negative(tmp_path):
    source = tmp_path / "export.jsonl"
    schema = {"version": "test-v1", "classes": DEFAULT_CONFIG["classes"]}
    positive = {
        "candidate_id": "one",
        "annotation_schema": schema,
        "task_state": "completed",
        "current_class": "positive",
        "image": {"width": 100, "height": 80, "local_path": "one.png"},
        "boxes": [
            {"class_id": 1, "class_name": "object_b", "x1": 10, "y1": 20, "x2": 50, "y2": 60}
        ],
    }
    negative = {**positive, "candidate_id": "two", "current_class": "negative", "boxes": []}
    source.write_text(json.dumps(positive) + "\n" + json.dumps(negative), encoding="utf-8")
    output = tmp_path / "yolo"
    assert convert(source, output) == 2
    contents = sorted(p.read_text() for p in (output / "labels").iterdir())
    assert contents == ["", "1 0.30000000 0.50000000 0.40000000 0.50000000\n"]
    with pytest.raises(FileExistsError):
        convert(source, output)


def test_invalid_config_and_nonfinite_geometry():
    bad = copy.deepcopy(DEFAULT_CONFIG)
    bad["classes"][0]["name"] = "x' OR 1=1"
    with pytest.raises(ValueError):
        validate_config(bad)
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            server.validated_boxes(
                [
                    {
                        "class_id": 0,
                        "class_name": "object_a",
                        "x1": value,
                        "y1": 1,
                        "x2": 50,
                        "y2": 60,
                    }
                ],
                100,
                80,
            )


def test_prepare_png_and_refuse_overwrite(tmp_path, monkeypatch):
    image_module = pytest.importorskip("PIL.Image")
    from labeler.prepare import main

    images = tmp_path / "images"
    images.mkdir()
    image_module.new("RGB", (64, 48), "blue").save(images / "test.png")
    output = tmp_path / "manifest.jsonl"
    monkeypatch.setattr("sys.argv", ["prepare", "--images", str(images), "--output", str(output)])
    main()
    row = json.loads(output.read_text())
    assert (row["width"], row["height"]) == (64, 48)
    assert len(read_manifest(output, images)) == 1
    with pytest.raises(FileExistsError):
        main()


def test_setup_creates_distinct_credentials_and_never_overwrites(tmp_path, monkeypatch):
    from labeler.setup import main

    monkeypatch.setattr("sys.argv", ["setup", "--root", str(tmp_path)])
    main()
    accounts = tmp_path / "wxz/accounts.json"
    before = accounts.read_bytes()
    passwords = json.loads(before)
    assert len(set(passwords.values())) == len(passwords) == 6
    assert all(len(password) >= 12 for password in passwords.values())
    with pytest.raises(FileExistsError):
        main()
    assert accounts.read_bytes() == before
