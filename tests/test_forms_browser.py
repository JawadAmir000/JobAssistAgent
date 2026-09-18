"""Form-detection tests that need a real DOM.

The rest of the suite is offline, and it stays that way — but the rules these cover live in page scripts,
where a fake page object proves nothing: the two bugs below were both in JavaScript, and both survived a
green offline suite. Skipped, never failed, where Playwright or its browser is not installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from jobbot.apply.generic import GenericFormAdapter as G  # noqa: E402


# The page behind "jobbot could not find an application form on this page", seen four times on one job:
# PageUp opens an application with an email box, a privacy tick and Next. Two controls.
GATEWAY = """
<h2>Begin application</h2>
<form id="applicationForm" action="/apply/1083/aw/applicationForm/initApplication.asp" method="post">
  <label for="em">Email address:</label><input type="text" id="em" name="sEmail">
  <label><input type="checkbox" name="bPrivacy"> I understand and agree that all personal information
    collected by Kinetic IT is subject to the Privacy Act 1988 (Cth)</label>
  <button type="submit" class="btn btn-next disabled" name="button_next.x" id="button_next"
          aria-describedby="button_next_label" aria-label="Next" value="Next" aria-disabled="true"
          ><span id="button_next_label">Next</span></button>
</form>
<p><b>New applicants:</b> Be sure to type your address correctly.</p>
"""

# Three controls, no application: the same count the old rule trusted. It would have filled and submitted
# the sign-in panel.
FURNITURE = """
<form role="search" action="/search"><input type="search" name="q"><button>Search</button></form>
<form id="newsletter" action="/subscribe"><input type="email" name="email"><button>Subscribe</button></form>
<form id="login" action="/signin"><input type="text" name="user"><input type="password" name="pw">
  <button>Sign in</button></form>
"""

ORDINARY = """
<form role="search" action="/search"><input type="search" name="q"><button>Search</button></form>
<form id="app"><label>First name<input name=fn></label><label>Last name<input name=ln></label>
  <label>Email<input type=email name=em></label><label>CV<input type=file name=cv></label>
  <button type=submit>Submit application</button></form>
"""

# Workday, iCIMS, SuccessFactors and SmartRecruiters ship no <form> element at all.
FORMLESS = """
<main><label>First name<input name=fn></label><label>Last name<input name=ln></label>
  <label>Email<input type=email name=em></label><button>Submit application</button></main>
