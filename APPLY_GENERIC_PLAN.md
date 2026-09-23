# Make the apply pipeline generic and self-learning

*Research done 2026-09-22 against the live code, `data/jobbot.db` (150 applications) and `data/jobbot.log`.
Every number below was measured; every `file:line` was read. Step 1 is implemented — see §6. Steps 2 to 6
are not. The `file:line` references describe the code as it was found, so the ones in §3.1 and §3.2 that
step 1 touched have moved.*

## In short

**Where we are.** 19 of 150 application attempts were submitted (13%); those attempts cover 92 distinct
jobs, so 19 of 92 jobs (21%) reached an employer. 75 attempts failed, 50 are parked waiting for you.
Progress is measured per distinct job and portal as well as per attempt, because seven retries of one
blocker otherwise make it look seven times as important as it is.
The bot is a deterministic walker with a very good answer memory bolted on. What it lacks is a loop:
when a page or a submit does not behave as the heuristics expect, it stops or retries the identical
sequence, and nothing it observes on the way feeds back into what it tries next or into memory.

**The five causes, in order of measured impact.**

1. **Retries are open-loop.** A rejected submit is retried with the same values, seven times over, because
   the rejection text is never mapped to a field and nothing changes between attempts.
2. **The model never sees the page.** It is asked for answer text and, three times ever, for a button
   index. Every "what is this page / where is the form / which field is the error about" decision is a
   fixed-order chain of booleans with hand-tuned sleeps, so every new portal costs a new special case.
3. **Memory is question→answer only.** There is no memory of *how a site works*, no structured memory for
   the blocks that keep pausing (address, prefix, work history, education entries), and a personal fact
   learned under one label ("Suburb") never answers its synonym ("City").
4. **Learn-back from the browser guesses instead of diffing.** It harvests every filled control on the re-run
   and infers "a human typed this" from eight exclusion rules. It has learned a password, a dial code as a
   country, and a tenant's default country as yours. The precise signal (what changed while parked) is unused.
5. **Every concept is implemented several times** across `common.py`, `generic.py` and the vendor adapters,
   and vendor names leak into the "generic" and "common" modules. Each new board becomes a code change.

**The shape of the fix** (detail in §4): keep the deterministic walker, and wrap it in a closed loop made of
(A) one page snapshot the model can be shown, (B) a per-site playbook written from what worked and what was
diagnosed, (C) a retry loop that diagnoses the rejection and changes something before trying again, with a
diff-based learn-back, (D) a richer facts model with synonyms and repeating sections, and (E) one walker
plus per-vendor hint tables instead of five adapters.

---

## 1. Baseline numbers

| | |
|---|---|
| application attempts | 150: submitted 19, failed 75, needs_you 50, manual 3, running 3 |
| distinct jobs | 92, of which 19 submitted; per portal: linkedin 9/51, greenhouse 7/15, other 2/10, ashby 1/7, lever 0/5, workday 0/3 |
| by source ATS | linkedin 43 failed / 18 parked / 9 sent; workday 25 parked / 0 sent; ashby 10 failed / 1 sent; greenhouse 10 failed / 7 sent; lever 7 failed / 0 sent; other 4 failed / 4 parked / 2 sent |
| LLM spend | answer 148 calls $1.00; cover letters 42 calls $0.20; navigate 3 calls; scoring 264 calls $0.17 |
| answers.json | 104 records: legacy 34, rule 31, llm 31, human 6, typed 2; 16 quarantined, 1 rejected |
| jobs discovered | 1465: linkedin 516, ashby 312, greenhouse 305, other 198 (34 distinct hosts), lever 53, workday 23 |

Note the LLM figures: cost is not the constraint. Only 8 of 104 remembered answers came from you; the rest
is what jobbot told itself. (`llm_calls` also holds 246 rows with provider `fake` and $123 of fake cost from
the old test suite writing to the live database; purge them so cost views stay honest.)

**Recurring blockers** (`issues` table, `seen_count`):

