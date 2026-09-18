"""Discovery layer tests — no network."""
from __future__ import annotations

from datetime import date, timedelta
import sqlite3

import pytest

from jobbot.models import Job
from jobbot.discovery.ats_boards import (
    _parse_greenhouse, _parse_lever, _parse_ashby, title_matches, expand_query, html_to_text,
    fetch_all_boards,
)
from jobbot.discovery.merge import dedupe, norm_company, norm_title
from jobbot.discovery.scorer import rule_score, score_job, _parse_llm
from jobbot.discovery.jobspy_source import _rows_to_jobs, country_for, _scrape_one, _short, fetch_jobspy
from jobbot.discovery.location import (
    matches as loc_matches, countries_named, canonical, is_fresh, filter_jobs,
)


# ---------- synonym matcher ----------
class TestTitleMatch:
    def test_expands_forward_deployed(self):
        phrases = expand_query("Forward Deployed Engineer")
        for p in ("fde", "applied ai", "solutions engineer", "customer engineer",
                  "deployment engineer", "ai engineer", "implementation engineer"):
            assert p in phrases

    @pytest.mark.parametrize("title", [
        "Forward Deployed Engineer", "Forward-Deployed Software Engineer", "FDE, Enterprise",
        "Applied AI Engineer", "Solutions Engineer", "Customer Engineer, EMEA",
        "AI Engineer (Agents)", "Implementation Engineer",
    ])
    def test_matches(self, title):
        assert title_matches(title, "Forward Deployed Engineer")

    @pytest.mark.parametrize("title", ["Accountant", "Recruiter", "Office Manager", "Data Analyst"])
    def test_rejects(self, title):
        assert not title_matches(title, "Forward Deployed Engineer")

    def test_significant_word_fallback(self):
        # "deployed" is a significant word and appears in the title
        assert title_matches("Deployed Systems Lead", "forward deployed")
        # 'fde' must be a whole word, not a substring
        assert not title_matches("Chief Defender", "fde")

    def test_empty_query_matches_all(self):
        assert title_matches("Anything", "")


# ---------- dedupe ----------
def _job(company, title, source="ats_board", desc="", id_=None):
    return Job(id=id_ or f"{source}:{company}:{title}", company=company, title=title,
               url="https://x.test/" + title.replace(" ", "-"), source=source, description=desc)


class TestDedupe:
    def test_normalisation(self):
        assert norm_company("Anthropic, Inc.") == "anthropic"
        assert norm_company("Scale AI Ltd") == "scale ai"
        assert norm_title("  Forward-Deployed   Engineer!! ") == "forward deployed engineer"

    def test_prefers_ats_board(self):
        a = _job("Anthropic", "Forward Deployed Engineer", "jobspy", desc="x" * 500)
        b = _job("Anthropic Inc.", "Forward-Deployed Engineer", "ats_board", desc="short")
        out = dedupe([a, b])
        assert len(out) == 1 and out[0] is b

    def test_prefers_longer_description_among_equals(self):
        a = _job("Sierra", "Solutions Engineer", desc="short", id_="a")
        b = _job("Sierra", "Solutions Engineer", desc="much longer description", id_="b")
        out = dedupe([a, b])
        assert out[0] is b

    def test_keeps_distinct(self):
        out = dedupe([_job("A", "X"), _job("A", "Y"), _job("B", "X")])
        assert len(out) == 3


# ---------- scorer rules ----------
class TestScorer:
    @pytest.mark.parametrize("title,desc,remote,expected", [
        ("Forward Deployed Engineer", "", True, 5),
        ("Applied AI Engineer", "", True, 4),
        ("Software Engineer", "Build LLM-powered agent workflows with LangChain", True, 3),
        ("Engineering Manager, Platform", "", True, 1),
        ("Staff Software Engineer", "LLM stuff", True, 1),
        ("Recruiter", "", True, 1),
    ])
    def test_rules(self, title, desc, remote, expected):
        j = Job(id="t", company="C", title=title, url="u", description=desc, remote=remote)
        score, reason = rule_score(j)
        assert score == expected, reason

    def test_staff_fde_still_five(self):
        j = Job(id="t", company="C", title="Staff Forward Deployed Engineer", url="u")
        assert rule_score(j)[0] == 5

    def test_clearance_caps_at_two_when_not_remote(self):
        j = Job(id="t", company="C", title="Applied AI Engineer", url="u",
                description="Requires active security clearance. US citizen only.", remote=False)
        assert rule_score(j)[0] == 2
        j.remote = True
        assert rule_score(j)[0] == 4

    def test_llm_failure_falls_back_to_rules(self, monkeypatch):
        import jobbot.discovery.scorer as sc

        def boom(*a, **k):
            raise RuntimeError("not configured")
        monkeypatch.setattr(sc, "llm_score", boom)
        j = Job(id="t", company="C", title="Software Engineer", url="u", description="agentic llm")
        assert score_job(j, {}) == (3, "software engineer with LLM/agent keywords")

    def test_llm_result_used(self, monkeypatch):
        import jobbot.discovery.scorer as sc
        monkeypatch.setattr(sc, "llm_score", lambda job, facts: (4, "good fit"))
        j = Job(id="t", company="C", title="Software Engineer", url="u", description="agentic llm")
        assert score_job(j, {}) == (4, "llm: good fit")

    def test_parse_llm_defensive(self):
        assert _parse_llm("SCORE: 4\nREASON: strong match") == (4, "strong match")
        assert _parse_llm("score=2 reason: meh") == (2, "meh")
        assert _parse_llm("I think it's a 5") == (5, "llm")
        assert _parse_llm("no idea") is None
        assert _parse_llm("") is None


