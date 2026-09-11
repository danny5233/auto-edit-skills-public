#!/usr/bin/env python3
"""Install selected public skill and its shared editing entry without replacing content."""
import argparse
from pathlib import Path
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("skill", choices=sorted(p.name for p in (root / "skills").iterdir() if p.is_dir()))
parser.add_argument("--skills-dir", type=Path, default=Path.home() / ".codex" / "skills")
args = parser.parse_args()
names = [args.skill]
if args.skill in ("premiere-auto-rough-cut", "subtitle-tools", "make-custom-srt-subtitles"):
    names.insert(0, "auto-edit")
plan = []
for name in names:
    source = root / "skills" / name
    target = args.skills_dir.expanduser() / name
    if target.is_symlink() and target.resolve() == source.resolve():
        continue
    if target.exists() or target.is_symlink():
        parser.error("Existing skill was preserved; reconcile it before installation.")
    plan.append((source, target))
for source, target in plan:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=True)
print("Installed or current:", ", ".join(names))