| times | blocker |
|---|---|
| 19 | could not find an application form on this page (LG ×5, Harper ×6, EY, ServiceNow ×3, NTT …) |
| 15 | No Apply button found on this LinkedIn page |
| 13 | LinkedIn Easy Apply — no external application (now walked, stale) |
| 10 | Captcha |
| 7 | Form rejected: "There are N issues that need your attention" (Presight / Oracle, apps 136–142) |
| 6 | Form rejected: "Please select a school, degree, and field of study from the suggestions" (C3 AI, apps 145–151) |
| 6 | Workday sign-in wall |
| 6 | Submit not confirmed — no confirmation page and no error |
| 3 | Form rejected: terms-and-conditions box (Westpac) |

Three traces from the log say most of what matters:

- **C3 AI, app 151** (seventh attempt): `fill_from_suggestions` typed "Computer Science", the box did not
  keep it, `_ask` then logged *"leaving the optional 'Field of study' blank"* although facts.yaml holds it,
  the submit bounced with the school/degree/field message, the refill did the identical thing, the run
  failed. Seven applications, one identical trace each. Nothing between attempt 1 and attempt 7 differed.
- **Presight (Oracle), app 142**: four wizard pages walked, emailed code fetched and entered, submit rejected
  with *"4 issues need your attention"*. The bot never reads which four. Refilled the same values, failed.
  Seven times.
- **LG, app 104**: pressed "Apply" three times, found no form, paused with *"Fill it in the browser window …
  whatever you type there is remembered"*. You pressed Continue; it pressed "Apply" three times again and
  paused again. Nothing was learned and nobody checked whether you had already submitted by hand. Same
  for apps 89, 91, 102, 58.

---

## 2. What exists today (the parts to keep)

- **Provenance-aware answer memory** (`jobbot/answers.py`): trust ladder human > facts > typed > legacy >
  rule > llm > scraped, confidence states, quarantine, merge-on-save, protected-question gating
  (`resolver.py` `_protected_answer`). This is well designed and should stay the spine of "what may answer what".
- **Owner-thread pause/resume with hot reload** (`runner.py` `_serve`, `_refresh_adapter`, `base.reload_adapters`):
  the parked window survives a fix. Any new module must be added to `_RELOADABLE` (`base.py`).
- **Account gate** (`apply/account.py` `at_gate`/`pass_gate`/`_read_refusal`, lines 188–315): genuinely
  portal-agnostic, multilingual, already used by the generic walker (`generic.py:554`).
- **Emailed-code handling** (`common.py:2777–2936`, `mail.py`).
- **Easy Apply walker** (`linkedin.py:334–544`): a `GenericFormAdapter` subclass with ~6 attributes and ~6
  overrides. It proves the "one walker + hints" shape works and is the template for §4E.
- **Facts-only rules** for authorisation, EEO, education, contact details; consent auto-tick (`is_agreeable`).

---

## 3. Findings, with evidence

### 3.1 Open-loop retries (cause 1)

- `submit_and_confirm` (`common.py:2938–2976`) refills once on "Form rejected" using the same resolver state
  and the same values; `generic.py:419` then raises `ApplyError`. The web Retry (`web/app.py:207–229`)
  re-runs `apply()` on the same page with nothing changed.
- `form_errors` (`common.py:971`) returns text, not (control, message) pairs; `error_for_field`
  (`common.py:1062`) exists but nothing uses a rejection to re-answer a named field.
- The `issues` table (`db.py:84–93`) has a `resolution` column that is never written (`record_issue`,
  `db.py:280`); the table is a counter, not a memory.
- `_ask` (`common.py:2452–2461`) swallows `NeedsHuman` for a control judged optional; `is_required`
  (`common.py:2464`) reads the star from the control's own label, so a group-level star (school / degree /
  field of study) makes required boxes look optional. That is the C3 AI chain.
- `field_of_study_alternatives` are consulted by `fill_from_suggestions` (`common.py:2191`, via
  `_answer_alternatives` :2438) but not by the resolver's protected path, so "Could not map answer
  'Computer Science & Engineering' to the options" still pauses (app 121 log).
- After a "no form found" pause, Continue re-enters `_open_form` (`generic.py:556–628`); there is no check
  for a confirmation or "already applied" page first. `raise_if_already_applied` (`common.py:927`) is
  called only by Workday.

### 3.2 The model never sees the page (cause 2)

