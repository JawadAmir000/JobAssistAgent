"""Offline tests for the apply layer: resolver, cache, protected questions, detect_ats. No browser, no network."""
from __future__ import annotations

import json

import pytest

import jobbot.apply  # noqa: F401  - package import must succeed without browsers
from jobbot import config
from jobbot.apply.base import NeedsHuman, detect_ats, get_adapter_for
from jobbot.apply.resolver import Resolver, normalize_question

FACTS = {
    "identity": {"first_name": "Jawad", "last_name": "Amir", "full_name": "Jawad Amir",
                 "email": "j@example.com", "phone": "+880123", "location": "Dhaka, Bangladesh",
                 "country": "Bangladesh", "linkedin": "https://linkedin.com/in/x", "github": "https://github.com/x",
                 "portfolio": "https://x.dev"},
    "work": {"current_title": "Forward Deployed Engineer", "current_company": "Anlytic", "years_experience": 6,
             "years_llm_experience": 3, "notice_period": "", "open_to_relocation": True, "open_to_remote": True},
    "authorization": {"requires_sponsorship": True, "authorized_countries": ["Bangladesh"], "citizenship": ""},
    "preferences": {"salary_currency": "USD", "salary_min": "", "start_date": ""},
    "skills": ["Python", "TypeScript", "AWS", "LLM applications"],
    "summary": "FDE.",
}
JOB = {"id": "greenhouse:1", "company": "Acme", "title": "AI Engineer"}


@pytest.fixture(autouse=True)
def _isolated_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.json")
    yield


@pytest.fixture
def no_llm(monkeypatch):
    import jobbot.llm
    import jobbot.llm.base

    def boom(*a, **k):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr(jobbot.llm, "complete", boom)
    monkeypatch.setattr(jobbot.llm.base, "complete", boom)


def mk(llm=False, answers=None):
    return Resolver(FACTS, answers or {}, JOB, llm_enabled=llm)


# ---------- normalize ----------
def test_normalize_question():
    assert normalize_question("  First Name *") == "first name"
    assert normalize_question("Phone (required)") == "phone"
    assert normalize_question("LinkedIn Profile (optional)") == "linkedin profile"
    assert normalize_question("Do   you require   sponsorship?") == "do you require sponsorship"
    assert normalize_question("E-mail Address") == "e mail address"
    assert normalize_question("") == ""


# ---------- rule answers ----------
def test_identity_rules(no_llm):
    r = mk()
    assert r.answer("First Name *") == "Jawad"
    assert r.answer("Last name") == "Amir"
    assert r.answer("Full Name") == "Jawad Amir"
    assert r.answer("Email Address") == "j@example.com"
    assert r.answer("Phone") == "+880123"
    assert r.answer("LinkedIn Profile") == "https://linkedin.com/in/x"
    assert r.answer("GitHub URL") == "https://github.com/x"
    assert r.answer("Current company") == "Anlytic"
    assert r.answer("Country") == "Bangladesh"
    assert r.answer("How did you hear about us?") == "LinkedIn"
    assert r.answer("What are your salary expectations?") == "Negotiable"


def test_sponsorship_with_options(no_llm):
    r = mk()
    assert r.answer("Will you now or in the future require sponsorship for employment visa status?",
                    ["Yes", "No"]) == "Yes"
    assert r.answer("Do you require visa sponsorship?", ["no", "YES"]) == "YES"


def test_authorized_country_from_facts_only(no_llm):
    r = mk()
    assert r.answer("Are you legally authorized to work in the United States?", ["Yes", "No"]) == "No"
    assert r.answer("Are you authorized to work in Bangladesh?", ["Yes", "No"]) == "Yes"
    assert r.answer("Are you authorized to work in the US?") == "No"


def test_years_of_experience(no_llm):
    r = mk()
    assert r.answer("How many years of experience do you have?") == "6"
    assert r.answer("Years of experience with Python") == "6"
    assert r.answer("How many years of experience do you have with LLMs?") == "3"
    assert r.answer("Years of experience with generative AI applications") == "3"


def test_eeo_always_asks_and_never_declines_on_your_behalf(no_llm):
    """Auto-picking "I prefer not to answer" is still answering for the candidate: it puts a refusal on a
    real application they never chose. Every EEO question is asked once and cached like any other."""
    r = mk()
    for q, opts in (("Gender", ["Male", "Female", "Non-binary", "I don't wish to answer"]),
                    ("Veteran Status", ["I am a veteran", "I am not a veteran", "Decline to self identify"]),
                    ("Race/Ethnicity", ["Asian", "White", "Prefer not to say"]),
                    ("Disability Status", ["Yes", "No", "I do not want to answer"])):
        with pytest.raises(NeedsHuman) as ei:
            r.answer(q, opts, "select")
        assert ei.value.options == opts, q

    # what the candidate chose themselves is honoured, including a decline
    chosen = mk(answers={"gender": "I don't wish to answer"})
    assert chosen.answer("Gender", ["Male", "Female", "I don't wish to answer"], "select") == "I don't wish to answer"


EEO_FACTS = {**FACTS, "eeo": {"gender": "Male", "disability_status": "No", "veteran_status": "No",
                              "race_ethnicity": "Asian"}}


def test_eeo_answers_come_from_facts_in_every_wording(no_llm):
    """These are legal self-identifications. Every board words the question and the options differently, so
    the stated answer has to survive the translation — "No" must become "I am not a protected veteran", not
    a shrug that stops the run, and never the affirmative option."""
    cases = [
        ("Gender", ["Male", "Female", "Non-binary", "I don't wish to answer"], "Male"),
        ("What is your gender identity?", ["Man", "Woman", "Decline to self-identify"], "Man"),
        ("Are you a protected veteran?",
         ["I identify as one or more classifications of protected veteran", "I am not a protected veteran",
          "I don't wish to answer"], "I am not a protected veteran"),
        ("Veteran Status", ["Yes", "No", "Decline to self identify"], "No"),
        ("Disability Status",
         ["Yes, I have a disability", "No, I don't have a disability", "I do not want to answer"],
         "No, I don't have a disability"),
        ("Do you have a disability?", ["Yes", "No", "Prefer not to answer"], "No"),
        ("Race / Ethnicity", ["Asian", "White", "Hispanic or Latino", "Prefer not to say"], "Asian"),
        ("Are you Hispanic or Latino?", ["Yes", "No", "Decline"], "No"),
    ]
    for q, opts, want in cases:
        r = Resolver(EEO_FACTS, {}, JOB, llm_enabled=False)
        assert r.answer(q, opts, "select") == want, q


def test_eeo_never_picks_the_decline_option_by_itself(no_llm):
    """A decline is a choice; only the candidate makes it. When their stated answer cannot be matched to any
    option the run stops rather than quietly declining for them."""
    r = Resolver(EEO_FACTS, {}, JOB, llm_enabled=False)
    with pytest.raises(NeedsHuman):
        r.answer("Gender", ["Prefer not to say", "Something else entirely"], "select")


def test_an_unstated_eeo_answer_still_asks(no_llm):
    blank = {**FACTS, "eeo": {"gender": "", "disability_status": "", "veteran_status": "", "race_ethnicity": ""}}
    r = Resolver(blank, {}, JOB, llm_enabled=False)
    with pytest.raises(NeedsHuman):
        r.answer("Gender", ["Male", "Female", "I don't wish to answer"], "select")


def test_eeo_without_decline_option_needs_human(no_llm):
    r = mk(llm=True)
    with pytest.raises(NeedsHuman) as ei:
        r.answer("Gender", ["Male", "Female"], "select")
    assert ei.value.question == "Gender"
    assert ei.value.options == ["Male", "Female"]
    assert ei.value.kind == "select"


def test_relocation_remote_yes_no(no_llm):
    r = mk()
    assert r.answer("Are you open to relocation?", ["Yes", "No"]) == "Yes"
    assert r.answer("Are you comfortable working remotely?") == "Yes"


# ---------- cache ----------
def test_learn_persists_and_fuzzy_hit(no_llm):
    r = mk()
    r.learn("Why do you want to work at Acme?", "Because of the mission.")
    saved = json.loads(config.ANSWERS_PATH.read_text())
    assert saved["why do you want to work at acme"] == "Because of the mission."
    # exact hit
    assert r.answer("Why do you want to work at Acme? *") == "Because of the mission."
    # fuzzy hit (token_set_ratio >= 92)
    assert r.answer("Why do you want to work at Acme") == "Because of the mission."
    # a fresh resolver loaded with the saved dict also hits
    r2 = mk(answers=config.load_answers())
    assert r2.answer("Why do you want to work at Acme?") == "Because of the mission."


def test_cache_maps_to_option_text(no_llm):
    r = mk(answers={"preferred work type": "remote"})
    assert r.answer("Preferred work type", ["Onsite", "Remote", "Hybrid"], "select") == "Remote"


def test_rule_answer_is_cached(no_llm):
    r = mk()
    r.answer("How did you hear about us?")
    assert config.load_answers()["how did you hear about us"] == "LinkedIn"


def test_unknown_needs_human_without_llm(no_llm):
    r = mk(llm=False)
    with pytest.raises(NeedsHuman) as ei:
        r.answer("Describe your favourite project", None, "textarea")
    assert ei.value.question == "Describe your favourite project"
    assert ei.value.kind == "textarea"


# ---------- protected ----------
def test_protected_keyword_set():
    assert Resolver.is_protected("Do you require visa sponsorship?")
    assert Resolver.is_protected("What is your salary expectation?")
    assert Resolver.is_protected("Do you hold a security clearance?")
    assert Resolver.is_protected("What is your citizenship status?")
    assert Resolver.is_protected("Veteran status")
    assert not Resolver.is_protected("Why do you want to work here?")
    assert not Resolver.is_protected("Years of experience with Python")


def test_protected_questions_never_hit_llm(no_llm):
    r = mk(llm=True)
    # clearance has no rule -> must go to NeedsHuman, never LLM
    with pytest.raises(NeedsHuman):
        r.answer("Do you currently hold an active security clearance?", ["Yes", "No"])
    # citizenship blank in facts -> NeedsHuman
    with pytest.raises(NeedsHuman):
        r.answer("What is your citizenship?")
    # sponsorship answer not among options -> NeedsHuman, not LLM
    with pytest.raises(NeedsHuman):
        r.answer("Will you require sponsorship?", ["Maybe", "Later"])


def test_llm_fallback_used_for_unprotected(monkeypatch):
    import jobbot.llm
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return "Because I like agents."
    monkeypatch.setattr(jobbot.llm, "complete", fake)
    r = mk(llm=True)
    assert r.answer("Why are you interested in this role?") == "Because I like agents."
    assert calls and calls[0][1]["purpose"] == "answer" and calls[0][1]["job_id"] == "greenhouse:1"
    assert "Facts:" in calls[0][0] and "Acme" in calls[0][0]
    # cached now -> second call does not hit the LLM
    calls.clear()
    assert r.answer("Why are you interested in this role?") == "Because I like agents."
    assert not calls


def test_llm_unknown_or_bad_option_needs_human(monkeypatch):
    import jobbot.llm
    monkeypatch.setattr(jobbot.llm, "complete", lambda *a, **k: "UNKNOWN")
    with pytest.raises(NeedsHuman):
        mk(llm=True).answer("What is your favourite colour?")
    monkeypatch.setattr(jobbot.llm, "complete", lambda *a, **k: "Purple")
    with pytest.raises(NeedsHuman):
        mk(llm=True).answer("Pick one", ["Red", "Blue"], "select")


def test_llm_not_configured_is_needs_human(monkeypatch):
    import jobbot.llm

    def raise_rt(*a, **k):
        raise RuntimeError("LLM provider 'anthropic' not configured")
    monkeypatch.setattr(jobbot.llm, "complete", raise_rt)
    with pytest.raises(NeedsHuman):
        mk(llm=True).answer("Tell us about yourself")


# ---------- detect_ats / registry ----------
def test_detect_ats():
    assert detect_ats("https://job-boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/abc/apply") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/abc/application") == "ashby"
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/x") == "workday"
    assert detect_ats("https://www.linkedin.com/jobs/view/1") == "linkedin"
    assert detect_ats("https://example.com/careers") == "other"


def test_adapters_registered():
    for ats in ("greenhouse", "lever", "ashby", "workday", "linkedin"):
        a = get_adapter_for(ats)
        assert a is not None and a.ats == ats


def test_plain_form_boards_all_resolve_to_the_generic_walker():
    """One walker, many names: the boards differ only in hostname, so each keeps its own ats label for the
    logs while sharing the implementation."""
    from jobbot.apply.generic import GenericFormAdapter
    for ats in ("zoho", "workable", "recruitee", "teamtailor", "jazzhr", "bamboohr",
                "smartrecruiters", "pageup", "other", "generic"):
        a = get_adapter_for(ats)
        assert isinstance(a, GenericFormAdapter) and a.ats == ats


def test_indeed_is_the_only_ats_left_without_an_adapter():
    """The guard on this whole exercise: if this starts failing because something else lost its adapter,
    jobs are silently going to 'manual' again."""
    from jobbot.apply.base import _HOSTS
    missing = sorted({ats for _h, ats in _HOSTS if get_adapter_for(ats) is None})
    assert missing == ["indeed"]


def test_fuzzy_does_not_match_subset_question(no_llm):
    r = mk(answers={"how many years of experience do you have": "6"})
    # a longer, more specific question must not be served the generic cached answer
    assert r.answer("How many years of experience do you have with LLMs?") == "3"


# ---------- runner (no browser) ----------
@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from jobbot import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    return db


def _mk_job(db, ats, url):
    from jobbot.models import Job
    j = Job(id=f"{ats}:1", company="Acme", title="AI Engineer", url=url, ats=ats)
    db.upsert_jobs([j])
    return db.create_application(j.id)


def test_runner_requires_cv(tmp_db, monkeypatch):
    from jobbot.apply import runner
    monkeypatch.setattr(config, "get_setting", lambda k, d=None: "" if k == config.SETTING_CV_PATH else d)
    app_id = _mk_job(tmp_db, "greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1")
    runner.run_application(app_id)
    row = tmp_db.get_application(app_id)
    assert row["status"] == "failed" and "Upload a CV" in row["reason"]
    assert app_id not in runner._LIVE


def test_runner_manual_when_no_adapter(tmp_db, tmp_path, monkeypatch):
    from jobbot.apply import runner
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    monkeypatch.setattr(config, "get_setting", lambda k, d=None: str(cv) if k == config.SETTING_CV_PATH else d)
    app_id = _mk_job(tmp_db, "indeed", "https://www.indeed.com/viewjob?jk=1")
    runner.run_application(app_id)
    row = tmp_db.get_application(app_id)
    assert row["status"] == "manual" and row["reason"] == "No adapter for indeed" and row["finished_at"]


def test_resume_without_live_browser_persists_answer(tmp_db, monkeypatch):
    """Server restarted: the answer is cached, then a fresh run starts (which fails fast here: no CV)."""
    from jobbot.apply import runner
    monkeypatch.setattr(config, "get_setting", lambda k, d=None: "" if k == config.SETTING_CV_PATH else d)
    app_id = _mk_job(tmp_db, "lever", "https://jobs.lever.co/acme/1/apply")
    tmp_db.update_application(app_id, status="needs_you", pending_question="Favourite framework?",
                              pending_options="[]")
    runner.resume_application(app_id, "FastAPI")
    assert config.load_answers()["favourite framework"] == "FastAPI"
    assert tmp_db.get_application(app_id)["status"] == "failed"


# ---------- linkedin adapter (offline, fake page) ----------
class _FakeLocator:
    def __init__(self, count=0, visible=False):
        self._count, self._visible = count, visible

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def wait_for(self, state="visible", timeout=0):
        if not self._visible:
            raise RuntimeError("not visible")


class _FakeContext:
    def __init__(self, cookies=()):
        self._cookies = list(cookies)

    def cookies(self):
        return self._cookies


class _FakePage:
    """Only the handful of methods the LinkedIn adapter touches. No cookies = signed out."""
    def __init__(self, tracking, wall=0, apply_visible=False, cookies=()):
        self.tracking, self.wall, self.apply_visible = tracking, wall, apply_visible
        self.url = "https://www.linkedin.com/jobs/view/1"
        self.context = _FakeContext(cookies)
        self.reloads = 0

    def is_closed(self):
        return False

    def reload(self, **_k):
        self.reloads += 1

    def wait_for_timeout(self, _ms):
        pass

    def eval_on_selector_all(self, _sel, _js):
        return self.tracking

    def get_by_role(self, _role, name=None):
        return _FakeLocator(1 if self.apply_visible else 0, self.apply_visible)

    def locator(self, _sel):
        return _FakeLocator(self.wall, False)


def _li_ctx(page):
    from jobbot.apply.base import ApplyContext
    return ApplyContext(job={"ats": "linkedin", "url": page.url, "company": "Acme"}, page=page, facts=FACTS,
                        cv_path="/tmp/cv.pdf", step=lambda s: None, answer=lambda *a, **k: "", screenshot=lambda l: "")


def test_linkedin_adapter_is_registered():
    assert get_adapter_for("linkedin") is not None


def test_linkedin_easy_apply_asks_the_user():
    page = _FakePage(["public_jobs_apply-link-simple_onsite"])
    with pytest.raises(NeedsHuman) as e:
        get_adapter_for("linkedin").apply(_li_ctx(page))
    assert "Easy Apply" in e.value.reason


def test_linkedin_signed_out_says_to_sign_in():
    page = _FakePage(["public_jobs_apply-link-offsite_contextual-sign-in-modal_join-link"], wall=1)
    with pytest.raises(NeedsHuman) as e:
        get_adapter_for("linkedin").apply(_li_ctx(page))
    assert "sign in" in e.value.reason.lower()


def test_linkedin_offsite_beats_onsite_marker():
    """A page carrying both markers is an offsite job, not Easy Apply."""
    from jobbot.apply.linkedin import LinkedInAdapter
    page = _FakePage(["public_jobs_apply-link-offsite_x", "public_jobs_apply-link-simple_onsite"])
    assert LinkedInAdapter._is_easy_apply(page) is False


# ---------- closed window is reported as such ----------
def test_closed_page_reports_closed_not_missing_form():
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyError

    class Closed:
        def is_closed(self):
            return True
    with pytest.raises(ApplyError) as e:
        c.require_open(Closed())
    assert "closed" in str(e.value).lower()


# ---------- answer quality: CV grounding, no placeholder answers ----------
def test_llm_prompt_is_grounded_in_the_cv(monkeypatch):
    import jobbot.llm
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"] = prompt
        return "Bedrock AgentCore and MCP."
    monkeypatch.setattr(jobbot.llm, "complete", fake)
    r = Resolver(FACTS, {}, JOB, llm_enabled=True, cv_text="Built multi-agent systems on Bedrock AgentCore.")
    assert r.answer("Which AI platforms have you shipped on?") == "Bedrock AgentCore and MCP."
    assert "Bedrock AgentCore" in seen["prompt"]           # the CV reached the model
    assert "Never invent" in seen["prompt"]                # and the truthfulness rule went with it


def test_contact_details_are_never_llm_guessed(no_llm):
    """An LLM must not invent a phone number onto a real application; with no fact it has to ask."""
    r = Resolver({"identity": {"first_name": "Jawad"}}, {}, JOB, llm_enabled=True)
    with pytest.raises(NeedsHuman):
        r.answer("Phone number")
    assert Resolver.is_never_guess("Mobile phone") and not Resolver.is_never_guess("Why this role?")


def test_placeholder_answers_ask_the_user_instead(monkeypatch):
    """No 'N/A' / 'I don't know' on a real application: if the model has nothing, the user is asked."""
    import jobbot.llm
    for placeholder in ("N/A", "n/a", "None", "Unknown", "I don't know", "Not applicable", "TBD", "skip it"):
        monkeypatch.setattr(jobbot.llm, "complete", lambda prompt, _p=placeholder, **kw: _p)
        r = Resolver(FACTS, {}, JOB, llm_enabled=True, cv_text="x")
        with pytest.raises(NeedsHuman):
            r.answer("If you answered Other above, please specify")
    # a real answer still goes straight through
    monkeypatch.setattr(jobbot.llm, "complete", lambda prompt, **kw: "Found the role on LinkedIn.")
    r = Resolver(FACTS, {}, JOB, llm_enabled=True, cv_text="x")
    assert r.answer("If you answered Other above, please specify") == "Found the role on LinkedIn."


def test_cached_placeholder_is_not_replayed(monkeypatch):
    """A bad answer cached by an earlier run must not be pasted onto every future application."""
    import jobbot.llm
    monkeypatch.setattr(jobbot.llm, "complete", lambda prompt, **kw: "UNKNOWN")
    r = Resolver(FACTS, {"describe your ideal team": "N/A"}, JOB, llm_enabled=True)
    with pytest.raises(NeedsHuman):
        r.answer("Describe your ideal team")


