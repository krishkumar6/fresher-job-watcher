# 🔔 Fresher Job Watcher

Get an **instant Telegram notification** whenever a company you're tracking
posts a new fresher-friendly opening on its **own career portal** — hours or
days before it shows up on aggregator sites.

It calls the public APIs of the applicant-tracking systems that power company
career pages (Greenhouse, Lever, Ashby, SmartRecruiters, Workday), so it reads
jobs straight from the source. It runs **free on GitHub Actions** every 2
hours — no server, no laptop left on, no signup.

It ships with **129 Indian and India-hiring tech companies** already
configured, and filters every posting three ways so you're only pinged for jobs
you'd actually apply to:

| Check | What it does |
|---|---|
| **Title** | must match your `include_keywords`, must not match `exclude_keywords` |
| **Experience** | reads the full job description and drops anything asking for more years than you have |
| **Stack** | drops postings that don't mention a single technology from your resume |

```
🔔 NEW FRESHER OPENING

🏢 Razorpay
💼 Software Engineer - Backend
📍 Bengaluru
🕐 asks 0+ years
🧰 Matches your stack: java, spring boot, mysql, rest

Apply: https://...
```

---

## Setup (one time, ~15 minutes)

### Step 1 — Make your own copy

Click **[Use this template] → Create a new repository** at the top of this page.

> ⚠️ **Use the template button, not Fork.** GitHub disables scheduled
> workflows on forked repositories, so a fork will never run on its own.

**Name it whatever you like, but think about public vs private** — it decides
whether this stays free.

One run polls 129 companies with a one-second pause between each. Measured
across real runs on GitHub's runners: **4-5 minutes** (4m11s to 5m24s). GitHub
rounds every run up to the whole minute, so a 2-hour schedule costs roughly
**1,800 minutes a month**.

| Repo | Actions minutes | What that means |
|---|---|---|
| **Public** | unlimited, free | Every 2 hours forever, no quota. Your `config.yaml` and job history are visible — neither contains secrets, but they do show what you're applying for. |
| **Private** | 2,000/month free | A 2-hour schedule uses ~1,800 of them. It fits, but with ~10% headroom — one slow month and your alerts stop without warning. |

If you want it **private**, change the schedule in
`.github/workflows/watch.yml` to every 4 hours. That's ~900 minutes, less than
half the allowance:

```yaml
    - cron: "0 */4 * * *"
```

You'll still hear about openings hours before they reach the aggregator sites.

### Step 2 — Create your Telegram bot (3 min)

1. Open Telegram, search for **@BotFather**, send `/newbot`.
2. Give it a name and a username.
3. BotFather replies with a **bot token** like `7123456789:AAF...` — save it.
4. **Open a chat with your new bot and send it any message** (e.g. "hi").
   Telegram bots cannot message you until you message them first.
5. Get your **chat ID**: open this in a browser —
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   — and copy the number at `"chat":{"id": ... }`.

   > **Delete the `<` and `>` too.** `<YOUR_TOKEN>` means replace the whole
   > thing, brackets included, so the URL reads
   > `https://api.telegram.org/bot7123456789:AAF.../getUpdates`. Leaving the
   > brackets in gives you `{"ok":false,"error_code":404}`.
   >
   > Getting `{"ok":true,"result":[]}` instead? You haven't messaged the bot
   > yet, or you did it over 24 hours ago — Telegram drops pending updates
   > after a day. Send it another message and reload.

### Step 3 — Add your credentials as repository secrets

In **your** repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add two:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the chat id from step 2 |

Secrets are encrypted and are never visible in logs, even on a public repo.

### Step 4 — Write your filters (required — nothing runs without this)

**There is no `config.yaml` in this template on purpose**, so you can't
accidentally end up running someone else's resume filters. You create it:

```bash
git clone https://github.com/<you>/<your-repo>.git
cd <your-repo>
cp config.example.yaml config.yaml
```

Now open `config.yaml` and make it yours: `stack_keywords` = the technologies on
*your* resume, `locations` = the cities you'd actually move to,
`max_experience_years` = how much experience you have. Every option is
documented inline in the file.

Then commit it — the GitHub Actions runner can only see what's in the repo:

```bash
git add config.yaml
git commit -m "My filters"
git push
```

> The example is filled in for a Java / Spring Boot / React fresher in India.
> That's a starting shape, not a default to keep. If it isn't you, change it —
> otherwise you'll get alerts for jobs you can't apply to and miss the ones you
> can.

### Step 5 — Test locally (5 min, strongly recommended)

```bash
pip install -r requirements.txt
python job_watcher.py --test
```

This fetches everything and prints what *would* have been sent, without
alerting anyone or saving any state. Read the output:

