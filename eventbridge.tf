# Periodic trigger for the due-job runner. A simple fixed-interval rule
# rather than one EventBridge Scheduler schedule per job -- see
# run_due_jobs.py's module docstring and store.py's gsi-due note for the
# tradeoffs (documented v1 scaling limit, not a blocker at expected volume).

resource "aws_cloudwatch_event_rule" "run_due_jobs" {
  name                = "datum-relay-run-due-jobs-${var.environment}"
  description         = "Invokes run_due_jobs every ${var.due_job_check_interval_minutes} minutes."
  schedule_expression = "rate(${var.due_job_check_interval_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "run_due_jobs" {
  rule = aws_cloudwatch_event_rule.run_due_jobs.name
  arn  = aws_lambda_function.run_due_jobs.arn
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.run_due_jobs.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.run_due_jobs.arn
}
