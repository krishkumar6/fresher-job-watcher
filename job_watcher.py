#!/usr/bin/env python3
"""
Fresher Job Watcher
-------------------
Polls company career portals (Greenhouse / Lever / Ashby / SmartRecruiters /
Workday) directly, filters postings by your keywords + location, and sends a
Telegram notification for every NEW posting it hasn't seen before.

Designed to run on a schedule (GitHub Actions cron) so you get alerted within
~1-2 hours of a job going live on the company's own portal.

Usage:
    python job_watcher.py            # normal run (needs TELEGRAM_* env vars)
    python job_watcher.py --test     # validate config, print matches, no alerts
"""

import html as html_lib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

# Windows terminals default to cp1252, which can't print the emoji used in
# alert text during --test runs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG_FILE = Path(__file__).parent / "companies.yaml"
STATE_FILE = Path(__file__).parent / "seen_jobs.json"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (job-watcher; personal use)"}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TEST_MODE = "--test" in sys.argv


# ----------------------------------------------------------------------------
# ATS fetchers -- each returns a list of {id, title, location, url} plus either
# a "description" (if the list API already includes it) or a "_desc" reference
# so the description can be fetched lazily for NEW matches only.
# ----------------------------------------------------------------------------

def strip_html(s):
    s = html_lib.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return html_lib.unescape(s)


def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"greenhouse:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "_desc": {"ats": "greenhouse", "slug": slug, "job_id": j["id"]},
        })
    return jobs


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        loc = (j.get("categories") or {}).get("location", "") or ""
        # Requirements usually live in the "lists" blocks, not the intro text.
        lists_text = " ".join(
            f"{l.get('text', '')} {l.get('content', '')}"
            for l in (j.get("lists") or [])
        )
        desc = strip_html(
            f"{j.get('descriptionPlain') or j.get('description', '')} "
            f"{lists_text} {j.get('additionalPlain') or ''}"
        ).strip()
        jobs.append({
            "id": f"lever:{slug}:{j.get('id')}",
            "title": j.get("text", ""),
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "description": desc or None,
        })
    return jobs


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"ashby:{slug}:{j.get('id')}",
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", ""),
            "description": strip_html(j.get("descriptionHtml") or "").strip() or None,
        })
    return jobs


def fetch_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        loc_str = ", ".join(filter(None, [loc.get("city", ""), loc.get("country", "")]))
        jobs.append({
            "id": f"smartrecruiters:{slug}:{j.get('id')}",
            "title": j.get("name", ""),
            "location": loc_str,
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            "_desc": {"ats": "smartrecruiters", "slug": slug, "job_id": j.get("id")},
        })
    return jobs


