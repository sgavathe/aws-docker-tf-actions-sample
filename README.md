# Geo DevOps Demo

A minimal end-to-end demo touching the stack from the Senior GIS/AWS Developer/Engineer JD:
Angular frontend, .NET backend, Docker, Terraform, ECS Fargate, IAM, ALB, and a GitHub
Actions pipeline deploying via OIDC (no long-lived AWS keys in GitHub secrets).

## Architecture

```
User -> ALB -> [ / -> frontend service (Angular, nginx) ]
             -> [ /api/*, /health -> backend service (.NET minimal API) ]

Both services run as ECS Fargate tasks in the account's default VPC.
Images live in two ECR repos, built and pushed by GitHub Actions on push to main.
GitHub Actions assumes an IAM role via OIDC federation - no static AWS access keys.
```

### Two deployment modes

| Mode | Code | Cost | Use |
|---|---|---|---|
| **ECS Fargate + ALB** | `infra/`, `.github/workflows/deploy.yml` | ~$50/month | Production-style reference architecture |
| **Serverless** | `serverless/`, `.github/workflows/deploy-serverless.yml` | under $1/month | Day-to-day hosting of the same app, scale to zero |

Same `backend/` and `frontend/` code in both. The repo variable `DEPLOY_TARGET` picks which one deploys on push.
See [serverless/README.md](serverless/README.md) for switching between them.

## Grid Cascade: critical-infrastructure dependencies

The second view (`/#cascade`) answers "what breaks if everything in this area goes down?"
Draw a polygon on the map; the API marks everything inside as down and cuts the power lines
crossing it, then traces the cascade:

- **Power grid:** energy assets that lose every path to a power source go dark. Redundant routes keep the rest live.
- **Services:** facilities lose power, water or comms when all their suppliers are down. Hospitals,
  exchanges and emergency services with backup show as *on backup* instead of failed.
- **How far:** every affected asset gets a hop count and a distance from the drawn area. The cascade
  graph shows sector × hop, with links colored by what they carry.

```
pipeline/build_ci_graph.py   OpenStreetMap extract -> inferred dependency graph (JSON)
.github/workflows/ci-graph.yml   weekly: Geofabrik Virginia -> graph -> S3 (serverless) 
backend/  GET  /api/infrastructure          what data is loaded
          GET  /api/infrastructure/graph    the whole graph (ETag, gzip)
          POST /api/infrastructure/impact   { "area": GeoJSON Polygon } -> failed / on backup / hops / reach
frontend/src/cascade/   ArcGIS SketchViewModel drawing, map layers, SVG cascade graph
```

Out of the box the API serves a **synthetic** Richmond-shaped sample (`backend/Data/ci-graph.sample.json`),
so nothing real is needed to run it. Real data comes from OpenStreetMap; all dependency links are inferred
from location and tags, not utility records. See [pipeline/README.md](pipeline/README.md).

## Repo layout

```
backend/    .NET 8 minimal API - incidents, hotspots (ML.NET), weather, infrastructure impact
frontend/   React + Vite + ArcGIS Maps SDK - Harbor Watch and Grid Cascade views
pipeline/   Python - OpenStreetMap extract -> infrastructure dependency graph
infra/      Terraform - ECR, ECS cluster/services/tasks, ALB, IAM (incl. GitHub OIDC role)
serverless/ Terraform + scripts - S3 + CloudFront + Lambda version of the same app
.github/workflows/   deploy.yml (ECS), deploy-serverless.yml, ci-graph.yml
```

## Running locally (no AWS needed)

```bash
# Backend
cd backend
docker build -t geo-backend .
docker run -p 8080:8080 geo-backend

# Frontend (in another terminal)
cd frontend
docker build -t geo-frontend .
docker run -p 8081:80 -e API_BASE_URL=http://localhost:8080 geo-frontend
```

Then open http://localhost:8081 - it should list three locations fetched from the backend.

## Deploying to your own AWS account

1. **Bootstrap the IAM OIDC role manually first, or via Terraform:**
   - Edit `infra/iam.tf` and replace `YOUR_GITHUB_USERNAME` in the `github_actions` role's
     trust policy with your actual GitHub username/org.
   - From `infra/`, run:
     ```bash
     terraform init
     terraform apply
     ```
     (First apply will fail to build the ECS services since no image exists yet in ECR -
     that's expected. It will still create the ECR repos and IAM role.)

2. **Push initial images manually once, to seed ECR:**
   ```bash
   aws ecr get-login-password --region us-east-1 | \
     docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

   docker build -t <ecr-backend-url>:init ./backend
   docker push <ecr-backend-url>:init
   docker build -t <ecr-frontend-url>:init ./frontend
   docker push <ecr-frontend-url>:init
   ```

3. **Re-run terraform apply** with the `-var backend_image=... -var frontend_image=...`
   flags pointing at those `:init` tags to stand up the ECS services for the first time.

4. **Add repo secrets in GitHub** (Settings -> Secrets and variables -> Actions):
   - `AWS_GITHUB_ACTIONS_ROLE_ARN` - the `github_actions_role_arn` Terraform output.

5. From then on, every push to `main` triggers `.github/workflows/deploy.yml`, which
   builds both images, pushes to ECR, and re-applies Terraform with the new image tags -
   updating the ECS services automatically.