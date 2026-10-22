# geo-mcp MCP server as a Lambda (private subnets, no internet). Build context: geo-mcp-aws/app/mcp-server
#   docker buildx build --platform linux/amd64 --provenance=false -f geo-mcp-aws/lambda/geo-mcp.Dockerfile geo-mcp-aws/app/mcp-server
# The AWS Lambda Web Adapter (extension) starts the normal MCP HTTP server on :8080 and turns
# each function-URL invoke into an HTTP request, same pattern as serverless/lambda/Dockerfile.
FROM python:3.12-slim AS build
WORKDIR /w
COPY requirements.txt requirements-aws.txt ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt -r requirements-aws.txt

FROM python:3.12-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels /wheels/* && rm -rf /wheels
# RDS CA bundle so database TLS is verify-full (checked against AWS's CA, not just encrypted)
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /app/rds-ca.pem
COPY geo_mcp ./geo_mcp
ENV HOME=/tmp PYTHONUNBUFFERED=1 PORT=8080 AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PROTOCOL=tcp DB_SSLROOTCERT=/app/rds-ca.pem MAPS_DIR=/tmp/maps
USER 10001
CMD ["python", "-m", "geo_mcp"]