def fetch_workday(careers_url, search_text=""):
    """
    careers_url looks like:
      https://company.wd5.myworkdayjobs.com/en-US/SiteName
    We derive: host, tenant (first subdomain label), site (last path segment).
    """
    parsed = urlparse(careers_url)
    host = parsed.netloc
    tenant = host.split(".")[0]
    site = [p for p in parsed.path.split("/") if p][-1]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset = [], 0
    while offset <= 100:  # up to ~2 pages is plenty for new postings
        payload = {"limit": 20, "offset": offset, "searchText": search_text,
                   "appliedFacets": {}}
        r = requests.post(api, json=payload, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            jobs.append({
                "id": f"workday:{tenant}:{path}",
                "title": j.get("title", ""),
                "location": j.get("locationsText", "") or "",
                "url": f"https://{host}/en-US/{site}{path}" if path else careers_url,
                "_desc": {"ats": "workday", "host": host, "tenant": tenant,
                          "site": site, "path": path},
            })
        offset += 20
    return jobs


def fetch_description(job):
    """
    Full description text for one job. Returns None when it can't be fetched
    (unknown ATS detail API, network error) -- callers treat None as
    "couldn't verify" and keep the job rather than silently dropping it.
    Only called for NEW matching jobs, so this adds a handful of requests
    per run at most.
    """
    if job.get("description"):
        return job["description"]
    ref = job.get("_desc")
    if not ref or (ref["ats"] != "workday" and not ref.get("job_id")):
        return None
    try:
        if ref["ats"] == "greenhouse":
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{ref['slug']}/jobs/{ref['job_id']}",
                headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return strip_html(r.json().get("content", "")).strip() or None
        if ref["ats"] == "smartrecruiters":
            r = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{ref['slug']}/postings/{ref['job_id']}",
                headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            sections = ((r.json().get("jobAd") or {}).get("sections") or {})
            text = " ".join((s or {}).get("text", "") for s in sections.values())
            return strip_html(text).strip() or None
        if ref["ats"] == "workday":
            r = requests.get(
                f"https://{ref['host']}/wday/cxs/{ref['tenant']}/{ref['site']}{ref['path']}",
                headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            desc = (r.json().get("jobPostingInfo") or {}).get("jobDescription", "")
            return strip_html(desc).strip() or None
    except Exception as e:
        print(f"[desc-error] {job['title']}: {e}")
    return None


FETCHERS = {
    "greenhouse": lambda c: fetch_greenhouse(c["slug"]),
    "lever": lambda c: fetch_lever(c["slug"]),
    "ashby": lambda c: fetch_ashby(c["slug"]),
    "smartrecruiters": lambda c: fetch_smartrecruiters(c["slug"]),
    "workday": lambda c: fetch_workday(c["url"], c.get("search_text", "")),
}


# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------

def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


# Titles that spell out "X+ years" / "X to Y years" experience requirements
# are almost always senior postings, even when they also contain a fresher
# keyword like "software engineer". None of exclude_keywords catches this
# since the years vary, so it's handled separately here.
EXPERIENCE_RE = re.compile(r"\b(\d{1,2})\s*(?:to\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b")
DEFAULT_MAX_YEARS = 1

# In descriptions, only count "N years" when "experience" follows within the
# same sentence -- avoids false hits like "10 years since we were founded".
DESC_EXPERIENCE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)\b(?=[^.\n]{0,60}?(?:experience|exp\b))",
    re.I,
)


def has_senior_experience(title, max_years):
    m = EXPERIENCE_RE.search(title)
    return bool(m) and int(m.group(1)) > max_years


def check_experience(desc, max_years):
    """
    (ok, note) based on the description text. Ranges like "3-5 years" count
    their lower bound; if several requirements appear, the smallest wins so
    "0-2 years" postings that also mention "5 years (nice to have)" survive.
    None/empty desc means we couldn't verify -- keep the job (fail open).
    """
    if not desc:
        return True, "experience not stated"
    mins = [int(m.group(1)) for m in DESC_EXPERIENCE_RE.finditer(desc)]
    if not mins:
        return True, "no experience requirement listed"
    lo = min(mins)
    if lo > max_years:
        return False, f"requires {lo}+ years"
    return True, f"asks {lo}+ years"


def stack_match(text, stack_keywords):
    """Which of the resume's stack keywords appear in title+description."""
    padded = f" {norm(text)} "
    return [k for k in stack_keywords if f" {norm(k)} " in padded]


def matches(job, filters):
    title = norm(job["title"])
    location = norm(job["location"])
    max_years = int(filters.get("max_experience_years", DEFAULT_MAX_YEARS))

    include = [norm(k) for k in filters.get("include_keywords", [])]
    exclude = [norm(k) for k in filters.get("exclude_keywords", [])]
    locations = [norm(k) for k in filters.get("locations", [])]

    if include and not any(k and f" {k} " in f" {title} " for k in include):
        return False
    if any(k and f" {k} " in f" {title} " for k in exclude):
        return False
    if has_senior_experience(title, max_years):
        return False
    if locations and location and not any(k in location for k in locations):
        return False
    # If the posting has no location string at all, keep it (better a false
    # positive than a missed opening).
    return True


