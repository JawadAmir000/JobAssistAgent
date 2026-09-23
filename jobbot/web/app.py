"""FastAPI app for the local UI. Run with `python -m jobbot` (uvicorn jobbot.web.app:app)."""
from __future__ import annotations

import json
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobbot import config, db
from jobbot.web import helpers as h

HERE = Path(__file__).resolve().parent


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.init()
    with db.connect() as c:
        c.execute("UPDATE searches SET status='error', warning='interrupted by a restart' "
                  "WHERE status='running'")
    yield


app = FastAPI(title="jobbot", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["date"] = h.fmt_date
templates.env.filters["cost"] = h.fmt_cost


# ---------- rendering ----------
def _page(request: Request, tab: str, template: str, ctx: dict | None = None, **extra) -> HTMLResponse:
    """Full page on direct navigation; just the tab fragment for HTMX tab switches."""
    ctx = {"tab": tab, "tab_template": template, **(ctx or {}), **extra}
    if h.hx_request(request):
        return templates.TemplateResponse(request, template, ctx)
    ctx["cost_text"] = h.fmt_cost(db.cost_total(h.today_iso()))
    return templates.TemplateResponse(request, "base.html", ctx)


def _partial(request: Request, template: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _app_ctx(app_id: int) -> dict:
    a = db.get_application(app_id)
    job = db.get_job(a["job_id"]) if a else None
    return {
        "a": a,
        "job": job,
        "options": h.parse_options(a["pending_options"]) if a else [],
        "cost_text": h.fmt_cost(db.cost_for_job(a["job_id"])) if a else "",
        "screenshot_name": Path(a["screenshot"]).name if a and a["screenshot"] else "",
    }


# ---------- health / meter ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cost", response_class=HTMLResponse)
def cost_meter(request: Request):
    return _partial(request, "partials/cost_meter.html", cost_text=h.fmt_cost(db.cost_total(h.today_iso())))


# ---------- jobs tab ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    s = h.settings_snapshot()
    active = db.latest_snapshot_search()
    return _page(
        request, "jobs", "tabs/jobs.html", settings=s, hours_choices=h.HOURS_CHOICES,
        active_search=active, active_search_id=active["id"] if active else "",
        form_title=active["query"] if active else "",
        form_locations=active["locations"] if active else s["locations"],
        form_hours=str(active["hours_old"] or s["hours_old"]) if active else s["hours_old"],
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, min_score: int = 3, q: str = "", low: str | None = None,
              search_id: str = ""):
    """Render one exact search snapshot; never fall back to unrelated job history."""
    if low:  # "show low scores" toggle
        min_score = 1
    sid = h.parse_search_id(search_id)
    search = db.get_search(sid) if sid else None
    if search is not None and search["has_snapshot"]:
        jobs = db.list_jobs(min_score=min_score, query=q or None, search_id=sid, limit=500)
        locs = h.parse_locations(search["locations"])
        total = db.count_search_jobs(sid)
        return _partial(request, "partials/job_list.html", jobs=jobs, shown=len(jobs), total=total,
                        min_score=min_score, q=q, locations=locs,
                        hours_label=h.hours_label(search["hours_old"]), search=search)
    return _partial(request, "partials/job_list.html", jobs=[], shown=0, total=0,
                    min_score=min_score, q=q, locations=[], hours_label="", search=None)


@app.post("/jobs/{job_id}/hide", response_class=HTMLResponse)
def hide_job(job_id: str):
    db.set_job_hidden(job_id, True)
    return HTMLResponse("")


@app.post("/search", response_class=HTMLResponse)
def start_search(request: Request, title: str = Form(""), locations: str = Form(""),
                 hours_old: str = Form("72"), src_ats: str | None = Form(None), src_jobspy: str | None = Form(None)):
    title = title.strip() or "Forward Deployed Engineer"
    locs = [x.strip() for x in locations.split(",") if x.strip()] or None
    try:
        hours = int(hours_old)
    except ValueError:
        hours = 72
    sources = h.sources_from_form(src_ats, src_jobspy)
    try:
        from jobbot.discovery import run_search
    except ImportError as e:
        return _partial(request, "partials/search_status.html", error=f"discovery package unavailable: {e}")
    search_id = db.create_search(title, ", ".join(locs) if locs else "", hours)

    def _run():
        try:
            run_search(title, locations=locs, hours_old=hours, sources=sources, search_id=search_id)
        except Exception as e:  # noqa: BLE001
            db.update_search(search_id, status="error", log=f"error: {e}")

    _spawn(_run)
    return _partial(request, "partials/search_status.html", search=db.get_search(search_id),
                    search_id=search_id, oob=True, restored=False)


@app.get("/search/{search_id}/status", response_class=HTMLResponse)
def search_status(request: Request, search_id: int):
    s = db.get_search(search_id)
    if s is None:
        return _partial(request, "partials/search_status.html", error="unknown search")
    return _partial(request, "partials/search_status.html", search=s, search_id=search_id,
                    oob=False, restored=False)


# ---------- apply flow ----------
@app.post("/apply/{job_id}", response_class=HTMLResponse)
def apply_job(request: Request, job_id: str):
    job = db.get_job(job_id)
    if job is None:
        return HTMLResponse("<p class='error'>unknown job</p>", status_code=404)
    app_id = db.create_application(job_id)
    try:
        from jobbot.apply import run_application
    except ImportError as e:
        db.update_application(app_id, status="failed", reason=f"apply package unavailable: {e}")
    else:
        db.update_application(app_id, status="running", step="starting")
        _spawn(_safe_run, run_application, app_id)
    return _partial(request, "partials/application_card.html", **_app_ctx(app_id))


def _safe_run(fn, app_id: int, *args) -> None:
    try:
        fn(app_id, *args)
    except Exception as e:  # noqa: BLE001 — runner should handle its own errors; this is the last resort
        db.update_application(app_id, status="failed", reason=f"unhandled: {e}")


@app.get("/application/{app_id}/card", response_class=HTMLResponse)
def application_card(request: Request, app_id: int):
    ctx = _app_ctx(app_id)
    if ctx["a"] is None:
        return HTMLResponse("<p class='error'>unknown application</p>", status_code=404)
    return _partial(request, "partials/application_card.html", **ctx)


@app.post("/application/{app_id}/answer", response_class=HTMLResponse)
def application_answer(request: Request, app_id: int, answer: str = Form("")):
    a = db.get_application(app_id)
    if a is None:
        return HTMLResponse("<p class='error'>unknown application</p>", status_code=404)
    if a["status"] != "needs_you":
        # Nothing is waiting for this answer. Resuming anyway would find no parked browser and re-run the
        # whole application from the top, which on a submitted one means applying twice.
        return _partial(request, "partials/application_card.html", **_app_ctx(app_id))
    try:
        from jobbot.apply import resume_application
    except ImportError as e:
        db.update_application(app_id, status="failed", reason=f"apply package unavailable: {e}")
    else:
        # NB: pending_question must survive until the runner has read it — resume_application looks the
        # question up in the DB to know what the answer belongs to. The runner clears it once it has learned.
        db.update_application(app_id, status="running", step="resuming")
        _spawn(_safe_run, resume_application, app_id, answer)
    return _partial(request, "partials/application_card.html", **_app_ctx(app_id))


@app.post("/application/{app_id}/retry", response_class=HTMLResponse)
def application_retry(request: Request, app_id: int):
    """Re-run the adapter on the window this application already has open.

    A failed run keeps its browser, so the half-filled form — and anything the user fixed in it by hand —
    survives the retry. Only when that window is gone does this fall back to a fresh application, which is
    what Retry always used to do.
    """
    a = db.get_application(app_id)
    if a is None:
        return HTMLResponse("<p class='error'>unknown application</p>", status_code=404)
    try:
        from jobbot.apply import resume_application
        from jobbot.apply.runner import has_live_browser
    except ImportError as e:
        db.update_application(app_id, status="failed", reason=f"apply package unavailable: {e}")
        return _partial(request, "partials/application_card.html", **_app_ctx(app_id))

    if has_live_browser(app_id):
        db.update_application(app_id, status="running", step="retrying")
        _spawn(_safe_run, resume_application, app_id, "")
        return _partial(request, "partials/application_card.html", **_app_ctx(app_id))
    return apply_job(request, a["job_id"])


@app.post("/application/{app_id}/mark-applied", response_class=HTMLResponse)
def application_mark_applied(request: Request, app_id: int):
    from datetime import datetime
    db.update_application(app_id, status="submitted", reason="marked applied manually",
                          finished_at=datetime.utcnow().isoformat(timespec="seconds"))
    return _partial(request, "partials/application_card.html", **_app_ctx(app_id))


@app.get("/screenshots/{name}")
def screenshot(name: str):
    path = (config.SCREENSHOTS_DIR / Path(name).name).resolve()
    if not path.is_file() or config.SCREENSHOTS_DIR.resolve() not in path.parents:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


# ---------- applications tab ----------
@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request):
    rows = db.list_applications()
    costs = {r["job_id"]: h.fmt_cost(db.cost_for_job(r["job_id"])) for r in rows}
    # Anything waiting on you or still running gets its full card here. Without this a paused application is
    # only reachable on the card that was swapped into the Jobs tab, so one reload stranded it with the
    # browser still open and no way to answer.
    live = [_app_ctx(r["id"]) for r in rows if r["status"] in ("needs_you", "running", "pending")]
    return _page(request, "applications", "tabs/applications.html", apps=rows, costs=costs, live=live)


# ---------- settings tab ----------
def _settings_ctx(msg: str | None = None, err: str | None = None) -> dict:
    return {
        "settings": h.settings_snapshot(),
        "providers": h.provider_infos(),
        "secrets": {n: h.secret_status(n) for n in h.SECRET_NAMES},
        "mail_state": h.mail_status(),
        "facts_text": h.read_text(config.FACTS_PATH),
        # The box stays the flat {question: answer} shape it has always been, even though the file now
        # stores provenance beside each one: it is for reading and fixing answers, not for editing records.
        "answers_text": json.dumps(config.load_answers(), indent=2, ensure_ascii=False, sort_keys=True),
        "answer_review": h.answer_rows(needs_review=True),
        "answer_rows": h.answer_rows(),
        "answer_quarantined": h.answer_rows(quarantined=True),
        "hours_choices": h.HOURS_CHOICES,
        "msg": msg, "err": err,
    }


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return _page(request, "settings", "tabs/settings.html", _settings_ctx())


CV_EXTS = {".pdf", ".doc", ".docx"}


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, provider: str = Form("anthropic"), model: str = Form(""),
                        locations: str = Form(""), hours_old: str = Form("72"), headless: str | None = Form(None),
                        cv: UploadFile | None = File(None), cv_path: str = Form("")):
    config.set_setting(config.SETTING_PROVIDER, provider.strip())
    config.set_setting(config.SETTING_MODEL, model.strip())
    config.set_setting(config.SETTING_LOCATIONS, locations.strip())
    config.set_setting(config.SETTING_HOURS_OLD, hours_old.strip() or "72")
    config.set_setting(config.SETTING_HEADLESS, "1" if headless else "0")
    msg, err = "Settings saved.", None
    if cv is not None and cv.filename:
        dest = config.CV_DIR / Path(cv.filename).name
        dest.write_bytes(await cv.read())
        config.set_setting(config.SETTING_CV_PATH, str(dest))
        msg += f" CV saved to {dest.name}."
    elif cv_path.strip():
        # Typed-path fallback: the file picker is unusable in some browsers (extensions hijack the click).
        src = Path(cv_path.strip().strip("\"'")).expanduser()
        if not src.is_file():
            err = f"No file at {src}"
        elif src.suffix.lower() not in CV_EXTS:
            err = f"CV must be {', '.join(sorted(CV_EXTS))} — got '{src.suffix or 'no extension'}'"
        else:
            dest = config.CV_DIR / src.name
            shutil.copyfile(src, dest)
            config.set_setting(config.SETTING_CV_PATH, str(dest))
            msg += f" CV copied from {src}."
    return _page(request, "settings", "tabs/settings.html", _settings_ctx(msg=msg, err=err))


