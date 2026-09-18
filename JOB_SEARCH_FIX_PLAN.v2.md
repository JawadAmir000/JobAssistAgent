# Fix Repeated Job Search Results

*Supersedes `JOB_SEARCH_FIX_PLAN.md` (the codex draft), which is left in place unchanged. Every claim below was reproduced against the live code and `data/jobbot.db` before being written down.*

## In short

**The problem:** when a search finishes, the app reloads the job list using only the location and date window — it never sends the title. So it re-queries the whole jobs table and shows you everything it has, again. Different search, same cards. And when a search genuinely finds nothing, the previous cards stay on screen, so it looks like the app ignored what you asked for.

**The fix, in four parts:**

1. **Each search owns its results.** A new `search_jobs` table links every job to the search that found it. The panel shows that set and nothing else.
2. **The panel stays on that search.** The active `search_id` is carried across HTMX swaps, so filtering or "show low scores" works *within* the search. Starting a new search clears the old cards immediately.
3. **Honest empty states.** Four distinct messages instead of one: still searching / nothing matched / a source was blocked so this is incomplete / found N but all below your score filter.
4. **Location matching fixed.** `Waterloo, NSW, AU` currently matches Canada (there is a Waterloo in Ontario). Explicit country names and codes now beat city-name guessing.

**Files touched:** `jobbot/db.py`, `jobbot/discovery/location.py`, `jobbot/discovery/__init__.py`, `jobbot/discovery/jobspy_source.py`, `jobbot/web/app.py`, `jobbot/web/helpers.py`, and the three templates `tabs/jobs.html`, `partials/search_status.html`, `partials/job_list.html`. Tests in `tests/test_web.py` and `tests/test_discovery.py`.

**Suggested order:** Step 1 (location) is independent and provable — land it first. Steps 2→6 are one chain and should land together, since the panel is broken in between.

---

## Context

The Jobs panel shows the same cards for different searches. I reproduced the whole chain against the live code and DB:

1. `partials/search_status.html` refreshes the list with `hx-get="/jobs?min_score=3"` and `hx-include` of `locations`, `hours_old` and `.filter`. **The search title is never sent.**
2. `/jobs` (`web/app.py:85`) calls `db.list_jobs(min_score, query, locations, max_age_days)`, which queries the whole `jobs` table (1145 rows live) filtered only by score/location/age. Two searches with different titles but the same locations+window render an identical list. That is the bug.
3. `db.list_jobs` (`db.py:170`) also drops the SQL `LIMIT` whenever `locations` is set, SELECTs every row including the ≤8 KB `description`, then Python-filters and slices — slow, and still history-wide.
4. The Australia case: search 3's stored log reads `jobspy[Australia]: RetryError … /sorry/index … too many 429` then `filter: kept 0 in ['Australia'] within 72h (168 out of area, 3 too old)`. `found=0`, so the panel silently kept the previous search's cards. It looked like Australia was ignored; it was actually a blocked scraper plus a stale list. No "incomplete" warning was produced because `fetch_jobspy` only counts *per-site* failures into its `failed` list — this one failed at the *location* level.
5. `countries_named()` in `discovery/location.py` unions positional code matching with a scan of `_ALIAS`, which contains country names **and** city names. Measured leaks (worse than the codex plan states):

   | input | today |
   |---|---|
   | `Waterloo, NSW, AU` | australia, **canada** |
   | `London, ON, Canada` | canada, **united kingdom** |
   | `Birmingham, AL` | **united kingdom**, united states |
   | `Perth, Scotland` | **australia**, united kingdom |
   | `Toronto, CA` | canada, **united states** |
   | `Berlin, DE` / `Bangalore, IN` | germany / india, **united states** |

**Outcome:** every search owns an exact result set; the panel shows only that search's jobs; a zero-result search shows a real empty state instead of stale cards; source failures surface as an "incomplete results" warning; the location matcher stops leaking wrong countries.

**Decisions taken (yours):** keep **score-first** ordering; keep the escape hatch but **relabel it "all jobs ever found"** as a scope toggle rather than a location control; on cold load **restore the newest snapshot-capable search**, and when only legacy searches exist show "Run a search above" (the toggle is the way to reach history).

