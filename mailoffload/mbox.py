"""Pack the .eml archive into .mbox files Mail.app can import."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

_MBOX_FROM_ESCAPE = re.compile(rb"(?m)^From ")


def mbox_escape(body: bytes) -> bytes:
    """mboxrd: a body line starting with 'From ' gets one '>' in front."""
    return _MBOX_FROM_ESCAPE.sub(b">From ", body)


def _year_of(path: Path) -> str:
    name = path.name
    if len(name) >= 4 and name[:4].isdigit():
        return name[:4]
    return time.strftime("%Y", time.localtime(path.stat().st_mtime))


def _stamp_of(path: Path) -> str:
    """Use the message's own date (in the filename) for the mbox separator."""
    try:
        day = datetime.strptime(path.name[:10], "%Y-%m-%d")
        return time.asctime(day.timetuple())
    except ValueError:
        return time.asctime(time.localtime(path.stat().st_mtime))


def build_mboxes(source: Path, destination: Path) -> List[Path]:
    """One .mbox per year. Returns the files written."""
    source = Path(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    grouped: Dict[str, List[Path]] = {}
    for eml in sorted(source.rglob("*.eml")):
        grouped.setdefault(_year_of(eml), []).append(eml)

    written: List[Path] = []
    for year, files in sorted(grouped.items()):
        target = destination / f"MailArchive-{year}.mbox"
        with target.open("wb") as out:
            for eml in files:
                data = eml.read_bytes()
                out.write(f"From MAILER-DAEMON {_stamp_of(eml)}\n".encode("ascii"))
                out.write(mbox_escape(data))
                if not data.endswith(b"\n"):
                    out.write(b"\n")
                out.write(b"\n")
        written.append(target)
    return written
