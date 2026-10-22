# Chat UI + agent as a Lambda behind CloudFront (outside the VPC: talks to Bedrock, Cognito,
# DynamoDB and the geo-mcp function URL over AWS endpoints). Build context: geo-mcp-aws/app/agent
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
COPY agent.py chat_app.py quota.py ./
COPY static ./static
# response_stream: the NDJSON progress events reach the browser as they happen
ENV HOME=/tmp PYTHONUNBUFFERED=1 PORT=8080 AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz AWS_LWA_INVOKE_MODE=response_stream
USER 10001
CMD ["uvicorn", "chat_app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
