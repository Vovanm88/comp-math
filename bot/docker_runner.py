from __future__ import annotations

import json
import os
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Any, Optional


class DockerRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerRunResult:
    result_json_path: str
    payload: dict[str, Any]


def _ensure_dir(path: str) -> None:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def run_in_docker(
    *,
    docker_image: str,
    extracted_artifact_dir: str,
    binary_path: str,
    public_tests_dir: str,
    out_dir: str,
    secret_tests_dir: Optional[str],
    cpus: float,
    memory: str,
    pids_limit: int,
    per_test_timeout_s: float,
) -> DockerRunResult:
    extracted_artifact_dir = os.path.abspath(extracted_artifact_dir)
    public_tests_dir = os.path.abspath(public_tests_dir)
    out_dir = os.path.abspath(out_dir)
    secret_tests_dir_abs = os.path.abspath(secret_tests_dir) if secret_tests_dir else None

    _ensure_dir(out_dir)

    # Compute binary path relative to artifact mount point.
    binary_abs = os.path.abspath(binary_path)
    try:
        rel = os.path.relpath(binary_abs, extracted_artifact_dir)
    except ValueError:
        rel = os.path.basename(binary_abs)
    container_binary = f"/artifact/{rel.replace(os.sep, '/')}"

    result_json_host = os.path.join(out_dir, "result.json")

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cpus",
        str(cpus),
        "--memory",
        str(memory),
        "--pids-limit",
        str(pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "-v",
        f"{extracted_artifact_dir}:/artifact:ro",
        "-v",
        f"{public_tests_dir}:/tests/open:ro",
        "-v",
        f"{out_dir}:/out:rw",
    ]

    if secret_tests_dir_abs:
        cmd += ["-v", f"{secret_tests_dir_abs}:/tests/secret:ro"]

    cmd += [
        docker_image,
        "--binary",
        container_binary,
        "--public-tests",
        "/tests/open",
        "--secret-tests",
        "/tests/secret" if secret_tests_dir_abs else "",
        "--per-test-timeout",
        str(per_test_timeout_s),
        "--out",
        "/out/result.json",
    ]

    # If no secret dir, pass arg but keep empty -> runner will skip.
    if not secret_tests_dir_abs:
        # Remove the empty secret-tests args to avoid confusion.
        i = cmd.index("--secret-tests")
        del cmd[i : i + 2]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DockerRunnerError(
            "Docker runner failed:\n"
            f"exit_code={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )

    if not os.path.exists(result_json_host):
        raise DockerRunnerError("Docker runner did not produce /out/result.json")

    payload = json.loads(pathlib.Path(result_json_host).read_text(encoding="utf-8"))
    return DockerRunResult(result_json_path=result_json_host, payload=payload)

