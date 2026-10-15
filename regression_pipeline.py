#!/usr/bin/env python3
"""Syzeteo regression pipeline.

Runs safe, repeatable regression checks without touching the configured
persistent database. By default, release-hygiene tests are excluded so local
presentation profiles such as ``locales/de_bsds.json`` can be validated and
used during development. Pass ``--release`` to include the public-release
hygiene suite as well.
"""

from __future__ import annotations

import argparse
import compileall
import json
import os
import shutil
import subprocess
import string
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
REQUIRED_FILES = (
    "app.py",
    "auth.py",
    "config.py",
    "i18n.py",
    "storage.py",
    "requirements.txt",
    "locales/en.json",
    "locales/de.json",
)


@dataclass
class StageResult:
    name: str
    ok: bool
    seconds: float
    detail: str


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def preflight() -> StageResult:
    started = time.monotonic()
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    if missing:
        return StageResult(
            "preflight",
            False,
            time.monotonic() - started,
            "missing required files: " + ", ".join(missing),
        )

    if sys.version_info < (3, 10):
        return StageResult(
            "preflight",
            False,
            time.monotonic() - started,
            f"Python 3.10+ required; found {sys.version.split()[0]}",
        )

    return StageResult(
        "preflight",
        True,
        time.monotonic() - started,
        f"Python {sys.version.split()[0]} | root={ROOT}",
    )


def compile_python() -> StageResult:
    started = time.monotonic()
    ok = compileall.compile_dir(
        str(ROOT),
        quiet=1,
        force=True,
        rx=None,
        maxlevels=10,
    )
    return StageResult(
        "python-compile",
        bool(ok),
        time.monotonic() - started,
        "all Python files compile" if ok else "one or more Python files failed to compile",
    )


def validate_locales() -> StageResult:
    started = time.monotonic()
    locale_dir = ROOT / "locales"
    errors: list[str] = []

    try:
        reference = json.loads((locale_dir / "en.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return StageResult(
            "locale-validation",
            False,
            time.monotonic() - started,
            f"cannot load locales/en.json: {exc}",
        )

    if not isinstance(reference, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in reference.items()
    ):
        return StageResult(
            "locale-validation",
            False,
            time.monotonic() - started,
            "locales/en.json must be a string-to-string JSON object",
        )

    ref_keys = set(reference)
    catalogs = sorted(locale_dir.glob("*.json"))
    for path in catalogs:
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path.name}: invalid UTF-8/JSON ({exc})")
            continue

        if not isinstance(catalog, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in catalog.items()
        ):
            errors.append(f"{path.name}: catalog must contain only string keys and values")
            continue

        keys = set(catalog)
        missing = sorted(ref_keys - keys)
        extra = sorted(keys - ref_keys)
        if missing:
            errors.append(f"{path.name}: missing keys: {', '.join(missing)}")
        if extra:
            errors.append(f"{path.name}: extra keys: {', '.join(extra)}")

        for key in sorted(ref_keys & keys):
            if _fields(reference[key]) != _fields(catalog[key]):
                errors.append(f"{path.name}: placeholder mismatch for {key}")

        language_name = catalog.get("language.name", "").strip()
        if not language_name:
            errors.append(f"{path.name}: language.name is empty or missing")

    detail = f"validated {len(catalogs)} catalog(s): " + ", ".join(p.stem for p in catalogs)
    if errors:
        detail += "\n" + "\n".join(errors)
    return StageResult(
        "locale-validation",
        not errors,
        time.monotonic() - started,
        detail,
    )


def discover_test_modules(include_release: bool) -> list[str]:
    modules: list[str] = []
    for path in sorted(ROOT.glob("test_*.py")):
        if path.name == "test_release.py" and not include_release:
            continue
        modules.append(path.stem)
    return modules



def _short_test_label(stripped: str) -> str:
    """Return compact '<file.py> - <task>' text for unittest verbose output."""
    head = stripped.split(" ... ", 1)[0]
    method, qualified = head.split(" (", 1)
    module = qualified.split(".", 1)[0]
    filename = f"{module}.py"
    task = method.removeprefix("test_").replace("_", " ")
    return f"{filename} - {task}"


def number_test_output(output: str) -> str:
    """Reduce verbose unittest result lines to numbered compact summaries."""
    numbered: list[str] = []
    counter = 0
    for line in output.splitlines():
        stripped = line.lstrip()
        is_test_result = (
            stripped.startswith("test_")
            and " ... " in stripped
            and stripped.rsplit(" ... ", 1)[-1] in {"ok", "FAIL", "ERROR", "skipped"}
        )
        if is_test_result:
            counter += 1
            status = stripped.rsplit(" ... ", 1)[-1]
            line = f"[{counter:03d}] {_short_test_label(stripped)} - {status}"
        elif stripped.startswith("test_"):
            continue
        numbered.append(line)
    return "\n".join(numbered)