- `navigator.press_next` (`navigator.py:101–133`) is the only page-level model call: ≤40 button names, the
  first 1200 characters of `innerText` (the header, not the form), `max_tokens=8`, a budget of 3 presses per
  application that survives pause/resume (`navigator.py:37`), a deny list that forbids "skip" and "close"
  (`navigator.py:44–52`), top page only (openers inside frames are invisible).
- Page kind is decided by a fixed chain: `_form_visible` → `_form_in_frame` → `_gateway_form` → `at_gate` →
  `_confirmed` → `verification_prompt` (`generic.py:556–628`, `1249–1284`), each with its own threshold
  (`MIN_FIELDS=3`) and sleeps (3 s, 7 s, 15 s at `generic.py:51–55`, 2.5 s in the navigator).
- The submit button is looked for *before* Next (`generic.py:350`), by name order (`_button`,
  `generic.py:1119–1155`), so a header "Apply" link or a widget "Send" on page 1 triggers submit-and-confirm.
- Confirmation is a substring sweep of the whole page (`CONFIRM_TEXTS`, `common.py:35–44`); *"has been sent
  to"* (:44) matches *"a verification code has been sent to"* and is tested before the verification check
  (`common.py:679` vs `:683`), so a code step can be reported as submitted. `form_errors`'s `checkValidity()`
  pass (`common.py:1000–1005`) runs over every input with no visibility test, so a hidden step or a footer
  newsletter form can make a clean submit read as rejected.
- Control discovery is label-text only: `_questions` (`generic.py:1019–1075`) enumerates `input`, `textarea`,
  `select`, `[role=combobox]`; ARIA `role=radio/checkbox/listbox`, `contenteditable`, `type=date`, chip
  multi-selects, and div-and-button dropdowns are invisible; an unlabelled control is dropped
  (`generic.py:1051`). `_LABEL_JS` (`common.py:1112–1155`) is not shadow-aware (unlike `DEEP_JS`), and a
  `placeholder` outranks a real label three levels up (:1123), so cache keys like "Enter your answer" appear.
- The answer prompt (`resolver.py:1148–1192`) gets CV text, four facts blocks (identity, work, skills,
  summary), company + title, and 12 fuzzy-nearest prior answers. It never sees the job description, the
  page, the ATS, or the education/preferences blocks.

### 3.3 Memory is question→answer only (cause 3)

- No site memory. Every run rediscovers the apply control, the frame, the accepted option strings, the
  error banner, the step sequence. Sessions (`data/sessions/*.json`) carry cookies only.
- No repeating-section logic anywhere (`grep` clean): each "Company", "Title", "Start date" in a work-history
  block reaches `ctx.answer` as a standalone question, and `ROW_FIELD_LABELS` (`answers.py`) then refuses to
  learn the answer, so the block is asked again next time. facts.yaml holds one job and one degree.
- Personal facts that paused and were then remembered by *wording*: Suburb (129), Postal Code (68), Prefix
  (109), time zone, name pronunciation (26, 27), languages. `config.set_fact` (`config.py:159–216`) can only
  overwrite an existing leaf, never add one, so none of these can become a fact.
- `AnswerRecord.scope` exists and is never set; a country-scoped answer could be reused within that country
  and is not.
- Nothing shows you what an application learned; learn-back goes to the log only (`runner.py:160–170`).
  The card has no "that answer was wrong" control; Settings offers set-aside/delete, no inline replace.

### 3.4 Learn-back guesses instead of diffing (cause 4)

- `make_seen` (`runner.py:80–171`) is fed by `answer_and_set` (`common.py:2514–2585`) with a `default` flag
  from `text_is_default` (`common.py:1612`, compares `value === defaultValue`). React and every ATS profile
  autofill set values by property, so `defaultValue` stays empty and a board-prefilled value reads as typed.
  `combobox_is_placeholder` (:1573) has no default analogue at all. Only identity keys get the
  `window_touched` guard (`runner.py:131–152`); everything else goes straight to answers.json.
