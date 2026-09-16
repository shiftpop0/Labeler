from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import Counter
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from labeler.server import (
    ADMIN,
    BOX_CLASSES,
    LABEL_SCHEMA_VERSION,
    USERS,
    AnnotationHandler,
    _assignment_maps,
    initialize_workspace,
    summary,
    validated_boxes,
)


def _create_source(path: Path, collection: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE reviews (
            candidate_id TEXT PRIMARY KEY,
            sheet INTEGER NOT NULL,
            position INTEGER NOT NULL,
            local_path TEXT NOT NULL,
            initial_status TEXT NOT NULL,
            current_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            training_use TEXT NOT NULL,
            training_reason TEXT NOT NULL,
            batch_id TEXT NOT NULL
        )
        """
    )
    statuses = ["accepted"] * 10 + ["uncertain"] * 5 + ["rejected"] * 3
    for index, status in enumerate(statuses, 1):
        relative = Path("images") / "pending" / f"sample-{index:03}.jpg"
        image = collection / relative
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fixture-image")
        metadata = {"width": 640, "height": 480, "sha256": f"hash-{index}"}
        connection.execute(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"sample-{index:03}",
                1,
                index,
                relative.as_posix(),
                status,
                status,
                "fixture",
                "tester",
                json.dumps(metadata),
                "hold",
                "",
                "batch-test",
            ),
        )
    connection.commit()
    connection.close()


def test_production_sized_assignment_is_balanced_and_stratified() -> None:
    records = [
        {"candidate_id": f"accepted-{index:05}", "source_status": "accepted"}
        for index in range(10_082)
    ] + [
        {"candidate_id": f"uncertain-{index:04}", "source_status": "uncertain"}
        for index in range(490)
    ]

    owners, ranks = _assignment_maps(records)

    totals = Counter(owners.values())
    uncertain = Counter(
        owners[str(record["candidate_id"])]
        for record in records
        if record["source_status"] == "uncertain"
    )
    assert [totals[user] for user in USERS] == [2115, 2115, 2114, 2114, 2114]
    assert [uncertain[user] for user in USERS] == [98, 98, 98, 98, 98]
    assert len(owners) == len(ranks) == 10_572
    for user in USERS:
        user_ranks = sorted(
            ranks[candidate] for candidate, owner in owners.items() if owner == user
        )
        assert user_ranks == list(range(1, totals[user] + 1))


def test_initialize_separates_class_task_owner_and_password(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    source = tmp_path / "source.sqlite3"
    password_file = tmp_path / "pwd.txt"
    password_file.write_text("shared-test-password\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    _create_source(source, collection)

    database = initialize_workspace(source, collection.resolve(), password_file, workspace)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        classes = connection.execute(
            "SELECT source_status,current_class,task_state,COUNT(*) count FROM annotations "
            "GROUP BY source_status,current_class,task_state ORDER BY source_status"
        ).fetchall()
        assignments = connection.execute(
            "SELECT assignee,source_status,COUNT(*) count FROM annotations "
            "WHERE task_required=1 GROUP BY assignee,source_status ORDER BY assignee,source_status"
        ).fetchall()
        users = connection.execute(
            "SELECT username,role,password_salt,password_hash FROM users ORDER BY username"
        ).fetchall()

    assert [tuple(row) for row in classes] == [
        ("accepted", "positive", "pending", 10),
        ("rejected", "negative", "not_required", 3),
        ("uncertain", "uncertain", "pending", 5),
    ]
    assert [tuple(row) for row in assignments] == [
        (user, status, count)
        for user in USERS
        for status, count in (("accepted", 2), ("uncertain", 1))
    ]
    assert len(users) == 6
    assert {row["username"] for row in users} == {*USERS, ADMIN}
    assert next(row for row in users if row["username"] == ADMIN)["role"] == "admin"
    assert all(bytes(row["password_hash"]) != b"shared-test-password" for row in users)
    assert all(len(bytes(row["password_salt"])) == 16 for row in users)
    assert not (workspace / "pwd.txt").exists()
    assert (workspace / "assignment-manifest.jsonl").is_file()
    assert (workspace / "assignment-summary.json").is_file()

    with sqlite3.connect(database) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    assert metadata["label_schema_version"] == LABEL_SCHEMA_VERSION
    assert json.loads(metadata["box_classes"])[0]["name"] == "object_a"

    admin_summary = summary(database, {"username": ADMIN, "role": "admin"})
    user_summary = summary(database, {"username": USERS[0], "role": "annotator"})
    assert admin_summary["total"] == 18
    assert admin_summary["pending"] == 15
    assert admin_summary["annotated"] == 0
    assert admin_summary["positive"] == 10
    assert admin_summary["uncertain"] == 5
    assert admin_summary["negative"] == 3
    assert user_summary["total"] == 3
    assert user_summary["pending"] == 3
    assert user_summary["annotated"] == 0
    assert user_summary["negative"] == 0


def test_validated_boxes_requires_matching_two_class_contract() -> None:
    for box_class in BOX_CLASSES:
        result = validated_boxes(
            [
                {
                    "class_id": box_class["id"],
                    "class_name": box_class["name"],
                    "x1": 10,
                    "y1": 20,
                    "x2": 100,
                    "y2": 120,
                }
            ],
            640,
            480,
        )
        assert result[0]["class_id"] == box_class["id"]
        assert result[0]["class_name"] == box_class["name"]

    invalid_boxes = (
        {"x1": 10, "y1": 20, "x2": 100, "y2": 120},
        {
            "class_id": 0,
            "class_name": "object_b",
            "x1": 10,
            "y1": 20,
            "x2": 100,
            "y2": 120,
        },
        {
            "class_id": 2,
            "class_name": "unknown",
            "x1": 10,
            "y1": 20,
            "x2": 100,
            "y2": 120,
        },
    )
    for invalid in invalid_boxes:
        try:
            validated_boxes([invalid], 640, 480)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("invalid box class was accepted")


def test_http_login_acl_and_negative_completion(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    source = tmp_path / "source.sqlite3"
    password_file = tmp_path / "pwd.txt"
    password_file.write_text("shared-test-password\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    _create_source(source, collection)
    database = initialize_workspace(source, collection.resolve(), password_file, workspace)
    ui = tmp_path / "index.html"
    script = tmp_path / "app.js"
    ui.write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
    script.write_text("'use strict';", encoding="utf-8")
    AnnotationHandler.database = database
    AnnotationHandler.workspace = workspace
    AnnotationHandler.collection_root = collection.resolve()
    AnnotationHandler.ui_path = ui
    AnnotationHandler.js_path = script
    server = ThreadingHTTPServer(("127.0.0.1", 0), AnnotationHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def session(username: str) -> tuple[object, dict[str, object]]:
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(
            f"{base}/api/login",
            data=json.dumps({"username": username, "password": "shared-test-password"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request) as response:
            return opener, json.load(response)

    def get_json(opener: object, url: str) -> dict[str, object]:
        with opener.open(f"{base}{url}") as response:  # type: ignore[attr-defined]
            return json.load(response)

    try:
        first, first_login = session(USERS[0])
        second, second_login = session(USERS[1])
        first_group = get_json(first, "/api/group?status=pending&page_size=1")
        second_group = get_json(second, "/api/group?status=pending&page_size=1")
        first_item = first_group["items"][0]  # type: ignore[index]
        second_id = second_group["items"][0]["candidate_id"]  # type: ignore[index]

        try:
            get_json(first, f"/api/item?id={second_id}")
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("cross-user detail access was not rejected")

        save = Request(
            f"{base}/api/save",
            data=json.dumps(
                {
                    "candidate_id": first_item["candidate_id"],  # type: ignore[index]
                    "status": "negative",
                    "boxes": [],
                    "revision": first_item["revision"],  # type: ignore[index]
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": str(first_login["csrf_token"]),
            },
            method="POST",
        )
        with first.open(save) as response:  # type: ignore[attr-defined]
            saved = json.load(response)
        assert saved["status"] == "negative"
        assert saved["task_state"] == "completed"
        assert saved["no_target"] is True
        assert saved["boxes"] == []
        assert saved["summary"]["pending"] == 2

        second_item = second_group["items"][0]  # type: ignore[index]
        unlabeled_save = Request(
            f"{base}/api/save",
            data=json.dumps(
                {
                    "candidate_id": second_item["candidate_id"],  # type: ignore[index]
                    "status": "positive",
                    "boxes": [{"x1": 10, "y1": 20, "x2": 200, "y2": 220}],
                    "revision": second_item["revision"],  # type: ignore[index]
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": str(second_login["csrf_token"]),
            },
            method="POST",
        )
        try:
            second.open(unlabeled_save)  # type: ignore[attr-defined]
        except HTTPError as exc:
            assert exc.code == 400
            assert "必须选择" in json.load(exc)["error"]
        else:
            raise AssertionError("API accepted a classless positive box")

        positive_save = Request(
            f"{base}/api/save",
            data=json.dumps(
                {
                    "candidate_id": second_item["candidate_id"],  # type: ignore[index]
                    "status": "positive",
                    "boxes": [
                        {
                            "class_id": 0,
                            "class_name": "object_a",
                            "x1": 10,
                            "y1": 20,
                            "x2": 200,
                            "y2": 220,
                        },
                        {
                            "class_id": 1,
                            "class_name": "object_b",
                            "x1": 250,
                            "y1": 40,
                            "x2": 500,
                            "y2": 300,
                        },
                    ],
                    "revision": second_item["revision"],  # type: ignore[index]
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": str(second_login["csrf_token"]),
            },
            method="POST",
        )
        with second.open(positive_save) as response:  # type: ignore[attr-defined]
            positive = json.load(response)
        assert positive["status"] == "positive"
        assert positive["task_state"] == "completed"
        assert positive["summary"]["annotated"] == 1
        assert positive["box_class_counts"] == {
            "object_b": 1,
            "object_a": 1,
        }
        annotated_group = get_json(second, "/api/group?status=annotated&page_size=100")
        assert annotated_group["pagination"]["total"] == 1
        assert annotated_group["items"][0]["candidate_id"] == second_item["candidate_id"]
        assert annotated_group["items"][0]["box_count"] == 2

        admin, admin_login = session(ADMIN)
        admin_group = get_json(admin, "/api/group?status=negative&page_size=100")
        assert admin_group["summary"]["total"] == 18
        assert admin_group["summary"]["completed"] == 2
        assert admin_group["summary"]["annotated"] == 1
        assert admin_group["pagination"]["total"] == 4

        export_request = Request(
            f"{base}/api/export",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": str(admin_login["csrf_token"]),
            },
            method="POST",
        )
        with admin.open(export_request) as response:  # type: ignore[attr-defined]
            exported = json.load(response)
        exported_records = [
            json.loads(line)
            for line in Path(exported["path"]).read_text(encoding="utf-8").splitlines()
        ]
        exported_positive = next(
            record for record in exported_records if record["current_class"] == "positive"
        )
        assert "class_name" not in exported_positive
        assert exported_positive["task_name"] == "demo"
        assert exported_positive["annotation_schema"]["version"] == LABEL_SCHEMA_VERSION
        assert exported_positive["boxes"][0]["class_name"] == "object_a"
        assert exported_positive["boxes"][1]["class_name"] == "object_b"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_seven_day_session_survives_server_restart(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    source = tmp_path / "source.sqlite3"
    password_file = tmp_path / "pwd.txt"
    password_file.write_text("shared-test-password\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    _create_source(source, collection)
    database = initialize_workspace(source, collection.resolve(), password_file, workspace)
    ui = tmp_path / "index.html"
    script = tmp_path / "app.js"
    ui.write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
    script.write_text("'use strict';", encoding="utf-8")
    AnnotationHandler.database = database
    AnnotationHandler.workspace = workspace
    AnnotationHandler.collection_root = collection.resolve()
    AnnotationHandler.ui_path = ui
    AnnotationHandler.js_path = script

    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    first_server = ThreadingHTTPServer(("127.0.0.1", 0), AnnotationHandler)
    first_thread = threading.Thread(target=first_server.serve_forever, daemon=True)
    first_thread.start()
    login_request = Request(
        f"http://127.0.0.1:{first_server.server_port}/api/login",
        data=json.dumps({"username": USERS[0], "password": "shared-test-password"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(login_request) as response:
        assert json.load(response)["authenticated"] is True
    cookie = next(iter(cookie_jar))
    assert cookie.expires is not None
    assert 604_700 <= cookie.expires - int(time.time()) <= 604_800
    first_server.shutdown()
    first_server.server_close()
    first_thread.join(timeout=5)

    second_server = ThreadingHTTPServer(("127.0.0.1", 0), AnnotationHandler)
    second_thread = threading.Thread(target=second_server.serve_forever, daemon=True)
    second_thread.start()
    try:
        with opener.open(f"http://127.0.0.1:{second_server.server_port}/api/session") as response:
            session = json.load(response)
        assert session["authenticated"] is True
        assert session["username"] == USERS[0]
    finally:
        second_server.shutdown()
        second_server.server_close()
        second_thread.join(timeout=5)
