"""The only part that talks to Gmail.

Everything here is plain stdlib IMAP. The parsing helpers are separate
functions so they can be tested without a live account.
"""

from __future__ import annotations

import imaplib
import re
import socket
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Callable, Iterable, Iterator, List, Optional, Sequence, Tuple

from .config import Account
from .models import MessageInfo, Quota

MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

DEFAULT_ALL_MAIL = "[Gmail]/All Mail"
DEFAULT_TRASH = "[Gmail]/Trash"

_MESSAGE_ID_RE = re.compile(r"<[^>]+>")


class GmailError(Exception):
    """A problem we can explain to a person."""


def imap_date(moment: datetime) -> str:
    """IMAP wants 01-Jan-2024, in English, whatever the local locale is."""
    return f"{moment.day:02d}-{MONTHS[moment.month - 1]}-{moment.year}"


def imap_quote(name: str) -> str:
    """Quote a mailbox name so names with spaces survive."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unquote_mailbox(name: str) -> str:
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    return name.replace('\\"', '"').replace("\\\\", "\\")


def parse_list_line(line: str) -> Tuple[List[str], Optional[str], str]:
    """Split a LIST response into (flags, delimiter, mailbox name)."""
    flags: List[str] = []
    flag_match = re.match(r"\(([^)]*)\)", line)
    if flag_match:
        flags = flag_match.group(1).split()

    rest = line[flag_match.end():].strip() if flag_match else line
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', rest)
    if len(quoted) >= 2:
        delimiter: Optional[str] = quoted[0]
        name = quoted[-1]
    else:
        parts = rest.split()
        delimiter = None
        name = parts[-1] if parts else ""
    return flags, delimiter, unquote_mailbox(name)


def parse_quota_response(text: str) -> Optional[Tuple[int, int]]:
    """Read '(STORAGE used limit)' out of a QUOTA line, in bytes.

    Gmail reports the numbers in kilobytes.
    """
    match = re.search(r"\(STORAGE\s+(\d+)\s+(\d+)\)", text)
    if not match:
        return None
    used_units, limit_units = int(match.group(1)), int(match.group(2))
    if limit_units <= 0:
        return None
    return used_units * 1024, limit_units * 1024


def parse_fetch_head(head: str) -> Tuple[Optional[str], Optional[int], Optional[datetime]]:
    """Pull UID, size and date out of the text before an IMAP literal."""
    uid_match = re.search(r"\bUID (\d+)", head)
    size_match = re.search(r"RFC822\.SIZE (\d+)", head)
    date_match = re.search(r'INTERNALDATE "([^"]+)"', head)

    uid = uid_match.group(1) if uid_match else None
    size = int(size_match.group(1)) if size_match else None
    when = None
    if date_match:
        try:
            when = datetime.strptime(
                date_match.group(1).strip(), "%d-%b-%Y %H:%M:%S %z"
            )
        except ValueError:
            when = None
    return uid, size, when


def iter_fetch_literals(data: Sequence[object]) -> Iterator[Tuple[str, bytes]]:
    """Walk imaplib's FETCH reply and yield (prefix, literal) pairs."""
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2:
            head, body = item
            if head is None:
                continue
            if isinstance(head, bytes):
                head = head.decode("utf-8", "replace")
            yield head, body if isinstance(body, bytes) else b""
        elif isinstance(item, bytes) and b"RFC822.SIZE" in item:
            # Some server replies arrive as one bare line.
            yield item.decode("utf-8", "replace"), b""


