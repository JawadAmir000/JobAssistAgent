"""Paths and settings. Settings live in the SQLite `settings` table; secrets in the OS keychain.

Contract (do not change signatures without updating all callers):
    ROOT, DATA_DIR, DB_PATH, FACTS_PATH, ANSWERS_PATH, COMPANIES_PATH, SESSIONS_DIR, SCREENSHOTS_DIR, CV_DIR
    get_setting(key, default=None) -> str | None
    set_setting(key, value) -> None
    get_secret(name) -> str | None            # keyring, falls back to env var of same name
    set_secret(name, value) -> None
    load_facts() -> dict                      # facts.yaml
    load_answers() -> dict[str, str]          # answers.json, flat view (normalised question -> answer)
    save_answers(d) -> None                   # a flat map the candidate asserted; replaces the store
    load_answer_records() -> dict[str, AnswerRecord]
    save_answer_records(records) -> None      # merge onto disk, keeping provenance
"""
from __future__ import annotations

import re
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobbot.db"
FACTS_PATH = ROOT / "facts.yaml"
ANSWERS_PATH = ROOT / "answers.json"
COMPANIES_PATH = ROOT / "companies.yaml"
SESSIONS_DIR = DATA_DIR / "sessions"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
CV_DIR = DATA_DIR / "cv"

for _d in (DATA_DIR, SESSIONS_DIR, SCREENSHOTS_DIR, CV_DIR):
    _d.mkdir(parents=True, exist_ok=True)

KEYRING_SERVICE = "jobbot"

# Setting keys used across the app
SETTING_PROVIDER = "llm_provider"          # "anthropic" | "claude_code" | "codex"
SETTING_MODEL = "llm_model"                # provider-specific model id (may be empty)
SETTING_LOCATIONS = "search_locations"     # comma-separated
SETTING_HOURS_OLD = "search_hours_old"     # e.g. "72"
SETTING_CV_PATH = "cv_path"
SETTING_HEADLESS = "browser_headless"      # "0" | "1"
SETTING_MAIL_USER = "mail_user"            # mailbox to read verification codes from (blank = identity.email)
SETTING_IMAP_HOST = "imap_host"            # blank = imap.gmail.com
SETTING_HUMAN_TYPING = "human_typing"      # "1" = type field values keystroke by keystroke

DEFAULTS = {
    SETTING_PROVIDER: "anthropic",
    SETTING_MODEL: "",
    SETTING_LOCATIONS: "Remote, Canada, Australia, United Kingdom, United Arab Emirates, Singapore",
    SETTING_HOURS_OLD: "72",
    SETTING_CV_PATH: "",
    SETTING_HEADLESS: "0",
    SETTING_MAIL_USER: "",
    SETTING_IMAP_HOST: "",
    SETTING_HUMAN_TYPING: "1",
}


def get_setting(key: str, default: str | None = None) -> str | None:
    from jobbot import db
    val = db.get_setting(key)
    if val is None:
        return DEFAULTS.get(key, default)
    return val


def set_setting(key: str, value: str) -> None:
    from jobbot import db
    db.set_setting(key, value)


def get_secret(name: str) -> str | None:
    """Secret from OS keychain, else environment variable of the same name."""
    try:
        import keyring
        val = keyring.get_password(KEYRING_SERVICE, name)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(name)


def set_secret(name: str, value: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, name, value)


# Keys that are credentials, not facts. facts.yaml is a plaintext file whose contents are put into LLM
# prompts, so a password written here would be sent to the model and cached in the prompt log. Anything
# matching moves to the OS keychain and is stripped from the file.
SECRET_FACT_KEYS = {
    "mail_password": "JOBBOT_MAIL_PASSWORD", "email_password": "JOBBOT_MAIL_PASSWORD",
    "imap_password": "JOBBOT_MAIL_PASSWORD", "app_password": "JOBBOT_MAIL_PASSWORD",
    "mail_app_password": "JOBBOT_MAIL_PASSWORD", "gmail_app_password": "JOBBOT_MAIL_PASSWORD",
    "gmail_password": "JOBBOT_MAIL_PASSWORD", "mail_pass": "JOBBOT_MAIL_PASSWORD",
    "anthropic_api_key": "ANTHROPIC_API_KEY", "api_key": "ANTHROPIC_API_KEY",
}


def _strip_secret_keys(node, found: dict) -> bool:
    """Remove credential keys anywhere in the facts tree, collecting them. True if anything was removed."""
    changed = False
    if isinstance(node, dict):
        for key in list(node.keys()):
            secret_name = SECRET_FACT_KEYS.get(str(key).strip().lower())
            value = node[key]
            if secret_name and isinstance(value, (str, int)) and str(value).strip():
                found[secret_name] = str(value).strip()
                del node[key]
                changed = True
            elif isinstance(value, (dict, list)):
                changed = _strip_secret_keys(value, found) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _strip_secret_keys(item, found) or changed
    return changed


