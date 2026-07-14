# App code (both functions share the same `relay` package) and the
# dependency layer. The layer's contents are produced by
# scripts/build_lambda_layer.sh into build/layer/python -- run that before
# `terraform plan`/`apply` (see README).

data "archive_file" "app_code" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/build/app.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

data "archive_file" "layer" {
  type        = "zip"
  source_dir  = "${path.module}/build/layer"
  output_path = "${path.module}/build/layer.zip"
}

resource "aws_lambda_layer_version" "dependencies" {
  layer_name          = "datum-relay-dependencies-${var.environment}"
  filename            = data.archive_file.layer.output_path
  source_code_hash    = data.archive_file.layer.output_base64sha256
  compatible_runtimes = ["python3.12"]
  description         = "pyjwt[crypto] + httpx[http2] -- see requirements-lambda.txt"
}

# --- jobs_api Lambda + its own log group (created explicitly so retention is
# set from day one, rather than defaulting to "never expire").

resource "aws_cloudwatch_log_group" "jobs_api" {
  name              = "/aws/lambda/datum-relay-jobs-api-${var.environment}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "jobs_api" {
  function_name = "datum-relay-jobs-api-${var.environment}"
  role          = aws_iam_role.jobs_api.arn
  handler       = "relay.handlers.jobs_api.handle"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  timeout       = 10
  memory_size   = 128

  filename         = data.archive_file.app_code.output_path
  source_code_hash = data.archive_file.app_code.output_base64sha256
  layers           = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      JOBS_TABLE_NAME = aws_dynamodb_table.jobs.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.jobs_api]
}

# --- run_due_jobs Lambda, invoked on a schedule (see eventbridge.tf).

resource "aws_cloudwatch_log_group" "run_due_jobs" {
  name              = "/aws/lambda/datum-relay-run-due-jobs-${var.environment}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "run_due_jobs" {
  function_name = "datum-relay-run-due-jobs-${var.environment}"
  role          = aws_iam_role.run_due_jobs.arn
  handler       = "relay.handlers.run_due_jobs.handle"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.app_code.output_path
  source_code_hash = data.archive_file.app_code.output_base64sha256
  layers           = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      JOBS_TABLE_NAME  = aws_dynamodb_table.jobs.name
      APNS_SECRET_ARN  = aws_secretsmanager_secret.apns_key.arn
      APNS_TEAM_ID     = var.apns_team_id
      APNS_KEY_ID      = var.apns_key_id
      APNS_BUNDLE_ID   = var.apns_bundle_id
      APNS_USE_SANDBOX = tostring(var.apns_use_sandbox)
    }
  }

  depends_on = [aws_cloudwatch_log_group.run_due_jobs]
}