# ---------- ATS parsers ----------
GH = {"jobs": [{
    "id": 4011, "title": "Forward Deployed Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/4011",
    "location": {"name": "Remote - Canada"}, "updated_at": "2026-09-10T12:00:00-04:00",
    "content": "&lt;p&gt;Build &amp; ship &lt;b&gt;agents&lt;/b&gt;.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
}, {"id": 1, "title": "", "absolute_url": "x"}]}

LEVER = [{
    "id": "abc-123", "text": "Solutions Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc-123",
    "applyUrl": "https://jobs.lever.co/acme/abc-123/apply", "createdAt": 1757462400000,
    "categories": {"location": "Sydney", "team": "GTM"}, "descriptionPlain": "Work with customers on LLM deployments.",
    "workplaceType": "remote",
}]

ASHBY = {"jobs": [{
    "id": "9f1", "title": "Applied AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/9f1",
    "location": "London", "publishedAt": "2026-09-01T00:00:00.000Z", "isRemote": True,
    "descriptionHtml": "<p>Deploy <i>Claude</i> for enterprise.</p>",
}]}


class TestParsers:
    def test_html_to_text(self):
        assert html_to_text("<p>Hi &amp; bye</p><ul><li>a</li></ul>") == "Hi & bye\n\na"

    def test_greenhouse(self):
        jobs = _parse_greenhouse(GH, "acme", "Acme")
        assert len(jobs) == 1
        j = jobs[0]
        assert j.id == "greenhouse:acme:4011" and j.ats == "greenhouse" and j.source == "ats_board"
        assert j.company == "Acme" and j.location == "Remote - Canada" and j.remote
        assert j.posted_at == "2026-09-10" and j.external_id == "4011"
        assert j.url == "https://boards.greenhouse.io/acme/jobs/4011"
        assert "Build & ship agents." in j.description and "<" not in j.description

    def test_lever(self):
        (j,) = _parse_lever(LEVER, "acme")
        assert j.id == "lever:acme:abc-123" and j.ats == "lever" and j.company == "acme"
        assert j.url == "https://jobs.lever.co/acme/abc-123"  # hostedUrl, not applyUrl
        assert j.title == "Solutions Engineer" and j.location == "Sydney"
        assert j.posted_at == "2025-09-10" and j.remote  # ms epoch -> date; workplaceType remote
        assert j.description.startswith("Work with customers")

    def test_ashby(self):
        (j,) = _parse_ashby(ASHBY, "acme")
        assert j.id == "ashby:acme:9f1" and j.ats == "ashby"
        assert j.remote and j.location == "London" and j.posted_at == "2026-09-01"
        assert j.description == "Deploy Claude for enterprise."

    def test_fetch_all_boards_logs_failures(self, monkeypatch):
        import jobbot.discovery.ats_boards as ab

        def fake_gh(slug, company=None):
            if slug == "bad":
                raise ValueError("404")
            return _parse_greenhouse(GH, slug, company)
        monkeypatch.setitem(ab.FETCHERS, "greenhouse", fake_gh)
        logs: list[str] = []
        cos = [{"name": "Good", "ats": "greenhouse", "slug": "good"},
               {"name": "Bad", "ats": "greenhouse", "slug": "bad"},
               {"name": "Unknown", "ats": "workday", "slug": "x"}]
        jobs = fetch_all_boards(cos, "Forward Deployed Engineer", logs.append)
        assert [j.company for j in jobs] == ["Good"]
        assert any(l.startswith("Bad: ValueError") for l in logs)