def decode_subject(raw: Optional[object]) -> Optional[str]:
    """Subjects arrive either raw (encoded words) or already decoded."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = str(raw)
    if not text:
        return None
    try:
        return str(make_header(decode_header(text)))
    except Exception:
        return text


def header_values(header_block: bytes, name: str) -> Optional[str]:
    if not header_block:
        return None
    try:
        message = BytesParser(policy=default_policy).parsebytes(header_block)
    except Exception:
        return None
    value = message.get(name)
    return str(value) if value is not None else None


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class GmailSession:
    """A thin, talkative wrapper around imaplib."""

    def __init__(
        self,
        account: Account,
        timeout: int = 120,
        all_mail: Optional[str] = None,
        trash: Optional[str] = None,
    ) -> None:
        self.account = account
        self.timeout = timeout
        self.all_mail = all_mail or DEFAULT_ALL_MAIL
        self.trash = trash or DEFAULT_TRASH
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    # -- connection ---------------------------------------------------

    def __enter__(self) -> "GmailSession":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._conn = imaplib.IMAP4_SSL(
                self.account.host, self.account.port, timeout=self.timeout
            )
        except (OSError, socket.timeout) as exc:
            raise GmailError(
                f"Could not reach {self.account.host} on port {self.account.port}. "
                "Check your internet connection. ({exc})"
            ) from exc
        try:
            password = self.account.password
            if "gmail" in self.account.host.lower():
                # Google shows app passwords as four groups of four; the
                # spaces are decoration and are ignored on login.
                password = password.replace(" ", "")
            self._conn.login(self.account.user, password)
        except imaplib.IMAP4.error as exc:
            raise GmailError(self._explain_login_error(exc)) from exc
        self._discover_folders()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except Exception:
            pass
        self._conn = None

    @staticmethod
    def _explain_login_error(exc: Exception) -> str:
        raw = exc.args[0] if exc.args else exc
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        lowered = text.lower()
        if "application-specific password" in lowered or "app password" in lowered:
            return (
                "Gmail needs an app password for this connection. Turn on 2-Step "
                "Verification, then create one under Google Account > Security > "
                "App passwords, and use that instead of your normal password."
            )
        if "invalid credentials" in lowered or "authentication" in lowered:
            return (
                "Gmail rejected the login. Check the address, and make sure you are "
                "using a 16-character app password (not your account password)."
            )
        return f"Gmail login failed: {text}"

    def _require(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise GmailError("Not connected to Gmail.")
        return self._conn

    def _discover_folders(self) -> None:
        """Find Gmail's real All Mail and Trash names (they are localised)."""
        conn = self._require()
        try:
            status, data = conn.list()
        except imaplib.IMAP4.error:
            return
        if status != "OK":
            return
        for item in data or []:
            line = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            flags, _delimiter, name = parse_list_line(line)
            flags_lower = {flag.lower() for flag in flags}
            if "\\all" in flags_lower:
                self.all_mail = name
            elif "\\trash" in flags_lower:
                self.trash = name

    # -- storage usage ------------------------------------------------

    def quota(self) -> Optional[Quota]:
        """Ask Gmail how full the account is. None if it will not say."""
        conn = self._require()
        try:
            conn._simple_command("GETQUOTAROOT", imap_quote(self.all_mail))
        except Exception:
            return None
        entries = list(conn.untagged_responses.get("QUOTA") or [])
        if not entries:
            try:
                _status, data = conn._untagged_response("OK", "QUOTA")
                entries = [item for item in (data or []) if item]
            except Exception:
                entries = []
        for entry in entries:
            text = entry.decode("utf-8", "replace") if isinstance(entry, bytes) else str(entry)
            parsed = parse_quota_response(text)
            if parsed:
                used, limit = parsed
                return Quota(used=used, limit=limit, source="server")
        return None

    def estimate_usage(self, limit_bytes: int, batch: int = 500) -> Optional[Quota]:
        """Fallback: add up every message size. Slow on big mailboxes.

        Gmail does report a real quota; this is only for the day it stops.
        """
        conn = self._require()
        status, _ = conn.select(imap_quote(self.all_mail), readonly=True)
        if status != "OK":
            return None
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK":
            return None
        uids = [uid.decode() for uid in (data[0] or b"").split()]
        total = 0
        for chunk in _chunks(uids, batch):
            status, reply = conn.uid("FETCH", ",".join(chunk), "(RFC822.SIZE)")
            if status != "OK":
                continue
            for head, _body in iter_fetch_literals(reply or []):
                match = re.search(r"RFC822\.SIZE (\d+)", head)
                if match:
                    total += int(match.group(1))
        return Quota(used=total, limit=limit_bytes, source="estimate") if total else None

    # -- reading ------------------------------------------------------

    def scan(
        self,
        older_than: datetime,
        batch: int = 200,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[MessageInfo]:
        """List messages older than a date, with size and subject."""
        conn = self._require()
        status, _ = conn.select(imap_quote(self.all_mail), readonly=True)
        if status != "OK":
            raise GmailError(f"Could not open {self.all_mail}.")

        status, data = conn.uid("SEARCH", None, "BEFORE", imap_date(older_than))
        if status != "OK":
            raise GmailError("Gmail refused the date search.")
        uids = [uid.decode() for uid in (data[0] or b"").split()]
        if not uids:
            return []

        found: List[MessageInfo] = []
        for index, chunk in enumerate(_chunks(uids, batch), start=1):
            status, reply = conn.uid(
                "FETCH",
                ",".join(chunk),
                "(RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])",
            )
            if status != "OK":
                continue
            for head, body in iter_fetch_literals(reply or []):
                uid, size, when = parse_fetch_head(head)
                if not uid or size is None or when is None:
                    continue
                found.append(
                    MessageInfo(
                        uid=uid,
                        size=size,
                        internal_date=when,
                        message_id=header_values(body, "Message-ID"),
                        subject=decode_subject(header_values(body, "Subject")),
                    )
                )
            if progress:
                progress(len(found), len(uids))
        return found

    def fetch_raw(self, uid: str) -> bytes:
        """Download one whole message, exactly as stored."""
        conn = self._require()
        status, reply = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise GmailError(f"Could not download message {uid}.")
        for head, body in iter_fetch_literals(reply or []):
            if "BODY[" in head:
                return body
        raise GmailError(f"Gmail returned no content for message {uid}.")

    # -- writing ------------------------------------------------------

    def delete_messages(self, uids: Sequence[str], batch: int = 100) -> int:
        """Remove messages from All Mail. Gmail then puts them in Trash."""
        if not uids:
            return 0
        conn = self._require()
        status, _ = conn.select(imap_quote(self.all_mail), readonly=False)
        if status != "OK":
            raise GmailError(f"Could not open {self.all_mail} for writing.")

        stored: List[str] = []
        for chunk in _chunks(list(uids), batch):
            status, _ = conn.uid(
                "STORE", ",".join(chunk), "+FLAGS.SILENT", "(\\Deleted)"
            )
            if status != "OK":
                raise GmailError("Gmail refused to mark messages as deleted.")
            stored.extend(chunk)
        self._expunge(stored)
        return len(stored)

    def _expunge(self, uids: Sequence[str]) -> None:
        conn = self._require()
        capabilities = {
            cap.decode("utf-8", "replace") if isinstance(cap, bytes) else str(cap)
            for cap in conn.capabilities
        }
        if "UIDPLUS" in capabilities:
            for chunk in _chunks(list(uids), 200):
                conn.uid("EXPUNGE", ",".join(chunk))
        else:
            conn.expunge()

    def purge_from_trash(self, message_ids: Iterable[Optional[str]]) -> int:
        """Permanently delete archived messages that Gmail moved to Trash.

        Trash still counts against the quota, so this is what actually
        turns a delete into free space.
        """
        wanted = [mid for mid in message_ids if mid]
        if not wanted:
            return 0
        conn = self._require()
        status, _ = conn.select(imap_quote(self.trash), readonly=False)
        if status != "OK":
            raise GmailError(f"Could not open {self.trash}.")

        purged = 0
        for message_id in wanted:
            status, data = conn.uid(
                "SEARCH", None, "HEADER", "Message-ID", imap_quote(message_id)
            )
            if status != "OK":
                continue
            uids = [uid.decode() for uid in (data[0] or b"").split()]
            if not uids:
                continue
            status, _ = conn.uid(
                "STORE", ",".join(uids), "+FLAGS.SILENT", "(\\Deleted)"
            )
            if status == "OK":
                self._expunge(uids)
                purged += len(uids)
        return purged


def cutoff_date(days: int, now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=days)
