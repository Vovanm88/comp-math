from __future__ import annotations

import argparse
import os
import pathlib
import textwrap
import time
from typing import Any, Optional

from artifact_downloader import ArtifactError, download_and_extract_binary, find_latest_successful_run_for_sha
from docker_runner import DockerRunnerError, run_in_docker
from github_client import GitHubClient, PullRequestInfo
from leaderboard import (
    LeaderboardEntry,
    SortOrder,
    load_leaderboard,
    rank_of_entry,
    rankings_for_base,
    register_result,
    save_leaderboard,
)
from utils import atomic_write_json, load_json, load_yaml


BOT_MARKER = "<!-- pr-benchmark-bot -->"


def _abs_from_repo_root(repo_root: str, maybe_rel: str) -> str:
    p = pathlib.Path(maybe_rel)
    if p.is_absolute():
        return str(p)
    return str(pathlib.Path(repo_root) / maybe_rel)


def load_processed(path: str) -> set[tuple[int, str]]:
    data = load_json(path, default={"processed": []})
    out: set[tuple[int, str]] = set()
    for x in data.get("processed", []):
        try:
            out.add((int(x["pr"]), str(x["sha"])))
        except Exception:
            continue
    return out


def save_processed(path: str, processed: set[tuple[int, str]]) -> None:
    atomic_write_json(
        path,
        {"processed": [{"pr": pr, "sha": sha} for (pr, sha) in sorted(processed, key=lambda t: (t[0], t[1]))]},
    )


def upsert_pr_comment(gh: GitHubClient, pr_number: int, body: str) -> None:
    comments = gh.list_issue_comments(pr_number, per_page=100)
    for c in comments:
        if BOT_MARKER in (c.get("body") or ""):
            gh.update_issue_comment(int(c["id"]), body)
            return
    gh.create_issue_comment(pr_number, body)


def _format_top(rankings: list[LeaderboardEntry], top_n: int) -> str:
    lines = []
    for i, e in enumerate(rankings[:top_n], start=1):
        lines.append(f"{i}. `{e.participant}` — score={e.score:.6f} ({e.score_unit}), attempt={e.attempt}, PR #{e.pr_number}")
    return "\n".join(lines) if lines else "_Пока нет результатов (нужны пройденные открытые тесты + бенчмарк)._"


def _format_failed_cases(public_payload: dict[str, Any], limit: int = 5) -> str:
    cases = list(public_payload.get("cases") or [])
    bad = [c for c in cases if not c.get("ok")]
    if not bad:
        return ""
    lines = []
    for c in bad[:limit]:
        name = c.get("name")
        reason = []
        if c.get("timed_out"):
            reason.append("timeout")
        if c.get("exit_code") not in (0, "0", None):
            reason.append(f"exit={c.get('exit_code')}")
        lines.append(f"- `{name}` ({', '.join(reason) or 'wrong answer'})")
    if len(bad) > limit:
        lines.append(f"- ... и ещё {len(bad) - limit}")
    return "\n".join(lines)


def format_comment(
    pr: PullRequestInfo,
    run_url: str,
    artifact_name: str,
    public_payload: dict[str, Any],
    benchmark_payload: dict[str, Any],
    entry: LeaderboardEntry,
    rankings: list[LeaderboardEntry],
    rank: Optional[int],
    top_n: int,
) -> str:
    pub_ok = bool(public_payload.get("passed"))
    pub_count = int(public_payload.get("case_count") or 0)
    pub_wall = float(public_payload.get("total_wall_s") or 0.0)

    bench_ran = bool(benchmark_payload.get("ran"))
    bench_score = benchmark_payload.get("score")
    bench_unit = benchmark_payload.get("score_unit")
    bench_wall = benchmark_payload.get("total_wall_s")
    bench_peak = benchmark_payload.get("peak_rss_bytes")

    failed = _format_failed_cases(public_payload)

    parts = [
        BOT_MARKER,
        "## Автопроверка (бот)",
        "",
        f"- **PR**: #{pr.number} — {pr.title}",
        f"- **Автор**: `{pr.author_login}`",
        f"- **Head**: `{pr.head_repo_full_name}:{pr.head_ref}` (`{pr.head_sha[:12]}`)",
        f"- **Base ветка**: `{pr.base_ref}`",
        f"- **CI run**: {run_url}",
        f"- **Artifact**: `{artifact_name}`",
        "",
        "### Открытые тесты",
        f"- **Результат**: {'OK' if pub_ok else 'FAIL'} ({pub_count} тестов), wall={pub_wall:.3f}s",
    ]
    if (not pub_ok) and failed:
        parts += ["", "**Первые ошибки:**", failed]

    parts += ["", "### Бенчмарк (закрытые тесты)"]
    if pub_ok and bench_ran and bench_score is not None:
        parts += [
            f"- **Score**: {float(bench_score):.6f} ({bench_unit})",
            f"- **Суммарное wall**: {float(bench_wall or 0.0):.3f}s",
            f"- **Peak RSS**: {int(bench_peak or 0)} bytes",
        ]
    else:
        parts += ["- _Не запускался (нет закрытых тестов или не пройдены открытые)._"]

    parts += [
        "",
        "### Лидерборд",
        f"- **Ваша попытка**: №{entry.attempt}",
    ]
    if rank is not None:
        parts.append(f"- **Текущее место**: {rank}/{max(len(rankings), 1)} (по `{pr.base_ref}`)")
    else:
        parts.append(f"- **Текущее место**: _нет в рейтинге (нужно пройти открытые тесты и бенчмарк)_")

    parts += ["", f"**Топ-{top_n} (ветка `{pr.base_ref}`):**", _format_top(rankings, top_n)]
    return "\n".join(parts).strip() + "\n"


