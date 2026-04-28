"""
Scrape all public Credly organizations and write them to credly_organizations.xlsx.

Method
------
Credly's public organization search page is a SPA that calls
    GET https://www.credly.com/api/v1/global_search?q=<query>
and the response includes up to 50 results of type "Organization" per query.
There is no pagination on this endpoint, so we issue many seed queries
(a-z, 0-9, aa-zz, plus topical keywords), dedupe by slug, and export.

Output: credly_organizations.xlsx with two columns (Organization Name, URL).
Resume: credly_orgs_progress.json caches discovered orgs + completed seeds.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import string
import sys
import time
from typing import Dict, Iterable

import requests
from openpyxl import Workbook

API_URL = "https://www.credly.com/api/v1/global_search"
PROFILE_URL_TEMPLATE = "https://www.credly.com/organizations/{slug}/badges"
PROGRESS_FILE = "credly_orgs_progress.json"
OUTPUT_FILE = "credly_organizations.xlsx"

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


def load_progress() -> tuple[Dict[str, str], set[str]]:
    """Load (orgs_by_slug, completed_seeds) from disk if present."""
    if not os.path.exists(PROGRESS_FILE):
        return {}, set()
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        return dict(blob.get("orgs", {})), set(blob.get("completed", []))
    except Exception as e:
        print(f"[warn] could not read {PROGRESS_FILE}: {e}; starting fresh")
        return {}, set()


def save_progress(orgs: Dict[str, str], completed: Iterable[str]) -> None:
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"orgs": orgs, "completed": sorted(completed)}, f)
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
    seeds = build_seeds()
    orgs, completed = load_progress()
    todo = [s for s in seeds if s not in completed]
    print(
        f"[start] seeds total={len(seeds)} done={len(completed)} "
        f"remaining={len(todo)} known_orgs={len(orgs)}"
    )

    session = requests.Session()
    started_at = time.time()

    try:
        for i, seed in enumerate(todo, 1):
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
            elapsed = time.time() - t0
            print(
                f"[{i:>4}/{len(todo)}] q={seed!r:<8} "
                f"got={len(results):>2} new={new:>2} "
                f"total_orgs={len(orgs):>5} ({elapsed:.1f}s)"
            )
            if i % 25 == 0:
                save_progress(orgs, completed)
            # Polite jitter between calls.
            time.sleep(random.uniform(1.0, 2.5))
    except KeyboardInterrupt:
        print("\n[interrupt] saving progress and exiting...")
    finally:
        save_progress(orgs, completed)

    rows = write_xlsx(orgs, OUTPUT_FILE)
    total_elapsed = time.time() - started_at
    print(
        f"[done] wrote {rows} rows -> {OUTPUT_FILE} "
        f"(unique slugs={len(orgs)}, runtime={total_elapsed/60:.1f} min)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
