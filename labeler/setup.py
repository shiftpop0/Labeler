"""Create configuration and unique passwords without overwriting existing files."""

import argparse
import json
import secrets
from pathlib import Path

from .configuration import DEFAULT_CONFIG, validate_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "project.json"
    if not config_path.exists():
        with config_path.open("x", encoding="utf-8") as stream:
            json.dump(DEFAULT_CONFIG, stream, ensure_ascii=False, indent=2)
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8-sig")))
    account_path = root / "wxz/accounts.json"
    account_path.parent.mkdir(parents=True, exist_ok=True)
    (root / "wxz/images").mkdir(exist_ok=True)
    with account_path.open("x", encoding="utf-8") as stream:
        json.dump(
            {u: secrets.token_urlsafe(24) for u in [*config["annotators"], config["admin"]]},
            stream,
            indent=2,
        )
    print(f"Config: {config_path}\nPrivate credentials: {account_path}")
    print("Passwords are not printed. Keep accounts.json local and private.")


if __name__ == "__main__":
    main()
