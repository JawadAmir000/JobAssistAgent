"""The one password jobbot uses for the accounts it creates on employers' ATS tenants.

Workday — and any career site that puts a signup in front of the form — will not show the application until
the candidate has an account with that employer, and every tenant is a separate account: fifty Workday
employers means fifty signups. Asking the user to invent and remember a password per employer is what turned
a run into a queue of pauses, so the password is generated once here, kept in the OS keychain, and reused for
every account. Reuse is the point, not a shortcut: an account jobbot created a week ago can only be signed
back into if the password is still known.

It never lives in facts.yaml. That file is read into LLM prompts, so a password written there would be sent
to the model and kept in the prompt log; config.migrate_secrets_from_facts() exists for exactly that reason.

Contract:
    account_password() -> str     # never empty, never asks; generates and stores on first use
    stored_password()  -> str     # what is stored now, '' if nothing yet; never generates
    location_hint()    -> str     # where to read it by hand, for the messages that tell the user to
"""
from __future__ import annotations

import logging
import os
import secrets
import string
import threading

from jobbot import config

log = logging.getLogger(__name__)

SECRET_NAME = "JOBBOT_ACCOUNT_PASSWORD"
# What the Workday adapter called this back when it was a password the user had to think up and type into
# Settings. Accounts created with it still exist at whatever employers it was used on, so it seeds the
# managed password rather than being replaced by a fresh one those accounts would reject.
LEGACY_SECRET_NAMES = ("JOBBOT_WORKDAY_PASSWORD",)
# A machine with no usable keychain (a headless box, a locked login keyring) must still be able to sign back
# into an account it created, so the password falls back to a file only this user can read.
FALLBACK_PATH = config.DATA_DIR / ".account_password"

LENGTH = 20
# The punctuation every ATS password box has accepted. No quotes, backslashes or spaces: those are what get
# mangled by a form that re-encodes the value before it stores it.
SYMBOLS = "!@#$%^*-_=+"

_lock = threading.Lock()


def meets_requirements(password: str) -> bool:
    """The union of the password rules these forms print above the box — Workday's list is the strictest we
    have seen (8+ characters, upper, lower, numeric, special) and this clears all of them at once."""
    return (len(password) >= 12
            and any(ch.islower() for ch in password)
            and any(ch.isupper() for ch in password)
            and any(ch.isdigit() for ch in password)
            and any(ch in SYMBOLS for ch in password))


def generate(length: int = LENGTH) -> str:
    """A random password that satisfies every rule at once by construction: one character from each class,
    the rest from all of them, then shuffled so the classes are not in a guessable order."""
    length = max(length, 12)
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits + SYMBOLS
    chars = [secrets.choice(string.ascii_lowercase), secrets.choice(string.ascii_uppercase),
             secrets.choice(string.digits), secrets.choice(SYMBOLS)]
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    password = "".join(chars)
    return password if meets_requirements(password) else generate(length)


def stored_password() -> str:
    """The managed password as it stands, '' before the first account was ever created. Never generates, so
    the Settings page can report honestly without creating a secret as a side effect of being looked at."""
    for name in (SECRET_NAME, *LEGACY_SECRET_NAMES):
        value = (config.get_secret(name) or "").strip()
        if value:
            return value
    try:
        return FALLBACK_PATH.read_text().strip()
    except Exception:
        return ""


def account_password() -> str:
    """The password for every account jobbot creates. Generated and stored the first time it is asked for.

    The lock matters: two applications starting together would otherwise generate two passwords, store the
    second, and leave the account the first one created unreachable.
    """
    with _lock:
        existing = stored_password()
        if existing:
            return existing
        password = generate()
        _store(password)
        log.info("generated the account password jobbot will use for employer signups (%s)", location_hint())
        return password


def location_hint() -> str:
    """Where a person can read the password themselves — printed in the message that asks them to sign in by
    hand, because without it they are being told to log into an account whose password they have never seen."""
    for name in (SECRET_NAME, *LEGACY_SECRET_NAMES):
        if config.get_secret(name):
            # Name the item that actually holds it: a machine upgraded from the old Workday-only secret is
            # still using that one, and sending someone to look for a keychain entry that is not there is
            # worse than not telling them at all.
            return f"your keychain, service '{config.KEYRING_SERVICE}', item {name}"
    if FALLBACK_PATH.exists():
        return str(FALLBACK_PATH)
    return f"{SECRET_NAME} (not stored yet)"


def _store(password: str) -> None:
    try:
        config.set_secret(SECRET_NAME, password)
        return
    except Exception as e:  # noqa: BLE001 - a keychain that refuses must not stop the application
        log.warning("keychain unavailable (%s); storing the account password in %s instead",
                    type(e).__name__, FALLBACK_PATH)
    try:
        FALLBACK_PATH.write_text(password)
        os.chmod(FALLBACK_PATH, 0o600)
    except Exception as e:  # noqa: BLE001
        log.error("could not store the account password (%s). Accounts created in this run will work, but "
                  "signing back into them later will need a password reset.", e)
