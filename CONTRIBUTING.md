# Contributing

The most useful contribution is **adding companies**. Everyone running this
watcher polls the same `companies.yaml`, so one entry you add gets picked up by
every user who pulls upstream.

## Adding a company (~1 minute)

1. Open the company's careers page and click any job posting.
2. Read the job board URL and map it to an `ats`:

   | URL you see | `ats` | what to put |
   |---|---|---|
   | `boards.greenhouse.io/SLUG` or `job-boards.greenhouse.io/SLUG` | `greenhouse` | `slug: SLUG` |
   | `jobs.lever.co/SLUG` | `lever` | `slug: SLUG` |
   | `jobs.ashbyhq.com/SLUG` | `ashby` | `slug: SLUG` |
   | `jobs.smartrecruiters.com/SLUG` | `smartrecruiters` | `slug: SLUG` |
   | `COMPANY.wdX.myworkdayjobs.com/en-US/SITE` | `workday` | `url:` the full URL |

3. Add the entry under the right section of `companies.yaml`:

   ```yaml
     - name: Acme
       ats: greenhouse
       slug: acme
   ```

4. **Verify it before opening the PR:**

   ```bash
   python job_watcher.py --test
   ```

   Find your company in the output. It must say `[ok] Acme: N postings, ...`
   with `N` greater than zero. If it says `[error]`, the slug is wrong — go back
   to the careers page and check the URL again.

5. Open a PR. Please paste the `[ok]` line from your `--test` output in the
   description so it can be merged without re-checking.

Slugs are case-sensitive on some systems (SmartRecruiters especially — it's
`Whatfix`, not `whatfix`).

## Removing or fixing a company

Companies switch ATS providers, and slugs go stale. If `--test` reports an
`[error]` for an entry that's already in the list, a PR that fixes the slug (or
removes the entry if the company moved to a custom portal) is very welcome.

If a company works but posts almost nothing, keep it and add a comment:

```yaml
  - name: Acme
    ats: greenhouse
    slug: acme
    # NOTE: only ~2 live postings — low volume, spot-check periodically.
```

## Adding support for a new ATS

`job_watcher.py` has one small `fetch_*` function per ATS, registered in the
`FETCHERS` dict. A new one needs to return a list of dicts with `id`, `title`,
`location`, `url`, plus either a `description` string (if the list API already
includes it) or a `_desc` reference so the description is only fetched for jobs
that actually match. Follow `fetch_lever` (description included) or
`fetch_greenhouse` (description fetched lazily) as the two models.

Please only add ATS platforms with a public, unauthenticated JSON API — no
scraping, no credentials, and keep the polite `time.sleep(1)` between companies.

## Changing filter behaviour

`config.example.yaml` is the documented default that new users copy. If you
change how a filter works in `job_watcher.py`, update the comments there too —
that file is the de-facto documentation for the filtering logic.

Don't commit your own `config.yaml` or `seen_jobs.json` changes in a PR.
