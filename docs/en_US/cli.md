# The CLI

Two entry points, `rhosocial-github-actions-tracker` and
`rhosocial-github-actions-tracker-db`. The global `--format` (`text` / `json`) and
`--verbose` flags go **before** the subcommand.

```bash
rhosocial-github-actions-tracker --format json status            # ci_status from the shell
rhosocial-github-actions-tracker runs --name owner/repo         # recent runs
rhosocial-github-actions-tracker latest --name owner/repo       # the newest run
rhosocial-github-actions-tracker jobs --run-id <id> --step 3    # a job's steps
rhosocial-github-actions-tracker logs --job-id <id> --step 3    # a step log
rhosocial-github-actions-tracker failures --run-id <id> --repo owner/repo   # one copy per distinct failure
rhosocial-github-actions-tracker trigger --workflow ci.yml --ref main --name owner/repo
rhosocial-github-actions-tracker repo subscribe --name owner/repo --credential pat/work
```

## Database maintenance

```bash
rhosocial-github-actions-tracker-db is-initialized
rhosocial-github-actions-tracker-db init
rhosocial-github-actions-tracker-db stats
rhosocial-github-actions-tracker-db usage
rhosocial-github-actions-tracker-db cleanup --runs-before 90d --logs-before 365d
rhosocial-github-actions-tracker-db deliveries --unhandled --limit 20
```