def test_technical_question_is_not_mistaken_for_protected():
    """Regression: 'age ' matched inside 'storage ', sending a data-architecture question to NeedsHuman."""
    q = ("Do you have hands-on experience owning end-to-end big data architectures in production, including "
         "ingestion, transformation, storage, orchestration, and downstream analytics or ML?")
    assert not Resolver.is_protected(q)
    for ok in ("What languages do you use?", "Describe your package management experience",
               "How do you manage stakeholders?", "Do you leverage CI?"):
        assert not Resolver.is_protected(ok), ok
    # and the real protected ones still match
    for bad in ("What is your age?", "Are you a veteran?", "Do you need visa sponsorship?",
                "What is your salary expectation?"):
        assert Resolver.is_protected(bad), bad


def test_years_for_unknown_skill_is_not_invented(no_llm):
    """Claiming the total years for a technology the CV never mentions is a false statement."""
    r = mk(llm=False)
    assert r.answer("How many years of experience do you have?") == "6"     # generic -> total
    assert r.answer("Years of experience with Python") == "6"               # a listed skill -> total
    with pytest.raises(NeedsHuman):
        r.answer("How many years of experience do you have with Apache Spark?")


def test_decline_option_detection_covers_real_ats_wordings():
    from jobbot.apply.resolver import _is_decline_option
    declines = ["I do not want to answer",            # federal disability form (CC-305)
                "I don't want to answer", "I don’t wish to answer", "Decline To Self Identify",
                "Prefer not to say", "I prefer not to answer", "I'd rather not say",
                "Choose not to disclose", "I do not wish to disclose"]
    for d in declines:
        assert _is_decline_option(d), d
    # real answers must not be mistaken for a decline
    for keep in ["Yes, I have a disability, or have had one in the past",
                 "No, I do not have a disability and have not had one in the past",
                 "I am not a veteran", "I am not Hispanic or Latino", "Asian", "Male", "Yes", "No"]:
        assert not _is_decline_option(keep), keep


# ---------- sponsorship scoped to the candidate's own country ----------
def test_sponsorship_in_home_country_answers_from_authorized_list(no_llm):
    facts = {**FACTS, "authorization": {"requires_sponsorship": True, "authorized_countries": ["Bangladesh"],
                                         "citizenship": ""}}
    r = Resolver(facts, {}, JOB)
    q = "Do you currently require visa sponsorship to work in the country you are based?"
    assert r.answer(q, ["Yes", "No"]) == "No"                       # authorised at home -> no sponsorship there
    assert r.answer("Will you require visa sponsorship for this role?", ["Yes", "No"]) == "Yes"  # abroad -> facts


def test_sponsorship_in_home_country_asks_when_facts_are_silent(no_llm):
    """requires_sponsorship=True is about moving abroad; it must never be pasted onto a home-country question."""
    facts = {**FACTS, "authorization": {"requires_sponsorship": True, "authorized_countries": [], "citizenship": ""}}
    r = Resolver(facts, {}, JOB, llm_enabled=True)
    with pytest.raises(NeedsHuman):
        r.answer("Do you currently require visa sponsorship to work in the country you are based?", ["Yes", "No"])


def test_ashby_adapter_registered_and_selectors_do_not_assume_a_form():
    import jobbot.apply.ashby as a
    assert get_adapter_for("ashby") is not None
    # Ashby renders no <form>; a 'form ' prefix anywhere in the field selectors would match nothing.
    for sel in (a.FIELD_ENTRY, a.FORM_READY, a.RESUME_INPUT, a.LOCATION_ENTRY):
        assert not sel.lstrip().startswith("form"), sel
    assert a.ON_APPLICATION_URL.search("https://jobs.ashbyhq.com/cohere/291e5dee/application?x=1")
    assert not a.ON_APPLICATION_URL.search("https://jobs.ashbyhq.com/cohere/291e5dee")


# ---------- cover letters ----------
def test_cover_letter_is_grounded_and_reads_like_a_person(monkeypatch, tmp_db):
    import jobbot.llm
    from jobbot import cover
    seen = {}

    def fake_complete(prompt, **_k):
        seen["prompt"] = prompt
        return ("I spent three years at Anlytic building a multi-agent analytics platform.\n\n"
                "That is the work you are describing.\n\nJawad Amir")

    monkeypatch.setattr(jobbot.llm, "complete", fake_complete)
    job = {"id": "greenhouse:acme:1", "company": "Acme", "title": "Forward Deployed Engineer",
           "description": "You will embed with customers and ship agents."}
    text = cover.letter_for(job, "CV: Anlytic, multi-agent analytics platform.", FACTS)

    assert "Anlytic" in text
    assert not cover.looks_machine_written(text)
    # the prompt carries the job, the CV and the grounding rule
    assert "Acme" in seen["prompt"] and "Forward Deployed Engineer" in seen["prompt"]
    assert "embed with customers" in seen["prompt"]
    assert "Never invent an employer" in seen["prompt"]
    # written once, then reused: the same employer must not get two different letters
    calls = []
    monkeypatch.setattr(jobbot.llm, "complete", lambda p, **k: calls.append(1) or "different letter")
    assert cover.letter_for(job, "CV", FACTS) == text
    assert calls == []


def test_cover_letter_strips_machine_tells(monkeypatch, tmp_db):
    """Em-dashes and stock openings are the clearest signs a model wrote it."""
    import jobbot.llm
    from jobbot import cover
    replies = iter([
        "I am writing to express my strong interest in this role. Jawad Amir",   # rejected, retried
        "I built the eval harness at Anlytic—350 scenarios in CI. Jawad Amir",
    ])
    monkeypatch.setattr(jobbot.llm, "complete", lambda p, **k: next(replies))
    text = cover.letter_for({"id": "x:9", "company": "A", "title": "T"}, "CV", FACTS)
    assert "writing to express" not in text
    assert "—" not in text and "350 scenarios" in text


def test_cover_letter_is_skipped_without_a_cv(monkeypatch, tmp_db):
    import jobbot.llm
    from jobbot import cover
    monkeypatch.setattr(jobbot.llm, "complete", lambda p, **k: pytest.fail("must not ask without a CV"))
    assert cover.letter_for({"id": "x:8", "company": "A", "title": "T"}, "", FACTS) == ""


def test_only_an_explicit_cover_letter_field_gets_the_letter():
    """"Why do you want to work here" is a screening question with its own short answer, not a place to
    paste a whole letter."""
    from jobbot.apply import common as c
    assert c.COVER_LABEL_RE.search("Cover Letter")
    assert c.COVER_LABEL_RE.search("Letter of interest")
    assert not c.COVER_LABEL_RE.search("Why do you want to work at Acme?")
    assert not c.COVER_LABEL_RE.search("Tell us about a project you are proud of")


# ---------- credentials must never live in facts.yaml ----------
def test_a_password_written_into_facts_is_moved_to_the_keychain(tmp_path, monkeypatch):
    """facts.yaml is read into LLM prompts, so a password put there would be sent to the model with every
    question. It is moved to the keychain and stripped from the file."""
    import yaml
    facts = tmp_path / "facts.yaml"
    facts.write_text(yaml.safe_dump({
        "identity": {"email": "me@example.com", "mail_password": "abcdefghijklmnop"},
        "work": {"current_title": "Engineer"},
    }))
    monkeypatch.setattr(config, "FACTS_PATH", facts)
    stored = {}
    monkeypatch.setattr(config, "set_secret", lambda n, v: stored.__setitem__(n, v))

    assert config.migrate_secrets_from_facts() == ["JOBBOT_MAIL_PASSWORD"]
    assert stored == {"JOBBOT_MAIL_PASSWORD": "abcdefghijklmnop"}
    on_disk = facts.read_text()
    assert "abcdefghijklmnop" not in on_disk
    assert "mail_password" not in on_disk
    assert yaml.safe_load(on_disk)["identity"]["email"] == "me@example.com"   # the real facts survive
    assert yaml.safe_load(on_disk)["work"]["current_title"] == "Engineer"


def test_load_facts_never_hands_a_credential_to_a_prompt(tmp_path, monkeypatch):
    import yaml
    facts = tmp_path / "facts.yaml"
    facts.write_text(yaml.safe_dump({"identity": {"email": "me@example.com", "app_password": "secretvalue1"}}))
    monkeypatch.setattr(config, "FACTS_PATH", facts)
    loaded = config.load_facts()
    assert "app_password" not in loaded["identity"]
    assert loaded["identity"]["email"] == "me@example.com"


def test_facts_without_credentials_are_left_alone(tmp_path, monkeypatch):
    import yaml
    facts = tmp_path / "facts.yaml"
    original = yaml.safe_dump({"identity": {"email": "me@example.com"}})
    facts.write_text(original)
    monkeypatch.setattr(config, "FACTS_PATH", facts)
    assert config.migrate_secrets_from_facts() == []
    assert facts.read_text() == original      # untouched, not reformatted


# ---------- the emailed verification code step ----------
def _page_eval(script: str, body_text: str):
    """Answer page.evaluate() the way a real page would: only the innerText probe returns the page text;
    the captcha and form-error probes return their own falsy shapes."""
    script = script or ""
    if "captcha" in script.lower():
        return False
    if "aria-invalid" in script or "field-error" in script:
        return []
    return body_text


class _VerifyPage:
    """A form showing Greenhouse's "enter the 8-character code" step."""
    TEXT = ("A verification code was sent to you@example.com. To submit your application, enter the "
            "8-character code to confirm you're a human. Security code")

    def __init__(self, boxes=8):
        self.boxes = boxes
        self.typed = []

    def evaluate(self, script="", *_a, **_k):
        return _page_eval(script, self.TEXT)

    def locator(self, selector):
        return _VerifyLocator(self, selector)

    def wait_for_timeout(self, _ms):
        pass


class _VerifyLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.single = "maxlength='1'" in selector

    def count(self):
        return self.page.boxes if self.single else 1

    @property
    def first(self):
        return self

    def nth(self, i):
        loc = _VerifyLocator(self.page, self.selector)
        loc.index = i
        return loc

    def is_visible(self):
        return True

    def click(self, **_k):
        pass

    def fill(self, value, **_k):
        if value == "":
            return          # the clear-before-type pass; only real characters are recorded
        self.page.typed.append(value)


def test_verification_prompt_is_recognised():
    from jobbot.apply import common as c
    prompt = c.verification_prompt(_VerifyPage())
    assert prompt == {"length": 8, "to": "you@example.com"}


def test_verification_prompt_ignores_an_ordinary_form():
    from jobbot.apply import common as c

    class _Plain(_VerifyPage):
        def evaluate(self, script="", *_a, **_k):
            return _page_eval(script, "First name Last name Submit application")

    assert c.verification_prompt(_Plain()) is None


def test_verification_code_is_typed_one_character_per_box():
    from jobbot.apply import common as c
    page = _VerifyPage()
    assert c.fill_verification_code(page, "AB12CD34") is True
    assert page.typed == list("AB12CD34")


def test_verification_inputs_are_not_treated_as_screening_questions():
    """Regression: the emailed-code boxes sit in the same <form> as the questions, so the control loop asked
    the user for "Security code" — a different question from the one the code was already answered under, and
    the resume asked again forever."""
    from jobbot.apply import common as c

    class _El:
        def __init__(self, attrs=""):
            self.attrs = attrs

        def evaluate(self, *_a, **_k):
            return self.attrs

    assert c.is_verification_control(_El(), "Security code")
    assert c.is_verification_control(_El(), "Verification code")
    assert c.is_verification_control(_El("one-time-code 1 code otp numeric"), "")
    assert c.is_verification_control(_El("off 1 security_code sec numeric"), "")
    # ordinary questions are untouched
    assert not c.is_verification_control(_El("off  title  text"), "What is your current job title?")
    assert not c.is_verification_control(_El("off  postal_code  text"), "Postal code")


class _SubmittingPage:
    """The last frame of the code step, then the thank-you page a moment later."""
    url = "https://job-boards.greenhouse.io/acme/jobs/1"

    def __init__(self, frames_before_confirm=2):
        self.calls = 0
        self.frames = frames_before_confirm

    def evaluate(self, script="", *_a, **_k):
        if "captcha" in (script or "").lower() or "aria-invalid" in (script or ""):
            return _page_eval(script, "")
        self.calls += 1
        if self.calls <= self.frames:
            return _VerifyPage.TEXT
        return "Thank you for applying. Your application has been received."

    def locator(self, selector):
        return _VerifyLocator(self, selector)

    @property
    def boxes(self):
        return 8

    def wait_for_timeout(self, _ms):
        pass


def test_a_submit_that_lands_is_not_mistaken_for_the_code_step():
    """Regression: the confirmation check ran in the same millisecond as the submit, saw the code step still
    painted on the outgoing page, re-submitted onto the thank-you page and reported "Submit button not
    found" — for an application Greenhouse had already accepted."""
    from jobbot.apply import common as c
    assert c.wait_for_confirmation(_SubmittingPage(), timeout_s=5) is True


def test_a_code_step_that_persists_is_still_reported():
    from jobbot.apply import common as c

    class _Stuck(_SubmittingPage):
        def evaluate(self, script="", *_a, **_k):
            return _page_eval(script, _VerifyPage.TEXT)

    with pytest.raises(c.VerificationRequired):
        c.wait_for_confirmation(_Stuck(), timeout_s=10)


def test_a_site_refusal_is_quoted_instead_of_submit_not_confirmed():
    """Regression: Cohere answered a correctly filled submission with "You have reached the maximum number of
    applications", and the bot reported "Submit not confirmed — no error message on the form"."""
    from jobbot.apply import common as c

    class _Refused:
        def evaluate(self, script="", *_a, **_k):
            return _page_eval(script, "We couldn't submit your application You have reached the maximum "
                                      "number of applications allowed on our career site (5 applications). "
                                      "This limit helps us ensure a quality experience.")

    msg = c.submit_blocked_message(_Refused())
    assert "maximum number of applications" in msg
    assert "quality experience" not in msg          # stops at the sentence that carries the reason

    class _Normal:
        def evaluate(self, script="", *_a, **_k):
            return _page_eval(script, "First name Last name Submit application")

    assert c.submit_blocked_message(_Normal()) == ""


# ---------- a parked browser that died while paused ----------
class _DeadPage:
    """Chromium killed under Playwright: is_closed() still False, but every call raises 'target closed'."""
    def is_closed(self):
        return False

    def evaluate(self, *_a, **_k):
        raise RuntimeError("Target page, context or browser has been closed")


class _LivePage:
    def is_closed(self):
        return False

    def evaluate(self, *_a, **_k):
        return 1


class _WrongThreadPage:
    """A page whose owner thread has exited: Playwright's greenlet dispatcher refuses to switch to us."""
    url = "https://jobs.lever.co/acme/1/apply"

    def is_closed(self):
        return False

    def evaluate(self, *_a, **_k):
        raise RuntimeError("cannot switch to a different thread (which happens to have exited)")


def test_page_alive_sees_through_is_closed_false():
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyError
    assert c.page_alive(_LivePage()) is True
    assert c.page_alive(_DeadPage()) is False
    with pytest.raises(ApplyError, match="closed"):
        c.require_open(_DeadPage())


def test_page_alive_treats_a_cross_thread_error_as_dead():
    """Regression (application 19): resuming from a second thread made every Playwright call raise
    greenlet.error. page_alive did not recognise the message, reported the page alive, and the adapter — whose
    probes all swallow exceptions — concluded "Lever application form not found" on a form that was there."""
    from jobbot.apply import common as c
    assert c.page_alive(_WrongThreadPage()) is False

    greenlet = pytest.importorskip("greenlet")

    class _RealGreenletError(_WrongThreadPage):
        def evaluate(self, *_a, **_k):
            raise greenlet.error("cannot switch to a different thread")

    assert c.page_alive(_RealGreenletError()) is False


# ---------- owner-thread resume ----------
class _FakeThread:
    """Stands in for a live owner thread; never equal to this thread's ident."""
    name = "app-fake"
    ident = -1

    def is_alive(self):
        return True


class _Untouchable:
    """Any use of this page fails the test: the web thread must not touch Playwright objects."""
    def __getattr__(self, name):
        raise AssertionError(f"page.{name} was used from the answering thread")


def _live(**over):
    import queue as _q
    live = {"queue": _q.Queue(), "parked": True, "owner": _FakeThread(), "page": _Untouchable(),
            "ctx": object(), "resolver": Resolver({}, {}, {}, False), "screenshot": lambda _l: "",
            "adapter": None}
    live.update(over)
    return live


def test_resume_hands_the_work_to_the_owner_thread(tmp_db, monkeypatch):
    """The answer must be queued to the thread that owns the browser, not applied on the web thread."""
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    monkeypatch.setattr(runner, "_run_application", lambda i: pytest.fail("must not start a fresh run"))
    app_id = _mk_job(tmp_db, "lever", "https://jobs.lever.co/acme/1/apply")
    tmp_db.update_application(app_id, status="needs_you", pending_question="Arabic?", pending_options='["Yes","No"]')
    live = _live()
    runner._LIVE[app_id] = live

    runner.resume_application(app_id, "No")

    assert live["queue"].get_nowait() == ("resume", "Arabic?", "No")
    assert live["parked"] is False
    assert app_id in runner._LIVE
    row = tmp_db.get_application(app_id)
    assert row["status"] == "running" and row["step"] == "Resuming" and row["pending_question"] == ""


def test_second_answer_for_the_same_pause_is_ignored(tmp_db, monkeypatch):
    """Double-clicking "Answer & continue" must not queue a stale answer that a later question would get."""
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    monkeypatch.setattr(runner, "_run_application", lambda i: pytest.fail("must not start a fresh run"))
    app_id = _mk_job(tmp_db, "lever", "https://jobs.lever.co/acme/1/apply")
    tmp_db.update_application(app_id, status="needs_you", pending_question="Arabic?", pending_options="[]")
    live = _live()
    runner._LIVE[app_id] = live

    runner.resume_application(app_id, "No")
    runner.resume_application(app_id, "Yes")

    assert live["queue"].get_nowait() == ("resume", "Arabic?", "No")
    assert live["queue"].empty()


class _TwoStepAdapter:
    """Asks one question, then completes — the shape of every real resume."""
    def __init__(self):
        self.calls = 0

    def apply(self, _ctx):
        self.calls += 1
        if self.calls == 1:
            raise NeedsHuman("need an answer", question="Favourite framework?")


def _owned_live(adapter, page):
    import queue as _q
    import threading
    return {"queue": _q.Queue(), "parked": False, "owner": threading.current_thread(), "page": page,
            "ctx": object(), "resolver": Resolver({}, {}, {}, False), "screenshot": lambda _l: "",
            "adapter": adapter}


def test_serve_applies_a_queued_answer_on_the_owner_thread(tmp_db, monkeypatch):
    """The whole point of the fix: the owner thread parks on NeedsHuman, then continues on the SAME page."""
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    app_id = _mk_job(tmp_db, "lever", "https://jobs.lever.co/acme/1/apply")
    adapter = _TwoStepAdapter()
    live = _owned_live(adapter, _LivePage())
    runner._LIVE[app_id] = live
    live["queue"].put(("resume", "Favourite framework?", "FastAPI"))

    runner._serve(app_id, live)

    assert adapter.calls == 2
    assert tmp_db.get_application(app_id)["status"] == "submitted"
    assert app_id not in runner._LIVE
    assert config.load_answers()["favourite framework"] == "FastAPI"


def test_serve_starts_fresh_when_the_parked_browser_died(tmp_db, monkeypatch):
    """Answer -> resume after a long pause must not die with 'form not found' on a dead page: it should cache
    the answer, drop the dead browser and start the application over (the cache then replays the answer)."""
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    app_id = _mk_job(tmp_db, "ashby", "https://jobs.ashbyhq.com/acme/1")
    adapter = _TwoStepAdapter()
    live = _owned_live(adapter, _DeadPage())
    runner._LIVE[app_id] = live
    live["queue"].put(("resume", "Arabic?", "No"))
    started: list[int] = []
    monkeypatch.setattr(runner, "_run_application", lambda i: started.append(i))

    runner._serve(app_id, live)

    assert started == [app_id]
    assert app_id not in runner._LIVE
    assert config.load_answers()["arabic"] == "No"


def test_serve_closes_on_command(tmp_db, monkeypatch):
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    app_id = _mk_job(tmp_db, "lever", "https://jobs.lever.co/acme/1/apply")

    class _AlwaysAsks:
        def apply(self, _ctx):
            raise NeedsHuman("need an answer", question="Q?")

    live = _owned_live(_AlwaysAsks(), _LivePage())
    runner._LIVE[app_id] = live
    live["queue"].put(("close",))

    runner._serve(app_id, live)

    assert app_id not in runner._LIVE
    assert tmp_db.get_application(app_id)["status"] == "needs_you"


