"""Adapter interface.

CONTRACT — each adapter (greenhouse.py, lever.py, ashby.py, ...) implements:

    class GreenhouseAdapter(Adapter):
        ats = "greenhouse"
        needs_account = False
        def apply(self, ctx: ApplyContext) -> None:
            # use ctx.page (Playwright Page, already navigated to ctx.job["url"])
            # call ctx.switch_page(page) when an Apply control opens a new Page
            # call ctx.step("...") to report progress
            # call ctx.answer(question, options=None, kind="text") to get an answer for a screening question
            #     -> returns str; raises NeedsHuman if the resolver needs the user (runner handles it)
            # call ctx.fact("email") etc for identity fields
            # ctx.cv_path for the CV file
            # raise NeedsHuman(reason) for captcha / phone verify / anything you must not automate
            # return normally after submit is confirmed; raise ApplyError on unrecoverable failure

Runner behaviour (runner.py): creates Playwright browser (headed unless setting says headless), navigates,
calls adapter.apply, catches NeedsHuman -> screenshot + status needs_you (+ pending_question when it came
from ctx.answer), ApplyError -> failed, success -> submitted; records cost via db.cost_for_job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse


class NeedsHuman(Exception):
    """Stop and ask the user. `question`/`options` set when a screening answer is needed."""
    def __init__(self, reason: str, question: str = "", options: list[str] | None = None, kind: str = "text"):
        super().__init__(reason)
        self.reason = reason
        self.question = question
        self.options = options or []
        self.kind = kind


class ApplyError(Exception):
    pass


class AlreadyApplied(ApplyError):
    """The employer says this application already exists, so there is nothing left to submit.

    Not a failure: the application is with them. Reporting it as one left a card in the failed pile with a
    Retry button that could only ever produce the same notice, and Workday's phrasing ("You've already
    applied for this job.") arrives on a page with no Next button, so before this the run asked the user to
    finish a step by hand that was already done a day earlier.
    """


@dataclass
class ApplyContext:
    job: dict                         # row from jobs table as dict
    page: Any                         # playwright.sync_api.Page
    facts: dict
    cv_path: str
    step: Callable[[str], None]       # report progress
    answer: Callable[..., str]        # answer(question, options=None, kind="text") -> str, may raise NeedsHuman
    screenshot: Callable[[str], str]  # screenshot(label) -> path
    # Report a value a control already carries, so the resolver knows what the last answer on this form was
    # without being asked for it. Adapters skip filled controls on a re-run, and an "if yes, explain" that
    # follows one needs to know whether its condition was met. Pass the control's label too: a value the
    # user typed into the window by hand is an answer jobbot never saw, and the runner caches it.
    #   seen(value, label="", *, kind="", options=None, default=False, placeholder=False)
    # `default`/`placeholder` say the markup put the value there rather than a person. Pass them: without
    # them an untouched country list reads as an answer, and the cache learns a citizenship nobody claimed.
    seen: Callable[..., None] = lambda *_a, **_k: None
    # A click may open the employer's application in another browser page. Keep the adapter's page and the
    # runner-owned references (screenshots, pause/resume, liveness checks) in sync when that happens.
    on_page_change: Callable[[Any], None] = lambda _page: None
    extra: dict = field(default_factory=dict)

    def switch_page(self, page: Any) -> None:
        self.page = page
        self.on_page_change(page)

    def fact(self, key: str, default: str = "") -> str:
        cur: Any = self.facts
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return "" if cur is None else str(cur)


class Adapter:
    ats: str = "other"
    needs_account: bool = False

    def apply(self, ctx: ApplyContext) -> None:
        raise NotImplementedError


_REGISTRY: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    _REGISTRY[cls.ats] = cls
    return cls


_LOADED = False


# The modules an adapter fix can live in, in dependency order: credentials, common and resolver first,
# because the adapters bind them by name at import and would otherwise keep pointing at the old copies.
# credentials is here for the same reason the rest are: a password rule it learns (a form's length cap, say)
# is useless if it only takes effect after a restart, and a restart is what closes the parked window.
_RELOADABLE = ("jobbot.answers", "jobbot.credentials", "jobbot.apply.common", "jobbot.apply.resolver",
               "jobbot.apply.account", "jobbot.apply.navigator",
               "jobbot.apply.generic",
               "jobbot.apply.greenhouse", "jobbot.apply.lever", "jobbot.apply.ashby",
               "jobbot.apply.linkedin", "jobbot.apply.workday")


def reload_adapters() -> list[str]:
    """Re-import the adapter modules in place and rebuild the registry. Returns what was reloaded.

    This is what makes a fix land on a browser that is already open. Python binds a module at import, so
    changing an adapter used to mean restarting the server — which closed every parked window and threw away
    the half-filled form inside it, along with the account signed into and the CV already uploaded. Keeping
    the window alive is the whole point of pausing rather than failing, and a restart undoes it.

    base itself is deliberately not reloaded: ApplyContext, NeedsHuman and ApplyError are live objects held
    by paused applications, and swapping their classes underneath would make `except NeedsHuman` stop
    catching the exceptions already in flight.

    The trap, for whoever adds the next module here: reloading rebuilds a module's contents, so `common.foo`
    and `store.BAR` pick the new code up, but a `from x import Name` in a module that is NOT reloaded keeps
    pointing at the object from before the reload. runner.py held `from jobbot.apply.resolver import
    Resolver` that way, so every edit to the resolver was reloaded and then quietly ignored — a rule that
    answers "What is highest level of education you have completed?" was written, saved, reloaded and still
    unused, and application 146 paused to ask it. Import such a name inside the function instead.
    """
    import importlib
    import sys

    _ensure_loaded()
    reloaded: list[str] = []
    for name in _RELOADABLE:
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            importlib.reload(module)
            reloaded.append(name)
        except Exception as e:  # noqa: BLE001 - a module that will not reload must not lose the browser
            import logging
            logging.getLogger(__name__).warning("could not reload %s: %s", name, e)
    return reloaded


def _ensure_loaded() -> None:
    """Import every adapter module, once. Guarding on an empty registry instead skips the import as soon as
    any single adapter has been registered on its own — and linkedin.py registers itself, then asks for the
    greenhouse/lever/ashby adapter it must hand off to, which would come back None on a supported board."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True          # set first: an adapter module importing back into here must not recurse
    from jobbot.apply import greenhouse, lever, ashby, linkedin, generic, workday  # noqa: F401