- `[ok] Acme: 120 postings, 3 match, 3 new` — working.
- `[error] Acme: ...` — that company's slug is stale. Fix it (see
  [CONTRIBUTING.md](CONTRIBUTING.md)) or delete the entry. One bad entry is
  skipped and doesn't affect the rest.
- **Zero matches everywhere?** Your filters are too tight — usually
  `stack_keywords` or `locations`.
- **Hundreds of matches?** Too loose — tighten `include_keywords`.

### Step 6 — Turn it on

Go to the **Actions** tab → enable workflows → open **Job Watcher** → **Run
workflow**.

Check the "Just send a test Telegram message" box on the first run to confirm
your credentials work. Then run it again unchecked.

- The **first real run only builds a baseline** — it memorises every current
  posting so you aren't spammed with hundreds of old jobs. This is normal;
  you'll get no alerts from it.
- **From the second run onward**, every new matching posting pings you.

It then repeats automatically every 2 hours, forever, for free.

---

## Tuning it to you

Everything personal is in **`config.yaml`**:

| Option | What it does |
|---|---|
| `max_experience_years` | The most years a posting may **demand**. Read it as "years I have": `0` drops "1+ years" postings, `1` allows them, `99` doesn't filter |
| `stack_keywords` | Title+description must mention one of these. **Empty list = no stack filter** |
| `include_keywords` | Title must contain one of these |
| `exclude_keywords` | Title must contain none of these (this is what kills senior roles) |
| `locations` | Location must contain one of these. Postings with no location listed are always kept. **Empty list = anywhere** |

**`companies.yaml`** is the shared company list. Adding companies takes about a
minute each and the more you add, the wider your net — instructions are at the
top of the file. Because your filters live in a separate file, you can pull
company additions from upstream without ever touching your own setup.

Any single company can override the defaults with its own `filters:` block —
useful for watching *everything* a dream company posts:

```yaml
  - name: Dream Company
    ats: greenhouse
    slug: dreamco
    filters:
      include_keywords: []      # every title
      max_experience_years: 3
```

---

## Troubleshooting

**No Telegram message ever arrives.**
Run the workflow with the ping box checked. If it fails, the usual cause is
that you never sent your bot a message (step 2.4) — a bot cannot start a
conversation with you.

**The workflow fails with "config.yaml is missing".**
You skipped step 4, or you created `config.yaml` locally but never committed
and pushed it. The runner only sees files that are in the repo.

**The workflow fails on "Commit updated state".**
Your repo needs Actions write permission: **Settings → Actions → General →
Workflow permissions → Read and write permissions**.

**Alerts stopped after a couple of months.**
GitHub disables scheduled workflows in repos with no activity for 60 days.
Normally the watcher's own state commits keep it alive; if it does get
disabled, GitHub emails you and one click re-enables it.

**I'm getting alerts for jobs asking "1+ years" and I have none.**
Set `max_experience_years: 0`. At `1` you are telling the watcher you have a
year, so it correctly lets "1+ years" postings through.

**I'm still getting alerts for jobs that clearly want more experience.**
The experience check reads the description and fails *open* — if it can't
fetch or parse the requirement, it keeps the job rather than silently dropping
it. Requirements written as "5 years with X is a plus" are ignored on purpose,
since they're not hard requirements. Tightening `exclude_keywords` catches most
of the rest.

**A company shows `[error]`.** Its slug changed. See
[CONTRIBUTING.md](CONTRIBUTING.md) — and please send a PR with the fix.

**How would I even notice a company going stale?** You don't have to. A second
workflow, **Company Health Check**, runs every Monday and opens a GitHub issue
listing any company whose board stopped answering. That matters because a stale
slug fails *silently* — that company just stops producing alerts. You can also
run it yourself any time:

```bash
python job_watcher.py --check-companies
```

It needs no `config.yaml` and no Telegram credentials, so it works in a fresh
clone.

---

## Limitations (honest ones)

- Covers companies on **Greenhouse / Lever / Ashby / SmartRecruiters /
  Workday** — that's most startups and many MNCs, but **not** TCS / Infosys /
  Wipro-style custom portals. For those, register directly on their portals
  (TCS NQT, Infosys careers) and join their official alert emails — mass drives
  are announced, not silently posted.
- GitHub cron isn't to-the-minute and can be delayed under load; expect alerts
  within ~2 hours of posting. That's still far ahead of aggregator sites.
- The filters are keyword matching, not comprehension. They will occasionally
  let through a role you don't want. That failure mode is deliberate — a false
  alert costs you five seconds, a missed opening costs you the job.

---

## Contributing

Adding or fixing companies helps everyone using this — see
[CONTRIBUTING.md](CONTRIBUTING.md). Verified slugs, stale-entry fixes, and new
ATS support are all welcome.

## License

[MIT](LICENSE) — do whatever you want with it. Good luck with the job hunt.