def test_resume_starts_fresh_when_the_owner_thread_is_gone(tmp_db, monkeypatch):
    """Server restarted, or the owner died: the answer is cached and the application starts over, on the
    thread that is answering. Nothing may touch the orphaned Playwright objects."""
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    app_id = _mk_job(tmp_db, "ashby", "https://jobs.ashbyhq.com/acme/1")
    tmp_db.update_application(app_id, status="needs_you", pending_question="Arabic?", pending_options='["Yes","No"]')

    class _DeadThread(_FakeThread):
        def is_alive(self):
            return False

    runner._LIVE[app_id] = _live(owner=_DeadThread())
    started: list[int] = []
    monkeypatch.setattr(runner, "_run_application", lambda i: started.append(i))

    runner.resume_application(app_id, "No")

    assert started == [app_id]
    assert app_id not in runner._LIVE
    assert config.load_answers()["arabic"] == "No"


# ---------- the application-source question: always answered, never asked ----------
SOURCE_QUESTIONS = [
    "How did you hear about us?", "How did you hear about this job?", "Where did you first learn about this role?",
    "How did you find us?", "Source of application", "Referral source", "Source",
    # every one below was a MISS before: the bot stopped and asked the user
    "Please tell us how you heard about this opportunity.",   # Palantir/Lever, the one that broke app 19
    "Please select how you heard about us", "How did you come across this opportunity?",
    "Where did you find this job posting?", "Where did you see this job advertised?", "Source of hire",
    "How'd you hear about us?", "How did you know about this job?", "How did you come to know about Acme?",
    "How have you heard about us?", "How did you become aware of this opportunity?",
    "How were you made aware of this role?", "Candidate source", "Lead Source", "Job source",
    "Heard about us via", "Where did you come across this job?", "How do you know about us?",
    "How did you find out about us?", "Where did you find out about this opening?",
]

NOT_SOURCE_QUESTIONS = [
    # an essay prompt that merely contains "hear about" must NOT get the source answer
    "Feel free to share any additional information here - we're especially keen to hear about your "
    "Kubernetes experience and working in client facing roles.",
    "Why do you want to work at Acme?", "Who referred you?", "Referrer name", "Where are you located?",
    "How did you find the interview process?", "How do you know Python?", "How did you learn Kubernetes?",
    "What source control tools do you use?", "Where did you go to university?",
    "How did you find your current role challenging?", "How did you hear the news about the merger?",
]

PALANTIR_OPTIONS = ["Agency or Non-Palantir Recruiter", "America's Job Exchange", "BuiltIn", "Campus Ambassador",
                    "Friend or Family", "Glassdoor", "Hackajob", "Hallo", "Handshake",
                    "Job Board (Indeed, Monster, etc.)", "LinkedIn", "Palantir Event", "Palantir Medium Blog",
                    "Palantir Recruiter", "Palantir Website", "Rewriting the Code", "Tapia", "University Job Board",
                    "University or University Organization", "Other"]


def test_source_question_is_answered_for_every_phrasing(no_llm):
    r = mk()
    for q in SOURCE_QUESTIONS:
        assert r.answer(q) == "LinkedIn", q


def test_source_question_is_anchored_on_the_question_shape(no_llm):
    """Regression: a bare "hear about" substring answered "LinkedIn" to an essay prompt that said "we're keen
    to hear about your Kubernetes experience". The match is on the question's shape, not its keywords.

    Asserted on the classifier rather than on the answer: some of these are legitimately answered by other
    rules ("Where are you located?" -> the location from facts). What must not happen is the source policy
    claiming them.
    """
    r = mk()
    for q in NOT_SOURCE_QUESTIONS:
        assert not r.is_source_question(normalize_question(q)), q
    # the essay prompt in full: it must still stop and ask
    with pytest.raises(NeedsHuman):
        r.answer(NOT_SOURCE_QUESTIONS[0])


def test_source_question_picks_the_linkedin_option(no_llm):
    """The exact failure of application 19: Palantir's Lever form asks this as a native select, and the bot
    stopped to ask the user instead of choosing LinkedIn from the list."""
    r = mk()
    assert r.answer("Please tell us how you heard about this opportunity.", PALANTIR_OPTIONS, "select") == "LinkedIn"
    # spacing variants and lists where LinkedIn is named inside a broader option
    assert r.answer("How did you hear about us?", ["Linked In", "Indeed", "Other"], "select") == "Linked In"
    assert r.answer("How did you hear about this role?",
                    ["Social Media (LinkedIn, X, etc.)", "Job Board", "Referral"], "select") \
        == "Social Media (LinkedIn, X, etc.)"


def test_source_question_falls_back_to_the_closest_category(no_llm):
    """No LinkedIn option at all: pick the category LinkedIn belongs to rather than stopping to ask."""
    r = mk()
    assert r.answer("How did you hear about us?",
                    ["Social media", "Employee referral", "Company website", "Other"], "select") == "Social media"
    assert r.answer("Where did you find this job posting?",
                    ["Online job board", "Recruiter", "Referral", "Other"], "select") == "Online job board"
    assert r.answer("How did you hear about this job?",
                    ["Employee referral", "Company website", "Other"], "select") == "Other"


def test_source_question_recognised_from_its_options_when_the_label_is_vague(no_llm):
    """Some boards label the field "Source" or "Please select one"; the option list gives it away."""
    r = mk()
    assert r.answer("Please select one", PALANTIR_OPTIONS, "select") == "LinkedIn"


def test_source_options_do_not_swallow_unrelated_lists(no_llm):
    from jobbot.apply.resolver import _looks_like_source_options
    assert not _looks_like_source_options(["Yes", "No"])
    assert not _looks_like_source_options(["Onsite", "Remote", "Hybrid"])
    assert not _looks_like_source_options(["English (ENG)", "Spanish (SPA)", "LinkedIn"])


def test_source_answer_beats_a_stale_cached_answer(no_llm):
    """A junk answer typed once to get past a pause must not become the policy for every application."""
    r = mk(answers={"how did you hear about us": "Other"})
    assert r.answer("How did you hear about us?", ["LinkedIn", "Other"], "select") == "LinkedIn"


def test_source_answer_comes_from_facts(no_llm):
    facts = {**FACTS, "preferences": {**FACTS["preferences"], "job_source": "Indeed"}}
    r = Resolver(facts, {}, JOB, llm_enabled=False)
    assert r.answer("How did you hear about us?") == "Indeed"
    assert r.answer("How did you hear about this role?", ["LinkedIn", "Indeed", "Other"], "select") == "Indeed"


# ---------- conditional follow-ups and one-time codes ----------
def test_conditional_followup_is_left_blank_when_its_condition_was_not_met(no_llm):
    """Regression (Scale AI): a non-compete question was answered "No" and the bot wrote a paragraph about
    visa sponsorship into the "If yes, please provide further explanation below." box underneath it."""
    r = mk(answers={"are you currently bound by any agreements with a former employer": "No"})
    assert r.answer("Are you currently bound by any agreements with a former employer?", ["Yes", "No"],
                    "select") == "No"
    assert r.previous_answer == "No"
    for q in ("If yes, please provide further explanation below.", "If so, please explain.",
              "If yes, please describe your experience", "If other, please specify"):
        assert r.answer(q, None, "textarea") == "", q


def test_conditional_followup_is_answered_when_its_condition_was_met(no_llm):
    r = mk()
    r.previous_answer = "Yes"
    with pytest.raises(NeedsHuman):
        r.answer("If yes, please explain", None, "textarea")
    # "If no, ..." triggers on a negative answer, not a positive one
    r.previous_answer = "No"
    with pytest.raises(NeedsHuman):
        r.answer("If no, please explain why", None, "textarea")


def test_a_plain_explain_question_is_not_treated_as_conditional(no_llm):
    r = mk()
    r.previous_answer = "No"
    with pytest.raises(NeedsHuman):
        r.answer("Please explain why you want this role", None, "textarea")


def test_an_already_filled_control_counts_as_the_previous_answer(no_llm):
    """On a re-run after a pause the adapter skips filled controls, so it reports their value instead."""
    r = mk()
    r.previous_answer = "Yes"
    r.previous_answer = "No"     # what ctx.seen() does for a control that already reads "No"
    assert r.answer("If yes, please explain", None, "textarea") == ""


def test_a_one_time_code_is_never_cached_to_disk(no_llm):
    """A verification code is spent the moment it is used; replaying it would fail the next application."""
    from jobbot.apply.resolver import is_one_time_secret
    assert is_one_time_secret("Verification code from the email (one-time)")
    assert not is_one_time_secret("What is your postal code?")

    r = mk()
    with pytest.raises(NeedsHuman):
        r.answer("Verification code from the email (one-time)")
    r.learn("Verification code from the email (one-time)", "AB12CD34")
    # readable for the rest of this run, so the adapter can type it after the pause
    assert r.answer("Verification code from the email (one-time)") == "AB12CD34"
    assert config.load_answers() == {}


def test_the_model_is_shown_the_cached_answers_that_match_the_question(monkeypatch):
    """Regression: the prompt carried an alphabetical slice of answers.json, so a question the candidate had
    already answered in other words ("Do you have business proficiency in English and Arabic?" vs the cached
    "Can you work professionally in both English and Arabic?") reached the model without its own answer
    attached, and it correctly declined — sending a solved question back to the user."""
    import jobbot.llm
    cache = {
        "can you work professionally in both english and arabic": "No",
        "aaa first alphabetically": "irrelevant",
        "zzz last alphabetically": "irrelevant",
        **{f"filler question number {i}": "x" for i in range(15)},
    }
    seen = {}

    def fake_complete(prompt, **_k):
        seen["prompt"] = prompt
        return "No"

    monkeypatch.setattr(jobbot.llm, "complete", fake_complete)

    r = mk(llm=True, answers=cache)
    assert r.answer("Do you have business proficiency in English and Arabic?") == "No"
    prompt = seen["prompt"]
    assert "can you work professionally in both english and arabic" in prompt
    assert "reply with that same answer" in prompt


def test_pronounce_is_not_mistaken_for_pronouns(no_llm):
    """Regression: "pronoun" was a prefix keyword, so it matched "pronounce" and Palantir's harmless
    "Name Pronunciation | How do you pronounce your name?" was treated as a protected EEO question."""
    for q in ("Name Pronunciation | How do you pronounce your name?", "How should we pronounce your name?"):
        assert not Resolver.is_eeo(q), q
        assert not Resolver.is_protected(q), q
    # real pronoun questions stay protected
    for q in ("What are your pronouns?", "Pronouns", "Please share your pronoun"):
        assert Resolver.is_eeo(q), q
        assert Resolver.is_protected(q), q


def test_pronunciation_does_not_answer_with_the_plain_name(no_llm):
    """"How do you pronounce your name" wants a phonetic rendering; echoing "Jawad Amir" answers nothing."""
    r = mk()
    with pytest.raises(NeedsHuman):
        r.answer("How do you pronounce your name?", None, "text")
    assert r.answer("What is your full name?") == "Jawad Amir"


def test_pronunciation_is_answered_by_the_model_not_the_user(monkeypatch):
    """It is derivable from the name, so the model must not decline it — the run used to stop here."""
    import jobbot.llm
    seen = {}

    def fake_complete(prompt, **_k):
        seen["prompt"] = prompt
        return "JAH-wahd Ah-MEER"

    monkeypatch.setattr(jobbot.llm, "complete", fake_complete)
    r = mk(llm=True)
    assert r.answer("Name Pronunciation | How do you pronounce your name?") == "JAH-wahd Ah-MEER"
    assert "phonetic rendering" in seen["prompt"]


def test_travel_questions_come_from_facts(no_llm):
    """Forward Deployed forms ask this constantly and no model can know the answer, so it must be a fact.
    Scale AI's "business trips every 6-8 weeks" stopped a run before this rule existed."""
    facts = {**FACTS, "preferences": {**FACTS["preferences"], "willing_to_travel": True}}
    r = Resolver(facts, {}, JOB, llm_enabled=False)
    for q in ("Are you ready to go on business trips every 6-8 weeks?", "Are you willing to travel up to 25%?",
              "Willing to travel?", "This role requires travel to client sites — are you comfortable?"):
        assert r.answer(q, ["Yes", "No"], "select") == "Yes", q

    facts["preferences"]["willing_to_travel"] = False
    assert Resolver(facts, {}, JOB, llm_enabled=False).answer("Willing to travel?", ["Yes", "No"]) == "No"

    # absent from facts: ask rather than guess a personal constraint
    bare = {**FACTS, "preferences": {k: v for k, v in FACTS["preferences"].items()}}
    with pytest.raises(NeedsHuman):
        Resolver(bare, {}, JOB, llm_enabled=False).answer("Willing to travel?", ["Yes", "No"])


def test_source_answer_is_cached(no_llm):
    r = mk()
    r.answer("How did you hear about us?")
    assert config.load_answers()["how did you hear about us"] == "LinkedIn"


# ---------- React-aware filling and refill-on-bounce ----------
class _ReactInput:
    """A controlled input whose first fill lands in the DOM but not in React's state (hydration race)."""
    def __init__(self, drop_first=True):
        self.dom, self.tracked, self.fills, self._drop = "", "", 0, drop_first

    def evaluate(self, js, *a):
        if "__reactProps" in js:
            return self.tracked
        if "tagName" in js:
            return "INPUT"
        return ""

    def get_attribute(self, name):
        return "text" if name == "type" else None

    def is_visible(self):
        return True

    def input_value(self):
        return self.dom

    def click(self, **k):
        pass

    def fill(self, v, **k):
        self.fills += 1
        self.dom = v
        if self._drop:
            self._drop = False      # React missed this one
        else:
            self.tracked = v


def test_fill_verified_refills_when_react_dropped_the_value():
    from jobbot.apply import common as c
    el = _ReactInput(drop_first=True)
    assert c.fill_verified(el, "Jawad Amir") is True
    assert el.tracked == "Jawad Amir" and el.fills == 2      # second fill made React register it


def test_fill_verified_leaves_a_real_different_value_alone():
    from jobbot.apply import common as c
    el = _ReactInput(drop_first=False)
    el.dom = el.tracked = "Someone Else"                      # user-typed value already registered
    c.fill_verified(el, "Jawad Amir")
    assert el.tracked == "Someone Else" and el.fills == 0


def test_submit_and_confirm_refills_once_on_form_rejection(monkeypatch):
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext, ApplyError
    clicks, refills, steps = [], [], []
    outcomes = iter([ApplyError("Form rejected: Missing entry for required field: Name"), True])
    monkeypatch.setattr(c, "click_submit", lambda page, names: clicks.append(1))

    def fake_wait(page, **k):
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o
    monkeypatch.setattr(c, "wait_for_confirmation", fake_wait)
    ctx = ApplyContext(job={}, page=object(), facts={}, cv_path="", step=steps.append, answer=lambda *a, **k: "",
                       screenshot=lambda l: "")
    c.submit_and_confirm(ctx, ("Submit",), refill=lambda: refills.append(1))
    assert clicks == [1, 1] and refills == [1]
    assert any("refilling" in s for s in steps)

    # a second rejection is surfaced, not swallowed
    outcomes = iter([ApplyError("Form rejected: x"), ApplyError("Form rejected: x")])
    with pytest.raises(ApplyError, match="Form rejected"):
        c.submit_and_confirm(ctx, ("Submit",), refill=lambda: None)


def test_session_path_is_per_company_but_shared_for_account_adapters():
    """LinkedIn is one account across every employer: keying its cookies per company asks for the same
    login once per employer and never reuses it. Real ATS boards stay per company."""
    from jobbot.apply import runner

    a = runner._session_path("greenhouse", "Acme Corp")
    b = runner._session_path("greenhouse", "Other Inc")
    assert a != b and a.name == "greenhouse-acme-corp.json"

    x = runner._session_path("linkedin", "Acme Corp", shared=True)
    y = runner._session_path("linkedin", "Other Inc", shared=True)
    assert x == y and x.name == "linkedin.json"


def test_linkedin_adapter_declares_needs_account():
    """The flag _session_path keys off; greenhouse/lever/ashby must not set it."""
    assert get_adapter_for("linkedin").needs_account is True
    assert all(get_adapter_for(a).needs_account is False for a in ("greenhouse", "lever", "ashby"))


def test_linkedin_signed_out_reads_the_cookie_not_the_modal():
    """The contextual sign-in modal carries neither SIGN_IN_WALL selector, so DOM sniffing alone reported a
    logged-out user as 'no Apply button' — the message that sent the user round the Continue loop."""
    from jobbot.apply.linkedin import LinkedInAdapter

    assert LinkedInAdapter._signed_out(_FakePage([], wall=0)) is True
    assert LinkedInAdapter._signed_out(_FakePage([], cookies=[{"name": "JSESSIONID"}])) is True
    assert LinkedInAdapter._signed_out(_FakePage([], cookies=[{"name": "li_at", "value": "x"}])) is False


def test_linkedin_reloads_stale_page_before_reporting_no_apply_button():
    """The resume loop: a login performed in the window leaves the signed-out DOM in place, so re-running
    the adapter on it raised the same NeedsHuman forever. It must reload before it concludes anything."""
    page = _FakePage(["public_jobs_apply-link-offsite_x"], cookies=[{"name": "li_at", "value": "x"}])
    with pytest.raises(NeedsHuman):
        get_adapter_for("linkedin").apply(_li_ctx(page))
    assert page.reloads == 1


def test_linkedin_signed_in_page_with_apply_button_is_not_reloaded():
    """The happy path pays nothing for the fix."""
    from jobbot.apply.linkedin import LinkedInAdapter
    page = _FakePage(["public_jobs_apply-link-offsite_x"], apply_visible=True,
                     cookies=[{"name": "li_at", "value": "x"}])
    assert LinkedInAdapter._apply_control(page) is not None
    assert page.reloads == 0


