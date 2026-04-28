"""
Scrape all public Credly organizations and write them to credly_organizations.xlsx.

Method
------
Credly's public organization search page is a SPA that calls
    GET https://www.credly.com/api/v1/global_search?q=<query>
and the response includes up to 50 results of type "Organization" per query.
There is no pagination on this endpoint, so we issue many seed queries
(a-z, 0-9, aa-zz, plus topical keywords), dedupe by slug, and export.

Adaptive drill-down: any seed that returns the full 50-result cap is
almost certainly hiding additional orgs behind it, so we automatically
queue child queries (e.g. ``in`` -> ``ina..inz``, ``academy`` ->
``academy a..academy z``) and recurse until the cap is no longer hit.

Output: credly_organizations.xlsx with two columns (Organization Name, URL).
Resume: credly_orgs_progress.json caches discovered orgs, completed seeds,
        and the pending drill-down queue.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import string
import sys
import time
from typing import Dict, Iterable, TextIO

import requests
from openpyxl import Workbook

API_URL = "https://www.credly.com/api/v1/global_search"
PROFILE_URL_TEMPLATE = "https://www.credly.com/organizations/{slug}/badges"
PROGRESS_FILE = "credly_orgs_progress.json"
OUTPUT_FILE = "credly_organizations.xlsx"
LOG_FILE = "credly_run.log"

# Credly's global_search endpoint returns at most this many hits per query
# and has no pagination, so any seed that returns exactly RESULT_CAP results
# is almost certainly hiding more orgs behind it -- we drill deeper on those.
RESULT_CAP = 50
# Hard ceiling on how long an auto-generated child query can get. Prevents
# runaway expansion if Credly ever returns 50 for very long strings.
MAX_QUERY_LEN = 12
# Maximum number of word-suffix drill-downs allowed on a phrase query.
# e.g. depth=1 allows ``academy`` -> ``academy a..z`` but blocks
# ``academy a`` -> ``academy a a..z``. Past depth 1 the API tends to keep
# returning the cap with 0 new results because the trailing single letters
# don't meaningfully narrow the substring match.
MAX_PHRASE_DEPTH = 1
# Checkpoint cadence -- save progress whichever happens first.
SAVE_EVERY_N = 25
SAVE_EVERY_SECS = 30.0


class Tee:
    """Minimal stdout tee that mirrors writes to a log file.

    Lets the script log to ``credly_run.log`` automatically without the
    user having to ``| tee`` it from the shell. Line-buffered so the log
    is readable in real time.
    """

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.credly.com/search/organizations",
}

# Topical keyword seeds. Chosen because they are common substrings of
# organization names on Credly and tend to surface different result sets
# than single/double letter searches.
KEYWORD_SEEDS = [
    "academy", "university", "college", "school", "institute", "association",
    "society", "council", "federation", "foundation", "consortium", "alliance",
    "certification", "certified", "credential", "training", "learning",
    "education", "professional", "international", "national", "global",
    "group", "company", "corporation", "incorporated", "limited", "services",
    "solutions", "systems", "technology", "technologies", "digital", "software",
    "cloud", "security", "cyber", "data", "analytics", "ai", "machine",
    "network", "networks", "devops", "agile", "scrum", "project", "product",
    "design", "marketing", "sales", "finance", "financial", "banking", "bank",
    "insurance", "health", "healthcare", "medical", "nursing", "pharmacy",
    "engineering", "manufacturing", "construction", "energy", "telecom",
    "transport", "logistics", "retail", "hospitality", "tourism", "media",
    "consulting", "advisory", "research", "science", "math", "language",
    "microsoft", "aws", "amazon", "google", "oracle", "ibm", "cisco", "adobe",
    "salesforce", "sap", "vmware", "redhat", "linux", "intel", "nvidia",
    "dell", "hp", "hewlett", "siemens", "huawei", "samsung", "apple", "meta",
    "africa", "asia", "europe", "america", "australia", "canada", "india",
    "china", "japan", "korea", "germany", "france", "italy", "spain",
    "brazil", "mexico", "uk", "british", "english", "french", "spanish",
    "ministry", "department", "government", "agency", "authority", "bureau",
    "center", "centre", "club", "league", "union", "trust", "fund", "board",
    "the", "of", "for", "and",
]


def build_seeds() -> list[str]:
    """Return a deduplicated list of seed query strings."""
    seeds: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = q.strip().lower()
        if q and q not in seen:
            seen.add(q)
            seeds.append(q)

    for c in string.digits:
        add(c)
    for c in string.ascii_lowercase:
        add(c)
    for a, b in itertools.product(string.ascii_lowercase, repeat=2):
        add(a + b)
    for kw in KEYWORD_SEEDS:
        add(kw)
    return seeds


def extract_slug(url: str | None) -> str | None:
    """Pull the organization slug from a `/organizations/<slug>` URL."""
    if not url:
        return None
    parts = url.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "organizations":
        slug = parts[1].strip()
        # Reject anything obviously not a slug.
        if slug and "?" not in slug and " " not in slug:
            return slug
    return None


def fetch_seed(session: requests.Session, query: str, retries: int = 3) -> list[dict]:
    """Return the list of Organization results for a single search query."""
    params = {"q": query}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(API_URL, params=params, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [429] rate limited; sleeping {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 3 * (attempt + 1)
                print(f"  [{resp.status_code}] server error; sleeping {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json().get("data", {}) or {}
            results = data.get("results") or []
            return [r for r in results if r.get("type") == "Organization"]
        except (requests.RequestException, ValueError) as e:
            last_err = e
            wait = 2 * (attempt + 1)
            print(f"  [retry {attempt + 1}] {e!r}; sleeping {wait}s")
            time.sleep(wait)
    print(f"  [give up] {query!r}: {last_err!r}")
    return []


def expand_query(query: str) -> list[str]:
    """Generate child queries to drill into a capped result set.

    Strategy:
    - Short tokens (<=4 chars, no spaces): append a-z to extend the prefix,
      e.g. ``in`` -> ``ina, inb, ..., inz``.
    - Longer tokens / phrases: append " a" .. " z", which on Credly's
      substring-style search reliably surfaces a different slice of orgs,
      e.g. ``academy`` -> ``academy a, academy b, ...``. Bounded by
      ``MAX_PHRASE_DEPTH`` so we don't recurse forever on broad terms.
    """
    q = query.strip()
    if not q or len(q) >= MAX_QUERY_LEN:
        return []
    if len(q) <= 4 and " " not in q:
        return [q + c for c in string.ascii_lowercase]
    # Phrase / longer-token expansion -- enforce depth limit.
    if q.count(" ") >= MAX_PHRASE_DEPTH:
        return []
    return [f"{q} {c}" for c in string.ascii_lowercase]


def load_progress() -> tuple[Dict[str, str], set[str], list[str]]:
    """Load (orgs_by_slug, completed_seeds, pending_seeds) from disk."""
    if not os.path.exists(PROGRESS_FILE):
        return {}, set(), []
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        return (
            dict(blob.get("orgs", {})),
            set(blob.get("completed", [])),
            list(blob.get("pending", [])),
        )
    except Exception as e:
        print(f"[warn] could not read {PROGRESS_FILE}: {e}; starting fresh")
        return {}, set(), []


def save_progress(
    orgs: Dict[str, str],
    completed: Iterable[str],
    pending: Iterable[str] = (),
) -> None:
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "orgs": orgs,
                "completed": sorted(completed),
                "pending": list(pending),
            },
            f,
        )
    os.replace(tmp, PROGRESS_FILE)


def write_xlsx(orgs: Dict[str, str], path: str) -> int:
    """Write final Excel file. Returns row count."""
    rows = [
        (name, PROFILE_URL_TEMPLATE.format(slug=slug))
        for slug, name in orgs.items()
        if name and slug
    ]
    rows.sort(key=lambda r: r[0].casefold())

    wb = Workbook()
    ws = wb.active
    ws.title = "Organizations"
    ws.append(["Organization Name", "URL"])
    for name, url in rows:
        ws.append([name, url])
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 90
    wb.save(path)
    return len(rows)


def main() -> int:
    # Mirror everything we print to a log file so progress is captured
    # without needing a shell `| tee`. Line-buffered for live tailing.
    log_fp = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_fp)  # type: ignore[assignment]
    sys.stderr = Tee(sys.__stderr__, log_fp)  # type: ignore[assignment]
    print(f"\n[run] {time.strftime('%Y-%m-%d %H:%M:%S')} ----------------")

    base_seeds = build_seeds()
    orgs, completed, pending = load_progress()

    # Build the work queue: anything previously queued for drill-down first,
    # then any base seeds we haven't yet processed. Dedupe against `completed`.
    # Drop any pending entry that violates the current expansion rules
    # (e.g. left over from before MAX_PHRASE_DEPTH existed) so we don't waste
    # requests grinding through a queue that can never produce new results.
    queue: list[str] = []
    seen_in_queue: set[str] = set()
    pruned = 0

    def is_allowed(q: str) -> bool:
        if len(q) > MAX_QUERY_LEN:
            return False
        # Phrase queries deeper than MAX_PHRASE_DEPTH almost never yield new
        # orgs -- they hit the cap because Credly's search loosely matches
        # the leading token and ignores the trailing single letters.
        if " " in q and q.count(" ") > MAX_PHRASE_DEPTH:
            return False
        return True

    def enqueue(q: str) -> None:
        nonlocal pruned
        q = q.strip().lower()
        if not q or q in completed or q in seen_in_queue:
            return
        if not is_allowed(q):
            pruned += 1
            return
        seen_in_queue.add(q)
        queue.append(q)

    for q in pending:
        enqueue(q)
    for q in base_seeds:
        enqueue(q)

    print(
        f"[start] base_seeds={len(base_seeds)} done={len(completed)} "
        f"queue={len(queue)} pruned_pending={pruned} known_orgs={len(orgs)}"
    )

    session = requests.Session()
    started_at = time.time()
    processed = 0
    capped_expansions = 0
    last_save = time.time()

    try:
        while queue:
            seed = queue.pop(0)
            seen_in_queue.discard(seed)
            processed += 1
            t0 = time.time()
            results = fetch_seed(session, seed)
            new = 0
            for r in results:
                slug = extract_slug(r.get("url"))
                name = (r.get("name") or "").strip()
                if not slug or not name:
                    continue
                # Prefer the longer / non-empty name if we see it again.
                prev = orgs.get(slug)
                if prev is None or (len(name) > len(prev) and name):
                    if prev is None:
                        new += 1
                    orgs[slug] = name
            completed.add(seed)

            # Adaptive drill-down: only worth doing if (a) we hit the cap AND
            # (b) this query actually surfaced new orgs. A capped query that
            # produced 0 new orgs means the result set is already fully
            # captured by some broader query upstream -- drilling deeper into
            # the same lexical neighborhood will just keep returning the same
            # 50 already-known slugs. Skipping these is the single biggest
            # speed win once the easy wins have been collected.
            expanded = 0
            skipped_unproductive = False
            if len(results) >= RESULT_CAP:
                if new > 0:
                    for child in expand_query(seed):
                        if child not in completed and child not in seen_in_queue:
                            enqueue(child)
                            expanded += 1
                    if expanded:
                        capped_expansions += 1
                else:
                    skipped_unproductive = True

            elapsed = time.time() - t0
            if expanded:
                tag = f" +{expanded}"
            elif skipped_unproductive:
                tag = " skip"
            else:
                tag = ""
            print(
                f"[{processed:>5}|q={len(queue):>4}] q={seed!r:<14} "
                f"got={len(results):>2} new={new:>2} "
                f"total_orgs={len(orgs):>5}{tag} ({elapsed:.1f}s)"
            )
            # Checkpoint on whichever fires first: every N queries, or every
            # SAVE_EVERY_SECS of wall time. Keeps progress.json fresh even on
            # slow API responses.
            if (
                processed % SAVE_EVERY_N == 0
                or (time.time() - last_save) >= SAVE_EVERY_SECS
            ):
                save_progress(orgs, completed, queue)
                last_save = time.time()
            # Polite jitter between calls.
            time.sleep(random.uniform(1.0, 2.5))
    except KeyboardInterrupt:
        print("\n[interrupt] saving progress and writing partial xlsx...")
    finally:
        save_progress(orgs, completed, queue)
        # Always write the xlsx -- even on interrupt -- so the user gets
        # a usable snapshot of everything found so far.
        try:
            rows = write_xlsx(orgs, OUTPUT_FILE)
        except Exception as e:
            rows = -1
            print(f"[warn] failed to write xlsx: {e!r}")
        total_elapsed = time.time() - started_at
        print(
            f"[done] wrote {rows} rows -> {OUTPUT_FILE} "
            f"(unique slugs={len(orgs)}, capped_expansions={capped_expansions}, "
            f"queue_remaining={len(queue)}, runtime={total_elapsed/60:.1f} min)"
        )
        log_fp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
