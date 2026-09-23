"""The one password jobbot uses for the accounts it creates on employers' ATS tenants.

Workday — and any career site that puts a signup in front of the form — will not show the application until
the candidate has an account with that employer, and every tenant is a separate account: fifty Workday
employers means fifty signups. Asking the user to invent and remember a password per employer is what turned
a run into a queue of pauses, so the password is generated once here, kept in the OS keychain, and reused for
every account. Reuse is the point, not a shortcut: an account jobbot created a week ago can only be signed
back into if the password is still known.

It never lives in facts.yaml. That file is read into LLM prompts, so a password written there would be sent
to the model and kept in the prompt log; config.migrate_secrets_from_facts() exists for exactly that reason.

Some forms cap the length as well as setting a minimum, and the cap can be below what the primary password
is: SAP SuccessFactors prints "Password must not be longer than 18 characters" and the primary is 20. So
there is a second, shorter managed password for those, generated and stored exactly like the first. Shorter
on the spot and forgotten would not do — the account created with it has to stay reachable.

Contract:
    account_password(max_length=0) -> str   # never empty, never asks; generates and stores on first use
    stored_password()  -> str     # what is stored now, '' if nothing yet; never generates
    location_hint(max_length=0) -> str      # where to read it by hand, for the messages that tell the user to
"""
from __future__ import annotations

import logging
import os
import secrets
import string
import threading
from pathlib import Path
from typing import NamedTuple

from jobbot import config

log = logging.getLogger(__name__)

SECRET_NAME = "JOBBOT_ACCOUNT_PASSWORD"
# The same thing again, short enough for the forms that cap the length below what SECRET_NAME holds. A
# second stored password rather than one derived on demand, because deriving it means the derivation has to
# be reproduced exactly on every later run or the account it created can never be signed into again.
SHORT_SECRET_NAME = "JOBBOT_ACCOUNT_PASSWORD_SHORT"
# What the Workday adapter called this back when it was a password the user had to think up and type into
# Settings. Accounts created with it still exist at whatever employers it was used on, so it seeds the
# managed password rather than being replaced by a fresh one those accounts would reject.
LEGACY_SECRET_NAMES = ("JOBBOT_WORKDAY_PASSWORD",)
# A machine with no usable keychain (a headless box, a locked login keyring) must still be able to sign back
# into an account it created, so the password falls back to a file only this user can read.
FALLBACK_PATH = config.DATA_DIR / ".account_password"
SHORT_FALLBACK_PATH = config.DATA_DIR / ".account_password_short"

LENGTH = 20
# Clears every minimum these forms print (12 is the strictest seen) and fits every maximum (SuccessFactors'
# 18 is the strictest seen). A form stricter than both cannot be satisfied by either managed password, and
# says so rather than being guessed at.
SHORT_LENGTH = 14
# The punctuation every ATS password box has accepted. No quotes, backslashes or spaces: those are what get
# mangled by a form that re-encodes the value before it stores it.
SYMBOLS = "!@#$%^*-_=+"

_lock = threading.Lock()