**Baseline: 323 tests pass** (`.venv/bin/python -m pytest -q`); 97 of those are `test_web.py` + `test_discovery.py`.

---

## Step 1 — `discovery/location.py`: explicit country beats city inference

Do this first: independent, smallest blast radius, provable.

**Rule.** Split on the existing `_FIELD_RE` (`[,;|/]` and ` - `). Walk fields left→right keeping a `run` of unconsumed fields. A field is a **marker** if it is a bare `_CODE_ONLY_RE` code that resolves to a country, or is exactly (after `_SEP_RE` normalisation) a country **name or alias**. A marker contributes its country and clears the run. Leftover fields are joined with `", "` and passed through the existing `_ALIAS_ORDER`/`_has` inference. If nothing resolves at all, fall back to scanning the raw string.

Two details that are load-bearing:

- **Markers come from a new dict built only from `(_name, *_aliases)` — never `_places`.** That exclusion is what keeps `Toronto, Ontario` and `Costa Mesa, California, United States` working.
- **Ambiguous-code disambiguation is required, not optional.** For a code that is both a region and an ISO code (`CA`, `DE`, `IN`, `ID`, `MT`, `MS`…), check the preceding run first: if the run's inferred country matches exactly one reading, take it; only then fall back to the existing positional rule (`trailing = i == len(fields)-1 and len(fields) >= 3`). I verified that without this, `'Toronto, CA'` resolves to `{united states}` — **worse than today's `{canada, united states}`**.

**`canonical()` must not use the walker.** Leave it on `_code_countries` so `canonical("NL")` stays `netherlands` and `canonical("UK")` stays `united kingdom`. Keep `_code_countries` — it is `canonical`'s only code path.

Why run-based rather than a simple "explicit beats inferred" tiering: tiering breaks `'London, UK / Bangalore'`, which correctly returns `{india, united kingdom}` today.

**Expected after the change** — the six leaks above collapse to the single right country; these must stay unchanged: `San Francisco, CA`→us, `Toronto, Ontario`→canada, `Victoria, BC`→canada, `Sydney, NSW, Australia`→australia, `London, UK / Bangalore`→{india, uk}, `San Francisco, CA, Toronto, ON, London, UK`→{us, canada, uk}, `Indianapolis, IN`→us, `Amsterdam, NL`→netherlands, `Washington, D.C.`→us.

**Optional (Step 1b):** `"St. John's, NL, CA"` → `{canada, netherlands}` today. Fix with a separate `_CONTEXT_REGION_CODES = {"NL": "canada"}` consulted **only inside the walker**. Do *not* add `NL` to `_REGION_CODES` — that would make `canonical("NL")` return canada for a user who typed "NL" meaning Netherlands.

**Document, don't chase:** `Sydney, Nova Scotia` → {australia, canada} (genuinely ambiguous, correct), and `Birmingham, Alabama` → uk only (region *names* aren't markers and `alabama` is in no table; the real fix is a `_REGION_NAMES` table — out of scope). Also note `canonical("CA")` returns `united states` (California) — pre-existing, not fixable by the walker.

---

## Step 2 — `db.py`: schema, migration, membership

**2a. Add to `SCHEMA`:**
```sql
CREATE TABLE IF NOT EXISTS search_jobs (
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    job_id    TEXT    NOT NULL REFERENCES jobs(id)     ON DELETE CASCADE,
    PRIMARY KEY (search_id, job_id)
);
```
The composite PK is the index for `WHERE search_id=?`.

**2b. Migration — this is the gap in the codex plan.** `init()` only runs `CREATE TABLE IF NOT EXISTS`, and the live `searches` table already exists with 9 rows, so new columns need `ALTER TABLE`. There is no migration helper anywhere in the codebase; add one:

```python
def _add_column_if_missing(c, table: str, column: str, ddl: str) -> None:
    # identifiers can't be bound as parameters; only ever called with literals below
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
```
Call from `init()` for `searches`: `hours_old INTEGER`, `has_snapshot INTEGER NOT NULL DEFAULT 0`, `warning TEXT DEFAULT ''`. (`NOT NULL DEFAULT 0` is legal in SQLite `ADD COLUMN` since the default is non-null; rows 1–9 get `0`.)