"""

NO_FORM_AT_ALL = """
<h1>Forward Deployed Engineer</h1><p>We are hiring.</p><a href="/apply">Apply for this job</a>
"""


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001 - browser binaries not downloaded on this machine
        pw.stop()
        pytest.skip(f"no chromium available: {e}")
    p = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    yield p
    browser.close()
    pw.stop()


@pytest.mark.parametrize("name, html, expected", [
    ("pageup gateway", GATEWAY, "form"),
    ("site furniture", FURNITURE, None),
    ("ordinary form", ORDINARY, "form"),
    ("formless board", FORMLESS, "main"),
    ("job page only", NO_FORM_AT_ALL, None),
])
def test_scope_detection(page, name, html, expected):
    page.set_content(html)
    assert G._detect_scope(page) == expected, name


def test_apply_opener_adopts_the_application_popup(page):
    """LG opens Dayforce in a second page; the adapter must continue there, not inspect the posting again."""
    from jobbot.apply.base import ApplyContext

    page.set_content("""
      <button onclick="const p = window.open('about:blank', '_blank');
        p.document.write(`<form><label>First name<input></label><label>Last name<input></label>
          <label>Email<input type='email'></label><button>Next</button></form>`);">Apply</button>
    """)
    adopted = []
    ctx = ApplyContext(job={}, page=page, facts={}, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "",
                       on_page_change=adopted.append)
    try:
        assert G()._click_opener(ctx) is True
        assert ctx.page is not page
        assert adopted == [ctx.page]
        assert G._form_visible(ctx.page, 1500) is True
    finally:
        for opened in page.context.pages:
            if opened is not page:
                opened.close()


def test_form_detection_waits_past_early_site_furniture(page):
    """Dayforce renders a stray control first; that must not end the wait before its real form arrives."""
    page.set_content("""
      <input type="search" aria-label="Search jobs">
      <main id="root"></main>
      <script>
        setTimeout(() => {
          document.getElementById('root').innerHTML = `<form>
            <label>First name<input name="first"></label>
            <label>Last name<input name="last"></label>
            <label>Email<input type="email" name="email"></label>
            <button>Next</button></form>`;
        }, 350);
      </script>
    """)

    assert G._form_visible(page, 1500) is True


def test_the_gateway_step_is_recognised_as_a_form(page):
    """Regression (application 98, Kinetic IT on PageUp): the rule was three visible controls, on the
    reasoning that "a search box or a newsletter signup does not reach three fields; an application always
    does". This page is an application and has two, so jobbot asked the user to fill by hand a form it could
    walk perfectly well."""
    page.set_content(GATEWAY)
    assert G._application_controls(page, "form") == 2, "two controls: below the three-field rule"
    assert G._gateway_form(page) is True
    assert G._form_visible(page, 1500) is True


def test_a_page_of_site_furniture_is_not_an_application(page):
    """The same rule failed the other way round, which is the dangerous direction: search + newsletter +
    sign-in reached three controls, so the walker would have filled and submitted the sign-in panel."""
    page.set_content(FURNITURE)
    assert G._application_controls(page, "form") == 2, "the search and newsletter boxes do not count"
    assert G._gateway_form(page) is False, "Search, Subscribe and Sign in are none of them Next or Submit"
    assert G._form_visible(page, 1500) is False


def test_a_sign_in_gate_is_still_walkable(page):
    """Sign-in forms are deliberately not excluded: several boards gate an application behind one, and
    dropping them would lock the walker out of the flow it exists to walk."""
    page.set_content("""
        <form id="login" action="/signin"><label>Email<input name=em></label>
          <label>Password<input type=password name=pw></label>
          <label>Confirm<input type=password name=pw2></label><button>Continue</button></form>
    """)
    assert G._application_controls(page, "form") == 3
    assert G._detect_scope(page) == "form"


def test_a_button_labelled_the_accessible_way_is_still_matched(page):
    """Regression (application 98, fifth occurrence): PageUp's control is the ordinary accessible shape —
    <button aria-label="Next" value="Next"><span>Next</span></button> — and the first cut of this test built
    its candidate string as innerText + ' ' + aria-label, which makes "Next Next". That matches none of the
    names, so the gateway went unrecognised on the real page while passing against a hand-written copy that
    had no aria-label. Each source is matched on its own."""
    page.set_content("""
        <form action="/apply"><input type="email" name="em">
          <button type="submit" aria-label="Next" value="Next"><span>Next</span></button></form>
    """)
    assert G._gateway_form(page) is True, "aria-label matching the inner text must not defeat the match"

    # the sources that carry the name when the visible text does not
    page.set_content("""
        <form action="/apply"><input type="email" name="em">
          <button type="submit" aria-label="Continue"><svg></svg></button></form>
    """)
    assert G._gateway_form(page) is True, "an icon button named only by aria-label"

    page.set_content("""
        <form action="/apply"><input type="email" name="em">
          <input type="submit" value="Submit application"></form>
    """)
    assert G._gateway_form(page) is True, "input[type=submit] carries its name in value"

    page.set_content("""
        <form action="/apply"><input type="email" name="em">
          <button type="submit" aria-label="Search jobs">Search</button></form>
    """)
    assert G._gateway_form(page) is False, "neither source names a control this walker would press"


# Gem (jobs.gem.com, application 99, Cevo): no <form>, no <label>, and no id, name, placeholder or aria
# attribute on any input. Each field is a <span> beside the div that wraps its input; the file inputs are
# hidden behind styled dropzones that both read "Click to upload or drag and drop here"; the two senders are
# "Apply and save" and "Apply without saving". Every label search came back empty, so the walker took the
# form for nine unlabelled boxes and filled none of them, then found no button it knew.
GEM = """
<div id="content"><div class="formLayout-43">
  <div class="flex-30"><span>Ready to apply?</span><span>Powered by Gem</span></div>
  <div class="form-38">
    <div class="flex-30"><span class="bodyImportant-47">First name *</span><div class="textField-77"><div><div><input type="text"></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">Last name *</span><div class="textField-77"><div><div><input type="text"></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">Email *</span><div class="textField-77"><div><div><input type="text"></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">LinkedIn URL</span><div class="textField-77"><div><div><input type="text"></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">Phone number *</span><div class="textField-77"><div><div><input type="text"></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">Location *</span><div class="textField-77"><div><div><input type="text"></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">Resume *</span><div><div><div class="container-105">
      <input type="file" style="display:none"><div class="promptContainer-107">Click to upload or drag and drop here</div></div></div></div></div>
    <div class="flex-30"><span class="bodyImportant-47">Cover letter</span><div><div><div class="container-105">
      <input type="file" style="display:none"><div class="promptContainer-107">Click to upload or drag and drop here</div></div></div></div></div>
    <div class="flex-30"><div class="flex-30">What are your working rights in Australia? *</div><div class="textField-77"><div><div><input type="text"></div></div></div></div>
  </div>
  <div class="primaryApplicationContainer-121"><p>By applying you agree to Gem's terms and privacy policy.</p>
    <p>Save your info to apply to other roles faster &amp; help employers reach you.</p>
    <button type="submit">Apply and save</button><button type="submit">Apply without saving</button></div>
