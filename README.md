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

## Repo layout

```
backend/    .NET 8 minimal API - /health and /api/locations endpoints
frontend/   Angular 17 app - fetches from the backend, runtime-configurable API URL
infra/      Terraform - ECR, ECS cluster/services/tasks, ALB, IAM (incl. GitHub OIDC role)
.github/workflows/deploy.yml   CI/CD pipeline
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

## Notes / talking points for interviews

- Uses the account's **default VPC** to keep the demo runnable in ~15 minutes; a real
  production setup would use a dedicated VPC with private subnets for the tasks and a
  NAT gateway, or no NAT at all if using VPC endpoints for ECR/CloudWatch.
- **GitHub OIDC federation** replaces static AWS access keys in CI - this is the current
  AWS-recommended pattern and a good thing to mention if asked about CI/CD security.
- IAM roles are split into **execution role** (ECS agent: pull image, write logs) vs.
  **task role** (app code: whatever AWS APIs your app itself needs) - a common point of
  confusion worth being able to explain clearly.
- The frontend injects its backend URL **at container runtime** (via `env.js` + envsubst)
  rather than baking it in at build time, so the same image can be promoted across
  environments without a rebuild - a real cloud-native pattern, not just a demo shortcut.
