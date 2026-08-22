#!/usr/bin/env python3
"""Run one ephemeral Sol Max routing assessment for auto-dispatch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from state import (
    ROUTES,
    SNAPSHOT_DIR_RE,
    StateError,
    assessor_lease,
    clean_snapshot,
    file_sha256,
    require_sha256,
    validate_temp_file_path,
)


def fail(message: str) -> int:
    print(f"auto-dispatch assessor: {message}", file=sys.stderr)
    return 1


def clean_scratch(scratch_dir: Path) -> bool:
    try:
        clean_snapshot(scratch_dir / "route-brief.md")
    except (OSError, StateError) as exc:
        print(f"auto-dispatch assessor: cleanup failed: {exc}", file=sys.stderr)
        return False
    return True


class TerminationRequested(Exception):
    pass


def request_termination(_signum, _frame) -> None:
    raise TerminationRequested


def run_command(
    command: list[str], scratch_dir: Path, lease_descriptor: int
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        command,
        cwd=scratch_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(lease_descriptor,),
    )
    try:
        stdout, stderr = process.communicate()
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
    parser.add_argument("--expect-sha256")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate local prerequisites without running an assessment",
    )
    args = parser.parse_args()

    skill_dir = Path(__file__).resolve().parent.parent
    schema_path = skill_dir / "assets" / "route.schema.json"
    codex_path = shutil.which("codex")

    if args.check:
        try:
            validate_schema(schema_path)
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return fail(f"invalid schema: {exc}")
        if codex_path is None:
            return fail("codex executable not found")
        print(f"OK: {schema_path}")
        return 0
    if args.brief is None:
        parser.error("brief is required unless --check is used")
    if args.expect_sha256 is None:
        parser.error("--expect-sha256 is required unless --check is used")

    try:
        expected_sha256 = require_sha256(
            args.expect_sha256, "expected snapshot SHA-256"
        )
        brief_path = validate_temp_file_path(
            args.brief,
            SNAPSHOT_DIR_RE,
            "route-brief.md",
            "brief",
        )
    except StateError as exc:
        return fail(str(exc))
    scratch_dir = brief_path.parent
    keep_snapshot = False
    previous_handlers = {
        signum: signal.signal(signum, request_termination)
        for signum in (signal.SIGHUP, signal.SIGTERM)
    }
    try:
        with assessor_lease(brief_path) as lease_descriptor:
            try:
                _, actual_sha256 = file_sha256(
                    brief_path,
                    SNAPSHOT_DIR_RE,
                    "route-brief.md",
                    "brief",
                )
                if actual_sha256 != expected_sha256:
                    raise StateError(
                        "brief hash does not match the assessment reservation"
                    )
                validate_schema(schema_path)
                if codex_path is None:
                    return fail("codex executable not found")

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
                completed = run_command(command, scratch_dir, lease_descriptor)
                if completed.returncode != 0:
                    if completed.stdout:
                        print(completed.stdout.rstrip(), file=sys.stderr)
                    if completed.stderr:
                        print(completed.stderr.rstrip(), file=sys.stderr)
                    return completed.returncode

                result = validate_result(output_path)
                keep_snapshot = True
                print(json.dumps(result, ensure_ascii=False))
                return 0
            except TerminationRequested:
                return fail("termination requested")
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                return fail(str(exc))
            finally:
                if not keep_snapshot:
                    clean_scratch(scratch_dir)
    except StateError as exc:
        return fail(str(exc))
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
