# State checker canary

Every 3 hours, each of the 25 states the eligibility checker serves gets an
end-to-end check from GitHub Actions. A check fails loudly when the state's
checker stops giving the right answers.

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
- a red run, with one error annotation per problem;
- the run summary;
- an issue labelled `canary` and `canary-<state>`, assigned to the repository owner, commented on by each later failure and closed by the first pass;
- GitHub's failure email for scheduled runs, if the owner's notification settings have Actions emails on.

Issues are opened only for runs of `main`; a run dispatched from a branch is
testing that branch.

## Layout

```
canary/                  the logic, Python standard library only
  states.py              the 25 states, their order and their schedule
  check.py               one state's check
  client.py              the checker's HTTP API, with transport retries
  issue.py               the state's issue: open, comment, close
  workflows.py           generates the per-state workflows and the table below
  __main__.py            python -m canary check | issue | workflows
.github/workflows/
  state-<code>.yml       one per state: schedule, manual run, then calls check-state.yml
  check-state.yml        the steps every state runs
  ci.yml                 lint, tests, workflow lint (actionlint, zizmor)
```

Each state has a workflow of its own: its own schedule, run history, badge,
manual run button and on/off switch. The per-state files are thin callers of
one reusable workflow, so the steps are written once. They are generated from
`canary/states.py`, and CI fails when the two disagree.

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
- **Cost.** About 200 jobs a day. In a private repository each job bills at
  least a minute, so that is about 6,000 runner minutes a month, more while a
  state fails (the retry and round-trip pauses). GitHub Free includes about
  2,000. Public repositories run standard runners free.
- **One store, 23 alerts.** When R2 is slow, every served state's round trip
  fails, so each state's issue opens. That is by design: each state stands
  alone.

## Running it

- **On demand:** Actions, then the state's workflow, then *Run workflow*. Tick
  *all_cases* to check every sample, not just two.
- **Locally:**
  ```sh
  CHECKER_BASE_URL=https://checker.example.com python -m canary check --state wa
  ```
- **Adding a state:** add a line to `canary/states.py`, run
  `python -m canary workflows --write`, and commit the new workflow.
- **Development:** `uv sync`, then `uv run pytest`, `uv run ruff check .`,
  `uv run actionlint` and `uv run zizmor .github/workflows`.

## States

The states start 7 minutes apart, so 25 checks spread over each 3-hour window
instead of hitting the checker and GitHub's scheduler at the top of the hour.

<!-- states:start -->
| State | Group | Runs at (UTC) | Last run |
|---|---|---|---|
| CA California | First 10 | 00:03, 03:03, ... | [![ca](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ca.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ca.yml) |
| CT Connecticut | First 10 | 00:10, 03:10, ... | [![ct](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ct.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ct.yml) |
| FL Florida | First 10 | 00:17, 03:17, ... | [![fl](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-fl.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-fl.yml) |
| NC North Carolina | First 10 | 00:24, 03:24, ... | [![nc](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-nc.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-nc.yml) |
| NY New York | First 10 | 00:31, 03:31, ... | [![ny](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ny.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ny.yml) |
| PA Pennsylvania | First 10 | 00:38, 03:38, ... | [![pa](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-pa.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-pa.yml) |
| VA Virginia | First 10 | 00:45, 03:45, ... | [![va](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-va.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-va.yml) |
| WA Washington | First 10 | 00:52, 03:52, ... | [![wa](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-wa.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-wa.yml) |
| WI Wisconsin | First 10 | 00:59, 03:59, ... | [![wi](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-wi.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-wi.yml) |
| WY Wyoming | First 10 | 01:06, 04:06, ... | [![wy](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-wy.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-wy.yml) |
| GA Georgia | Second 8 | 01:13, 04:13, ... | [![ga](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ga.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ga.yml) |
| IN Indiana | Second 8 | 01:20, 04:20, ... | [![in](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-in.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-in.yml) |
| MO Missouri | Second 8 | 01:27, 04:27, ... | [![mo](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-mo.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-mo.yml) |
| MS Mississippi | Second 8 | 01:34, 04:34, ... | [![ms](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ms.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ms.yml) |
| MT Montana | Second 8 | 01:41, 04:41, ... | [![mt](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-mt.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-mt.yml) |
| NH New Hampshire | Second 8 | 01:48, 04:48, ... | [![nh](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-nh.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-nh.yml) |
| NV Nevada | Second 8 | 01:55, 04:55, ... | [![nv](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-nv.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-nv.yml) |
| VT Vermont | Second 8 | 02:02, 05:02, ... | [![vt](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-vt.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-vt.yml) |
| AZ Arizona | Third 7 | 02:09, 05:09, ... | [![az](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-az.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-az.yml) |
| MA Massachusetts | Third 7 | 02:16, 05:16, ... | [![ma](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ma.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-ma.yml) |
| MI Michigan | Third 7 | 02:23, 05:23, ... | [![mi](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-mi.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-mi.yml) |
| OH Ohio | Third 7 | 02:30, 05:30, ... | [![oh](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-oh.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-oh.yml) |
| OR Oregon | Third 7 | 02:37, 05:37, ... | [![or](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-or.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-or.yml) |
| SD South Dakota | Third 7 | 02:44, 05:44, ... | [![sd](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-sd.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-sd.yml) |
| TX Texas | Third 7 | 02:51, 05:51, ... | [![tx](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-tx.yml/badge.svg)](https://github.com/vvAlpha/state-checker-canary/actions/workflows/state-tx.yml) |
<!-- states:end -->
