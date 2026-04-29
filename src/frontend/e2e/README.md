# Frontend E2E Tests

Playwright-based end-to-end tests for the Trinity frontend (#556).

## Run locally

```bash
# 1. Start a Trinity stack (the tests don't spin one up themselves)
./scripts/deploy/start.sh

# 2. Run the tests
cd src/frontend
ADMIN_PASSWORD=<your-admin-password> npm run test:e2e
```

`ADMIN_PASSWORD` is required — `e2e/auth.setup.js` uses it to log in once and
caches the session in `e2e/.auth/admin.json` (gitignored).

## Useful flags

```bash
npm run test:e2e:headed     # run with visible browser
npm run test:e2e:ui         # interactive Playwright UI
npm run test:e2e:update     # update visual regression snapshots
```

After a run, the HTML report is at `e2e/playwright-report/index.html`.

## CI

CI runs e2e only on PRs **labeled `ui`** — add the label to any frontend PR
that should be exercised end-to-end. The workflow lives at
`.github/workflows/frontend-e2e.yml` and stands up the full Trinity stack
before running tests (~5 min total).

To make e2e a required check on a PR, add the `ui` label and wait for the
workflow to complete.

## Adding tests

- Smoke tests live in `e2e/smoke.spec.js` — the lightweight cross-page checks
- New flows go in their own `*.spec.js` next to the smoke file
- Visual regression: use `await expect(page).toHaveScreenshot()`. Snapshots
  are committed in `e2e/<spec>.spec.js-snapshots/`. Run
  `npm run test:e2e:update` after intentional UI changes, then commit the
  updated PNGs.

## Why this layer exists

The frontend has no other automated test coverage today. E2E tests catch:
- Login regressions
- Top-level routing breakage
- Auth boundary violations exposed via the UI
- Color drift on the design system (with visual regression)

Cheaper layers (Vitest unit tests, type checking) are tracked in #556
Phase 1 / Phase 3 — separate follow-ups.
