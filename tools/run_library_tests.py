"""Run standalone source tests and the same coverage policy as the HA adapter."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.run_tests import check_summary

PACKAGE = Path(__file__).resolve().parents[1] / "packages/sma-net2"


def main() -> int:
    env = {**os.environ, "PYTHONPATH": str(PACKAGE / "src")}
    for args in (
        ("run", "-m", "unittest", "discover", "-s", "tests", "-v"),
        ("report", "-m"),
        ("json", "-o", "coverage-report/coverage.json"),
    ):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                args[0],
                "--rcfile=pyproject.toml",
                *args[1:],
            ],
            cwd=PACKAGE,
            env=env,
            check=True,
        )
    report = json.loads((PACKAGE / "coverage-report/coverage.json").read_text())
    passed = check_summary("Library overall", report["totals"])
    for module, result in report["files"].items():
        passed = check_summary(module, result["summary"]) and passed
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
