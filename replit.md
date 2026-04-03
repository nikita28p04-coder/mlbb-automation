# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Structure

```text
artifacts-monorepo/
├── artifacts/              # Deployable applications
│   └── api-server/         # Express API server
├── lib/                    # Shared libraries
│   ├── api-spec/           # OpenAPI spec + Orval codegen config
│   ├── api-client-react/   # Generated React Query hooks
│   ├── api-zod/            # Generated Zod schemas from OpenAPI
│   └── db/                 # Drizzle ORM schema + DB connection
├── scripts/                # Utility scripts (single workspace package)
│   └── src/                # Individual .ts scripts, run via `pnpm --filter @workspace/scripts run <script>`
├── pnpm-workspace.yaml     # pnpm workspace (artifacts/*, lib/*, lib/integrations/*, scripts)
├── tsconfig.base.json      # Shared TS options (composite, bundler resolution, es2022)
├── tsconfig.json           # Root TS project references
└── package.json            # Root package with hoisted devDeps
```

## TypeScript & Composite Projects

Every package extends `tsconfig.base.json` which sets `composite: true`. The root `tsconfig.json` lists all packages as project references. This means:

- **Always typecheck from the root** — run `pnpm run typecheck` (which runs `tsc --build --emitDeclarationOnly`). This builds the full dependency graph so that cross-package imports resolve correctly. Running `tsc` inside a single package will fail if its dependencies haven't been built yet.
- **`emitDeclarationOnly`** — we only emit `.d.ts` files during typecheck; actual JS bundling is handled by esbuild/tsx/vite...etc, not `tsc`.
- **Project references** — when package A depends on package B, A's `tsconfig.json` must list B in its `references` array. `tsc --build` uses this to determine build order and skip up-to-date packages.

## Root Scripts

- `pnpm run build` — runs `typecheck` first, then recursively runs `build` in all packages that define it
- `pnpm run typecheck` — runs `tsc --build --emitDeclarationOnly` using project references

## Packages

### `artifacts/api-server` (`@workspace/api-server`)

Express 5 API server. Routes live in `src/routes/` and use `@workspace/api-zod` for request and response validation and `@workspace/db` for persistence.

- Entry: `src/index.ts` — reads `PORT`, starts Express
- App setup: `src/app.ts` — mounts CORS, JSON/urlencoded parsing, routes at `/api`
- Routes: `src/routes/index.ts` mounts sub-routers; `src/routes/health.ts` exposes `GET /health` (full path: `/api/health`)
- Depends on: `@workspace/db`, `@workspace/api-zod`
- `pnpm --filter @workspace/api-server run dev` — run the dev server
- `pnpm --filter @workspace/api-server run build` — production esbuild bundle (`dist/index.cjs`)
- Build bundles an allowlist of deps (express, cors, pg, drizzle-orm, zod, etc.) and externalizes the rest

### `lib/db` (`@workspace/db`)

Database layer using Drizzle ORM with PostgreSQL. Exports a Drizzle client instance and schema models.

- `src/index.ts` — creates a `Pool` + Drizzle instance, exports schema
- `src/schema/index.ts` — barrel re-export of all models
- `src/schema/<modelname>.ts` — table definitions with `drizzle-zod` insert schemas (no models definitions exist right now)
- `drizzle.config.ts` — Drizzle Kit config (requires `DATABASE_URL`, automatically provided by Replit)
- Exports: `.` (pool, db, schema), `./schema` (schema only)

Production migrations are handled by Replit when publishing. In development, we just use `pnpm --filter @workspace/db run push`, and we fallback to `pnpm --filter @workspace/db run push-force`.

### `lib/api-spec` (`@workspace/api-spec`)

Owns the OpenAPI 3.1 spec (`openapi.yaml`) and the Orval config (`orval.config.ts`). Running codegen produces output into two sibling packages:

1. `lib/api-client-react/src/generated/` — React Query hooks + fetch client
2. `lib/api-zod/src/generated/` — Zod schemas

Run codegen: `pnpm --filter @workspace/api-spec run codegen`

### `lib/api-zod` (`@workspace/api-zod`)

Generated Zod schemas from the OpenAPI spec (e.g. `HealthCheckResponse`). Used by `api-server` for response validation.

### `lib/api-client-react` (`@workspace/api-client-react`)

Generated React Query hooks and fetch client from the OpenAPI spec (e.g. `useHealthCheck`, `healthCheck`).

### `scripts` (`@workspace/scripts`)

Utility scripts package. Each script is a `.ts` file in `src/` with a corresponding npm script in `package.json`. Run scripts via `pnpm --filter @workspace/scripts run <script>`. Scripts can import any workspace package (e.g., `@workspace/db`) by adding it as a dependency in `scripts/package.json`.

