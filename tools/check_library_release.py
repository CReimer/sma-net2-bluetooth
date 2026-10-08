"""Check that a library release tag matches its declared distribution version."""

import argparse
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    metadata = tomllib.loads((ROOT / "packages/sma-net2/pyproject.toml").read_text())
    expected = f"sma-net2-v{metadata['project']['version']}"
    if args.tag != expected:
        raise SystemExit(f"Release tag must be {expected}, got {args.tag}")
    print(f"Verified release tag: {args.tag}")


if __name__ == "__main__":
    main()
