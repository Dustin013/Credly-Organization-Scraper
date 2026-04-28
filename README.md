# Credly Organization Scraper

A small, polite Python script that discovers public organizations on
[Credly](https://www.credly.com/) and exports them to an Excel file
(`credly_organizations.xlsx`) with two columns: **Organization Name** and
**URL** (linking to the org's public badges page).

> Uses only Credly's public, unauthenticated `global_search` endpoint —
> the same one the website itself calls. No login, no API key.

---

## How it works

Credly's public organization search page is a single‑page app that calls:

```
GET https://www.credly.com/api/v1/global_search?q=<query>
```

The endpoint returns up to **50 results per query** and has **no
pagination**. To surface as many organizations as possible the scraper
issues a large set of seed queries, then dedupes the results by slug:

- every digit `0`–`9`
- every letter `a`–`z`
- every two‑letter combination `aa`–`zz` (676 queries)
- a curated list of topical keywords (industries, vendors, regions, etc.)

**Adaptive drill-down.** Because the API caps at 50, any seed that
returns exactly 50 results is hiding more organizations behind it. The
scraper auto-queues child queries for every capped seed and recurses
until the cap is no longer hit:

- short tokens like `in` → `ina, inb, …, inz`
- longer keywords like `academy` → `academy a, academy b, …, academy z`

Discovered organizations, the list of completed seeds, **and the
pending drill-down queue** are persisted to `credly_orgs_progress.json`
after every 25 queries, so the run is fully **resumable** — re-running
the script picks up where it left off.

---

## Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/)
- [`openpyxl`](https://pypi.org/project/openpyxl/)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
python credly.py
```

You'll see a progress line per seed query, e.g.:

```
[start] base_seeds=848 done=0 queue=848 known_orgs=0
[    1|q= 847] q='0'            got= 0 new= 0 total_orgs=    0 (0.6s)
[   11|q= 837] q='a'            got=50 new=50 total_orgs=   50 +26 (0.5s)
...
[done] wrote 4231 rows -> credly_organizations.xlsx (unique slugs=4231, capped_expansions=124, queue_remaining=0, runtime=42.7 min)
```

The `[N|q=K]` prefix shows the Nth processed query and `K` queries
still in the queue. A trailing `+N` means the query hit the 50‑result
cap and `N` child queries were auto-queued for drill-down.

**Logging.** Everything printed to the terminal is also appended to
`credly_run.log` automatically — no need to pipe through `tee`. Tail it
from another terminal to watch progress live:

```bash
tail -f credly_run.log
```

**Checkpointing.** Progress is saved to `credly_orgs_progress.json`
every 25 queries *or* every 30 seconds, whichever comes first. The
final `credly_organizations.xlsx` is written at the end of the run
**and also when you `Ctrl+C`**, so an interrupted run still produces a
usable snapshot of everything found so far.

### Resuming an interrupted run

Just run the same command again:

```bash
python credly.py
```

Anything in `credly_orgs_progress.json` is loaded back in, completed
seeds are skipped, and the run continues. Delete that file to start
completely fresh.

---

## Output

`credly_organizations.xlsx`

| Organization Name | URL                                                        |
|-------------------|------------------------------------------------------------|
| Example Academy   | https://www.credly.com/organizations/example-academy/badges |
| ...               | ...                                                        |

Rows are sorted alphabetically (case‑insensitive) by organization name.

---

## Files

| File                          | Purpose                                                    |
|-------------------------------|------------------------------------------------------------|
| `credly.py`                   | The scraper.                                               |
| `requirements.txt`            | Python dependencies.                                       |
| `credly_organizations.xlsx`   | Final output (generated, also written on `Ctrl+C`).        |
| `credly_orgs_progress.json`   | Resume cache: discovered orgs, completed seeds, pending drill-down queue (generated). |
| `credly_run.log`              | Auto-appended log of every run (generated).                |

---

## Tuning

A few constants near the top of `credly.py` you may want to tweak:

- `KEYWORD_SEEDS` — add domain‑specific terms to surface more orgs in
  niches you care about.
- `SAVE_EVERY_N` / `SAVE_EVERY_SECS` — how often progress is checkpointed
  to `credly_orgs_progress.json` (default: every 25 queries or 30 seconds).
- `MAX_QUERY_LEN` — hard cap on auto-generated drill-down query length
  (default: 12).
- The `time.sleep(random.uniform(1.0, 2.5))` jitter between requests —
  keep it polite. Lowering it materially raises the chance of `429`s.
- `retries` in `fetch_seed()` — bump if your network is flaky.

The script already handles HTTP `429` (rate limit) and `5xx` responses
with exponential‑ish back‑off.

---

## Notes & etiquette

- This script only reads the **public** search endpoint that powers
  credly.com's own organization search page. It does not authenticate,
  does not scrape badge holders, and does not bypass any access
  controls.
- Be respectful: don't crank up concurrency, don't remove the sleeps,
  and don't run it in a tight loop. One full pass per day is more than
  enough for any reasonable use case.
- Credly may change their endpoints at any time. If the script suddenly
  returns 0 results, the response shape probably changed — open an
  issue or send a PR.

---

## License

MIT — see [`LICENSE`](LICENSE).
