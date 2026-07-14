data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- jobs_api: CRUD on the job store only. No Secrets Manager access, no
# outbound-fetch capability -- it never sends a push or fetches a URL, so it
# has no business touching either of those.

resource "aws_iam_role" "jobs_api" {
  name               = "datum-relay-jobs-api-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "jobs_api_permissions" {
  statement {
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.jobs.arn,
      "${aws_dynamodb_table.jobs.arn}/index/*",
    ]
  }

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "jobs_api" {
  name   = "datum-relay-jobs-api-${var.environment}"
  role   = aws_iam_role.jobs_api.id
  policy = data.aws_iam_policy_document.jobs_api_permissions.json
}

# --- run_due_jobs: reads/updates/deletes jobs, reads the APNs secret, and
# (implicitly, via outbound HTTPS from within the Lambda sandbox -- no IAM
# permission needed for that) fetches remoteFetch endpoints.

resource "aws_iam_role" "run_due_jobs" {
  name               = "datum-relay-run-due-jobs-${var.environment}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "run_due_jobs_permissions" {
  statement {
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
    ]
    resources = [
      aws_dynamodb_table.jobs.arn,
      "${aws_dynamodb_table.jobs.arn}/index/*",
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.apns_key.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "run_due_jobs" {
  name   = "datum-relay-run-due-jobs-${var.environment}"
  role   = aws_iam_role.run_due_jobs.id
  policy = data.aws_iam_policy_document.run_due_jobs_permissions.json
}
