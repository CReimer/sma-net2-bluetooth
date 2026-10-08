"""Strictly check all library modules and the public consumer contract."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/sma-net2"


def main() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(PACKAGE / "pyproject.toml"),
            str(PACKAGE / "src/sma_net2"),
            str(PACKAGE / "typing_examples/usage.py"),
        ],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