@app.post("/settings/secret", response_class=HTMLResponse)
def settings_secret(request: Request, name: str = Form(...), value: str = Form("")):
    if name not in h.SECRET_NAMES:
        return _page(request, "settings", "tabs/settings.html", _settings_ctx(err=f"unknown secret {name}"))
    if not value:
        return _page(request, "settings", "tabs/settings.html", _settings_ctx(err="empty value ignored"))
    try:
        config.set_secret(name, value)
        msg, err = f"{name} stored in keychain.", None
    except Exception as e:  # noqa: BLE001 — keyring backend missing / locked
        msg, err = None, f"Keychain unavailable ({e}). Export {name}=... in the shell that runs jobbot instead."
    return _page(request, "settings", "tabs/settings.html", _settings_ctx(msg=msg, err=err))


@app.post("/settings/facts", response_class=HTMLResponse)
def settings_facts(request: Request, facts: str = Form("")):
    try:
        parsed = yaml.safe_load(facts)
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("top level must be a mapping")
    except Exception as e:  # noqa: BLE001
        ctx = _settings_ctx(err=f"facts.yaml not saved: {e}")
        ctx["facts_text"] = facts
        return _page(request, "settings", "tabs/settings.html", ctx)
    config.FACTS_PATH.write_text(facts)
    # A password pasted in here would otherwise be sent to the model with every question.
    moved = config.migrate_secrets_from_facts()
    msg = "facts.yaml saved."
    if moved:
        msg += (f" {', '.join(moved)} was a credential, not a fact — it has been moved to the keychain "
                f"and removed from the file.")
    return _page(request, "settings", "tabs/settings.html", _settings_ctx(msg=msg))


