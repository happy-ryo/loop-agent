"""Verify built wheels contain the bundled loop-agent coding-agent skill."""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


REQUIRED_FILES = {
    "loop_agent/skills/loop-agent/SKILL.md",
    "loop_agent/skills/loop-agent/references/transport.md",
    "loop_agent/skills/loop-agent/references/safety.md",
}


def _wheel_files(dist: Path) -> list[Path]:
    return sorted(dist.glob("loop_agent-*.whl"))


def verify(dist: Path) -> None:
    wheels = _wheel_files(dist)
    if not wheels:
        raise SystemExit(f"no loop-agent wheel found in {dist}")

    for wheel_path in wheels:
        with zipfile.ZipFile(wheel_path) as wheel:
            names = set(wheel.namelist())

        missing = sorted(REQUIRED_FILES - names)
        if missing:
            raise SystemExit(
                f"{wheel_path.name} is missing bundled skill files: {missing}"
            )
        has_references = any(
            name.startswith("loop_agent/skills/loop-agent/references/")
            and name.endswith(".md")
            for name in names
        )
        if not has_references:
            raise SystemExit(
                f"{wheel_path.name} has no bundled skill reference markdown files"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", nargs="?", default="dist", type=Path)
    args = parser.parse_args()
    verify(args.dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())