def vet_new_job(job, filters):
    """
    Deep check for a NEW title-matched job: fetch the full description, then
    (a) reject if it demands more experience than max_experience_years, and
    (b) reject if it mentions none of the resume's stack keywords.
    Returns (ok, exp_note, matched_skills).
    """
    max_years = int(filters.get("max_experience_years", DEFAULT_MAX_YEARS))
    stack_kw = filters.get("stack_keywords", [])

    desc = fetch_description(job)
    ok, exp_note = check_experience(desc, max_years)
    skills = stack_match(f"{job['title']} {desc or ''}", stack_kw)
    if not ok:
        return False, exp_note, skills
    if stack_kw and desc and not skills:
        return False, "no stack keyword in description", skills
    return True, exp_note, skills


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def send_telegram(text):
    if TEST_MODE:
        print("[TEST] Would send Telegram message:\n" + text + "\n" + "-" * 60)
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(api, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=TIMEOUT)
        if not r.ok:
            print(f"Telegram API error {r.status_code}: {r.text}")
        return r.ok
    except requests.RequestException as e:
        print(f"Telegram send failed: {e}")
        return False


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_state(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=0))


def main():
    config = yaml.safe_load(CONFIG_FILE.read_text())
    default_filters = config.get("filters", {})
    companies = config.get("companies", [])
    seen = load_state()
    first_run = len(seen) == 0

    new_matches = []
    for company in companies:
        name = company.get("name", company.get("slug", "?"))
        ats = company.get("ats", "").lower()
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            print(f"[skip] {name}: unknown ats '{ats}'")
            continue
        try:
            jobs = fetcher(company)
        except Exception as e:
            print(f"[error] {name}: {e}")
            continue

        filters = {**default_filters, **company.get("filters", {})}
        hits = [j for j in jobs if matches(j, filters)]
        fresh = [j for j in hits if j["id"] not in seen]
        print(f"[ok] {name}: {len(jobs)} postings, {len(hits)} match, {len(fresh)} new")

        for j in hits:
            seen.add(j["id"])
        for j in fresh:
            new_matches.append((name, j, filters))
        time.sleep(1)  # be polite to APIs

    if first_run and not TEST_MODE:
        # First run just builds the baseline so you aren't spammed with
        # every existing posting. Alerts start from the next run.
        print(f"First run: baseline of {len(seen)} matching jobs saved. "
              f"Alerts begin next run.")
        save_state(seen)
        return

    # In --test mode with no saved state every posting counts as "new";
    # cap the deep description checks so a test run stays fast.
    if TEST_MODE and len(new_matches) > 20:
        print(f"[test] {len(new_matches)} matches; deep-checking first 20 only.")
        new_matches = new_matches[:20]

    alerted = 0
    for name, j, filters in new_matches:
        ok, exp_note, skills = vet_new_job(j, filters)
        if not ok:
            print(f"[skip] {name}: {j['title']} ({exp_note or 'no stack match'})")
            continue
        text = (f"🔔 NEW FRESHER OPENING\n\n"
                f"🏢 {name}\n"
                f"💼 {j['title']}\n"
                f"📍 {j['location'] or 'Location not listed'}\n"
                f"🕐 {exp_note}\n"
                f"🧰 Matches your stack: {', '.join(skills[:8]) or '—'}\n\n"
                f"Apply: {j['url']}")
        sent = send_telegram(text)
        alerted += 1
        print(f"[alert{'✓' if sent else '✗'}] {name}: {j['title']}")
        time.sleep(1)

    # A --test run must not mark jobs as seen, or the next real run would
    # silently skip alerting on them.
    if not TEST_MODE:
        save_state(seen)
    print(f"Done. {alerted} alert(s) sent, "
          f"{len(new_matches) - alerted} new job(s) filtered out by deep check.")


if __name__ == "__main__":
    main()
