# CI/CD — what runs when you push to main

## TL;DR

Every push to `main` triggers **`test.yml`** (backend pytest + frontend build). If both pass, **`deploy.yml`** builds Docker images, pushes them to `ronexaacr.azurecr.io`, and runs `./deploy.sh` on the orchestrator over SSH — same steps a human operator would run from their laptop.

Failed tests block the deploy. The Actions tab shows live progress: <https://github.com/ReveloopRCM/ronexa-carelon-sub/actions>.

## Pull requests

Open a PR against `main` → `test.yml` runs. PRs do **not** deploy anything (no Azure secrets are exposed to PR runs from forks; secrets only flow on direct pushes to `main`).

## The deploy gate

`deploy.yml` only starts after `test.yml` succeeds on the same commit. If `test.yml` fails, the deploy job stays in the "Waiting for test gate" state until you either fix the issue and push again, or cancel the workflow.

## Manual override

When you need to deploy a specific commit without pushing, or skip a flaky test:

1. Actions → `deploy` → **Run workflow**
2. Pick branch + the `deploy.sh` target you want (`auto`, `backend`, `frontend`, `workers`, `restart`)
3. Manual runs **skip the test gate** (`if: github.event_name == 'push'` only on the wait job)

## Emergency operator override (CI is broken)

The exact same `./deploy.sh all` you've always used still works from your laptop. CI is additive, not exclusive. If GitHub Actions is unavailable:

```bash
./deploy.sh all
```

Runs interactively. Prompts you to confirm uncommitted changes if any. CI mode skips that prompt.

## Secrets in use

| Secret | What it is | Rotation |
|---|---|---|
| `AZURE_CLIENT_ID` | App registration `github-actions-ronexa-deploy` | Recreate the app if compromised |
| `AZURE_TENANT_ID` | Your Azure tenant — public-ish, but kept secret out of habit | Stable |
| `AZURE_SUBSCRIPTION_ID` | Subscription holding `rg-ronexa-prod` | Stable |
| `DEPLOY_SSH_KEY` | ed25519 private key for `ronexa@` on all 4 VMs | Generate a new keypair, install pubkey on VMs, update secret |
| `DEPLOY_SSH_KNOWN_HOSTS` | SSH host fingerprints for the 4 VMs | Re-run `ssh-keyscan` if a VM is rebuilt |

The Azure app registration uses **OIDC federation** — there is **no long-lived client secret** stored anywhere. GitHub gives Azure a short-lived token per workflow run.

## Restate / WorkerLoop handling

`deploy.sh` kills in-flight Restate invocations when workflow files change (avoids journal-replay errors). When `CI=true` (set by `deploy.yml`), it also:

- Auto-runs `alembic upgrade head` on the orchestrator
- Auto-restarts WorkerLoops via the Restate API

So a CI deploy is end-to-end — no manual post-deploy checklist. Interactive `./deploy.sh` still prints the checklist for human operators who may want to batch migrations.

## When things go wrong

- **`test.yml` fails** → check the Actions tab for the failing test name. Fix locally, push again.
- **OIDC login fails** → the federated credential subject must be `repo:ReveloopRCM/ronexa-carelon-sub:ref:refs/heads/main`. If you forked or renamed the repo, recreate the federated credential in Azure (AAD app `github-actions-ronexa-deploy` → Certificates & secrets → Federated credentials).
- **SSH connection refused** → A VM was rebuilt and lost the deploy pubkey. Re-install it (see `.github/workflows/README.md` "Adding a new VM" below).
- **Need to deploy hotfix without testing** → don't. Push a PR with the fix + a skipping test marker if needed. The whole point of the gate is to prevent the "deploy broken code" pattern.

## Adding a new VM

If you spin up `ronexa-worker-d`:

1. Install the deploy pubkey on the new VM:
   ```bash
   PUB="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEkvy76wjVzJbhLKXei5MjhqyECIQE+7oKXUI9ygwpkq github-actions-ronexa-deploy"
   ssh ronexa@<new-vm-ip> "echo \"$PUB\" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```
2. Re-scan the host and update `DEPLOY_SSH_KNOWN_HOSTS` secret:
   ```bash
   ssh-keyscan -t ed25519 <new-vm-ip> | gh secret set DEPLOY_SSH_KNOWN_HOSTS --repo ReveloopRCM/ronexa-carelon-sub --body-file=-
   ```
3. Add the new IP to `deploy.sh`'s `WORKER_IPS` array.
