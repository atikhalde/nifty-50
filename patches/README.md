# Pending workflow patch

`0001-fix-actions-market-hours-gate-and-dedupe-cache.patch` fixes two real bugs in
`.github/workflows/scanner.yml`. It could **not** be pushed with the rest of the fix
because the bot credential used for that push does not carry GitHub's `workflows`
permission (GitHub refuses any push from an app that adds or edits files under
`.github/workflows/` without it).

## What it fixes

1. **The market-hours gate never gated anything.**
   The step ran `sys.exit(0)` when the clock was outside 09:15–15:30 IST. Exiting a
   step with status 0 just marks that step successful — every later step, including
   the scan itself, still ran. The cron window (`*/5 3-10 * * 1-5` = 03:00–10:55 UTC
   = 08:30–16:25 IST) is wider than the NSE session, so the scanner was firing
   pre-open and post-close.
   The patch has the gate publish a `run` output via `$GITHUB_OUTPUT` and guards the
   later steps with `if: steps.gate.outputs.run == 'true'`. It also checks the IST
   weekday and lets manual `workflow_dispatch` runs through unconditionally.

2. **The dedupe cache was pinned to the first ever snapshot.**
   The restore step used `actions/cache@v4` with the constant key
   `sent-alerts-placeholder`. `restore-keys` are only consulted when the primary key
   *misses*; once the post-job save created `sent-alerts-placeholder`, every later
   run scored an exact-key hit and restored that first, permanently stale snapshot.
   The freshly saved `sent-alerts-<run_id>-<hash>` entries were never read, so old
   alerts could be re-sent — breaking the "never send the same alert twice" promise.
   The patch switches to `actions/cache/restore@v4` with a run-unique primary key
   plus the `sent-alerts-` prefix, so the newest saved state always wins.

## How to apply

From the repository root, on branch `arena/01a05b54-nifty-50`:

```bash
git apply patches/0001-fix-actions-market-hours-gate-and-dedupe-cache.patch
git add .github/workflows/scanner.yml
git commit -m "Fix Actions market-hours gate and dedupe cache key"
git push origin arena/01a05b54-nifty-50
```

A normal user push (or a token with the `workflow` scope) is accepted.
Delete this `patches/` directory once the patch has landed.