def test_every_adapter_loads_even_when_one_was_imported_first():
    """linkedin.py registers itself and then asks for the adapter it hands off to. A registry-empty guard
    would skip loading the others, reporting a supported Greenhouse board as unsupported."""
    import subprocess, sys
    code = ("import jobbot.apply.linkedin;"
            "from jobbot.apply.base import get_adapter_for;"
            "print(all(get_adapter_for(a) for a in ('greenhouse','lever','ashby','linkedin')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "True", out.stderr


def test_resume_on_the_employer_form_does_not_reload_or_re_resolve():
    """The refill bug: a resume re-enters LinkedIn's apply() on the half-filled ATS form. Re-resolving
    there reloads the page and throws away every answer the user just gave."""
    from jobbot.apply.base import Adapter, register, _REGISTRY

    page = _FakePage([], cookies=[{"name": "li_at", "value": "x"}])
    page.url = "https://job-boards.greenhouse.io/acme/jobs/1"
    seen: list[str] = []

    class _Spy(Adapter):
        ats = "greenhouse"
        def apply(self, ctx):
            seen.append(ctx.job["url"])

    keep = _REGISTRY.get("greenhouse")
    register(_Spy)
    try:
        get_adapter_for("linkedin").apply(_li_ctx(page))
    finally:
        if keep is not None:
            _REGISTRY["greenhouse"] = keep

    assert seen == ["https://job-boards.greenhouse.io/acme/jobs/1"]
    assert page.reloads == 0, "a half-filled employer form must never be reloaded on resume"


def test_resume_on_an_unsupported_site_still_asks_rather_than_reloading():
    """Indeed is the one destination left with no adapter; PageUp and friends now have the generic one."""
    page = _FakePage([], cookies=[{"name": "li_at", "value": "x"}])
    page.url = "https://www.indeed.com/viewjob?jk=1"
    with pytest.raises(NeedsHuman, match="no adapter"):
        get_adapter_for("linkedin").apply(_li_ctx(page))
    assert page.reloads == 0


def test_failed_run_keeps_its_browser_so_retry_can_reuse_it(tmp_db, monkeypatch):
    """Closing on failure threw away a half-filled form and made Retry start over on an empty one."""
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    closed: list[int] = []
    monkeypatch.setattr(runner, "_close", lambda app_id, **k: closed.append(app_id))
    monkeypatch.setattr(runner, "_save_state", lambda _live: None)

    app_id = _mk_job(tmp_db, "greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1")
    live = _live(parked=False)
    runner._LIVE[app_id] = live

    runner._finish_failed(app_id, RuntimeError("form not found"), lambda _l: "")

    assert tmp_db.get_application(app_id)["status"] == "failed"
    assert closed == [], "the window must stay open for Retry"
    assert live["parked"] is True
    assert runner.has_live_browser(app_id) is True


def test_failed_setup_with_no_browser_still_closes(tmp_db, monkeypatch):
    from jobbot.apply import runner
    monkeypatch.setattr(runner, "_LIVE", {})
    closed: list[int] = []
    monkeypatch.setattr(runner, "_close", lambda app_id, **k: closed.append(app_id))
    app_id = _mk_job(tmp_db, "lever", "https://jobs.lever.co/acme/1/apply")

    runner._finish_failed(app_id, RuntimeError("playwright missing"), lambda _l: "")

    assert closed == [app_id]
    assert runner.has_live_browser(app_id) is False


def test_fact_values_flattens_every_scalar():
    from jobbot.apply.runner import _fact_values
    vals = _fact_values({"identity": {"email": "J@Example.com"}, "skills": ["Python", "AWS"],
                         "work": {"open_to_remote": True, "years": 6}})
    assert "j@example.com" in vals and "python" in vals and "6" in vals
    assert "true" not in vals, "booleans are not values a form field could carry"


def _seen_fn(tmp_path, monkeypatch, facts, answers, answered=()):
    """The runner's seen() callback, built the way _run_application builds it."""
    from jobbot.apply import runner
    monkeypatch.setattr(config, "ANSWERS_PATH", tmp_path / "answers.json")
    resolver = Resolver(facts, dict(answers), {"company": "Acme"}, llm_enabled=False)
    return runner.make_seen(resolver, {normalize_question(q) for q in answered},
                            runner._fact_values(facts)), resolver


def test_learns_an_answer_the_user_typed_into_the_window(tmp_path, monkeypatch):
    """The point of the feature: an answer jobbot never saw, sitting in a field the user filled by hand."""
    seen, resolver = _seen_fn(tmp_path, monkeypatch, FACTS, {})
    seen("I led the Bedrock AgentCore rollout at Anlytic.", "Describe your agent experience")
    assert resolver.answer("Describe your agent experience") == "I led the Bedrock AgentCore rollout at Anlytic."
    assert json.loads((tmp_path / "answers.json").read_text()), "and it is on disk for the next application"


def test_does_not_learn_its_own_answer_back(tmp_path, monkeypatch):
    """jobbot's own LLM text for this employer must never be cached and replayed at the next one."""
    q = "Why do you want to work at Acme?"
    seen, resolver = _seen_fn(tmp_path, monkeypatch, FACTS, {}, answered=[q])
    seen("Because Acme's platform work matches my agent background.", q)
    assert normalize_question(q) not in resolver.answers


def test_does_not_learn_a_value_that_came_from_facts(tmp_path, monkeypatch):
    seen, resolver = _seen_fn(tmp_path, monkeypatch, FACTS, {})
    seen("j@example.com", "Email")
    seen("Jawad Amir", "Full name")
    assert resolver.answers == {}, "facts.yaml already answers these; caching them only invites fuzzy mis-hits"


def test_does_not_relearn_something_already_cached(tmp_path, monkeypatch):
    q = "How many years of Python?"
    seen, resolver = _seen_fn(tmp_path, monkeypatch, FACTS, {normalize_question(q): "6"})
    seen("9", q)
    assert resolver.answers[normalize_question(q)] == "6", "the cached answer wins over a stale field value"


# ---------- generic form adapter ----------
def test_apply_context_switches_the_page_and_notifies_its_owner():
    """A popup becomes the application page for both the adapter and the runner that owns the browser."""
    from jobbot.apply.base import ApplyContext

    original = object()
    popup = object()
    adopted: list[object] = []
    ctx = ApplyContext(job={}, page=original, facts={}, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "",
                       on_page_change=adopted.append)

    ctx.switch_page(popup)

    assert ctx.page is popup
    assert adopted == [popup]


def test_runner_tracks_the_page_an_adapter_switches_to():
    """Screenshots and pause/resume must follow the popup instead of the posting left behind."""
    from jobbot.apply import runner

    class _Page:
        def __init__(self): self.timeout = None
        def set_default_timeout(self, value): self.timeout = value

    original = _Page()
    popup = _Page()
    live = {"page": original}
    page_ref = {"page": original}

    runner._page_change_handler(live, page_ref)(popup)

    assert live["page"] is popup
    assert page_ref["page"] is popup
    assert popup.timeout == 8000


def test_generic_open_form_continues_on_the_page_an_opener_created(monkeypatch):
    """After an Apply popup is adopted, every form probe must move to it immediately."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext
    from jobbot.apply.generic import GenericFormAdapter

    class _Page:
        frames = []
        url = "https://careers.example/jobs/1"
        def is_closed(self): return False

    posting = _Page()
    popup = _Page()
    popup.url = "https://ats.example/jobs/1/apply"
    ctx = ApplyContext(job={"url": posting.url}, page=posting, facts={}, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "")
    adapter = GenericFormAdapter()

    monkeypatch.setattr(c, "detect_captcha", lambda *_a, **_k: False)
    monkeypatch.setattr(c, "detect_bot_block", lambda *_a, **_k: False)
    monkeypatch.setattr(adapter, "_refuse_redirect_home", lambda *_: None)
    monkeypatch.setattr(adapter, "_form_visible", lambda page, *_: page is popup)
    monkeypatch.setattr(adapter, "_form_in_frame", lambda *_: False)
    monkeypatch.setattr(adapter, "_enter_iframe", lambda *_: False)
    monkeypatch.setattr(adapter, "_click_opener", lambda current: current.switch_page(popup) or True)

    adapter._open_form(ctx)

    assert ctx.page is popup


def test_generic_open_form_reuses_an_existing_application_popup(monkeypatch):
    """A retry on a parked LG run adopts its Dayforce page instead of opening another duplicate."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext
    from jobbot.apply.generic import GenericFormAdapter

    class _Context:
        pages = []

    class _Page:
        frames = []
        context = _Context()
        def __init__(self, url): self.url = url
        def is_closed(self): return False

    posting = _Page("https://globalcareers.example/jobs/1")
    popup = _Page("https://ats.example/jobs/1/apply")
    posting.context.pages = [posting, popup]
    adopted = []
    ctx = ApplyContext(job={"url": posting.url}, page=posting, facts={}, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "",
                       on_page_change=adopted.append)
    adapter = GenericFormAdapter()

    monkeypatch.setattr(c, "detect_captcha", lambda *_a, **_k: False)
    monkeypatch.setattr(c, "detect_bot_block", lambda *_a, **_k: False)
    monkeypatch.setattr(adapter, "_refuse_redirect_home", lambda *_: None)
    monkeypatch.setattr(adapter, "_form_visible", lambda page, *_: page is popup)
    monkeypatch.setattr(adapter, "_form_in_frame", lambda *_: False)
    monkeypatch.setattr(adapter, "_click_opener", lambda *_: pytest.fail("must reuse the open popup"))

    adapter._open_form(ctx)

    assert ctx.page is popup
    assert adopted == [popup]


def test_generic_identity_labels_map_to_facts():
    """Label -> facts key is the whole basis of the generic adapter; the ordering traps are what break it."""
    from jobbot.apply.generic import GenericFormAdapter as G
    cases = {
        "First name": "identity.first_name",
        "Given Name *": "identity.first_name",
        "Last name": "identity.last_name",
        "Surname": "identity.last_name",
        "Full name": "identity.full_name",
        "Name": "identity.full_name",
        "Email address": "identity.email",
        "E-mail": "identity.email",
        "Mobile phone": "identity.phone",
        "LinkedIn profile": "identity.linkedin",
        "GitHub": "identity.github",
        "Personal website": "identity.portfolio",
        "Current employer": "work.current_company",
        "Current job title": "work.current_title",
        "City": "identity.location",
    }
    for label, key in cases.items():
        assert G._identity_key(label) == key, label


def test_generic_skips_middle_name_but_asks_about_unknown_labels():
    """'' means a field to leave alone, None means a question for the resolver — they must not collapse."""
    from jobbot.apply.generic import GenericFormAdapter as G
    assert G._identity_key("Middle name") == ""
    assert G._identity_key("Why do you want this job?") is None
    assert G._identity_key("") is None


def test_generic_first_name_is_not_read_as_full_name():
    """The regex order trap: a bare-name pattern that runs first turns 'First name' into the full name."""
    from jobbot.apply.generic import GenericFormAdapter as G
    assert G._identity_key("First name") == "identity.first_name"
    assert G._identity_key("Last Name") == "identity.last_name"


def test_generic_stops_when_the_page_has_no_form():
    """An unrecognised page is jobbot's failure, not work silently handed back to the candidate."""
    from jobbot.apply.generic import GenericFormAdapter
    from jobbot.apply.base import ApplyContext, ApplyError

    class _NoForm:
        url = "https://careers.example.com/jobs/1"
        def is_closed(self): return False
        def locator(self, *_a, **_k): return _FakeLocator(0, False)
        def get_by_role(self, *_a, **_k): return _FakeLocator(0, False)

    page = _NoForm()
    ctx = ApplyContext(job={}, page=page, facts={}, cv_path="", step=lambda s: None,
                       answer=lambda *a, **k: "", screenshot=lambda l: "")
    with pytest.raises(ApplyError, match="could not find an application form"):
        GenericFormAdapter().apply(ctx)


def test_generic_missing_next_or_submit_is_an_automation_failure(monkeypatch):
    """A changed site is jobbot's bug; it must not ask the candidate to finish the page manually."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext, ApplyError
    from jobbot.apply.generic import GenericFormAdapter

    class _Page:
        url = "https://ats.example/jobs/1/apply"
        def is_closed(self): return False

    page = _Page()
    ctx = ApplyContext(job={}, page=page, facts={}, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "")
    adapter = GenericFormAdapter()
    monkeypatch.setattr(c, "dismiss_cookie_banner", lambda *_: False)
    monkeypatch.setattr(c, "detect_captcha", lambda *_a, **_k: False)
    monkeypatch.setattr(c, "detect_bot_block", lambda *_a, **_k: False)
    monkeypatch.setattr(adapter, "_open_form", lambda *_: None)
    monkeypatch.setattr(adapter, "_form_root", lambda current: current)
    monkeypatch.setattr(adapter, "_detect_scope", lambda *_: "form")
    monkeypatch.setattr(adapter, "_fill_page", lambda *_: None)
    monkeypatch.setattr(adapter, "_button", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter, "_signature", lambda *_: "page-one")
    monkeypatch.setattr(adapter, "_press", lambda *_a, **_k: False)

    with pytest.raises(ApplyError, match="neither a Submit nor a Next"):
        adapter.apply(ctx)


def test_generic_stuck_next_step_is_an_automation_failure(monkeypatch):
    """A Next button that does nothing without a validation error is not a task for the candidate."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext, ApplyError
    from jobbot.apply.generic import GenericFormAdapter

    class _Page:
        url = "https://ats.example/jobs/1/apply"
        def is_closed(self): return False
        def wait_for_timeout(self, _ms): pass

    page = _Page()
    ctx = ApplyContext(job={}, page=page, facts={}, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "")
    adapter = GenericFormAdapter()
    monkeypatch.setattr(c, "dismiss_cookie_banner", lambda *_: False)
    monkeypatch.setattr(c, "detect_captcha", lambda *_a, **_k: False)
    monkeypatch.setattr(c, "detect_bot_block", lambda *_a, **_k: False)
    monkeypatch.setattr(c, "submit_blocked_message", lambda *_: "")
    monkeypatch.setattr(c, "form_errors", lambda *_: [])
    monkeypatch.setattr(adapter, "_open_form", lambda *_: None)
    monkeypatch.setattr(adapter, "_form_root", lambda current: current)
    monkeypatch.setattr(adapter, "_detect_scope", lambda *_: "form")
    monkeypatch.setattr(adapter, "_fill_page", lambda *_: None)
    monkeypatch.setattr(adapter, "_button", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter, "_signature", lambda *_: "page-one")
    monkeypatch.setattr(adapter, "_press", lambda *_a, **_k: True)
    monkeypatch.setattr(adapter, "_confirmed", lambda *_: False)

    with pytest.raises(ApplyError, match="pressed Next but the page did not move"):
        adapter.apply(ctx)


def test_workday_keeps_sessions_per_company():
    """Workday accounts are per tenant: a shared session would send one employer's cookies to another."""
    assert get_adapter_for("workday").needs_account is False
    assert get_adapter_for("linkedin").needs_account is True


def test_generic_identity_pass_reads_real_controls(monkeypatch):
    """_identity walks _text_controls; the first version called a helper that did not exist, and no test
    reached it because they all stopped at the no-form guard."""
    from jobbot.apply.generic import GenericFormAdapter
    from jobbot.apply.base import ApplyContext
    filled: list[tuple[str, str]] = []

    monkeypatch.setattr(GenericFormAdapter, "_text_controls",
                        staticmethod(lambda _page, _scope="form": [("EL_EMAIL", "Email address"),
                                                                   ("EL_MIDDLE", "Middle name"),
                                                                   ("EL_WHY", "Why this role?")]))
    monkeypatch.setattr("jobbot.apply.common.fill_verified",
                        lambda el, value: filled.append((el, value)) or True)

    ctx = ApplyContext(job={}, page=object(), facts=FACTS, cv_path="", step=lambda s: None,
                       answer=lambda *a, **k: pytest.fail("identity pass must not ask"), screenshot=lambda l: "")
    GenericFormAdapter()._identity(ctx)
    assert filled == [("EL_EMAIL", "j@example.com")], "middle name skipped, free text left to _questions"


def test_generic_never_asks_about_a_field_it_means_to_skip(monkeypatch):
    """'' (skip) and None (unknown) both read as falsy, so a truthiness check asked about middle names."""
    from jobbot.apply.generic import GenericFormAdapter as G
    assert G._identity_key("Middle name") == ""
    # the guard _questions uses, spelled out: skip when a key exists at all, including the empty one
    assert (G._identity_key("Middle name") is not None) is True
    assert (G._identity_key("Why this role?") is not None) is False


def test_captcha_detected_from_its_prompt_wording():
    """Zoho Recruit ships no captcha widget class or iframe — only the words next to the box."""
    from jobbot.apply.common import detect_captcha

    class _Page:
        def __init__(self, hit): self.hit = hit
        def evaluate(self, js): return self.hit

    assert detect_captcha(_Page(True), raise_=False) is True
    assert detect_captcha(_Page(False), raise_=False) is False


class _FormPage:
    """Counts what _form_visible asks for. `files` are attached-but-maybe-hidden; `fields` are visible."""
    def __init__(self, has_form=True, fields=0, files=0):
        self.has_form, self.fields, self.files = has_form, fields, files

    def wait_for_selector(self, _sel, **_k):
        if not self.has_form:
            raise RuntimeError("no form")

    def locator(self, sel):
        from jobbot.apply.generic import FORM_FILE
        return _FakeLocator(self.files if sel == FORM_FILE else self.fields, False)

    def evaluate(self, script="", arg=None):
        # Control counting moved into the page (it has to ignore the site's own search and newsletter
        # boxes, which no count of locators can tell apart). The DOM-dependent half of these rules is
        # covered for real in tests/test_forms_browser.py; this fake only keeps the arithmetic honest.
        return False if "names" in (script or "") else self.fields


def test_form_visible_needs_more_than_a_hidden_file_input():
    """The regression: boards keep a hidden file input in the page long before the form opens. Trusting it
    alone reported 'form found' on a teaser page, so the Apply button was never clicked and the real form
    never appeared."""
    from jobbot.apply.generic import GenericFormAdapter as G
    assert G._form_visible(_FormPage(fields=0, files=1)) is False, "hidden file input alone proves nothing"
    assert G._form_visible(_FormPage(fields=1, files=1)) is True, "CV upload plus a field is an application"
    assert G._form_visible(_FormPage(fields=3, files=0)) is True, "three visible fields is a real form"
    assert G._form_visible(_FormPage(fields=2, files=0)) is False, "a search box is not an application"
    assert G._form_visible(_FormPage(has_form=False, fields=9)) is False, "no <form> at all"


# ---------- the account jobbot creates for itself ----------
@pytest.fixture
def keychain(tmp_path, monkeypatch):
    """An isolated keychain + fallback file, so these never read or write the real one."""
    from jobbot import credentials
    store: dict[str, str] = {}
    monkeypatch.setattr(config, "get_secret", lambda n: store.get(n))
    monkeypatch.setattr(config, "set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr(credentials, "FALLBACK_PATH", tmp_path / ".account_password")
    return store


def test_account_password_is_generated_once_and_reused(keychain):
    """The whole point of a managed password: an account created at one employer today has to be signable
    into next week, which only works if the password never changes underneath it."""
    from jobbot import credentials

    first = credentials.account_password()
    assert credentials.meets_requirements(first)
    assert keychain[credentials.SECRET_NAME] == first
    assert credentials.account_password() == first, "a second employer must get the same password"


def test_generated_password_satisfies_the_rules_workday_prints():
    """8+ characters with an upper, a lower, a number and a special — the strictest list we have met."""
    from jobbot import credentials

    for _ in range(50):
        pw = credentials.generate()
        assert credentials.meets_requirements(pw), pw
    assert not credentials.meets_requirements("alllowercase123")
    assert not credentials.meets_requirements("Sh0rt!")


def test_account_password_keeps_the_one_already_in_use(keychain):
    """Upgrading from the hand-set JOBBOT_WORKDAY_PASSWORD must not mint a new password: the accounts it
    already created at real employers would become unreachable."""
    from jobbot import credentials

    keychain["JOBBOT_WORKDAY_PASSWORD"] = "TheOneAlready!nUse9"
    assert credentials.account_password() == "TheOneAlready!nUse9"
    assert credentials.SECRET_NAME not in keychain, "the old secret is used where it is, not copied over"


def test_account_password_survives_a_keychain_that_refuses(keychain, monkeypatch):
    """A locked or missing keyring must not mean a password that vanishes at the end of the run."""
    from jobbot import credentials

    def refuse(_n, _v):
        raise RuntimeError("no keyring")
    monkeypatch.setattr(config, "set_secret", refuse)

    password = credentials.account_password()
    assert credentials.FALLBACK_PATH.read_text().strip() == password
    assert oct(credentials.FALLBACK_PATH.stat().st_mode)[-3:] == "600"
    assert credentials.stored_password() == password, "and it is found again on the next application"


# ---------- Workday: the signup the run does for itself ----------
class _WdEl:
    """One Workday control. Enough of the Playwright element surface for common.py's helpers."""
    def __init__(self, page, key, typ="text", visible=True, value="", checked=False):
        self.page, self.key, self.typ, self._visible = page, key, typ, visible
        self.value, self.checked = value, checked

    # locator surface
    def count(self): return 1
    @property
    def first(self): return self
    def nth(self, _i): return self
    def is_visible(self): return self._visible
    def wait_for(self, state="visible", timeout=0):
        if not self._visible:
            raise RuntimeError("not visible")

    # element surface
    def scroll_into_view_if_needed(self, timeout=0): pass
    def click(self, timeout=0, force=False):
        self.page.clicks.append(self.key)
        self.page.after_click(self.key)
    def fill(self, value, timeout=0):
        self.value = value
        self.page.filled[self.key] = value
    def input_value(self): return self.value
    def get_attribute(self, name): return self.typ if name == "type" else None
    def is_checked(self): return self.checked
    def check(self, timeout=0):
        self.checked = True
        self.page.clicks.append(f"check:{self.key}")
    def evaluate(self, js, *_a):
        if "tagName" in js:
            return "INPUT"
        if "__reactProps" in js:
            return None
        return ""


class _WdGroup:
    """A locator matching several controls (every password box, say)."""
    def __init__(self, els): self.els = els
    def count(self): return len(self.els)
    def nth(self, i): return self.els[i]
    @property
    def first(self): return self.els[0] if self.els else _WdEl(None, "none", visible=False)


class _WorkdayPage:
    """A Workday screen described by the automation ids it shows. Nothing else exists on it."""
    # Pressing these takes Workday to the next screen, which is how the adapter knows the signup worked:
    # the password boxes are gone. A page built with advance_on=() is one that rejected what it was given.
    ADVANCE_ON = ("createAccountSubmitButton", "signInSubmitButton")

    def __init__(self, aids, body="", password_boxes=0, checkboxes=0, url="https://x.myworkdayjobs.com/j",
                 advance_on=ADVANCE_ON):
        self.url, self.body, self.advance_on = url, body, advance_on
        self.clicks: list[str] = []
        self.filled: dict[str, str] = {}
        self.els = {aid: _WdEl(self, aid) for aid in aids}
        self.passwords = [_WdEl(self, f"pw{i}", typ="password") for i in range(password_boxes)]
        self.checkboxes = [_WdEl(self, f"cb{i}", typ="checkbox") for i in range(checkboxes)]

    def locator(self, sel):
        if sel == "input[type=password]":
            return _WdGroup(self.passwords)
        if sel.startswith("input[type=checkbox]"):
            return _WdGroup(self.checkboxes)
        if sel == "input[type=file]":
            return _WdGroup([])
        for aid, el in self.els.items():
            if f"'{aid}'" in sel:
                return el
        if "formField" in sel and self.els:
            return _WdGroup([])
        return _WdGroup([])

    def after_click(self, key):
        """Leave the account screen once its button is pressed, as the real page does."""
        if key in self.advance_on:
            self.passwords = []
            self.els = {"bottom-navigation-next-button": _WdEl(self, "bottom-navigation-next-button")}

    def inner_text(self, _sel="body"): return self.body
    def evaluate(self, _js, *_a): return self.body
    def wait_for_timeout(self, _ms): pass
    def wait_for_selector(self, _sel, **_k): return None
    def is_closed(self): return False
    def get_by_text(self, *_a, **_k): return _WdGroup([])
    def get_by_role(self, *_a, **_k): return _WdGroup([])


def _wd_ctx(page, answer=None):
    from jobbot.apply.base import ApplyContext
    return ApplyContext(job={"ats": "workday", "url": page.url, "company": "Salesforce"}, page=page,
                        facts=FACTS, cv_path="/tmp/cv.pdf", step=lambda s: None,
                        answer=answer or (lambda *a, **k: pytest.fail("must not ask the user")),
                        screenshot=lambda l: "")


def test_workday_create_account_page_is_not_the_application_form():
    """The regression behind "Needs your answer: Password".

    Workday builds Create Account out of the same `formField` wrappers as the application, so _on_form said
    we were already inside it, _account was skipped, and the walker went on to treat the password box as a
    screening question — the run stopped and asked the user to invent a password.
    """
    from jobbot.apply.workday import WorkdayAdapter

    signup = _WorkdayPage(["createAccountSubmitButton"], password_boxes=2)
    assert WorkdayAdapter._on_account_page(signup) is True
    assert WorkdayAdapter._on_form(signup) is False


def test_workday_creates_the_account_without_asking(keychain):
    """Email, password and its confirmation filled, terms ticked, submit pressed — and ctx.answer untouched."""
    from jobbot import credentials
    from jobbot.apply.workday import WorkdayAdapter

    page = _WorkdayPage(["email", "password", "verifyPassword", "createAccountSubmitButton"],
                        password_boxes=2, checkboxes=1)
    WorkdayAdapter()._account(_wd_ctx(page))

    password = credentials.stored_password()
    assert page.filled["email"] == FACTS["identity"]["email"]
    assert page.filled["password"] == password
    assert page.filled["verifyPassword"] == password, "the confirmation must match, or Workday rejects it"
    assert "check:cb0" in page.clicks and "createAccountSubmitButton" in page.clicks


def test_workday_says_whose_account_it_is_when_the_password_is_not_ours(keychain):
    """An account that exists with someone else's password is the one case a person has to settle, so the
    message names the email and where to read jobbot's own password."""
    from jobbot.apply.workday import WorkdayAdapter

    # Workday's own wording, which says neither "incorrect password" nor "account exists"
    page = _WorkdayPage(["email", "password", "signInSubmitButton"], password_boxes=1, advance_on=(),
                        body="You may have entered the wrong email address or password or your account "
                             "might be locked.")
    with pytest.raises(NeedsHuman) as e:
        WorkdayAdapter()._account(_wd_ctx(page))
    assert FACTS["identity"]["email"] in e.value.reason
    assert "Forgot your password" in e.value.reason, "the way out that leaves jobbot able to get in next time"
    assert page.clicks.count("signInSubmitButton") == 1, "one rejected sign-in is enough to know"


def test_workday_takes_the_autofill_route_when_it_is_offered(monkeypatch):
    """The CV fills employment history and education — pages of dates jobbot would otherwise ask about."""
    from jobbot.apply import common as c
    from jobbot.apply.workday import WorkdayAdapter

    uploaded: list[str] = []
    monkeypatch.setattr(c, "upload_resume", lambda page, cv, fi=None: bool(uploaded.append(cv)) or True)

    both = _WorkdayPage(["autofillWithResume", "applyManually", "continueButton"])
    WorkdayAdapter()._choose_route(_wd_ctx(both))
    assert both.clicks[0] == "autofillWithResume", "resume route first, always"
    assert uploaded == ["/tmp/cv.pdf"] and "continueButton" in both.clicks

    manual_only = _WorkdayPage(["applyManually"])
    WorkdayAdapter()._choose_route(_wd_ctx(manual_only))
    assert manual_only.clicks == ["applyManually"]


def test_workday_never_offers_the_last_application_route():
    """'Use My Last Application' copies an application jobbot cannot read, so it stays out of the routes."""
    from jobbot.apply.workday import APPLY_ROUTES
    assert "useMyLastApplication" not in APPLY_ROUTES
    assert APPLY_ROUTES[0] == "autofillWithResume"


# ---------- password boxes are credentials, never questions ----------
def test_fill_account_password_fills_every_empty_box_with_the_same_value(keychain):
    from jobbot import credentials
    from jobbot.apply import common as c

    page = _WorkdayPage([], password_boxes=3)
    page.passwords[2].value = "typed by the user"
    assert c.fill_account_password(page) is True
    password = credentials.stored_password()
    assert page.passwords[0].value == page.passwords[1].value == password
    assert page.passwords[2].value == "typed by the user", "a box already filled is left alone"


def test_a_one_time_code_is_not_treated_as_an_account_password():
    """It is a question for the user (the code is in their inbox), so it must not be quietly filled."""
    from jobbot.apply import common as c

    page = _WorkdayPage([], password_boxes=1)
    box = page.passwords[0]
    assert c.is_password_control(box, "Password") is True
    assert c.is_password_control(box, "Verify New Password") is True
    assert c.is_password_control(box, "One-time password") is False
    assert c.is_password_control(box, "Verification code") is False


def test_generic_walker_skips_password_controls(monkeypatch):
    """The walker asked the resolver about every control it could not map — including the signup password,
    which is how a Workday run ended up asking the user for one."""
    from jobbot.apply.generic import GenericFormAdapter
    from jobbot.apply.base import ApplyContext

    page = _WorkdayPage([])
    page.els = {}
    controls = _WdGroup([_WdEl(page, "pw", typ="password"), _WdEl(page, "why", typ="text")])
    monkeypatch.setattr(page, "locator", lambda _sel: controls)
    asked: list[str] = []
    monkeypatch.setattr("jobbot.apply.common.get_label_for", lambda el: "Password" if el.typ == "password"
                        else "Why this role?")
    monkeypatch.setattr("jobbot.apply.common.answer_and_set",
                        lambda ctx, el, label, kind, options=None, container=None: asked.append(label))

    ctx = ApplyContext(job={}, page=page, facts=FACTS, cv_path="", step=lambda s: None,
                       answer=lambda *a, **k: pytest.fail("must not ask"), screenshot=lambda l: "")
    GenericFormAdapter()._questions(ctx)
    assert asked == ["Why this role?"]


def test_autofill_offer_must_name_the_cv():
    """"Autofill with LinkedIn" opens an OAuth dance with the user's LinkedIn account; only the CV offers
    are ours to take."""
    from jobbot.apply.common import AUTOFILL_NAMES

    assert AUTOFILL_NAMES.search("Autofill with Resume")
    assert AUTOFILL_NAMES.search("Autofill from resume")
    assert AUTOFILL_NAMES.search("Fill application with CV")
    assert not AUTOFILL_NAMES.search("Autofill with LinkedIn")
    assert not AUTOFILL_NAMES.search("Apply manually")


def test_workday_signs_in_when_the_signup_bounces_to_a_sign_in_card(keychain):
    """Salesforce's tenant answers a successful signup with an empty Sign In card and no message at all.

    Reading the outcome once made that look like a failed signup, and the run stopped with "Workday would
    not let jobbot past the sign-in" on an account it had just created. Each attempt re-reads the page.
    """
    from jobbot.apply.workday import WorkdayAdapter

    page = _WorkdayPage(["email", "password", "verifyPassword", "createAccountSubmitButton"],
                        password_boxes=2, checkboxes=1, advance_on=())

    def bounce_to_sign_in(key):
        if key == "createAccountSubmitButton":      # account made; Workday asks you to sign in with it
            page.els = {"email": _WdEl(page, "email"), "password": _WdEl(page, "password"),
                        "signInSubmitButton": _WdEl(page, "signInSubmitButton")}
            page.passwords = [_WdEl(page, "pw0", typ="password")]
            page.filled.clear()
        elif key == "signInSubmitButton":
            page.passwords = []                     # signed in: the application form is next
            page.els = {"bottom-navigation-next-button": _WdEl(page, "bottom-navigation-next-button")}
    page.after_click = bounce_to_sign_in

    WorkdayAdapter()._account(_wd_ctx(page))
    assert "signInSubmitButton" in page.clicks, "the bounce is signed into, not reported as a failure"
    assert page.filled["password"] == credentials_password()


def credentials_password() -> str:
    from jobbot import credentials
    return credentials.stored_password()


def test_workday_create_account_switch_is_proven_by_the_second_password_box():
    """A "Create Account" control that is a tab on one tenant and a link on another: the click proves
    nothing, so a sign-in card would have been filled in as though it were a signup."""
    from jobbot.apply.workday import WorkdayAdapter

    sign_in_only = _WorkdayPage(["email", "password", "createAccountLink", "signInSubmitButton"],
                                password_boxes=1)
    assert WorkdayAdapter()._to_create_account(sign_in_only) is False
    assert WorkdayAdapter._is_create_form(_WorkdayPage(["verifyPassword"], password_boxes=2)) is True


def test_workday_clicks_are_forced_past_the_click_shield():
    """Workday marks its real buttons aria-hidden and covers each with a transparent `click_filter` div.
    Playwright refuses a click another element would intercept, and the shield itself sends no request — so
    the polite click is tried once and then forced, and the shield is never a target of its own."""
    from jobbot.apply.workday import ACCOUNT_SUBMIT, CLICK_SHIELD, WorkdayAdapter

    assert CLICK_SHIELD not in ACCOUNT_SUBMIT

    class _Shielded(_WdEl):
        def __init__(self, page, key):
            super().__init__(page, key)
            self.attempts = []
        def click(self, timeout=0, force=False):
            self.attempts.append(force)
            if not force:
                raise RuntimeError("intercepts pointer events")
            self.page.clicks.append(self.key)
            self.page.after_click(self.key)

    page = _WorkdayPage([])
    shielded = _Shielded(page, "createAccountSubmitButton")
    page.els = {"createAccountSubmitButton": shielded}
    assert WorkdayAdapter._click_first(page, ACCOUNT_SUBMIT) is True
    assert shielded.attempts == [False, True], "polite click first, then forced"
    assert page.clicks == ["createAccountSubmitButton"]


def test_honeypot_fields_are_left_alone():
    """Salesforce's Workday signup carries "Enter website. This input is for robots only, do not enter if
    you're human." The walker's website rule would have filled it with the portfolio URL and had the whole
    application thrown away as bot traffic."""
    from jobbot.apply import common as c

    class _El:
        def __init__(self, hidden=None, box=None): self.hidden, self.box = hidden, box
        def get_attribute(self, name): return self.hidden if name == "aria-hidden" else None
        def bounding_box(self): return self.box
        def evaluate(self, _js, *_a): return ""

    real = _El(box={"width": 320, "height": 40})
    assert c.is_honeypot(real, "Enter website. This input is for robots only, do not enter if you're human.")
    assert c.is_honeypot(real, "Leave this field blank")
    assert c.is_honeypot(_El(hidden="true", box={"width": 320, "height": 40}), "Website")
    assert c.is_honeypot(_El(box={"width": 1, "height": 1}), "Website")
    assert not c.is_honeypot(real, "Website")
    assert not c.is_honeypot(real, "Portfolio URL")


def test_workday_answers_the_sign_in_chooser_with_email(keychain):
    """Cloudera's tenant opens on "Sign in with Google / Sign in with email" — a screen with no fields at
    all, which read as neither the account page nor the form, so the walker treated it as a step, filled
    nothing and stopped at "no Next button"."""
    from jobbot.apply.workday import SSO_AUTH, WorkdayAdapter

    page = _WorkdayPage(["SignInWithEmailButton", "GoogleSignInButton"])

    def reveal_the_form(key):
        if key == "SignInWithEmailButton":
            page.els = {"email": _WdEl(page, "email"), "password": _WdEl(page, "password"),
                        "verifyPassword": _WdEl(page, "verifyPassword"),
                        "createAccountSubmitButton": _WdEl(page, "createAccountSubmitButton")}
            page.passwords = [_WdEl(page, "pw0", typ="password"), _WdEl(page, "pw1", typ="password")]
        elif key == "createAccountSubmitButton":
            page.passwords = []
            page.els = {"bottom-navigation-next-button": _WdEl(page, "bottom-navigation-next-button")}
    page.after_click = reveal_the_form

    assert WorkdayAdapter._on_account_page(page) is True, "a chooser is a credentials screen"
    assert WorkdayAdapter._on_form(page) is False
    WorkdayAdapter()._account(_wd_ctx(page))
    assert page.clicks[0] == "SignInWithEmailButton"
    assert not any(aid in page.clicks for aid in SSO_AUTH), "the user's Google account is never signed into"
    assert page.filled["email"] == FACTS["identity"]["email"]


def test_identity_dropdowns_are_answered_rather_than_skipped(monkeypatch):
    """_identity fills text boxes only, and _questions used to skip every identity-labelled control — so a
    required Country dropdown (Workday has one on My Information) was left empty and the step never moved."""
    from jobbot.apply.generic import GenericFormAdapter
    from jobbot.apply.base import ApplyContext

    class _Ctl:
        def __init__(self, tag, label, typ=""):
            self.tag, self.label, self.typ = tag, label, typ
        def is_visible(self): return True
        def get_attribute(self, name): return {"type": self.typ, "role": ""}.get(name)
        def evaluate(self, js, *_a): return self.tag.upper() if "tagName" in js else ""

    controls = [_Ctl("select", "Country"), _Ctl("input", "Email address"), _Ctl("input", "Middle name")]

    class _Page:
        def locator(self, _sel): return _WdGroup(controls)
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr("jobbot.apply.common.get_label_for", lambda el: el.label)
    monkeypatch.setattr("jobbot.apply.common.is_honeypot", lambda el, label="": False)
    monkeypatch.setattr("jobbot.apply.common.answer_and_set",
                        lambda ctx, el, label, kind, options=None, container=None: asked.append((label, kind)))

    ctx = ApplyContext(job={}, page=_Page(), facts=FACTS, cv_path="", step=lambda s: None,
                       answer=lambda *a, **k: "", screenshot=lambda l: "")
    GenericFormAdapter()._questions(ctx)
    assert asked == [("Country", "select")], "the dropdown is answered; the text email and middle name are not"


def test_country_field_gets_the_country_not_the_city_line():
    from jobbot.apply.generic import GenericFormAdapter as G

    assert G._identity_key("Country") == "identity.country"
    assert G._identity_key("Country of residence") == "identity.country"
    assert G._identity_key("Country/Region") == "identity.country"
    assert G._identity_key("City") == "identity.location"
    assert G._identity_key("Country phone code") != "identity.country", "not a country name field"


def test_workday_steps_are_walked_outside_a_form_element():
    """A Workday step has no <form>: its fields live in the applyFlowPage container. Scoped to `form`, the
    walker found nothing, filled nothing, and pressed Save and Continue on an empty step — which came back
    as "The field Given Name(s) - Western Script is required and must have a value"."""
    from jobbot.apply.generic import GenericFormAdapter
    from jobbot.apply.workday import STEP_SCOPES, WorkdayAdapter

    class _ScopedPage:
        """Only the applyFlowPage scope matches anything here, as on a real Workday step."""
        def __init__(self, hits): self.hits, self.asked = hits, []
        def locator(self, sel):
            self.asked.append(sel)
            return _WdGroup([_WdEl(self, "ctl")] if sel.startswith(self.hits) else [])
        def after_click(self, _k): pass

    page = _ScopedPage(STEP_SCOPES[0])
    assert WorkdayAdapter._step_walker(page).scope == STEP_SCOPES[0]
    assert GenericFormAdapter().scope == "form", "plain boards keep the safe default"

    # a page where nothing matches still gets a usable scope rather than silently walking nothing
    assert WorkdayAdapter._step_walker(_ScopedPage("no-such-scope")).scope == STEP_SCOPES[-1]


def test_a_stuck_workday_step_names_the_field_it_is_stuck_on(monkeypatch):
    """"probably asking for something jobbot could not fill" sent the user to go and look, when the page
    already said which field was empty."""
    from jobbot.apply import common as c
    from jobbot.apply.workday import WorkdayAdapter

    page = _WorkdayPage(["bottom-navigation-next-button"])
    monkeypatch.setattr(c, "detect_captcha", lambda p, raise_=True: False)
    monkeypatch.setattr(c, "form_errors", lambda p: [
        "The field How Did You Hear About Us? is required and must have a value."])
    monkeypatch.setattr(WorkdayAdapter, "_fill_step", lambda self, ctx: None)
    monkeypatch.setattr(WorkdayAdapter, "_heading", staticmethod(lambda p: "My Information"))

    with pytest.raises(NeedsHuman) as e:
        WorkdayAdapter()._walk_steps(_wd_ctx(page))
    assert "My Information" in e.value.reason and "How Did You Hear About Us?" in e.value.reason


def test_workday_waits_for_a_step_to_stop_rendering():
    """Workday paints the shell first and the fields a second later. Reading between the two saw a step
    with nothing on it, filled nothing, and pressed Save and Continue on an empty form."""
    from jobbot.apply.workday import WorkdayAdapter

    class _Growing:
        """0 controls, then 3, then 3 — a step arriving the way a real one does."""
        def __init__(self): self.counts, self.samples = iter([0, 3, 3, 3, 3]), 0
        def locator(self, _sel):
            self.samples += 1
            return _WdGroup([object()] * next(self.counts, 3))
        def wait_for_timeout(self, _ms): pass

    page = _Growing()
    WorkdayAdapter._wait_for_fields(page)
    assert page.samples == 3, "returns as soon as the count repeats, not on the first look"


def test_workday_prompt_options_ignore_what_is_already_chosen():
    """The chips showing a made choice carry the same markup as the options, so the phone code's
    "Bangladesh (+880)" was offered as an answer to "How did you hear about us"."""
    from jobbot.apply.workday import WorkdayAdapter

    class _Opt:
        def __init__(self, text, chosen): self.text, self.chosen = text, chosen
        def inner_text(self): return self.text
        def evaluate(self, _js, *_a): return self.chosen

    options = [_Opt("Bangladesh (+880)", True), _Opt("Job Board", False), _Opt("Website", False)]

    class _Page:
        def locator(self, _sel): return _WdGroup(options)

    assert [t for t, _ in WorkdayAdapter._prompt_options(_Page())] == ["Job Board", "Website"]


def test_workday_prompt_is_answered_a_level_at_a_time(keychain):
    """Typing into a Workday prompt filters nothing: "Job Board" has to be opened before "LinkedIn Jobs"
    exists to click. The resolver picks at each level, which is how the source policy lands on LinkedIn."""
    from jobbot.apply.base import ApplyContext
    from jobbot.apply.workday import WorkdayAdapter

    levels = iter([["Contractor, Consultant, Intern, Vendor", "Job Board", "Recruiting Event", "Website"],
                   ["Glassdoor", "Indeed", "LinkedIn Jobs", "Naukri"]])
    state = {"options": next(levels), "chosen": None}

    class _Opt:
        def __init__(self, text): self.text = text
        def inner_text(self): return self.text
        def evaluate(self, _js, *_a): return False
        def click(self, timeout=0):
            state["chosen"] = self.text
            nxt = next(levels, None)
            state["options"] = nxt if nxt is not None else []

    class _Box:
        def click(self, timeout=0): pass
        def evaluate(self, _js, *_a): return state["options"] == []      # chosen once the list is gone

    class _Page:
        def locator(self, _sel): return _WdGroup([_Opt(t) for t in state["options"]])
        def wait_for_timeout(self, _ms): pass

    asked = []
    def answer(question, options=None, kind="text"):
        asked.append(options)
        return Resolver(FACTS, {}, JOB, llm_enabled=False).answer(question, options, kind)

    ctx = ApplyContext(job=JOB, page=_Page(), facts=FACTS, cv_path="", step=lambda s: None,
                       answer=answer, screenshot=lambda l: "")
    WorkdayAdapter()._fill_prompt(ctx, _Box(), "How Did You Hear About Us?")
    assert state["chosen"] == "LinkedIn Jobs"
    assert len(asked) == 2, "one question per level of the tree"


def test_an_optional_question_with_no_answer_is_left_blank_not_asked():
    """Workday's My Information offers an optional Postal Code. The resolver will never invent one — it is
    in NEVER_GUESS for good reason — but stopping the whole application to ask for a field the form does not
    require turns a finished application into a pause."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext

    class _Input:
        def __init__(self, required): self.required, self.value = required, ""
        def evaluate(self, js, *_a):
            if "aria-required" in js:
                return self.required
            return "INPUT" if "tagName" in js else ""
        def get_attribute(self, name): return "text" if name == "type" else None
        def is_visible(self): return True
        def input_value(self): return self.value
        def click(self, timeout=0): pass
        def fill(self, value, timeout=0): self.value = value

    def ask(question, options=None, kind="text"):
        raise NeedsHuman(f"Needs your answer (protected question): {question}", question=question)

    ctx = ApplyContext(job={}, page=object(), facts=FACTS, cv_path="", step=lambda s: None, answer=ask,
                       screenshot=lambda l: "")

    optional = _Input(required=False)
    c.answer_and_set(ctx, optional, "Postal Code", "text")      # must not raise
    assert optional.value == ""

    with pytest.raises(NeedsHuman):
        c.answer_and_set(ctx, _Input(required=True), "Phone Number", "text")


def test_unreadable_controls_count_as_required():
    """Asking one question too many beats submitting a form with a hole in it."""
    from jobbot.apply import common as c

    class _Opaque:
        def evaluate(self, *_a, **_k): raise RuntimeError("detached")
    assert c.is_required(_Opaque()) is True


def test_phone_loses_the_country_code_when_the_form_holds_it_separately():
    """Workday asks for the dial code in its own prompt and then rejects "+8801700000000" in the box beside
    it with "Enter a valid format for Phone Number"."""
    from jobbot.apply import common as c

    assert c.national_phone("+8801700000000", "880") == "1700000000"
    assert c.national_phone("+880 170 000 0000", "880") == "1700000000"
    assert c.national_phone("+8801700000000", "") == "+8801700000000", "no separate code field: send it whole"
    assert c.national_phone("+14155551234", "880") == "+14155551234", "a number that is not ours is untouched"


def test_workday_attaches_the_cv_through_the_dropzone(monkeypatch):
    """My Experience renders "Drop files here or Select files" and creates the <input type=file> only when
    that button is pressed — so the ordinary upload found nothing and the step went on with no CV."""
    from jobbot.apply import common as c
    from jobbot.apply.workday import WorkdayAdapter

    monkeypatch.setattr(c, "upload_resume", lambda page, cv, fi=None: False)   # no plain input on the page
    chosen: list[str] = []

    class _Chooser:
        value = type("FC", (), {"set_files": staticmethod(lambda p: chosen.append(p))})()
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    page = _WorkdayPage(["select-files"])
    page.expect_file_chooser = lambda timeout=0: _Chooser()

    assert WorkdayAdapter()._upload_cv(_wd_ctx(page)) is True
    assert chosen == ["/tmp/cv.pdf"] and "select-files" in page.clicks


def test_workday_picks_up_an_application_it_already_started():
    """Once a draft exists the posting says "Continue Application", not "Apply" — which jobbot itself causes
    on any second run, and which it read as "the posting may be closed"."""
    from jobbot.apply.workday import APPLY_TEXT, WorkdayAdapter

    assert APPLY_TEXT.search("Apply") and APPLY_TEXT.search("Continue Application")
    assert APPLY_TEXT.search("Resume Application")
    assert not APPLY_TEXT.search("Applied Filters"), "anchored, so it does not press whatever says Applied"

    class _Posting:
        """No automation id jobbot knows; the button is found by its accessible name."""
        def __init__(self): self.clicks = []
        def locator(self, _sel): return _WdGroup([])
        def get_by_role(self, role, name=None):
            return _WdEl(self, f"{role}:Continue Application") if role == "link" else _WdGroup([])
        def after_click(self, _k): pass

    page = _Posting()
    assert WorkdayAdapter()._press_apply(page) is True
    assert page.clicks == ["link:Continue Application"]


def test_workday_waits_for_a_slow_save_before_calling_a_step_stuck(monkeypatch):
    """Workday saves a step server-side before rendering the next one; for several seconds the screen still
    shows the step just filled. A flat two-second check called that a validation error and stopped a run
    whose form was complete."""
    from jobbot.apply import common as c
    from jobbot.apply.workday import WorkdayAdapter

    monkeypatch.setattr(c, "form_errors", lambda _p: [])
    states = iter(["Forward Deployed AI Engineer | My Information"] * 4
                  + ["Forward Deployed AI Engineer | My Experience"] * 6)

    class _Saving:
        url = "https://x.myworkdayjobs.com/apply"
        def wait_for_timeout(self, _ms): pass
        def evaluate(self, _js, *_a): return next(states)
    page = _Saving()

    a = WorkdayAdapter()
    before = a._state(page)
    assert "My Information" in before and "Forward Deployed AI Engineer" in before
    assert a._wait_for_move(page, before) is True

    # and a step that comes back with an error is not waited out
    monkeypatch.setattr(c, "form_errors", lambda _p: ["Phone Number is required"])
    page.evaluate = lambda _js, *_a: "Forward Deployed AI Engineer | My Information"
    assert a._wait_for_move(page, a._state(page)) is False


def test_drop_file_hands_the_page_a_real_file(tmp_path):
    """Workday's upload area has no <input type=file> to set and no button that opens a chooser, so the CV
    is dropped on it the way a person dragging it from the desktop would."""
    from jobbot.apply import common as c

    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 fake")
    seen = {}

    class _Zone:
        def evaluate(self, js, payload):
            seen.update(payload)
            seen["js"] = js
            return True

    class _Page:
        def wait_for_timeout(self, _ms): pass

    assert c.drop_file(_Page(), _Zone(), str(cv)) is True
    assert seen["name"] == "cv.pdf" and seen["type"] == "application/pdf"
    assert "DataTransfer" in seen["js"] and "drop" in seen["js"]
    assert c.drop_file(_Page(), _Zone(), str(tmp_path / "missing.pdf")) is False


def test_a_success_announcement_is_not_a_form_error(monkeypatch):
    """Workday announces "<file> successfully uploaded" in the same live region it uses for validation
    messages. Counted as an error, it made a step that had just accepted the CV look stuck on it."""
    from jobbot.apply import common as c

    class _Page:
        def evaluate(self, _js):
            return ["Jawad-Amir-Forward-Deployed-Engineer.pdf successfully uploaded",
                    "Phone Number: Enter a valid format for Phone Number."]

    assert c.form_errors(_Page()) == ["Phone Number: Enter a valid format for Phone Number."]


def test_workday_select_one_buttons_are_answered(keychain):
    """Workday's Yes/No dropdowns are <button aria-haspopup=listbox> reading "Select One", with the question
    on the wrapper around them. Not an input, a select or a combobox — so the walker looked straight past
    five required Application Questions and the step would not move, with nothing logged as skipped."""
    from jobbot.apply.base import ApplyContext
    from jobbot.apply.workday import PLACEHOLDER_RE, WorkdayAdapter

    assert PLACEHOLDER_RE.match("Select One") and PLACEHOLDER_RE.match("")
    assert not PLACEHOLDER_RE.match("Yes")

    state = {"text": "Select One", "options": ["Yes", "No"]}

    class _Opt:
        def __init__(self, text): self.text = text
        def inner_text(self): return self.text
        def evaluate(self, _js, *_a): return False
        def click(self, timeout=0):
            state["text"], state["options"] = self.text, []

    class _Button:
        def inner_text(self): return state["text"]
        def is_visible(self): return True
        def click(self, timeout=0): pass
        def evaluate(self, js, *_a):
            return "Will you now or in the future require sponsorship?*" if "formField" in js else ""

    button = _Button()

    class _Page:
        def locator(self, sel):
            return _WdGroup([button] if "haspopup" in sel else
                            [_Opt(t) for t in state["options"]] if state["options"] else [])
        def wait_for_timeout(self, _ms): pass

    page = _Page()
    asked: list[list[str]] = []

    def answer(question, options=None, kind="text"):
        asked.append(options or [])
        return Resolver(FACTS, {}, JOB, llm_enabled=False).answer(question, options, kind)

    ctx = ApplyContext(job=JOB, page=page, facts=FACTS, cv_path="", step=lambda s: None, answer=answer,
                       screenshot=lambda l: "")
    a = WorkdayAdapter()
    assert a._field_label(button).startswith("Will you now"), "the question, not the placeholder"
    assert a._select_answered(button) is False
    a._prompts(ctx)
    assert state["text"] == "Yes", "sponsorship comes from facts.yaml, which says it is required"
    assert asked == [["Yes", "No"]]


def test_workday_retakes_the_email_route_after_a_bounce(keychain):
    """Cloudera answers a submitted signup by returning to "Sign in with Google / with email". Taking that
    route only once meant the attempts after the bounce typed into a screen with no fields on it."""
    from jobbot.apply.workday import WorkdayAdapter

    chooser = ["SignInWithEmailButton"]
    signin = ["email", "password", "signInSubmitButton"]
    page = _WorkdayPage(chooser, advance_on=())
    page.passwords = []
    seen_routes = []

    def react(key):
        if key == "SignInWithEmailButton":                  # the chooser opens the credentials form
            seen_routes.append(key)
            page.els = {k: _WdEl(page, k) for k in signin}
            page.passwords = [_WdEl(page, "pw0", typ="password")]
        elif key == "signInSubmitButton":
            if len(seen_routes) < 2:                        # first submit bounces back to the chooser
                page.els = {k: _WdEl(page, k) for k in chooser}
                page.passwords = []
            else:                                           # second one gets in
                page.els = {"bottom-navigation-next-button": _WdEl(page, "bottom-navigation-next-button")}
                page.passwords = []
    page.after_click = react

    WorkdayAdapter()._account(_wd_ctx(page))
    assert len(seen_routes) == 2, "the chooser is answered again after the bounce"


def test_workday_recognises_the_review_step_without_the_word_review(monkeypatch):
    """Cloudera's heading is the job title on every step, so the review page never matched on its name —
    and the last screen of a completed application was reported as "no Next button"."""
    from jobbot.apply import common as c
    from jobbot.apply.workday import WorkdayAdapter

    a = WorkdayAdapter()
    monkeypatch.setattr(WorkdayAdapter, "_heading", staticmethod(lambda _p: "Forward Deployed AI Engineer"))

    class _Page:
        def __init__(self, has_submit): self.has_submit = has_submit
        def get_by_role(self, _role, name=None): return _WdGroup([_WdEl(self, "submit")] if self.has_submit else [])
        def after_click(self, _k): pass

    monkeypatch.setattr(c, "visible", lambda loc, timeout=0: loc.count() > 0)
    assert a._is_review(_Page(has_submit=True)) is True, "a Submit button is what makes it the last step"
    assert a._is_review(_Page(has_submit=False)) is False

    monkeypatch.setattr(WorkdayAdapter, "_heading", staticmethod(lambda _p: "Review"))
    assert a._is_review(_Page(has_submit=False)) is True, "and the name still counts where there is one"


def test_workday_unverified_account_is_named_as_such(keychain, monkeypatch):
    """Workday will not let a brand-new account sign in until the link it mailed is opened, and it says so
    in the same breath as offering to resend — which read as a wrong password for a long time."""
    from jobbot import mail
    from jobbot.apply.workday import ACCOUNT_UNVERIFIED, WorkdayAdapter

    assert ACCOUNT_UNVERIFIED.search("Verify your account before you sign in or request a verification email.")
    monkeypatch.setattr(mail, "fetch_link", lambda *a, **k: None)      # no mailbox configured

    page = _WorkdayPage(["email", "password", "signInSubmitButton"], password_boxes=1, advance_on=(),
                        body="Verify your account before you sign in or request a verification email.")
    with pytest.raises(NeedsHuman) as e:
        WorkdayAdapter()._account(_wd_ctx(page))
    assert "verification link" in e.value.reason and FACTS["identity"]["email"] in e.value.reason
    assert "JOBBOT_MAIL_PASSWORD" in e.value.reason, "and how to make it automatic next time"


def test_verification_link_is_picked_out_of_the_mail():
    from jobbot import mail

    body = ("Welcome to Workday. Please confirm your email.\n"
            "https://cloudera.wd5.myworkdayjobs.com/en-US/verify?token=abc123.\n"
            "Unsubscribe: https://example.com/unsub")
    assert mail.extract_link(body) == "https://cloudera.wd5.myworkdayjobs.com/en-US/verify?token=abc123"
    assert mail.extract_link("no links here") is None
    assert mail.extract_link("https://example.com/unsub only") is None, "an unrelated link is not followed"


def test_verification_mail_is_looked_for_from_before_the_signup(keychain, monkeypatch):
    """Workday sends the verification mail when the account is created, but only says "verify your account"
    on the sign-in that follows. Searching the mailbox from that sign-in looked straight past it."""
    from datetime import datetime, timezone
    from jobbot import mail
    from jobbot.apply.workday import MAIL_LOOKBACK_S, WorkdayAdapter

    asked_since: list = []
    monkeypatch.setattr(mail, "fetch_link", lambda since, **k: asked_since.append(since) or None)

    page = _WorkdayPage(["email", "password", "signInSubmitButton"], password_boxes=1, advance_on=(),
                        body="Verify your account before you sign in.")
    started = datetime.now(timezone.utc)
    with pytest.raises(NeedsHuman):
        WorkdayAdapter()._account(_wd_ctx(page))

    assert asked_since, "the mailbox is consulted"
    age = (started - asked_since[0]).total_seconds()
    assert age >= MAIL_LOOKBACK_S - 5, "the window starts before the signup, not after the failed sign-in"


def test_workday_asks_for_a_fresh_verification_mail(keychain, monkeypatch):
    """The account-creation mail may be hours old or already opened — a run that only searched the minutes
    around its own sign-in never found it. The page refusing the sign-in offers "Resend Account
    Verification"; pressing it makes the mail land now, in a window worth searching."""
    from datetime import datetime, timezone
    from jobbot import mail
    from jobbot.apply.workday import RESEND_TEXT, WorkdayAdapter

    assert RESEND_TEXT.search("Resend Account Verification")
    since_used: list = []
    monkeypatch.setattr(mail, "fetch_link", lambda since, **k: since_used.append(since) or None)

    class _Page(_WorkdayPage):
        def get_by_role(self, role, name=None):
            return _WdEl(self, "resend") if name is RESEND_TEXT and role == "link" else _WdGroup([])

    page = _Page(["email", "password", "signInSubmitButton"], password_boxes=1, advance_on=(),
                 body="Verify your account before you sign in or request a verification email.")
    monkeypatch.setattr("jobbot.apply.common.visible", lambda loc, timeout=0: loc.count() > 0)

    with pytest.raises(NeedsHuman):
        WorkdayAdapter()._account(_wd_ctx(page))
    assert "resend" in page.clicks, "the resend link is pressed before the mailbox is searched"
    assert since_used and (datetime.now(timezone.utc) - since_used[0]).total_seconds() < 200, \
        "and the search window starts at the resend, not hours earlier"


def test_verification_buys_its_own_round_and_then_signs_in(keychain, monkeypatch):
    """Opening the activation link is what makes the next sign-in possible, so it must not spend one of the
    three tries — and what follows it is a sign-in, never another signup for an account that now exists.
    The round it buys is capped: a tenant that keeps saying "unverified" must not loop for ever."""
    from jobbot.apply.workday import MAX_VERIFICATIONS, WorkdayAdapter

    assert MAX_VERIFICATIONS == 1

    page = _WorkdayPage(["email", "password", "verifyPassword", "createAccountSubmitButton",
                         "signInSubmitButton", "signInLink"], password_boxes=2, advance_on=(),
                        body="Verify your account before you sign in.")

    def react(key):
        if key == "signInLink":                         # the sign-in card carries neither of these
            page.els.pop("verifyPassword", None)
            page.els.pop("createAccountSubmitButton", None)
        elif key == "signInSubmitButton" and not page.body:
            page.passwords, page.els = [], {"bottom-navigation-next-button": _WdEl(page, "next")}
    page.after_click = react

    def follow_the_link(_self, _ctx, _since):
        page.body = ""                                  # the notice goes once the link has been opened
        return True
    monkeypatch.setattr(WorkdayAdapter, "_verify_account", follow_the_link)

    WorkdayAdapter()._account(_wd_ctx(page))
    assert "signInSubmitButton" in page.clicks, "it signs in after the link is followed"
    assert page.clicks.count("createAccountSubmitButton") <= 1, "and does not re-create an account it has"


def test_source_question_is_never_guessed_by_the_model(no_llm):
    """Mimecast's tree went Social Media -> [Facebook, Instagram, X, YouTube]. With no LinkedIn on the list
    the resolver used to fall through to the model, which answered YouTube — a false statement about where
    the candidate found the job, on a real application."""
    r = mk(llm=True)
    with pytest.raises(NeedsHuman):
        r.answer("How Did You Hear About Us?", ["Facebook", "Instagram", "X", "YouTube"], "select")


def test_source_tree_prefers_the_branch_that_holds_linkedin(no_llm):
    """Both tenants put LinkedIn under Job Boards; Social Media opens onto networks the candidate did not
    use. So the job-board branch is taken first when the list is categories rather than sources."""
    r = mk()
    categories = ["Events", "Former Employee", "Job Boards", "Social Media", "University Recruiting"]
    assert r.answer("How Did You Hear About Us?", categories, "select") == "Job Boards"
    # and the leaf under it is answered from facts, as ever
    assert r.answer("How Did You Hear About Us?",
                    ["Glassdoor", "Indeed", "LinkedIn Jobs", "Naukri"], "select") == "LinkedIn Jobs"


def test_a_wrong_source_answer_left_in_a_draft_is_corrected():
    """A draft holding "YouTube" would be submitted saying the candidate found the job on YouTube. That one
    answer is policy, so it is re-read and replaced; every other filled control is left alone."""
    from jobbot.apply.base import ApplyContext
    from jobbot.apply.workday import WorkdayAdapter

    class _Chip:
        def __init__(self, text): self.text = text
        def evaluate(self, _js, *_a): return self.text
        def inner_text(self): return self.text

    ctx = ApplyContext(job={}, page=object(), facts=FACTS, cv_path="", step=lambda s: None,
                       answer=lambda *a, **k: "", screenshot=lambda l: "")
    a = WorkdayAdapter()
    assert a._wrong_source(ctx, _Chip("YouTube"), "How Did You Hear About Us?") is True
    assert a._wrong_source(ctx, _Chip("LinkedIn Jobs"), "How Did You Hear About Us?") is False
    assert a._wrong_source(ctx, _Chip("Bangladesh (+880)"), "Country Phone Code") is False, "only this question"


def test_prompts_are_answered_before_the_phone_is_typed(monkeypatch):
    """The country dial code lives in a prompt, and the number beside it is only right once that code is on
    screen. Filling identity first wrote +8801700000000 into a box whose form already said +880."""
    from jobbot.apply.workday import WorkdayAdapter
    from jobbot.apply import common as c

    order: list[str] = []
    monkeypatch.setattr(WorkdayAdapter, "_upload_cv", lambda self, ctx: False)
    monkeypatch.setattr(WorkdayAdapter, "_prompts", lambda self, ctx: order.append("prompts"))
    monkeypatch.setattr(WorkdayAdapter, "_step_walker", staticmethod(lambda page: type(
        "W", (), {"_identity": lambda self, ctx: order.append("identity"),
                  "_questions": lambda self, ctx: order.append("questions")})()))
    monkeypatch.setattr(c, "fill_account_password", lambda page, password="": False)
    monkeypatch.setattr(c, "fill_cover_letter", lambda ctx: False)

    WorkdayAdapter()._fill_step(_wd_ctx(_WorkdayPage([])))
    assert order == ["prompts", "identity", "questions"]


def test_cv_capitals_are_normalised_but_content_is_never_rewritten(monkeypatch):
    """Workday's CV parse fills the name in the CV's own capitals ("JAWAD"), and then flags it: "Verify that
    the field is correctly capitalized". Same name, so the case is corrected — a different value is left."""
    from jobbot.apply.generic import GenericFormAdapter
    from jobbot.apply.base import ApplyContext

    class _El:
        def __init__(self, value): self.value, self.cleared = value, False
        def is_visible(self): return True
        def get_attribute(self, name): return "text" if name == "type" else None
        def evaluate(self, js, *_a):
            if "__reactProps" in js:
                return None             # a plain input: React tracks nothing, so nothing is re-filled
            return "INPUT" if "tagName" in js else ""
        def input_value(self): return self.value
        def click(self, timeout=0): pass
        def fill(self, v, timeout=0):
            self.value, self.cleared = v, True

    shouty, theirs, empty = _El("JAWAD"), _El("Someone Else"), _El("")
    monkeypatch.setattr(GenericFormAdapter, "_text_controls",
                        staticmethod(lambda _p, _s="form": [(shouty, "First name"), (theirs, "Last name"),
                                                            (empty, "Email address")]))
    monkeypatch.setattr("jobbot.apply.common.dial_code_on_page", lambda _p: "")

    ctx = ApplyContext(job={}, page=object(), facts=FACTS, cv_path="", step=lambda s: None,
                       answer=lambda *a, **k: "", screenshot=lambda l: "")
    GenericFormAdapter()._identity(ctx)
    assert shouty.value == "Jawad", "the same name, correctly capitalised"
    assert theirs.value == "Someone Else", "a different value is never overwritten"
    assert empty.value == "j@example.com"


def test_adapter_code_can_be_reloaded_onto_an_open_browser():
    """The point of pausing rather than failing is that the window stays open with the form most of the way
    filled. Restarting the server to load a fix threw exactly that away, every time."""
    from jobbot.apply.base import ApplyContext, NeedsHuman, get_adapter_for, reload_adapters

    before = get_adapter_for("workday").__class__
    names = reload_adapters()
    assert "jobbot.apply.workday" in names and "jobbot.apply.common" in names
    assert get_adapter_for("workday") is not None
    assert get_adapter_for("workday").__class__ is not before, "the registry now holds the reloaded class"

    # base is deliberately left alone: paused applications hold these classes, and swapping them would make
    # `except NeedsHuman` stop catching exceptions already in flight.
    from jobbot.apply.base import ApplyContext as after_ctx, NeedsHuman as after_needs
    assert after_ctx is ApplyContext and after_needs is NeedsHuman


def test_repeated_blockers_are_counted(tmp_path, monkeypatch):
    """The same employer stops for the same reason next time too; a reason seen six times is the adapter's
    problem, not the user's, and that was visible only to whoever read the log."""
    from jobbot import config, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()

    first = db.record_issue("workday", "Cloudera", "Workday would not move past 'My Information'.")
    again = db.record_issue("workday", "Cloudera", "Workday would not move past 'My Experience'.")
    assert (first, again) == (1, 2), "the step name differs; the blocker does not"

    other = db.record_issue("greenhouse", "Acme", "Captcha on this form.")
    assert other == 1
    assert [r["seen_count"] for r in db.known_issues()][0] == 2, "worst first"


def test_cookie_walls_are_dismissed_by_their_real_labels():
    """EY's SuccessFactors wall says "Reject All Cookies". Matching "Reject" exactly left it standing over
    the Apply button, and the run reported "no application form on this page"."""
    from jobbot.apply import common as c

    clicked: list[str] = []

    class _Page:
        def __init__(self, labels): self.labels, self.clicks = labels, clicked
        def get_by_role(self, _role, name=None):
            hit = [t for t in self.labels if name.search(t)]
            return _WdGroup([_WdEl(self, hit[0])] if hit else [])
        def wait_for_timeout(self, _ms): pass
        def after_click(self, key): pass

    assert c.dismiss_cookie_banner(_Page(["Accept All Cookies", "Reject All Cookies"])) is True
    assert clicked == ["Reject All Cookies"], "refusal is taken before acceptance"


def test_successfactors_is_a_known_board():
    from jobbot.apply.base import detect_ats, get_adapter_for

    assert detect_ats("https://career5.successfactors.eu/careers?company=EY") == "successfactors"
    assert get_adapter_for("successfactors") is not None
    assert detect_ats("https://globalcareers-atlassian.icims.com/jobs/27183/job") == "icims"
    assert get_adapter_for("icims") is not None


def test_a_form_with_no_form_element_is_still_an_application():
    """Five stops across three boards — EY on SuccessFactors, Atlassian on iCIMS, ServiceNow on
    SmartRecruiters — all said "could not find an application form on this page", and all had one. None of
    them ships a <form> element, which is the only thing the walker used to look for."""
    from jobbot.apply.generic import GenericFormAdapter as G

    class _Page:
        """Controls exist under <main>, but there is no <form> anywhere."""
        def __init__(self, counts): self.counts = counts
        def locator(self, sel):
            for scope, n in self.counts.items():
                if sel.startswith(scope):
                    return _WdGroup([object()] * n)
            return _WdGroup([])
        def wait_for_selector(self, _sel, **_k): return None
        def evaluate(self, script="", arg=None):
            if "names" in (script or ""):
                return False        # the gateway test; this board is found by its control count
            return self.counts.get(arg, 0)

    react_board = _Page({"form": 0, "main": 6, "body": 9})
    assert G._detect_scope(react_board) == "main", "the narrowest scope that holds the controls"
    assert G._form_visible(react_board) is True

    plain_board = _Page({"form": 5, "main": 7, "body": 9})
    assert G._detect_scope(plain_board) == "form", "a real <form> is still preferred and kept narrow"

    empty = _Page({"form": 0, "main": 0, "body": 1})
    assert G._detect_scope(empty) is None, "one stray input is not an application"


# ---------- the long tail: openers, wizards, other languages ----------
def test_opener_names_never_match_third_party_logins():
    """"Apply With LinkedIn" and "Postuler via Indeed" sit right beside the real opener on SmartRecruiters;
    pressing one opens someone else's login instead of the form."""
    from jobbot.apply.generic import OPEN_NAMES, THIRD_PARTY_RE
    for name in ("Apply", "Apply now", "Apply for this job online", "I'm interested", "Je suis intéressé(e)",
                 "Postuler", "Jetzt bewerben", "Start application"):
        assert OPEN_NAMES.search(name), name
    for name in ("Apply With LinkedIn", "Apply with Indeed", "Postuler via Indeed", "Apply using Google",
                 "Bewerben mit XING"):
        assert not OPEN_NAMES.search(name) or THIRD_PARTY_RE.search(name), name


def test_identity_labels_in_other_languages_map_to_facts():
    """A Quebec posting on SmartRecruiters asks for Prénom / Nom / Courriel / Ville; asking the resolver
    about those instead of filling them from facts.yaml stopped the run on the first field."""
    from jobbot.apply.generic import GenericFormAdapter as G
    cases = {
        "Prénom*": "identity.first_name", "Nom*": "identity.last_name", "Nom de famille": "identity.last_name",
        "Nom complet": "identity.full_name", "Courriel*": "identity.email", "Confirmer votre courriel*": "identity.email",
        "Numéro de téléphone": "identity.phone", "Ville*": "identity.location", "Site Web": "identity.portfolio",
        "Vorname": "identity.first_name", "Nachname": "identity.last_name", "E-Mail-Adresse": "identity.email",
        "Telefon": "identity.phone", "Nombre": "identity.first_name", "Apellidos": "identity.last_name",
        "Correo electrónico": "identity.email", "Ciudad": "identity.location",
        "Facebook": "", "X (anciennement Twitter)": "",      # handles we do not have: skipped, never invented
    }
    for label, key in cases.items():
        assert G._identity_key(label) == key, label
    assert G._identity_key("First name") == "identity.first_name", "the English labels still win"


def test_known_manual_hosts_say_why():
    from jobbot.apply.generic import GenericFormAdapter as G

    class _P:
        url = "https://www.mycareersfuture.gov.sg/job/abc"
    with pytest.raises(NeedsHuman, match="Singpass"):
        G._refuse_known_manual(_P())


def test_a_job_link_that_lands_on_the_home_page_is_a_closed_posting():
    """nttdata.jobs/vacancies/7627 redirected to nttdata.jobs/ and the walker filled the front page's
    search form, then reported "Submit button not found"."""
    from jobbot.apply.generic import GenericFormAdapter as G
    from jobbot.apply.base import ApplyContext, ApplyError

    class _P:
        url = "https://nttdata.jobs/"
    ctx = ApplyContext(job={"url": "http://nttdata.jobs/vacancies/7627.20260914/IND/"}, page=_P(), facts={},
                       cv_path="", step=lambda s: None, answer=lambda *a, **k: "", screenshot=lambda l: "")
    with pytest.raises(ApplyError, match="home page"):
        G._refuse_redirect_home(ctx)

    class _Still:
        url = "https://careers.example.com/jobs/1/apply"
    ctx.page = _Still()
    G._refuse_redirect_home(ctx)     # a page that is still somewhere is not a refusal


def test_wizard_buttons_are_matched_exactly():
    """"Postuler" must not press "Postuler via Indeed"; "Next" must be found on an <input type=submit>."""
    from jobbot.apply.generic import GenericFormAdapter as G, NEXT_NAMES, SUBMIT_NAMES

    class _El:
        def __init__(self, name, enabled=True): self.name, self.enabled = name, enabled
        def is_visible(self): return True
        def is_enabled(self): return self.enabled

    class _Loc:
        def __init__(self, els): self.els = els
        def count(self): return len(self.els)
        def nth(self, i): return self.els[i]

    class _Page:
        def __init__(self, buttons, submits=()):
            self.buttons, self.submits = buttons, submits
        def get_by_role(self, role, name=None):
            if role != "button":
                return _Loc([])
            return _Loc([_El(b) for b in self.buttons if name.search(b)])
        def locator(self, sel):
            return _Loc([_El(v) for v in self.submits if f"value='{v}' i" in sel])

    assert G._button(_Page(["Postuler via Indeed", "Paramètres des cookies"]), SUBMIT_NAMES) is None
    assert G._button(_Page(["Postuler via Indeed", "Suivant"]), NEXT_NAMES).name == "Suivant"
    assert G._button(_Page([], submits=["Next"]), NEXT_NAMES).name == "Next"
    assert G._button(_Page(["Submit application"]), SUBMIT_NAMES).name == "Submit application"


def test_single_option_select_is_taken_without_asking():
    """iCIMS's consent gate is a <select> whose only real option is "Continue"."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext

    class _Sel:
        def __init__(self): self.chosen = None
        def evaluate(self, js, *a):
            if "selectedIndex" in js:
                return ""
            if "options" in js:
                return ["Continue"]
            return "SELECT"
        def get_attribute(self, _n): return None
        def select_option(self, label=None, **_k): self.chosen = label

    asked = []
    ctx = ApplyContext(job={}, page=object(), facts={}, cv_path="", step=lambda s: None,
                       answer=lambda q, o=None, k="text": asked.append(q) or "x", screenshot=lambda l: "")
    el = _Sel()
    c.answer_and_set(ctx, el, "— Make a Selection — Continue", "select")
    assert el.chosen == "Continue" and not asked


def test_combobox_value_reads_react_select_beside_the_input():
    """The chosen value lives in a sibling of the input's wrapper; searching inside the wrapper read every
    answered Greenhouse dropdown as empty, so each was re-asked on every pass."""
    from jobbot.apply import common as c

    class _Combo:
        def __init__(self, single): self.single = single
        def evaluate(self, js, *a):
            if "tagName" in js and "closest" not in js:
                return "INPUT"
            if "value-container" in js:
                return self.single
            return ""
        def get_attribute(self, _n): return "text"
        def input_value(self): return ""
    assert c.combobox_value(_Combo("Yes")) == "Yes"
    assert c.combobox_value(_Combo("")) == ""


# ---------- resolver: no chat on the form, consent is yes, authorisation by meaning ----------
def test_model_replies_that_talk_to_us_are_not_answers():
    """"I'm ready to answer the application question. Please provide the specific question" was cached and
    would have been pasted onto every later form asking for "Additional information"."""
    from jobbot.apply.resolver import _is_non_answer
    for junk in ("I'm ready to answer the application question. Please provide the specific question.",
                 "UNKNOWN\n\nI need you to clarify what specific keywords you're looking for.",
                 "Could you clarify which field this is for?", "Which question would you like me to answer?"):
        assert _is_non_answer(junk), junk
    for real in ("Yes", "Python, TypeScript, Claude, Bedrock AgentCore",
                 "I built a multi-agent analytics platform at Anlytic and shipped it to production."):
        assert not _is_non_answer(real), real


def test_consent_boxes_are_ticked_without_asking(no_llm):
    r = mk()
    for q in ("I accept", "I agree to the terms and conditions", "I have read and understood the candidate "
              "privacy notice", "I certify that the information provided is accurate", "Point of Data Transfer",
              "I acknowledge the privacy policy"):
        assert r.answer(q, ["Yes", "No"], "checkbox") == "Yes", q
    with pytest.raises(NeedsHuman):
        r.answer("Describe the terms of your current notice period")   # merely mentions "terms"


def test_authorisation_lists_are_answered_by_meaning_not_first_yes(no_llm):
    """Cloudera (Australia) offered three sentences; the first one starting with "Yes" said the candidate is
    currently authorised to work there, which is false. The facts decide: needs sponsorship, and is only
    authorised at home."""
    job = {"id": "workday:1", "company": "Cloudera", "title": "FDE", "location": "Sydney, Australia"}
    r = Resolver(FACTS, {}, job, llm_enabled=False)
    opts = ["No, I am currently authorized to work, and I will not require sponsorship now or in the future.",
            "Yes, I am currently authorized to work. However, I will require sponsorship at some point in the future.",
            "Yes, I am not currently authorized to work, and I will require sponsorship now or in the future."]
    assert r.answer("Will you now or in the future require employment authorization or sponsorship to work "
                    "legally in the country for which you are applying?", opts) == opts[2]
    intercom = ["I am authorised to work in the country which this role is located (citizen, permanent resident etc)",
                "My current work authorisation requires sponsorship or renewal now or in the future.",
                "My authorisation to work in this country is unknown"]
    assert r.answer("Are you authorised to work in the country in which this role is located?", intercom) == intercom[1]
    # a wrong answer cached at another employer must not replay
    r2 = Resolver(FACTS, {normalize_question("Will you now or in the future require employment authorization or "
                                             "sponsorship to work legally in the country for which you are applying?"): opts[1]},
                  job, llm_enabled=False)
    assert r2.answer("Will you now or in the future require employment authorization or sponsorship to work "
                     "legally in the country for which you are applying?", opts) == opts[2]
    # at home the candidate IS authorised and still needs sponsorship elsewhere: plain yes/no keeps working
    assert r.answer("Do you require visa sponsorship?", ["Yes", "No"]) == "Yes"


def test_option_pick_uses_only_the_first_line_of_the_reply(monkeypatch):
    import jobbot.llm
    monkeypatch.setattr(jobbot.llm, "complete", lambda *a, **k: "Yes\n\nThe CV shows Kafka and Snowflake pipelines.")
    r = Resolver(FACTS, {}, JOB, llm_enabled=True, cv_text="Kafka Snowflake")
    assert r.answer("Do you have hands-on experience owning big data architectures?", ["Yes", "No"]) == "Yes"


# ---------- forms that live inside an iframe ----------
def test_frame_view_answers_like_a_page():
    """iCIMS frame-busts its content URL, so the walker fills the form inside the frame: locators go to the
    frame, the keyboard and liveness checks to the page that owns it."""
    from jobbot.apply.common import FrameView

    class _Page:
        keyboard = "KB"
        frames = ["top", "child"]
        def is_closed(self): return False
        def screenshot(self, **_k): return "shot"

    class _Frame:
        page = _Page()
        url = "https://x.icims.com/jobs/1/login?in_iframe=1"
        def is_detached(self): return False
        def locator(self, sel): return ("frame-locator", sel)
        def evaluate(self, js, *a): return "frame-eval"

    v = FrameView(_Frame())
    assert v.keyboard == "KB" and v.screenshot() == "shot" and v.frames == ["top", "child"]
    assert v.locator("input") == ("frame-locator", "input") and v.evaluate("1") == "frame-eval"
    assert v.url.endswith("in_iframe=1") and v.is_closed() is False


def test_form_root_prefers_the_page_and_falls_back_to_the_frame_with_the_controls():
    from jobbot.apply.generic import GenericFormAdapter as G
    from jobbot.apply.common import FrameView

    class _Frame:
        def __init__(self, n, page): self.n, self.page, self.url = n, page, "https://x/frame"
        def locator(self, _sel): return _WdGroup([object()] * self.n)
        def is_detached(self): return False

    class _Page:
        url = "https://x/"
        def __init__(self, own, frames): self.own, self.frames = own, [self] + frames
        def locator(self, _sel): return _WdGroup([object()] * self.own)

    page = _Page(0, [])
    page.frames = [page, _Frame(0, page), _Frame(4, page)]
    root = G._form_root(page)
    assert isinstance(root, FrameView) and root._frame.n == 4, "the child frame holding the form"
    own = _Page(5, [])
    assert G._form_root(own) is own, "a page with its own form is walked directly"
    bare = _Page(0, [])
    assert G._form_root(bare) is bare, "nothing anywhere: the page, so the caller reports it"


def test_bot_block_page_pauses_with_a_reason():
    """SmartRecruiters (DataDome) served "Access is temporarily restricted" instead of the job; that is
    neither a form nor a captcha, and the old run reported "could not find an application form"."""
    from jobbot.apply.common import detect_bot_block, BOT_BLOCK_MSG

    class _Page:
        def __init__(self, text): self.text = text
        def evaluate(self, js, *a):
            if "querySelectorAll" in js:
                return False    # no captcha widget on the page
            return self.text
    with pytest.raises(NeedsHuman, match="bot-protection"):
        detect_bot_block(_Page("Access is temporarily restricted. We detected unusual activity from your device or network."))
    assert detect_bot_block(_Page("Forward Deployed Engineer. Apply now."), raise_=False) is False
    assert "Continue" in BOT_BLOCK_MSG


def test_typing_that_does_not_land_falls_back_to_setting_the_value():
    """Typing needs the field to keep focus, and a page re-rendering mid-word swallows the keystrokes
    silently. iCIMS took a blank email that way and answered "the format of the email address is not
    valid" — on a form whose email jobbot thought it had filled."""
    from jobbot.apply import common as c

    class _Swallows:
        """Accepts keystrokes and keeps none of them, exactly as a re-rendering field does."""
        def __init__(self): self.value, self.set_directly = "", False
        def is_visible(self): return True
        def get_attribute(self, name): return "text" if name == "type" else None
        def evaluate(self, js, *_a):
            return None if "__reactProps" in js else ("INPUT" if "tagName" in js else "")
        def input_value(self): return self.value
        def click(self, timeout=0): pass
        def press_sequentially(self, value, delay=0, timeout=0): pass      # types into the void
        def fill(self, value, timeout=0):
            self.value = value
            self.set_directly = bool(value)

    el = _Swallows()
    assert c.fill_if_empty(el, "j@example.com") is True
    assert el.value == "j@example.com" and el.set_directly, "the value is there either way"


def test_the_cv_never_goes_into_a_photo_dropzone():
    """Regression (application 94, Two Circles on Workable): Workable puts an optional image-only "Photo"
    dropzone above the required "Resume" one, so the first file input on the page was the wrong one. The
    PDF set there was not refused out loud — the widget redrew as though it had taken, no upload request
    was made, and the form was left holding an attachment with a name and no URL. Submit then did nothing
    at all and the run ended on "Submit not confirmed — no confirmation page and no error message"."""
    from jobbot.apply import common as c

    class _Input:
        def __init__(self, accept, context, name=""):
            self.accept, self.context, self.name, self.files = accept, context, name, []
        def get_attribute(self, attr): return {"accept": self.accept}.get(attr)
        def evaluate(self, js, *_a):
            if "files" in js:
                return 1 if self.files else 0
            return self.context
        def set_input_files(self, path, timeout=0): self.files.append(path)

    photo = _Input(".jpg,.jpeg,.gif,.png,image/jpeg,image/gif,image/png", "Photo (Optional) Choose file")
    resume = _Input(".pdf,.doc,.docx,application/pdf", "* Resume Choose file or drag and drop here")

    assert c._accepts_document(photo) is False
    assert c._accepts_document(resume) is True
    # an input that names no restriction, or allows anything alongside images, is still a CV candidate
    assert c._accepts_document(_Input(None, "Resume")) is True
    assert c._accepts_document(_Input(".pdf,.png", "Attachments")) is True

    class _Page:
        def __init__(self, inputs): self.inputs = inputs
        def locator(self, _sel): return self
        def count(self): return len(self.inputs)
        def nth(self, i): return self.inputs[i]
        def wait_for_timeout(self, _ms): pass

    assert c.upload_resume(_Page([photo, resume]), "/tmp/cv.pdf") is True
    assert photo.files == [], "the image-only dropzone must be left alone"
    assert resume.files == ["/tmp/cv.pdf"]


class _CaptchaFrame:
    """A Playwright frame, with the bounding box its <iframe> element reports."""
    def __init__(self, url, box):
        self._url, self._box = url, box

    @property
    def url(self):
        return self._url

    def frame_element(self):
        box = self._box
        return type("_El", (), {"bounding_box": staticmethod(lambda: box)})()


class _FramedPage:
    def __init__(self, *frames):
        self.frames = [_CaptchaFrame("https://apply.workable.com/acme/j/1/apply/", None), *frames]

    def evaluate(self, script="", *_a, **_k):
        # the in-page DOM scan is blind to Turnstile: no iframes, no prompt text, no class hook
        return _page_eval(script, "First name Last name Submit application")

    def wait_for_timeout(self, _ms):
        # These frames are static, so the re-measure pause proves nothing here and only costs the suite
        # three seconds per positive. The timing itself is covered by _SelfSolving below.
        pass


def test_a_turnstile_the_dom_cannot_see_is_still_found():
    """Regression (applications 94 and 95, Two Circles on Workable): Cloudflare Turnstile renders its widget
    into a closed shadow root and keeps the "Verify you are human" prompt inside a cross-origin frame, so
    `document.querySelectorAll('iframe')` returned nothing, `document.body.innerText` carried no prompt, and
    `.cf-turnstile` was absent because Workable renders the widget explicitly. Every in-page probe came back
    clean while the checkbox sat there unticked, the submit button said "Submitting...", and the run waited
    out its 20 seconds and reported "Submit not confirmed — no error message on the form"."""
    from jobbot.apply import common as c

    widget = _CaptchaFrame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2",
                           {"x": 490, "y": 490, "width": 300, "height": 65})
    assert c.captcha_frame_showing(_FramedPage(widget)) is True
    assert c.detect_captcha(_FramedPage(widget), raise_=False) is True
    with pytest.raises(NeedsHuman, match="Captcha"):
        c.detect_captcha(_FramedPage(widget))


def test_a_passive_turnstile_does_not_pause_a_run():
    """The invisible widget is on plenty of forms that submit perfectly well by themselves. It renders 0x0,
    and pausing on it would hand back every application that was going through on its own."""
    from jobbot.apply import common as c

    invisible = _CaptchaFrame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2",
                              {"x": 0, "y": 0, "width": 0, "height": 0})
    assert c.captcha_frame_showing(_FramedPage(invisible)) is False

    unmeasurable = _CaptchaFrame("https://challenges.cloudflare.com/turnstile/v0/api.js", None)
    assert c.captcha_frame_showing(_FramedPage(unmeasurable)) is False

    ordinary = _CaptchaFrame("https://www.youtube.com/embed/abc", {"x": 0, "y": 0, "width": 560, "height": 315})
    assert c.captcha_frame_showing(_FramedPage(ordinary)) is False