# ---------- jobspy mapping ----------
class TestJobspyMapping:
    def test_rows(self):
        nan = float("nan")
        rows = [{"job_url": "https://www.linkedin.com/jobs/view/1", "job_url_direct": "https://boards.greenhouse.io/a/jobs/1",
                 "title": "AI Engineer", "company": "Acme", "location": "Toronto, ON", "is_remote": nan,
                 "description": nan, "date_posted": "2026-09-11", "id": "li-1"},
                {"job_url": nan, "title": "x"}]
        (j,) = _rows_to_jobs(rows)
        assert j.id.startswith("jobspy:") and len(j.id) == len("jobspy:") + 16
        assert j.ats == "greenhouse" and j.url.startswith("https://boards.greenhouse.io")
        assert j.source == "jobspy" and j.description == "" and not j.remote
        assert j.posted_at == "2026-09-11" and j.external_id == "li-1"

    def test_country_map(self):
        assert country_for("Canada") == "canada"
        assert country_for("United Kingdom") == "uk"
        assert country_for("Toronto, Canada") == "canada"
        assert country_for("Mars") == "worldwide"


# ---------- companies.yaml ----------
def test_companies_yaml_loads():
    from jobbot.discovery import list_companies
    cos = list_companies()
    assert len(cos) >= 60
    assert all(c["ats"] in ("greenhouse", "lever", "ashby") for c in cos)
    assert {"name": "Anthropic", "ats": "greenhouse", "slug": "anthropic", "verified": True} in cos


# ---------- location filter ----------
class TestLocationMatch:
    WANTED = ["Remote", "Canada", "Australia", "United Kingdom", "United Arab Emirates", "Singapore"]

    @pytest.mark.parametrize("loc,remote", [
        ("Singapore, Singapore", False), ("Dubai, Dubai, United Arab Emirates", False),
        ("London, United Kingdom", False), ("London", False), ("London, UK", True),
        ("Toronto, Ontario, Canada", False), ("Sydney, NSW, AU", False), ("Vancouver, BC", False),
        ("Montréal, QC, CA", False), ("St. John's, NL, CA", True), ("Distributed", False),
        ("", True), ("Remote", True), ("Remote, Canada", True), ("Hybrid - London, Berlin", False),
    ])
    def test_keeps(self, loc, remote):
        assert loc_matches(loc, remote, self.WANTED)

    @pytest.mark.parametrize("loc,remote", [
        ("San Francisco, CA", False), ("New York, NY", False), ("Seattle, WA", False),
        ("Tokyo, Japan", False), ("Paris, France", False), ("Bengaluru, Karnataka, India", False),
        ("Washington, D.C.", False), ("Costa Mesa, California, United States", False),
        ("San Francisco, CA | New York City, NY", False), ("Cork, Ireland; Dublin, Ireland", False),
        ("Hybrid", False), ("", False),
    ])
    def test_drops(self, loc, remote):
        assert not loc_matches(loc, remote, self.WANTED)

    def test_remote_pinned_to_an_unwanted_country_is_dropped(self):
        # "Remote" in the wanted list must not smuggle in a US-only remote job.
        assert not loc_matches("Remote - US", True, ["Remote", "Canada"])
        assert loc_matches("Remote - Canada", True, ["Remote", "Canada"])
        assert loc_matches("Remote - US", True, ["Remote", "United States"])

    def test_state_code_beats_country_code_mid_string(self):
        # "CA" is California in a two-part location and Canada as the trailing field of a three-part one.
        assert countries_named("San Francisco, CA") == {"united states"}
        assert "canada" in countries_named("Ottawa, ON, CA")

    def test_empty_wanted_matches_everything(self):
        assert loc_matches("Anywhere", False, [])
        assert loc_matches("Anywhere", False, None)

    def test_unknown_term_falls_back_to_substring(self):
        assert loc_matches("Cairo, EMEA hub", False, ["EMEA"])
        assert not loc_matches("Tokyo, Japan", False, ["EMEA"])

    def test_country_for_uses_the_same_table(self):
        assert country_for("Dubai") == "united arab emirates"
        assert country_for("Toronto") == "canada"
        assert country_for("Atlantis") == "worldwide"

    @pytest.mark.parametrize("location,expected", [
        ("Waterloo, NSW, AU", {"australia"}),
        ("London, ON, Canada", {"canada"}),
        ("Birmingham, AL", {"united states"}),
        ("Perth, Scotland", {"united kingdom"}),
        ("Toronto, CA", {"canada"}),
        ("Berlin, DE", {"germany"}),
        ("Bangalore, IN", {"india"}),
        ("San Francisco, CA", {"united states"}),
        ("Toronto, Ontario", {"canada"}),
        ("Victoria, BC", {"canada"}),
        ("Sydney, NSW, Australia", {"australia"}),
        ("London, UK / Bangalore", {"united kingdom", "india"}),
        ("San Francisco, CA, Toronto, ON, London, UK",
         {"united states", "canada", "united kingdom"}),
        ("Indianapolis, IN", {"united states"}),
        ("Amsterdam, NL", {"netherlands"}),
        ("Washington, D.C.", {"united states"}),
    ])
    def test_explicit_country_markers_beat_ambiguous_city_names(self, location, expected):
        assert countries_named(location) == expected

    def test_australian_waterloo_does_not_match_canada(self):
        assert loc_matches("Waterloo, NSW, AU", False, ["Australia"])
        assert not loc_matches("Waterloo, NSW, AU", False, ["Canada"])

    def test_country_input_codes_keep_their_existing_meaning(self):
        assert canonical("NL") == "netherlands"
        assert canonical("UK") == "united kingdom"


