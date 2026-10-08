"""Run the offline suite and enforce separate line and branch coverage gates."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "coverage-report"
MINIMUM = 96


def run(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "coverage", *args], cwd=ROOT, check=True)


def check_summary(label: str, summary: dict, *, minimum: int = MINIMUM) -> bool:
    """Enforce line and branch floors independently, without rounded comparisons."""
    passed = True
    for metric, covered, total in (
        ("lines", summary["covered_lines"], summary["num_statements"]),
        ("branches", summary["covered_branches"], summary["num_branches"]),
    ):
        percentage = 100 * covered / total if total else 100
        print(
            f"{label} {metric}: {percentage:.2f}% ({covered}/{total}); required: {minimum}%",
            flush=True,
        )
        passed &= covered * 100 >= minimum * total
    return passed


def main() -> int:
    REPORT.mkdir(exist_ok=True)
    run("run", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v")
    run("report", "-m")
    run("json", "-o", str(REPORT / "coverage.json"))
    run("xml", "-o", str(REPORT / "coverage.xml"))
    run("html", "-d", str(REPORT / "html"))
    report = json.loads((REPORT / "coverage.json").read_text())
    passed = check_summary("Overall", report["totals"])
    for module, result in report["files"].items():
        passed = check_summary(module, result["summary"]) and passed
    passed = (
        check_summary(
            "Config flow",
            report["files"]["custom_components/sma_bluetooth/config_flow.py"][
                "summary"
            ],
            minimum=100,
        )
        and passed
    )
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