- Log evidence of wrong learns: `'Choose Password:' -> 'DJ-L$…'` (app 105, a live password into the answers
  file), `'Email Address:' -> 'Yes'` (105), `'Cover note' -> 'Attach'` (113), `'— Make a Selection — Continue'
  -> 'Continue'` (90), `identity.location = '🇧🇩 +880'` (122), `identity.phone = '+880'` (129, 142, 139),
  `identity.country = '+880'` (131), `identity.country = 'United Arab Emirates'` (136, a tenant default).
  Each got a guard afterwards; the design cannot close the class.
- The pending question loses its `kind` on the way to the card (`web/app.py:186–204`,
  `partials/application_card.html:17–29`): number, checkbox, file all render as a text box; there is no file
  answer path.

### 3.5 Duplication and vendor leakage (cause 5)

- Lone-checkbox Yes/No: `greenhouse.py:220–227`, `lever.py:154–160`, `ashby.py:246–254`,
  `generic.py:1093–1108`. Location autocomplete ×3 (`greenhouse.py:160–183`, `lever.py:113–129`,
  `ashby.py:150–169`) while `fill_from_suggestions` exists. `_open_form` ×3 with their own timeouts.
- Two account gates (`workday.py:232–299` vs `account.py`), two refusal regex sets (`workday.py:126–141` vs
  `account.py:86–105`), two IMAP scanners (`mail.py:202–240` vs `:270–304`), two code paths
  (`workday._verification_code` vs `common.handle_verification`).
- In `common.py`: "is this a placeholder/default" five ways (:1552, :1573, :1591, :1612, :2009 plus
  `answers.is_placeholder`); "find the named/submit button" three ways (:634, :1620, :2377); "text near a
  control" six ways; "visible" defined eight times in JS with different size floors; regexes mirrored by hand
  between Python and JS (required mark :2471/:2504, captcha hosts :550/:749, in-flight :864/:881).
- Vendor names inside "generic"/"common": Gem, Oracle, LG, Dayforce, SmartRecruiters, NAB, PageUp, Google
  Places, Workday, LinkedIn, Zoho, JobAdder, SuccessFactors, Workable, iCIMS, Palantir, Ashby, Greenhouse
  (`generic.py:54–135`, `common.py` §5 of the toolbox review). Twelve board names bind to one class
  (`generic.py:1356–1359`) — fine — but each carries no hint of its own.
- Greenhouse ≈85%, Lever ≈90%, Ashby ≈70% boilerplate over `common`; what is load-bearing per vendor is a
  short list (open path, iframe rule, Ashby `button[data-option]` groups and required preflight, submit
  names after a code).

### 3.6 Smaller defects worth fixing on the way

- `already applied` is an `ApplyError` inside `wait_for_confirmation` (`common.py:711`, via
  `_SUBMIT_BLOCKED_RE` :936) but `AlreadyApplied` in `raise_if_already_applied` (:927).
- `choose_combobox` still presses a blind Enter when nothing matched (`common.py:1490`); `combobox_options`
  reads options page-wide with a cap of 60 (:1432), so long country lists are truncated while `_dial_rows`
  (:1778) already scopes by `aria-controls`.
- `click_submit`'s second pass is a substring (`button:has-text('Submit')`, :1620): "Submit a referral" qualifies.
- `_form_root` adopts the first frame with ≥3 controls (`generic.py:465–470`); a chat widget qualifies.
  `_adopt_existing_application_page` adopts any other open page (`generic.py:658–676`).
- `running` cards never time out; after a server restart they spin forever; Retry on a dead window creates a
  new application row (`web/app.py:229`). `ctx.extra` (flow URL, step, mailbox search start) is never
  persisted, so a restart always starts over.
- `mail.py` scans INBOX only (:212, :276), never Spam; `account.py` raises on "check your email" (:279)
  without consulting `mail.fetch_link`, which Workday's copy does (`workday.py:359–392`).
- `workday.py:245` calls `credentials.account_password()` with no length cap; `account_password(cap)` returns
  the 14-char password even when the cap is below 14 (`credentials.py:162–167`).
- Dead code: `NeedsHuman` "no adapter for" at `linkedin.py:111–114` (the registry always falls back to
  generic); `_INFLIGHT_RE`, `is_identity_label` in `common.py`.
