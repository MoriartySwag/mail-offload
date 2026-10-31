"""The part that ties it together: measure, copy, check, delete."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .archive import Archive, Archived
from .config import Config, human_bytes
from .gmail import GmailError, GmailSession, cutoff_date
from .models import MessageInfo, Quota
from .select import bytes_to_free, choose, needs_run

GMAIL_FREE_BYTES = 15 * 1024 ** 3


@dataclass
class Report:
    status: str
    dry_run: bool
    usage_before: Optional[Quota] = None
    usage_after: Optional[Quota] = None
    candidates: int = 0
    chosen: List[MessageInfo] = field(default_factory=list)
    archived: List[Archived] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    purged: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    run_id: str = ""
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished: Optional[datetime] = None

    @property
    def bytes_archived(self) -> int:
        return sum(item.size for item in self.archived)

    @property
    def bytes_freed(self) -> int:
        if not self.usage_before or not self.usage_after:
            return 0
        return max(0, self.usage_before.used - self.usage_after.used)

    def summary_lines(self) -> List[str]:
        lines = []
        if self.usage_before:
            lines.append(
                "Storage before: "
                f"{human_bytes(self.usage_before.used)} of "
                f"{human_bytes(self.usage_before.limit)} "
                f"({self.usage_before.percent:.1f}%)"
            )
        if self.usage_after:
            lines.append(
                "Storage after:  "
                f"{human_bytes(self.usage_after.used)} "
                f"({self.usage_after.percent:.1f}%)"
            )
        lines.append(f"Old enough to move: {self.candidates} messages")
        lines.append(f"Selected: {len(self.chosen)} messages "
                     f"({human_bytes(sum(m.size for m in self.chosen))})")
        lines.append(f"Archived and checked: {len(self.archived)} messages "
                     f"({human_bytes(self.bytes_archived)})")
        if self.dry_run:
            lines.append("Deleted from Gmail: 0 (rehearsal run)")
        else:
            lines.append(f"Deleted from Gmail: {len(self.deleted)} messages")
            lines.append(f"Purged from Trash: {self.purged} messages")
            lines.append(f"Space actually freed: {human_bytes(self.bytes_freed)}")
        return lines


def _log_run(config: Config, report: Report) -> None:
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "run": report.run_id,
            "status": report.status,
            "dry_run": report.dry_run,
            "started": report.started.isoformat(timespec="seconds"),
            "finished": report.finished.isoformat(timespec="seconds")
            if report.finished
            else None,
            "used_before": report.usage_before.used if report.usage_before else None,
            "used_after": report.usage_after.used if report.usage_after else None,
            "archived": len(report.archived),
            "deleted": len(report.deleted),
            "purged": report.purged,
            "errors": report.errors,
            "warnings": report.warnings,
        }
        with config.run_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_usage(
    session: GmailSession,
    assume_used: Optional[int] = None,
    assume_limit: int = GMAIL_FREE_BYTES,
) -> Quota:
    quota = session.quota()
    if quota is not None:
        return quota
    if assume_used is not None:
        return Quota(used=assume_used, limit=assume_limit, source="assumed")
    raise GmailError(
        "Gmail did not report storage usage. Pass --assume-used (for example "
        "--assume-used 13G) to continue without it."
    )


def run(
    config: Config,
    apply: bool = False,
    force: bool = False,
    max_messages: Optional[int] = None,
    assume_used: Optional[int] = None,
    plan_only: bool = False,
    session_factory: Optional[Callable[[], GmailSession]] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> Report:
    """Copy older mail to disk, optionally deleting it from Gmail.

    Without apply=True nothing is deleted anywhere. With plan_only=True
    nothing is even downloaded; you just get the list.
    """
    factory = session_factory or (lambda: GmailSession(config.account))
    report = Report(status="error", dry_run=not apply, run_id=uuid.uuid4().hex[:12])
    archive = Archive(config.archive_dir)

    with factory() as session:
        usage = read_usage(session, assume_used=assume_used)
        report.usage_before = usage
        if usage.source == "estimate":
            report.warnings.append(
                "Gmail did not report usage, so it was added up from message "
                "sizes. Treat the numbers as approximate."
            )

        if not force and not needs_run(usage, config.policy.trigger_percent):
            report.status = "below-threshold"
            report.finished = datetime.now(timezone.utc)
            _log_run(config, report)
            return report

        target = bytes_to_free(usage, config.policy.target_percent)
        if target <= 0:
            report.status = "nothing-to-do"
            report.finished = datetime.now(timezone.utc)
            _log_run(config, report)
            return report

        cutoff = cutoff_date(config.policy.min_age_days)

        def scan_progress(found: int, total: int) -> None:
            if on_progress:
                on_progress("scan", found, total)

        candidates = session.scan(cutoff, progress=scan_progress)
        report.candidates = len(candidates)

        limit = max_messages or config.policy.max_messages_per_run
        chosen = choose(
            candidates,
            target_bytes=target,
            max_messages=limit,
            max_bytes=config.policy.max_bytes_per_run,
            order=config.policy.order,
        )
        report.chosen = chosen

        if plan_only:
            report.status = "plan"
            report.finished = datetime.now(timezone.utc)
            _log_run(config, report)
            return report

        verified: List[Archived] = []
        for index, info in enumerate(chosen, start=1):
            if on_progress:
                on_progress("copy", index, len(chosen))
            try:
                raw = session.fetch_raw(info.uid)
            except GmailError as exc:
                report.errors.append(f"{info.uid}: {exc}")
                continue

            if len(raw) != info.size:
                report.warnings.append(
                    f"{info.uid}: Gmail said {info.size} bytes, downloaded "
                    f"{len(raw)}. Keeping the downloaded copy."
                )

            try:
                item = archive.write(raw, info)
            except OSError as exc:
                report.errors.append(f"{info.uid}: could not write archive ({exc})")
                continue

            if not archive.verify(item):
                report.errors.append(
                    f"{info.uid}: copy on disk does not match what was "
                    "downloaded, so it will not be deleted."
                )
                continue

            archive.append_manifest(item, report.run_id)
            verified.append(item)
            report.archived.append(item)

        report.status = "archived-only"
        if not apply:
            report.finished = datetime.now(timezone.utc)
            _log_run(config, report)
            return report

        # Re-check every file immediately before asking Gmail to delete it.
        safe = [item for item in verified if archive.verify(item)]
        if len(safe) != len(verified):
            report.errors.append(
                "Some copies changed on disk between writing and deleting; "
                "those messages were kept on Gmail."
            )

        if safe:
            try:
                session.delete_messages([item.uid for item in safe])
                report.deleted = [item.uid for item in safe]
            except GmailError as exc:
                report.errors.append(f"Could not delete from Gmail: {exc}")

        if report.deleted:
            try:
                report.purged = session.purge_from_trash(
                    item.message_id for item in safe
                )
            except GmailError as exc:
                report.warnings.append(f"Could not clean up Trash: {exc}")

        report.usage_after = session.quota()
        if report.deleted and report.usage_after and report.bytes_archived:
            if report.bytes_freed < report.bytes_archived * 0.1:
                report.warnings.append(
                    "Gmail reports almost no change in storage. The messages "
                    "may still be in All Mail or Trash; check the web interface."
                )
        report.status = "ok" if not report.errors else "partial"
        report.finished = datetime.now(timezone.utc)
        _log_run(config, report)
        return report


@dataclass
class ArchiveCheck:
    files: int = 0
    missing: List[str] = field(default_factory=list)
    damaged: List[str] = field(default_factory=list)
    bytes_total: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing and not self.damaged


def verify_archive(config: Config) -> ArchiveCheck:
    """Re-check every file the manifest remembers."""
    import hashlib

    result = ArchiveCheck()
    manifest = config.manifest_path
    if not manifest.is_file():
        return result
    root = config.archive_dir
    with manifest.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            path = root / entry.get("path", "")
            result.files += 1
            if not path.is_file():
                result.missing.append(str(path))
                continue
            data = path.read_bytes()
            result.bytes_total += len(data)
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                result.damaged.append(str(path))
            elif len(data) != entry.get("size"):
                result.damaged.append(str(path))
    return result
