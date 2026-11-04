"""One-command installer for the PaperTrade Falsifier Pro skill.

Pure stdlib. Detects the agent environments present on this machine and
installs the skill into each one's convention:

  Claude Code / Claude Desktop : <project>/.claude/skills/papertrade-falsifier-pro/
  Cursor                       : <project>/.cursor/rules/papertrade-falsifier-pro/

Usage:
  python install_skill.py                 # install into the current directory
  python install_skill.py /path/to/project
  python install_skill.py --list          # show what would be installed where

Nothing is installed outside the target directory; nothing is downloaded;
the skill is entirely local. Re-running updates in place.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_NAME = "papertrade-falsifier-pro"

# (environment label, relative target dir under the project)
TARGETS = [
    ("Claude Code / Claude Desktop", Path(".claude") / "skills" / SKILL_NAME),
    ("Cursor", Path(".cursor") / "rules" / SKILL_NAME),
]

# What ships inside the installed skill (everything except the installer).
PAYLOAD = ["SKILL.md", "engine", "playbooks", "presets", "references", "README.md"]


def install(project: Path, dry_run: bool = False) -> list[tuple[str, Path]]:
    installed = []
    for label, rel in TARGETS:
        dest = project / rel
        if dry_run:
            installed.append((label, dest))
            continue
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for item in PAYLOAD:
            src = HERE / item
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, dest / item,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dest / item)
        installed.append((label, dest))
    return installed


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    dry = "--list" in argv
    project = Path(args[0]).resolve() if args else Path.cwd()

    print(f"Papertrade Falsifier Pro installer")
    print(f"  source:   {HERE}")
    print(f"  project:  {project}")
    print()
    for label, dest in install(project, dry_run=dry):
        print(f"  [{'would install' if dry else 'installed'}] {label}")
        print(f"      -> {dest}")
    print()
    if dry:
        print("dry run (--list): nothing was written.")
        return 0
    print("Done. Verify the install:")
    print(f"  cd {project}")
    print("  python -m research.verify --demo     # if installed at project root")
    print("  # or, from inside the installed skill directory:")
    print("  python -m engine --demo")
    print()
    print("Then tell your agent: 'falsify this strategy: <description>' --")
    print("the SKILL.md iron rules take over from there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
