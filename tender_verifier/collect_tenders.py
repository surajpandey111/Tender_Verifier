r"""
For the common real-world situation: you already have ~400 folders, one per
tender submission (each with its own PDFs inside, however they're nested),
but they're scattered across a few different download locations instead of
sitting together in one tenders_root — this script gathers them into one
place in a single run, so you never manually create or drag 400 folders.

Usage:
    # Preview first (nothing is copied yet) — always do this first:
    python collect_tenders.py --sources "D:\Downloads\PortalBatch1" "D:\Downloads\PortalBatch2" --dest samples\tenders_root --dry-run

    # Then actually run it:
    python collect_tenders.py --sources "D:\Downloads\PortalBatch1" "D:\Downloads\PortalBatch2" --dest samples\tenders_root

    # If your 400 folders are all already sitting together in one place,
    # just pass that one folder as --sources — main.py can likely already
    # point straight at it without running this script at all.

What it does:
    Every immediate subfolder of each --sources path is treated as ONE
    tender submission (whatever PDFs/nesting exists inside it travels along
    as-is) and is copied into --dest with its original folder name. Name
    collisions (two different sources happening to have a folder with the
    same name) are handled by suffixing _2, _3, etc. — never silently
    overwritten.

    Copies by default (originals untouched, safer). Pass --move to move
    instead once you've verified the copy worked and want to save disk space.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def find_tender_folders(source: Path) -> list[Path]:
    """Every immediate subdirectory of `source` is treated as one tender's folder."""
    if not source.exists():
        print(f"  [SKIP] Source does not exist: {source}")
        return []
    return sorted(p for p in source.iterdir() if p.is_dir())


def unique_dest_name(dest_root: Path, name: str) -> Path:
    candidate = dest_root / name
    if not candidate.exists():
        return candidate
    i = 2
    while (dest_root / f"{name}_{i}").exists():
        i += 1
    return dest_root / f"{name}_{i}"


def main():
    parser = argparse.ArgumentParser(description="Gather scattered per-tender folders into one tenders_root.")
    parser.add_argument("--sources", nargs="+", required=True, help="One or more parent folders; every immediate subfolder inside each is treated as one tender.")
    parser.add_argument("--dest", required=True, help="Destination tenders_root folder (created if it doesn't exist).")
    parser.add_argument("--move", action="store_true", help="Move instead of copy (default: copy, originals untouched).")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would happen without touching any files.")
    args = parser.parse_args()

    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)

    all_tender_folders: list[Path] = []
    for src in args.sources:
        found = find_tender_folders(Path(src))
        print(f"Source '{src}': found {len(found)} tender folder(s).")
        all_tender_folders.extend(found)

    if not all_tender_folders:
        print("\nNo tender folders found. Check your --sources paths.")
        return

    print(f"\nTotal: {len(all_tender_folders)} tender folder(s) to {'move' if args.move else 'copy'} into {dest_root}\n")

    pdf_warnings = []
    for folder in all_tender_folders:
        dest = unique_dest_name(dest_root, folder.name)
        pdf_count = len(list(folder.rglob("*.pdf")))
        if pdf_count == 0:
            pdf_warnings.append(folder.name)

        action = "MOVE" if args.move else "COPY"
        print(f"  [{action}] {folder}  ->  {dest}   ({pdf_count} PDF file(s) inside)")

        if not args.dry_run:
            if args.move:
                shutil.move(str(folder), str(dest))
            else:
                shutil.copytree(folder, dest)

    if args.dry_run:
        print("\n--dry-run: nothing was actually copied/moved. Remove --dry-run to execute.")
    else:
        print(f"\nDone. {len(all_tender_folders)} tender folder(s) now in {dest_root}")

    if pdf_warnings:
        print(f"\nWARNING: {len(pdf_warnings)} folder(s) had ZERO PDF files inside them (main.py will report these as errors):")
        for name in pdf_warnings:
            print(f"    - {name}")


if __name__ == "__main__":
    main()
