"""Read one-time verification codes out of the applicant's mailbox over IMAP.

Greenhouse (and increasingly others) will not accept a submission until a code mailed to the candidate is
typed back into the form. Without mailbox access that turns every application into a manual step, so the
runner reads the code itself when an app password is configured and only asks the user when it is not.

Contract:
    is_configured() -> (bool, str)                 # str explains what is missing
    fetch_code(since, length=8, timeout_s=..., ) -> str | None

Gmail needs an App Password (a normal account password will not work with IMAP while 2FA is on):
    https://myaccount.google.com/apppasswords  ->  paste into Settings -> Secrets -> JOBBOT_MAIL_PASSWORD
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Iterable

from jobbot import config

log = logging.getLogger(__name__)

MAIL_PASSWORD_SECRET = "JOBBOT_MAIL_PASSWORD"
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

POLL_S = 6              # the mail usually lands within a few seconds; check often enough to feel instant
DEFAULT_TIMEOUT_S = 150
_MAX_MESSAGES = 25      # newest-first scan depth; a busy inbox should not turn into a full sync

# Words that mark the mail we want. Kept broad: every ATS words this differently.
_SUBJECT_HINTS = ("verification", "verify", "confirm", "security code", "one-time", "one time", "code",
                  "application")
_SENDER_HINTS = ("greenhouse", "lever", "ashby", "workday", "no-reply", "noreply", "donotreply")


def mail_user() -> str:
    """The mailbox to read. Explicit setting wins; otherwise the address the applications are sent with."""
    user = (config.get_setting(config.SETTING_MAIL_USER) or "").strip()
    if user:
        return user
    facts = config.load_facts() or {}
    return str(((facts.get("identity") or {}).get("email") or "")).strip()


def mail_password() -> str:
    """The app password with spaces removed — Google shows it in four groups and it gets pasted that way."""
    return (config.get_secret(MAIL_PASSWORD_SECRET) or "").replace(" ", "").strip()


def is_configured() -> tuple[bool, str]:
    user = mail_user()
    if not user:
        return False, "no mailbox address (set identity.email in facts.yaml)"
    if not mail_password():
        return False, f"no app password (Settings -> Secrets -> {MAIL_PASSWORD_SECRET})"
    return True, f"reading {user}"


def check_connection(timeout_s: int = 20) -> tuple[bool, str]:
    """Log in for real, so a wrong app password shows up here instead of as every Greenhouse application
    quietly stopping to ask for a code.

    The usual failure is not a typo: an app password only works for the Google account that created it, and
    the codes arrive at whatever address the applications were sent with. They have to be the same account.
    """
    ok, why = is_configured()
    if not ok:
        return False, why
    user = mail_user()
    try:
        imap = imaplib.IMAP4_SSL(config.get_setting(config.SETTING_IMAP_HOST) or DEFAULT_IMAP_HOST,
                                 DEFAULT_IMAP_PORT, timeout=timeout_s)
    except Exception as e:
        return False, f"could not reach the mail server: {e}"
    try:
        imap.login(user, mail_password())
        imap.select("INBOX")
        return True, f"connected to {user}"
    except imaplib.IMAP4.error as e:
        detail = str(e)
        if "AUTHENTICATIONFAILED" in detail.upper() or "Invalid credentials" in detail:
            return False, (f"{user} rejected the app password. An app password only works for the Google "
                           f"account that created it, so generate it while signed in as {user}.")
        return False, f"login failed: {detail[:160]}"
    except Exception as e:
        return False, f"login failed: {str(e)[:160]}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _decoded(value) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _body_text(msg: email.message.Message) -> str:
    """Flatten a message to text, HTML tags stripped. Codes are often only in the HTML part."""
    parts: list[str] = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True)
            if raw is None:
                continue
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                        .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
        parts.append(text)
    return re.sub(r"[ \t\r\f\v]+", " ", "\n".join(parts))


def extract_code(text: str, length: int = 8) -> str | None:
    """Pull a one-time code of `length` characters out of message text.

    Anchored on the words around it rather than on any single vendor's layout: the first pass looks for a
    token introduced by "code is"/"security code", and only then falls back to a bare token of the right
    shape. Without the anchor, a tracking id or a date in the footer wins.
    """
    if not text:
        return None
    # Case is preserved deliberately: Greenhouse mails codes like "yFENClG3", and an uppercased copy of that
    # is a different string. Matching stays case-insensitive; only the captured value is returned verbatim.
    token = rf"([A-Za-z0-9]{{{length}}})"
    anchored = [
        rf"(?:verification|security|confirmation|access|one[- ]time)\s+code[^A-Za-z0-9]{{0,60}}{token}",
        rf"\bcode\s+(?:is|:)\s*[^A-Za-z0-9]{{0,10}}{token}",
        rf"{token}\s*(?:is\s+your|to\s+(?:confirm|verify|submit))",
        rf"\benter\s+(?:this\s+|the\s+)?(?:code\s+)?[^A-Za-z0-9]{{0,10}}{token}",
        rf"\bapplication:\s*{token}",           # "paste this code into ... your application: yFENClG3"
    ]
    for pat in anchored:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    # Fallback: a standalone token of the right length that mixes letters and digits — prose does not.
    for m in re.finditer(rf"\b{token}\b", text):
        cand = m.group(1)
        if re.search(r"\d", cand) and re.search(r"[A-Za-z]", cand):
            return cand
    for m in re.finditer(rf"\b{token}\b", text):
        if m.group(1).isdigit():
            return m.group(1)
    return None


def _search_since(imap: imaplib.IMAP4_SSL, since: datetime) -> list[bytes]:
    # IMAP SINCE has day granularity, so it can only narrow the set; the per-message date check below is what
    # actually enforces "after we clicked submit".
    day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
    try:
        typ, data = imap.search(None, "SINCE", day)
    except Exception as e:
        log.warning("mail: IMAP search failed: %s", e)
        return []
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _looks_relevant(subject: str, sender: str, extra_hints: Iterable[str]) -> bool:
    hay = f"{subject} {sender}".lower()
    if any(h.lower() in hay for h in extra_hints if h):
        return True
    return any(h in hay for h in _SUBJECT_HINTS) or any(h in hay for h in _SENDER_HINTS)


# A link in a mail, and the ones worth following. Workday's new-account mail carries no code at all: it
# carries a "Verify Email" button, and until it is followed the account it just created cannot be signed
# into ("Verify your account before you sign in").
_LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
VERIFY_LINK_RE = re.compile(r"verif|activat|confirm", re.I)


def extract_link(text: str, match: re.Pattern = VERIFY_LINK_RE) -> str | None:
    """The first link in `text` whose URL looks like the one the mail is asking you to follow."""
    for url in _LINK_RE.findall(text or ""):
        url = url.rstrip(".,);\"'")
        if match.search(url):
            return url
    return None


def _scan_messages(since: datetime, hints: Iterable[str], pick):
    """Walk the newest relevant mail since `since`, handing each one's text to `pick`. First hit wins.

    Shared by the code and link scans: what differs between them is only what they pull out of the message.
    """
    user, password = mail_user(), mail_password()
    host = config.get_setting(config.SETTING_IMAP_HOST) or DEFAULT_IMAP_HOST
    imap = imaplib.IMAP4_SSL(host, DEFAULT_IMAP_PORT)
    try:
        imap.login(user, password)
        imap.select("INBOX")
        ids = _search_since(imap, since)
        for num in reversed(ids[-_MAX_MESSAGES:]):
            typ, data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            try:
                sent = email.utils.parsedate_to_datetime(msg.get("Date"))
                if sent is not None:
                    if sent.tzinfo is None:
                        sent = sent.replace(tzinfo=timezone.utc)
                    if sent < since:
                        continue        # older than this submit: its code is already spent
            except Exception:
                pass
            subject, sender = _decoded(msg.get("Subject")), _decoded(msg.get("From"))
            if not _looks_relevant(subject, sender, hints):
                continue
            found = pick(f"{subject}\n{_body_text(msg)}")
            if found:
                log.info("mail: %r from %s answered the wait", subject, sender)
                return found
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return None


def fetch_link(since: datetime, match: re.Pattern = VERIFY_LINK_RE, timeout_s: int = DEFAULT_TIMEOUT_S,
               hints: Iterable[str] = ()) -> str | None:
    """Poll the mailbox for a verification link sent after `since`. None if it never arrives or mail is off.

    Never raises, for the same reason fetch_code does not: a mailbox problem must degrade into asking the
    user to click the link themselves, not into a failed application.
    """
    ok, why = is_configured()
    if not ok:
        log.info("mail: not configured (%s)", why)
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            link = _scan_messages(since, hints, lambda text: extract_link(text, match))
            if link:
                return link
        except imaplib.IMAP4.error as e:
            log.warning("mail: IMAP login/command rejected (%s) — check %s", e, MAIL_PASSWORD_SECRET)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("mail: link scan failed: %s", e)
        time.sleep(POLL_S)
    log.info("mail: no verification link arrived within %ds", timeout_s)
    return None


def _scan_once(since: datetime, length: int, hints: Iterable[str]) -> str | None:
    user, password = mail_user(), mail_password()
    host = config.get_setting(config.SETTING_IMAP_HOST) or DEFAULT_IMAP_HOST
    imap = imaplib.IMAP4_SSL(host, DEFAULT_IMAP_PORT)
    try:
        imap.login(user, password)
        imap.select("INBOX")
        ids = _search_since(imap, since)
        for num in reversed(ids[-_MAX_MESSAGES:]):
            typ, data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            try:
                sent = email.utils.parsedate_to_datetime(msg.get("Date"))
                if sent is not None:
                    if sent.tzinfo is None:
                        sent = sent.replace(tzinfo=timezone.utc)
                    if sent < since:
                        continue        # older than this submit: its code is already spent
            except Exception:
                pass
            subject, sender = _decoded(msg.get("Subject")), _decoded(msg.get("From"))
            if not _looks_relevant(subject, sender, hints):
                continue
            code = extract_code(f"{subject}\n{_body_text(msg)}", length)
            if code:
                log.info("mail: found a %d-character code in %r from %s", length, subject, sender)
                return code
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return None


def fetch_code(since: datetime, length: int = 8, timeout_s: int = DEFAULT_TIMEOUT_S,
               hints: Iterable[str] = ()) -> str | None:
    """Poll the mailbox for a one-time code sent after `since`. None if it never arrives or mail is off.

    Never raises: a mailbox problem must degrade to asking the user, not fail the application.
    """
    ok, why = is_configured()
    if not ok:
        log.info("mail: not configured (%s)", why)
        return None
    deadline = time.time() + timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            code = _scan_once(since, length, hints)
            if code:
                return code
        except imaplib.IMAP4.error as e:
            # Bad credentials will not fix themselves; stop rather than hammering the server for two minutes.
            log.warning("mail: IMAP login/command rejected (%s) — check %s", e, MAIL_PASSWORD_SECRET)
            return None
        except Exception as e:
            log.warning("mail: scan %d failed: %s", attempt, e)
        time.sleep(POLL_S)
    log.info("mail: no code arrived within %ds", timeout_s)
    return None