</div></div>
"""


def test_a_label_that_is_plain_text_beside_the_control_is_read(page):
    """Regression (application 99, Cevo on Gem): see GEM above."""
    from jobbot.apply import common as c
    from jobbot.apply.generic import SUBMIT_FALLBACK_RE, SUBMIT_NAMES
    page.set_content(GEM)
    assert G._detect_scope(page) == "#content", "no <form>: the form is found by its controls"
    got = [(label, c.is_required(el)) for el, label in G._text_controls(page, "#content")]
    assert got == [("First name", True), ("Last name", True), ("Email", True), ("LinkedIn URL", False),
                   ("Phone number", True), ("Location", True),
                   ("What are your working rights in Australia?", True)]
    files = page.locator("input[type=file]")
    assert c.get_label_for(files.nth(0)) == "Resume *", "a hidden input is still named by the span above it"
    assert c.get_label_for(files.nth(1)) == "Cover letter"
    assert c._wants_cv(files.nth(0)) and not c._wants_cv(files.nth(1))
    assert G._button(page, SUBMIT_NAMES, fallback=SUBMIT_FALLBACK_RE).inner_text() == "Apply without saving", \
        "the plain sender, not the one that also opens a Gem profile"


def test_a_row_shared_by_two_controls_lends_its_text_to_neither(page):
    """The sibling-text rule is bounded to a wrapper holding one control, so a "Name" caption over a
    first/last pair cannot become the label of both."""
    from jobbot.apply import common as c
    page.set_content("""<div id="content"><div><span>Name</span><input name="fn"><input name="ln"></div>
        <div><span>Email</span><div><input name="em"></div></div><div><p>A long paragraph of instructions
        that runs well past the two hundred characters a label is allowed, and on, and on, and on, and on,
        and on, and on, and on, and on, and on, and on, and on, and on, and on, and on, and on, and on.</p>
        <div><input name="q"></div></div></div>""")
    inputs = page.locator("input")
    assert c.get_label_for(inputs.nth(0)) == "fn", "falls back to the name attribute"
    assert c.get_label_for(inputs.nth(1)) == "ln"
    assert c.get_label_for(inputs.nth(2)) == "Email"
    assert c.get_label_for(inputs.nth(3)) == "q", "a paragraph is not a label"


def test_unknown_submit_wording_is_pressed_by_its_shape_only_when_safe(page):
    """"Submit & Continue" was never on the list but is plainly the sender. "Apply with LinkedIn", "Send via
    Indeed" and "Apply later" are not, whatever they start with."""
    from jobbot.apply.generic import SUBMIT_FALLBACK_RE, SUBMIT_NAMES
    fields = "<input name=a><input name=b><input name=c>"
    page.set_content(f"""<div id="content">{fields}<button>Apply with LinkedIn</button>
        <button>Save for later</button><button>Apply later</button><button>Send via Indeed</button>
        <button type="submit">Submit &amp; Continue</button></div>""")
    assert G._button(page, SUBMIT_NAMES) is None, "no exact name on this page"
    assert G._button(page, SUBMIT_NAMES, fallback=SUBMIT_FALLBACK_RE).inner_text() == "Submit & Continue"
    page.set_content(f"""<div id="content">{fields}<button>Apply with LinkedIn</button>
        <button>Save for later</button><button>Apply later</button><button>Send via Indeed</button></div>""")
    assert G._button(page, SUBMIT_NAMES, fallback=SUBMIT_FALLBACK_RE) is None


FORM_TO_VANISH = """
<div id="app"><form><label>First name<input name=fn></label><label>Email<input name=em></label>
  <label>Phone<input name=ph></label><button type="submit">Apply without saving</button></form></div>
"""


def test_a_form_that_vanishes_after_submit_counts_as_sent(page):
    """Gem, and boards like it, word their confirmation however they like. A page whose form disappeared
    after the click, with no error, no challenge and none of its own buttons left, has taken the submission
    — the alternative was "Submit not confirmed" on an application the employer already had, and a Retry
    that sent it twice."""
    from jobbot.apply import common as c
    from jobbot.apply.generic import SUBMIT_NAMES
    page.set_content(FORM_TO_VANISH)
    page.evaluate("() => setTimeout(() => { document.getElementById('app').innerHTML = '<p>Done.</p>'; }, 800)")
    assert c.wait_for_confirmation(page, timeout_s=12, names=SUBMIT_NAMES) is True


