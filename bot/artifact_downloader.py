from __future__ import annotations

import io
import os
import pathlib
import zipfile
from dataclasses import dataclass
from typing import Optional

from github_client import GitHubClient, WorkflowRunInfo


class ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedArtifact:
    run: WorkflowRunInfo
    artifact_id: int
    extracted_dir: str
    binary_path: str


def _safe_mkdir(path: str) -> None:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def _extract_zip_bytes(zip_bytes: bytes, dest_dir: str) -> list[str]:
    _safe_mkdir(dest_dir)
    extracted_files: list[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            zf.extract(member, path=dest_dir)
            extracted_files.append(os.path.join(dest_dir, member.filename))
    return extracted_files


def _pick_binary_path(extracted_files: list[str], binary_path_in_artifact: Optional[str]) -> str:
    if binary_path_in_artifact:
        # binary_path_in_artifact is relative to artifact root
        candidate = os.path.normpath(binary_path_in_artifact).lstrip("\\/")  # prevent absolute
        for f in extracted_files:
            if os.path.normpath(f).endswith(candidate):
                return f
        raise ArtifactError(f"binary_path_in_artifact '{binary_path_in_artifact}' not found in artifact zip")

    # Auto-pick if exactly one file extracted (common case).
    if len(extracted_files) == 1:
        return extracted_files[0]

    # Otherwise, prefer common names.
    preferred = {"solution", "solution.exe", "main", "a.out"}
    for f in extracted_files:
        if os.path.basename(f) in preferred:
            return f

    raise ArtifactError(
        "Artifact contains multiple files; set artifacts.binary_path_in_artifact in config to pick the binary explicitly"
    )


def find_latest_successful_run_for_sha(
    gh: GitHubClient, pr_number: int, head_sha: str, *, per_page: int = 50
) -> WorkflowRunInfo:
    runs = gh.list_workflow_runs_for_pull_request(pr_number, per_page=per_page)
    # Most recent first (GitHub already returns recent runs first).
    for run in runs:
        if run.head_sha != head_sha:
            continue
        if run.status != "completed":
            continue
        if (run.conclusion or "").lower() != "success":
            continue
        return run
    raise ArtifactError(f"No successful workflow run found for PR #{pr_number} head_sha={head_sha}")


def download_and_extract_binary(
    gh: GitHubClient,
    run: WorkflowRunInfo,
    *,
    artifact_name: str,
    dest_dir: str,
    binary_path_in_artifact: Optional[str] = None,
) -> DownloadedArtifact:
    artifacts = gh.list_artifacts_for_run(run.id)
    target = None
    for a in artifacts:
        if a.get("name") == artifact_name and not a.get("expired", False):
            target = a
            break
    if not target:
        available = ", ".join(str(a.get("name")) for a in artifacts)
        raise ArtifactError(f"Artifact '{artifact_name}' not found for run {run.id}. Available: [{available}]")

    artifact_id = int(target["id"])
    zip_bytes = gh.download_artifact_zip(artifact_id)

    extracted_dir = os.path.join(dest_dir, f"run-{run.id}-artifact-{artifact_id}")
    files = _extract_zip_bytes(zip_bytes, extracted_dir)
    if not files:
        raise ArtifactError("Downloaded artifact zip is empty")

    binary_path = _pick_binary_path(files, binary_path_in_artifact)
    return DownloadedArtifact(run=run, artifact_id=artifact_id, extracted_dir=extracted_dir, binary_path=binary_path)

