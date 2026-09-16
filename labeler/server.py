#!/usr/bin/env python3
"""Multi-user bounding-box annotation service. See docs/PROVENANCE.md for origins."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, quote, urlparse

MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200
SESSION_SECONDS = 7 * 24 * 60 * 60
PASSWORD_ITERATIONS = 310_000
from .configuration import DEFAULT_CONFIG, validate_config

SOURCE_TO_CLASS = {"accepted": "positive", "uncertain": "uncertain", "rejected": "negative"}
CURRENT_CLASSES = ("positive", "negative", "uncertain", "deleted")
GROUPS = ("pending", "annotated", *CURRENT_CLASSES)


def configure(config: dict) -> None:
    """Configure one project before serving; one project per process."""
    global USERS, ADMIN, ALL_USERS, ROLES, BOX_CLASSES, BOX_CLASS_BY_ID
    global LABEL_SCHEMA_VERSION, VALID_LABELED_BOXES_SQL, PROJECT_CONFIG
    PROJECT_CONFIG = validate_config(config)
    USERS = tuple(PROJECT_CONFIG["annotators"])
    ADMIN = PROJECT_CONFIG["admin"]
    ALL_USERS = (*USERS, ADMIN)
    ROLES = {**{user: "annotator" for user in USERS}, ADMIN: "admin"}
    BOX_CLASSES = tuple(PROJECT_CONFIG["classes"])
    BOX_CLASS_BY_ID = {item["id"]: item for item in BOX_CLASSES}
    LABEL_SCHEMA_VERSION = PROJECT_CONFIG["schema_version"]
    # Names and IDs are strictly validated before they become SQL literals.
    pairs = " OR ".join(
        f"(json_extract(box.value,'$.class_id')={item['id']} AND "
        f"json_extract(box.value,'$.class_name')='{item['name']}')"
        for item in BOX_CLASSES
    )
    VALID_LABELED_BOXES_SQL = (
        "json_array_length(boxes_json)>0 AND NOT EXISTS (SELECT 1 FROM "
        f"json_each(boxes_json) AS box WHERE COALESCE(({pairs}),0)=0)"
    )


configure(DEFAULT_CONFIG)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('annotator','admin')),
    password_salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    csrf_token TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_annotation_sessions_user
ON sessions(username, expires_at);
CREATE TABLE IF NOT EXISTS annotations (
    candidate_id TEXT PRIMARY KEY,
    source_status TEXT NOT NULL CHECK(source_status IN ('accepted','uncertain','rejected')),
    image_path TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    current_class TEXT NOT NULL CHECK(current_class IN ('positive','negative','uncertain','deleted')),
    task_required INTEGER NOT NULL CHECK(task_required IN (0,1)),
    task_state TEXT NOT NULL CHECK(task_state IN ('not_required','pending','completed')),
    assignee TEXT REFERENCES users(username),
    assignment_rank INTEGER,
    no_target INTEGER NOT NULL CHECK(no_target IN (0,1)),
    boxes_json TEXT NOT NULL,
    original_record_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    deleted_from_class TEXT,
    deleted_from_task_state TEXT,
    deleted_from_no_target INTEGER
);
CREATE INDEX IF NOT EXISTS idx_annotations_class
ON annotations(current_class, candidate_id);
CREATE INDEX IF NOT EXISTS idx_annotations_owner_task
ON annotations(assignee, task_state, assignment_rank, candidate_id);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    assignee TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    old_class TEXT NOT NULL,
    new_class TEXT NOT NULL,
    old_task_state TEXT NOT NULL,
    new_task_state TEXT NOT NULL,
    old_no_target INTEGER NOT NULL,
    new_no_target INTEGER NOT NULL,
    old_boxes_json TEXT NOT NULL,
    new_boxes_json TEXT NOT NULL,
    old_revision INTEGER NOT NULL,
    new_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_candidate
ON revisions(candidate_id, revision_id);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    username TEXT,
    candidate_id TEXT,
    assignee TEXT,
    remote_address TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class NotFoundError(ValueError):
    """The requested record is absent or intentionally hidden by authorization."""


class ConflictError(ValueError):
    """The client attempted to overwrite a newer revision."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_key(candidate_id: str) -> bytes:
    return hashlib.sha256(candidate_id.encode("utf-8")).digest()


