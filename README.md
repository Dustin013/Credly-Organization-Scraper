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

Discovered organizations and the list of completed seeds are persisted to
`credly_orgs_progress.json` after every 25 queries, so the run is fully
**resumable** — re-running the script picks up where it left off.

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
[start] seeds total=778 done=0 remaining=778 known_orgs=0
[   1/778] q='0'      got=12 new=12 total_orgs=   12 (1.4s)
[   2/778] q='1'      got= 8 new= 7 total_orgs=   19 (1.6s)
...
[done] wrote 4231 rows -> credly_organizations.xlsx (unique slugs=4231, runtime=18.7 min)
```

When the run finishes (or you stop it with `Ctrl+C`), the script writes
`credly_organizations.xlsx`.

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
| `credly_organizations.xlsx`   | Final output (generated).                                  |
| `credly_orgs_progress.json`   | Resume cache: discovered orgs + completed seeds (generated). |

---

## Tuning

A few constants near the top of `credly.py` you may want to tweak:

- `KEYWORD_SEEDS` — add domain‑specific terms to surface more orgs in
  niches you care about.
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