class TestFreshness:
    def test_posted_at_wins(self):
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=30)).isoformat()
        assert is_fresh(today, None, 3)
        assert not is_fresh(old, today, 3)          # found today, but posted a month ago

    def test_falls_back_to_found_at(self):
        assert is_fresh(None, date.today().isoformat(), 3)
        assert not is_fresh(None, (date.today() - timedelta(days=9)).isoformat(), 3)

    def test_no_date_is_not_recent(self):
        assert not is_fresh(None, None, 3)
        assert is_fresh(None, None, None)           # no filter asked for

    def test_filter_jobs_counts_both_reasons(self):
        today = date.today().isoformat()
        jobs = [
            Job(id="a", company="A", title="T", url="u", location="Toronto, ON", posted_at=today),
            Job(id="b", company="B", title="T", url="u", location="Austin, TX", posted_at=today),
            Job(id="c", company="C", title="T", url="u", location="Toronto, ON",
                posted_at=(date.today() - timedelta(days=40)).isoformat()),
        ]
        kept, dropped = filter_jobs(jobs, ["Canada"], 3)
        assert [j.id for j in kept] == ["a"]
        assert dropped == {"location": 1, "stale": 1}


# ---------- run_search applies the criteria before storing ----------
def test_run_search_stores_only_what_was_asked_for(tmp_path, monkeypatch):
    """The whole point of the filter: an out-of-area or stale posting never reaches the db, so it is
    never scored and never shows up in the Jobs tab."""
    from jobbot import config, db
    import jobbot.discovery as disco
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "COMPANIES_PATH", tmp_path / "companies.yaml")
    db.init()

    today = date.today().isoformat()
    stale = (date.today() - timedelta(days=30)).isoformat()
    found = [
        Job(id="k:1", company="Maple", title="FDE", url="u1", location="Toronto, ON", posted_at=today),
        Job(id="k:2", company="Bay", title="FDE", url="u2", location="San Francisco, CA", posted_at=today),
        Job(id="k:3", company="Old", title="FDE", url="u3", location="Toronto, ON", posted_at=stale),
    ]
    monkeypatch.setattr(disco, "_run_ats", lambda q, log: found)
    monkeypatch.setattr(disco, "_run_jobspy", lambda q, locs, h, log, problems=None: [])
    monkeypatch.setattr("jobbot.discovery.scorer.score_all", lambda jobs, facts, log: {j.id: (5, "") for j in jobs})
    monkeypatch.setattr(config, "load_facts", lambda: {})

    sid = disco.run_search("FDE", locations=["Canada"], hours_old=72)
    row = db.get_search(sid)
    assert row["status"] == "done", row["log"]
    assert {j["id"] for j in db.list_jobs(min_score=1)} == {"k:1"}
    assert {j["id"] for j in db.list_jobs(min_score=1, search_id=sid)} == {"k:1"}
    assert "1 out of area, 1 too old" in row["log"]


def test_search_membership_keeps_known_jobs_and_orders_newest_first(tmp_path, monkeypatch):
    from jobbot import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    older = Job(id="m:old", company="Old", title="FDE", url="u1", score=5,
                posted_at="2026-09-10")
    newer = Job(id="m:new", company="New", title="FDE", url="u2", score=2,
                posted_at="2026-09-12")
    db.upsert_jobs([older, newer])

    first = db.create_search("FDE", "Australia", 72)
    db.record_search_jobs(first, [older.id, newer.id])
    second = db.create_search("FDE", "Australia", 72)
    db.record_search_jobs(second, [newer.id])  # already known must still belong to this search

    assert [j["id"] for j in db.list_jobs(min_score=1, search_id=first)] == [newer.id, older.id]
    assert [j["id"] for j in db.list_jobs(min_score=1, search_id=second)] == [newer.id]
    assert db.count_search_jobs(first) == 2
    assert db.latest_snapshot_search()["id"] == second


