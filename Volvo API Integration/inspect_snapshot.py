#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inspect_snapshot.py — print the SHAPE of sample_snapshot.json (not the bulk data).

Shows keys, types, array lengths, the keys inside the first array element, and a
short sample value for scalars. This is enough to map the 12 report columns and
fix the envelope unwrapping without pasting the whole file.

    python inspect_snapshot.py                 # reads sample_snapshot.json
    python inspect_snapshot.py other_file.json
"""
import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "sample_snapshot.json"
MAX_DEPTH = 9
STR_PREVIEW = 60


def short(v):
    s = repr(v)
    return s if len(s) <= STR_PREVIEW else s[:STR_PREVIEW] + "…"


def walk(node, depth=0, label=""):
    pad = "  " * depth
    if depth > MAX_DEPTH:
        print(f"{pad}{label}…(deeper)")
        return

    if isinstance(node, dict):
        print(f"{pad}{label}{{}} dict ({len(node)} keys)")
        for k, v in node.items():
            walk(v, depth + 1, f"{k}: ")
    elif isinstance(node, list):
        print(f"{pad}{label}[] list (len={len(node)})")
        if node:
            # describe just the first element to reveal element shape
            walk(node[0], depth + 1, "[0] ")
    else:
        print(f"{pad}{label}{type(node).__name__} = {short(node)}")


def main():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Not found: {PATH}")
    except json.JSONDecodeError as e:
        sys.exit(f"Bad JSON in {PATH}: {e}")

    print(f"=== structure of {PATH} ===")
    walk(data)


if __name__ == "__main__":
    main()
