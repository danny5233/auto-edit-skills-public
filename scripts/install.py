#!/usr/bin/env python3
"""Install one selected skill without replacing existing content."""
import argparse
from pathlib import Path
parser = argparse.ArgumentParser(description=__doc__)
root = Path(__file__).resolve().parents[1]
parser.add_argument("skill", choices=sorted(p.name for p in (root / "skills").iterdir() if p.is_dir()))
parser.add_argument("--skills-dir", type=Path, default=Path.home() / ".codex" / "skills")
args = parser.parse_args()
source = root / "skills" / args.skill
target = args.skills_dir.expanduser() / args.skill
if target.is_symlink() and target.resolve() == source.resolve():
    print("Already installed:", args.skill)
elif target.exists() or target.is_symlink():
    parser.error("Existing skill was preserved; reconcile it before installation.")
else:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=True)
    print("Installed:", args.skill)
