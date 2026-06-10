"""
Runner para ejecutar todos los smoke tests del proyecto API1_TITAN.

Uso:
  python run_all_smoke_tests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_script(script_path: Path) -> int:
    print(f"\n[RUN] {script_path.name}")
    cmd = [sys.executable, str(script_path)]
    result = subprocess.run(cmd, check=False)
    if result.returncode == 0:
        print(f"[OK] {script_path.name}")
    else:
        print(f"[FAIL] {script_path.name} (exit code {result.returncode})")
    return result.returncode


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    scripts = [
        base_dir / "smoke_tests.py",
        base_dir / "smoke_tests_sim3.py",
    ]

    missing = [s.name for s in scripts if not s.exists()]
    if missing:
        print("[ERROR] Faltan archivos de smoke tests:")
        for name in missing:
            print(f"  - {name}")
        return 2

    failures = 0
    for script in scripts:
        failures += 1 if run_script(script) != 0 else 0

    if failures == 0:
        print("\n[OK] Todos los smoke tests pasaron")
        return 0

    print(f"\n[FAIL] {failures} suite(s) fallaron")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
