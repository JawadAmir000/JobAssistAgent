# Fix Repeated Job Search Results

## Summary

The Jobs panel currently reads from one shared job history instead of showing the results produced by the current search. Although discovery receives the entered title, location, and date window, the completed-search refresh does not use the title or identify the search. Different searches can therefore display the same job cards.

An earlier Australia-only search returned no results because its scraper was rate-limited. Since the old cards remained visible, the failure looked like the application had ignored Australia and repeated the previous search. Location matching also has an ambiguity where an Australian location such as `Waterloo, NSW, AU` can incorrectly match Canada.

The intended outcome is for every search to show only its own results, with the newest postings first and clear warnings when sources fail or return incomplete results.

## Key Changes

- Add a `search_jobs(search_id, job_id)` association so every search owns an exact result set.
- Store each search's freshness window and mark new searches as snapshot-capable. Do not attempt to reconstruct memberships for older searches because that information was never stored.
- Associate every filtered result with the current search, including jobs that already exist in the database.
- Update the Jobs query to accept a `search_id` and return only jobs associated with that search.
- Sort results by posting date descending, falling back to first-seen time when no posting date is available. Use match score as the tie-breaker.
- Clear the previous cards as soon as a new search starts.
- When a search completes, refresh the Jobs panel using its `search_id`.
- Show a genuine empty state when a search finds nothing instead of displaying historical jobs.
- Surface scraper failures and rate limits as an `incomplete results` warning.
- Restore the latest snapshot-capable search after a page reload. If only legacy searches exist, ask the user to run a new search.
- Fix country matching so explicit country names, country codes, and region codes take precedence over ambiguous city-name inference. For example, `Waterloo, NSW, AU` must match Australia but not Canada.
- Remove the result panel's `any location` option because it conflicts with showing the exact result set. Users can change the location and run another search instead.

## Interface and Data Changes

- Add a `search_jobs` table with a composite primary key of `search_id` and `job_id`, plus foreign keys to `searches` and `jobs`.
- Extend `searches` with the selected `hours_old` value and a flag identifying searches with reliable result snapshots.
- Update `create_search()` to record the freshness window.
- Add database operations for recording and retrieving a search's job memberships.
- Extend `db.list_jobs()` with an optional `search_id` argument.
- Add `search_id` to the `/jobs` endpoint. Location and date fields define discovery; the text and score controls filter only within the selected search.
- Keep the current title-synonym behavior. Related searches may overlap, but they will no longer appear identical because of unrelated historical jobs.

## Search and UI Flow

1. The user submits a title, location, freshness window, and source selection.
2. The server creates a search record containing those criteria.
3. The UI immediately replaces the previous cards with a searching state.
4. Discovery fetches jobs, deduplicates them, applies location and freshness rules, and upserts them into the shared jobs table.
5. Every retained job is linked to the current search, whether the job is new or already known.
6. The completed-search response reloads `/jobs` with the current `search_id`.
7. The Jobs panel displays only that search's jobs, ordered newest-first.
8. If no jobs matched, the panel remains empty and explains whether nothing matched or sources failed.

## Test Plan

- Verify two different title searches create and render independent result sets.
- Verify an Australia-only search cannot display Canadian or unrelated historical jobs.
- Verify a zero-result search clears the previous search's cards.
- Verify a failed or incomplete source produces a visible warning.
- Verify jobs already stored in the database are still associated with a later search.
- Verify results are ordered by posting date, then first-seen time, then match score.
- Verify the low-score and text filters operate only within the active search.
- Verify page reloads restore the latest snapshot-capable search.
- Verify legacy searches without recorded memberships are not presented as exact result sets.
- Add regression coverage proving `Waterloo, NSW, AU` matches Australia and does not match Canada.
- Preserve the existing remote-country restrictions and other location-matching behavior.
- Run the discovery and web test suites.
- Restart the server because it runs with automatic reload disabled.
- Repeat the live comparison using two different titles and confirm their displayed job IDs are no longer identical.

## Assumptions

- "Latest" means newest posting date first, with first-seen time used when a source supplies no posting date.
- Existing jobs and applications remain untouched.
- Legacy search memberships will not be backfilled using guesses.
- Existing broad title synonyms remain in scope; tightening title matching would be a separate product change.
