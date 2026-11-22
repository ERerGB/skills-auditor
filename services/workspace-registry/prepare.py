"""Prepare a private immutable release import or a pointer promotion for Wrangler D1."""
import argparse
import os
from pathlib import Path

from skills_auditor.workspace.catalog import DIGEST, PART, encode, sha
from skills_auditor.workspace.common import identifier, read_json


def stage(bundle):
    wire = encode(bundle)
    workspace, key = bundle["workspace"], sha(wire)
    statements = []
    for index, offset in enumerate(range(0, len(wire), PART)):
        part = wire[offset:offset + PART].hex()
        statements.append(f"INSERT INTO chunks VALUES ('{workspace}','{key}',{index},X'{part}');")
    statements.append(f"INSERT INTO releases VALUES ('{workspace}','{key}',{len(statements)},{len(wire)});")
    return key, statements


def promote(workspace, key, previous_generation):
    identifier(workspace)
    if not DIGEST.fullmatch(key) or previous_generation < 0:
        raise ValueError("Invalid release pin or generation.")
    return f"INSERT INTO promotions VALUES ('{workspace}',{previous_generation + 1},'{key}');"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="op", required=True)
    up = sub.add_parser("stage")
    up.add_argument("bundle", type=Path)
    switch = sub.add_parser("promote")
    switch.add_argument("workspace")
    switch.add_argument("digest")
    switch.add_argument("previous_generation", type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.op == "stage":
        pin, sql = stage(read_json(args.bundle))
    else:
        pin, sql = args.digest, [promote(args.workspace, args.digest, args.previous_generation)]
    fd = os.open(args.out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write("\n".join(sql) + "\n")
    print(pin)
