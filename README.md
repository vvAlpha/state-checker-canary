# State checker canary

Every 3 hours, one batch checks all 25 states the eligibility checker serves,
end to end, from GitHub Actions: one job per state, side by side, then one
summary table for the batch. A state fails loudly when its checker stops
giving the right answers.

In the Actions tab, each run is one batch. Its summary page is the batch
table, with each state's detail folded underneath; its jobs are the states.

## What one check does

1. **Reads the checker's configuration.** This gives the state's sample entity
   ids (from the checker's regression pack, each with its expected answer) and
   whether the state is served.
2. **Looks up two samples live.** It calls `?live=true`, so the checker scrapes
   the state's registry right then:
   - the first sample expected to be active, in every run;
   - one of the others in turn, so a day of runs covers every sample.

   Each answer must be well formed and say it came from the registry, not
   storage. An existing entity must come back `verified`, and a made-up id
   `not_found`. A registry that does not answer gets one more try a minute
   later.
3. **Round-trips through storage.** Right after the live answer, a normal
   lookup must be served from R2 with an observation this run produced. This
   proves answers are saved and read back.

Whether each state *should* be served is set here, in `canary/states.py`
(Indiana is not, since its firewall blocks the checker). A checker that drops
a state by mistake therefore fails its check rather than excusing itself.

| Fails the run (someone has to act) | Only warns (the run stays green) |
|---|---|
| The checker unreachable, or its configuration unreadable or short of samples | A verdict that moved since the pack was recorded (the pack wants updating) |
| A state served, or not served, against `canary/states.py` | A state meant not to be served (Indiana) |
| An answer that is malformed, not live when asked live, or has the wrong outcome | No answer in the run that storage would serve |
| The registry not answering, twice in a row | |
| Storage not serving this run's answer back, or serving it back different | |
| The 20-minute budget running out, or the check itself crashing | |

**Failing loudly** means:
- the state's red job in the batch, with one error annotation per problem;
- the batch table, which lists every state with its first problem;
- an issue labelled `canary` and `canary-<state>`, assigned to the repository owner, commented on by each later failure and closed by the first pass;
- GitHub's failure email for scheduled runs, if the owner's notification settings have Actions emails on.

Issues are opened only for runs of `main`; a batch dispatched from a branch is
testing that branch.

## Layout

```
canary/                  the logic, Python standard library only
  states.py              the 25 states, in the console's order
  check.py               one state's check
  client.py              the checker's HTTP API, with transport retries
  batch.py               reads a batch's reports: the summary table, each state's result
  issue.py               a state's issue: open, comment, close
  __main__.py            python -m canary plan | check | batch
.github/workflows/
  check.yml              the batch: plan, one check job per state, summary
  ci.yml                 lint, tests, workflow lint (actionlint, zizmor)
```

```
plan ── check CA ─┐
     ├─ check CT ─┤
     ├─ ...       ├── batch summary (table, each state's issue)
     └─ check TX ─┘
```

Each state's job is independent: one failing never stops another, and none
waits on another. The workflow only runs Python; what a check does is in
`canary/`.

## Setup

One repository secret:

| Secret | Value |
|---|---|
| `CHECKER_BASE_URL` | The checker's https origin, no path (for example `https://checker.example.com`) |

The checks call the checker's public lookup API, so the registry scraping, the
proxies, the captcha handling and the R2 credentials all stay on the checker's
server. None of them is in this repository or its secrets.

## Limits

- **Nothing notices the canary stopping.** A disabled workflow, a dropped
  schedule or an exhausted Actions allowance runs no step, so no step can
  report it. Badges and the last run time show it; an external heartbeat
  would catch it.
- **Cost.** 27 jobs a batch (plan, 25 states, summary), 8 batches a day. In
  a private repository each job bills at least a minute, so that is about
  6,500 runner minutes a month, more while states fail (the retry and
  round-trip pauses). GitHub Free includes about 2,000. Public repositories
  run standard runners free.
- **One store, 23 alerts.** When R2 is slow, every served state's round trip
  fails, so each state's issue opens. That is by design: each state stands
  alone.

## Running it

- **On demand:** Actions, then *State checks*, then *Run workflow*. Leave
  *states* as `all`, or name some (`oh,wa`) to rerun just those. Tick
  *all_cases* to check every sample, not just two per state.
- **Locally:**
  ```sh
  CHECKER_BASE_URL=https://checker.example.com python -m canary check --state wa
  ```
- **Adding a state:** add a line to `canary/states.py`. The next batch gives
  it a job.
- **Development:** `uv sync`, then `uv run pytest`, `uv run ruff check .`,
  `uv run actionlint` and `uv run zizmor .github/workflows`.
