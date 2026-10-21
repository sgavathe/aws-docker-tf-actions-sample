# Geo MCP analyst chat on AWS

The air-gapped geospatial MCP demo (chat + MCP tools + PostGIS + crash analysis), deployed
serverless at **https://mcp.spatialenable.com**. It costs a few dollars a month when idle, and
no password or API key is stored anywhere.

```
browser ── CloudFront (mcp.spatialenable.com) ─┬─ /*        -> Lambda chat  (Function URL, IAM via OAC, streams NDJSON)
                                               └─ /vendor/* -> S3 (MapLibre libraries)

chat Lambda (no VPC) ── Amazon Bedrock (Converse + tool use, IAM)        model: var.bedrock_model_id
                     ── Cognito (sign-in; groups geo-reader / geo-writer)
                     ── DynamoDB (sessions, TTL)       ── S3 maps/ (served only to signed-in users)
                     ── geo-mcp Function URL: SigV4 (IAM) + the user's Cognito token

geo-mcp Lambda (private subnets, NO internet) ── Aurora PostgreSQL Serverless v2 + PostGIS (IAM auth, pauses at 0 ACU)
                                              └─ S3 gateway endpoint (free; this bucket only)
jobs Lambda (private subnets) ── crash loader: s3://…/import/ -> Aurora
```

## Enclave vs AWS: same code, switched by environment

| | Enclave (docker compose) | AWS (this folder) |
|---|---|---|
| Model | Ollama `qwen2.5:14b`, local | Bedrock (`LLM_PROVIDER=bedrock`); default Amazon Nova Lite (`us.amazon.nova-lite-v1:0`), US-only routing |
| Sign-in | Keycloak | Cognito user pool, admin-created users only (`AUTH_PROVIDER=cognito`) |
| Database | PostGIS container, password roles | Aurora + PostGIS, IAM-token roles (`DB_IAM_AUTH=1`), TLS verify-full |
| MCP server | long-running container | Lambda, stateless HTTP (`MCP_STATELESS=1`) |
| Maps / exports | nginx map viewer | private S3, served by the chat app to signed-in users only |
| ArcGIS Pro | Pro MCP server on Windows | not deployed: exports come back as GeoJSON **download links** |

The app code in `app/` is the same as the enclave repo's, with these switches added.
The tool behavior, roles, SQL guard and audit log are unchanged.

## Security

- **No stored credentials:**
  - **GitHub → AWS:** OIDC role, no access keys.
  - **Aurora admin password:** created and rotated by RDS in Secrets Manager (`manage_master_user_password`). It never appears in Terraform state; only `scripts/db-bootstrap.py` uses it, through the RDS Data API by ARN.
  - **App database users** (`mcp_reader`, `mcp_writer`, `crash_loader`): IAM authentication, so no password exists at all.
  - **Bedrock:** IAM permission on one model.
  - **Cognito:** a public client, so no client secret.
  - **Chat users' passwords:** typed into `scripts/users.sh` and sent to Cognito on stdin; never stored.
- **Network:**
  - The database, the MCP server and the loader run in a VPC with no internet gateway or NAT. Their only way out is an S3 gateway endpoint whose policy allows only this stack's bucket.
  - Both function URLs are `AWS_IAM`. Only CloudFront can call the chat function, and only the chat function's role can call geo-mcp.
- **Two layers on every tool call:**
  - The IAM signature proves the call comes from the chat Lambda.
  - The user's Cognito access token (signature, issuer, `token_use=access`, `client_id`, expiry) sets their role.
  - Read-only users can't make maps or write layers; that is checked in the MCP server, not the UI.
- **Data access:**
  - `mcp_reader` is read-only with a 60-second query timeout.
  - SQL from the model goes through the same guard as in the enclave.
  - Crash data is owned by the loader role.
- **Browser:**
  - The session cookie is opaque, HttpOnly, Secure and SameSite=Strict.
  - The session table is keyed by a SHA-256 of the cookie, so the table never holds a usable cookie.
  - Every POST must be JSON (CSRF protection), with strict CSP and HSTS.
- **Checks in CI:**
  - Checkov runs on this Terraform; accepted cost trade-offs are written down in `/.checkov.yaml`.
  - Unit tests cover Cognito, SigV4, S3, sessions, Bedrock and the SQL guard.
  - Trivy scans the images before deploy (pinned and checksum-verified).
  - gitleaks and the sensitive-file check run on every push.

## Cost (us-east-1, light demo use)

| Item | Monthly |
|---|---|
| Aurora Serverless v2: storage (~2 GB) + active hours at 0.5–2 ACU, paused otherwise | ~$1–3 |
| RDS-managed admin secret (the only Secrets Manager secret) | $0.40 |
| Lambda, CloudFront, S3, DynamoDB on-demand, Cognito (≤10k MAU), CloudWatch (7-day logs) | ~$0 (free tiers) |
| Bedrock | per token; cents per demo session |
| Route 53 zone (already exists for map.spatialenable.com) | $0 extra |

