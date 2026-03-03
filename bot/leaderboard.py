from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Literal, Optional


SortOrder = Literal["asc", "desc"]


@dataclasses.dataclass(frozen=True)
class LeaderboardEntry:
    participant: str
    base_ref: str
    attempt: int
    pr_number: int
    commit_sha: str
    public_passed: bool
    benchmark_ran: bool
    score: Optional[float]
    score_unit: Optional[str]
    meta: dict[str, Any]
    timestamp_utc: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_leaderboard(path: str) -> list[LeaderboardEntry]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    entries: list[LeaderboardEntry] = []
    for e in raw.get("entries", []):
        entries.append(
            LeaderboardEntry(
                participant=str(e["participant"]),
                base_ref=str(e["base_ref"]),
                attempt=int(e["attempt"]),
                pr_number=int(e["pr_number"]),
                commit_sha=str(e["commit_sha"]),
                public_passed=bool(e.get("public_passed", False)),
                benchmark_ran=bool(e.get("benchmark_ran", False)),
                score=(float(e["score"]) if e.get("score") is not None else None),
                score_unit=(str(e["score_unit"]) if e.get("score_unit") is not None else None),
                meta=dict(e.get("meta") or {}),
                timestamp_utc=str(e.get("timestamp_utc") or e.get("timestamp") or ""),
            )
        )
    return entries


def save_leaderboard(path: str, entries: list[LeaderboardEntry]) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": [
            {
                "participant": e.participant,
                "base_ref": e.base_ref,
                "attempt": e.attempt,
                "pr_number": e.pr_number,
                "commit_sha": e.commit_sha,
                "public_passed": e.public_passed,
                "benchmark_ran": e.benchmark_ran,
                "score": e.score,
                "score_unit": e.score_unit,
                "meta": e.meta,
                "timestamp_utc": e.timestamp_utc,
            }
            for e in entries
        ]
    }
    tmp = str(p) + ".tmp"
    pathlib.Path(tmp).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def register_result(
    entries: list[LeaderboardEntry],
    *,
    participant: str,
    base_ref: str,
    pr_number: int,
    commit_sha: str,
    public_passed: bool,
    benchmark_payload: dict[str, Any],
    meta: dict[str, Any],
) -> LeaderboardEntry:
    attempt = 1 + sum(1 for e in entries if e.participant == participant and e.base_ref == base_ref)
    benchmark_ran = bool(benchmark_payload.get("ran", False))
    score = benchmark_payload.get("score")
    score_unit = benchmark_payload.get("score_unit")
    entry = LeaderboardEntry(
        participant=participant,
        base_ref=base_ref,
        attempt=attempt,
        pr_number=pr_number,
        commit_sha=commit_sha,
        public_passed=public_passed,
        benchmark_ran=benchmark_ran,
        score=(float(score) if score is not None else None),
        score_unit=(str(score_unit) if score_unit is not None else None),
        meta=meta,
        timestamp_utc=_utc_now_iso(),
    )
    entries.append(entry)
    return entry


def rankings_for_base(entries: list[LeaderboardEntry], base_ref: str, *, sort_order: SortOrder) -> list[LeaderboardEntry]:
    relevant = [e for e in entries if e.base_ref == base_ref and e.public_passed and e.benchmark_ran and e.score is not None]
    reverse = sort_order == "desc"
    return sorted(relevant, key=lambda e: float(e.score), reverse=reverse)


def rank_of_entry(rankings: list[LeaderboardEntry], entry: LeaderboardEntry) -> Optional[int]:
    for i, e in enumerate(rankings, start=1):
        if e.commit_sha == entry.commit_sha and e.participant == entry.participant and e.attempt == entry.attempt:
            return i
    return None

