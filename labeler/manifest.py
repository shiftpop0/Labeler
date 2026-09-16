"""Import independent JSONL image manifests without a review-database dependency."""

import hashlib
import json
from pathlib import Path


def read_manifest(path: Path, collection_root: Path) -> list[dict]:
    root = collection_root.resolve()
    records = []
    seen = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise TypeError(f"line {number}: expected an object")
        candidate_id = row.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip() or candidate_id in seen:
            raise ValueError(f"line {number}: missing or duplicate candidate_id")
        seen.add(candidate_id)
        relative = row.get("local_path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError(f"line {number}: local_path must be relative to the image folder")
        image = (root / relative).resolve()
        if not image.is_relative_to(root) or not image.is_file():
            raise ValueError(f"line {number}: missing image or path outside collection")
        width, height = row.get("width"), row.get("height")
        if type(width) is not int or type(height) is not int or min(width, height) <= 0:
            raise ValueError(f"line {number}: width and height must be positive integers")
        status = row.get("source_status", "uncertain")
        if status not in ("accepted", "uncertain", "rejected"):
            raise ValueError(f"line {number}: invalid source_status")
        with image.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if row.get("sha256") and row["sha256"] != digest:
            raise ValueError(f"line {number}: image SHA-256 differs")
        records.append(
            {
                "candidate_id": candidate_id,
                "source_status": status,
                "image_path": str(image),
                "width": width,
                "height": height,
                "local_path": Path(relative).as_posix(),
                "sha256": digest,
                "source_review": {"manifest_row": number, "provenance": row.get("provenance", {})},
            }
        )
    if not records:
        raise ValueError("image manifest is empty")
    return records