- Greenhouse skips identity-looking labels in `_questions` (`greenhouse.py:231`), so an empty required one
  bounces at submit; Ashby skips them only when filled (`ashby.py:185`).

---

## 4. Target design

Keep the deterministic walker as the fast path. Add a closed loop around it. Five pieces.

### A. One page snapshot (`apply/observe.py`)

`snapshot(page_or_frame) -> PageSnapshot`: a single deep- and shadow-aware extraction that returns
- every interactive control with a stable id stamped on the DOM (the navigator already stamps
  `data-jobbot-nav`), its label (one label algorithm, replacing `_LABEL_JS`, `label_context`, the `labelFor`
  inside `form_errors`, `_wants_cv`'s and `upload_resume`'s scans), kind (text/number/date/textarea/select/
  combobox/radio-group/checkbox/file/button/link, including ARIA-only widgets and `contenteditable`), current
  value, required flag (own star *or* group star), error text near it, group/fieldset membership and index
  (for repeating blocks), frame path;
- the visible headings, the progress indicator if any, the page's visible text near the form (not the
  header), and the confirmation/error/captcha/verification signals now spread across `wait_for_confirmation`,
  `detect_captcha`, `verification_prompt`, `submit_blocked_message`;
- optionally a screenshot.

This is the fold-point for the eight "visible" definitions, the six "text near control" scans, the five
"is placeholder" tests and the hand-mirrored regexes. It is also what the walker, the learn-back and the
model all consume, so they finally agree on what is on the page.

### B. Three model calls, used only when the walker is unsure (`apply/planner.py`)

All three take a `PageSnapshot`, run on a Haiku-class model, and are cached in the playbook (C).

1. `classify(snapshot)` → one of `listing | apply_gateway | login | signup | verification | form_step |
   review | confirmation | already_applied | closed | captcha | error`, plus the control id to press next
   (if any) and a confidence. Replaces the fixed boolean chain for the cases it fails on today: "no form
   found" (19), "no Apply button" (15), "no Next" (6), "did not move on", and the Continue-after-hand-fill
   case (was it submitted?).
2. `plan_fields(snapshot, facts_schema)` → for each control: a facts key, a question for the resolver, a
   repeating-block assignment (`work_history[1].company`), or skip. Runs when the label mapper has no rule
   for a required control, and for every control inside a repeating block. The resolver keeps deciding what
   may *answer* each question; the planner only decides what the question *is*.
3. `diagnose(snapshot_after_submit)` → `[{control_id, problem, fix}]` where `fix` is one of `retype`,
   `pick_from_suggestions(query)`, `use_alternative`, `tick`, `ask_user(question, kind, options)`,
   `not_a_field(message)`. Turns "4 issues need your attention" and "select a school from the suggestions"
   into per-field actions. The deterministic `error_for_field` runs first; the model only reads what it
   could not attribute.

The navigator's 3-press budget becomes a per-attempt planner budget (calls, not presses), reset on resume.
The model only ever chooses among controls present in the snapshot, and the page is snapshotted again
after every action it chose, so a wrong choice is caught on the next step rather than at submit.

The answer prompt (`resolver.py:1148–1192`) changes in one place: today it says *"always pick one: UNKNOWN is
never a valid reply to a list"*. That stays for questions the CV can settle (skills, tools, experience,
willingness). For a personal claim the CV and facts cannot contain (a licence, a clearance, a conviction, a
relationship to an employee) the model answers UNKNOWN and the run asks you once, as it already does for
free text. The model maps fields to known facts; it never manufactures a fact.

### C. Site playbook and a closed retry loop

**Playbook** (`apply/playbook.py`, SQLite table keyed by portal tenant + a form/step signature, with locale
where it matters): what worked and what was diagnosed. A familiar host alone is not enough to replay: before
any hint is used the snapshot is checked for the controls the hint expects, and a mismatch sends the walker
back to observation and diagnosis and revises the hint. Apply control, form location (frame/tab), account needed?, step sequence
with their classifications, field → facts mapping including the accepted option strings (`education.field_of_study
→ "Computer Science"` on job-boards.greenhouse.io), submit control, where errors are rendered, known
quirks (drop-zone CV, "Select One" trees). Written on a confirmed submit; revised by every diagnosis. The
second run on a host is deterministic and costs no model calls; when the page no longer matches, the walker
falls back to (B) and the playbook is updated. The `issues.resolution` column is retired into this.

