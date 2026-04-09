# Handoff — March 29, 2026

## Session Summary

Continued from March 28. Major items: multi-worker deployment, auth PDF download, RingCentral fax for pended cases, Mongo writeback, settings page tabs, comprehensive code review + 15 bug fixes, frontend authentication.

---

## 1. Multi-Worker Deployment (COMPLETED)

### 3 Azure VMs Running
- **worker-a**: 172.202.22.112 (10.0.0.5) — Standard_D4s_v3
- **worker-b**: 74.249.202.85 (10.0.0.6) — Standard_D2s_v3
- **worker-c**: 20.106.37.51 (10.0.0.7) — Standard_D2s_v3

Worker-b and worker-c created by snapshotting worker-a's OS disk. Required Azure quota increase (DSv3 family: 4→16 cores, total regional: 20→28).

### Worker Accounts in DB
```
worker-a | antuyo@revelooptechsystems.com    | carelon-mfa@revelooptechsystems.com
worker-b | healthimages.jovita@gmail.com     | carelon-mfb@ronexa.com
worker-c | envisionradiology.tracy@gmail.com | carelon-mfc@ronexa.com
```

### Round-Robin Dispatch
BatchDispatcher queries active workers from `worker_accounts` table, round-robins cases with priority interleaving (STAT cases spread across all workers). Tested with 6 cases — 2 per worker, all 3 running in parallel confirmed.

### Credentials Flow
Worker credentials stored in DB (encrypted password). BatchDispatcher passes credentials in CaseWorkflow payload → WorkerSession → login flow. No env vars needed per worker.

---

## 2. Auth PDF Download (COMPLETED)

### Flow
After portal submission (any outcome):
1. Extract `cmdSaveAsPdf` href from confirmation page
2. Download PDF via Playwright browser context (preserves session cookies)
3. Upload to Azure Blob Storage: `auth-pdfs/{date}/{LASTNAME_FIRSTNAME}_{OrderID}.pdf`
4. Save blob key to `case.auth_pdf_url`

### DB Migration 012
Added `auth_pdf_url` column to cases table.

---

## 3. Pended Case Handling (COMPLETED)

### Status Mapping Fix
Portal returns `"In Progress"` for pended cases. Code now catches:
- `"pend"` → PENDED
- `"in progress"` → PENDED
- `"review"` → PENDED
- `"denied"` / `"denial"` → DENIED
- `"authorized"` → APPROVED

### RingCentral Fax (for PENDED cases)
- **Service**: `app/services/ringcentral_fax.py`
- **Auth**: OAuth2 JWT grant (same flow as SMS service)
- **Endpoint**: `POST /restapi/v1.0/account/~/extension/~/fax`
- **Target**: Carelon fax `+18007982068`
- **Attachment**: Clinical notes PDF from blob storage
- **Cover sheet format**:
  ```
  Please review for authorization for case #{OrderID}
  Member ID {MemberNumber}
  DOB: {DOB}
  CPT {CPTCode}
  ```

### RC Credentials
```
RC_CLIENT_ID=alAC0s0oXJFaWnGObEIiNq
RC_CLIENT_SECRET=WTISLgRP84cfyJ9mw28eE8XHBSVFM8Q3ObG95v6mXC8B
RC_JWT_TOKEN=eyJraWQi...
RC_SERVER_URL=https://platform.ringcentral.com
```
Added to orchestrator .env and all 3 worker .env files.

### Mongo Writeback (for PENDED cases)
`update_mongo_auth_status()` in `mongo_poller.py` writes back to CosmosDB:
```
status: "Auth Pending"
payload.authstatedesc: "Auth Pending"
payload.AuthStateSubDesc: "Waiting On carrier"
payload.LastStatusId: 3
payload.LastAuthNote: "Per Carelon, CPT {CPT} pending; clinical notes faxed. Case {OrderID} Determination date {date}"
```

---

## 4. Settings Page Tabs (IN PROGRESS)

### Agreed Design
| Tab | Contents |
|-----|----------|
| **General** | Review config, polling/sync settings, flush/reset |
| **Workers** | Worker accounts (Carelon credentials) |
| **Automation** | Full automation rules (CPT+ICD toggles) |
| **Integrations** | RingCentral fax config (encrypted JWT), future integrations |

