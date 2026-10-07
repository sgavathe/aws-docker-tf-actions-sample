# Lambda image for the SAME backend/ code that runs on ECS - no code changes.
#
# The AWS Lambda Web Adapter runs as a Lambda extension: it starts the normal
# ASP.NET Core app (listening on :8080), waits for /health, then translates each
# Lambda invoke (Function URL event) into a plain HTTP request to the app.
#
# Build context is ./backend:
#   docker buildx build --platform linux/amd64 --provenance=false \
#     -f serverless/lambda/Dockerfile -t <ecr-url>:<tag> backend

# Build stage runs on the BUILD machine's own architecture (e.g. arm64 on Apple Silicon)
# and cross-compiles for the Lambda's architecture. Running the .NET SDK under amd64
# emulation is very slow and often hangs in `dotnet restore`; cross-compiling avoids it.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0 AS build
ARG TARGETARCH
WORKDIR /src
COPY backend.csproj .
RUN RID="linux-$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo x64)" && \
    dotnet restore -r "$RID"
COPY . .
RUN RID="linux-$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo x64)" && \
    dotnet publish -c Release -r "$RID" --self-contained false -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runtime

# Lambda Web Adapter (pinned). Anything in /opt/extensions is started by Lambda.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app
COPY --from=build /app .

# Lambda's filesystem is read-only except /tmp.
ENV HOME=/tmp \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health

# No USER / HEALTHCHECK here: Lambda always runs as its own non-root sandbox
# user and ignores Docker health checks (the adapter's readiness check replaces it).
ENTRYPOINT ["dotnet", "backend.dll"]
