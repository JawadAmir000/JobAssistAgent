# JobBot

Manual-first, low-token job application assistant. Type a title, click **Find my jobs**, click a job to apply. LLM is used only for unknown screening questions and ambiguous scores; every call's tokens and cost are shown per application.

## Setup (macOS)

```bash
cd jobbot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
jobbot                      # opens http://127.0.0.1:8000
```

Then in **Settings**:
1. Upload your CV (PDF).
2. Pick an LLM provider:
   - **anthropic** — paste `ANTHROPIC_API_KEY` in Secrets (stored in macOS Keychain). Default model `claude-haiku-4-5`.
   - **claude_code** — uses your Claude Pro/Max subscription via the Claude Code CLI (`npm i -g @anthropic-ai/claude-code && claude login`).
   - **codex** — uses ChatGPT subscription via Codex CLI (`npm i -g @openai/codex && codex login`).
3. Copy the template and fill it in: `cp facts.example.yaml facts.yaml` (phone, notice period,
   authorisation). Work-authorisation answers come only from here. `facts.yaml` and `answers.json` hold
   your personal data and are gitignored — they stay on your machine.
   Never put a password or API key in it — that file is read into the model's prompts. Credentials go in
   **Settings → Secrets** (macOS keychain); anything credential-shaped found in `facts.yaml` is moved there
   automatically and stripped from the file.

## Usage

- **Jobs** tab → search. ATS boards (Greenhouse/Lever/Ashby from `companies.yaml`) + JobSpy (Indeed/LinkedIn/Google) run in parallel; results are deduped and rule-scored 1–5.
- Click **Apply**. A Chromium window opens and fills the form. If a question isn't cached, the card asks you inline; your answer is saved to `answers.json`, tagged as yours, and never asked again. The browser window belongs to the thread that opened it and stays open while it waits for you, so answering continues the same half-filled form rather than starting over.
- **Applications** tab: everything submitted, with cost.
- **Companies** tab: add boards. Entries with `verified: false` are slug guesses — prune the ones that 404 in the search log.
- A failed run keeps its browser window open. Fix whatever went wrong in it and press **Retry**: the adapter
  re-runs on that same half-filled form instead of starting over, and anything you typed by hand is learned
  into `answers.json` so the next application does not ask. A control still showing what the page put in it
  is not learned — an untouched country dropdown is not you saying you are Afghan.

## Which ATSs are automated

