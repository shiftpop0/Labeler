"""Create a verified, private SQLite online snapshot; never overwrite a backup."""

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_workspace(workspace: Path, output: Path) -> dict:
    database = workspace.resolve() / "annotations.current.sqlite"
    if not database.is_file():
        raise FileNotFoundError(database)
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / database.name
    with (
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source,
        sqlite3.connect(snapshot) as target,
    ):
        source.backup(target)
    with sqlite3.connect(snapshot) as connection:
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("snapshot integrity verification failed; keep it for inspection")
        revisions = connection.execute("SELECT * FROM revisions ORDER BY revision_id").fetchall()
        revision_sum = connection.execute(
            "SELECT COALESCE(SUM(revision),0) FROM annotations"
        ).fetchone()[0]
        if revision_sum != len(revisions):
            raise RuntimeError("revision chain count differs")
        annotations = connection.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
        assignments = connection.execute(
            "SELECT candidate_id,source_status,assignee,assignment_rank FROM annotations "
            "WHERE task_required=1 ORDER BY assignee,assignment_rank"
        ).fetchall()
    with (output / "annotation-revisions.jsonl").open("x", encoding="utf-8") as stream:
        for row in revisions:
            record = dict(row)
            for field in ("old_boxes_json", "new_boxes_json"):
                record[field.removesuffix("_json")] = json.loads(record.pop(field))
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with (output / "assignment-manifest.jsonl").open("x", encoding="utf-8") as stream:
        for row in assignments:
            stream.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "integrity_check": integrity,
        "annotations": annotations,
        "revisions": len(revisions),
        "images_included": False,
        "private_accounts_and_sessions_included": True,
        "journal": "reconstructed from the same database snapshot",
    }
    (output / "backup-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    files = []
    for path in sorted(output.iterdir()):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": path.name, "bytes": path.stat().st_size, "sha256": digest})
    (output / "backup-manifest.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("wxz/workspace"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("wxz/backups") / datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    print(json.dumps({"backup": str(output), **backup_workspace(args.workspace, output)}, indent=2))


if __name__ == "__main__":
    main()