def test_a_form_that_stays_is_not_confirmed(page):
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyError
    from jobbot.apply.generic import SUBMIT_NAMES
    page.set_content(FORM_TO_VANISH)
    with pytest.raises(ApplyError, match="Submit not confirmed"):
        c.wait_for_confirmation(page, timeout_s=7, names=SUBMIT_NAMES)


def test_a_bot_wall_in_place_of_the_form_is_not_confirmed(page):
    """The DataDome page (ServiceNow on SmartRecruiters, application 91) has no inputs either. It is a
    challenge, so the run pauses for a person — it must never read as a submission that went through."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyError, NeedsHuman
    from jobbot.apply.generic import SUBMIT_NAMES
    page.set_content(FORM_TO_VANISH)
    page.evaluate("""() => setTimeout(() => { document.getElementById('app').innerHTML =
        '<h1>Verification Required</h1><p>We detected unusual activity from your device or network.</p>'; }, 500)""")
    with pytest.raises((ApplyError, NeedsHuman)) as e:
        c.wait_for_confirmation(page, timeout_s=8, names=SUBMIT_NAMES)
    assert "Captcha" in str(e.value) or "Submit not confirmed" in str(e.value)


REJECTING_URL_FORM = """
<div id="content">
  <div><span>First name *</span><div><input type="text"></div></div>
  <div><span>Email *</span><div><input type="text"></div></div>
  <div><span>LinkedIn URL</span><div><input type="text" id="li"></div></div>
  <div id="err" class="error" style="display:none">Please enter a valid LinkedIn URL.</div>
  <button type="submit">Apply without saving</button>
</div>
<script>
  // Accepts only the www host, exactly as Gem's validator does.
  document.querySelector('button').addEventListener('click', () => {
    const v = document.getElementById('li').value;
    document.getElementById('err').style.display =
      (v === '' || v.startsWith('https://www.linkedin.com/')) ? 'none' : 'block';
  });
</script>
"""


def test_a_url_the_form_refuses_is_rewritten_in_a_shape_it_takes(page):
    """Regression (application 100, Cevo on Gem): the refill pass wrote the identical rejected string back,
    so the form bounced twice on a URL that was right but misshapen and the run ended on "Form rejected"."""
    from jobbot.apply import common as c
    from jobbot.apply.base import ApplyContext
    page.set_content(REJECTING_URL_FORM)
    facts = {"identity": {"linkedin": "https://linkedin.com/in/jawad-amir"}}
    ctx = ApplyContext(job={}, page=page, facts=facts, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "")
    a = G()
    a.scope = "#content"

    a._identity(ctx)                       # first pass: the canonical shape
    assert page.locator("#li").input_value() == "https://www.linkedin.com/in/jawad-amir"

    # the same form with a validator that wants the bare host instead
    page.set_content(REJECTING_URL_FORM.replace("https://www.linkedin.com/", "https://linkedin.com/"))
    ctx = ApplyContext(job={}, page=page, facts=facts, cv_path="", step=lambda *_: None,
                       answer=lambda *_a, **_k: "", screenshot=lambda *_: "")
    a._identity(ctx)
    page.locator("button").click()
    assert c.form_errors(page), "the form rejected the first shape"
    a._identity(ctx)                       # the refill pass must try a different shape
    assert page.locator("#li").input_value() == "https://linkedin.com/in/jawad-amir"


def test_a_url_the_user_fixed_by_hand_is_reported_for_learning(page):
    """What the user types into the window is their correction — never overwritten, and handed to the
    runner so facts.yaml learns it."""
    from jobbot.apply.base import ApplyContext
    page.set_content(REJECTING_URL_FORM)
    page.locator("#li").fill("https://www.linkedin.com/in/jawad-amir-real")
    learned: list = []
    ctx = ApplyContext(
        job={}, page=page, facts={"identity": {"linkedin": "https://linkedin.com/in/jawad-amir"}},
        cv_path="", step=lambda *_: None, answer=lambda *_a, **_k: "", screenshot=lambda *_: "",
        seen=lambda v, label="": learned.append((label, v)))
    a = G()
    a.scope = "#content"
    a._identity(ctx)
    assert page.locator("#li").input_value() == "https://www.linkedin.com/in/jawad-amir-real", "not overwritten"
    assert ("LinkedIn URL", "https://www.linkedin.com/in/jawad-amir-real") in learned