# Hostname -> ats name, most specific first. The plain-form boards all resolve to adapters that are the same
# generic walker under different names (see generic.py); they are listed separately so a search log, a job
# row and a failure message all say which board it actually was.
_HOSTS: tuple[tuple[str, str], ...] = (
    ("greenhouse.io", "greenhouse"),
    ("grnh.se", "greenhouse"),             # Greenhouse's own shortener, which is what a LinkedIn Apply
                                           # button usually carries (see linkedin._delegate)
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("workday.com", "workday"),
    ("zohorecruit.com", "zoho"),
    ("workable.com", "workable"),
    ("recruitee.com", "recruitee"),
    ("teamtailor.com", "teamtailor"),
    ("applytojob.com", "jazzhr"),          # JazzHR serves boards from applytojob.com
    ("jazz.co", "jazzhr"),
    ("bamboohr.com", "bamboohr"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("pageuppeople.com", "pageup"),
    ("icims.com", "icims"),
    # Oracle Recruiting (Fusion "CandidateExperience"): every tenant is <code>.fa.<region>.oraclecloud.com,
    # so the host is the only stable part. It is walked by the generic adapter like the rest of this
    # group -- naming it buys nothing at apply time and everything in the issues table, where six months
    # of Oracle failures were filed under "other" together with every unrecognised careers page.
    ("oraclecloud.com", "oracle"),                     # Atlassian and much of the enterprise mid-market
    ("successfactors.com", "successfactors"),   # SAP SuccessFactors; EY and a lot of enterprises run it
    ("successfactors.eu", "successfactors"),
    ("sapsf.com", "successfactors"),
    ("sapsf.eu", "successfactors"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
)


def detect_ats(url: str) -> str:
    """The ats name for a URL. Unknown hosts come back as "other", which the generic adapter also claims —
    it inspects the page and asks for help rather than guessing, so an unrecognised career site is attempted
    carefully instead of being refused outright."""
    host = (urlparse(url).netloc or "").lower()
    for needle, ats in _HOSTS:
        if needle in host:
            return ats
    return "other"


def get_adapter_for(ats: str) -> Adapter | None:
    """The adapter for an ats name, falling back to the generic walker for one nothing claims.

    The fallback is the whole point of the walker: it inspects the page rather than assuming a vendor, so a
    board jobbot has never seen is attempted carefully instead of refused. Before it, an unrecognised name
    ended the application on "No adapter for <name>" without the browser ever opening — which is a worse
    answer than "I looked at the page and could not find a form on it", and left nothing on screen for the
    user to finish by hand.
    """
    _ensure_loaded()
    cls = _REGISTRY.get(ats) or _REGISTRY.get("generic")
    return cls() if cls else None
