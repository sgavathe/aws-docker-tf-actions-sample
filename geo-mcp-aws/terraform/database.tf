# Aurora PostgreSQL Serverless v2 with PostGIS, pausing to 0 ACU when idle (storage-only billing).
#
# Passwords: the admin password is created and rotated by RDS in Secrets Manager
# (manage_master_user_password) and never appears in Terraform state or the repo. It is used
# only by scripts/db-bootstrap.py through the RDS Data API. The app roles (mcp_reader,
# mcp_writer, crash_loader) log in with IAM authentication tokens: no password exists.

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_rds_cluster" "main" {
  depends_on = [aws_cloudwatch_log_group.db]

  cluster_identifier = "${local.name}-db"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.aurora_engine_version
  database_name      = "geo"
  master_username    = "geo_admin"

  manage_master_user_password         = true # RDS-owned secret in Secrets Manager, rotated by RDS
  iam_database_authentication_enabled = true
  enable_http_endpoint                = true # RDS Data API: bootstrap runs from outside the VPC, no tunnel

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  storage_encrypted      = true
  deletion_protection    = var.db_deletion_protection
  skip_final_snapshot    = !var.db_deletion_protection
  copy_tags_to_snapshot  = true
  apply_immediately      = true

  enabled_cloudwatch_logs_exports = ["postgresql"] # errors and connection events (log group below, 7-day retention)

  backup_retention_period      = 1 # free up to the cluster's storage size
  preferred_backup_window      = "07:00-08:00"
  preferred_maintenance_window = "sun:08:00-sun:09:00"

  serverlessv2_scaling_configuration {
    min_capacity             = 0
    max_capacity             = var.aurora_max_acu
    seconds_until_auto_pause = var.aurora_pause_after_seconds
  }
}

resource "aws_rds_cluster_instance" "main" {
  identifier                 = "${local.name}-db-1"
  cluster_identifier         = aws_rds_cluster.main.id
  instance_class             = "db.serverless"
  engine                     = aws_rds_cluster.main.engine
  engine_version             = aws_rds_cluster.main.engine_version
  publicly_accessible        = false
  auto_minor_version_upgrade = true
  db_subnet_group_name       = aws_db_subnet_group.main.name

  performance_insights_enabled          = true
  performance_insights_retention_period = 7 # free tier
}

# Created up front so RDS doesn't create it with "never expire".
resource "aws_cloudwatch_log_group" "db" {
  name              = "/aws/rds/cluster/${local.name}-db/postgresql"
  retention_in_days = var.log_retention_days
}

locals {
  db_host = aws_rds_cluster.main.endpoint
  # IAM auth: rds-db:connect is granted per database user name
  dbuser_arn = "arn:${data.aws_partition.current.partition}:rds-db:${local.region}:${local.account}:dbuser:${aws_rds_cluster.main.cluster_resource_id}"
}
