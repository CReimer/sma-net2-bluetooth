"""Install both SMA-Net2 artifacts and run the offline suite without Home Assistant."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "sma-net2"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=PACKAGE / "dist")
    args = parser.parse_args()
    artifacts = sorted(args.dist_dir.glob("*.whl")) + sorted(
        args.dist_dir.glob("*.tar.gz")
    )
    if len(artifacts) != 2 or {p.suffix for p in artifacts} != {".whl", ".gz"}:
        raise SystemExit("Expected exactly one wheel and one source distribution")
    # Keep build scratch off RAM-backed /tmp; each environment is removed on exit.
    with tempfile.TemporaryDirectory(
        prefix=".artifact-check-", dir=PACKAGE
    ) as temporary:
        scratch = Path(temporary)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        for index, artifact in enumerate(artifacts):
            venv = scratch / f"venv-{index}"
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)], check=True, env=env
            )
            python = str(venv / "bin" / "python")
            subprocess.run(
                [python, "-m", "pip", "install", str(artifact.resolve())],
                check=True,
                env=env,
            )
            result = subprocess.run(
                [
                    python,
                    "-I",
                    "-c",
                    "import importlib.util, importlib.metadata, json; import sma_net2; print(json.dumps({'path': sma_net2.__file__, 'version': importlib.metadata.version('sma-net2'), 'homeassistant': importlib.util.find_spec('homeassistant') is not None}))",
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=scratch,
                env=env,
            )
            installed = json.loads(result.stdout)
            if installed["homeassistant"] or not Path(installed["path"]).is_relative_to(
                venv
            ):
                raise SystemExit(f"Artifact is not isolated: {installed}")
            subprocess.run(
                [
                    python,
                    "-I",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(PACKAGE / "tests"),
                    "-v",
                ],
                check=True,
                cwd=scratch,
                env=env,
            )
            print(f"Verified {artifact.name}: {installed}", flush=True)


if __name__ == "__main__":
    main()
