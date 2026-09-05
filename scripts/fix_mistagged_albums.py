#!/usr/bin/env python3
"""Repair two mis-tagged albums in the music library.

Both problems are in the file tags, so this must run ON the machine that stores
the music (the Navidrome host, 192.168.0.7), then Navidrome must rescan.

  1. Blondie - "Plastic Letters": the 13 tracks sit in per-track folders
     01/ .. 13/ and each file's ALBUMARTIST is the track number instead of
     "Blondie", so Navidrome shows 13 one-track albums by artists "01".."13".

  2. The Smashing Pumpkins - "Siamese Dream": ALBUMARTIST holds two values,
     "(The) Smashing Pumpkins" and "Smashing Pumpkins", so the artist shows up
     as "(The) Smashing Pumpkins * Smashing Pumpkins".

Usage:
    python3 fix_mistagged_albums.py /path/to/music            # dry run
    python3 fix_mistagged_albums.py /path/to/music --apply    # write tags
    python3 fix_mistagged_albums.py /path/to/music --apply --consolidate
        also move the scattered Blondie files into <root>/Blondie/Plastic Letters/

Needs: pip install mutagen   (handles ID3 and FLAC/Vorbis the same way)
"""
import argparse
import sys
from pathlib import Path

import mutagen


def retag(path: Path, apply: bool, **wanted: str) -> bool:
    audio = mutagen.File(path, easy=True)
    if audio is None:
        print(f"  ??  cannot read: {path}")
        return False
    diff = {k: (audio.get(k), [v]) for k, v in wanted.items() if audio.get(k) != [v]}
    if not diff:
        print(f"  ok  {path.name}")
        return False
    print(f"  fix {path}")
    for k, (old, new) in diff.items():
        print(f"        {k}: {old!r} -> {new!r}")
    if apply:
        for k, v in wanted.items():
            audio[k] = [v]
        audio.save()
    return True


def fix_blondie(root: Path, apply: bool, consolidate: bool) -> int:
    print("\n== Blondie / Plastic Letters ==")
    dest = root / "Blondie" / "Plastic Letters"
    touched = 0
    for n in range(1, 14):
        folder = root / f"{n:02d}" / "Plastic Letters"
        if not folder.is_dir():
            print(f"  --  not found: {folder}")
            continue
        for f in sorted(folder.glob("*.mp3")):
            if retag(f, apply, albumartist="Blondie", album="Plastic Letters", artist="Blondie"):
                touched += 1
            if consolidate and apply:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f.name
                if not target.exists():
                    f.rename(target)
        if consolidate and apply:
            for d in (folder, folder.parent):
                try:
                    d.rmdir()
                except OSError:
                    pass
    return touched


def fix_pumpkins(root: Path, apply: bool) -> int:
    print("\n== The Smashing Pumpkins / Siamese Dream ==")
    name = "The Smashing Pumpkins"
    touched = 0
    candidates = list(root.glob("*Smashing Pumpkins*/Siamese Dream")) or \
        list(root.glob("**/Siamese Dream"))
    for folder in candidates:
        for f in sorted(folder.glob("*.flac")) + sorted(folder.glob("*.mp3")):
            if retag(f, apply, albumartist=name, artist=name):
                touched += 1
    if not candidates:
        print("  --  no 'Siamese Dream' folder found under", root)
    return touched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="music library root")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--consolidate", action="store_true", help="also move scattered Blondie files into one folder")
    args = ap.parse_args()

    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2

    total = fix_blondie(args.root, args.apply, args.consolidate)
    total += fix_pumpkins(args.root, args.apply)

    print(f"\n{'wrote' if args.apply else 'would change'} {total} file(s).")
    if args.apply:
        print("Now rescan Navidrome:  Settings > click the rescan icon, or")
        print("  curl 'http://192.168.0.7:4533/rest/startScan?u=USER&p=PASS&v=1.16.1&c=fix&f=json'")
    else:
        print("Re-run with --apply (optionally --consolidate) to make the changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
