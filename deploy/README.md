# CI/CD: GitHub → VPS

Every push to `main` runs `.github/workflows/deploy.yml`:

1. **Check** — lint (undefined names, unused imports, syntax errors), compile
   every Python file, check the deploy script. Pull requests stop here.
2. **Deploy** — copy the code to `/opt/crystalgenie/incoming` on the VPS, then
   run `deploy/remote_deploy.sh` there, which:
   - backs up the running code to `/opt/crystalgenie/previous`
   - installs the new code — never touching `.env*`, `.venv`, `best.pt`,
     `models/` or training photos
   - reinstalls packages only if `requirements.txt` changed
   - restarts the API and waits for `/health`
   - **rolls back to the previous code automatically** if it doesn't come up
   - restarts the trainer only if the trainer's code changed
3. **Verify** — calls the public `/health`.

Deploys never overlap; a push made during a deploy waits its turn.

## One-time setup (GitHub → Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `VPS_SSH_KEY` | the private key: on the Mac, `pbcopy < ~/.ssh/crystalgenie_github_deploy` then paste |
| `VPS_KNOWN_HOSTS` | the server's fingerprints: `pbcopy < ~/.ssh/crystalgenie_known_hosts` then paste |

The key is used only by GitHub Actions; its public half is in the server's
`/root/.ssh/authorized_keys` (comment `github-actions-deploy@crystal_genie_backend`).

**To revoke it** (e.g. if the repo is compromised):

```sh
ssh root@187.127.213.241 "sed -i '/github-actions-deploy@crystal_genie_backend/d' /root/.ssh/authorized_keys"
```

## Day to day

- Deploy: push to `main` (or Actions → *Check & deploy* → *Run workflow*).
- Watch: the Actions tab on GitHub.
- A failed deploy leaves the previous version running; the log shows why.
- Server settings (`.env`) are not deployed — edit them on the server and
  `systemctl restart crystalgenie`.
- Rollback doesn't undo package installs; if a `requirements.txt` change breaks
  things, revert the commit and push.
