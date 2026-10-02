"""One look at a page: what it says, what can be filled, what can be pressed, and what it is signalling.

The walker asks the page a dozen yes/no questions in a fixed order — is there a form, a captcha, a code box, a
sign-in, a thank-you — and every one of them is a rule somebody wrote after a board broke. When none of them
answers, the walker used to give up. The planner (planner.py) does not: it looks at the page as a whole and
decides what kind of page it is. This module is what it looks at, and what the playbook (playbook.py) keys
its memory on, so the two always agree on what a page was.

Contract:
    snapshot(page) -> dict            # see the keys built in snapshot()
    signature(snap) -> str            # this step of this site, stable across visits
    lesson_text(snap) -> str          # the words that say what kind of page it is, for cross-site memory

Read-only apart from one thing: the pressable controls are marked (navigator's data-jobbot-nav) so the one
the planner chooses can be pressed without writing a selector.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from jobbot.apply import account, common as c, navigator

log = logging.getLogger(__name__)

TEXT_CHARS = 1500

_PAGE_JS = "() => {" + c.DEEP_JS + c.WIDGET_JS + r"""
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const heads = [];
    for (const h of deepAll('h1, h2, h3, [role=heading], legend')) {
        if (!vis(h)) continue;
        const t = (h.innerText || '').replace(/\s+/g, ' ').trim();
        if (t && t.length < 200 && !heads.includes(t)) heads.push(t);
        if (heads.length >= 6) break;
    }
    let fields = 0;
    for (const el of deepAll('input, select, textarea, [role=combobox], [contenteditable=true]')) {
        const t = (el.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(t)) continue;
        if (vis(el) || ((t === 'radio' || t === 'checkbox') && el.labels && el.labels.length && vis(el.labels[0]))) fields++;
    }
    fields += widgetRoots('body').length;     // dropdowns built out of divs (common.WIDGET_JS)
    return {heads: heads, fields: fields,
            files: deepAll('input[type=file]').length,
            text: ((document.body && document.body.innerText) || '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n')};
}"""


def snapshot(page: Any) -> dict:
    """Everything the planner and the playbook need about the page as it is right now."""
    try:
        raw = page.evaluate(_PAGE_JS) or {}
    except Exception as e:  # noqa: BLE001 - a page mid-navigation; an empty look is still a look
        log.debug("observe: page read failed: %s", e)
        raw = {}
    url = getattr(page, "url", "") or ""
    text = c.clean(raw.get("text") or "")
    snap = {
        "url": url,
        "host": _host(url),
        "path": _path_shape(url),
        "title": _title(page),
        "headings": raw.get("heads") or [],
        "text": text[:TEXT_CHARS],
        "fields": int(raw.get("fields") or 0),
        "file_inputs": int(raw.get("files") or 0),
        "controls": navigator.controls(page),
        "signals": _signals(page),
    }
    return snap


def _signals(page: Any) -> dict:
    """The page's own answers to the questions the walker already knows how to ask. Each is cheap and
    deterministic, and when one is set the planner trusts it over the model."""
    sig: dict = {}
    for name, probe in (
        ("confirmation", lambda: c.confirmation_showing(page)),
        ("already_applied", lambda: c.already_applied_message(page)),
        ("captcha", lambda: c.detect_captcha(page, raise_=False)),
        ("bot_block", lambda: c.detect_bot_block(page, raise_=False)),
        ("verification_code", lambda: c.verification_prompt(page)),
        ("email_link", lambda: c.login_link_prompt(page)),
        ("sso", lambda: c.sso_provider(getattr(page, "url", "") or "")),
        ("account_gate", lambda: account.at_gate(page)),
        ("blocked", lambda: c.submit_blocked_message(page)),
    ):
        try:
            value = probe()
        except Exception as e:  # noqa: BLE001 - one probe failing must not blind the others
            log.debug("observe: %s probe: %s", name, e)
            value = None
        if value:
            sig[name] = value
    return sig


def signature(snap: dict) -> str:
    """This step of this site, the same on every visit: host, the URL's shape, and what the page is headed.

    Ids and counts are taken out of both, so the third application to a board lands on the same key as the
    first, and "Step 2 of 5" matches "Step 2 of 6".
    """
    head = _norm(" | ".join(snap.get("headings", [])[:2])) or _norm(snap.get("text", "")[:80])
    return f"{snap.get('host', '')}|{snap.get('path', '')}|{head[:120]}"


def lesson_text(snap: dict) -> str:
    """The words that say what kind of page this is, whichever site it is on: its headings and the start of
    its text. What a cross-site lesson is matched on (playbook.recall)."""
    return _norm(" ".join(snap.get("headings", [])[:3]) + " " + snap.get("text", "")[:300])


def describe(snap: dict, limit: int = 1200) -> str:
    """The snapshot as the model is shown it."""
    sig = snap.get("signals") or {}
    lines = [
        f"URL: {snap.get('url', '')[:160]}",
        f"Title: {snap.get('title', '')}",
        f"Headings: {' / '.join(snap.get('headings', [])) or '(none)'}",
        f"Fillable fields visible: {snap.get('fields', 0)}; file inputs: {snap.get('file_inputs', 0)}",
    ]
    if sig:
        lines.append("Detected: " + ", ".join(f"{k}={str(v)[:60]}" for k, v in sig.items()))
    lines.append("Page text:\n" + snap.get("text", "")[:limit])
    return "\n".join(lines)


def _norm(text: str) -> str:
    text = re.sub(r"\d+", "#", (text or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w#\s]", " ", text)).strip()


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def _path_shape(url: str) -> str:
    """The URL's path with its ids taken out: /companies/stan/16453078/apply/cv -> /companies/stan/#/apply/cv."""
    try:
        path = urlparse(url).path or "/"
    except Exception:  # noqa: BLE001
        return "/"
    parts = []
    for seg in path.split("/"):
        if re.search(r"\d{3,}|^[0-9a-f]{12,}$|^[0-9a-f-]{20,}$", seg, re.I):
            seg = "#"
        parts.append(seg)
    return "/".join(parts)[:120]


def _title(page: Any) -> str:
    try:
        return c.clean(page.title())[:100]
    except Exception:  # noqa: BLE001
        return ""
