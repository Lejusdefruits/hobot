# hobot

A job-hunting agent that runs in the background and is driven from Discord:
finds postings, scores them against your profile, writes its own cover
letters, optionally watches your inbox and drafts replies, and can dig up
company contacts. Runs on a local LLM (Ollama), no paid API required to work.

## What it does

- **Finds postings continuously** (JobSpy, scraping, no API key needed, plus
  French sources if you set them up: La Bonne Alternance, Adzuna, France
  Travail), scores each one against your profile, and writes a cover letter in
  PDF for the best matches automatically.
- **Checks that postings are still live**: one that's disappeared from the
  source site (position filled, listing pulled) gets flagged and dropped
  instead of sitting in the list looking just as valid as everything else.
- **Runs from Discord**: `/status`, `/offers`, `/offer <id>`, `/applied`,
  `/funnel` (where things stall in the application process), `/pause`,
  `/resume`, and `/ask "<anything>"` for everything else: looking up a
  specific posting, excluding a listing, checking the score breakdown,
  drafting or fixing an email, changing your profile on the fly. What
  `/ask` covers in more detail is further down.
- **No CV required to get started**: describe what you're looking for in one
  sentence in Discord ("I'm a Python developer looking for something in
  Lyon") and it builds a structured profile from that. Attaching a CV (PDF
  or `.docx`) via `/profile` works too, and it'll ask follow-up questions if
  anything looks thin.
- **Tailors your own CV per offer**, once one's uploaded: edits the summary
  paragraph (and, carefully, the visible skills) in place on your actual
  file, same layout, same fonts, nothing else touched, nothing invented.
- **Never sends an email without confirmation.** A Gmail draft, always. A real
  send only after clicking a "Confirm" button, never the agent on its own.

## Required vs. optional

Exactly one thing is required for this to run at all: **Discord + a local
Ollama model**. Everything else turns on by filling in its section of `.env`;
left empty, the matching feature just stays off, nothing breaks.

| Feature | Required? | What it needs |
|---|---|---|
| Discord bot + job discovery (JobSpy) | **Yes** | Discord token + local Ollama |
| French source, apprenticeship only | No | La Bonne Alternance — **France only**, free |
| French source, any contract type (filterable) | No | France Travail "Offres d'emploi v2" — **France only**, free |
| General-purpose, multi-country source | No | Adzuna — free up to 2500 calls/month |
| Spontaneous-application leads (France) | No | La Bonne Boite — **needs manual approval from France Travail**, not active on credentials alone |
| Mail monitoring (reading + draft replies) | No | One or more Gmail accounts |
| Web search for sharper letters/contacts | No | Self-hosted SearXNG (docker, free) |
| Company contacts (legal representatives) | No | Pappers — **France only** |
| Company contacts (verified emails) | No | Hunter.io + Snov.io — free up to 50-100/month |

Each section below matches one row of this table. Do the base install first,
then pick whichever optional sections you want, there's no need to do them
all the same day.

## Base install

### Requirements

