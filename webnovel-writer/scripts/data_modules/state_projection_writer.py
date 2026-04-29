#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .story_contracts import read_json_if_exists, write_json

try:
    from chapter_paths import find_chapter_file
except ImportError:  # pragma: no cover
    from scripts.chapter_paths import find_chapter_file


class StateProjectionWriter:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def apply(self, commit_payload: dict) -> dict:
        chapter = int(commit_payload.get("meta", {}).get("chapter") or 0)
        status = commit_payload["meta"]["status"]

        if status == "rejected":
            if chapter > 0:
                state_path = self.project_root / ".webnovel" / "state.json"
                state = read_json_if_exists(state_path) or {}
                progress = state.setdefault("progress", {})
                chapter_status = progress.setdefault("chapter_status", {})
                chapter_status[str(chapter)] = "chapter_rejected"
                write_json(state_path, state)
            return {"applied": True, "writer": "state", "reason": "commit_rejected_status_updated"}

        if status != "accepted":
            return {"applied": False, "writer": "state", "reason": f"unknown_status:{status}"}

        state_path = self.project_root / ".webnovel" / "state.json"
        state = read_json_if_exists(state_path) or {}
        entity_state = state.setdefault("entity_state", {})
        progress = state.setdefault("progress", {})
        chapter_status = progress.setdefault("chapter_status", {})

        applied_count = 0
        for delta in self._collect_state_deltas(commit_payload):
            entity_id = str(delta.get("entity_id") or "").strip()
            field = str(delta.get("field") or "").strip()
            if not entity_id or not field:
                continue
            entity_state.setdefault(entity_id, {})[field] = delta.get("new")
            applied_count += 1

        if chapter > 0:
            old_current = self._safe_int(progress.get("current_chapter"))
            old_total = self._safe_int(progress.get("total_words"))
            old_status = chapter_status.get(str(chapter))

            chapter_status[str(chapter)] = "chapter_committed"
            progress["current_chapter"] = max(old_current, chapter)

            projected_total = self._project_total_words(chapter_status)
            if projected_total > 0:
                progress["total_words"] = projected_total
            else:
                progress["total_words"] = old_total

            if (
                old_status != "chapter_committed"
                or progress.get("current_chapter") != old_current
                or progress.get("total_words") != old_total
            ):
                progress["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        write_json(state_path, state)
        return {
            "applied": applied_count > 0 or chapter > 0,
            "writer": "state",
            "applied_count": applied_count,
        }

    def _collect_state_deltas(self, commit_payload: dict) -> list[dict]:
        deltas = [dict(delta) for delta in (commit_payload.get("state_deltas") or []) if isinstance(delta, dict)]
        seen = {
            (
                str(delta.get("entity_id") or "").strip(),
                str(delta.get("field") or "").strip(),
            )
            for delta in deltas
        }

        for event in commit_payload.get("accepted_events") or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "").strip()
            payload = dict(event.get("payload") or {})
            entity_id = str(payload.get("entity_id") or event.get("subject") or "").strip()
            if not entity_id:
                continue

            field = ""
            if event_type == "power_breakthrough":
                field = str(payload.get("field") or "realm").strip()
            elif event_type == "character_state_changed":
                field = str(payload.get("field") or "").strip()
            else:
                continue

            key = (entity_id, field)
            if not field or key in seen:
                continue

            seen.add(key)
            deltas.append(
                {
                    "entity_id": entity_id,
                    "field": field,
                    "old": payload.get("old") if "old" in payload else payload.get("from"),
                    "new": payload.get("new") if "new" in payload else payload.get("to"),
                }
            )
        return deltas

    def _project_total_words(self, chapter_status: dict) -> int:
        total = 0
        for raw_chapter, raw_status in chapter_status.items():
            if raw_status != "chapter_committed":
                continue
            chapter = self._safe_int(raw_chapter)
            if chapter <= 0:
                continue
            chapter_file = find_chapter_file(self.project_root, chapter)
            if chapter_file is None:
                continue
            try:
                total += self._count_chapter_words(chapter_file.read_text(encoding="utf-8"))
            except OSError:
                continue
        return total

    def _count_chapter_words(self, content: str) -> int:
        text = re.sub(r"```[\s\S]*?```", "", content)
        text = re.sub(r"^#+ .*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"---", "", text)
        return len(text.strip())

    def _safe_int(self, value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