# The real Databricks embed, as measured on application 101 in a 1470x722 window: one anchor frame, no bframe.
_ENTERPRISE_ANCHOR = ("https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=6LcSiteKey&co=aHR0cHM6Ly9qb2Jz"
                      "&hl=en&v=abc123&size=invisible&anchor-ms=20000&execute-ms=30000&cb=xyz")


def test_the_invisible_recaptcha_badge_does_not_pause_a_run():
    """Regression (application 101, Databricks on Greenhouse): every Greenhouse form carries reCAPTCHA
    Enterprise in invisible mode. Its anchor frame is the 256x60 "protected by reCAPTCHA" badge, parked in a
    fixed div hanging off the right edge of the window, and its token box stays empty until the submit — so
    the frame measure added for Turnstile saw a vendor frame bigger than 30x30 with no token, three seconds
    running, and asked the user to solve a challenge that was never going to be shown. The run paused on
    "Opening application form" before a field was filled, on a board that had gone through end to end the day
    before (application 33). The badge names itself in its own URL, which is a fact about the widget rather
    than about the page's wording."""
    from jobbot.apply import common as c

    badge = _CaptchaFrame(_ENTERPRISE_ANCHOR, {"x": 1400, "y": 648, "width": 256, "height": 60})
    assert c._PASSIVE_FRAME_RE.search(badge.url)
    assert c.captcha_frame_showing(_FramedPage(badge)) is False
    assert c.detect_captcha(_FramedPage(badge), raise_=False) is False

    v2_badge = _CaptchaFrame(_ENTERPRISE_ANCHOR.replace("recaptcha/enterprise/", "recaptcha/api2/"),
                             {"x": 1400, "y": 648, "width": 256, "height": 60})
    assert c.captcha_frame_showing(_FramedPage(v2_badge)) is False, "v3 and v2-invisible share the badge shape"