**`has_snapshot=1` is set at row creation, not completion** — otherwise the panel falls back to history the instant a search starts and the stale cards return for the duration of the run.

**2c. New/changed functions:**
- `create_search(query, locations, hours_old=None)` — inserts `has_snapshot=1`.
- `record_search_jobs(search_id, job_ids)` — `executemany("INSERT OR IGNORE INTO search_jobs …")`.
- `count_search_jobs(search_id, include_hidden=False)`.
- `latest_snapshot_search()` — `WHERE has_snapshot=1 ORDER BY id DESC LIMIT 1`.

**2d. `list_jobs(..., search_id=None)`.** Only two callers (`app.py:93`, `test_discovery.py:327`), so the signature is safe to change. Build `args` in SQL-text order — the JOIN's `?` precedes every other placeholder:

```python
sql = f"SELECT {cols}, a.status AS app_status, a.id AS app_id FROM jobs j "
args = []
if search_id is not None:
    sql += "JOIN search_jobs sj ON sj.job_id=j.id AND sj.search_id=? "
    args.append(search_id)
sql += "LEFT JOIN applications a ON a.id = (SELECT id FROM applications WHERE job_id=j.id ORDER BY id DESC LIMIT 1) WHERE 1=1"
```
When `search_id` is set: apply `hidden`, `min_score`, `query`; **skip both `locations` and `max_age_days`** (membership already encodes them — skipping the age filter matters as much as the location one, or changing "Posted within" after a search silently deletes rows from a finished result set); always `LIMIT` in SQL; return `fetchall()` with no Python pass. When `search_id is None`, behaviour is byte-for-byte what it is today.

Add a module-level `JOB_COLUMNS` (everything except `description`) and select that instead of `j.*` — `job_card.html` never renders the description, and the history path was pulling ≤8 KB × 1145 rows on every keystroke.

**Ordering stays `COALESCE(score,0) DESC, posted_at DESC, found_at DESC`** per your decision — unchanged from today.

---

## Step 3 — `discovery/jobspy_source.py`: make failure reach the caller

`fetch_jobspy(query, locations, hours_old, log, problems: list[str] | None = None)` — an **optional out-param list**, mirroring the `_scrape_one(..., failed=None)` idiom already in this file and keeping the existing `test_failures_are_reported_not_read_as_no_jobs` passing untouched.

Append to `problems` for all four failure shapes, and count the two new ones in the summary log: per-site failures (existing `failed`); **location-level exceptions** in the loop's `except Exception` arm (the exact arm that logged the Australia `RetryError`); **`FutTimeout`**; and `jobspy: import failed`.

**How incompleteness reaches the UI: a `searches.warning` column, not log parsing.** `search_status.html` only renders `log.splitlines()[-12:]`, so a mid-run warning scrolls off; `/jobs` needs the flag too; and the existing `note:` hack is already unreliable — search 3's live log has no `note:` line despite taking that branch. Non-empty `warning` *means* incomplete. Keep `note:` for the human prose.

---

## Step 4 — `discovery/__init__.py`: `run_search`

1. `_run_jobspy(...)` forwards `problems`.
2. `guarded()` — its `except` already logs `"{name}: source failed"`; also `problems.append(f"{name} failed: {type(e).__name__}")`. A whole source blowing up is the most incomplete a result can be, and today it is invisible to the UI.
3. Row setup after `hours_old` is resolved from settings: CLI path (`search_id is None`) → `db.create_search(query, ", ".join(locs), hours_old)`; web path → `db.update_search(search_id, locations=…, hours_old=hours_old, status="running")`.
4. **Membership, strictly after `upsert_jobs`** (`PRAGMA foreign_keys=ON` is live — inserting a membership for a job not yet in `jobs` raises `IntegrityError` inside the search thread and surfaces as an unrelated `status='error'`):
```python
new = db.upsert_jobs(merged)
db.record_search_jobs(search_id, [j.id for j in merged])
db.update_search(search_id, found=len(merged), new=new)
```
`merged` is post-`filter_jobs` and holds new **and** already-known jobs — without the known ones, a second search for the same title shows an empty panel.
5. On completion (and on the error path): `db.update_search(…, warning="; ".join(problems)[:500])`.

