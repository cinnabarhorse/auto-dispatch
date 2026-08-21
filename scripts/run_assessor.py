#!/usr/bin/env python3
"""Run one ephemeral Sol Max routing assessment for auto-dispatch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROUTES = (
    "luna-low",
    "luna-medium",
    "terra-low",
    "terra-medium",
    "terra-high",
    "terra-xhigh",
    "sol-medium",
    "sol-high",
    "sol-xhigh",
    "sol-max",
)


def fail(message: str) -> int:
    print(f"auto-dispatch assessor: {message}", file=sys.stderr)
    return 1


def clean_scratch(scratch_dir: Path) -> bool:
    try:
        shutil.rmtree(scratch_dir)
    except OSError as exc:
        print(f"auto-dispatch assessor: cleanup failed: {exc}", file=sys.stderr)
        return False
    return True


def validate_schema(schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    routes = tuple(schema["properties"]["route"]["enum"])
    if routes != ROUTES:
        raise ValueError("route schema does not match the wrapper route set")


def validate_result(output_path: Path) -> dict[str, str]:
    result = json.loads(output_path.read_text(encoding="utf-8"))
    if set(result) != {"route", "reason"}:
        raise ValueError("result must contain only route and reason")
    if result["route"] not in ROUTES:
        raise ValueError(f"unsupported route: {result['route']!r}")
    if not isinstance(result["reason"], str) or not result["reason"].strip():
        raise ValueError("reason must be a non-empty string")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("brief", nargs="?", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate local prerequisites without running an assessment",
    )
    args = parser.parse_args()

    skill_dir = Path(__file__).resolve().parent.parent
    schema_path = skill_dir / "assets" / "route.schema.json"
    codex_path = shutil.which("codex")

    try:
        validate_schema(schema_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail(f"invalid schema: {exc}")
    if codex_path is None:
        return fail("codex executable not found")
    if args.check:
        print(f"OK: {schema_path}")
        return 0
    if args.brief is None:
        parser.error("brief is required unless --check is used")

    brief_path = args.brief.resolve()
    scratch_dir = brief_path.parent
    if (
        brief_path.name != "route-brief.md"
        or not scratch_dir.name.startswith("auto-dispatch.")
        or scratch_dir.parent.resolve() != Path("/tmp").resolve()
        or not brief_path.is_file()
    ):
        return fail("brief must be /tmp/auto-dispatch.*/route-brief.md")

    output_path = scratch_dir / "route.json"
    output_path.unlink(missing_ok=True)
    command = [
        codex_path,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="max"',
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
        "-C",
        str(scratch_dir),
        f"Read {brief_path.name} and return only the routing decision.",
    ]
    completed = subprocess.run(
        command,
        cwd=scratch_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout.rstrip(), file=sys.stderr)
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)
        clean_scratch(scratch_dir)
        return completed.returncode

    try:
        result = validate_result(output_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        clean_scratch(scratch_dir)
        return fail(f"invalid assessor result: {exc}")
    if not clean_scratch(scratch_dir):
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
