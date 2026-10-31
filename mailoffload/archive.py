"""Write messages to disk, then prove the copy is good."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import MessageInfo


@dataclass(frozen=True)
class Archived:
    path: Path
    sha256: str
    size: int
    uid: str
    internal_date: datetime
    message_id: Optional[str]


def _fsync_dir(path: Path) -> None:
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


class Archive:
    """A plain folder of .eml files, one per message, plus a manifest."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def target_path(self, info: MessageInfo, sha256: str) -> Path:
        when = info.internal_date
        return (
            self.root
            / f"{when:%Y}"
            / f"{when:%m}"
            / f"{when:%Y-%m-%d}_{info.uid}_{sha256[:8]}.eml"
        )

    def write(self, raw: bytes, info: MessageInfo) -> Archived:
        sha256 = hashlib.sha256(raw).hexdigest()
        destination = self.target_path(info, sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() == sha256:
                return Archived(
                    path=destination,
                    sha256=sha256,
                    size=len(raw),
                    uid=info.uid,
                    internal_date=info.internal_date,
                    message_id=info.message_id,
                )
            # Same name, different content: never overwrite an archive.
            destination = destination.with_name(
                destination.stem + "-" + sha256[8:16] + ".eml"
            )

        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_dir(destination.parent)

        return Archived(
            path=destination,
            sha256=sha256,
            size=len(raw),
            uid=info.uid,
            internal_date=info.internal_date,
            message_id=info.message_id,
        )

    def verify(self, item: Archived) -> bool:
        """Read the file back and compare it to what we fetched."""
        try:
            data = item.path.read_bytes()
        except OSError:
            return False
        return len(data) == item.size and hashlib.sha256(data).hexdigest() == item.sha256

    def append_manifest(self, item: Archived, run_id: str) -> None:
        entry = {
            "run": run_id,
            "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "uid": item.uid,
            "date": item.internal_date.isoformat(),
            "message_id": item.message_id,
            "sha256": item.sha256,
            "size": item.size,
            "path": str(item.path.relative_to(self.root)),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def iter_eml(self) -> List[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.rglob("*.eml"))
