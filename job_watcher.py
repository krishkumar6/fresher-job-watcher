#!/usr/bin/env python3
"""
Fresher Job Watcher
-------------------
Polls company career portals (Greenhouse / Lever / Ashby / SmartRecruiters /
Workday) directly, filters postings by your keywords + location, and sends a
Telegram notification for every NEW posting it hasn't seen before.

Designed to run on a schedule (GitHub Actions cron) so you get alerted within
~1-2 hours of a job going live on the company's own portal.

Config:
    config.yaml     your filters (copy config.example.yaml to create it)
    companies.yaml  the shared list of companies to poll

Usage:
    python job_watcher.py            # normal run (needs TELEGRAM_* env vars)
    python job_watcher.py --test     # validate config, print matches, no alerts
    python job_watcher.py --ping     # send one test message, check credentials
    python job_watcher.py --check-companies
                                     # is every company's board still alive?
                                     # needs no config.yaml, no credentials
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

HERE = Path(__file__).parent
COMPANIES_FILE = HERE / "companies.yaml"   # shared list, maintained upstream
CONFIG_FILE = HERE / "config.yaml"         # your filters, copied from the example
EXAMPLE_CONFIG = HERE / "config.example.yaml"
STATE_FILE = HERE / "seen_jobs.json"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (job-watcher; personal use)"}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TEST_MODE = "--test" in sys.argv
PING_MODE = "--ping" in sys.argv
CHECK_MODE = "--check-companies" in sys.argv
# --forget=mastercard re-arms already-seen jobs so they alert again. Exists so
# nobody has to hand-edit seen_jobs.json to test that alerts work.
FORGET = next((a.split("=", 1)[1] for a in sys.argv
               if a.startswith("--forget=") and len(a) > 9), None)


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
    jobs, seen_paths, offset = [], set(), 0
    while offset <= 100:  # up to ~2 pages is plenty for new postings
        payload = {"limit": 20, "offset": offset, "searchText": search_text,
                   "appliedFacets": {}}
        r = requests.post(api, json=payload, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        # Some Workday tenants ignore `offset` and hand back page 1 every time.
        # Stop as soon as a page adds nothing new instead of collecting the
        # same postings six times over.
        paths = {j.get("externalPath", "") for j in postings}
        if paths <= seen_paths:
            break
        seen_paths |= paths
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

# In descriptions, count "N years" only where it reads as a requirement.
# Requiring the literal word "experience" nearby was too narrow: postings
# routinely write "5+ years building production systems" with no "experience"
# anywhere near it, and those were being read as having no requirement at all.
# So also accept a role verb ("years building/developing/working..."), a
# qualified noun ("years of professional/hands-on..."), or "years as a ...",
# while still rejecting "10 years since we were founded".
DESC_EXPERIENCE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)\b"
    r"(?!\s+(?:since|ago|in\s+business|of\s+operation))"
    r"(?="
    r"[^.\n]{0,60}?(?:experience|exp\b)"
    r"|\s+(?:build|develop|design|writ|cod|program|engineer|work|ship|deliver)\w*"
    r"|\s+(?:of|in|with)\s+(?:professional|hands|relevant|industry|software|"
    r"engineering|backend|frontend|full|web|product|technical)\b"
    r"|\s+as\s+an?\b"
    r")",
    re.I,
)

# "1 year of Kafka is a plus" is not a requirement. Counting it let postings
# demanding 5+ years through, because the smallest number in the description
# won. Only clearly-optional phrasings are listed -- "preferred" is left out on
# purpose, since it's very often how a real hard requirement is worded.
OPTIONAL_RE = re.compile(
    r"\b(?:a plus|nice to have|good to have|bonus|desirable)\b", re.I)

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def has_senior_experience(title, max_years):
    m = EXPERIENCE_RE.search(title)
    return bool(m) and int(m.group(1)) > max_years


def check_experience(desc, max_years):
    """
    (ok, note) based on the description text. Ranges like "3-5 years" count
    their lower bound. Sentences phrased as optional ("...is a plus") are
    ignored, so a "0-2 years" posting that also mentions "5 years with Kafka is
    a plus" survives while a genuine "5+ years" posting does not. Of the
    remaining hard requirements the smallest wins.
    None/empty desc means we couldn't verify -- keep the job (fail open).
    """
    if not desc:
        return True, "experience not stated"
    mins = []
    for sentence in SENTENCE_RE.split(desc):
        if OPTIONAL_RE.search(sentence):
            continue
        mins += [int(m.group(1)) for m in DESC_EXPERIENCE_RE.finditer(sentence)]
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

def load_filters():
    """
    Your personal filters from config.yaml. Older single-file setups kept a
    `filters:` block inside companies.yaml -- still honoured so an existing
    checkout doesn't break, but config.yaml wins when both exist.
    """
    if CONFIG_FILE.exists():
        config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        filters = config.get("filters")
        if filters:
            return filters
        sys.exit(f"{CONFIG_FILE.name} has no `filters:` block. "
                 f"Compare it against {EXAMPLE_CONFIG.name}.")

    legacy = (yaml.safe_load(COMPANIES_FILE.read_text(encoding="utf-8")) or {}).get("filters")
    if legacy:
        print(f"[warn] Using the `filters:` block in {COMPANIES_FILE.name}. "
              f"Move it to {CONFIG_FILE.name} so you can pull upstream company "
              f"updates without conflicts.")
        return legacy

    sys.exit(f"No {CONFIG_FILE.name} found. Create one with:\n"
             f"    cp {EXAMPLE_CONFIG.name} {CONFIG_FILE.name}\n"
             f"then edit it to match your stack, locations and experience level.")


def load_companies():
    companies = (yaml.safe_load(COMPANIES_FILE.read_text(encoding="utf-8")) or {}).get("companies", [])
    if not companies:
        sys.exit(f"No companies listed in {COMPANIES_FILE.name}.")
    return companies


def check_companies():
    """
    Does every company's job board still answer? Companies switch ATS provider
    without notice, which turns a slug stale -- and a stale slug fails quietly,
    so that company simply stops producing alerts and you never find out.

    Needs no config.yaml and no Telegram credentials, so it can run anywhere.

    Exit codes:
        0  everything reachable
        1  specific companies are broken -- worth opening an issue
        2  so many failed that the network is the likely cause, not the slugs
    """
    companies = load_companies()
    broken, empty = [], []
    for company in companies:
        name = company.get("name", company.get("slug", "?"))
        fetcher = FETCHERS.get(company.get("ats", "").lower())
        if not fetcher:
            print(f"[error] {name}: unknown ats '{company.get('ats')}'")
            broken.append(name)
            continue
        # One retry: a momentary blip shouldn't get a company reported as
        # permanently stale, which is what sends people chasing a phantom.
        for attempt in (1, 2):
            try:
                jobs = fetcher(company)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[error] {name}: {e}")
                    broken.append(name)
                    jobs = None
                else:
                    time.sleep(3)
        if jobs is None:
            continue
        if jobs:
            print(f"[ok] {name}: {len(jobs)} postings")
        else:
            # Not an error -- a company can legitimately have nothing open.
            print(f"[warn] {name}: reachable but listing 0 postings")
            empty.append(name)
        time.sleep(1)

    print(f"\n{len(companies) - len(broken)}/{len(companies)} companies reachable, "
          f"{len(broken)} broken, {len(empty)} listing nothing.")
    if not broken:
        sys.exit(0)
    print("Broken: " + ", ".join(broken))

    # Slugs go stale one at a time. Dozens failing at once means DNS died or
    # the network dropped -- reporting that as "fix your slugs" is pure noise,
    # and noise is how a useful alert gets ignored.
    if len(broken) > max(5, len(companies) // 4):
        print(f"\n{len(broken)} of {len(companies)} failed at once. That is almost "
              f"certainly a network or DNS problem on this machine, not stale "
              f"slugs. Not reporting these as broken.")
        sys.exit(2)
    sys.exit(1)


def load_state():
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except json.JSONDecodeError as e:
        # Never quietly treat a damaged state file as "no history". Doing that
        # makes this look like a first run, which rebuilds the baseline, marks
        # every currently-live posting as seen and alerts on none of them --
        # a silent, unrecoverable loss that still exits green.
        sys.exit(
            f"{STATE_FILE.name} is not valid JSON: {e}\n"
            f"It was most likely hand-edited. Fix the syntax and re-run.\n"
            f"To re-alert specific jobs, don't edit this file by hand -- use:\n"
            f"    python job_watcher.py --forget=<text>\n"
            f"Deleting {STATE_FILE.name} outright also works, but it starts a\n"
            f"fresh baseline: every posting live right now is marked seen and\n"
            f"you will never be alerted about any of them."
        )


def save_state(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=0))


def main():
    if PING_MODE:
        ok = send_telegram("✅ Test message from your job watcher — "
                           "Telegram credentials work!")
        print("Ping sent — check your Telegram." if ok else
              "Ping FAILED — see the error above. Check that the "
              "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets are set and that "
              "you have sent your bot a message first (it can't message you "
              "until you do).")
        sys.exit(0 if ok else 1)

    if CHECK_MODE:
        check_companies()

    default_filters = load_filters()
    companies = load_companies()
    seen = load_state()
    first_run = len(seen) == 0

    if FORGET and not first_run:
        drop = {x for x in seen if FORGET.lower() in x.lower()}
        if not drop:
            sys.exit(f"No seen job ids contain {FORGET!r}. Nothing to re-arm.")
        print(f"[forget] re-arming {len(drop)} job(s) matching {FORGET!r} — "
              f"they will be treated as new and alerted on this run:")
        for d in sorted(drop):
            print(f"   {d}")
        seen -= drop

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

        # Belt-and-braces: a list API that returns the same posting twice would
        # otherwise produce one Telegram alert per copy, since `fresh` below is
        # computed before anything is added to `seen`.
        deduped = {}
        for j in jobs:
            deduped.setdefault(j["id"], j)
        jobs = list(deduped.values())

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
    failed = 0
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
        if sent:
            alerted += 1
        else:
            failed += 1
            # Un-mark it so the next run retries instead of losing the alert.
            seen.discard(j["id"])
        print(f"[alert{'✓' if sent else '✗'}] {name}: {j['title']}")
        time.sleep(1)

    # A --test run must not mark jobs as seen, or the next real run would
    # silently skip alerting on them.
    if not TEST_MODE:
        save_state(seen)
    print(f"Done. {alerted} alert(s) sent, {failed} failed, "
          f"{len(new_matches) - alerted - failed} filtered out by deep check.")
    if failed and not TEST_MODE:
        sys.exit(f"{failed} Telegram send(s) FAILED -- check the "
                 f"TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID repo secrets. "
                 f"Failed jobs stay unseen and will retry next run.")


if __name__ == "__main__":
    main()
