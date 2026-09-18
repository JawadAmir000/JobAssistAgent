"""Small helpers for the web layer: formatting, settings snapshot, guarded imports of sibling packages."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from jobbot import config, credentials
from jobbot.models import CostSummary

# Every ats detect_ats can return, so the filters name what jobs actually carry. COMPANY_ATS_CHOICES stays
# the shorter list: discovery can only crawl boards it knows how to enumerate, which is not the same thing
# as the adapters that can fill a form.
ATS_CHOICES = ["greenhouse", "lever", "ashby", "workday", "zoho", "workable", "recruitee", "teamtailor",
               "jazzhr", "bamboohr", "smartrecruiters", "pageup", "linkedin", "indeed", "other"]
COMPANY_ATS_CHOICES = ["greenhouse", "lever", "ashby"]  # add_company() accepts only these
HOURS_CHOICES = [("24", "Last 24 hours"), ("72", "Last 3 days"), ("168", "Last week")]
# Secrets the Settings form will store. The account password is the one jobbot uses for every employer
# account it creates (Workday and anything else that puts a signup in front of the form); it is generated on
# first use, so the field is here to override it or to read one in from another machine, not to be filled in.
SECRET_NAMES = ["ANTHROPIC_API_KEY", "JOBBOT_MAIL_PASSWORD", credentials.SECRET_NAME]
SECRET_NOTES = {
    credentials.SECRET_NAME: "Generated automatically the first time jobbot signs you up to an employer, "
                             "and reused everywhere so those accounts stay reachable. Paste one to override.",
}


def today_iso() -> str:
    """ISO timestamp for today 00:00 UTC (matches llm_calls.created_at format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_cost(cs: CostSummary | None) -> str:
    """'N calls · X tokens · $Y' or '0 LLM calls'."""
    if cs is None or not cs.calls:
        return "0 LLM calls"
    calls = f"{cs.calls} call" + ("s" if cs.calls != 1 else "")
    return f"{calls} · {fmt_tokens(cs.total_tokens)} tokens · ${cs.cost_usd:.4f}"


def fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    return iso[:10]


def parse_locations(raw: str | None) -> list[str]:
    """'Remote, Canada, UK' -> ['Remote', 'Canada', 'UK']. None/blank means no location filter."""
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def parse_search_id(raw: str | int | None) -> int | None:
    try:
        search_id = int(raw or 0)
    except (TypeError, ValueError):
        return None
    return search_id if search_id > 0 else None


def days_from_hours(hours_old: str | int | None) -> int | None:
    """The 'Posted within' select as whole days, rounded up; None when it is unset or unparsable."""
    if hours_old in (None, ""):
        return None
    try:
        hours = int(hours_old)
    except (TypeError, ValueError):
        return None
    return max(1, round(hours / 24)) if hours > 0 else None


def hours_label(hours_old: str | int | None) -> str:
    """Human label for the chosen 'Posted within' value, '' when unset."""
    return dict(HOURS_CHOICES).get(str(hours_old or ""), "")


def parse_options(raw: str | None) -> list[str]:
    """pending_options is a JSON list; tolerate garbage."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(v) for v in val] if isinstance(val, list) else []


def settings_snapshot() -> dict[str, Any]:
    return {
        "provider": config.get_setting(config.SETTING_PROVIDER) or "anthropic",
        "model": config.get_setting(config.SETTING_MODEL) or "",
        "locations": config.get_setting(config.SETTING_LOCATIONS) or "",
        "hours_old": config.get_setting(config.SETTING_HOURS_OLD) or "72",
        "cv_path": config.get_setting(config.SETTING_CV_PATH) or "",
        "headless": (config.get_setting(config.SETTING_HEADLESS) or "0") == "1",
        "mail_user": _mail_user(),
    }


def _mail_user() -> str:
    """Mailbox the verification-code reader will use, '' if it cannot be determined."""
    try:
        from jobbot import mail
        return mail.mail_user()
    except Exception:
        return ""


def mail_status() -> tuple[str, str]:
    """('ok'|'warn'|'off', message) for the Settings page. Never raises, never blocks for long."""
    try:
        from jobbot import mail
        ok, why = mail.is_configured()
        if not ok:
            return "off", why
        ok, why = mail.check_connection(timeout_s=12)
        return ("ok" if ok else "warn"), why
    except Exception as e:  # noqa: BLE001
        return "warn", f"could not check: {e}"


def provider_infos() -> list[dict[str, Any]]:
    """[{name, default_model, ok, message}] — never raises (LLM package may be incomplete)."""
    try:
        from jobbot.llm.base import available_providers
        providers = available_providers()
    except Exception as e:  # noqa: BLE001 — ImportError or a backend that fails at import
        return [{"name": "anthropic", "default_model": "", "ok": False, "message": f"provider registry unavailable: {e}"}]
    out = []
    for name, cls in sorted(providers.items()):
        try:
            ok, msg = cls().is_configured()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"error: {e}"
        out.append({"name": name, "default_model": getattr(cls, "default_model", ""), "ok": ok, "message": msg})
    return out


def secret_status(name: str) -> tuple[str, str | None]:
    """('set'|'not set', note) without ever revealing the value."""
    note = SECRET_NOTES.get(name)
    if name == credentials.SECRET_NAME and credentials.stored_password():
        # Not necessarily under this name: it may still be the old JOBBOT_WORKDAY_PASSWORD, or the file used
        # where there is no keychain. Reporting "not set" for a password jobbot is actively using would send
        # the user off to set one that then replaces the accounts' real password.
        return "set", note
    try:
        import keyring
        if keyring.get_password(config.KEYRING_SERVICE, name):
            return "set", note
        keyring_note = None
    except Exception as e:  # noqa: BLE001
        keyring_note = f"keychain unavailable ({type(e).__name__}); export {name} as an environment variable instead"
    import os
    if os.environ.get(name):
        return "set (env)", keyring_note or note
    return "not set", keyring_note or note


def read_text(path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def sources_from_form(ats: str | None, jobspy: str | None) -> tuple[str, ...]:
    src = []
    if ats:
        src.append("ats")
    if jobspy:
        src.append("jobspy")
    return tuple(src) or ("ats", "jobspy")


def hx_request(request) -> bool:
    return request.headers.get("HX-Request") == "true"
