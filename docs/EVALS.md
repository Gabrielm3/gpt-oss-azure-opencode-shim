# Evals in CI/CD

The shim changes what an LLM answer looks like on its way to the client, so its
quality is a property of **our code and a model we do not control**. The eval
setup separates the two, because they fail in different ways and need
different gates.

| Tier | Runs | Catches | Cost | Gate |
| ---- | ---- | ------- | ---- | ---- |
| 1. Scoring and polyfill unit tests | every PR | logic bugs in scoring, repair and parsing | free | must pass |
| 2. Replay of recorded real answers | every PR | **code regressions**: a change that alters a decision on a real Azure answer | free | must pass |
| 3. Live eval against Azure | nightly on `master`, or by hand | **upstream drift**: the model behaves differently with the same code | ~60 requests per run | statistical, opens an issue |

## Tier 2: recorded answers are the regression gate

A PR must not change how the shim treats answers the model really gave. The
shim can record raw upstream answers (`SHIM_TRACE_DIR`), and
[`evals/fixtures.py`](../evals/fixtures.py) turns chosen ones, after review and
scrubbing, into fixtures in `tests/fixtures/polyfill/`. Each fixture stores the
answer and the expected decision (`rescued`, `native`, left unchanged), and
[`tests/test_fixture_replay.py`](../tests/test_fixture_replay.py) replays them all on every PR.

This is deterministic and free, and it grows from production: a new failure
seen in traces becomes a fixture, so it can never regress silently. Live
calls in a PR gate would be slow, would cost money on every push, and would
fail on upstream noise that has nothing to do with the diff.

## Tier 3: the nightly run detects drift

[`live-eval.yml`](../.github/workflows/live-eval.yml) runs every night on
`master`. It installs the shim, starts it with the Azure credentials, sends the
15 forced-tool scenarios ([`evals/scenarios.py`](../evals/scenarios.py)) 4 times
each, and hands the results to [`evals/gate.py`](../evals/gate.py).

Nothing in the code changed since the last green night, so a significant
change means Azure changed the model's behavior. This is not hypothetical:
between 2026-09-22 and 2026-09-23 the rewrite-only stall rate went from 12%
to 5% with identical code and scenarios.

### Why a statistical gate and not a threshold

With 60 requests, a rate has a 95% interval about ±10 points wide. A fixed
threshold such as "stalled turns under 10%" would fail on noise on some nights
and miss real changes on others. The gate asks a different question: *is
tonight different from the baseline beyond sampling noise?*

- **One gating metric.** Stalled turns (no tool call at all) is the failure a
  user sees: the agent loop stops. Strict success and progress are reported
  with intervals but never fail the run. Each check is one-sided at 2.5%,
  so gating on three metrics would raise a false alarm about every two weeks.
  One metric still means about one false alarm in 40 quiet nights. The next
  refinement is to open an issue only after two drift nights in a row, which
  cuts that to about one in 1,600.
- **Interval on the difference.** Both tonight's run and the baseline are
  samples. The gate computes Newcombe's hybrid score interval for
  `tonight - baseline` (built from the two Wilson intervals). It reports
  **drift** only when that interval lies entirely above zero.
- **Three outcomes, not two.** `pass`, `drift`, and `inconclusive`, which
  covers too many upstream errors, too few answers, or a broken setup. An
  Azure outage must not look like a model change, and a broken setup must
  never look like a pass. Each outcome has its own exit code.

### What the gate can and cannot see

Against the current baseline (4 stalled of 60):

| Nightly run | Baseline | Drift is flagged from |
| ----------- | -------- | --------------------- |
| 60 requests | 60 | 12/60 stalled (20%) |
| 240 requests | 60 | 41/240 (17%) |
| 60 requests | 240 (pooled) | 9/60 (15%) |

The baseline, not the nightly run, limits sensitivity: a run 4× larger barely
helps, while a larger baseline does. So the next step is to pool several
nightly results into the baseline. The current baseline is typed from the
2026-09-23 run in [`PROBLEM.md`](PROBLEM.md) and says so in its `source` field.
The baseline changes only through a reviewed PR, like any golden file.

### When the gate fires

- **Drift:** the run fails and an `eval-drift` issue is opened, or commented
  on if one is already open, with the gate's table and a link to the run.
  The results, the report and the content-free outcome log are kept as an
  artifact for 90 days.
- **Inconclusive:** the run fails without an issue. GitHub already emails the
  owner of a failed scheduled run.

## Cost and safety

- **Cost:** one run is 60 requests with at most 2,048 output tokens each.
  `timeout-minutes` and a single-run `concurrency` group bound it. A yearly
  Azure budget (`infra/budget.tf`) emails the subscription Owner at 20%, 50%,
  80% and 100% of actual spend.
- **Credentials:** none stored. The job logs in the `id-azure-shim-eval`
  managed identity through GitHub OIDC (a federated credential for the
  `live-eval` environment, which only `master` can use), and the shim gets
  Entra ID tokens from that session. The identity holds only `Cognitive
  Services OpenAI User` on the account. The workflow never runs on
  `pull_request`, so code from a fork can never reach it. The shim's own log,
  which may contain upstream error text, is not uploaded.
- **Scheduled workflows** are disabled by GitHub after 60 days without
  repository activity. Re-enable it from the Actions tab.

## What changes at larger scale

- **Free-form outputs:** this eval scores tool calls against JSON Schemas, which
  is exact. For free text, the scorer would be an LLM judge, calibrated
  against a small human-labeled set, with the judge's agreement rate tracked
  as its own metric.
- **More scenarios per failure mode**, stratified, so a drift report can say
  *which* behavior moved.
- **Per-PR live evals** for prompt or model changes: same gate, but the
  baseline is the `master` run from the same hour, so drift cancels out and
  only the diff is measured.
