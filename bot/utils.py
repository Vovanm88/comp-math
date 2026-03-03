from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import yaml


def load_yaml(path: str) -> dict[str, Any]:
    return yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}


def load_json(path: str, default: Any) -> Any:
    p = pathlib.Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def atomic_write_json(path: str, payload: Any) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    pathlib.Path(tmp).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)