def password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def read_password(path: Path) -> str | dict[str, str]:
    if path.suffix.lower() == ".json":
        passwords = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(passwords, dict) or set(passwords) != set(ALL_USERS):
            raise ValueError("credentials must contain exactly the configured accounts")
        if any(not isinstance(p, str) or len(p) < 12 for p in passwords.values()):
            raise ValueError("each account needs a password of at least 12 characters")
        return passwords
    password = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise RuntimeError("password file is empty")
    return password


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=30, check_same_thread=False
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=60, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def validated_boxes(value: object, width: int, height: int) -> list[dict[str, float | int | str]]:
    if not isinstance(value, list):
        raise TypeError("boxes must be a list")
    if len(value) > 1000:
        raise ValueError("too many boxes")
    boxes: list[dict[str, float | int | str]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise TypeError(f"box {index + 1} must be an object")
        class_id = raw.get("class_id")
        if isinstance(class_id, bool) or not isinstance(class_id, int):
            raise TypeError(f"框 {index + 1} 必须选择有效的对象类别")
        box_class = BOX_CLASS_BY_ID.get(class_id)
        if box_class is None or raw.get("class_name") != box_class["name"]:
            raise ValueError(f"框 {index + 1} 的类别 ID 与类别名称不匹配")
        try:
            x1, x2 = sorted((float(raw["x1"]), float(raw["x2"])))
            y1, y2 = sorted((float(raw["y1"]), float(raw["y2"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"box {index + 1} has invalid coordinates") from exc
        if not all(math.isfinite(v) for v in (x1, x2, y1, y2)):
            raise ValueError("box coordinates must be finite")
        x1, x2 = max(0.0, x1), min(float(width), x2)
        y1, y2 = max(0.0, y1), min(float(height), y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            raise ValueError(f"box {index + 1} is too small")
        boxes.append(
            {
                "class_id": class_id,
                "class_name": str(box_class["name"]),
                "x1": round(x1, 4),
                "x2": round(x2, 4),
                "y1": round(y1, 4),
                "y2": round(y2, 4),
            }
        )
    return boxes


def sync_users(connection: sqlite3.Connection, password: str | dict[str, str]) -> None:
    now = utc_now()
    for username in ALL_USERS:
        account_password = password[username] if isinstance(password, dict) else password
        row = connection.execute(
            "SELECT password_salt,password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            salt = secrets.token_bytes(16)
            connection.execute(
                "INSERT INTO users(username,role,password_salt,password_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    username,
                    ROLES[username],
                    salt,
                    password_digest(account_password, salt),
                    now,
                    now,
                ),
            )
            continue
        salt = bytes(row["password_salt"])
        expected = password_digest(account_password, salt)
        if not hmac.compare_digest(bytes(row["password_hash"]), expected):
            connection.execute(
                "UPDATE users SET password_hash=?,role=?,updated_at=? WHERE username=?",
                (expected, ROLES[username], now, username),
            )
            connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL",
                (int(time.time()), username),
            )


def _source_records(source_database: Path, collection_root: Path) -> list[dict[str, object]]:
    if source_database.suffix.lower() == ".jsonl":
        from .manifest import read_manifest

        return read_manifest(source_database, collection_root)
    records: list[dict[str, object]] = []
    with connect(source_database, read_only=True) as source:
        rows = source.execute(
            "SELECT candidate_id,sheet,position,local_path,initial_status,current_status,"
            "reason,reviewer,metadata_json,training_use,training_reason,batch_id "
            "FROM reviews ORDER BY candidate_id"
        )
        for row in rows:
            candidate_id = str(row["candidate_id"] or "")
            source_status = str(row["current_status"] or "")
            if not candidate_id or source_status not in SOURCE_TO_CLASS:
                raise RuntimeError(f"invalid source row: {candidate_id!r}/{source_status!r}")
            metadata = json.loads(str(row["metadata_json"] or "{}"))
            width = int(metadata.get("width") or 0)
            height = int(metadata.get("height") or 0)
            image_path = (collection_root / str(row["local_path"])).resolve()
            if width <= 0 or height <= 0:
                raise RuntimeError(f"image dimensions are missing for {candidate_id}")
            if not image_path.is_relative_to(collection_root) or not image_path.is_file():
                raise RuntimeError(f"image is missing or outside collection: {candidate_id}")
            records.append(
                {
                    "candidate_id": candidate_id,
                    "source_status": source_status,
                    "image_path": str(image_path),
                    "width": width,
                    "height": height,
                    "local_path": str(row["local_path"]),
                    "sha256": str(metadata.get("sha256") or ""),
                    "source_review": {
                        "batch_id": str(row["batch_id"] or ""),
                        "initial_status": str(row["initial_status"] or ""),
                        "position": int(row["position"]),
                        "reason": str(row["reason"] or ""),
                        "reviewer": str(row["reviewer"] or ""),
                        "sheet": int(row["sheet"]),
                        "training_reason": str(row["training_reason"] or ""),
                        "training_use": str(row["training_use"] or ""),
                    },
                }
            )
    if len({str(record["candidate_id"]) for record in records}) != len(records):
        raise RuntimeError("duplicate candidate IDs in source review database")
    return records


def _assignment_maps(
    records: list[dict[str, object]],
) -> tuple[dict[str, str], dict[str, int]]:
    owners: dict[str, str] = {}
    for source_status in ("accepted", "uncertain"):
        candidates = sorted(
            (
                str(record["candidate_id"])
                for record in records
                if record["source_status"] == source_status
            ),
            key=stable_key,
        )
        for index, candidate_id in enumerate(candidates):
            owners[candidate_id] = USERS[index % len(USERS)]
    ranks: dict[str, int] = {}
    for username in USERS:
        candidates = sorted(
            (candidate_id for candidate_id, owner in owners.items() if owner == username),
            key=stable_key,
        )
        ranks.update({candidate_id: index for index, candidate_id in enumerate(candidates, 1)})
    return owners, ranks


def write_assignment_manifests(database: Path, workspace: Path) -> None:
    manifest = workspace / "assignment-manifest.jsonl"
    report = workspace / "assignment-summary.json"
    if manifest.exists() and report.exists():
        return
    with connect(database, read_only=True) as connection:
        rows = connection.execute(
            "SELECT candidate_id,source_status,assignee,assignment_rank FROM annotations "
            "WHERE task_required=1 ORDER BY assignee,assignment_rank"
        ).fetchall()
        counts = connection.execute(
            "SELECT assignee,source_status,COUNT(*) count FROM annotations "
            "WHERE task_required=1 GROUP BY assignee,source_status ORDER BY assignee,source_status"
        ).fetchall()
    if not manifest.exists():
        with manifest.open("x", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    if not report.exists():
        payload = {
            "created_at": utc_now(),
            "manifest": str(manifest),
            "manifest_sha256": sha256_file(manifest),
            "records": len(rows),
            "counts": [dict(row) for row in counts],
        }
        with report.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def initialize_workspace(
    source_database: Path,
    collection_root: Path,
    password_file: Path,
    workspace: Path,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    database = workspace / "annotations.current.sqlite"
    password = read_password(password_file)
    with connect(database) as connection:
        connection.executescript(SCHEMA)
        identity = json.dumps(
            {
                key: PROJECT_CONFIG[key]
                for key in ("project_id", "schema_version", "classes", "annotators", "admin")
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        old = connection.execute(
            "SELECT value FROM metadata WHERE key='project_contract'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
        if (old and old[0] != identity) or (count and not old):
            raise RuntimeError(
                "workspace contract differs; use a new workspace, never relabel existing data"
            )
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('project_contract',?)", (identity,)
        )
        sync_users(connection, password)
        existing = int(connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0])
        if not existing:
            records = _source_records(source_database, collection_root)
            owners, ranks = _assignment_maps(records)
            now = utc_now()
            rows: list[tuple[object, ...]] = []
            for record in records:
                candidate_id = str(record["candidate_id"])
                source_status = str(record["source_status"])
                task_required = int(source_status in {"accepted", "uncertain"})
                original = {
                    "candidate_id": candidate_id,
                    "class_name": PROJECT_CONFIG["project_id"],
                    "image": {
                        "height": record["height"],
                        "local_path": record["local_path"],
                        "sha256": record["sha256"],
                        "width": record["width"],
                    },
                    "source_review": {
                        **dict(record["source_review"]),
                        "current_status": source_status,
                    },
                }
                rows.append(
                    (
                        candidate_id,
                        source_status,
                        record["image_path"],
                        record["width"],
                        record["height"],
                        SOURCE_TO_CLASS[source_status],
                        task_required,
                        "pending" if task_required else "not_required",
                        owners.get(candidate_id),
                        ranks.get(candidate_id),
                        int(source_status == "rejected"),
                        "[]",
                        json.dumps(original, ensure_ascii=False, separators=(",", ":")),
                        now,
                        "initializer",
                    )
                )
            connection.executemany(
                "INSERT INTO annotations(candidate_id,source_status,image_path,width,height,"
                "current_class,task_required,task_state,assignee,assignment_rank,no_target,"
                "boxes_json,original_record_json,updated_at,updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            metadata = {
                "created_at": now,
                "source_review_database": str(source_database),
                "collection_root": str(collection_root),
                "initial_counts": {
                    status: sum(record["source_status"] == status for record in records)
                    for status in SOURCE_TO_CLASS
                },
            }
            for key, value in metadata.items():
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                    (key, value if isinstance(value, str) else json.dumps(value, sort_keys=True)),
                )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
            ("label_schema_version", LABEL_SCHEMA_VERSION),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
            ("box_classes", json.dumps(BOX_CLASSES, ensure_ascii=False, sort_keys=True)),
        )
        connection.commit()
    password = ""  # Do not retain the plaintext beyond initialization.
    write_assignment_manifests(database, workspace)
    return database


def access_clause(user: dict[str, str]) -> tuple[str, list[object]]:
    if user["role"] == "admin":
        return "1=1", []
    return "assignee=?", [user["username"]]


def summary(database: Path, user: dict[str, str]) -> dict[str, object]:
    scope, parameters = access_clause(user)
    result: dict[str, object] = {
        "total": 0,
        "pending": 0,
        "annotated": 0,
        "positive": 0,
        "negative": 0,
        "uncertain": 0,
        "deleted": 0,
        "completed": 0,
        "boxes": 0,
        "revisions": 0,
    }
    with connect(database, read_only=True) as connection:
        result["total"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM annotations WHERE {scope}", parameters
            ).fetchone()[0]
        )
        for row in connection.execute(
            f"SELECT current_class,COUNT(*) count FROM annotations WHERE {scope} "
            "GROUP BY current_class",
            parameters,
        ):
            result[str(row["current_class"])] = int(row["count"])
        result["pending"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM annotations WHERE {scope} AND task_required=1 "
                "AND task_state='pending' AND current_class IN ('positive','uncertain')",
                parameters,
            ).fetchone()[0]
        )
        result["annotated"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM annotations WHERE {scope} "
                f"AND current_class='positive' AND {VALID_LABELED_BOXES_SQL}",
                parameters,
            ).fetchone()[0]
        )
        result["completed"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM annotations WHERE {scope} AND task_required=1 "
                "AND task_state='completed'",
                parameters,
            ).fetchone()[0]
        )
        result["boxes"] = int(
            connection.execute(
                f"SELECT COALESCE(SUM(json_array_length(boxes_json)),0) FROM annotations "
                f"WHERE {scope} AND current_class='positive' AND {VALID_LABELED_BOXES_SQL}",
                parameters,
            ).fetchone()[0]
        )
        result["invalid_positive_boxes"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM annotations WHERE {scope} "
                "AND current_class='positive' AND json_array_length(boxes_json)>0 "
                f"AND NOT ({VALID_LABELED_BOXES_SQL})",
                parameters,
            ).fetchone()[0]
        )
        revision_scope = "1=1" if user["role"] == "admin" else "assignee=?"
        result["revisions"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM revisions WHERE {revision_scope}", parameters
            ).fetchone()[0]
        )
        if user["role"] == "admin":
            per_user = []
            for username in USERS:
                row = connection.execute(
                    "SELECT COUNT(*) total,"
                    "SUM(CASE WHEN task_state='pending' AND current_class IN ('positive','uncertain') "
                    "THEN 1 ELSE 0 END) pending,"
                    "SUM(CASE WHEN task_state='completed' THEN 1 ELSE 0 END) completed "
                    "FROM annotations WHERE assignee=? AND task_required=1",
                    (username,),
                ).fetchone()
                per_user.append(
                    {
                        "username": username,
                        "total": int(row["total"] or 0),
                        "pending": int(row["pending"] or 0),
                        "completed": int(row["completed"] or 0),
                    }
                )
            result["per_user"] = per_user
    return result