def process_one_pr(
    *,
    gh: GitHubClient,
    repo_root: str,
    pr: PullRequestInfo,
    cfg: dict[str, Any],
    processed: set[tuple[int, str]],
) -> bool:
    key = (pr.number, pr.head_sha)
    if key in processed:
        return False

    artifact_cfg = cfg["artifacts"]
    runner_cfg = cfg["runner"]
    tests_cfg = cfg["tests"]
    state_cfg = cfg["state"]
    lb_cfg = cfg.get("leaderboard") or {}

    work_dir = _abs_from_repo_root(repo_root, state_cfg["work_dir"])
    pr_dir = os.path.join(work_dir, f"pr-{pr.number}", pr.head_sha)
    out_dir = os.path.join(pr_dir, "out")
    os.makedirs(pr_dir, exist_ok=True)

    run = find_latest_successful_run_for_sha(gh, pr.number, pr.head_sha)
    dl = download_and_extract_binary(
        gh,
        run,
        artifact_name=str(artifact_cfg["artifact_name"]),
        dest_dir=os.path.join(pr_dir, "artifact"),
        binary_path_in_artifact=artifact_cfg.get("binary_path_in_artifact"),
    )

    public_tests_dir = _abs_from_repo_root(repo_root, tests_cfg["public_tests_dir"])
    secret_tests_dir = tests_cfg.get("secret_tests_dir")
    secret_tests_dir_abs = os.path.abspath(secret_tests_dir) if secret_tests_dir else None

    docker_res = run_in_docker(
        docker_image=str(runner_cfg["docker_image"]),
        extracted_artifact_dir=dl.extracted_dir,
        binary_path=dl.binary_path,
        public_tests_dir=public_tests_dir,
        out_dir=out_dir,
        secret_tests_dir=secret_tests_dir_abs,
        cpus=float(runner_cfg.get("cpus", 1.0)),
        memory=str(runner_cfg.get("memory", "1g")),
        pids_limit=int(runner_cfg.get("pids_limit", 256)),
        per_test_timeout_s=float(runner_cfg.get("per_test_timeout_seconds", 2.0)),
    )

    payload = docker_res.payload
    public_payload = payload.get("public") or {}
    benchmark_payload = payload.get("benchmark") or {}

    leaderboard_path = _abs_from_repo_root(repo_root, state_cfg["leaderboard_path"])
    entries = load_leaderboard(leaderboard_path)

    entry = register_result(
        entries,
        participant=pr.author_login,
        base_ref=pr.base_ref,
        pr_number=pr.number,
        commit_sha=pr.head_sha,
        public_passed=bool(public_payload.get("passed")),
        benchmark_payload=benchmark_payload,
        meta={
            "head_repo": pr.head_repo_full_name,
            "head_ref": pr.head_ref,
            "run_id": dl.run.id,
            "run_url": dl.run.html_url,
            "artifact_id": dl.artifact_id,
        },
    )
    save_leaderboard(leaderboard_path, entries)

    sort_order: SortOrder = str(lb_cfg.get("sort_order", "asc"))
    rankings = rankings_for_base(entries, pr.base_ref, sort_order=sort_order)
    rank = rank_of_entry(rankings, entry)

    comment = format_comment(
        pr=pr,
        run_url=dl.run.html_url or "(no url)",
        artifact_name=str(artifact_cfg["artifact_name"]),
        public_payload=public_payload,
        benchmark_payload=benchmark_payload,
        entry=entry,
        rankings=rankings,
        rank=rank,
        top_n=int(lb_cfg.get("top_n_in_comment", 10)),
    )
    upsert_pr_comment(gh, pr.number, comment)

    processed.add(key)
    save_processed(_abs_from_repo_root(repo_root, state_cfg["processed_commits_path"]), processed)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="bot/config.yaml", help="Path to config.yaml")
    args = ap.parse_args()

    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    cfg = load_yaml(_abs_from_repo_root(repo_root, args.config))

    gh = GitHubClient.from_config(cfg["github"])
    processed_path = _abs_from_repo_root(repo_root, cfg["state"]["processed_commits_path"])
    processed = load_processed(processed_path)

    polling = cfg.get("polling") or {}
    loop_forever = bool(polling.get("loop_forever", True))
    interval_s = float(polling.get("interval_seconds", 60))

    while True:
        try:
            prs = gh.list_open_prs(per_page=50)
            changed = 0
            for pr in prs:
                try:
                    if process_one_pr(gh=gh, repo_root=repo_root, pr=pr, cfg=cfg, processed=processed):
                        changed += 1
                except (ArtifactError, DockerRunnerError) as e:
                    # Keep running other PRs; we will retry next poll.
                    msg = textwrap.shorten(str(e), width=600, placeholder=" ...")
                    upsert_pr_comment(
                        gh,
                        pr.number,
                        "\n".join(
                            [
                                BOT_MARKER,
                                "## Автопроверка (бот)",
                                "",
                                f"Не удалось обработать PR #{pr.number} (`{pr.head_sha[:12]}`):",
                                "",
                                f"```\n{msg}\n```",
                                "",
                                "_Бот попробует снова позже (после следующего успешного CI run-а / исправления)._",
                            ]
                        )
                        + "\n",
                    )
            if not loop_forever:
                return 0
        except Exception:
            if not loop_forever:
                raise
        time.sleep(interval_s)


if __name__ == "__main__":
    raise SystemExit(main())

