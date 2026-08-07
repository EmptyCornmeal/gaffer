"""Run every gate, in the order CI runs them, and say what failed.

One command so "did I break anything" has one answer on Windows, on the Mac Mini
and in Actions:

    python scripts/verify.py            # backend only
    python scripts/verify.py --all      # + the front-end (needs npm)
    python scripts/verify.py --fast     # skip the slowest steps

Exit status is 0 only if every step passed. Nothing here writes an artifact,
sends a notification or touches git.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def run(label: str, cmd: list[str], *, cwd: Path = ROOT) -> tuple[str, bool, float]:
    print(f"\n=== {label} ===", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, check=False)
    return label, proc.returncode == 0, time.perf_counter() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="also run the front-end gates")
    ap.add_argument("--fast", action="store_true", help="skip the pipeline contract")
    args = ap.parse_args(argv)

    py = [sys.executable]
    steps: list[tuple[str, list[str], Path]] = [
        ("dependencies", [*py, "-m", "gaffer.deps"], ROOT),
        ("ruff", [*py, "-m", "ruff", "check", "."], ROOT),
        ("pytest", [*py, "-m", "pytest", "-q"], ROOT),
    ]
    if not args.fast:
        steps.append(("artifact contract",
                      [*py, "-m", "gaffer.contract", "--max-age-hours", "8760"], ROOT))
    if args.all:
        npm = shutil.which("npm")
        if npm is None:
            print("npm not found on PATH — skipping the front-end gates",
                  file=sys.stderr)
        else:
            # Build before test, for the same reason deploy.yml does: the
            # performance budgets measure `dist/` and skip when it is absent.
            steps += [("npm check", [npm, "run", "check"], WEB),
                      ("npm build", [npm, "run", "build"], WEB),
                      ("npm test", [npm, "run", "test"], WEB)]

    results = [run(label, cmd, cwd=cwd) for label, cmd, cwd in steps]

    print("\n" + "=" * 52)
    width = max(len(label) for label, _, _ in results)
    for label, ok, secs in results:
        print(f"  {label:<{width}}  {'PASS' if ok else 'FAIL'}   {secs:6.1f}s")
    failed = [label for label, ok, _ in results if not ok]
    print("=" * 52)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