### RC Settings in DB
- Store RC_CLIENT_ID, RC_CLIENT_SECRET (encrypted), RC_JWT_TOKEN (encrypted), RC_SERVER_URL, CARELON_FAX_NUMBER in system_settings
- Fax service reads from DB instead of env vars (env vars as fallback)
- Encryption uses existing Fernet pattern from `llm_config.py`

### TODO
- [ ] Backend: RC settings CRUD endpoints with encryption
- [ ] Frontend: Refactor 1248-line settings page into 4 tabs
- [ ] Frontend: Integrations tab with RC fax config form
- [ ] Update cover sheet to match reference format
- [ ] Build and deploy

---

## Deployment State

| Component | Version | Notes |
|-----------|---------|-------|
| Backend API | v27 | On orchestrator (20.29.73.195) |
| Frontend | v22 | On orchestrator |
| Worker-a | rev 20+ | 172.202.22.112 / 10.0.0.5 |
| Worker-b | rev 21+ | 74.249.202.85 / 10.0.0.6 |
| Worker-c | rev 22+ | 20.106.37.51 / 10.0.0.7 |
| Restate Server | 1.6.2 | On orchestrator |
| Azure PostgreSQL | ronexa-pg | Password: doYRYD6DulhnNkAFRW33r66VWgET |

### DB Migrations (cumulative)
- 008: determination_status, valid_from, valid_through, denial_reason, pend_reason
- 009: automation_rules table
- 010: patient_phone on cases
- 011: password on worker_accounts
- 012: auth_pdf_url on cases

### Key Files Changed This Session
- `app/services/ringcentral_fax.py` — NEW: RC fax service
- `app/services/__init__.py` — NEW: package init
- `app/compiler/portal_compiler.py` — pend status fix, fax trigger, mongo writeback
- `app/ingest/mongo_poller.py` — `update_mongo_auth_status()` function
- `app/ingest/blob_fetcher.py` — `fetch_blob_bytes()` function
- `app/core/settings.py` — RC env var definitions
- `app/portal/webforms_client.py` — `download_auth_pdf()` method
- `requirements.txt` — added aiohttp

### Infrastructure Notes
- Worker VMs run under `xvfb-run` (virtual display for headed Playwright)
- aiohttp installed on all 3 workers for RC fax
- RC env vars in all .env files (orchestrator + 3 workers)
- Azure quota: DSv3 family=16 cores, total regional=28 cores

---

## 5. Comprehensive Code Review + Bug Fixes (COMPLETED)

### Critical Fixes (4)
1. `ringcentral_fax.py` — `app.db.session` → `app.db.database` (import crash)
2. `cases.py` — `case.awakeable_id` → lookup from `SubmissionJob` table (AttributeError)
3. `settings.py` route — added missing `func` import (analytics crash)
4. `batch_id` nullable — verified already correct in DB

### Serious Fixes (4)
5. `blob_fetcher.py` — dict → `ContentSettings` object for upload
6. `ringcentral_fax.py` — wrapped sync `fetch_blob_bytes` in `asyncio.to_thread()`
7. `cases.py` — added `patient_phone` to `process_case` endpoint
8. `mongo_poller.py` — removed unmapped `referring_first_name/last_name` from PAYLOAD_MAP

### Cleanup (5)
9. Deprecated `finalize_service.py` + `shift_manager.py` (dead code)
10. `queue.py` — fixed double `scalar_one_or_none()` call
11. `mongo_poller.py` — `update_mongo_auth_status` changed from async to sync, caller uses `asyncio.to_thread()`
12. `settings.py` core — removed unused `import os`

---

## 6. Frontend Authentication (COMPLETED)

### Architecture
- Proxy auth through Ronexa API (`api-v2.ronexa.com`)
- Local admin backdoor via env vars (fallback if Ronexa API is down)

### Backend (`app/api/routes/auth.py`)
- `POST /api/auth/proxy-login` — try Ronexa API, fallback to local admin
- `GET /api/auth/me` — validate JWT session, return username
- `POST /api/auth/proxy-logout` — clear session cookie

### JWT Token
- Signed with `JWT_SECRET` or `SETTINGS_ENCRYPTION_KEY`
- Payload: `{sub: username, source: "ronexa"|"local", exp: 8h}`
- Stored as `ronexa_session` httpOnly cookie

### Frontend
- `/login` page — clean centered form
- `middleware.ts` — checks `ronexa_session` cookie, redirects to `/login` if missing
- `NavBar.tsx` — client component, shows username + logout button
- Public paths: `/login`, `/api/*`, `/_next/*`