class AnnotationHandler(BaseHTTPRequestHandler):
    database: Path
    workspace: Path
    collection_root: Path
    ui_path: Path
    js_path: Path
    mutation_lock = threading.Lock()
    login_lock = threading.Lock()
    login_failures: ClassVar[dict[tuple[str, str], list[float]]] = {}

    def send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_html(self) -> None:
        body = self.ui_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_payload(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise ValueError("invalid request size")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise TypeError("request body must be an object")
        return payload

    def cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001 - malformed cookies must act as no session
            return ""
        morsel = cookie.get("labeler_session")
        return morsel.value if morsel else ""

    def authenticate(self) -> dict[str, str] | None:
        token = self.cookie_token()
        if not token:
            return None
        now = int(time.time())
        with connect(self.database) as connection:
            row = connection.execute(
                "SELECT s.username,s.csrf_token,s.last_seen_at,u.role,u.enabled "
                "FROM sessions s JOIN users u ON u.username=s.username "
                "WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?",
                (token_digest(token), now),
            ).fetchone()
            if row is None or not int(row["enabled"]):
                return None
            if int(row["last_seen_at"]) < now - 3600:
                connection.execute(
                    "UPDATE sessions SET last_seen_at=? WHERE token_hash=?",
                    (now, token_digest(token)),
                )
                connection.commit()
        return {
            "username": str(row["username"]),
            "role": str(row["role"]),
            "csrf_token": str(row["csrf_token"]),
        }

    def require_user(self) -> dict[str, str] | None:
        user = self.authenticate()
        if user is None:
            self.send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED)
        return user

    def require_csrf(self, user: dict[str, str]) -> bool:
        if self.headers.get("X-CSRF-Token") != user["csrf_token"]:
            self.send_json({"error": "invalid CSRF token"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def audit(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        *,
        username: str | None,
        candidate_id: str | None = None,
        assignee: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(event_type,username,candidate_id,assignee,remote_address,"
            "details_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                event_type,
                username,
                candidate_id,
                assignee,
                self.client_address[0],
                json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                utc_now(),
            ),
        )

    def login_throttled(self, username: str) -> bool:
        key = (self.client_address[0], username)
        cutoff = time.monotonic() - 15 * 60
        with self.login_lock:
            attempts = [stamp for stamp in self.login_failures.get(key, []) if stamp >= cutoff]
            self.login_failures[key] = attempts
            return len(attempts) >= 10

    def record_login_failure(self, username: str) -> None:
        key = (self.client_address[0], username)
        with self.login_lock:
            self.login_failures.setdefault(key, []).append(time.monotonic())

    def clear_login_failures(self, username: str) -> None:
        with self.login_lock:
            self.login_failures.pop((self.client_address[0], username), None)

    def login(self, payload: dict[str, object]) -> None:
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if self.login_throttled(username):
            self.send_json({"error": "账号或密码错误，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        with connect(self.database) as connection:
            row = connection.execute(
                "SELECT username,role,password_salt,password_hash,enabled FROM users WHERE username=?",
                (username,),
            ).fetchone()
            salt = bytes(row["password_salt"]) if row else b"labeler-dummy-salt"
            candidate = password_digest(password, salt)
            valid = bool(
                row
                and int(row["enabled"])
                and hmac.compare_digest(candidate, bytes(row["password_hash"]))
            )
            if not valid:
                self.record_login_failure(username)
                self.audit(connection, "login_failed", username=username or None)
                connection.commit()
                self.send_json({"error": "账号或密码错误"}, HTTPStatus.UNAUTHORIZED)
                return
            self.clear_login_failures(username)
            raw_token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            now = int(time.time())
            connection.execute(
                "INSERT INTO sessions(token_hash,username,csrf_token,created_at,last_seen_at,expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (token_digest(raw_token), username, csrf, now, now, now + SESSION_SECONDS),
            )
            self.audit(connection, "login_succeeded", username=username)
            connection.commit()
        cookie = (
            f"labeler_session={raw_token}; HttpOnly; SameSite=Lax; Path=/; "
            f"Max-Age={SESSION_SECONDS}"
        )
        self.send_json(
            {
                "authenticated": True,
                "csrf_token": csrf,
                "role": row["role"],
                "username": username,
                "label_schema_version": LABEL_SCHEMA_VERSION,
                "box_classes": BOX_CLASSES,
                "project": PROJECT_CONFIG,
            },
            headers=[("Set-Cookie", cookie)],
        )

    def logout(self, user: dict[str, str]) -> None:
        token = self.cookie_token()
        with connect(self.database) as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE token_hash=?",
                (int(time.time()), token_digest(token)),
            )
            self.audit(connection, "logout", username=user["username"])
            connection.commit()
        self.send_json(
            {"ok": True},
            headers=[
                (
                    "Set-Cookie",
                    "labeler_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
                )
            ],
        )

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            self.send_html()
            return
        if parsed.path == "/app.js":
            body = self.js_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if parsed.path == "/healthz":
            self.send_json({"ok": True, "label_schema_version": LABEL_SCHEMA_VERSION})
            return
        if parsed.path == "/api/session":
            user = self.authenticate()
            if user is None:
                self.send_json({"authenticated": False})
            else:
                self.send_json(
                    {
                        "authenticated": True,
                        **user,
                        "label_schema_version": LABEL_SCHEMA_VERSION,
                        "box_classes": BOX_CLASSES,
                        "project": PROJECT_CONFIG,
                    }
                )
            return
        user = self.require_user()
        if user is None:
            return
        try:
            if parsed.path == "/api/group":
                self.send_json(self.group_payload(user, query))
                return
            if parsed.path == "/api/item":
                self.send_json(self.item_payload(user, query.get("id", [""])[0]))
                return
            if parsed.path == "/media":
                self.serve_media(user, query.get("id", [""])[0])
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except NotFoundError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (TypeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_payload()
            if parsed.path == "/api/login":
                self.login(payload)
                return
            user = self.require_user()
            if user is None or not self.require_csrf(user):
                return
            if parsed.path == "/api/logout":
                self.logout(user)
                return
            with self.mutation_lock:
                if parsed.path == "/api/save":
                    result = self.save_item(user, payload)
                elif parsed.path == "/api/restore":
                    result = self.restore_item(user, payload)
                elif parsed.path == "/api/export":
                    result = self.export_snapshot(user, payload)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            self.send_json(result)
        except ConflictError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except NotFoundError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - HTTP boundary returns structured failures
            self.send_json(
                {"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def _filters(
        self, user: dict[str, str], query: dict[str, list[str]], group: str
    ) -> tuple[str, list[object]]:
        if group not in GROUPS:
            raise ValueError("unknown group")
        clauses: list[str] = []
        parameters: list[object] = []
        if user["role"] != "admin":
            clauses.append("assignee=?")
            parameters.append(user["username"])
        else:
            owner = query.get("assignee", [""])[0]
            if owner == "unassigned":
                clauses.append("assignee IS NULL")
            elif owner:
                if owner not in USERS:
                    raise ValueError("unknown assignee")
                clauses.append("assignee=?")
                parameters.append(owner)
        if group == "pending":
            clauses.extend(
                [
                    "task_required=1",
                    "task_state='pending'",
                    "current_class IN ('positive','uncertain')",
                ]
            )
        elif group == "annotated":
            clauses.extend(
                [
                    "current_class='positive'",
                    VALID_LABELED_BOXES_SQL,
                ]
            )
        else:
            clauses.append("current_class=?")
            parameters.append(group)
        search = query.get("q", [""])[0].strip().lower()
        if search:
            clauses.append(
                "instr(lower(candidate_id || ' ' || source_status || ' ' || "
                "coalesce(assignee,'')),?)>0"
            )
            parameters.append(search)
        return " AND ".join(clauses) or "1=1", parameters

    @staticmethod
    def row_payload(row: sqlite3.Row) -> dict[str, object]:
        boxes = json.loads(str(row["boxes_json"]))
        class_counts = {
            str(box_class["name"]): sum(
                box.get("class_id") == box_class["id"]
                and box.get("class_name") == box_class["name"]
                for box in boxes
                if isinstance(box, dict)
            )
            for box_class in BOX_CLASSES
        }
        return {
            "assignee": str(row["assignee"] or ""),
            "assignment_rank": int(row["assignment_rank"] or 0),
            "box_count": len(boxes),
            "boxes": boxes,
            "box_class_counts": class_counts,
            "candidate_id": str(row["candidate_id"]),
            "height": int(row["height"]),
            "media_url": f"/media?id={quote(str(row['candidate_id']), safe='')}",
            "no_target": bool(row["no_target"]),
            "revision": int(row["revision"]),
            "source_status": str(row["source_status"]),
            "status": str(row["current_class"]),
            "task_required": bool(row["task_required"]),
            "task_state": str(row["task_state"]),
            "updated_at": str(row["updated_at"]),
            "updated_by": str(row["updated_by"]),
            "width": int(row["width"]),
        }

    def group_payload(self, user: dict[str, str], query: dict[str, list[str]]) -> dict[str, object]:
        group = query.get("status", ["pending"])[0]
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
            page_size = min(
                MAX_PAGE_SIZE,
                max(1, int(query.get("page_size", [str(DEFAULT_PAGE_SIZE)])[0])),
            )
        except ValueError as exc:
            raise ValueError("invalid pagination") from exc
        where, parameters = self._filters(user, query, group)
        order = (
            "COALESCE(assignment_rank,2147483647),candidate_id"
            if group == "pending"
            else "candidate_id"
        )
        with connect(self.database, read_only=True) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM annotations WHERE {where}", parameters
                ).fetchone()[0]
            )
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = connection.execute(
                "SELECT candidate_id,source_status,width,height,current_class,task_required,"
                "task_state,assignee,assignment_rank,no_target,boxes_json,revision,updated_at,"
                f"updated_by FROM annotations WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [self.row_payload(row) for row in rows],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "pages": pages,
                "total": total,
            },
            "status": group,
            "summary": summary(self.database, user),
        }

    def _authorized_row(
        self, connection: sqlite3.Connection, user: dict[str, str], candidate_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM annotations WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None or (user["role"] != "admin" and row["assignee"] != user["username"]):
            raise NotFoundError("candidate is unavailable")
        return row

    def item_payload(self, user: dict[str, str], candidate_id: str) -> dict[str, object]:
        with connect(self.database, read_only=True) as connection:
            return self.row_payload(self._authorized_row(connection, user, candidate_id))

    def _write_revision(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        user: dict[str, str],
        action: str,
        new_class: str,
        new_task_state: str,
        new_no_target: int,
        new_boxes_json: str,
        now: str,
    ) -> int:
        new_revision = int(row["revision"]) + 1
        connection.execute(
            "INSERT INTO revisions(candidate_id,assignee,actor,action,old_class,new_class,"
            "old_task_state,new_task_state,old_no_target,new_no_target,old_boxes_json,"
            "new_boxes_json,old_revision,new_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["candidate_id"],
                row["assignee"],
                user["username"],
                action,
                row["current_class"],
                new_class,
                row["task_state"],
                new_task_state,
                row["no_target"],
                new_no_target,
                row["boxes_json"],
                new_boxes_json,
                row["revision"],
                new_revision,
                now,
            ),
        )
        return new_revision

    def _append_revision_log(self, revision_id: int) -> None:
        with connect(self.database, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
        if row is None:
            return
        record = dict(row)
        for field in ("old_boxes_json", "new_boxes_json"):
            record[field.removesuffix("_json")] = json.loads(str(record.pop(field)))
        path = self.workspace / "annotation-revisions.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def save_item(self, user: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        candidate_id = str(payload.get("candidate_id") or "")
        requested = str(payload.get("status") or "")
        if requested not in CURRENT_CLASSES:
            raise ValueError("invalid target class")
        expected_revision = int(payload.get("revision", -1))
        with connect(self.database) as connection:
            row = self._authorized_row(connection, user, candidate_id)
            if int(row["revision"]) != expected_revision:
                raise ConflictError("图片已被其他操作更新，请重新载入后再保存")
            boxes = validated_boxes(payload.get("boxes"), int(row["width"]), int(row["height"]))
            task_required = int(row["task_required"])
            deleted_from_class = row["deleted_from_class"]
            deleted_from_task_state = row["deleted_from_task_state"]
            deleted_from_no_target = row["deleted_from_no_target"]
            if requested == "positive":
                if not boxes:
                    raise ValueError("正样本至少需要一个目标框")
                new_task_state = "completed" if task_required else "not_required"
                no_target = 0
                action = "complete_positive" if task_required else "reclassify_positive"
            elif requested == "negative":
                if boxes:
                    raise ValueError("负样本必须无目标框")
                new_task_state = "completed" if task_required else "not_required"
                no_target = 1
                action = "complete_negative" if task_required else "reclassify_negative"
            elif requested == "uncertain":
                if boxes:
                    raise ValueError("改为不确定前必须清空目标框")
                task_required = 1
                new_task_state = "pending"
                no_target = 0
                action = "reopen_uncertain"
            else:
                new_task_state = str(row["task_state"])
                no_target = int(row["no_target"])
                action = "delete"
                if row["current_class"] != "deleted":
                    deleted_from_class = row["current_class"]
                    deleted_from_task_state = row["task_state"]
                    deleted_from_no_target = row["no_target"]
            now = utc_now()
            boxes_json = json.dumps(boxes, ensure_ascii=False, separators=(",", ":"))
            new_revision = self._write_revision(
                connection,
                row=row,
                user=user,
                action=action,
                new_class=requested,
                new_task_state=new_task_state,
                new_no_target=no_target,
                new_boxes_json=boxes_json,
                now=now,
            )
            connection.execute(
                "UPDATE annotations SET current_class=?,task_required=?,task_state=?,no_target=?,"
                "boxes_json=?,revision=?,updated_at=?,updated_by=?,deleted_from_class=?,"
                "deleted_from_task_state=?,deleted_from_no_target=? WHERE candidate_id=?",
                (
                    requested,
                    task_required,
                    new_task_state,
                    no_target,
                    boxes_json,
                    new_revision,
                    now,
                    user["username"],
                    deleted_from_class,
                    deleted_from_task_state,
                    deleted_from_no_target,
                    candidate_id,
                ),
            )
            revision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            self.audit(
                connection,
                action,
                username=user["username"],
                candidate_id=candidate_id,
                assignee=str(row["assignee"] or "") or None,
                details={"new_class": requested, "new_revision": new_revision},
            )
            connection.commit()
        self._append_revision_log(revision_id)
        result = self.item_payload(user, candidate_id)
        result["summary"] = summary(self.database, user)
        return result

    def restore_item(self, user: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        candidate_id = str(payload.get("candidate_id") or "")
        expected_revision = int(payload.get("revision", -1))
        with connect(self.database) as connection:
            row = self._authorized_row(connection, user, candidate_id)
            if row["current_class"] != "deleted":
                raise ValueError("image is not deleted")
            if int(row["revision"]) != expected_revision:
                raise ConflictError("图片已被其他操作更新，请重新载入后再恢复")
            new_class = str(row["deleted_from_class"] or SOURCE_TO_CLASS[str(row["source_status"])])
            new_task_state = str(
                row["deleted_from_task_state"]
                or ("pending" if int(row["task_required"]) else "not_required")
            )
            new_no_target = int(
                row["deleted_from_no_target"]
                if row["deleted_from_no_target"] is not None
                else new_class == "negative"
            )
            now = utc_now()
            new_revision = self._write_revision(
                connection,
                row=row,
                user=user,
                action="restore",
                new_class=new_class,
                new_task_state=new_task_state,
                new_no_target=new_no_target,
                new_boxes_json=str(row["boxes_json"]),
                now=now,
            )
            connection.execute(
                "UPDATE annotations SET current_class=?,task_state=?,no_target=?,revision=?,"
                "updated_at=?,updated_by=?,deleted_from_class=NULL,deleted_from_task_state=NULL,"
                "deleted_from_no_target=NULL WHERE candidate_id=?",
                (
                    new_class,
                    new_task_state,
                    new_no_target,
                    new_revision,
                    now,
                    user["username"],
                    candidate_id,
                ),
            )
            revision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            self.audit(
                connection,
                "restore",
                username=user["username"],
                candidate_id=candidate_id,
                assignee=str(row["assignee"] or "") or None,
                details={"new_revision": new_revision},
            )
            connection.commit()
        self._append_revision_log(revision_id)
        result = self.item_payload(user, candidate_id)
        result["summary"] = summary(self.database, user)
        return result

    def export_snapshot(
        self, user: dict[str, str], payload: dict[str, object]
    ) -> dict[str, object]:
        if user["role"] != "admin":
            raise PermissionError("only administrators can export")
        include_source_negative = bool(payload.get("include_source_negative", False))
        with connect(self.database, read_only=True) as connection:
            rows = connection.execute(
                "SELECT candidate_id,source_status,current_class,task_state,assignee,no_target,"
                "boxes_json,original_record_json,revision,updated_at,updated_by,width,height "
                "FROM annotations "
                "WHERE task_required=1 AND task_state='completed' "
                "AND current_class IN ('positive','negative') ORDER BY candidate_id"
            ).fetchall()
            source_negative = (
                connection.execute(
                    "SELECT candidate_id,source_status,current_class,task_state,assignee,no_target,"
                    "boxes_json,original_record_json,revision,updated_at,updated_by,width,height "
                    "FROM annotations "
                    "WHERE source_status='rejected' AND task_required=0 AND current_class='negative' "
                    "ORDER BY candidate_id"
                ).fetchall()
                if include_source_negative
                else []
            )
            revision_count = int(connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0])
        combined = [*rows, *source_negative]
        records: list[dict[str, object]] = []
        for row in combined:
            record = json.loads(str(row["original_record_json"]))
            boxes = validated_boxes(
                json.loads(str(row["boxes_json"])), int(row["width"]), int(row["height"])
            )
            if row["current_class"] == "positive" and not boxes:
                raise RuntimeError(
                    f"completed positive record has no labeled boxes: {row['candidate_id']}"
                )
            if row["current_class"] == "negative" and boxes:
                raise RuntimeError(
                    f"completed negative record contains boxes: {row['candidate_id']}"
                )
            record.pop("class_name", None)
            record.update(
                {
                    "annotation_schema": {
                        "version": LABEL_SCHEMA_VERSION,
                        "classes": BOX_CLASSES,
                    },
                    "assignee": str(row["assignee"] or ""),
                    "boxes": boxes,
                    "current_class": str(row["current_class"]),
                    "no_target": bool(row["no_target"]),
                    "task_state": str(row["task_state"]),
                    "task_name": PROJECT_CONFIG["project_id"],
                    "web_review_revision": int(row["revision"]),
                    "web_review_updated_at": str(row["updated_at"]),
                    "web_review_updated_by": str(row["updated_by"]),
                }
            )
            records.append(record)

        exports = self.workspace / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = exports / f"annotations-rev{revision_count}-{timestamp}.jsonl"
        suffix = 1
        while path.exists():
            path = exports / f"annotations-rev{revision_count}-{timestamp}-{suffix}.jsonl"
            suffix += 1
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with connect(self.database) as connection:
            self.audit(
                connection,
                "export",
                username=user["username"],
                details={
                    "include_source_negative": include_source_negative,
                    "path": str(path),
                    "records": len(combined),
                },
            )
            connection.commit()
        return {
            "path": str(path),
            "records": len(combined),
            "task_records": len(rows),
            "source_negative_records": len(source_negative),
            "revisions": revision_count,
            "sha256": sha256_file(path),
        }

    def serve_media(self, user: dict[str, str], candidate_id: str) -> None:
        with connect(self.database, read_only=True) as connection:
            row = self._authorized_row(connection, user, candidate_id)
        path = Path(str(row["image_path"])).resolve()
        if not path.is_relative_to(self.collection_root) or not path.is_file():
            raise NotFoundError("image is unavailable")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        stat = path.stat()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 256):
                    self.wfile.write(chunk)


def resolved_inside(root: Path, value: Path, label: str) -> Path:
    path = (value if value.is_absolute() else root / value).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f"{label} must stay inside the project root: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Labeler: configurable multi-user box annotation")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("project.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument("--workspace", type=Path, default=Path("wxz/workspace"))
    parser.add_argument("--source", type=Path, default=Path("wxz/manifest.jsonl"))
    parser.add_argument("--collection-root", type=Path, default=Path("wxz/images"))
    parser.add_argument("--password-file", type=Path, default=Path("wxz/accounts.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    config_path = resolved_inside(root, args.config, "config")
    configure(json.loads(config_path.read_text(encoding="utf-8-sig")))
    workspace = resolved_inside(root, args.workspace, "workspace")
    source = resolved_inside(root, args.source, "source manifest")
    collection = resolved_inside(root, args.collection_root, "collection root")
    password_file = resolved_inside(root, args.password_file, "password file")
    if not collection.is_dir():
        raise RuntimeError("image collection directory does not exist")
    database = initialize_workspace(source, collection, password_file, workspace)
    AnnotationHandler.database = database
    AnnotationHandler.workspace = workspace
    AnnotationHandler.collection_root = collection
    AnnotationHandler.ui_path = Path(__file__).parent / "static/index.html"
    AnnotationHandler.js_path = Path(__file__).parent / "static/app.js"
    totals = summary(database, {"username": ADMIN, "role": "admin"})
    if totals["invalid_positive_boxes"]:
        raise RuntimeError("invalid box classes found; inspect the workspace before serving")
    server = ThreadingHTTPServer((args.host, args.port), AnnotationHandler)
    print(
        f"Labeler http://{args.host}:{server.server_port} total={totals['total']} "
        f"pending={totals['pending']} workspace={workspace}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
