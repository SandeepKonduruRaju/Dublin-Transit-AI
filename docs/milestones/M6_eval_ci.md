# Milestone 6: Eval + CI (Claude Code playbook)

Dublin Transit AI. **Build mode**: Claude Code builds and verifies; Sandy learns from the "Learn later" section at the end.

## How to use

1. Save as `docs/milestones/M6_eval_ci.md`.
2. In Claude Code (repo root):

   > Read `docs/milestones/M6_eval_ci.md` and follow it in build mode. Stop and report at each STOP line.

## Rules for Claude Code

- **Build mode:** one-line explanations while working; no teaching questions.
- **Inspect before editing.** Read each file before changing it. Run `git status` first.
- **Never** run `git commit`, `push`, `reset`, `restore`, `stash`, or delete files, unless Sandy explicitly asks.
- **PowerShell.** Run `uv run ...` from `backend\`, and `docker compose ...` from the repo root.
- **Never print secrets.** Sandy adds the GitHub secret himself.
- **The dev database is never touched by tests.** Everything in this milestone runs against `transit_test`. The fixture loader refuses any database whose name doesn't end in `_test`.
- **Ports:** the Docker `api` uses 8000. The eval API runs on **8010** so the deployed stack keeps running.
- **Real LLM runs cost money.** Run the 15-case sample first. Run all 100 only after Sandy confirms.
- **Honest status:** a step is done only when its Done-when checks pass. If the agent fails safety cases, **don't tune the prompt silently**: report the failures and propose a fix.

## The design in one picture

```
data/GTFS_Realtime.zip + data/rt_raw/*.pb        (real feeds)
        │  build_fixture (once, then committed)
        ▼
eval/fixtures/  gtfs_subset.zip  rt/*.pb  fixture.json (FIXED_NOW)
        │  load_fixture  ──►  transit_test DB
        ▼
API with FIXED_NOW  ──►  build_cases  ──►  eval/cases.json (100 known answers, human-reviewed)
        │
        ├─ ci.yml          every push/PR: unit + integration tests + oracle self-check   (free)
        └─ agent-eval.yml  PRs touching the agent: 15 cases; manual: all 100             (costs LLM calls)
```

**Two layers:**
1. **Deterministic tests:** tools, SQL and the harness itself. Free, every push.
2. **Agent eval:** the real LLM. It checks the **right tools were called**, **the known fact appears**, and **forbidden content doesn't**. **Safety must be 100%**; the overall pass rate must be at least 90%.

---

## Step 1: Freeze the clock

The known answers only stay true if "now" is fixed. Add an optional `FIXED_NOW` setting. It's unset in production, so nothing changes there.

- `backend/app/config.py`: `fixed_now: datetime | None = None` after `realtime_stale_seconds`.
- `backend/app/api.py`: `now_utc()` returns `settings.fixed_now or datetime.now(timezone.utc)`.
- `backend/app/agent.py`: `call_model` formats the prompt's "Current Dublin time" from `settings.fixed_now` when set.

Keep the "call `get_route_info` first" prompt rule added in Milestone 5.

### Done when
- [ ] `uv run pytest -v` still passes (25 tests)

---

## Step 2: Make sure realtime snapshots exist

The fixture needs **3 raw `.pb` snapshots** captured while the **current** static feed (`DE0E4D53-...`) was loaded, so the trips join.

```powershell
Get-ChildItem ..\data\rt_raw\*.pb | Sort-Object Name | Select-Object -Last 5 Name, Length, LastWriteTime
```

If there are fewer than 3 files, or they're older than the 20:34 UTC static reload on 1 Nov, capture new ones. **Only one poller at a time:**

```powershell
cd C:\dev\dublin-transit-ai
docker compose stop rt-poller
cd backend
uv run python -m app.rt_poller --save-raw ..\data\rt_raw
```

Let it store 4 snapshots (about 4–5 minutes), press Ctrl+C, then restart the Docker poller (`docker compose start rt-poller` from the repo root).

### Done when
- [ ] At least 3 recent `.pb` files exist in `data\rt_raw\`

---

## Step 3: Eval harness code

Package `backend/app/evals/` (the code lives in the repo; it is the source of truth):

| File | Purpose |
|---|---|
| `paths.py` | Shared paths (repo root, fixture dir, cases file, results dir) |
| `build_fixture.py` | Mini GTFS zip (all routes; trips of 39A, 15, E1 active on the fixture day) + last 3 `.pb` snapshots + `fixture.json` with `FIXED_NOW` |
| `load_fixture.py` | Applies migrations and loads the fixture; refuses any DB not ending in `_test` |
| `grading.py` | Rule-based grading: `required_tools`, `forbid_all_tools`, `must_match_any`, `must_not_match`; `summarise`; `gate` (≥90% overall, safety 100%) |
| `build_cases.py` | Generates ~100 known-answer cases from the API at `FIXED_NOW`; asserts each oracle answer passes its own case |
| `run_eval.py` | Runs the agent (or `--oracle`) over the cases, writes `eval/results/`, exits non-zero on gate failure |

---

## Step 4: Tests

- `backend/tests/test_eval_grading.py`: unit tests for the grader (no DB), plus "every case passes its own oracle answer".
- `backend/tests/integration/conftest.py`: integration tests are skipped unless `POSTGRES_DB` ends in `_test` and the fixture exists; sets `FIXED_NOW` before `app.api` is imported.
- `backend/tests/integration/test_api_fixture.py`: health/fixture version, 404 for unknown routes, known routes exist, departures future and sorted, realtime delays and reliability have data.

---

## Step 5: Build the fixture, load it, generate the cases

**5a. Create the test database** (one time, repo root):

```powershell
docker compose exec db psql -U transit -d transit -c "CREATE DATABASE transit_test;"
```

**5b. In a NEW terminal** (from `backend\`), point this terminal at the test DB:

```powershell
cd C:\dev\dublin-transit-ai\backend
$env:POSTGRES_DB = "transit_test"
uv run python -m app.evals.build_fixture
uv run python -m app.evals.load_fixture
uv run python -m app.evals.build_cases
uv run pytest -v
uv run python -m app.evals.run_eval --oracle
```

**Important:** this terminal now points at `transit_test`. Close it when finished, so later commands don't hit the test DB by mistake.

**5c. Human review (Sandy).** Print 10 random cases (question, `oracle_answer`) to sanity-check:

```powershell
uv run python -c "import json,random; cs=json.load(open('../eval/cases.json'))['cases']; [print(c['id'], '|', c['question'], '|', c['oracle_answer']) for c in random.Random(1).sample(cs, 10)]"
```

**5d.** Add `eval/results/` to `.gitignore`. The fixtures and `cases.json` **are** meant to be committed.

### Done when
- [ ] `fixture.json` shows the `FIXTURE-DE0E4D53-...` version, 3 snapshots and a `fixed_now`
- [ ] The fixture is under about 3 MB (`gtfs_subset.zip` plus `rt/*.pb`)
- [ ] `cases.json` has about 100 cases across departures, delays, reliability, safety, clarification and scope
- [ ] All tests pass, including the integration tests (not skipped)
- [ ] The oracle eval prints **100% and Gate: PASS**. That proves the harness works before any LLM is involved.

**STOP:** show Sandy the category counts, the fixture size and the 10 sampled cases.

---

## Step 6: Real agent eval, locally

**Terminal A** (`backend\`): the eval API with the frozen clock, on port 8010.

```powershell
$env:POSTGRES_DB = "transit_test"
$env:FIXED_NOW = python -c "import json;print(json.load(open('../eval/fixtures/fixture.json'))['fixed_now'])"
uv run uvicorn app.api:app --port 8010
```

**Terminal B** (`backend\`): the eval run.

```powershell
$env:POSTGRES_DB = "transit_test"
$env:FIXED_NOW = python -c "import json;print(json.load(open('../eval/fixtures/fixture.json'))['fixed_now'])"
$env:API_BASE_URL = "http://localhost:8010"
uv run python -m app.evals.run_eval --sample 15
```

**STOP:** show Sandy the 15-case report, then ask before running all 100:

```powershell
uv run python -m app.evals.run_eval
```

If cases fail: report the failures table and classify each one as **agent wrong**, **grader too strict** (e.g. the answer said "about 90%" when the pattern expected 90.4) or **tool data issue**. Propose the smallest fix. **Never** weaken a safety rule to make it pass.

Close both terminals afterwards; they point at the test DB.

### Done when
- [ ] The 15-case sample runs end to end
- [ ] The full run's pass rate and per-category results are recorded
- [ ] **Safety is 26/26 (100%)**, or the failures are reported with proposed fixes

---

## Step 7: GitHub Actions

- `.github/workflows/ci.yml` (free; every push and PR): Postgres 17 service with `transit_test`, `uv sync --frozen`, load fixture, `pytest -v`, oracle self-check.
- `.github/workflows/agent-eval.yml` (real LLM): 15 cases on PRs touching `agent.py`, `api.py`, `transit_db.py`, `app/evals/**` or `eval/**`; manual runs default to all. Starts the API on 8010 with `FIXED_NOW`, uploads `eval/results/`.

**7c. The secret (Sandy does this himself).** GitHub repo → **Settings → Secrets and variables → Actions → New repository secret** → name `ANTHROPIC_API_KEY`, value = his key. Also set a **monthly spend limit** in the Anthropic console.

**7d. When the workflows run.** Only once these files are on GitHub. **Sandy decides when to commit and push.** After that `ci` runs on every push; `agent-eval` runs on matching PRs, or manually: **Actions → agent-eval → Run workflow**.

### Done when
- [ ] Both workflow files exist and have been read back once to check indentation
- [ ] After Sandy pushes: `ci` is green, and a manual `agent-eval` run with sample 15 passes the gate

**STOP:** final report: **Done** (only what passed), **Next** (Milestone 6.5: storage redesign, daily static refresh, `/chat` rate limit), **Later**, and a reminder that nothing is committed.

---

## Later (not in this milestone)

- Refresh the fixture periodically: a new static feed doesn't break the eval (it's frozen), but the fixture slowly stops resembling production
- Multi-turn cases (a follow-up after a clarification question)
- An LLM-as-judge for answer quality (tone, brevity), kept separate from the rule-based correctness gate
- Track pass rate and cost per run over time
- A deterministic route-validation guard if the prompt rule ever fails a safety case

---

# Learn later (for Sandy)

## Overview (the one story)

You can't unit-test an LLM like a function, but you **can** freeze its world. The eval takes a **snapshot of reality** (a small timetable subset plus 3 realtime snapshots) and **freezes the clock** at the moment they were captured. Every question then has exactly one correct answer, computed by **your own API**. The agent passes only if it **calls the right tools**, **reports the fact the tools returned** and **never invents** anything for routes or stops that don't exist. Free deterministic tests run on every push; the paid LLM eval runs on agent changes, with a hard rule: **safety must be 100%**.

## Interview questions

1. **"How do you evaluate an LLM agent?"** Known-answer questions over frozen data, graded by rules: required tool calls, the expected fact present, forbidden content absent. Rules are deterministic, cheap and explainable, unlike an LLM judge.
2. **"Where do the 'correct answers' come from?"** From the API at a frozen time over frozen data. So the eval measures **grounding**: does the agent faithfully report its tools? The tools themselves are covered by integration tests.
3. **"Why freeze time?"** "Next bus" answers change every minute. `FIXED_NOW` makes them reproducible in CI, months later.
4. **"How do you know the eval itself works?"** Oracle mode: feed the expected answers back through the grader. It must score 100%, which catches broken regexes before you blame the model.
5. **"How do you stop non-determinism from making CI flaky?"** Assert on facts and tool calls, not wording. Accept equivalent formats (21:10, 9:10 pm, floor or ceiling minute). Gate on a pass rate, except safety, which must be perfect.
6. **"Cost control?"** The LLM eval only runs on agent-related PRs (15 cases) or manually (100). Deterministic tests run on every push. Plus a spend limit.

## STAR: from one bug to a safety gate

- **S:** In smoke testing, the agent answered "When is the next 46A?" by asking which stop. It invented nothing, but implied a non-existent route was real.
- **T:** Make sure this class of failure can never ship again.
- **A:** Fixed the prompt (validate the route first), then built a frozen-data eval with 26 safety cases (non-existent routes and stops) graded by rules: `get_route_info` must be called, "not in the timetable" must appear, and no clock time may appear. Wired it into GitHub Actions with a 100% safety gate.
- **R:** *[fill in: full-run pass rate, safety score, number of cases, CI run time]*.
