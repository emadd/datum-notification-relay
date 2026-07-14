output "api_endpoint" {
  description = "Base URL of the job registration API."
  value       = aws_apigatewayv2_api.jobs.api_endpoint
}

output "jobs_table_name" {
  value = aws_dynamodb_table.jobs.name
}

output "apns_secret_arn" {
  description = "Populate this secret out-of-band with the APNs .p8 key -- see secrets.tf."
  value       = aws_secretsmanager_secret.apns_key.arn
}