def test_init_migrates_legacy_searches_idempotently(tmp_path, monkeypatch):
    from jobbot import db
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as c:
        c.execute("""CREATE TABLE searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, locations TEXT DEFAULT '',
            found INTEGER DEFAULT 0, new INTEGER DEFAULT 0, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT DEFAULT 'running', log TEXT DEFAULT '')""")
        c.execute("INSERT INTO searches(query, locations, started_at) VALUES('old', 'Canada', '2026-01-01')")
    monkeypatch.setattr(db, "DB_PATH", path)

    db.init()
    db.init()

    with db.connect() as c:
        columns = {r["name"] for r in c.execute("PRAGMA table_info(searches)")}
        old = c.execute("SELECT * FROM searches WHERE query='old'").fetchone()
    assert {"hours_old", "has_snapshot", "warning"} <= columns
    assert old["has_snapshot"] == 0
    assert db.latest_snapshot_search() is None


# ---------- one blocked site must not cost us the others ----------
class TestJobspySiteIsolation:
    def _df(self, n):
        import pandas as pd
        return pd.DataFrame([{"job_url": f"https://x.test/{i}", "title": "AI Engineer",
                              "company": "Acme", "location": "Sydney, NSW, AU"} for i in range(n)])

    def test_google_429_keeps_indeed_and_linkedin(self):
        """The real failure from the Australia search: Google rate-limited, and because all three
        sites went out in one jobspy call its re-raise threw away the other two sites' results."""
        seen, logs = [], []

        def scrape(**kw):
            site = kw["site_name"][0]
            seen.append(site)
            if site == "google":
                raise RuntimeError("too many 429 error responses")
            return self._df(2)

        rows = _scrape_one(scrape, "AI Engineer", "Australia", 72, logs.append)
        assert sorted(seen) == ["google", "indeed", "linkedin"]   # every site still attempted
        assert len(rows) == 4                                     # and the working two survived
        assert any("google" in m and "429" in m for m in logs)

    def test_one_site_per_call(self):
        calls = []

        def scrape(**kw):
            calls.append(kw["site_name"])
            return None

        _scrape_one(scrape, "q", "Australia", 72)
        assert calls == [["indeed"], ["linkedin"], ["google"]]

    def test_failures_are_reported_not_read_as_no_jobs(self, monkeypatch):
        logs, problems = [], []
        monkeypatch.setitem(__import__("sys").modules, "jobspy",
                            type("M", (), {"scrape_jobs": staticmethod(
                                lambda **kw: (_ for _ in ()).throw(RuntimeError("blocked")))})())
        out = fetch_jobspy("q", ["Australia"], 72, logs.append, problems)
        assert out == []
        assert any("site attempts failed" in m and "incomplete" in m for m in logs)
        assert problems and "site attempts failed" in problems[0]

    def test_long_errors_are_trimmed(self):
        msg = _short(RuntimeError("x" * 500))
        assert msg.startswith("RuntimeError: ") and len(msg) < 200 and msg.endswith("…")

    def test_location_level_failure_marks_results_incomplete(self, monkeypatch):
        import jobbot.discovery.jobspy_source as js
        monkeypatch.setitem(__import__("sys").modules, "jobspy",
                            type("M", (), {"scrape_jobs": staticmethod(lambda **kw: None)})())
        monkeypatch.setattr(js, "_scrape_one",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("blocked location")))
        logs, problems = [], []

        assert js.fetch_jobspy("q", ["Australia"], 72, logs.append, problems) == []
        assert any("jobspy[Australia] failed" in problem for problem in problems)


def test_run_search_records_a_source_warning(tmp_path, monkeypatch):
    from jobbot import config, db
    import jobbot.discovery as disco
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "COMPANIES_PATH", tmp_path / "companies.yaml")
    monkeypatch.setattr(disco, "_run_ats", lambda q, log: (_ for _ in ()).throw(RuntimeError("offline")))

    sid = disco.run_search("FDE", locations=["Australia"], hours_old=72, sources=("ats",))

    search = db.get_search(sid)
    assert search["status"] == "done"
    assert "ats failed" in search["warning"]