There is no NAT gateway, ALB, interface endpoint, public IPv4 address or always-on compute.
Set `BUDGET_EMAIL` to get a budget alarm (default $10).

Aurora pauses after 10 idle minutes. The first question after a pause waits about 15 seconds
(30+ seconds after a day idle) while it resumes; the chat shows progress meanwhile.

## Deploy (first time, admin credentials)

Prerequisites: terraform ≥ 1.5, Docker with buildx, Node 20+, Python 3 with boto3, and the AWS CLI.

1. **Check the model answers.** In the Bedrock console, open Playground, pick Nova Lite and send "hi". For another model, deploy with
   `TF_VAR_bedrock_model_id=us.amazon.nova-pro-v1:0 TF_VAR_bedrock_foundation_model=amazon.nova-pro-v1:0`
   (Anthropic models may be restricted on newer accounts until AWS approves them).
2. **Deploy.** This takes about 15 minutes the first time; Aurora is most of it.
   ```bash
   BUDGET_EMAIL=you@example.com ./geo-mcp-aws/scripts/deploy.sh
   ```
3. **Create users.** There is no self sign-up.
   ```bash
   ./geo-mcp-aws/scripts/users.sh analyst writer     # can make maps and exports
   ./geo-mcp-aws/scripts/users.sh viewer reader      # read-only tools
   ```
4. **Crash data syncs itself daily** from VDOT's public ArcGIS layer (`terraform/ingest.tf`, 05:17 ET):
   the ingest Lambda (outside the VPC) pages all ~1.16M records by OBJECTID into S3 parts, then one
   load job upserts them into Aurora. It does nothing when VDOT hasn't edited the layer. Run it now:
   ```bash
   aws lambda invoke --function-name geo-devops-demo-geomcp-ingest --invocation-type Event \
     --cli-binary-format raw-in-base64-out --payload '{"force":true}' /dev/null
   aws logs tail /aws/lambda/geo-devops-demo-geomcp-ingest --follow
   ```
   The synced layer includes street names (`ROUTE_OR_STREET_NM`), so `find_intersection` accepts
   "Middle Road" and "Prince George Drive" as well as route numbers. Records VDOT deletes upstream
   are not deleted here (upsert only).

   **Or load a file once (optional, public VDOT data only).**
   ```bash
   ./geo-mcp-aws/scripts/load-crashes.sh ~/Downloads/CrashData_Basic.geojson
   ```
   For 1M+ rows, re-apply with `-var aurora_max_acu=4` first. Set it back afterwards.
5. **Turn on CI deploys.** In GitHub, add the secret `AWS_GEO_MCP_ROLE_ARN` and the variables `GEO_MCP_URL` and `GEO_MCP_ENABLED=true` (`deploy.sh` prints the values).

After that, pushes to `main` that touch `geo-mcp-aws/app/**` test, build, scan and deploy new
code. The CI role can't change infrastructure or IAM; for those, re-run `deploy.sh` as an admin.

To serve on `*.cloudfront.net` instead of the domain: `ATTACH_DOMAIN=false ./geo-mcp-aws/scripts/deploy.sh`.

## Files

```
geo-mcp-aws/
  app/mcp-server/     geo_mcp package (MCP tools, crash analysis, loader) + tests
  app/agent/          agent loop + chat app + static UI + tests
  app/db/             synthetic infrastructure schema + seed
  lambda/             geo-mcp, chat and jobs Dockerfiles (Lambda Web Adapter 0.9.1)
  terraform/          own state key: geo-devops-demo/geo-mcp.tfstate
    network.tf        private VPC, S3 gateway endpoint, security groups
    database.tf       Aurora Serverless v2 (min 0 ACU), IAM auth, Data API
    auth.tf           Cognito pool, groups, public client, JWKS for the MCP Lambda
    lambdas.tf        ECR, 3 functions, least-privilege roles, function URLs
    site.tf           CloudFront + OACs, optional mcp.spatialenable.com
    storage.tf        private bucket (maps/, import/, vendor/), DynamoDB sessions
    ops.tf            budget alarm, GitHub OIDC deploy role (code-only)
  scripts/            deploy.sh, build-images.sh, sync-vendor.sh, db-bootstrap.py, users.sh, load-crashes.sh
.github/workflows/deploy-geo-mcp.yml
```

## Tear down

```bash
terraform -chdir=geo-mcp-aws/terraform destroy
```
This removes everything in this stack, including the database. Nothing outside this state key is
touched.