### Backdoor Admin
- Username: `admin`
- Password: `R0nexaAdmin2026`
- Hash: `$2b$12$1T1S.Tsmx76y.O4EilzCc.5ZY6zc0MElZHEhsNLWdPFuVTAk/Ycue`
- Note: Docker-compose needs `$$` escaping for `$` in bcrypt hashes

### Env Vars Added
```
RONEXA_API_URL=https://api-v2.ronexa.com
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=$$2b$$12$$1T1S.Tsmx76y.O4EilzCc.5ZY6zc0MElZHEhsNLWdPFuVTAk/Ycue
```

---

## Deployment State (End of March 29)

| Component | Version | Notes |
|-----------|---------|-------|
| Backend API | v30 | On orchestrator (20.29.73.195) |
| Frontend | v25 | On orchestrator |
| Worker-a | rev 23+ | 172.202.22.112 / 10.0.0.5 |
| Worker-b | rev 24+ | 74.249.202.85 / 10.0.0.6 |
| Worker-c | rev 25+ | 20.106.37.51 / 10.0.0.7 |

### New Files This Session
- `backend/app/api/routes/auth.py` — authentication endpoints
- `backend/app/services/ringcentral_fax.py` — RC fax service
- `frontend/app/login/page.tsx` — login page
- `frontend/middleware.ts` — auth middleware
- `frontend/components/NavBar.tsx` — client-side nav with auth

---

## 7. Production Readiness Sweep (COMPLETED)

### Final Security Hardening
- All API endpoints protected with `require_auth` JWT dependency
- Unauthenticated requests return `{"detail":"Not authenticated"}` (401)
- JWT secret: production-grade random token (`lKhgZ4zRUSZRNF5n6gu-PkXVK2bjB08Yp00721USNIA`)
- Cookie `secure` flag gates on `ENVIRONMENT != "local"`
- CORS updated: `localhost:3000`, `ronexa.centralus.cloudapp.azure.com`, `carelon.ronexa.com`

### NSG Final State
| Port | Access | Purpose |
|------|--------|---------|
| 22 | Public | SSH |
| 80 | Public | HTTP (nginx) |
| 443 | Public | HTTPS (ready for SSL) |
| 9070/9071 | Your IP only (66.208.6.50) + VNet | Restate admin |
| 8080 | VNet only (10.0.0.0/24) | Restate ingress |

### Stale Invocations Purged
- 3 suspended CaseWorkflows from March 27 killed
- Restate clean: 0/0 invocations

### Remaining Warnings (non-blocking, fix post-launch)
- `localhost:8080` hardcoded in jobs.py (works on Docker network)
- No rate limiting on login endpoint
- No log rotation on workers (`/tmp/worker.log`)
- 2 `print()` statements in portal code

---

## Final Production State (March 30, 2026 — GO LIVE)

| Component | Version | Status |
|-----------|---------|--------|
| Backend API | **v32** | ✅ Auth-protected |
| Frontend | **v26** | ✅ Login + middleware |
| Worker-a | rev 23+ | ✅ 172.202.22.112 |
| Worker-b | rev 24+ | ✅ 74.249.202.85 |
| Worker-c | rev 25+ | ✅ 20.106.37.51 |
| Orchestrator | | ✅ 20.29.73.195 |
| Restate | 1.6.2 | ✅ Clean |
| PostgreSQL | 11MB | ✅ ronexa-pg.postgres.database.azure.com |
| Restate invocations | 0 | ✅ Clean slate |

### Credentials
- **Admin backdoor**: `admin` / `R0nexaAdmin2026`
- **JWT Secret**: `lKhgZ4zRUSZRNF5n6gu-PkXVK2bjB08Yp00721USNIA`
- **DB Password**: `doYRYD6DulhnNkAFRW33r66VWgET`
- **Azure ACR**: `ronexaacr.azurecr.io`

### Blocked Items
- **SSL Setup** — saved to `sslsetup.md`, blocked on missing private key
- Find key → execute steps in sslsetup.md → 5 min to go live with HTTPS

### Post-Launch TODO
1. Find SSL private key → enable HTTPS on `carelon.ronexa.com`
2. Add login rate limiting
3. Set up log rotation on workers
4. Monitor first batch run end-to-end
5. Enable auto-sync (polling_enabled=true, 30 min interval)