def meets_requirements(password: str, max_length: int = 0) -> bool:
    """The union of the password rules these forms print above the box — Workday's list is the strictest we
    have seen (8+ characters, upper, lower, numeric, special) and this clears all of them at once.

    `max_length` is a cap the form states, 0 when it states none. It is a parameter rather than a constant
    because only the form knows it, and only some forms have one: SuccessFactors caps at 18 beside a box
    whose maxlength attribute says 99, so a password that satisfies every other rule here is still refused.
    """
    return (len(password) >= 12
            and (not max_length or len(password) <= max_length)
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


class _Managed(NamedTuple):
    """One managed password: where it is kept and how long it is generated."""
    name: str
    legacy: tuple[str, ...]
    path: Path
    length: int


def _primary() -> _Managed:
    """Built from the module's own constants on every call rather than frozen at import. A spec that
    captured FALLBACK_PATH once would be a second place the path lives, and the two drift the moment
    anything — a test, a machine without a keychain — sets the constant."""
    return _Managed(SECRET_NAME, LEGACY_SECRET_NAMES, FALLBACK_PATH, LENGTH)


def _short() -> _Managed:
    return _Managed(SHORT_SECRET_NAME, (), SHORT_FALLBACK_PATH, SHORT_LENGTH)


def _stored(spec: _Managed) -> str:
    """What `spec` holds right now, '' if nothing yet. Never generates."""
    for name in (spec.name, *spec.legacy):
        value = (config.get_secret(name) or "").strip()
        if value:
            return value
    try:
        return spec.path.read_text().strip()
    except Exception:
        return ""


def _password(spec: _Managed) -> str:
    """`spec`'s password, generated and stored on first use.

    The lock matters: two applications starting together would otherwise generate two passwords, store the
    second, and leave the account the first one created unreachable.
    """
    with _lock:
        existing = _stored(spec)
        if existing:
            return existing
        password = generate(spec.length)
        _store(password, spec)
        log.info("generated the account password jobbot will use for employer signups (%s)", _hint(spec))
        return password


def stored_password() -> str:
    """The managed password as it stands, '' before the first account was ever created. Never generates, so
    the Settings page can report honestly without creating a secret as a side effect of being looked at."""
    return _stored(_primary())


def stored_short_password() -> str:
    """The shorter managed password as it stands, '' before a length-capped form ever needed one."""
    return _stored(_short())


def account_password(max_length: int = 0) -> str:
    """The password for every account jobbot creates, short enough for a form that caps the length.

    `max_length` is what the form says it will take (see common.password_max_length), 0 when it says
    nothing — which is almost every form, and gets the primary password. Only a form whose cap is below the
    primary's length is given the short one, so accounts already created on the primary are undisturbed and
    the second password stays a rarity rather than a second thing to keep track of.
    """
    primary = _password(_primary())
    if not max_length or len(primary) <= max_length:
        return primary
    short = _password(_short())
    if len(short) > max_length:
        # Both are too long for this form. Returning the shorter of the two is still the right move: the
        # form refuses it and says why, which reaches the user as the site's own words, and inventing a
        # password that fits here would create an account nothing could ever sign back into.
        log.warning("this form caps passwords at %d characters and neither managed password is that short "
                    "(%d, %d); the site will refuse it and say so", max_length, len(primary), len(short))
    else:
        log.info("this form caps passwords at %d characters; using the short managed password (%s)",
                 max_length, _hint(_short()))
    return short


def _hint(spec: _Managed) -> str:
    for name in (spec.name, *spec.legacy):
        if config.get_secret(name):
            # Name the item that actually holds it: a machine upgraded from the old Workday-only secret is
            # still using that one, and sending someone to look for a keychain entry that is not there is
            # worse than not telling them at all.
            return f"your keychain, service '{config.KEYRING_SERVICE}', item {name}"
    if spec.path.exists():
        return str(spec.path)
    return f"{spec.name} (not stored yet)"


def location_hint(max_length: int = 0) -> str:
    """Where a person can read the password themselves — printed in the message that asks them to sign in by
    hand, because without it they are being told to log into an account whose password they have never seen.

    Takes the same `max_length` as account_password so that the message names the password that was actually
    used on this form. Pointing someone at the primary when the account was created with the short one is
    how a person ends up certain the password is wrong.
    """
    primary = _stored(_primary())
    if max_length and primary and len(primary) > max_length:
        return _hint(_short())
    return _hint(_primary())


def _store(password: str, spec: _Managed) -> None:
    try:
        config.set_secret(spec.name, password)
        return
    except Exception as e:  # noqa: BLE001 - a keychain that refuses must not stop the application
        log.warning("keychain unavailable (%s); storing the account password in %s instead",
                    type(e).__name__, spec.path)
    try:
        spec.path.write_text(password)
        os.chmod(spec.path, 0o600)
    except Exception as e:  # noqa: BLE001
        log.error("could not store the account password (%s). Accounts created in this run will work, but "
                  "signing back into them later will need a password reset.", e)