---

## Step 5 — `web/app.py`

**`_lifespan`** — after `db.init()`, sweep orphans: `UPDATE searches SET status='error', warning='interrupted by a restart' WHERE status='running'`. **Put this in `_lifespan`, never in `init()`**: `run_search` calls `db.init()` at its own top, *after* `/search` created the row with `status='running'` — an init-time sweep would flip the search you just started to `error`.

**`index()`** — `active = db.latest_snapshot_search()`; pass `active_search` and `active_search_id` into the template. With the live DB (legacy rows only) `active` is `None`, so the panel renders the "Run a search above" empty state, per your decision; "all jobs ever found" is the way to reach history.

**`/jobs`** — add `search_id: str = ""`. **Type it `str`, not `int | None`**: the hidden input is always sent and is empty on a cold start, and `int | None` would 422 on `?search_id=` — which htmx's default `responseHandling` swallows silently, so the list would just stop updating with no error anywhere. Parse with a new `h.parse_search_id()` mapping `""`/`"abc"`/`"0"` → `None`.

```python
sid = h.parse_search_id(search_id)
search = db.get_search(sid) if sid else None
if anyloc or search is None or not search["has_snapshot"]:
    search, sid = None, None       # escape hatch / unknown id / legacy -> history
```
The `has_snapshot` check is what stops legacy rows 1–9 ever being presented as exact result sets. When `sid` is set: `list_jobs(..., search_id=sid, limit=500)` plus `count_search_jobs(sid)` for the total, and **derive the scope label from the search row, not the form** (`search["locations"]`, `h.hours_label(search["hours_old"])`) — that is the payoff for storing `hours_old`: the list stops claiming a window the user has since changed in the form.

**`anyloc`** — keep the parameter and the checkbox, widen its meaning to "ignore the active search *and* the location filter", relabel to **"all jobs ever found"**. `test_web.py:135` keeps passing unchanged.

**`/search`** — pass `hours` (already parsed at `app.py:114`) into `create_search`; render the status partial with `oob=True`.
**`/search/{id}/status`** — unchanged except `oob=False`; deliberately does *not* republish the hidden input on every 2 s poll.

`_latest_search_id()` at `app.py:131` is currently dead code — delete it; `db.latest_snapshot_search()` replaces it.

---

## Step 6 — HTMX wiring

