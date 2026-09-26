"""JSONL 事件日志：追加写入与启动重放。

每行一个领域事件信封（与 ``contracts/domain.schema.json`` 同构）。
日志是服务状态的唯一事实来源：内存状态随时可由重放重建，
因此服务重启不会丢失已记录的锁定、确认与结算事实。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Mapping


class Journal:
    """追加式 JSONL 日志，按事件标识去重。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_ids: set[str] = set()
        for event in self.read_all():
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                self._seen_ids.add(event_id)

    def read_all(self) -> Iterator[dict[str, Any]]:
        """按写入顺序产出全部事件；文件不存在时为空。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def contains(self, event_id: str) -> bool:
        return event_id in self._seen_ids

    def append(self, event: Mapping[str, Any]) -> None:
        """追加一个事件并立即落盘；相同事件标识不得重复追加。"""
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("事件必须携带非空 event_id")
        if event_id in self._seen_ids:
            raise ValueError(f"事件标识重复: {event_id}")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
        self._seen_ids.add(event_id)
