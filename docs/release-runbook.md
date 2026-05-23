# Cutting an Agent Release

## One-time setup (do once, ever)

### 1. Generate the release signing keypair

```
python scripts/generate_release_keypair.py
```

This writes:
- `agent/release_pubkey.pem` — **commit this** (it will overwrite any existing key).
- `~/.dld-keys/release_private.pem` — **do not commit**. The script prints the
  exact `gh secret set` command to upload this for both newer and older `gh`
  versions; copy/paste whichever fits your installed `gh`.

> **Important — line endings.** The private key MUST land in the GHA secret
> with LF line endings only. The script writes to a file so you can upload it
> from there (no clipboard, no terminal copy/paste) — if you ever paste a PEM
> by hand on Windows you will almost certainly introduce CRLF and the GHA
> `Build + sign` step will fail with `MalformedFraming`.

### 2. Create the release SSH key (separate from your personal key)

The GHA workflow needs an SSH key whose public half is in the VPS user's
`~/.ssh/authorized_keys`. Generate one to a file outside the repo:

```powershell
mkdir $env:USERPROFILE\.dld-keys -Force | Out-Null
ssh-keygen -t ed25519 -f $env:USERPROFILE\.dld-keys\release_key -N "" -C deploy-key
```

Add the public half to the VPS:

```powershell
type $env:USERPROFILE\.dld-keys\release_key.pub
wsl ssh dropshippa "cat >> ~/.ssh/authorized_keys"
# (paste, then Enter, then Ctrl+D)
```

### 3. Set the four GHA secrets

For each of these, upload from the file — never paste from a terminal:

| Secret | Value |
|--------|-------|
| `AGENT_RELEASE_PRIVATE_KEY` | contents of `~/.dld-keys/release_private.pem` |
| `VPS_SSH_KEY`               | contents of `~/.dld-keys/release_key` |
| `VPS_HOST`                  | the VPS IP (not the Cloudflare-fronted hostname — Cloudflare doesn't proxy SSH) |
| `VPS_USER`                  | the VPS user, e.g. `root` |

**Newer `gh` (>= 2.40):**
```powershell
gh secret set AGENT_RELEASE_PRIVATE_KEY --body-file "$env:USERPROFILE\.dld-keys\release_private.pem"
gh secret set VPS_SSH_KEY               --body-file "$env:USERPROFILE\.dld-keys\release_key"
gh secret set VPS_HOST                  --body "167.235.26.13"
gh secret set VPS_USER                  --body "root"
```

**Older `gh` (no `--body-file`)** — read the file as raw text, normalize line
endings to LF, pass via `--body`:
```powershell
$priv = (Get-Content -Raw "$env:USERPROFILE\.dld-keys\release_private.pem") -replace "`r`n","`n" -replace "`r","`n"
gh secret set AGENT_RELEASE_PRIVATE_KEY --body $priv

$ssh = (Get-Content -Raw "$env:USERPROFILE\.dld-keys\release_key") -replace "`r`n","`n" -replace "`r","`n"
gh secret set VPS_SSH_KEY --body $ssh

gh secret set VPS_HOST --body "167.235.26.13"
gh secret set VPS_USER --body "root"
```

### 4. Create the release directory on the VPS

```
wsl ssh dropshippa "mkdir -p ~/dld-releases"
```

(The `dld` container bind-mounts this at `/data/releases` read-only. Already
configured in `deploy/docker-compose.yml`.)

## Cutting a release

1. Bump `agent/_version.py` to the new version (e.g. `"0.3.0"`):
   ```powershell
   (Get-Content agent\_version.py) -replace '"\d+\.\d+\.\d+"', '"0.3.0"' | Set-Content agent\_version.py
   git diff agent\_version.py    # sanity-check
   ```
2. Commit and push:
   ```
   git add agent\_version.py
   git commit -m "chore(agent): bump version 0.3.0"
   git push
   ```
3. Tag and push the tag:
   ```
   git tag agent-v0.3.0
   git push --tags
   ```
4. Watch the `release-agent` workflow on GitHub Actions. It builds Windows +
   macOS in parallel, signs each, assembles `manifest.json`, and SCPs
   everything to `~/dld-releases/` on the VPS.
5. Verify the release went live:
   ```
   curl https://autoalert.pro/agent/releases/manifest.json
   ```
6. Every running agent picks up the update on its next startup. (Relay-pushed
   "update available" notifications are a future enhancement.)

### Re-running a failed release with the same tag

If the workflow fails partway, delete the tag and re-push after the fix:
```
git tag -d agent-v0.3.0
git push origin :refs/tags/agent-v0.3.0
git tag agent-v0.3.0
git push --tags
```

## Rotating the signing key

Run `scripts/generate_release_keypair.py` again, replace the GHA secret +
committed public key, and cut a new release. **Agents running an OLDER public
key will reject the new builds and stay on their current version.** Plan key
rotations to coincide with handoff events; never silently rotate.
