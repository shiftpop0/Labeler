"""Public, non-secret project configuration."""

import copy
import re

DEFAULT_CONFIG = {
    "project_id": "demo",
    "title": "Labeler · 图片框标注",
    "schema_version": "demo-v1",
    "instructions": "选择对象类别，再框选图片中可见的目标；无目标图片标为负样本。",
    "classes": [
        {"id": 0, "name": "object_a", "label_zh": "目标 A", "color": "#1677ff"},
        {"id": 1, "name": "object_b", "label_zh": "目标 B", "color": "#fa8c16"},
    ],
    "annotators": ["annotator1", "annotator2", "annotator3", "annotator4", "annotator5"],
    "admin": "admin",
}


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise TypeError("project config must be an object")
    required = set(DEFAULT_CONFIG)
    if set(config) != required:
        raise ValueError(f"config needs exactly these fields: {sorted(required)}")
    config = copy.deepcopy(config)
    identifier = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
    for key in ("project_id", "schema_version", "admin"):
        if not isinstance(config[key], str) or not identifier.fullmatch(config[key]):
            raise ValueError(f"invalid {key}")
    for key in ("title", "instructions"):
        if not isinstance(config[key], str) or not config[key] or len(config[key]) > 4000:
            raise ValueError(f"invalid {key}")
    users = config["annotators"]
    if not isinstance(users, list) or not 1 <= len(users) <= 100:
        raise ValueError("configure 1 to 100 annotators")
    if any(not isinstance(u, str) or not identifier.fullmatch(u) for u in users):
        raise ValueError("invalid annotator name")
    if len(set(users)) != len(users) or config["admin"] in users:
        raise ValueError("account names must be distinct")
    classes = config["classes"]
    if not isinstance(classes, list) or not 1 <= len(classes) <= 100:
        raise ValueError("configure 1 to 100 classes")
    names = set()
    for index, item in enumerate(classes):
        if not isinstance(item, dict) or set(item) != {"id", "name", "label_zh", "color"}:
            raise ValueError("each class needs id, name, label_zh, color")
        if type(item["id"]) is not int or item["id"] != index:
            raise ValueError("class IDs must be consecutive integers starting at zero")
        name = item["name"]
        if not isinstance(name, str) or not identifier.fullmatch(name) or name in names:
            raise ValueError("class names must be unique identifiers")
        names.add(name)
        if not isinstance(item["label_zh"], str) or not 1 <= len(item["label_zh"]) <= 80:
            raise ValueError("invalid class display label")
        if not isinstance(item["color"], str) or not re.fullmatch(
            r"#[0-9a-fA-F]{6}", item["color"]
        ):
            raise ValueError("class colors must be #RRGGBB")
    return config
