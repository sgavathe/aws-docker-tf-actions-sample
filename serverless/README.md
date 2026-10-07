# Low-cost serverless mode

The same app (`backend/` .NET API + `frontend/` React/Vite + ArcGIS) with no always-on infrastructure:

```
map.spatialenable.com ──> CloudFront ─┬─ /*               -> S3 (private, Origin Access Control)
                                      └─ /api/*, /health  -> Lambda Function URL (IAM auth via OAC)
                                                             .NET 8 API via AWS Lambda Web Adapter
                                                                  │ reads (cached, ETag-checked every 15 min)
                                                                  ▼
            ci-graph.yml (weekly) ──> s3://<site bucket>/data/ci-graph.json   (Grid Cascade data)
```

| | ECS stack (`infra/`) | Serverless (`serverless/`) |
|---|---|---|
| Compute | 2 Fargate tasks, 24/7 | Lambda, runs only per request |
| Entry point | ALB + 2 public IPv4s | CloudFront (no IPv4 charges) |
| Idle cost | ~$1.60/day | ~$0 |
| Typical month | ~$50 | **under $1** (free tiers: Lambda 1M req, CloudFront 1 TB / 10M req) |
| Trade-off | Always warm | ~2–4 s cold start on the first API call after idle |

**No application code changes.** `serverless/lambda/Dockerfile` builds the same `backend/` source and adds the
[AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter) as a Lambda extension, which forwards
each invocation to the normal ASP.NET Core app on port 8080. Same paths as the ALB rules, so the frontend
doesn't change either.

## Layout

```
serverless/
  lambda/Dockerfile        Lambda image for backend/ (adds the Web Adapter)
  terraform/               Own state key: geo-devops-demo/serverless.tfstate
    main.tf  variables.tf  outputs.tf
    api.tf                 ECR (keeps last 5 images), Lambda, Function URL
    site.tf                S3, CloudFront, OACs, SPA-rewrite function
    dns.tf                 ACM cert + A/AAAA alias records (toggle: attach_domain)
    iam.tf                 GitHub OIDC deploy role (reuses existing OIDC provider)
                           (api.tf also grants the Lambda read access to data/ only)
  scripts/
    deploy.sh              build + push image, terraform apply, upload frontend, smoke test
    park-ecs.sh            remove only the billable parts of the ECS stack
.github/workflows/deploy-serverless.yml
.github/workflows/ci-graph.yml      OSM extract -> pipeline/ -> s3://<site bucket>/data/ci-graph.json
```

## Grid Cascade data

The API's `/api/infrastructure/*` endpoints read the dependency graph from the site bucket
(`CiGraph__S3Bucket` / `CiGraph__S3Key` on the Lambda). Until something is published there, they serve
the synthetic sample bundled in the image, so the page always works.

To publish real OpenStreetMap data, run **Infrastructure graph (OpenStreetMap)** from the Actions tab
(defaults: Virginia extract, 70 miles around Richmond). It also runs weekly once `DEPLOY_TARGET` is
`serverless`. The Lambda notices the new file within 15 minutes; no redeploy. `deploy.sh` leaves
`data/` alone when it syncs the frontend.

## Switching from ECS to serverless (one time)

Run these locally (laptop or CloudShell) with admin credentials. The first create needs permissions
the CI role intentionally doesn't have. You need terraform, docker and node 20+.

```bash
# 1. Stand it up next to ECS on the *.cloudfront.net URL. Nothing live changes.
ATTACH_DOMAIN=false ./serverless/scripts/deploy.sh
#    -> open the printed cloudfront URL and check the map, hotspots, weather.

# 2. Park ECS: removes the ALB, Fargate tasks, their public IPs and the DNS record.
./serverless/scripts/park-ecs.sh

# 3. Point map.spatialenable.com at CloudFront (cert + DNS; CloudFront update takes ~5-10 min).
./serverless/scripts/deploy.sh
```

There are a few minutes of downtime between steps 2 and 3. Then in GitHub
(Settings → Secrets and variables → Actions):

- **Secret** `AWS_SERVERLESS_ROLE_ARN`: the `github_deploy_role_arn` value the script prints
- **Variable** `DEPLOY_TARGET` = `serverless`

From then on, every push to `main` runs **Deploy (serverless, low-cost)**, and **Build and Deploy** (ECS)
skips on push.

## Switching back to ECS (e.g. before an interview)

```bash
ATTACH_DOMAIN=false ./serverless/scripts/deploy.sh   # serverless releases map.spatialenable.com
```

Then delete or change the `DEPLOY_TARGET` variable and run **Build and Deploy** manually from the Actions tab.
It recreates the ALB, services and DNS record from the unchanged `infra/` code. The serverless stack
can stay up on its cloudfront.net URL, since it costs nothing while idle.

## Notes

- **Shared ACM validation record.** ACM uses the same validation CNAME for `map.spatialenable.com`
  in both stacks, so `dns.tf` uses `allow_overwrite`. If either stack is ever fully destroyed and
  removes that record, re-apply the other one so its cert keeps auto-renewing.
- **CI role scope.** `github-actions-serverless-deploy-role` can update this stack (new image, new
  frontend, config tweaks) but not create new resource types. If a later change adds one, apply it
  locally once, or add the action to `iam.tf`.
- **Lambda image format.** If Lambda reports an unsupported image manifest, the build pushed an
  attestation/index. Keep `--provenance=false --sbom=false` in `deploy.sh`.
- **POST bodies.** CloudFront OAC to Lambda signs requests, and Lambda only accepts a signed POST/PUT
  body when the viewer sends its SHA-256 in `x-amz-content-sha256`. `frontend/src/api.js` (`postJson`)
  adds it for `POST /api/infrastructure/impact`; any new POST endpoint should go through the same helper.
- **Cost guard.** There's no reserved concurrency (accounts with a concurrency quota of 10 reject it).
  An AWS Budget alert covers the unlikely case of abusive traffic.
