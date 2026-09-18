"""持久化层：追加式事件日志、月度快照文件与幂等键存储。

所有文件都写入运行目录（默认 ``.runtime/``）：

- ``events.jsonl``      —— 追加式事件日志，每行一个 JSON 事件，是系统唯一事实来源；
- ``snapshots/*.json``  —— 已对外报出的月度快照，发布后不可改写；
- ``idempotency.json``  —— 幂等键到响应的映射，用于请求级重放。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


class EventStore:
    """追加式 JSONL 事件日志。"""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.dir = Path(runtime_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def load(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


class SnapshotStore:
    """已发布月度快照的只增文件存储。"""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.dir = Path(runtime_dir) / "snapshots"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, snapshot_id: str) -> Path:
        return self.dir / f"{snapshot_id}.json"

    def save(self, doc: dict[str, Any]) -> None:
        path = self._path(doc["snapshot_id"])
        if path.exists():
            raise FileExistsError(f"快照已存在，禁止改写: {doc['snapshot_id']}")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self._path(snapshot_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


class IdempotencyStore:
    """请求级幂等键存储（键 -> 已返回的响应）。"""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.path = Path(runtime_dir) / "idempotency.json"
        self._data: dict[str, Any] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, scope: str, key: str) -> dict[str, Any] | None:
        return self._data.get(scope, {}).get(key)

    def put(self, scope: str, key: str, status: int, body: dict[str, Any]) -> None:
        self._data.setdefault(scope, {})[key] = {"status": status, "body": body}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
