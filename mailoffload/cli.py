"""Command line entry point. Plain text, no surprises."""

from __future__ import annotations

import argparse
import imaplib
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .archive import Archive
from .config import CONFIG_SEARCH, ConfigError, human_bytes, load_config, parse_size, write_template
from .gmail import GmailError, GmailSession
from .mbox import build_mboxes
from .runner import Report, read_usage, run, verify_archive


def _say(text: str = "") -> None:
    print(text, flush=True)


def _progress(kind: str, done: int, total: int) -> None:
    if total <= 0:
        return
    if done == total or done % 50 == 0 or done == 1:
        if kind == "scan":
            _say(f"  scanning: {done}/{total} messages read")
        else:
            _say(f"  copying:  {done}/{total}")


def _config(args: argparse.Namespace):
    return load_config(Path(args.config).expanduser() if args.config else None)


def _print_report(report: Report) -> None:
    for line in report.summary_lines():
        _say(line)
    if report.chosen:
        _say("")
        if report.status == "plan":
            _say("Would move these messages:")
        else:
            _say("Selected messages:")
        for info in report.chosen[:20]:
            _say(
                f"  {info.internal_date:%Y-%m-%d}  "
                f"{human_bytes(info.size):>9}  {info.short_subject}"
            )
        if len(report.chosen) > 20:
            _say(f"  ... and {len(report.chosen) - 20} more")
    if report.warnings:
        _say("")
        for warning in report.warnings:
            _say(f"warning: {warning}")
    if report.errors:
        _say("")
        for error in report.errors:
            _say(f"error: {error}", )


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else CONFIG_SEARCH[0]
    try:
        written = write_template(path, force=args.force)
    except ConfigError as exc:
        _say(f"error: {exc}")
        return 1
    _say(f"Wrote {written}")
    _say("")
    _say("Next steps:")
    _say("  1. Create a Google app password: Account > Security > App passwords")
    _say("  2. Put your address and that password in the file (it is chmod 600)")
    _say("  3. Run: mailoffload check")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = _config(args)
    with GmailSession(config.account) as session:
        quota = read_usage(
            session,
            assume_used=parse_size(args.assume_used) if args.assume_used else None,
        )
    policy = config.policy
    trigger = quota.limit * policy.trigger_percent / 100.0
    _say(f"Account:  {config.account.user}")
    _say(f"Storage:  {human_bytes(quota.used)} of {human_bytes(quota.limit)} "
         f"({quota.percent:.1f}%)")
    _say(f"Trigger:  {policy.trigger_percent:.0f}% = {human_bytes(trigger)}")
    if quota.percent >= policy.trigger_percent:
        _say("Status:   over the trigger. Run 'mailoffload plan' to see what moves.")
    else:
        remaining = trigger - quota.used
        _say(f"Status:   below the trigger, {human_bytes(remaining)} of headroom left.")

    archive = Archive(config.archive_dir)
    known = len(archive.iter_eml())
    _say(f"Archive:  {config.archive_dir} ({known} files on disk)")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    config = _config(args)
    report = run(
        config,
        apply=False,
        force=args.force,
        max_messages=args.limit,
        assume_used=parse_size(args.assume_used) if args.assume_used else None,
        plan_only=True,
        on_progress=_progress,
    )
    _say("")
    if report.status == "below-threshold":
        _say("Under the trigger threshold, so there is nothing to do.")
        _say("Add --force to plan a run anyway.")
    elif report.status == "nothing-to-do":
        _say("Already at or below the target, so there is nothing to move.")
    else:
        _print_report(report)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.account.password:
        _say("error: no app password configured. Run 'mailoffload init' and set it,")
        _say("       or export MAILOFFLOAD_APP_PASSWORD.")
        return 1
    report = run(
        config,
        apply=args.apply,
        force=args.force,
        max_messages=args.limit,
        assume_used=parse_size(args.assume_used) if args.assume_used else None,
        on_progress=_progress,
    )
    _say("")
    if report.status == "below-threshold":
        _say("Under the trigger threshold, so nothing was touched.")
        return 0
    if report.status == "nothing-to-do":
        _say("Already at or below the target, so nothing was moved.")
        return 0
    _print_report(report)
    if not args.apply and report.archived:
        _say("")
        _say("Nothing was deleted. This was a rehearsal: every copy was written")
        _say("and checked. Add --apply to remove those messages from Gmail.")
    return 0 if not report.errors else 1


def cmd_verify(args: argparse.Namespace) -> int:
    config = _config(args)
    result = verify_archive(config)
    _say(f"Checked {result.files} archived messages ({human_bytes(result.bytes_total)}).")
    if result.ok:
        _say("All files are present and match their recorded checksums.")
        return 0
    for path in result.missing:
        _say(f"missing: {path}")
    for path in result.damaged:
        _say(f"damaged: {path}")
    return 1


def cmd_pack(args: argparse.Namespace) -> int:
    config = _config(args)
    destination = Path(args.dest).expanduser() if args.dest else config.archive_dir / "mbox"
    files = build_mboxes(config.archive_dir, destination)
    if not files:
        _say("Nothing to pack yet.")
        return 0
    for path in files:
        _say(f"Wrote {path} ({human_bytes(path.stat().st_size)})")
    _say("")
    _say("In Mail.app: File > Import Mailboxes... > Files > choose these.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailoffload",
        description="Move older mail off Gmail's servers, keeping a checked copy on this Mac.",
    )
    parser.add_argument("--version", action="version", version=f"mailoffload {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--config", help="path to config.ini")
        item.add_argument("--assume-used", help="treat storage as this full, e.g. 13G")
        return item

    init = sub.add_parser("init", help="write a starter config file")
    init.add_argument("--path", help="where to write it")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_init)

    check = common("check", "show storage and threshold status")
    check.set_defaults(func=cmd_check)

    plan = common("plan", "list the messages a run would move")
    plan.add_argument("--limit", type=int, help="max messages this run")
    plan.add_argument("--force", action="store_true", help="ignore the trigger threshold")
    plan.set_defaults(func=cmd_plan)

    run_cmd = common("run", "copy older mail to disk (add --apply to delete from Gmail)")
    run_cmd.add_argument("--apply", action="store_true",
                         help="actually delete the archived messages from Gmail")
    run_cmd.add_argument("--limit", type=int, help="max messages this run")
    run_cmd.add_argument("--force", action="store_true", help="ignore the trigger threshold")
    run_cmd.set_defaults(func=cmd_run)

    verify = common("verify", "re-check every archived file")
    verify.set_defaults(func=cmd_verify)

    pack = common("pack", "build .mbox files for importing into Mail.app")
    pack.add_argument("--dest", help="where to put the .mbox files")
    pack.set_defaults(func=cmd_pack)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, GmailError) as exc:
        _say(f"error: {exc}")
        return 1
    except (imaplib.IMAP4.error, OSError) as exc:
        _say(f"error: lost the connection to Gmail ({exc}).")
        _say("Nothing was deleted that had not already been checked and saved.")
        return 1
    except KeyboardInterrupt:
        _say("")
        _say("Stopped. Any message deleted so far had already been verified on disk.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
