# One-off database jobs (crash loader) as a plain Lambda handler in the private subnets.
# Build context: geo-mcp-aws/app/mcp-server
FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt requirements-aws.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-aws.txt --target "${LAMBDA_TASK_ROOT}" \
 && rm requirements.txt requirements-aws.txt
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /opt/rds-ca.pem
COPY geo_mcp ${LAMBDA_TASK_ROOT}/geo_mcp
ENV DB_SSLROOTCERT=/opt/rds-ca.pem PYTHONUNBUFFERED=1
CMD ["geo_mcp.lambda_jobs.handler"]