**Carrying `search_id` across swaps: a hidden input in `#search-form`, published by `hx-swap-oob` from `/search`, read by the existing `hx-include`.** Rejected alternatives: OOB-swapping the `.filter` form (wipes the user's typed `q` and checkbox state); OOB-swapping `#job-list` with a fresh URL (fixes the auto-refresh but not the `.filter` form, which has its own `hx-get` — the exact class of bug being fixed); `hx-push-url` (the tab nav already pushes `/`); a server-side "current search" setting (least wiring, but `/jobs` stops being a pure function of its params — keep as fallback if the include plumbing turns fiddly).

**`tabs/jobs.html`:**
1. `<input type="hidden" id="active-search-id" name="search_id" value="{{ active_search_id }}">` inside `#search-form`.
2. Add `#search-form [name='search_id']` to the `.filter` form's `hx-include` and to `#job-list`'s.
3. Pre-render the restored search's status into `#search-status` (`{% with search=active_search, search_id=…, restored=True %}`), so a reload during a running search resumes polling instead of losing it.
4. **Cut `#job-list`'s triggers to `hx-trigger="load"`.** Delete `change from:#search-form` and `keyup changed delay:500ms from:#search-form input[name='locations']` — with `search_id` set, `/jobs` ignores `locations`/`hours_old`, so they re-fetch an identical list, the keyup one once per 500 ms of typing against the query that drops its LIMIT.
5. **Replace, don't edit, the two comments.** The one above `#job-list` ("Editing Locations or Posted-within re-filters what is already on screen straight away") is now the opposite of the truth: the list is one search's exact result set; Locations and Posted-within describe what the *next* search will fetch. The `hx-include` comment on `.filter` now describes the membership join.

**`partials/search_status.html`** — add, as top-level siblings of `.search-status`, guarded by `{% if oob and not error %}`:
```html
<input type="hidden" id="active-search-id" name="search_id" value="{{ search_id }}" hx-swap-oob="true">
<div id="job-list" hx-swap-oob="innerHTML"><p class="muted empty">Searching “{{ search['query'] }}”…</p></div>
```
**`hx-swap-oob="innerHTML"`, not `"true"`** — an outerHTML swap would replace the div along with its `hx-trigger="load"`, and the replacement's `load` would immediately re-fetch the *old* scope, putting the stale cards straight back. htmx here is 2.0.10 with `allowNestedOobSwaps:true`, so the top-level `<input>` OOB is fine.

Also in this file: header `found {{ n }}` → `{{ n }} matched · {{ new }} new`; a warning line independent of the `empty` branch so it shows even when results came back (`{% if search['warning'] %}Incomplete results — …{% endif %}`); and the `{% if st == 'done' %}` refresh div gains `search_id` in its `hx-include` and a `and not restored` guard so cold load doesn't duplicate `#job-list`'s own `load`.

**`partials/job_list.html` — the empty state must be four-way, not two-way.** New context: `search`, `total`, `shown`.

| when | copy |
|---|---|
| `search.status == 'running'` | "Searching “Q” in L… results appear when it finishes." |
| `total == 0` and `search.warning` | "Nothing came back, and sources failed or were rate-limited, so this is incomplete: {warning}. Run it again." |
| `total == 0`, no warning | "“Q” matched nothing in L within W. Widen the locations or the window and search again." |
| `total > 0`, `shown == 0` | "All {total} jobs from this search score below {min_score}. Tick “show low scores”." |
| `shown > 0` | "Showing {shown} of {total} · “Q” · {scope}" (+ warning banner) |

The fourth row is the one the codex plan misses and it matters most: `min_score=3` is the default, so a search returning ten 2★ jobs would otherwise read as "the bug is back". "Showing M of N" is also where the list's number and the header's "N matched" are reconciled — the list is the only place that names both, so they can't contradict. If `total != search['found']`, append "({{ found - total }} hidden)". When `search is None`, keep today's copy with the scope reading "all jobs ever found".

---

## Step 7 — Tests

**`tests/test_discovery.py`** — a parametrized table over `countries_named` covering every row in the Context table plus every "must stay unchanged" case from Step 1; matcher regressions (`not matches("Waterloo, NSW, AU", False, ["Canada"])`, `matches(…, ["Australia"])`, `not matches("Perth, Scotland", False, ["Australia"])`, `not matches("Birmingham, AL", False, ["United Kingdom"])`); guards that the walker did not leak into `canonical` (`canonical("NL") == "netherlands"`, `canonical("UK") == "united kingdom"`); and deliberately-pinned rows for the documented residual gaps so a future "fix" has to confront them.

Plus: `run_search` records memberships for new **and** already-known jobs (search 1 over {A,B}, search 2 over {B,C} → exactly `{A,B}` and `{B,C}` — **the single most important assertion in this change**); `hours_old` + `has_snapshot` are stored on the CLI path; a failing source sets `warning` while `status` stays `done`; `INSERT OR IGNORE` survives a repeated identical search. And for `fetch_jobspy`: a location-level exception and a location-level timeout each populate `problems` (this is exactly the Australia failure, which today produces nothing), and the 4-arg call still works.

**`tests/test_web.py`** (the `client` fixture already monkeypatches `db.DB_PATH`) — two searches render separate lists (*the* regression test); scope ignores the form's `locations`/`hours_old`; a zero-result search shows the empty state and none of the three seeded history jobs; `warning` renders "incomplete"; all-filtered-by-score says "0 of 3" and offers "show low scores" rather than "matched nothing", and `&low=1` shows all three; "3 of 5" reconciles; a hidden member keeps the counts consistent; a running search shows a searching state; a `has_snapshot=0` row falls back to history; `?search_id=` and `?search_id=999` both return 200 (not 422); `/search` clears the list and publishes the id (monkeypatch `_spawn`); cold load restores the newest snapshot search and falls back to history with legacy-only; `anyloc` escapes the active search; `init()` migrates a legacy `searches` table idempotently (call twice, legacy row survives with `has_snapshot=0`); `_lifespan` sweeps orphaned running searches.

---

## Step 8 — Verification

**Back up first.** WAL is on, so copying the file alone is not a backup:
`sqlite3 data/jobbot.db ".backup 'data/jobbot-backup-$(date +%Y%m%d).db'"`

1. `.venv/bin/python -m pytest -q` → **323 + new**, zero failures.
2. Restart. Live PID is **63438** running `/opt/homebrew/…/Python -m jobbot` — **Homebrew python, not `.venv/bin/python`**; relaunch with the same interpreter. `pkill -fi "python -m jobbot"`, then `lsof -nP -iTCP:8000 -sTCP:LISTEN` and confirm the PID differs from 63438.
3. Confirm the migration ran against the real DB: `PRAGMA table_info(searches)` shows the three new columns, `.schema search_jobs` exists, and `SELECT id, has_snapshot FROM searches` shows 1–9 all `0`.
4. **Two-title comparison**, proved in SQL rather than by eyeballing cards:
```sql
SELECT (SELECT COUNT(*) FROM search_jobs WHERE search_id=A),
       (SELECT COUNT(*) FROM search_jobs WHERE search_id=B),
       (SELECT COUNT(*) FROM search_jobs a JOIN search_jobs b USING(job_id)
          WHERE a.search_id=A AND b.search_id=B);
```
Use two **genuinely unrelated** titles for the proof (see R1).
5. Zero-result check: search a nonsense title → empty state, and `#job-list` no longer contains the prior `article.job` ids.
6. Incomplete-results check: a real 429 is hard to force; either trust the unit test or `UPDATE searches SET warning='forced test'` on a throwaway row and refresh.

---

## Risks

**R1 — the live comparison can look like a failure even when the fix works.** `expand_query`/`title_matches` deliberately treat "Forward Deployed Engineer", "AI Engineer", "Applied AI" and "Solutions Engineer" as one family, and `scorer.py` scores fit-to-*you*, not fit-to-the-query, so a job keeps one score across searches. Two related titles over the same locations will legitimately overlap. After this change their sets will be *different*, not *disjoint*. Pick unrelated titles for the proof.

**R2 — the UI double-submits searches.** Live rows 6/7 (1 s apart) and 8/9 (3 s apart) are identical query+locations pairs. Post-change this is mostly benign (the second response wins the hidden input and the OOB clear), but it doubles scraping and LLM spend. `hx-disabled-elt` won't help — the POST returns as soon as the thread is spawned. The real fix is server-side in `start_search`: if a `running` search with the same query+locations started within ~10 s, return its status instead of creating a row. **Separable — flagging rather than folding in.**

**R3 — the history path stays slow.** With `locations` set and no `search_id`, `list_jobs` still drops the LIMIT and scans everything. Dropping `description` from the SELECT and removing the per-keystroke trigger takes most of the sting out, and the path is now only reachable via "all jobs ever found" or a legacy-only DB. Bounding it properly changes semantics (a SQL LIMIT before the Python filter can drop real matches), so leave it and say so in the docstring.

**R4 — where this diverges from `JOB_SEARCH_FIX_PLAN.md`.** That doc orders results `posted_at DESC` with score as tie-break and deletes the "any location" checkbox. Per your decisions this plan keeps **score-first** ordering (changing it is a product decision unrelated to the bug, and everything in a search is already inside the freshness window) and **keeps the checkbox** with a widened meaning and a new label. The doc also omits the `searches` migration entirely, which would have silently broken `init()` against the live DB.

**R5 — connection hygiene.** `with db.connect() as c` commits but never closes; this adds two more connections per search and one per `/jobs` request. Pre-existing pattern, not worsened in kind. Out of scope.
