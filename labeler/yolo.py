"""Convert an exported JSONL snapshot into YOLO labels (no image copying or train split)."""

import argparse
import hashlib
import json
import math
from pathlib import Path


def convert(source: Path, output: Path) -> int:
    rows = [
        json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not rows:
        raise ValueError("export is empty")
    schema = rows[0]["annotation_schema"]
    classes = schema["classes"]
    if [c["id"] for c in classes] != list(range(len(classes))):
        raise ValueError("YOLO requires consecutive class IDs")
    mapping = []
    prepared = []
    seen = set()
    for row in rows:
        if row["annotation_schema"] != schema:
            raise ValueError("mixed class schemas in export")
        if row["task_state"] != "completed" or row["current_class"] not in {"positive", "negative"}:
            raise ValueError("only completed positive/negative samples can be converted")
        candidate = row["candidate_id"]
        if not isinstance(candidate, str) or candidate in seen:
            raise ValueError("invalid or duplicate candidate_id")
        seen.add(candidate)
        width, height = row["image"]["width"], row["image"]["height"]
        if width <= 0 or height <= 0:
            raise ValueError("invalid image dimensions")
        boxes = row["boxes"]
        if bool(boxes) != (row["current_class"] == "positive"):
            raise ValueError("sample class disagrees with boxes")
        lines = []
        for box in boxes:
            class_id = box["class_id"]
            if (
                type(class_id) is not int
                or not 0 <= class_id < len(classes)
                or classes[class_id]["name"] != box["class_name"]
            ):
                raise ValueError("box class does not match schema")
            x1, y1, x2, y2 = [box[k] for k in ("x1", "y1", "x2", "y2")]
            if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or not (
                0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height
            ):
                raise ValueError("invalid box geometry")
            values = (
                (x1 + x2) / (2 * width),
                (y1 + y2) / (2 * height),
                (x2 - x1) / width,
                (y2 - y1) / height,
            )
            lines.append(str(class_id) + " " + " ".join(f"{v:.8f}" for v in values))
        label = hashlib.sha256(candidate.encode("utf-8")).hexdigest() + ".txt"
        prepared.append((label, "\n".join(lines) + ("\n" if lines else "")))
        mapping.append(
            {
                "candidate_id": candidate,
                "image": row["image"]["local_path"],
                "label": "labels/" + label,
            }
        )
    output.mkdir(parents=True, exist_ok=False)
    (output / "labels").mkdir()
    for filename, text in prepared:
        (output / "labels" / filename).write_text(text, encoding="utf-8")
    (output / "classes.json").write_text(
        json.dumps(classes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "image-label-map.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(f"Converted {convert(args.source, args.output)} samples into {args.output}")


if __name__ == "__main__":
    main()
