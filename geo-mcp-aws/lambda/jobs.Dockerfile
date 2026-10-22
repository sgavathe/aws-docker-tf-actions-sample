# One-off database jobs (crash loader) as a plain Lambda handler in the private subnets.
# Build context: geo-mcp-aws/app/mcp-server
FROM public.ecr.aws/lambda/python:3.12
# OS security fixes released after the base image was built. AL2023 pins packages to the image's
# release, so ask for the latest one. The image's package manager is microdnf (symlinked as dnf).
# aws-lambda-rie is the local test emulator; Lambda never runs it (it provides the runtime API),
# and the bundled copy lags Go security releases. Without it, test locally with a separately
# installed RIE (docker run -v ~/.aws-lambda-rie:/aws-lambda --entrypoint /aws-lambda/aws-lambda-rie ...).
RUN dnf -y --releasever=latest upgrade \
 && dnf clean all && rm -rf /var/cache/dnf \
 && rm -f /usr/local/bin/aws-lambda-rie
COPY requirements.txt requirements-aws.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-aws.txt --target "${LAMBDA_TASK_ROOT}" \
 && rm requirements.txt requirements-aws.txt
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /opt/rds-ca.pem
COPY geo_mcp ${LAMBDA_TASK_ROOT}/geo_mcp
ENV DB_SSLROOTCERT=/opt/rds-ca.pem PYTHONUNBUFFERED=1
CMD ["geo_mcp.lambda_jobs.handler"]