def test_a_recaptcha_the_user_must_tick_is_still_a_blocker():
    """The same anchor URL with size=normal is the "I'm not a robot" box, which only the user can tick, and
    the image challenge is a separate bframe frame — hidden, so unmeasurable, until Google decides to ask."""
    from jobbot.apply import common as c

    tickbox = _CaptchaFrame(_ENTERPRISE_ANCHOR.replace("size=invisible", "size=normal"),
                            {"x": 100, "y": 500, "width": 304, "height": 78})
    assert c._PASSIVE_FRAME_RE.search(tickbox.url) is None
    assert c.captcha_frame_showing(_FramedPage(tickbox)) is True
    assert c.detect_captcha(_FramedPage(tickbox), raise_=False) is True

    challenge = _CaptchaFrame("https://www.recaptcha.net/recaptcha/enterprise/bframe?hl=en&v=abc123&k=6LcSiteKey",
                              {"x": 535, "y": 70, "width": 400, "height": 580})
    assert c.captcha_frame_showing(_FramedPage(challenge)) is True
    with pytest.raises(NeedsHuman, match="Captcha"):
        c.detect_captcha(_FramedPage(challenge))

    hidden = _CaptchaFrame(challenge.url, None)
    assert c.captcha_frame_showing(_FramedPage(hidden)) is False, "the bframe is on every page until asked for"