def migrate_secrets_from_facts() -> list[str]:
    """Move any credential written into facts.yaml to the keychain and rewrite the file without it.

    Returns the secret names moved. A password put in facts.yaml is not a typo to correct silently: the file
    is read into LLM prompts, so it has to come out of there whether or not the keychain write succeeds.
    """
    if not FACTS_PATH.exists():
        return []
    try:
        data = yaml.safe_load(FACTS_PATH.read_text()) or {}
    except Exception:
        return []
    found: dict[str, str] = {}
    if not _strip_secret_keys(data, found) or not found:
        return []
    for name, value in found.items():
        try:
            set_secret(name, value)
        except Exception:
            import logging
            logging.getLogger(__name__).error(
                "%s found in facts.yaml but the keychain is unavailable; removing it from the file anyway. "
                "Export %s=... in the shell that runs jobbot, or add it in Settings.", name, name)
    FACTS_PATH.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return sorted(found)


def load_facts() -> dict:
    if not FACTS_PATH.exists():
        return {}
    facts = yaml.safe_load(FACTS_PATH.read_text()) or {}
    # Defence in depth: even if migration has not run, a credential never reaches a prompt.
    _strip_secret_keys(facts, {})
    return facts


def set_fact(key: str, value: str) -> bool:
    """Write one dotted key back into facts.yaml, keeping the file's comments. True when it was written.

    A surgical line edit, not a re-dump: facts.yaml is hand-written and its comments are the record of why
    each value is what it is ("confirmed 2026-09-14: no sponsorship needed to work in Bangladesh"), and
    yaml.safe_dump would throw all of them away. Only an existing leaf under an existing block is touched,
    so this can add nothing and move nothing; anything else returns False and the caller falls back.
    """
    parts = [p for p in (key or "").split(".") if p]
    value = str(value or "").strip()
    if len(parts) != 2 or not value or "\n" in value or len(value) > 300 or not FACTS_PATH.exists():
        return False
    block, leaf = parts
    try:
        lines = FACTS_PATH.read_text().splitlines(keepends=True)
    except OSError:
        return False
    in_block = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if re.match(rf"^{re.escape(block)}\s*:", stripped):
            in_block = True
            continue
        if in_block and stripped and not stripped[0].isspace():
            break                      # the next top-level block started; the leaf is not in ours
        if not in_block:
            continue
        m = re.match(rf"^(\s+{re.escape(leaf)}\s*:\s*)(.*?)(\s+#.*)?$", stripped)
        if not m:
            continue
        # Quoted unless the value is plainly a word or a URL. An unquoted "+8801XXXXXXXXX" is read back
        # by YAML as the integer 8801XXXXXXXXX — the phone number silently loses its "+" and every form
        # after that gets a number that is not the candidate's.
        plain = bool(re.fullmatch(r"[A-Za-z][\w@:/.\-]*", value))
        quoted = value if plain else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines[i] = m.group(1) + quoted + (m.group(3) or "") + "\n"
        original = FACTS_PATH.read_text()
        candidate = "".join(lines)
        try:
            # Never leave the file in a state that does not read back as what was asked for: it is the
            # source of every answer on every form, and a corrupted value is worse than no update at all.
            parsed = yaml.safe_load(candidate) or {}
            if str(((parsed.get(block) or {}) if isinstance(parsed.get(block), dict) else {}).get(leaf)) != value:
                import logging
                logging.getLogger(__name__).warning(
                    "not writing %s to facts.yaml: it would not read back as %r", key, value)
                return False
            FACTS_PATH.write_text(candidate)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("facts.yaml not updated (%s); leaving it as it was", e)
            try:
                FACTS_PATH.write_text(original)
            except OSError:
                pass
            return False
        return True
    return False


def load_answer_records() -> dict[str, "answers.AnswerRecord"]:
    """The answer memory with its provenance. jobbot.answers owns the format; this is the way in."""
    from jobbot import answers
    return answers.load()


def save_answer_records(records: dict[str, "answers.AnswerRecord"]) -> None:
    """Merge records onto what is on disk. What a run learns must not drop what a parallel run learned."""
    from jobbot import answers
    answers.save_merged(records)


def load_answers() -> dict[str, str]:
    """The flat {question: answer} view, for callers that only want the text. Quarantined and rejected
    records are not in it: they are remembered, but they are not answers."""
    from jobbot import answers
    return answers.text_view(answers.load())


def save_answers(d: dict[str, str]) -> None:
    """Replace the store from a flat map. Everything in `d` is taken as the candidate's own answer, which is
    what it is: this is the Settings box and the test fixtures, not anything jobbot inferred."""
    from jobbot import answers
    answers.save(answers.from_mapping(d, default_source="human"))


CV_TEXT_MAX = 12000
_cv_text_cache: dict[str, str] = {}


def load_cv_text(cv_path: str | None = None) -> str:
    """Plain text of the CV, so screening answers can be grounded in what the CV actually says rather than
    in facts.yaml alone. Cached per (path, mtime); returns '' if the file is missing or unreadable."""
    path = cv_path or get_setting(SETTING_CV_PATH) or ""
    if not path or not os.path.exists(path):
        return ""
    key = f"{path}:{os.path.getmtime(path)}"
    if key in _cv_text_cache:
        return _cv_text_cache[key]
    text = ""
    try:
        if path.lower().endswith(".pdf"):
            from pypdf import PdfReader
            text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        else:
            text = Path(path).read_text(errors="ignore")
    except Exception:  # noqa: BLE001 - a CV we cannot parse must never break an application
        text = ""
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()[:CV_TEXT_MAX]
    _cv_text_cache[key] = text
    return text
