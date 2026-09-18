"""追加式事件存储：JSONL 日志，重启后按 seq 重放恢复状态。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[dict] = self._read_all()
        self._seq = self._events[-1]["seq"] if self._events else 0

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        events = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        events.sort(key=lambda e: e["seq"])
        return events

    def load_all(self) -> list[dict]:
        return list(self._events)

    def append(self, event: dict) -> dict:
        self._seq += 1
        event["seq"] = self._seq
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(event)
        return event