def test_a_challenge_gets_a_moment_before_the_run_pauses():
    """A managed Turnstile often ticks itself a second or two after the submit. Pausing on first sight would
    ask the user to solve a challenge that was about to clear without them."""
    import time
    from jobbot.apply import common as c

    class _SelfSolving:
        """Shows the challenge briefly, then confirms — exactly what a managed Turnstile that passes does."""
        url = "https://apply.workable.com/acme/j/1/apply/"
        def __init__(self): self.t0 = time.time()
        def solved(self): return time.time() - self.t0 > 1.5
        @property
        def frames(self):
            box = None if self.solved() else {"x": 1, "y": 1, "width": 300, "height": 65}
            return [_CaptchaFrame(self.url, None),
                    _CaptchaFrame("https://challenges.cloudflare.com/turnstile/if/ov2", box)]
        def evaluate(self, script="", *_a, **_k):
            text = "Thank you for applying." if self.solved() else "Submitting..."
            return _page_eval(script, text)
        def wait_for_timeout(self, _ms): time.sleep(_ms / 1000)

    assert c.wait_for_confirmation(_SelfSolving(), timeout_s=12) is True

    class _Stuck(_SelfSolving):
        def solved(self): return False      # the checkbox is never ticked

    with pytest.raises(NeedsHuman, match="Captcha"):
        c.wait_for_confirmation(_Stuck(), timeout_s=30)


