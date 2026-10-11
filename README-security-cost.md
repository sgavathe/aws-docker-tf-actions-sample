# Security and cost checks

Three pieces, all read-only against AWS:

| File | What it does | Where it runs |
| --- | --- | --- |
| `infra/prowler-role/` | `ProwlerReadOnly` IAM role that GitHub Actions assumes via OIDC (main branch only) | `terraform apply` once, from your laptop |
| `.github/workflows/prowler.yml` | Weekly Prowler security scan of the account + "Run workflow" button | GitHub Actions |
| `scripts/prebuild-check.sh` | fmt, validate, Prowler IaC scan, actionlint, shellcheck before you push | Your laptop |
| `scripts/cost-check.sh` | Trusted-Advisor-style cost checks (AWS CLI, Steampipe Thrifty, Infracost) | Your laptop |

## 1. Check locally first

```bash
chmod +x scripts/*.sh
scripts/prebuild-check.sh            # fmt, validate, Prowler IaC scan, linters
scripts/prebuild-check.sh --aws      # + live Prowler scan of the account (critical/high)
scripts/cost-check.sh                # quick cost review, AWS CLI only
```

Tools (macOS):

```bash
brew install hashicorp/tap/terraform actionlint shellcheck
# Prowler: either have Docker Desktop running (image includes Trivy), or
pipx install prowler==5.44.0 && brew install trivy
```

Reports go to `./reports/` (add `reports/` to `.gitignore`).

## 2. Create the role

```bash
terraform -chdir=infra/prowler-role init
terraform -chdir=infra/prowler-role plan     # review: 1 role, 2 managed attachments, 1 inline policy
terraform -chdir=infra/prowler-role apply
terraform -chdir=infra/prowler-role output -raw prowler_role_arn
```

It looks up the GitHub OIDC provider your deploy role already uses; it does not create or change it.
State is local to that folder on purpose, so it can't affect the site's stack.

## 3. Wire up the workflow

1. GitHub repo -> Settings -> Secrets and variables -> Actions -> **Variables** -> `PROWLER_ROLE_ARN` = the output above.
2. Push `.github/workflows/prowler.yml` to `main`.
3. Actions -> Prowler AWS Scan -> Run workflow.

Results: the run's summary page, the `prowler-aws` artifact (open the `.html`), and Security -> Code scanning.

## Cost check modes

```bash
scripts/cost-check.sh quick        # spend by service + leftovers (ALBs, NAT, EIPs, EBS, snapshots, log retention, ECR, ECS)
scripts/cost-check.sh thrifty      # Steampipe "AWS Thrifty" benchmarks -> reports/thrifty-*.html
scripts/cost-check.sh thrifty-ui   # same, as a dashboard at http://localhost:9033
scripts/cost-check.sh infracost    # monthly estimate per Terraform folder
SKIP_CE=1 scripts/cost-check.sh    # skip Cost Explorer ($0.01 per query)
```

Thrifty needs `brew install turbot/tap/steampipe turbot/tap/powerpipe`. Infracost needs `brew install infracost && infracost auth login` (free).
