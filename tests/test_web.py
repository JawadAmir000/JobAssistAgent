"""Web UI smoke tests (FastAPI TestClient against a temporary SQLite DB)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from jobbot import config, db
from jobbot.models import Job


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "CV_DIR", tmp_path / "cv")
    monkeypatch.setattr(config, "SCREENSHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(config, "FACTS_PATH", tmp_path / "facts.yaml")
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.json")
    for d in (config.CV_DIR, config.SCREENSHOTS_DIR):
        d.mkdir()
    db.init()
    from jobbot.web.app import app
    with TestClient(app) as c:
        yield c


def _snapshot(job_ids: list[str], query: str = "FDE", locations: str = "Australia",
              hours_old: int = 72, status: str = "done") -> int:
    search_id = db.create_search(query, locations, hours_old)
    db.record_search_jobs(search_id, job_ids)
    db.update_search(search_id, status=status, found=len(job_ids))
    return search_id


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Jobs" in r.text
    assert "Applications" in r.text
    assert "Settings" in r.text


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_cost_meter(client):
    r = client.get("/cost")
    assert r.status_code == 200
    assert "0 LLM calls" in r.text


def test_settings_update(client):
    r = client.post("/settings", data={
        "provider": "anthropic", "model": "claude-haiku-4-5", "locations": "Remote, Canada",
        "hours_old": "24", "headless": "1",
    })
    assert r.status_code == 200
    assert config.get_setting(config.SETTING_MODEL) == "claude-haiku-4-5"
    assert config.get_setting(config.SETTING_LOCATIONS) == "Remote, Canada"
    assert config.get_setting(config.SETTING_HOURS_OLD) == "24"
    assert config.get_setting(config.SETTING_HEADLESS) == "1"
    # page re-renders with the new values
    assert "Remote, Canada" in r.text

    r = client.get("/settings")
    assert r.status_code == 200
    assert "ANTHROPIC_API_KEY" in r.text


def test_settings_cv_upload(client):
    r = client.post("/settings", data={"provider": "anthropic"},
                    files={"cv": ("me.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 200
    path = config.get_setting(config.SETTING_CV_PATH)
    assert path.endswith("me.pdf")
    assert (config.CV_DIR / "me.pdf").read_bytes() == b"%PDF-1.4 fake"


def test_facts_and_answers_validation(client):
    r = client.post("/settings/facts", data={"facts": "identity:\n  first_name: Test\n"})
    assert r.status_code == 200 and "saved" in r.text
    assert config.load_facts()["identity"]["first_name"] == "Test"

    r = client.post("/settings/facts", data={"facts": "a: [unclosed"})
    assert "not saved" in r.text
    assert config.load_facts()["identity"]["first_name"] == "Test"

    r = client.post("/settings/answers", data={"answers": '{"are you legally authorised": "no"}'})
    assert "saved" in r.text
    assert config.load_answers() == {"are you legally authorised": "no"}
    r = client.post("/settings/answers", data={"answers": "[1,2]"})
    assert "not saved" in r.text


def test_jobs_render(client):
    db.upsert_jobs([
        Job(id="greenhouse:1", company="Acme", title="Forward Deployed Engineer", url="https://boards.greenhouse.io/acme/jobs/1",
            ats="greenhouse", location="Remote", remote=True, score=5, score_reason="great fit", posted_at="2026-09-10"),
        Job(id="lever:2", company="Globex", title="Solutions Engineer", url="https://jobs.lever.co/globex/2",
            ats="lever", source="jobspy", score=2),
    ])
    search_id = _snapshot(["greenhouse:1", "lever:2"])
    r = client.get(f"/jobs?search_id={search_id}")
    assert r.status_code == 200
    assert "Acme" in r.text and "Forward Deployed Engineer" in r.text
    assert "greenhouse" in r.text
    assert "/apply/greenhouse:1" in r.text
    assert "Globex" not in r.text  # score 2 hidden by default min_score=3

    r = client.get(f"/jobs?search_id={search_id}&low=1")
    assert "Globex" in r.text
    r = client.get(f"/jobs?search_id={search_id}&min_score=1&q=Glob")
    assert "Globex" in r.text and "Acme" not in r.text


def test_search_snapshot_is_not_reinterpreted_by_edited_form_filters(client):
    today = date.today().isoformat()
    db.upsert_jobs([
        Job(id="a:1", company="Maple", title="FDE", url="https://x.test/1",
            location="Toronto, Ontario, Canada", score=5, posted_at=today),
        Job(id="a:2", company="Bayside", title="FDE", url="https://x.test/2",
            location="San Francisco, CA", score=5, posted_at=today),
        Job(id="a:3", company="Marina", title="FDE", url="https://x.test/3",
            location="Singapore, Singapore", score=5, posted_at=today),
        Job(id="a:4", company="Stale Maple", title="FDE", url="https://x.test/4",
            location="Toronto, Ontario, Canada", score=5,
            posted_at=(date.today() - timedelta(days=40)).isoformat()),
    ])
    search_id = _snapshot(["a:1"], locations="Remote, Canada")
    r = client.get(f"/jobs?search_id={search_id}&locations=Singapore&hours_old=24")
    assert "Maple" in r.text
    assert "Bayside" not in r.text        # wrong country
    assert "Marina" not in r.text         # wrong country
    assert "Stale Maple" not in r.text    # posted 40 days ago
    assert "Remote, Canada" in r.text and "last 3 days" in r.text


def test_jobs_list_names_the_active_filter(client):
    db.upsert_jobs([Job(id="b:1", company="Maple", title="FDE", url="https://x.test/1",
                        location="Toronto, Ontario, Canada", score=5, posted_at=date.today().isoformat())])
    canada = _snapshot(["b:1"], locations="Remote, Canada")
    r = client.get(f"/jobs?search_id={canada}")
    assert "Remote, Canada" in r.text and "last 3 days" in r.text
    germany = _snapshot([], query="FDE", locations="Germany")
    r = client.get(f"/jobs?search_id={germany}")
    assert "matched nothing in Germany" in r.text


def test_search_result_sets_are_isolated(client):
    db.upsert_jobs([
        Job(id="s:au", company="Sydney AI", title="AI Engineer", url="https://x.test/au",
            location="Sydney, NSW, AU", score=4, posted_at="2026-09-18"),
        Job(id="s:ca", company="Toronto Systems", title="Platform Engineer", url="https://x.test/ca",
            location="Toronto, ON, Canada", score=5, posted_at="2026-09-18"),
    ])
    australia = db.create_search("AI Engineer", "Australia", 72)
    canada = db.create_search("Platform Engineer", "Canada", 72)
    db.record_search_jobs(australia, ["s:au"])
    db.record_search_jobs(canada, ["s:ca"])
    db.update_search(australia, status="done", found=1)
    db.update_search(canada, status="done", found=1)

    au = client.get(f"/jobs?search_id={australia}&min_score=1")
    ca = client.get(f"/jobs?search_id={canada}&min_score=1")

    assert "Sydney AI" in au.text and "Toronto Systems" not in au.text
    assert "Toronto Systems" in ca.text and "Sydney AI" not in ca.text
    assert "AI Engineer" in au.text and "Australia" in au.text


def test_empty_search_does_not_show_historical_jobs(client):
    db.upsert_jobs([Job(id="history:1", company="Old Result", title="FDE", url="https://x.test/old",
                        location="Sydney, NSW, AU", score=5, posted_at="2026-09-18")])
    search_id = db.create_search("Nonsense Role", "Australia", 72)
    db.update_search(search_id, status="done", found=0)

    r = client.get(f"/jobs?search_id={search_id}")

    assert "Old Result" not in r.text
    assert "matched nothing" in r.text


def test_search_warning_is_visible_with_results(client):
    db.upsert_jobs([Job(id="warn:1", company="Partial Result", title="AI Engineer",
                        url="https://x.test/partial", score=4, posted_at="2026-09-18")])
    search_id = db.create_search("AI Engineer", "Australia", 72)
    db.record_search_jobs(search_id, ["warn:1"])
    db.update_search(search_id, status="done", found=1,
                     warning="jobspy: 1/3 site attempts failed")

    r = client.get(f"/jobs?search_id={search_id}")

    assert "Partial Result" in r.text
    assert "Incomplete results" in r.text


def test_index_restores_latest_snapshot_search(client):
    older = db.create_search("Older", "Canada", 24)
    latest = db.create_search("Latest AI", "Australia", 168)
    db.update_search(older, status="done")
    db.update_search(latest, status="done")

    r = client.get("/")

    assert f'value="{latest}"' in r.text
    assert 'value="Latest AI"' in r.text
    assert 'value="Australia"' in r.text
    assert '<option value="168" selected>' in r.text


def test_start_search_clears_old_results_and_publishes_search_id(client, monkeypatch):
    monkeypatch.setattr("jobbot.web.app._spawn", lambda *args: None)

    r = client.post("/search", data={
        "title": "AI Engineer", "locations": "Australia", "hours_old": "72",
        "src_jobspy": "1",
    })

    assert 'id="active-search-id"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert 'id="job-list" hx-swap-oob="innerHTML"' in r.text
    assert "Searching" in r.text


def test_invalid_search_id_never_leaks_history(client):
    db.upsert_jobs([Job(id="private:history", company="Historical Job", title="FDE",
                        url="https://x.test/history", score=5)])

    empty = client.get("/jobs?search_id=")
    blank = client.get("/jobs?search_id=abc")
    missing = client.get("/jobs?search_id=999")

    assert empty.status_code == 200 and blank.status_code == 200 and missing.status_code == 200
    assert "Historical Job" not in empty.text
    assert "Historical Job" not in blank.text
    assert "Historical Job" not in missing.text


def test_missing_search_id_prompts_instead_of_showing_history(client):
    db.upsert_jobs([Job(id="history:none", company="Historical Job", title="FDE",
                        url="https://x.test/history", score=5)])

    r = client.get("/jobs")

    assert "Run a search above" in r.text
    assert "Historical Job" not in r.text


def test_score_filter_explains_hidden_search_results(client):
    db.upsert_jobs([Job(id="low:1", company="Low Match", title="Engineer",
                        url="https://x.test/low", score=2)])
    search_id = db.create_search("Engineer", "Australia", 72)
    db.record_search_jobs(search_id, ["low:1"])
    db.update_search(search_id, status="done", found=1)

    filtered = client.get(f"/jobs?search_id={search_id}&min_score=3")
    visible = client.get(f"/jobs?search_id={search_id}&low=1")

    assert "All 1 jobs from this search score below 3" in filtered.text
    assert "Low Match" not in filtered.text
    assert "Low Match" in visible.text


def test_search_list_reconciles_partial_and_hidden_counts(client):
    db.upsert_jobs([
        Job(id="count:high", company="High", title="Engineer", url="https://x.test/high", score=5),
        Job(id="count:low", company="Low", title="Engineer", url="https://x.test/low", score=2),
        Job(id="count:hidden", company="Hidden", title="Engineer", url="https://x.test/hidden", score=5),
    ])
    search_id = _snapshot(["count:high", "count:low", "count:hidden"], query="Engineer")
    db.set_job_hidden("count:hidden", True)

    r = client.get(f"/jobs?search_id={search_id}&min_score=3")

    assert "Showing 1 of 2" in r.text
    assert "1 hidden" in r.text


def test_running_search_has_a_searching_state(client):
    search_id = _snapshot([], query="Still Running", status="running")

    r = client.get(f"/jobs?search_id={search_id}")

    assert "Searching “Still Running”" in r.text


def test_legacy_only_cold_load_does_not_restore_history(client):
    with db.connect() as c:
        c.execute("INSERT INTO searches(query, locations, started_at, status, has_snapshot) "
                  "VALUES('Legacy', 'Canada', '2026-01-01', 'done', 0)")

    r = client.get("/")

    assert 'id="active-search-id" name="search_id" value=""' in r.text
    assert "Run a search above" in r.text


def test_startup_marks_orphaned_running_search_as_interrupted(client):
    search_id = _snapshot([], query="Interrupted", status="running")
    from jobbot.web.app import app

    with TestClient(app) as restarted:
        assert restarted.get("/health").status_code == 200

    search = db.get_search(search_id)
    assert search["status"] == "error"
    assert search["warning"] == "interrupted by a restart"


def test_failed_search_clears_searching_state_without_showing_history(client):
    db.upsert_jobs([Job(id="old:error", company="Old Result", title="FDE",
                        url="https://x.test/old", score=5)])
    search_id = db.create_search("Broken Search", "Australia", 72)
    db.update_search(search_id, status="error", warning="source crashed")

    status = client.get(f"/search/{search_id}/status")
    jobs = client.get(f"/jobs?search_id={search_id}")

    assert f"/jobs?min_score=3&search_id={search_id}" in status.text
    assert "Search failed" in jobs.text
    assert "Old Result" not in jobs.text


def test_hide_job(client):
    db.upsert_jobs([Job(id="x:1", company="Hidden Co", title="T", url="https://example.com", score=5)])
    search_id = _snapshot(["x:1"])
    assert client.post("/jobs/x:1/hide").status_code == 200
    r = client.get(f"/jobs?search_id={search_id}")
    assert "Hidden Co" not in r.text
    assert "All 1 jobs from this search are hidden" in r.text


def test_application_card_states(client):
    db.upsert_jobs([Job(id="x:2", company="Acme", title="T", url="https://example.com/apply", score=5)])
    search_id = _snapshot(["x:2"])
    app_id = db.create_application("x:2")
    db.update_application(app_id, status="needs_you", reason="captcha", pending_question="Visa status?",
                          pending_options='["Yes", "No"]')
    r = client.get(f"/application/{app_id}/card")
    assert r.status_code == 200
    assert "Visa status?" in r.text and "<option" in r.text and "captcha" in r.text

    db.update_application(app_id, status="manual", reason="no adapter")
    r = client.get(f"/application/{app_id}/card")
    assert "Open and apply manually" in r.text
    r = client.post(f"/application/{app_id}/mark-applied")
    assert "Submitted" in r.text and "0 LLM calls" in r.text
    assert db.get_application(app_id)["status"] == "submitted"

    # job card now shows the application status instead of an Apply button
    r = client.get(f"/jobs?search_id={search_id}")
    assert "/apply/x:2" not in r.text and "submitted" in r.text


def test_applications_tab(client):
    db.upsert_jobs([Job(id="x:3", company="Initech", title="FDE", url="https://example.com", ats="ashby")])
    db.create_application("x:3")
    r = client.get("/applications")
    assert r.status_code == 200
    assert "Initech" in r.text and "ashby" in r.text


def test_companies_tab(client):
    r = client.get("/companies")
    assert r.status_code == 200
    assert "Add company" in r.text


def test_htmx_tab_returns_fragment(client):
    r = client.get("/applications", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text and "Applications" in r.text


def test_answer_endpoint_ignores_an_application_that_is_not_waiting(client, monkeypatch):
    """A stray answer (a double-click, a stale card) must not re-run a finished application: the fresh-run
    path would fill the form again and submit a second time."""
    import jobbot.web.app as webapp

    db.upsert_jobs([Job(id="x:4", company="Acme", title="T", url="https://example.com/apply",
                        ats="greenhouse", score=5)])
    app_id = db.create_application("x:4")
    db.update_application(app_id, status="submitted", pending_question="", pending_options="")
    monkeypatch.setattr(webapp, "_spawn", lambda fn, *a: pytest.fail("must not resume a finished application"))

    r = client.post(f"/application/{app_id}/answer", data={"answer": "anything"})
    assert r.status_code == 200
    assert db.get_application(app_id)["status"] == "submitted"


def test_answer_endpoint_persists_the_answer(client, monkeypatch):
    """Regression: the endpoint used to blank pending_question before the runner thread read it back, so
    resolver.learn() never ran and the same question was asked again on every pass."""
    import jobbot.web.app as webapp
    from jobbot.apply import runner

    db.upsert_jobs([Job(id="x:3", company="Acme", title="T", url="https://example.com/apply",
                        ats="greenhouse", score=5)])
    app_id = db.create_application("x:3")
    db.update_application(app_id, status="needs_you", pending_question="Favourite framework?",
                          pending_options="[]")

    monkeypatch.setattr(webapp, "_spawn", lambda fn, *a: fn(*a))      # run the resume inline
    monkeypatch.setattr(runner, "_run_application", lambda _id: None)  # no browser in tests

    r = client.post(f"/application/{app_id}/answer", data={"answer": "FastAPI"})
    assert r.status_code == 200
    assert config.load_answers()["favourite framework"] == "FastAPI"


def test_account_password_is_a_storable_secret():
    """Employer signups run on a password jobbot generates (credentials.py). The Settings form still has to
    accept one, so a password set on another machine — or the old hand-set Workday one — can be pasted in."""
    from jobbot.web import helpers as h
    from jobbot import credentials
    assert credentials.SECRET_NAME in h.SECRET_NAMES
    assert h.SECRET_NOTES[credentials.SECRET_NAME], "the field must say it fills itself, or it reads as a gap"


def test_account_password_reports_set_when_it_lives_under_the_old_name(monkeypatch):
    """A machine upgraded from JOBBOT_WORKDAY_PASSWORD is using that password. Reporting 'not set' invites
    the user to type a new one, which would lock them out of the accounts already created with the old."""
    from jobbot.web import helpers as h
    from jobbot import credentials

    monkeypatch.setattr(credentials, "stored_password", lambda: "TheOneAlready!nUse9")
    state, note = h.secret_status(credentials.SECRET_NAME)
    assert state == "set" and note