**Explicit outcomes.** A submit ends in exactly one of `confirmed`, `rejected(fields)`, `already_applied`,
or `submission_uncertain`. Today the last two are folded into "failed" (`common.py:711`, `:744`) and Retry
re-submits. An uncertain submit (Two Circles, apps 94–95) keeps the window open and asks you to check; it is
never sent again automatically, because a second send is worse than a missed one.

**Retry loop** in the runner: attempt → on `rejected` run `diagnose` → apply fixes → re-submit. Bounded by
*distinct* diagnoses, not attempts: a retry happens only when a value or an action has changed, and the same
diagnosis twice means pause, naming the field, with the question carried as a normal `NeedsHuman` (kind and
options intact). "Seen 7 times" cannot happen because attempt 2 is never identical to attempt 1.

**Continue after a pause**: `classify` first. `confirmation`/`already_applied` → submitted. `form_step` →
diff learn-back (below), then continue. Otherwise the pause reason is re-derived from the snapshot, not
replayed.

**Diff-based learn-back**: snapshot the form when parking; snapshot again on Continue/Retry; only controls
whose value changed, on a window the user has interacted with, are the human's. Two classes of learning:

- *Ordinary answers* (a screening question, a preference) are learned with `source="typed"` and reused
  automatically, as now.
- *Important facts* (identity, employment, education, eligibility, address) become a **proposed change**
  shown on the card ("Change facts.yaml identity.phone to +880…? Keep / Discard"), and reach facts.yaml only
  when you approve. Every corruption of facts.yaml so far came from an automatic write on this path.