Python 3.10 or newer (built and tested on 3.12), `git`, and something to run
an Ollama model on (a 7-8B model with tool-calling support runs fine on a
consumer GPU, or CPU-only if you're fine with slower replies). The systemd
instructions below assume Linux; on macOS or Windows, `python daemon.py` in a
terminal that stays open does the same thing, just without automatic restart
on boot.

### 1. Ollama

Install it from [ollama.com](https://ollama.com), then pull a model:

```bash
ollama pull qwen3.8
```

**qwen3.8 is the recommended default here**, not just a placeholder example:
it's a recent release and, in practice, one of the few local models in this
size class that reliably drives multi-step tool-calling without losing track
of what it's doing (repeating a tool call, forgetting to conclude, or
returning malformed arguments). That's the actual bottleneck for something
like this bot, which has to chain database lookups, letter drafting, and
mail tools correctly turn after turn. Other tool-calling models (qwen2.5,
llama3.1, mistral-nemo) will run, but expect more of the failure modes above
the smaller/older they get. Check Ollama is actually responding before
moving on:

```bash
curl http://localhost:11434/api/tags
```

### 2. Clone the repo and install dependencies

```bash
git clone <your-fork-or-this-repo> hobot
cd hobot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create the Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications),
   click **New Application**, give it a name.
2. In the **Bot** tab, click **Reset Token** (or **Add Bot** if it doesn't
   exist yet) to get the token, keep it, it's `DISCORD_BOT_TOKEN`. No
   privileged intent (message content, presence, members) is needed: the bot
   only uses slash commands.
3. In **OAuth2 → URL Generator**, check the `bot` and `applications.commands`
   scopes, then in permissions check at least **Send Messages**, **Embed
   Links**, **Attach Files**, and **Use Slash Commands**. Open the generated
   URL at the bottom of the page and invite the bot to your server (or a
   private server made just for this, the simplest option, a channel for
   yourself alone).
4. Turn on developer mode in Discord (Settings → Advanced → Developer Mode),
   then right-click the channel you want the bot to post in and pick **Copy
   Channel ID**, that's `DISCORD_CHANNEL_ID`.

### 4. Minimal configuration and first launch

```bash
cp .env.example .env
```

Open `.env` and fill in at least `OLLAMA_MODEL` (the exact name from step 1),
`DISCORD_BOT_TOKEN`, and `DISCORD_CHANNEL_ID`. Everything else can stay empty
for now. Run it:

```bash
python daemon.py
```

The bot should connect and show up online on your server within a few
seconds. `Ctrl+C` to stop it, the "running continuously" section below covers
leaving it running without a terminal open.

### 5. Set your profile

A few equivalent options, pick one:

- **With a CV**, straight from Discord: `/profile` and attach a PDF or `.docx`
  file. It reads it (PDF or Word, real text or a flattened/scanned page,
  either way), saves the profile, and follows up with 2-4 questions in the
  same conversation if something looks thin or missing (no target city
  detected, a vague target role, very few skills) -- answer normally with
  `/ask`. This CV is also what per-offer tailoring (see below) edits.
- **No CV**, straight from Discord: `/ask "I'm a Python developer
  looking for something in Lyon"`. The agent turns that into a structured
  profile (skills, target roles, target locations) and saves it. Adjust it
  any time the same way (`/ask "add Rust to my skills"`, `/ask "I'm
  also open to Bordeaux"`, ...).
- **With a CV, from the command line**: set `CV_PATH=/path/to/your_cv.pdf`
  in `.env`, then run `python -m core.profile` once -- same reading logic as
  `/profile`, no Discord round-trip. No need to do more than one of these,
  they all write to the same place.

From here, discovery runs on the schedule set by
`DISCOVERY_HOURS_WEEKDAY`/`DISCOVERY_HOURS_WEEKEND` (9am/11am/1pm/3pm/5pm on
weekdays and 10am/4pm on weekends by default), nothing needs to be triggered
by hand, though `/ask "find me developer postings in Lyon right now"`
runs an immediate search if you don't want to wait.

## French job sources (optional)

JobSpy alone already works with nothing else configured. These three sources
add to it, each independent of the others:

**La Bonne Alternance**: searches by ROME code (the French job-role taxonomy,
not free text), apprenticeship only. Free key on
[api.gouv.fr](https://api.gouv.fr) (search "La Bonne Alternance", request
access). Fill in `LBA_API_KEY` and, if the target role isn't IT, change
`LBA_ROME_CODES` (the full ROME reference is on
[francetravail.io](https://francetravail.io)).

**Adzuna**: general-purpose, multi-country, free up to 2500 calls/month.
Developer account on [developer.adzuna.com](https://developer.adzuna.com/),
which gives you an `app_id` and an `app_key`, put them in `ADZUNA_APP_ID` and
`ADZUNA_APP_KEY`.

**France Travail "Offres d'emploi v2"**: the French national job board, every
contract type by default (filterable, see below). Create an account on
[francetravail.io](https://francetravail.io), register an application in the
developer area, then subscribe to the "Offres d'emploi v2" product, that
subscription is immediate, no waiting. You get a client id and secret, put
them in `FRANCE_TRAVAIL_CLIENT_ID` and `FRANCE_TRAVAIL_CLIENT_SECRET`. To
filter on a specific contract type (apprenticeship, say), set
`FRANCE_TRAVAIL_NATURE_CONTRAT=E1,E2`; left empty, the search covers every
contract type.

While subscribing you'll also see "La Bonne Boite v2" (spontaneous-application
leads: companies flagged as likely hiring but with no published listing). The
code is ready and reuses the same client id/secret, but France Travail
requires a manual approval on their end on top of the subscription for this
particular API; until it's granted, this source just stays inactive without
blocking anything else (the bot handles that on its own, no error surfaces).

## CV tailoring (optional)

Needs a CV uploaded via `/profile` first (see step 5 above). Once you have
one, `adapter_cv` (through `/ask`, or automatically alongside the
auto-drafted cover letter for any offer scoring above
`DISCOVERY_LETTER_SCORE_THRESHOLD`) generates a tailored version of your
*own* CV for a specific offer -- same file, same layout, same fonts, same
colors. Only two things ever change: the profile/summary paragraph, and, if
your CV's skills section is a plain text list rather than an icon/pill
layout, which of your real skills fill the visible slots. Everything else
(name, contact info, dates, job titles, company names, degree names, images,
section order) stays untouched, and nothing is ever invented: a rewritten
paragraph or a surfaced skill only ever draws on what's already in your
profile.

What that promise actually depends on is your CV's own file:

- **A PDF or `.docx` with a real, selectable text layer** (most CVs from
  Word, Google Docs, LaTeX, and plenty of Canva exports) gets a true in-place
  edit -- the target text is located, removed, and the new text inserted with
  matched font, size, and color. `.docx` tailoring additionally needs
  [LibreOffice](https://www.libreoffice.org/) installed (`soffice` on your
  `PATH`) to convert the tailored file to a final PDF -- not a Python
  package, install it the normal way for your OS.
- **A fully flattened CV** (no selectable text at all -- some Canva "PDF for
  print" downloads export this way) can't be edited in place; there's no
  text object to find. This case gets a best-effort partial edit instead: the
  summary paragraph's region is located visually, covered, and replaced with
  real, ATS-readable text in a generic font, since the whole point of this
  feature is a CV that job boards can actually parse, not just one that
  looks right. It won't match your original font in that region, and only
  that one region becomes newly readable, not the rest of the page --
  flagged as a partial edit wherever it's mentioned, not silently blended in
  with the other case. If you'd rather have full fidelity, re-exporting your
  CV with text kept intact (or as a `.docx`) sidesteps this entirely.

## Mail monitoring (optional)

Reads incoming mail (automatic classification, recruiter replies detected)
and drafts replies, never a send without your explicit go-ahead.

1. On the Gmail account to monitor, turn on 2-step verification (required for
   the next part), then create an "app password" in Google's security
   settings, not your normal password, one generated specifically for this.
2. Fill in `GMAIL_ACCOUNT_1` (the address) and `GMAIL_APP_PASSWORD_1` (the
   generated password) in `.env`.
3. To watch more than one account, add `GMAIL_ACCOUNT_2`/
   `GMAIL_APP_PASSWORD_2`, `_3`, and so on, each one added gets watched for
   reading.
4. `GMAIL_SEND_ACCOUNT` picks which of those accounts is allowed to draft or
   send mail (only one, even if several are watched for reading), set it to
   one of the addresses already declared.

`EMAIL_POLL_INTERVAL_MIN` controls how often it checks during the day (20
minutes by default), `EMAIL_INTERVAL_OFFPEAK_MIN` at night.

## Web search and company contacts (optional)

Without any of this, cover letters still get written, just without company
enrichment (recent news, what the company actually does). Each piece below
is independent of the others.

**SearXNG** (web search, self-hosted, free): sharpens letters and contact
lookups by giving the agent some real context on the company before writing:

```bash
# once, any random string for SEARXNG_SECRET in .env
docker-compose up -d
```

That starts SearXNG on `localhost:8888` (`SEARXNG_HOST` in `.env`, already
set to that by default). Nothing else to do.

**Pappers** (French legal registry, company officers): France only. Free
token on [pappers.fr/api](https://www.pappers.fr/api), goes in
`PAPPERS_API_TOKEN`.

**Hunter.io** (verified emails by domain, any country): free plan, 50
searches/month. Key on [hunter.io/api](https://hunter.io/api), in
`HUNTER_API_KEY`.

**Snov.io** (automatic fallback when Hunter comes up empty, separate quota):
free plan, 50 credits/month, no card required. Credentials on
[snov.io/api](https://snov.io/api), in `SNOV_USER_ID` and `SNOV_API_SECRET`.

## Running it continuously

In development, `python daemon.py` in a terminal is enough. To run it in the
background without a session open (Linux, user-level systemd):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/hobot.service ~/.config/systemd/user/
# edit the copied file: replace both occurrences of /path/to/hobot with the
# real path where you cloned the repo
systemctl --user daemon-reload
systemctl --user enable --now hobot
loginctl enable-linger $USER   # so it keeps running without a session open
```

Managing it: `systemctl --user status|restart|stop hobot`,
`journalctl --user -u hobot -f` for live logs. The service is deliberately
constrained (2GB RAM cap, low CPU priority, restarts after a crash but gives
up after 5 failures in 10 minutes) so a problem in this process can't drag
down the rest of the machine.

## First steps in Discord

Once the bot is online and the profile is set (step 5 above):

- `/status` gives a summary of the last discovery run and mail check, and
  when the next one is due, useful to confirm everything's running without
  waiting for the first cycle.
- `/offers` lists the best postings found so far, with a button on each one
  to mark it applied directly.
- `/offer 12` (swap 12 for a real number) gives the full detail on one
  posting: description, score, reasoning, status, last time it was seen live.
- `/ask "..."` takes pretty much any request in plain language, see the
  list below for what that actually covers.

## Commands

Eight dedicated slash commands, plus `/ask`, which opens up a much wider
set of tools in plain language:

| Command | What it does |
|---|---|
| `/status` | Summary of the last discovery/mail run and when the next one is due |
| `/offers` | Best active postings, with an "already applied" button on each |
| `/offer <id>` | Full detail on one posting: description, score, status, last seen live |
| `/funnel` | Conversion funnel: found → scored → letter → sent → reply → interview |
| `/applied <id>` | Marks a posting applied (drops it from `/offers`) |
| `/profile <file>` | Sets your profile from an attached CV (PDF or `.docx`) |
| `/pause` / `/resume` | Stops or restarts scheduled checks (postings + mail) |
| `/ask "<text>"` | Everything else, see below |

`/ask` is backed by an agent that can, among other things: look up a
specific posting or run a live search for a city/keyword outside your usual
coverage, give the full detail on a posting, write or review a cover letter,
tailor your CV for a specific offer, look up a company's contacts (officers,
verified emails), exclude or re-include a posting, show or edit your
profile, give the score breakdown across all postings, list applications
already sent, list pending mail drafts, report how many sends are left for
the day, and create/edit/delete a draft reply. One plain-language sentence
is enough, no need to know a tool's exact name for the agent to pick the
right one.

## Architecture

```
daemon.py            — single process: scheduler (APScheduler) + Discord bot
graphs/
  discovery_graph.py  — fetch (JobSpy + French sources) -> dedup -> score (LLM) -> letters -> log
  email_graph.py      — fetch mail -> classify (LLM) -> draft replies
  chat_agent.py        — the agent behind /ask (ReAct, tool-calling)
core/
  db.py               — SQLite schema (offers, applications, user_profile...)
  llm.py              — shared Ollama client
  profile.py          — CV or free text -> structured profile
  circuit_breaker.py  — automatic backoff per failing source (anti-ban)
  locations.py         — French city registry -> coordinates/INSEE code (optional)
  france_travail_auth.py — shared OAuth2 for the France Travail Connect APIs (optional)
  api_usage.py        — monthly quota tracking (Adzuna)
tools/
  sources_jobspy.py   — discovery connector (no key required)
  sources_lba.py, sources_adzuna.py, sources_francetravail.py,
  sources_labonneboite.py — French sources (optional, see table above)
  sources_pappers.py, sources_hunter.py, sources_snov.py — contacts (optional)
  web_search.py       — SearXNG (optional)
  email_tools.py      — IMAP/SMTP (optional)
  link_check.py       — flags postings whose link has died
  documents.py        — renders cover letters to PDF
  cv_tailor.py        — edits your own uploaded CV in place, per offer (optional)
  discord_bot.py       — slash commands + bridge to the agent
```

Every posting goes through: discovery -> dedup (against the database and
across sources) -> LLM scoring (against your profile) -> above a threshold,
an automatic cover letter. Nothing ever gets sent automatically anywhere,
applications and emails stay drafts until an explicit human action.

## Search tip

JobSpy does real scraping, not an API call: broad, generic target roles
("backend developer" rather than a very specific title) tend to give better
results. `modifier_profil`/`definir_profil` (via `/ask`) let you adjust
that at any point.

## Design and safety

A few rules held everywhere in the code, not just stated here:

- **No automatic email send, ever.** The agent drafts one on its own
  initiative when a posting is worth it; only a human clicking a button in
  Discord triggers an actual SMTP send.
- **Automatic backoff per failing source** (`core/circuit_breaker.py`): a
  source that errors out gets backed off progressively (2h, doubling on each
  failure, capped at 24h) instead of retried without limit, to stay
  reasonable toward sites that don't appreciate repeated hits.
- **Deliberately conservative link checking** (`tools/link_check.py`): only
  an unambiguous 404/410 marks a posting dead; a timeout or a 403 (often an
  anti-bot block on a perfectly live listing) changes nothing rather than
  risk a false positive.
- **Nothing ever gets silently dropped.** A posting gets saved as soon as
  it's found, even before scoring; if scoring falls behind on a given day, it
  just waits for the next run instead of being forgotten.

## What's not here (yet)

This started as a generic fork of a more personal project, and a few pieces
of that one didn't make it over, either because they assume an already
well-worn setup or didn't fit a generic version. For reference, if you want
to take it further:

- Search keywords come straight from the profile, not adjusted automatically
  run to run based on results.
- No unified quota dashboard across every API (Adzuna has its own monthly
  guard, Hunter/Snov/Pappers don't here).
- No dedicated command to send already-generated files (CV/letter) back
  through Discord.

None of that stops the bot from working correctly for the main loop: finding
postings, sorting them, writing letters, handling mail.

## License

MIT, see [LICENSE](LICENSE). `assets/fonts/` bundles static instances of
Montserrat (SIL Open Font License, see `assets/fonts/Montserrat-OFL.txt`),
used as a font-fidelity fallback by the CV tailoring feature.
