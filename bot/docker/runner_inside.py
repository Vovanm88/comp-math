from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import psutil
import yaml


@dataclass(frozen=True)
class ProcMetrics:
    wall_s: float
    cpu_user_s: float
    cpu_system_s: float
    peak_rss_bytes: int
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str


def _read_text(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _normalize_output(s: str) -> str:
    # Normalize trailing whitespace/newlines to reduce false negatives.
    return s.replace("\r\n", "\n").rstrip() + "\n"


def run_with_metrics(cmd: list[str], *, stdin_bytes: bytes, timeout_s: float) -> ProcMetrics:
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ps_proc = psutil.Process(proc.pid)
    peak_rss = 0
    timed_out = False

    if proc.stdin:
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    # Poll until exit/timeout; sample memory.
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.perf_counter()
        if now - t0 > timeout_s:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            break
        try:
            rss = ps_proc.memory_info().rss
            if rss > peak_rss:
                peak_rss = rss
        except Exception:
            pass
        time.sleep(0.01)

    try:
        out_b, err_b = proc.communicate(timeout=0.2)
    except Exception:
        out_b, err_b = b"", b""

    t1 = time.perf_counter()
    cpu_user = 0.0
    cpu_sys = 0.0
    try:
        ct = ps_proc.cpu_times()
        cpu_user = float(ct.user)
        cpu_sys = float(ct.system)
    except Exception:
        pass

    exit_code = proc.returncode if proc.returncode is not None else -1
    stdout = out_b.decode("utf-8", errors="replace")
    stderr = err_b.decode("utf-8", errors="replace")
    return ProcMetrics(
        wall_s=float(t1 - t0),
        cpu_user_s=cpu_user,
        cpu_system_s=cpu_sys,
        peak_rss_bytes=int(peak_rss),
        exit_code=int(exit_code),
        timed_out=bool(timed_out),
        stdout=stdout,
        stderr=stderr,
    )


def discover_public_cases(public_dir: str) -> list[tuple[str, str, str]]:
    p = pathlib.Path(public_dir)
    if not p.exists():
        return []

    # 1) Prefer *.in/*.out pairs
    ins = sorted(p.glob("*.in"))
    outs = {x.stem: x for x in p.glob("*.out")}
    cases: list[tuple[str, str, str]] = []
    for in_path in ins:
        out_path = outs.get(in_path.stem)
        if out_path:
            cases.append((in_path.stem, str(in_path), str(out_path)))
    if cases:
        return cases

    # 2) Fallback: inputN.txt/outputN.txt
    inputs = sorted(p.glob("input*.txt"))
    outputs = {x.name.replace("output", "input"): x for x in p.glob("output*.txt")}
    for in_path in inputs:
        out_path = outputs.get(in_path.name)
        if out_path:
            stem = in_path.stem.replace("input", "")
            cases.append((stem or in_path.stem, str(in_path), str(out_path)))
    return cases


def load_secret_bench_manifest(secret_dir: str) -> dict[str, Any]:
    p = pathlib.Path(secret_dir) / "bench.yaml"
    if not p.exists():
        return {"cases": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"cases": []}


def discover_secret_cases(secret_dir: str) -> list[dict[str, Any]]:
    manifest = load_secret_bench_manifest(secret_dir)
    cases = manifest.get("cases")
    if isinstance(cases, list) and cases:
        return cases

    # Fallback: run every *.in from secret_dir/cases (or secret_dir)
    base = pathlib.Path(secret_dir)
    candidates = list((base / "cases").glob("*.in")) if (base / "cases").exists() else list(base.glob("*.in"))
    return [{"name": c.stem, "type": "file", "path": str(c), "repeats": 1} for c in sorted(candidates)]


def run_public_tests(solution_path: str, public_dir: str, timeout_s: float) -> dict[str, Any]:
    cases = discover_public_cases(public_dir)
    results: list[dict[str, Any]] = []
    all_ok = True
    total_wall = 0.0

    for name, in_path, out_path in cases:
        stdin_bytes = pathlib.Path(in_path).read_bytes()
        expected = _normalize_output(_read_text(out_path))
        m = run_with_metrics([solution_path], stdin_bytes=stdin_bytes, timeout_s=timeout_s)
        total_wall += m.wall_s
        got = _normalize_output(m.stdout)
        ok = (not m.timed_out) and (m.exit_code == 0) and (got == expected)
        if not ok:
            all_ok = False
        results.append(
            {
                "name": name,
                "ok": ok,
                "exit_code": m.exit_code,
                "timed_out": m.timed_out,
                "wall_s": m.wall_s,
                "cpu_user_s": m.cpu_user_s,
                "cpu_system_s": m.cpu_system_s,
                "peak_rss_bytes": m.peak_rss_bytes,
                "stderr_tail": m.stderr[-4000:],
            }
        )

    return {"passed": all_ok, "case_count": len(cases), "total_wall_s": total_wall, "cases": results}


def _run_bench_case(solution_path: str, case: dict[str, Any], secret_dir: str, timeout_s: float) -> list[ProcMetrics]:
    ctype = case.get("type", "file")
    repeats = int(case.get("repeats", 1) or 1)

    if ctype == "file":
        path = case["path"]
        stdin_bytes = pathlib.Path(path).read_bytes()
        return [run_with_metrics([solution_path], stdin_bytes=stdin_bytes, timeout_s=timeout_s) for _ in range(repeats)]

    if ctype == "generator":
        cmd = case.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise ValueError("generator case requires command: [..]")
        # Run generator INSIDE the same container; it must live under secret_dir.
        gen = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
        stdin_bytes = gen.stdout
        return [run_with_metrics([solution_path], stdin_bytes=stdin_bytes, timeout_s=timeout_s) for _ in range(repeats)]

    raise ValueError(f"Unknown benchmark case type: {ctype}")


def run_secret_benchmark(solution_path: str, secret_dir: str, timeout_s: float) -> dict[str, Any]:
    cases = discover_secret_cases(secret_dir)
    if not cases:
        return {"ran": False, "reason": "no_cases", "cases": []}

    all_metrics: list[dict[str, Any]] = []
    total_wall = 0.0
    total_cpu_user = 0.0
    total_cpu_sys = 0.0
    peak_rss = 0

    for case in cases:
        name = str(case.get("name") or "case")
        metrics_list = _run_bench_case(solution_path, case, secret_dir, timeout_s)
        for i, m in enumerate(metrics_list):
            total_wall += m.wall_s
            total_cpu_user += m.cpu_user_s
            total_cpu_sys += m.cpu_system_s
            peak_rss = max(peak_rss, m.peak_rss_bytes)
            all_metrics.append(
                {
                    "name": name,
                    "repeat": i + 1,
                    "exit_code": m.exit_code,
                    "timed_out": m.timed_out,
                    "wall_s": m.wall_s,
                    "cpu_user_s": m.cpu_user_s,
                    "cpu_system_s": m.cpu_system_s,
                    "peak_rss_bytes": m.peak_rss_bytes,
                    "stderr_tail": m.stderr[-4000:],
                }
            )

    # Score: total wall time (smaller is better).
    score = total_wall
    return {
        "ran": True,
        "case_exec_count": len(all_metrics),
        "total_wall_s": total_wall,
        "total_cpu_user_s": total_cpu_user,
        "total_cpu_system_s": total_cpu_sys,
        "peak_rss_bytes": peak_rss,
        "score": score,
        "score_unit": "seconds_total_wall",
        "cases": all_metrics,
    }


def _prepare_solution(binary_path: str) -> str:
    # Copy to /tmp to ensure exec perms even when mounted from Windows.
    dst = "/tmp/solution"
    shutil.copy2(binary_path, dst)
    os.chmod(dst, 0o755)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True, help="Path to binary inside container (mounted read-only).")
    ap.add_argument("--public-tests", required=True, help="Path to public tests directory inside container (ro).")
    ap.add_argument("--secret-tests", default=None, help="Path to secret tests directory inside container (ro).")
    ap.add_argument("--per-test-timeout", type=float, default=2.0)
    ap.add_argument("--out", required=True, help="Output JSON path (must be writable).")
    args = ap.parse_args()

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "public": None,
        "benchmark": None,
        "meta": {
            "binary": args.binary,
            "public_tests": args.public_tests,
            "secret_tests": args.secret_tests,
            "timeout_s": args.per_test_timeout,
        },
    }

    try:
        solution = _prepare_solution(args.binary)
        public_res = run_public_tests(solution, args.public_tests, args.per_test_timeout)
        result["public"] = public_res

        if public_res.get("passed") and args.secret_tests and pathlib.Path(args.secret_tests).exists():
            result["benchmark"] = run_secret_benchmark(solution, args.secret_tests, args.per_test_timeout)
        else:
            result["benchmark"] = {"ran": False, "reason": "public_failed_or_no_secret"}

        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as e:
        # Best effort: emit failure JSON.
        result["error"] = {"message": str(e), "type": e.__class__.__name__}
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