def run_unittests(include_release: bool, verbosity: int, failfast: bool, native: bool) -> StageResult:
    started = time.monotonic()
    modules = discover_test_modules(include_release)
    if not modules:
        return StageResult(
            "unittest-regression",
            False,
            time.monotonic() - started,
            "no test_*.py modules found",
        )

    unit_args = ["python", "-m", "unittest"]
    if verbosity >= 2:
        unit_args.append("-v")
    if failfast:
        unit_args.append("-f")
    unit_args.extend(modules)

    with tempfile.TemporaryDirectory(prefix="syzeteo-regression-") as td:
        if native:
            cmd = [sys.executable, *unit_args[1:]]
            env = os.environ.copy()
            env["SYZETEO_DATA_DIR"] = td
        else:
            if not shutil.which("docker"):
                return StageResult(
                    "unittest-regression",
                    False,
                    time.monotonic() - started,
                    "docker executable not found; use --native only when host dependencies are installed",
                )
            cmd = [
                "docker", "compose", "run", "--rm", "--no-deps",
                "-e", "SYZETEO_DATA_DIR=/data",
                "-v", f"{ROOT}:/app",
                "-v", f"{td}:/data",
                "syzeteo",
                *unit_args,
            ]
            env = os.environ.copy()

        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )

        captured: list[str] = []
        counter = 0
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            captured.append(line)
            stripped = line.lstrip()
            is_test_result = (
                stripped.startswith("test_")
                and " ... " in stripped
                and stripped.rsplit(" ... ", 1)[-1] in {"ok", "FAIL", "ERROR", "skipped"}
            )
            if is_test_result:
                counter += 1
                status = stripped.rsplit(" ... ", 1)[-1]
                print(f"[{counter:03d}] {_short_test_label(stripped)} - {status}", flush=True)

        returncode = proc.wait()

    mode = "release" if include_release else "regression"
    runner = "native" if native else "docker"
    failures = "\n".join(
        line for line in captured
        if line.startswith("FAIL:")
        or line.startswith("ERROR:")
        or line.startswith("AssertionError:")
        or line.startswith("Traceback")
    )
    detail = f"runner={runner}; mode={mode}; modules={len(modules)}"
    if failures:
        detail += "\n" + failures
    return StageResult(
        "unittest-regression",
        returncode == 0,
        time.monotonic() - started,
        detail,
    )


def print_result(result: StageResult, verbose: bool = False) -> None:
    status = "PASS" if result.ok else "FAIL"
    print(f"[{status}] {result.name} ({result.seconds:.2f}s)")
    if verbose or not result.ok:
        print(result.detail)


def write_json_report(path: Path, results: Iterable[StageResult], release: bool) -> None:
    payload = {
        "pipeline": "syzeteo-regression",
        "release_mode": release,
        "ok": all(r.ok for r in results),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Syzeteo regression pipeline safely against the source tree."
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="also run test_release.py; intended for the clean public release tree",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="run unit tests with host Python instead of Docker; host dependencies must be installed",
    )
    parser.add_argument(
        "--failfast",
        action="store_true",
        help="stop unittest execution at the first failure",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show details for successful stages and verbose unittest output",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="write a machine-readable JSON report to this path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results: list[StageResult] = []

    first = preflight()
    results.append(first)
    print_result(first, args.verbose)
    if not first.ok:
        if args.json_report:
            write_json_report(args.json_report, results, args.release)
        return 2

    for stage in (compile_python, validate_locales):
        result = stage()
        results.append(result)
        print_result(result, args.verbose)
        if not result.ok and args.failfast:
            if args.json_report:
                write_json_report(args.json_report, results, args.release)
            return 1

    unit = run_unittests(args.release, 2 if args.verbose else 1, args.failfast, args.native)
    results.append(unit)
    print_result(unit, args.verbose)

    if args.json_report:
        write_json_report(args.json_report, results, args.release)
        print(f"JSON report: {args.json_report}")

    passed = sum(1 for r in results if r.ok)
    print(f"\nSyzeteo regression: {passed}/{len(results)} stages PASS")
    print("Runner:", "native" if args.native else "docker")
    if args.release:
        print("Mode: release (includes public-release hygiene tests)")
    else:
        print("Mode: regression (release-hygiene tests excluded; local locale profiles allowed)")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
