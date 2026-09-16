"""Build a manifest from local images. Requires optional Pillow; never downloads images."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, default=Path("wxz/images"))
    parser.add_argument("--output", type=Path, default=Path("wxz/manifest.jsonl"))
    args = parser.parse_args()
    from PIL import Image

    root = args.images.resolve()
    records = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".bmp",
        }:
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"image escapes collection: {path}")
        with Image.open(path) as image:
            if image.getexif().get(274, 1) != 1:
                raise ValueError(f"normalize EXIF orientation before import: {path}")
            width, height = image.size
            image.load()
        relative = path.relative_to(root).as_posix()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append(
            {
                "candidate_id": relative,
                "local_path": relative,
                "width": width,
                "height": height,
                "sha256": digest,
                "source_status": "uncertain",
            }
        )
    if not records:
        raise ValueError("no supported images found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {len(records)} images to {args.output}")


if __name__ == "__main__":
    main()
