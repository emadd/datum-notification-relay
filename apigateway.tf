# HTTP API (cheaper + simpler than a REST API for a Lambda-proxy-only
# surface). No authorizer -- see handlers/jobs_api.py's module docstring for
# why "no auth beyond supportID+deviceToken" is the deliberate model here.

resource "aws_apigatewayv2_api" "jobs" {
  name          = "datum-relay-jobs-api-${var.environment}"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.jobs.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      integrationErr = "$context.integrationErrorMessage"
      responseLength = "$context.responseLength"
    })
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/datum-relay-jobs-api-${var.environment}"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_integration" "jobs_api" {
  api_id                 = aws_apigatewayv2_api.jobs.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.jobs_api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "put_job" {
  api_id    = aws_apigatewayv2_api.jobs.id
  route_key = "PUT /jobs/{jobId}"
  target    = "integrations/${aws_apigatewayv2_integration.jobs_api.id}"
}

resource "aws_apigatewayv2_route" "get_job" {
  api_id    = aws_apigatewayv2_api.jobs.id
  route_key = "GET /jobs/{jobId}"
  target    = "integrations/${aws_apigatewayv2_integration.jobs_api.id}"
}

resource "aws_apigatewayv2_route" "delete_job" {
  api_id    = aws_apigatewayv2_api.jobs.id
  route_key = "DELETE /jobs/{jobId}"
  target    = "integrations/${aws_apigatewayv2_integration.jobs_api.id}"
}

resource "aws_apigatewayv2_route" "list_jobs_for_device" {
  api_id    = aws_apigatewayv2_api.jobs.id
  route_key = "GET /devices/{supportID}/{deviceToken}/jobs"
  target    = "integrations/${aws_apigatewayv2_integration.jobs_api.id}"
}

resource "aws_lambda_permission" "api_gateway_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.jobs_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.jobs.execution_arn}/*/*"
}