def test_a_form_still_submitting_is_not_called_a_failure():
    """Workable disables its button and relabels it "Submitting..." while it waits on its captcha token.
    That is a submission in progress, not a form that ignored the click."""
    from jobbot.apply import common as c

    class _Page:
        def __init__(self, busy, label): self.busy, self.label = busy, label
        def evaluate(self, script="", *_a, **_k):
            if "aria-busy" in script:
                return self.busy and bool(c._INFLIGHT_RE.search(self.label))
            return _page_eval(script, "")

    assert c.submit_in_flight(_Page(True, "Submitting...")) is True
    assert c.submit_in_flight(_Page(False, "Submit application")) is False
    assert c.submit_in_flight(_Page(True, "Submit application")) is False, "disabled alone is not in flight"


def test_a_challenge_that_has_already_passed_is_not_a_blocker():
    """Turnstile does not go away when it passes — it keeps its 300x65 box and swaps in a green tick. Size
    alone therefore called every solved widget a blocker, which would have paused a run on every
    Cloudflare-protected form that was working perfectly. The token the widget hands the form is the
    difference, and it sits in the light DOM where the widget itself does not."""
    from jobbot.apply import common as c

    class _Solved(_FramedPage):
        def evaluate(self, script="", *_a, **_k):
            if "cf-turnstile-response" in script:
                return True                     # the widget has handed over its token
            return _page_eval(script, "First name Last name Submit application")

    widget = _CaptchaFrame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2",
                           {"x": 490, "y": 490, "width": 300, "height": 65})
    assert c.captcha_token_present(_Solved(widget)) is True
    assert c.captcha_frame_showing(_Solved(widget)) is False
    assert c.detect_captcha(_Solved(widget), raise_=False) is False, "a passed challenge must not pause a run"

    # and the same widget with no token is still a blocker
    assert c.detect_captcha(_FramedPage(widget), raise_=False) is True


def test_an_application_the_employer_already_has_is_not_a_failure():
    """Regression (application 80, Cloudera on Workday): the tenant needs an account, so the public job page
    showed Apply and only after the sign-in did Workday say "You've already applied for this job." The check
    ran once, before the sign-in, and its pattern wanted "you have already applied" — so the contraction
    missed, and the notice reached the step walker as a page with nothing to fill and no Next button. The run
    asked the user to finish by hand an application that had gone in the day before."""
    from jobbot.apply import common as c
    from jobbot.apply.base import AlreadyApplied, ApplyError

    class _Page:
        def __init__(self, text): self.text = text
        def evaluate(self, script="", *_a, **_k): return _page_eval(script, self.text)

    cloudera = _Page("Forward Deployed AI Engineer You've already applied for this job. View My Applications")
    assert "already applied for this job" in c.already_applied_message(cloudera)
    with pytest.raises(AlreadyApplied):
        c.raise_if_already_applied(cloudera)
    assert issubclass(AlreadyApplied, ApplyError), "still an ApplyError, so nothing that catches those breaks"

    # the other shapes boards use for the same notice
    for text in ("You applied for this job on September 14, 2026. View Application",
                 "You have already applied to this position.",
                 "Thanks! You already submitted an application for this role."):
        assert c.already_applied_message(_Page(text)), text

    # and prose that merely contains the words must never mark a job submitted that was never sent
    for text in ("First name Last name Submit application",
                 "Read about how we handle applications you have already applied elsewhere",
                 "If you have already applied with another company we still want to hear from you",
                 "Apply now for this job. Already applied? Sign in to track your application status here"):
        assert c.already_applied_message(_Page(text)) == "", text
        c.raise_if_already_applied(_Page(text))     # must not raise


def test_consent_boxes_are_ticked_but_claims_about_you_are_not():
    """A consent box's label names the employer, so a cached answer never repeats and every new company
    stopped the run on a tick that has to be made to apply at all. They are agreed to without asking —
    opt-ins included, by explicit instruction. The line holds at statements of fact: a box saying "I am a
    veteran" or "I have the right to work here" is not a consent, and ticking it would put a claim about the
    user in front of an employer that may not be true, so those stay with facts.yaml and the user."""
    from jobbot.apply.resolver import is_agreeable, normalize_question

    def is_consent_box(label):
        return is_agreeable(normalize_question(label))

    for label in ("I understand and agree that all personal information collected by Kinetic IT is subject "
                  "to the Privacy Act 1988 (Cth)",
                  "I have read and accept the Terms and Conditions",
                  "I consent to the processing of my personal data",
                  "I certify that the information provided is true and complete",
                  # opt-ins: included because the user asked for them to be
                  "Add me to the talent community for future roles",
                  "Send me marketing emails about new opportunities",
                  "Keep my CV on file"):
        assert is_consent_box(label) is True, label

    for label in ("I require visa sponsorship to work in this country",
                  "I have the right to work in Australia",
                  "I identify as a veteran",
                  "I have a disability",
                  "I am a citizen of this country",
                  "Are you currently employed?"):
        assert is_consent_box(label) is False, f"a claim about the user, not a consent: {label}"

    for label in ("Tick here if you do not want to receive updates",
                  "Please opt me out of marketing emails",
                  "I object to my data being kept",
                  "Unsubscribe me from all communications"):
        assert is_consent_box(label) is False, f"the meaning inverts; leave it to the user: {label}"

    assert is_consent_box("") is False
    assert is_consent_box("Upload your portfolio") is False

    # and the rule really is what answers them, ahead of everything else in _rule_answer
    import yaml
    from jobbot.apply.resolver import Resolver
    r = Resolver(facts=yaml.safe_load(open("facts.yaml")), answers={},
                 job={"company": "Kinetic IT", "title": "FDE"})
    consent = ("I understand and agree that all personal information collected by Kinetic IT is subject to "
               "the Privacy Act 1988 (Cth)")
    assert r._rule_answer(normalize_question(consent), consent) == "Yes"
    optout = "Tick here if you do not want to receive updates"
    assert r._rule_answer(normalize_question(optout), optout) != "Yes", "an opt-out must not be ticked"


# ---------- open work-rights questions (application 99, Cevo on Gem) ----------
_AU_JOB = {"company": "Cevo Australia", "title": "Senior AI Engineer (AWS)",
           "location": "Sydney, New South Wales, Australia"}
_AUTH_FACTS = {"identity": {"first_name": "Jawad", "last_name": "Amir", "country": "Bangladesh"},
               "authorization": {"requires_sponsorship": True, "authorized_countries": ["Bangladesh"],
                                 "citizenship": "Bangladesh"}}


def test_an_open_working_rights_question_is_answered_from_the_facts(no_llm):
    """"What are your working rights in Australia?" is a text box. Nothing on the list of Yes/No rules
    answers it, "working rights" was not on the protected list, and the model is never shown the
    authorization block — so it would have guessed. The answer is the facts, as sentences."""
    r = Resolver(_AUTH_FACTS, {}, _AU_JOB)
    assert r.answer("What are your working rights in Australia? *", None, "text") == (
        "I am a citizen of Bangladesh. I do not currently hold the right to work in Australia and would "
        "need visa sponsorship.")
    assert r.answer("Please describe your visa status", None, "textarea").startswith("I am a citizen of Bangladesh.")
    # the Yes/No shapes keep their Yes/No answers
    assert r.answer("Do you have the right to work in Australia?", ["Yes", "No"]) == "No"
    assert r.answer("Are you legally authorised to work in Australia?", None, "text") == "No"
    # at home the statement says so, and names the country from the job when the question does not
    home = Resolver(_AUTH_FACTS, {}, {"company": "X", "title": "Y", "location": "Dhaka, Bangladesh"})
    assert home.answer("Working rights", None, "text") == (
        "I am a citizen of Bangladesh. I have the right to work in Bangladesh and do not need visa sponsorship.")
    # a remote job names no country: the facts are stated on their own
    remote = Resolver(_AUTH_FACTS, {}, {"company": "X", "title": "Y", "location": "Remote"})
    assert remote.answer("What are your working rights?", None, "text") == (
        "I am a citizen of Bangladesh. I have the right to work in Bangladesh and would need visa sponsorship elsewhere.")


def test_working_rights_are_never_guessed_when_the_facts_are_silent(no_llm):
    r = Resolver({"identity": {"country": "Bangladesh"}, "authorization": {}}, {}, _AU_JOB, llm_enabled=True)
    with pytest.raises(NeedsHuman):
        r.answer("What are your working rights in Australia?", None, "text")
    assert Resolver.is_protected("What are your working rights in Australia?")
    assert Resolver.is_protected("Work rights")


def test_the_country_a_question_names_is_read_off_it():
    f = Resolver._country_in_question
    assert f("what are your working rights in australia") == "Australia"
    assert f("do you require sponsorship to work in the uk now or in future") == "United Kingdom"
    assert f("working rights") == ""
    assert f("are you eligible to work in the united arab emirates") == "United Arab Emirates"


# ---------- the cover letter as an attachment ----------
def test_cover_letter_is_attached_as_a_pdf_the_form_will_take(tmp_path, monkeypatch):
    """A .txt is refused by some boards, and the refusal surfaces as a form error on an attachment that was
    optional. A PDF is taken everywhere; it is written without a PDF library."""
    import tempfile
    from jobbot import cover
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    path = cover.letter_file("Dear team,\n\nI build agents — production ones.\n\nJawad", "jawad-amir", "")
    assert path.suffix == ".pdf" and path.read_bytes().startswith(b"%PDF-1.4")
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)
    assert "I build agents — production ones." in text
    assert cover.letter_file("x", "n", ".txt,.doc").suffix == ".txt", "the input rules PDFs out"
    assert cover.letter_file("x", "n", ".pdf,.docx").suffix == ".pdf"
    long = cover.letter_file("line\n" * 200, "n", "")
    assert len(PdfReader(str(long)).pages) == 5, "paginated at 46 lines"


# ---------- identity URLs: the shape a validator will take (application 100, Cevo on Gem) ----------
def test_a_url_fact_is_written_in_the_shape_validators_accept():
    """Gem refuses "https://linkedin.com/in/x" and takes the www host. The URL is correct either way, so
    this is a shape to get right, not a fact to ask the user about."""
    from jobbot.apply import common as c
    assert c.canonical_url("https://linkedin.com/in/jawad-amir") == "https://www.linkedin.com/in/jawad-amir"
    assert c.canonical_url("linkedin.com/in/jawad-amir") == "https://www.linkedin.com/in/jawad-amir"
    assert c.canonical_url("http://www.linkedin.com/in/jawad-amir") == "https://www.linkedin.com/in/jawad-amir"
    # GitHub is canonical without www, and an ordinary site keeps whatever host it was given
    assert c.canonical_url("https://www.github.com/JawadAmir000") == "https://github.com/JawadAmir000"
    assert c.canonical_url("jawadamir.space") == "https://jawadamir.space"
    assert c.canonical_url("") == "" and c.canonical_url("not a url") == "not a url"


def test_a_rejected_url_is_retried_in_another_shape_not_the_same_one():
    from jobbot.apply import common as c
    v = c.url_variants("https://linkedin.com/in/jawad-amir")
    assert v[0] == "https://www.linkedin.com/in/jawad-amir", "the shape most of them take goes first"
    assert "https://linkedin.com/in/jawad-amir" in v
    assert len(v) == len(set(v)), "no shape is offered twice"


def test_the_error_message_is_matched_to_the_field_it_is_about():
    from jobbot.apply import common as c
    errs = ["Please enter a valid LinkedIn URL.", "Phone number is required"]
    assert c.error_for_field(errs, "LinkedIn URL") == "Please enter a valid LinkedIn URL."
    assert c.error_for_field(errs, "Phone number") == "Phone number is required"
    assert c.error_for_field(errs, "First name") == "", "a field nothing complained about"
    assert c.error_for_field([], "LinkedIn URL") == ""


def test_same_value_ignores_case_spacing_and_a_trailing_slash():
    from jobbot.apply import common as c
    assert c.same_value("https://x.com/a/", "https://x.com/a")
    assert c.same_value(" Jawad Amir ", "jawad amir")
    assert not c.same_value("https://x.com/a", "https://x.com/b")


# ---------- a correction typed in the window goes where it is read from ----------
def test_set_fact_keeps_the_file_readable_and_its_comments(tmp_path, monkeypatch):
    """facts.yaml is hand-written and its comments are the record of why each value is what it is, so a
    write-back is a line edit rather than a re-dump. And an unquoted "+880…" would read back as an
    integer, quietly losing the "+" from every future application."""
    import yaml
    facts = tmp_path / "facts.yaml"
    facts.write_text('# who you are\nidentity:\n  email: old@example.com\n  phone: "+10000000000"   # from CV\n'
                     'work:\n  current_title: Engineer   # as of 2026\n')
    monkeypatch.setattr(config, "FACTS_PATH", facts)

    assert config.set_fact("identity.phone", "+8801700000000") is True
    assert config.set_fact("identity.email", "new@example.com") is True
    text = facts.read_text()
    assert "# who you are" in text and "# from CV" in text and "# as of 2026" in text
    loaded = yaml.safe_load(text)
    assert loaded["identity"]["phone"] == "+8801700000000", "the + survives"
    assert loaded["identity"]["email"] == "new@example.com"
    assert loaded["work"]["current_title"] == "Engineer", "the rest of the file is untouched"

    # it can only ever change a leaf that is already there
    assert config.set_fact("identity.nickname", "JJ") is False
    assert config.set_fact("nosuchblock.email", "x@y.z") is False
    assert config.set_fact("identity.email", "") is False
    assert config.set_fact("a.b.c", "x") is False
    assert yaml.safe_load(facts.read_text())["identity"]["email"] == "new@example.com"


def test_an_identity_field_corrected_in_the_window_is_written_to_facts_not_the_cache(tmp_path, monkeypatch):
    """The whole point of application 100: the user fixed the LinkedIn URL by hand, and answers.json is
    the one place that correction could not be used from — every adapter fills identity from facts.yaml."""
    import yaml
    from jobbot.apply.runner import make_seen
    facts_file = tmp_path / "facts.yaml"
    facts_file.write_text("identity:\n  linkedin: https://linkedin.com/in/jawad-amir\n  email: me@example.com\n")
    monkeypatch.setattr(config, "FACTS_PATH", facts_file)
    cached: dict = {}
    monkeypatch.setattr(config, "save_answers", cached.update)

    r = Resolver(config.load_facts(), {}, JOB)
    seen = make_seen(r, set(), {"https://linkedin.com/in/jawad-amir", "me@example.com"}, app_id=1)
    seen("https://www.linkedin.com/in/jawad-amir", "LinkedIn URL")

    assert yaml.safe_load(facts_file.read_text())["identity"]["linkedin"] == \
        "https://www.linkedin.com/in/jawad-amir"
    assert cached == {}, "an identity fix does not belong in answers.json"
    assert r.facts["identity"]["linkedin"] == "https://www.linkedin.com/in/jawad-amir", "and this run sees it"

    # a screening answer still goes to the cache, as before
    seen("Two weeks", "What is your notice period?")
    assert any("notice" in k for k in cached), cached


def test_a_value_that_came_from_facts_teaches_nothing(tmp_path, monkeypatch):
    """Re-reporting what jobbot itself put in the box must not rewrite the file it came from."""
    from jobbot.apply.runner import make_seen
    facts_file = tmp_path / "facts.yaml"
    facts_file.write_text("identity:\n  email: me@example.com\n")
    monkeypatch.setattr(config, "FACTS_PATH", facts_file)
    before = facts_file.read_text()
    seen = make_seen(Resolver(config.load_facts(), {}, JOB), set(), {"me@example.com"}, app_id=1)
    seen("me@example.com", "Email")
    assert facts_file.read_text() == before
