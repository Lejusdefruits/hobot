# hobot

A job-hunting agent that runs in the background and is driven from Discord
and/or a terminal UI: finds postings, scores them against your profile,
writes its own cover letters, optionally watches your inbox and drafts
replies, and can dig up company contacts. Runs on a local LLM (Ollama) by
default, no paid API required to work -- a cloud key (OpenAI/Anthropic) is
a drop-in alternative if you'd rather not run a model locally.

**Contents:** [Quick start](#quick-start) &middot;
[Platforms](#platforms) &middot;
[What it does](#what-it-does) &middot;
[Required vs. optional](#required-vs-optional) &middot;
[Base install](#base-install) &middot;
[Job sources](#job-sources-and-search-coverage) &middot;
[French job sources](#french-job-sources-optional) &middot;
[ATS watchlist](#ats-watchlist-optional) &middot;
[Funding-news leads](#funding-news-leads-optional) &middot;
[CV tailoring](#cv-tailoring-optional) &middot;
[Quality checks](#quality-checks) &middot;
[Mail monitoring](#mail-monitoring-optional) &middot;
[Web search & contacts](#web-search-and-company-contacts-optional) &middot;
[Terminal UI](#terminal-ui) &middot;
[Accessibility](#accessibility) &middot;
[Running it continuously](#running-it-continuously) &middot;
[Commands](#commands) &middot;
[Architecture](#architecture) &middot;
[Design and safety](#design-and-safety)

## Quick start

```bash
git clone <your-fork-or-this-repo> hobot && cd hobot
./setup.sh          # Linux/macOS. On Windows (PowerShell): .\setup.ps1
```

Open `.env`, fill in the `REQUIRED` block at the top (an LLM -- Ollama by
default, nothing to pay for; see [Base install](#base-install) for a cloud
key instead), then:

```bash
.venv/bin/python daemon.py    # discovery + Discord (if configured)
.venv/bin/python cli.py       # terminal UI, in a second terminal
# Windows: .venv\Scripts\python.exe instead of .venv/bin/python
```

That's the whole loop already running, headless if you skip Discord.
Everything below this point -- Discord, French job sources, mail
monitoring, CV tailoring, company contacts -- is optional, each one turned
on by filling in its own section of `.env`; see
[Required vs. optional](#required-vs-optional) for what each needs.

## Platforms

One codebase -- no separate Windows/macOS/Linux build to pick between.
Everything here is plain Python plus libraries that ship native builds for
all three (Ollama, Textual, WeasyPrint, LibreOffice). Desktop notifications
and the terminal UI's "open file" buttons already detect the OS and use the
right mechanism underneath (`notify-send`/`osascript`/a PowerShell balloon
tip; `xdg-open`/`open`/`os.startfile`) -- nothing to configure there.

What actually differs per OS:

| | Linux | macOS | Windows |
|---|---|---|---|
| Base install | `./setup.sh` | `./setup.sh` | `.\setup.ps1` |
| Terminal for `cli.py` | any | Terminal.app, iTerm2 | Windows Terminal (recommended over legacy `cmd.exe`) |
| Run continuously in the background | systemd, see [below](#running-it-continuously) | launchd, see [below](#running-it-continuously) | Task Scheduler, see [below](#running-it-continuously) |
| WeasyPrint's system libraries (Pango/Cairo) | usually already present, else one `apt`/`dnf` line | `brew install pango` | a one-time GTK3 runtime installer -- see [WeasyPrint's own docs](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows) |

`.docx` CV tailoring additionally needs
[LibreOffice](https://www.libreoffice.org/) (`soffice` on `PATH`) on any of
the three -- same install either way, just from your OS's normal package
manager or installer.

## What it does

- **Finds postings continuously** (JobSpy, scraping, no API key needed, plus
  French sources if you set them up: La Bonne Alternance, Adzuna, France
  Travail), scores each one against your profile, and writes a cover letter in
  PDF for the best matches automatically.
- **Checks that postings are still live**: one that's disappeared from the
  source site (position filled, listing pulled) gets flagged and dropped
  instead of sitting in the list looking just as valid as everything else.
- **Flags likely ghost jobs and unreadable generated PDFs** -- see
  [Quality checks](#quality-checks) below.
- **Runs from Discord and/or a terminal UI**: `/status`, `/offers`,
  `/offer <id>`, `/applied`, `/funnel` (where things stall in the application
  process), `/pause`, `/resume`, and `/ask "<anything>"` for everything else:
  looking up a specific posting, excluding a listing, checking the score
  breakdown, drafting or fixing an email, changing your profile on the fly.
  What `/ask` covers in more detail is further down. The terminal UI (`python
  cli.py`, below) covers the same ground with a full-screen dashboard --
  browsable postings/applications/drafts and a chat pane -- instead of slash
  commands. Pick one interface or run both side by side; they read and write
  the same database, so nothing needs syncing between them.
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

Exactly one thing is required for this to run at all: **an LLM**, local
(Ollama, the default, no paid API) or a cloud key (OpenAI/Anthropic/any
OpenAI-compatible endpoint) if you'd rather not run a model yourself. Job
discovery (JobSpy) works out of the box either way. Discord is an optional
interface -- turn it on or not (a fully headless install still runs discovery
and scoring on schedule; the terminal UI, `python cli.py`, always works
regardless, since it's a separate process against the same database rather
than something the daemon has to be running for). Everything else turns on
by filling in its section of `.env`; left empty, the matching feature just
stays off, nothing breaks.

| Feature | Required? | What it needs |
|---|---|---|
| LLM (scoring, letters, `/ask`) | **Yes** | Local Ollama (default, free) or a cloud key (`LLM_PROVIDER`) |
| Job discovery (JobSpy) | **Yes** | Nothing extra -- scraping, no API key |
| Discord interface | No | A Discord bot token |
| Terminal UI | No | Nothing extra -- `python cli.py` |
| French source, apprenticeship only | No | La Bonne Alternance — **France only**, free |
| French source, any contract type (filterable) | No | France Travail "Offres d'emploi v2" — **France only**, free |
| General-purpose, multi-country source | No | Adzuna — free up to 2500 calls/month |
| Spontaneous-application leads (France) | No | La Bonne Boite — **needs manual approval from France Travail**, not active on credentials alone |
| Tech-company career pages (any country) | No | Greenhouse/Ashby/Lever — free, but needs `ATS_WATCHLIST` (or chat) to name companies, no keyword search |
| Funding-news leads for the ATS watchlist | No | Nothing extra -- free RSS, proposes companies, never adds one on its own |
| Mail monitoring (reading + draft replies) | No | One or more Gmail accounts |
| Web search for sharper letters/contacts | No | Self-hosted SearXNG (docker, free) |
| Company contacts (legal representatives) | No | Pappers — **France only** |
| Company contacts (verified emails) | No | Hunter.io + Snov.io — free up to 50-100/month |

Each section below matches one row of this table. Do the base install first,
then pick whichever optional sections you want, there's no need to do them
all the same day.

## Base install

The [Quick start](#quick-start) above (`setup.sh`/`setup.ps1`) already does
steps 1-2 below -- venv, dependencies, `.env` copied from the example. This
section is the same install broken into its individual pieces, for when you
want to see (or change) what each one actually does.

### Requirements

Python 3.10 or newer (built and tested on 3.12), `git`, and either something
to run an Ollama model on or a cloud LLM key (see "Cloud LLM" below) if you'd
rather skip running a model yourself. qwen3.8, the recommended default below,
is a 27B model (an 18GB download at its default Q4 quantization) -- a GPU
with at least 18-20GB VRAM makes for a solid experience, and it's usable but
slow CPU-only if you have the RAM and patience for that; a lighter
tool-calling model (see the note below) is the better fit for more modest
hardware. See [Platforms](#platforms) for what else differs by OS.

### 1. Ollama

Install it from [ollama.com](https://ollama.com), then pull a model:

```bash
ollama pull qwen3.8
```

**qwen3.8 is the recommended default here**, not just a placeholder example:
it's a recent release and, in practice, one of the few local models that
reliably drives multi-step tool-calling without losing track of what it's
doing (repeating a tool call, forgetting to conclude, or returning malformed
arguments). That's the actual bottleneck for something like this bot, which
has to chain database lookups, letter drafting, and mail tools correctly turn
after turn -- worth the heavier hardware it needs (see "Requirements" above)
if you can run it. If you can't, a smaller tool-calling model (qwen2.5,
llama3.1, mistral-nemo) will run on much lighter hardware, but expect more of
the failure modes above the smaller/older it gets; a cloud key (see "Cloud
LLM" below) sidesteps the hardware question entirely. Check Ollama is
actually responding before moving on:

`OLLAMA_NUM_CTX` (`.env`, 20000 by default) matters more than it looks:
left unset, Ollama picks its own context window, which is often smaller than
what this agent's system prompt plus ~37 tool schemas already need before a
single word of conversation -- the visible symptom is short, seemingly
truncated (or outright empty) replies to even a simple question in Discord
or the terminal UI's Chat pane. If that happens, check `OLLAMA_NUM_CTX`
is actually being applied rather than raise it blindly first.

```bash
curl http://localhost:11434/api/tags
```

### Cloud LLM (optional alternative to Ollama)

Skip the Ollama install entirely and point hobot at a cloud provider instead
-- covers scoring, letters, `/ask`, and everything else that calls the LLM,
with no other code path affected. Set in `.env`:

```bash
LLM_PROVIDER=openai            # or: anthropic
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini       # any OpenAI model with tool-calling support
OPENAI_BASE_URL=               # optional: OpenRouter/Groq/any OpenAI-compatible endpoint instead of OpenAI itself
```

or, for Anthropic:

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
```

`LLM_PROVIDER=ollama` (the default) ignores both blocks entirely -- there's
no need to remove them if you switch back later.

### 2. Clone the repo and install dependencies

```bash
git clone <your-fork-or-this-repo> hobot
cd hobot
./setup.sh          # Linux/macOS. On Windows (PowerShell): .\setup.ps1
```

or by hand, same result:

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 3. Create the Discord bot (optional -- skip if you only want the terminal UI)

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

`.env` already exists if step 2 used `setup.sh`/`setup.ps1`; otherwise
`cp .env.example .env` first. Open it and fill in your LLM (`OLLAMA_MODEL`,
or the cloud block above), and `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` if
you want Discord. Everything else can stay empty for now. Run it:

```bash
python daemon.py
```

Discord (if configured) should connect and show up online on your server
within a few seconds. `Ctrl+C` to stop it, the "running continuously" section
below covers leaving it running without a terminal open.

In a second terminal, whether or not Discord is configured:

```bash
python cli.py
```

opens the terminal UI -- a full-screen dashboard against the same database
`daemon.py` is writing to. More on it in "Terminal UI" below.

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

## Job sources and search coverage

`JOBSPY_SITES` (`.env`) controls which sites JobSpy scrapes, comma-separated.
`indeed` is the default and most reliable. `linkedin` is worth adding
(`JOBSPY_SITES=indeed,linkedin`) — live-tested, real extra volume, often
more detailed listings than Indeed for the same search — but JobSpy's own
upstream docs warn it rate-limits hard (around page 10) without a paid
proxy, so accept that it may go quiet after a while; nothing needs fixing
when that happens, it just means fewer results from that one site.
`glassdoor` and `google` are also technically supported but were found
broken in testing (glassdoor: location resolution fails outright; google:
returns 0 results on every query tried) — see `tools/sources_jobspy.py` if
you want to re-check them yourself later. `zip_recruiter` is untested here.

If your profile has more than one target role, each one is searched
separately against every enabled source (not merged into a single combined
query) — feel free to list a few rather than trying to phrase one query that
covers all of them.

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

## ATS watchlist (optional)

Not France-specific, and fundamentally different from every other source
above: no keyword search. Greenhouse, Ashby, and Lever (the three ATS
platforms behind a lot of tech-company career pages) each expose a free,
unauthenticated JSON feed of a single company's current openings, keyed by
that company's own "board slug" -- there's no "search every company on
Greenhouse" endpoint, only "this one company's board." So this source works
off an explicit list of companies (`core/ats_watchlist.py`) instead of your
profile's target roles/locations.

Two ways onto that list:

```bash
ATS_WATCHLIST=GitLab,Anthropic,Ramp
```

in `.env` (company names -- resolved to a working board on first use and
cached from then on), and, for growing the list afterward without editing
files, three chat tools reachable through `/ask`: `surveiller_entreprise`
("watch Anthropic"), `retirer_entreprise_suivie`, and
`lister_entreprises_suivies`.

Resolution is best-effort: a company name gets tried against all three
platforms under a few common spelling conventions (lowercase, hyphenated, no
spaces). If none of those match, that's reported plainly rather than
guessed at -- pass the company's exact slug from its careers page URL
instead if you already know it uses one of these three
(`boards.greenhouse.io/THIS-PART`, `jobs.ashbyhq.com/THIS-PART`,
`jobs.lever.co/THIS-PART`).

`surveiller_entreprise` does one more thing on success: it also creates a
spontaneous-application lead for that company (same idea as La Bonne
Alternance/La Bonne Boite's recruiter leads -- a placeholder posting for a
company with no open role yet, tagged `ats_lead`) and immediately looks up a
contact for it (Pappers + web search first, Hunter.io/Snov.io only as their
own existing fallback for a verified email -- same priority order as asking
for a company's contacts by hand, nothing changed there). The lead shows up
in `/offers` like any other posting, gets picked up by the next scheduled
run for scoring, and an automatic cover letter if it scores well -- nothing
extra to do for that part.

Worth knowing before adding a long list of companies: these three platforms
skew heavily toward tech/software/startup hiring -- genuinely useful if
that's the target role, close to empty otherwise. Postings found this way
go through the exact same dedup/scoring/letter pipeline as every other
source, tagged `ats_greenhouse`/`ats_ashby`/`ats_lever` in `/sources` and
`/log`.

## Funding-news leads (optional)

Grows the ATS watchlist above on its own, without ever adding to it
silently: every `FUNDING_CHECK_INTERVAL_DAYS` (2 by default), hobot reads
recent headlines from Maddyness and Frenchweb (free RSS, no key), and for
anything that reads like a specific company just raised money, checks
whether that company has a Greenhouse/Ashby/Lever board -- a company that
just closed a round is a reasonable bet to be hiring soon, even before a
posting shows up.

A match is **proposed**, never added automatically: a notification (Discord
and desktop, same as everything else proactive in this project) lists what
was found, and adding one for real is one message away --
`"surveille <company>"` through `/ask`, same as adding any other company by
hand. Nothing here writes to the watchlist on its own.

`verifier_actus_levees_de_fonds` (through `/ask`) runs the check on demand
instead of waiting for its schedule -- slow (an LLM call per candidate
headline), and an empty result most of the time is normal, not a failure:
of 18 real headlines checked while building this, 6 turned out to be a
specific company with a real board on one of the three platforms, the rest
were either opinion pieces/retrospectives (no specific company) or a
company not on Greenhouse/Ashby/Lever.

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

## Quality checks

Two advisory checks, on by default, that never change what happens to a
posting or a generated file -- they add a warning where you'd already be
looking, nothing more:

**Possible ghost jobs** (`tools/ghost_job.py`): a posting flagged when it's
been open for a while with no sign of actually filling
(`GHOST_JOB_DAYS_THRESHOLD`, default 45 days) and/or its own text uses the
stock phrasing a company reaches for when a listing isn't tied to a current
opening ("keep your CV on file", "talent pool", "always accepting
applications", and a few equivalents in French). Either signal alone is
enough to flag -- shown as a "Ghost?" column in the terminal UI's Offers
tab, a warning line on the offer detail screen and in Discord's
`/offers`/`/offer`, and mentioned to the chat agent so `/ask` brings it up
before drafting anything for that posting. Neither signal is proof by
itself: a slow, legitimate hiring process can take 45+ days too. Treat it
as "worth a second look before spending time on this one," not a verdict --
nothing gets excluded or hidden because of it.

**ATS-readability check** (`tools/ats_check.py`): after generating a cover
letter or a tailored CV, hobot re-opens the PDF it just wrote and tries to
extract its text back out, the same way an Applicant Tracking System's own
scanner would -- catching the case where a document looks right on screen
but an ATS would see little or nothing (an image-only render, a font
substitution that silently failed). A failure logs a warning and shows up
as a note on the offer detail screen next to the file in question; it never
blocks the file from being saved; a low character count is the only
signal, so a deliberately partial edit (the CV tailoring section above,
"fully flattened CV" case) is not mistaken for a failure -- only whichever
document was actually generated stays that low.

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

`EMAIL_POLL_INTERVAL_MIN` controls how often it checks, day and night alike
(20 minutes by default, plus `EMAIL_POLL_JITTER_MIN` of random jitter so it
doesn't land on the exact same minute every time) -- there's no separate
off-peak interval.

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

## Terminal UI

A second interface onto the exact same engine Discord talks to -- postings,
applications, drafts, profile, chat, and every report Discord's slash
commands cover, as a full-screen dashboard instead of commands. It's its own
separate process, not something `daemon.py` runs for you (a systemd-managed
daemon has no terminal attached for a full-screen app to take over) -- start
it whenever you want to look at something, close it when you're done:

```bash
python cli.py
```

Seven tabs (`F1`-`F7` to jump between them, or click/Tab through them):

- **Status** -- active/paused, the scoring backlog, the best open posting,
  last discovery/mail-check runs plus when each is next due (computed from
  the same cron/interval config the daemon itself schedules from, not a live
  connection to it -- the terminal UI is its own process, see "Terminal UI"
  above), and buttons for pause/resume, an on-demand weekly digest, and a
  reset (wipes everything, asks for confirmation first).
- **Offers** -- the best-scored open postings; Enter on a row (scored or
  still waiting to be scored) opens the full posting with mark-applied /
  exclude / tailor-CV / edit-and-save-the-letter / open-the-original-listing
  actions, same as the equivalent Discord buttons. "Show unscored" swaps the
  same table to the backlog still waiting on a score, oldest first; "Score
  now" scores that backlog immediately instead of waiting for the next
  scheduled discovery run (same step, same per-run cap, as the scheduled one
  -- `/unscored` and `/ask "score the pending offers"` cover the same ground
  from Discord).
- **Applications**, **Drafts** (send/delete, same daily send cap as
  everywhere else), **Profile** (view + upload a CV; "Edit profile" changes
  name/skills/target roles/target locations directly, no LLM round-trip --
  free-text updates, and anything CV-derived like experience/education, still
  go through Chat instead, same as Discord's `/profile`/`/ask` flow).
- **Chat** -- the same agent `/ask` talks to, full conversation history,
  proposed email sends shown with their own confirm button.
- **Reports** -- sources, quotas, search log, search strategy,
  notifications, funnel, score breakdown, and an on-demand gap analysis.

No host, port, or login to configure -- it never listens on the network,
it's just a program you run in your own terminal, so there's nothing for
anyone else to reach. It talks to the same `hobot.db` (and the same
`checkpoints.db` conversation memory) `daemon.py` does, whether or not the
daemon happens to be running at the time; the only thing that needed real
cross-process wiring was pause/resume, which is why that's stored in the
database now instead of only in the daemon's memory.

`CLI_CHAT_THREAD_ID` (default `cli`): the Chat pane uses its own fixed
conversation, separate from any Discord user's, so the two never interleave
unexpectedly by default. Set it to a real Discord user id instead if you want
one shared conversation across both interfaces.

## Accessibility

The terminal UI is built on [Textual](https://textual.textualize.io/), which
implements a couple of standard, external conventions -- no flag inside this
project needed:

- `NO_COLOR=1 python cli.py` switches every screen to a monochrome
  rendering (the [no-color.org](https://no-color.org) convention, read by
  Textual itself). Every status here is backed by real text as well as
  color -- `Active`/`Paused`, `yes`/`no`, a score number -- so nothing is
  lost in this mode; that's checked by actually running it this way, not
  assumed.
- `TEXTUAL_ANIMATIONS=none python cli.py` turns off the (already minimal)
  UI animations, for anyone sensitive to motion.
- Fully operable by keyboard alone: `Tab`/`Shift+Tab` moves focus,
  `Enter`/`Space` activates a button or opens a posting, arrow keys move
  inside a table, `F1`-`F7` jump between tabs, `Ctrl+P` opens Textual's own
  command palette (theme switching included). No feature here needs a mouse.

Screen readers: a terminal screen reader (NVDA/JAWS on Windows Terminal,
VoiceOver on Terminal.app, Orca on Linux) reads whatever text the terminal
draws, the same as any other terminal program -- there's no special
integration on top of that here, and a live-updating dashboard is
inherently noisier for one than a static command-line tool would be.
`NO_COLOR=1` combined with your screen reader's own review-cursor/say-all
commands is the most usable combination available today; if that's still not
enough, Discord (plain text and embeds, whatever accessibility tooling you
already use there) is the more accessible way to run this right now.

## Running it continuously

In development, `python daemon.py` in a terminal is enough on any OS. All
three options below are the daemon only -- `cli.py` stays something you run
by hand in your own terminal whenever you want it, whether or not the
background service is running.

### Linux (systemd, user-level)

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

### macOS (launchd)

```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/com.hobot.daemon.plist ~/Library/LaunchAgents/
# edit the copied file: replace every /path/to/hobot (five places: python and
# daemon.py under ProgramArguments, WorkingDirectory, and the two log paths)
# with the real path where you cloned the repo
launchctl load ~/Library/LaunchAgents/com.hobot.daemon.plist
```

Managing it: `launchctl list | grep hobot` (running if listed),
`launchctl unload ~/Library/LaunchAgents/com.hobot.daemon.plist` to stop,
`tail -f daemon.log` in the repo directory for live logs. Runs only while
you're logged in (a LaunchAgent, not a LaunchDaemon) -- the right scope for
a personal tool watching your own inbox/Discord, and restarts itself after a
crash the same way the systemd unit does.

### Windows (Task Scheduler)

No bundled task file here (Task Scheduler's XML export ties itself to the
exporting machine's paths and user SID, so a checked-in one would need
editing anyway) -- five minutes in the GUI instead:

1. Open Task Scheduler, **Create Task** (not *Basic Task*, so the extra
   options below are available).
2. **General**: name it `hobot`, **Run whether user is logged on or not**
   if you want it to survive a logout, not just a locked screen.
3. **Triggers** -> **New**: **At log on**.
4. **Actions** -> **New**: Program/script `C:\path\to\hobot\.venv\Scripts\python.exe`,
   arguments `-u daemon.py`, "Start in" `C:\path\to\hobot`.
5. **Settings**: check **If the task fails, restart every** (1 minute, a
   few attempts) for the same crash-recovery the systemd/launchd options get.

`Get-ScheduledTask -TaskName hobot` (PowerShell) to check it's registered;
the Task Scheduler GUI's **History** tab for logs, since this path has
nothing built-in equivalent to `journalctl`/`tail -f`.

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

23 slash commands in total -- `/ask` is the last one below, and opens up a
much wider set of tools in plain language on top of the other 22. Roughly the
Discord equivalent of the terminal UI's tabs (see below): most of these
mirror something a Reports/Status/Drafts pane shows at a glance there:

| Command | What it does |
|---|---|
| `/status` | Summary of the last discovery/mail run and when the next one is due |
| `/offers` | Best active postings, with an "already applied" button on each |
| `/offer <id>` | Full detail on one posting: description, score, status, last seen live |
| `/unscored` | Postings still waiting to be scored, oldest first |
| `/files <id>` | Sends the already-generated CV + cover letter for a posting |
| `/applied <id>` | Marks a posting applied (drops it from `/offers`) |
| `/exclude <id>` | Manually excludes a posting (no longer appears in `/offers`, `/status`, searches) |
| `/funnel` | Conversion funnel: found → scored → letter → sent → reply → interview |
| `/breakdown` | Postings per score tier, and how many are still waiting to be scored |
| `/applications` | Lists applications already sent or marked |
| `/sources` | Status of each discovery source (last attempt, errors, backing off or not) |
| `/strategy` | Search keyword currently in use for each discovery source |
| `/log` | Recent history of discovery runs (keywords searched, results found) |
| `/notifications` | History of notifications sent (postings, mail, digest, cleanup) |
| `/quotas` | This month's usage for quota-limited APIs (Adzuna, Hunter.io, Snov.io) |
| `/drafts` | Pending reply drafts on the sending account (Gmail) |
| `/gaps` | AI analysis of the gaps that show up most often in poorly-scored postings |
| `/digest` | Triggers the weekly digest right now (summary of the week) |
| `/profile <file>` | Sets your profile from an attached CV (PDF or `.docx`) |
| `/pause` / `/resume` | Stops or restarts scheduled checks (postings + mail) |
| `/reset` | Wipes everything (postings, applications, contacts, uploaded CV) -- asks for confirmation, start clean |
| `/ask "<text>"` | Everything else, see below |

`/ask` is backed by an agent that can, among other things: look up a
specific posting or run a live search for a city/keyword outside your usual
coverage, list postings still waiting to be scored and score that backlog
right now instead of waiting for the next scheduled discovery run, write or
review a cover letter, tailor your CV for a specific offer, look up a
company's contacts (officers, verified emails), add or remove a company from
the ATS watchlist or run the funding-news check on demand (see above),
exclude or re-include a posting, show or edit your profile, give the score
breakdown across all postings, list applications already sent, list pending
mail drafts, report how many sends are left for the day, and create/edit/
delete a draft reply. One plain-language sentence is enough, no need to know
a tool's exact name for the agent to pick the right one.

## Architecture

```
daemon.py            — the daemon process: scheduler (APScheduler) + Discord bot (optional)
cli.py                — terminal UI entry point (its own separate process, see "Terminal UI" above)
setup.sh, setup.ps1  — base install (venv, dependencies, .env) for Linux/macOS and Windows
systemd/, launchd/    — background-service unit files, see "Running it continuously"
graphs/
  discovery_graph.py  — fetch (JobSpy + French sources + ATS watchlist) -> dedup -> score (LLM) -> letters -> log
  email_graph.py      — fetch mail -> classify (LLM) -> draft replies
  chat_agent.py        — the agent behind /ask and the terminal UI's Chat pane (ReAct, tool-calling,
                          persistent SqliteSaver memory shared between both interfaces)
core/
  db.py               — SQLite schema (offers, applications, user_profile, daemon_flags...)
  daemon_state.py      — pause flag (DB-backed, cross-process) + live scheduler ref (in-process only)
  queries.py          — read queries shared by discord_bot.py and tui/ (single source of truth)
  llm.py, llm_provider.py — shared chat entry point + provider factory (Ollama/OpenAI/Anthropic)
  profile.py          — CV or free text -> structured profile
  circuit_breaker.py  — automatic backoff per failing source (anti-ban)
  locations.py         — French city registry -> coordinates/INSEE code (optional)
  france_travail_auth.py — shared OAuth2 for the France Travail Connect APIs (optional)
  api_usage.py        — monthly quota tracking (Adzuna, Hunter.io, Snov.io)
  ats_watchlist.py    — the company list tools/sources_ats.py checks (optional)
tools/
  sources_jobspy.py   — discovery connector (no key required)
  sources_lba.py, sources_adzuna.py, sources_francetravail.py,
  sources_labonneboite.py — French sources (optional, see table above)
  sources_ats.py      — Greenhouse/Ashby/Lever connector, per-company (optional, see table above)
  sources_funding_news.py — Maddyness/Frenchweb RSS, feeds the ATS watchlist (optional)
  funding_check.py    — turns a funding headline into a watchlist proposal (optional)
  sources_pappers.py, sources_hunter.py, sources_snov.py — contacts (optional)
  web_search.py       — SearXNG (optional)
  email_tools.py      — IMAP/SMTP (optional)
  link_check.py       — flags postings whose link has died
  ghost_job.py        — flags a posting that looks like it may not be a real, current opening
  ats_check.py        — verifies a generated PDF's text is actually extractable
  documents.py        — renders cover letters to PDF
  cv_tailor.py        — edits your own uploaded CV in place, per offer (optional)
  discord_bot.py       — slash commands + bridge to the agent (optional interface)
tui/                  — the terminal UI (see "Terminal UI" above)
  app.py              — Textual App: tabs, theme, cross-pane wiring
  modals.py            — confirmation dialogs, text prompts, the offer detail screen
  panes/               — one module per tab, each reusing core/queries.py, tools/common.py, etc.
  app.tcss             — stylesheet
```

Every posting goes through: discovery -> dedup (against the database and
across sources) -> LLM scoring (against your profile) -> above a threshold,
an automatic cover letter. Nothing ever gets sent automatically anywhere,
applications and emails stay drafts until an explicit human action, on
either interface.

## Search tip

JobSpy does real scraping, not an API call: broad, generic target roles
("backend developer" rather than a very specific title) tend to give better
results. `modifier_profil`/`definir_profil` (via `/ask`) let you adjust
that at any point.

## Design and safety

A few rules held everywhere in the code, not just stated here:

- **No automatic email send, ever.** The agent drafts one on its own
  initiative when a posting is worth it; only a human clicking a button --
  in Discord or the terminal UI -- triggers an actual SMTP send.
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

None of that stops the bot from working correctly for the main loop: finding
postings, sorting them, writing letters, handling mail.

## License

MIT, see [LICENSE](LICENSE). `assets/fonts/` bundles static instances of
Montserrat (SIL Open Font License, see `assets/fonts/Montserrat-OFL.txt`),
used as a font-fidelity fallback by the CV tailoring feature.