Never learned, whatever changed: passwords and one-time codes, a value the walker itself wrote, and a control
that changed because the page re-rendered or a portal default arrived (compare against the page's *own*
prefill by taking the first snapshot after the walker's pass, not before it). The card shows the list
("Learned from the window: Suburb = Mirpur"). This retires the eight exclusion rules, the `human_touched`
flag, `text_is_default`, `combobox_is_placeholder` and `_not_a_correction`, and makes learning a tenant
default impossible. A model-written answer can never be promoted to a confirmed personal fact.

### D. Memory model

- **facts.yaml gains the blocks that keep pausing**: `address` (street, suburb/city, state, postcode),
  `identity.prefix`, `identity.time_zone`, `identity.name_pronunciation`, `languages[]`,
  `work_history[]` (company, title, location, start, end, current, summary), `education[]` (school, degree,
  field, start, end), `certifications[]`. A repeating-section filler in the walker reads the lists and is the
  only thing allowed to answer `ROW_FIELD_LABELS`.
- **Fact synonyms** in the resolver: one table mapping label families to fact keys (city/suburb/town;
  postcode/zip/postal code; prefix/title/salutation; time zone/timezone; pronunciation/phonetic). A
  non-protected personal fact asked once is written to facts.yaml as a *new leaf* (extend `set_fact` to add
  under an existing block) rather than remembered by wording.
- **Scope actually used**: an answer learned on a form the resolver can place in a country is saved with
  `scope=country:<name>` and reused only there. Protected rules stay above it.
- **Per-application trace**: persist a redacted trace (question, answer, source, control id, page
  classification, outcome) per application, plus `ctx.extra` (flow URL, step index, mailbox search start),
  so the card can show provenance. A server restart cannot recover the browser: the tab belongs to a thread
  of the old process. Runs left `running`/`needs_you` by a restart are marked *interrupted* on startup and
  the card offers a guarded fresh run, which uses the trace to skip what the site already holds (an account,
  a saved draft) rather than pretending to resume.
- **UI**: the card shows what was learned and what answered what, with "wrong → replace" inline; the pending
  question keeps its `kind` (number box, checkbox, file picker); a `running` card gets a stall timeout and a
  Cancel.

### E. One walker, hint tables per vendor

- Greenhouse, Lever and Ashby become `GenericFormAdapter` subclasses in the Easy Apply shape: open path,
  iframe rule, submit names, and for Ashby the `button[data-option]` groups and the required preflight
  (which the generic walker should adopt for everyone). Their identity/location/checkbox/question loops go.
  The existing adapters stay in place until the shared walker has been seen to handle each one's proven
  behaviour on real applications; only then is the old code deleted. Greenhouse is 7 of the 19 submissions
  and must not regress while the walker catches up.
- Workday keeps its `data-automation-id` layer but hands account handling to `account.py` (one refusal set,
  one mail-link follower, `password_max_length` passed at `workday.py:245`) and its list handling to the
  snapshot's combobox kind.
- `common.py` is split by concern (observe / fill / submit / verification / phone), each concept implemented
  once. Anything vendor-specific moves into that vendor's hint table or the playbook.

---

## 5. Order of work, by measured payoff

| # | step | what it unblocks (from the 125 non-submitted) |
|---|---|---|
| 1 | **Done — see §6.** Rejection diagnosis + per-field fixes + bounded-by-diagnosis retry; `is_required` group stars; resolver uses `field_of_study_alternatives`; explicit already-applied and uncertain-submit outcomes | C3 AI ×7, Presight ×7, Westpac terms ×3, Cloudflare phone, NAB "Categories", Deloitte "Prefix", Fusion5 mobile — ~25 failures |
| 2 | `snapshot` + `classify` on the no-form / no-Apply / no-Next / Continue paths; "already submitted" detection; frame-aware opener search | 19 + 15 + 6 pauses, and the hand-filled pauses that currently loop |
| 3 | Diff-based learn-back, "learned" list on the card, kind-aware answer UI | stops the wrong-learn class for good; makes the "remembered for next time" promise true |
| 4 | facts.yaml blocks + synonyms + repeating-section filler; `set_fact` can add a leaf | Suburb, Postal Code, Prefix, time zone, pronunciation, education suggestion lists, work-history blocks |
| 5 | Playbook + `plan_fields` for unmapped required controls | second run on any host is free and deterministic; unseen widgets get a mapping |
| 6 | Consolidation (E), persistence of `ctx.extra` and the per-application trace, small defects in §3.6 | fewer regressions per change; restart no longer loses the run |

Constraints that hold throughout (from the project's own rules): no test suite — verify each step on real
applications through the UI; never restart the server mid-run, hot reload instead; every new module goes
into `base._RELOADABLE`; Playwright objects stay on the owner thread; a fact worked out mid-fix goes to
facts.yaml, not into code.

**Verification scenarios**, one per cause, all reachable from the existing failure set: the C3 AI suggestion
fields (retry must change something), an Oracle "N issues" rejection (the fields must be named), LG's Apply
loop (Continue must classify the page and detect a hand submission), a browser-edited answer (must appear
as learned or as a proposed fact change), a custom control, an emailed-code step, and an uncertain submit
(must never be sent twice). Track confirmed submissions and repeated blockers per distinct job and portal.

## 6. What landed in step 1 (2026-09-22)

All of it in modules that hot-reload onto an open browser (`base._RELOADABLE`), because the server was up
with 50 parked windows at the time. Nothing needs a restart; templates re-render on their own.

**`common.py`**

- `field_errors(page)` — every validation message with the control it is about. Stamps the control with
  `data-jobbot-err` so Python can reach it again, reads shadow DOM, and attributes a message by its aria
  reference, by the nearest wrapper holding exactly one field, or — when a message names its fields, as
  "Please select a school, degree, and field of study from the suggestions" does — by matching those names
  against the labels in the section, so one complaint repairs all three boxes.
- `rejection_signature(fields, errors)` — what a rejection *is*, with the digits that vary between attempts
  taken out, so "1 out of 4 issues" and "2 out of 4 issues" compare equal.
- `repair_fields(ctx, fields)` — acts on each named control (tick a consent, re-choose a list, rotate a URL
  shape, commit a suggestion using `field_of_study_alternatives`, fill an empty required box) and returns
  what actually changed. A field the form named is never treated as optional, so an unanswerable one asks
  the candidate with the site's own complaint attached, instead of being skipped.
- `submit_and_confirm` — four outcomes now: confirmed, already applied, rejected, uncertain. Retries are
  bounded by *distinct diagnoses*: the form is sent again only once something about it has changed, and the
  same complaint twice pauses naming the field.
- Uncertain submits (`_guard_uncertain_submit`, `confirmation_showing`) — a submit that produced neither a
  confirmation nor a complaint is never repeated. It pauses; on Continue the page is re-read, and it
  re-submits only if the form is plainly still there complaining.
- `already_applied_message` is now told apart from a quota or closed-posting refusal inside
  `wait_for_confirmation`, so an application the employer already holds stops being filed as a failure.
- `is_required` reads a star on the *group* when the box has none and the form is not starring fields
  individually. This is the other half of the C3 AI chain: "Field of study" carried no star, read as
  optional, was skipped, and the submit was then refused over the empty box.
- `form_errors`' constraint pass now skips controls that are not visible (nor drawn by a visible label), so
  a step a single-page wizard keeps mounted out of sight stops turning a clean submit into "Form rejected".

**`resolver.py`** — `_map_answer` tries the wordings `education.field_of_study_alternatives` allows when the
true answer is not on a form's list, at every mapping site (cache, rules, protected, remembered, LLM). A
dropdown now reaches the same answer the suggestion-box filler already reached.

**`answers.py`** — `_better` carries a rejection through the merge. Without it the refused answer was written
back as still-usable and replayed onto the next application.

**`generic.py`** — the wizard's bounce path diagnoses and repairs the named controls instead of refilling
blind, and pauses with the field names when a repair changes nothing.

**`partials/application_card.html`** — "Mark applied" on a paused or failed card. The uncertain-submit
message tells the candidate to use it, and the 19 "fill it in the window yourself" pauses had no way to say
the job was done.

Verified against real Chromium pages reproducing the failing shapes: the education group now reads as
required; a section-level message repairs all three of its boxes, with the field of study committed as
"Computer Science" from the declared alternatives; a consent box is ticked; a refused LinkedIn URL rotates
shape; an unanswerable box pauses carrying its question and kind. On the submit loop: a repairable
rejection submits on the second click, an unrepairable one pauses after the second with the field named,
and a silent submit clicks exactly once and still once after a Continue.

**Not done here, still open:** `ctx.extra` is not persisted, so a server restart loses the uncertain-submit
flag along with the rest of the run; radio groups and file inputs fall through to the broad refill rather
than being repaired; and a visible required field in an unrelated form on the same page (a footer
newsletter) can still be read as a validation error.

## 7. What the wider field does

The three open-source browser-agent families converge on the same split this plan makes: a structured
element list (accessibility tree or DOM snapshot with element ids, ~5–10% of the raw DOM) is shown to a
model for *understanding*, actions are *cached per site* and replayed deterministically, and the model is
called again only when the page no longer matches the cache.

- Skyvern: annotated screenshot + "a structured description of every interactable element" per action; a
  planner / actor / validator split where the validator "handles error correction by verifying that each
  step was completed"; first run records, later runs replay a generated script and fall back to the live
  agent when the site changes. <https://www.skyvern.com/blog/how-skyvern-reads-and-understands-the-web/>,
  <https://github.com/skyvern-ai/skyvern>
- Stagehand: `observe` / `act` / `extract` over the Chrome accessibility tree; "caches action mappings on
  first encounter, replaying cached actions on subsequent similar pages without calling the LLM at all".
  <https://www.browserbase.com/blog/ai-web-agent-sdk>
- browser-use: DOM-first, accessibility-tree-derived element list per step.
  <https://dev.to/stevengonsalvez/browser-tools-for-ai-agents-part-2-the-framework-wars-browser-use-stagehand-skyvern-4gn>
- Survey of the three perception architectures (vision / accessibility tree / live DOM in the user's own
  session) and why "a serious browser-agent product probably uses more than one":
  <https://dev.to/alexey_sokolov_10deecd763/runtime-snapshots-16-the-three-architectures-of-browser-agents-4gkc>

jobbot already has the deterministic half (walker + cookies + hot reload) and the trust model that none of
those tools have. What it lacks is the observe → diagnose → adapt loop and the site memory that makes the
loop pay for itself.