Greenhouse, Lever, Ashby and Workday have adapters of their own. Zoho Recruit, Workable, Recruitee,
Teamtailor, JazzHR, BambooHR, SmartRecruiters, PageUp, iCIMS and SuccessFactors share one generic adapter
that finds fields by their visible label, and any unrecognised career site is attempted with it too. It
presses the Apply / "I'm interested" / "Postuler" control in whichever frame holds it (never "Apply with
LinkedIn" or "Postuler via Indeed"), follows application forms opened in a new tab, waits through delayed
single-page-app rendering, opens an embedded form as the page when the form lives in an iframe,
fills a page, presses Submit if there is one and Next otherwise, and repeats until the site confirms — so a
SmartRecruiters or iCIMS wizard is walked page by page. Labels in French, German, Spanish, Portuguese,
Italian and Dutch ("Prénom", "Courriel", "Vorname") are filled from `facts.yaml` like their English
equivalents. It is still careful: it proceeds only when it can see a real application form, and asks about
any label it cannot map rather than guessing. Indeed is the only source with no adapter at all.

Two sites in the long tail stop on purpose: MyCareersFuture (Singapore) needs a Singpass login, and iCIMS
(Atlassian) shows an hCaptcha puzzle when a candidate account is first created — the run pauses on it,
you solve it in the window, and Continue carries on with the wizard.

Workday needs an account per employer, and jobbot creates it itself: the password is generated on first use
and kept in your keychain as `JOBBOT_ACCOUNT_PASSWORD` (`Settings → Secrets` shows it as set and lets you
paste your own), then reused at every employer so the accounts stay reachable. A tenant that already knows
your email is signed into instead. One thing to set up if you want Workday unattended: **a new Workday account cannot sign in until the link
Workday emails has been opened**, so set `JOBBOT_MAIL_PASSWORD` (a Gmail app password) in Settings → Secrets
and jobbot opens that link itself. Without it the run pauses once per employer and tells you to click the
link. It also pauses for an account whose password isn't the one jobbot manages.

Workday is the fiddliest board jobbot drives, and four of its habits are worth knowing because they look
like bugs when a run stops: every real button is covered by a transparent `click_filter` div that swallows
ordinary clicks (jobbot forces past it); its steps are not inside a `<form>`; its dropdowns are trees that
do not filter when typed into ("How Did You Hear About Us?" is Job Board → LinkedIn Jobs); and the CV step
is a dropzone with no file input, so the CV is dropped on it the way a person would. Each step is given time
to render and to save before jobbot decides it is stuck.

Where a board offers to fill the form from your CV — Workday's "Autofill with Resume", Ashby's autofill
dropzone, anything named "autofill with resume" on a plain career page — that route is taken first: it fills
the employment and education blocks from the CV you uploaded in Settings, and everything it leaves empty is
filled from `facts.yaml` afterwards.

## Layout

```
jobbot/
  config.py      paths, settings, keychain secrets
  credentials.py the generated password jobbot signs up to employers with
  db.py          SQLite (jobs, applications, llm_calls, searches)
  llm/           Provider interface: anthropic_api, claude_code, codex, prices
  discovery/     ats_boards, jobspy_source, merge, scorer, run_search
  apply/         resolver (cached answers), runner (Playwright), greenhouse/lever/ashby adapters,
                 workday (account per employer, data-automation-id selectors), generic (one label-driven
                 walker registered under zoho/workable/recruitee/teamtailor/jazzhr/bamboohr/
                 smartrecruiters/pageup and as the catch-all for unknown career sites),
                 linkedin (resolves a listing to the employer's ATS, then delegates)
  web/           FastAPI + HTMX UI
facts.example.yaml  template — copy to facts.yaml and fill in
facts.yaml       truths about you (forms are filled from this; gitignored)
answers.json     learned screening answers, with where each came from (gitignored)
data/jobbot.log  runner log (rotated); the thread name shows which application a line belongs to
companies.yaml   company → ATS → slug
```

CLI: `jobbot discover "Forward Deployed Engineer"` runs a search without the UI.

Tests: `pytest -q` (441 tests). All offline; the DOM ones skip themselves where Playwright or its
browser is not installed.

## Not automated on purpose

Captchas, SMS/phone verification, LinkedIn Easy Apply, ID uploads, and any authorisation/visa/salary/EEO question that isn't in `facts.yaml` or `answers.json` stop and ask you.

**Captchas** (Palantir's Lever board is the one that reliably shows one): the run pauses, brings the Chromium
window to the front and waits. Solve the puzzle there and click **Continue**; the browser stays on the same
half-filled form. The clearance cookie is saved per company, so later applications to the same board
usually go through without one.

**Cover letters** are written per job from your CV and the posting, cached in the database so the same
employer always gets the same letter, and put into the form's cover-letter field (textarea, else attached as
a `.txt`). They are checked for stock phrasing and rewritten once if they read like boilerplate.

**Nothing is declined on your behalf.** Gender, race, veteran and disability questions are asked, never
auto-answered with "prefer not to say" — that is still a choice, and it is yours. Answer once; it is cached
and never asked again.

**Every remembered answer records where it came from** — you, `facts.yaml`, a value you typed into the
browser window, or the model — and that decides what it is allowed to settle later. Visa, citizenship,
EEO and contact-detail questions are answered from `facts.yaml` or by you and from nothing else, so an
answer picked up on one employer's form can never be replayed onto another country's. A model answer is
the weakest thing in the file and is dropped as soon as `facts.yaml` can answer the same question, which
is what makes editing `facts.yaml` take effect. **Settings → Remembered answers** lists the lot: what
jobbot will reuse, what it has set aside and why, and a review list for anything whose origin it cannot
vouch for. Keep, set aside or delete each one.

**Number fields** (`Notice period (in week)`, `Salary Expectations (day rate/annual)`) take a number and
nothing else — a browser silently drops the letters out of a word typed into one, leaves a field that reads
back as empty, and then refuses the submit with "Please enter a number." So an answer that names a number is
converted (a notice period of `None` is 0 weeks, "3 weeks" is 3, "$120,000" is 120000) and an answer that
names none — "Negotiable" for a salary — stops and asks you for the figure. It is cached like any other
answer, so it is asked once.

**Emailed verification codes** (Greenhouse now requires one before it will accept a submission): set an app
password in **Settings → Secrets → `JOBBOT_MAIL_PASSWORD`** (Gmail: <https://myaccount.google.com/apppasswords>)
and the code is read from the mailbox automatically. Without it the run pauses and asks you to paste the code.

"How did you hear about us?" is the exception in the other direction: it is always answered from
`preferences.job_source` in `facts.yaml` (LinkedIn by default) and never asked, in any phrasing. When the
form offers a list, the closest option is chosen — the LinkedIn entry, else "Social media", else a job
board, else "Other".
