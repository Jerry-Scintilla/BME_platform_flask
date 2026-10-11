# BME backend maintenance rules


## Server maintenance branch

For maintenance on server 172.25.56.83, use `codex/server-maintenance` in both BME repositories.
Make fixes and commits on that branch and deploy its committed versions.
Do not merge, cherry-pick, push changes to, or deploy from master until the user explicitly approves synchronization.
Preserve unrelated local changes and record the deployed commit IDs.
