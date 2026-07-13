# 🔔 Fresher Job Watcher

Get an **instant Telegram notification** whenever a company you're tracking
posts a new fresher-friendly opening on its **own career portal** — hours or
days before it shows up on aggregator sites.

It works by calling the public APIs of the applicant-tracking systems that
power company career pages (Greenhouse, Lever, Ashby, SmartRecruiters,
Workday), so it reads jobs straight from the source. It runs free on GitHub
Actions every 2 hours — no server, no laptop left on.

---

## Setup (one time, ~15 minutes)

### Step 1 — Create your Telegram bot (3 min)
1. Open Telegram, search for **@BotFather**, send `/newbot`.
2. Give it a name (e.g. `Krish Job Alerts`) and a username.
3. BotFather replies with a **bot token** like `7123456789:AAF...` — save it.
4. Open a chat with **your new bot** and send it any message (e.g. "hi").
5. Get your **chat ID**: open in a browser
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   and copy the number at `"chat":{"id": ... }`.

### Step 2 — Put this project on GitHub (5 min)
1. Create a new **private** repository (e.g. `fresher-job-watcher`).
2. Upload all files from this folder, keeping the structure
   (`.github/workflows/watch.yml` must be in that exact path).
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**. Add two secrets:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat id

### Step 3 — Verify the company list (5 min)
Some slugs in `companies.yaml` may need fixing (companies change systems).
Locally, run:

```bash
pip install -r requirements.txt
python job_watcher.py --test
```

Any line printed as `[error] CompanyName: ...` → open that company's careers
page, click a job, look at the URL, and fix the `ats`/`slug`/`url` using the
instructions at the top of `companies.yaml`. Delete entries you can't fix.

### Step 4 — Turn it on
Go to the repo's **Actions** tab → enable workflows → open **Job Watcher** →
**Run workflow** once manually.

- The **first run only builds a baseline** (it memorizes all current postings
  so you don't get spammed with old jobs).
- From the next run onward, every **new** matching posting triggers a
  Telegram message like:

```
🔔 NEW FRESHER OPENING

🏢 Razorpay
💼 Software Engineer - Backend
📍 Bengaluru

Apply: https://...
```

It then repeats automatically every 2 hours, forever, for free.

---

## Tuning it to you

Everything is in `companies.yaml`:

- **`include_keywords`** — job title must contain one of these. Already tuned
  for Java/full-stack fresher titles (software engineer, java, backend, sde,
  graduate, trainee, associate engineer, intern...).
- **`exclude_keywords`** — filters out senior/lead/manager/SDE2+ roles.
- **`locations`** — currently Bengaluru/Bangalore/India/remote.
- **Adding companies** — 1 minute each; instructions are at the top of the
  file. The more you add, the wider your net.

You can also give any single company its own `filters:` block to override the
defaults (e.g., watch *everything* a dream company posts).

## Limitations (honest ones)

- Covers companies on Greenhouse / Lever / Ashby / SmartRecruiters / Workday —
  that's most startups and many MNCs, but **not** TCS/Infosys/Wipro-style
  custom portals. For those, register directly on their portals (TCS NQT,
  Infosys careers) and join their official alert emails — mass drives are
  announced, not silently posted.
- GitHub cron isn't to-the-minute; expect alerts within ~2 hours of posting.
  That's still far ahead of aggregator sites.
- Run `--test` after adding companies; a wrong slug just logs an error and is
  skipped, it won't break anything else.
