# Cutting an Agent Release

## One-time setup (do once, ever)

1. Generate the release keypair locally:
   ```
   python scripts/generate_release_keypair.py
   ```
   - Commit `agent/release_pubkey.pem` (it will overwrite the placeholder).
   - Copy the printed private key.
2. In GitHub repo -> Settings -> Secrets and variables -> Actions, add:
   - `AGENT_RELEASE_PRIVATE_KEY` - the private key PEM from step 1.
   - `VPS_SSH_KEY` - an ed25519 private key whose public half you've added to
     `~/.ssh/authorized_keys` on the VPS user (e.g. for the `dropshippa`
     user).
   - `VPS_HOST` - e.g. `autoalert.pro`.
   - `VPS_USER` - e.g. `dropshippa`.
3. On the VPS, create the release dir:
   ```
   mkdir -p ~/dld-releases
   ```
   The dld container will bind-mount this at `/data/releases` read-only.
   Redeploy once (`cd ~/DailyLifeDistributor/deploy && docker compose up -d`).

## Cutting a release

1. Bump `agent/_version.py` to the new version (e.g. `"0.2.0"`).
2. Commit: `chore(agent): bump version 0.2.0`.
3. Tag and push:
   ```
   git tag agent-v0.2.0
   git push --tags
   ```
4. The `release-agent` GHA workflow builds Windows + macOS, signs each, and
   SCPs the binaries + `manifest.json` to `~/dld-releases/` on the VPS.
5. Every running agent picks up the update on its next startup (or sooner if
   we add relay-pushed update notifications later).

## Rotating the signing key

Run `scripts/generate_release_keypair.py` again, replace the GHA secret +
committed public key, and cut a new release. **Agents running an OLDER public
key will reject the new builds and stay on their current version.** Plan
key rotations to coincide with handoff events; never silently rotate.