@app.post("/settings/answers", response_class=HTMLResponse)
def settings_answers(request: Request, answers: str = Form("{}")):
    try:
        parsed = json.loads(answers or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("top level must be an object")
    except Exception as e:  # noqa: BLE001
        ctx = _settings_ctx(err=f"answers.json not saved: {e}")
        ctx["answers_text"] = answers
        return _page(request, "settings", "tabs/settings.html", ctx)
    from jobbot import answers as store
    # What they changed or added is their own answer; what they left alone keeps the provenance it had.
    # Quarantined records are not in the box, so being absent from it never deletes one.
    store.save(store.apply_text_edit(store.load(), {str(k): str(v) for k, v in parsed.items()}))
    return _page(request, "settings", "tabs/settings.html", _settings_ctx(msg="answers.json saved."))


@app.post("/settings/answers/review", response_class=HTMLResponse)
def settings_answer_review(request: Request, key: str = Form(""), action: str = Form("")):
    """Keep, quarantine, restore or delete one remembered answer.

    The key is a form field rather than part of the path: these are whole questions, spaces and all.
    """
    from jobbot import answers as store
    records = store.load()
    rec = records.get(key)
    if rec is None:
        return _page(request, "settings", "tabs/settings.html",
                     _settings_ctx(err="That answer is no longer in the list."))
    if action == "delete":
        del records[key]
        msg = "Answer deleted."
    elif action == "quarantine":
        rec.confidence, rec.note = "quarantined", "you set this aside"
        msg = "Answer set aside; it will not be used again."
    elif action in ("keep", "restore"):
        # Confirming it makes it theirs: that is what lifts it above a rule or anything the model wrote.
        rec.source, rec.confidence = "human", "confirmed"
        rec.confirmed_at, rec.note = store.now(), ""
        msg = "Answer confirmed."
    else:
        return _page(request, "settings", "tabs/settings.html", _settings_ctx(err=f"Unknown action {action!r}."))
    store.save(records)
    return _page(request, "settings", "tabs/settings.html", _settings_ctx(msg=msg))


@app.post("/settings/answers/purge", response_class=HTMLResponse)
def settings_answers_purge(request: Request):
    from jobbot import answers as store
    records = store.load()
    gone = [k for k, r in records.items() if r.confidence == "quarantined"]
    for k in gone:
        del records[k]
    store.save(records)
    return _page(request, "settings", "tabs/settings.html",
                 _settings_ctx(msg=f"{len(gone)} set-aside answer(s) deleted."))


# ---------- companies tab ----------
def _companies_ctx(msg: str | None = None, err: str | None = None) -> dict:
    companies, load_err = [], None
    try:
        from jobbot.discovery import list_companies
        companies = list(list_companies())
    except Exception as e:  # noqa: BLE001
        load_err = f"discovery package unavailable: {e}"
    return {"companies": companies, "ats_choices": h.COMPANY_ATS_CHOICES, "msg": msg, "err": err or load_err}


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request):
    return _page(request, "companies", "tabs/companies.html", _companies_ctx())


@app.post("/companies", response_class=HTMLResponse)
def companies_add(request: Request, name: str = Form(...), ats: str = Form("greenhouse"), slug: str = Form("")):
    try:
        from jobbot.discovery import add_company
        add_company(name.strip(), ats.strip(), slug.strip())
        ctx = _companies_ctx(msg=f"Added {name.strip()}.")
    except Exception as e:  # noqa: BLE001
        ctx = _companies_ctx(err=f"could not add company: {e}")
    return _page(request, "companies", "tabs/companies.html", ctx)