## Python Automation Package

### `python-automation/` — MLBB Device Farm Automation

Standalone Python package (not part of the pnpm monorepo). Automates Mobile Legends: Bang Bang on Android devices via Selectel Mobile Farm.

**Install:** `cd python-automation && pip install -e .`

**Run:** `mlbb-automation run --config config.yaml`

**Stack:**
- Python 3.10+
- Appium-Python-Client 5.x (UiAutomator2 for Android)
- Pydantic v2 (configuration)
- structlog (structured JSON logging)
- Pillow (screenshot handling)

**Package structure:**
```
python-automation/mlbb_automation/
├── config/settings.py          # Pydantic v2 config (YAML + MLBB_* env vars)
├── device_farm/
│   ├── base.py                 # Abstract DeviceFarmClient + ReservedDevice (adb_host, adb_port)
│   ├── selectel_client.py      # Selectel Mobile Farm REST API client (IAM auth)
│   └── adb_connector.py        # ADB key gen, adb connect/disconnect (wraps adb binary)
├── actions/executor.py         # Appium WebDriver action wrapper (tap, swipe, type, etc.)
│                               # → runs adb connect before session, disconnect after
├── cv/
│   ├── ocr.py                  # EasyOCR wrapper (OcrEngine, OcrResult)
│   ├── template_matcher.py     # Multi-scale template matching (TemplateMatcher)
│   ├── screen_detector.py      # 11-state ScreenDetector (OCR + template signals, RU+EN)
│   └── state_machine.py        # BFS-based StateMachine for screen navigation
├── logging/logger.py           # Structlog JSON logger + RunLogger (artifacts, screenshots)
├── recovery/manager.py         # Freeze detection + auto-recovery watchdog
└── scenarios/
    ├── engine.py               # ScenarioRunner: sequential steps, retry, recovery, checkpoints
    ├── watchdog.py             # Background popup-dismissal watchdog
    └── steps/
        ├── google_account.py   # Full: Settings → Add Account → Google (email+password, no 2FA)
        ├── install_mlbb.py     # Full: Play Store install + launch (idempotent)
        ├── mlbb_onboarding.py  # Full: Skip intro/tutorial, reach main menu
        └── payment.py          # Full: Shop → Diamonds → Google Pay → result detection
```

**CLI:**
```
python -m mlbb_automation setup-adb                          # Generate ADB key, print QAAAA... pubkey
python -m mlbb_automation check --config config.yaml        # Pre-flight: creds + ADB key + devices
python -m mlbb_automation run --config config.yaml           # Full scenario
python -m mlbb_automation run --config config.yaml --dry-run # Navigate to payment, skip tap
python -m mlbb_automation run --config config.yaml --step google_account  # Single step
python -m mlbb_automation devices --config config.yaml       # List farm devices
```

**ADB setup (one-time):**
1. `python -m mlbb_automation setup-adb` — generates `~/.android/adbkey.pub`
2. Copy the `QAAAA...` key → Selectel: Control Panel → Account → Access → ADB Keys
3. `python -m mlbb_automation check` — verify connectivity

**Config file:** `python-automation/config.example.yaml` — copy to `config.yaml` and fill in credentials.

**Artifacts per run:** `run_artifacts/<run_id>/` — `events.jsonl`, `report.json`, `screenshots/`

**Test suite:** 172+ tests in `python-automation/tests/` — `python -m pytest tests/ -v`

**Key design decisions:**
- Selectel farm uses ADB over TCP (not a farm-hosted Appium URL): `adb connect adb.mobfarm.selectel.ru:<port>`
- ADB public key (QAAAA... format) must be registered in Selectel before connecting
- AppiumExecutor does `adb connect` before Appium session, `adb disconnect` after
- Local Appium server (localhost:4723) is used for sessions, not farm-provided URL
- IAM token auth (X-Auth-Token) from cloud.api.selcloud.ru — 24h TTL, auto-refresh
- UiAutomator2Options used for Appium 5.x compatibility
- Abstract `DeviceFarmClient` base allows swapping providers
- All actions retry 3x with exponential backoff on StaleElement/Timeout
- RecoveryManager detects screen freezes via image hash comparison
- ScenarioRunner: per-step retry + RecoveryManager recovery + checkpoint screenshots
- find_element() 4-stage strategy: template → OCR → Appium @text → content-desc
- Google Pay: probes NATIVE_APP then each WEBVIEW context for the Pay button
- dry_run mode navigates to payment screen but skips the final Pay tap
- ScreenDetector: 11 states, EN + RU OCR signals, collision-free spec ordering
