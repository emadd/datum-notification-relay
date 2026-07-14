# The job store. See src/relay/store.py's module docstring for the full key
# design rationale (in particular the gsi-due single-partition tradeoff).

resource "aws_dynamodb_table" "jobs" {
  name         = "datum-relay-jobs-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  attribute {
    name = "supportID"
    type = "S"
  }

  attribute {
    name = "deviceToken"
    type = "S"
  }

  attribute {
    name = "duePartition"
    type = "S"
  }

  attribute {
    name = "nextDueAt"
    type = "S"
  }

  # Lets the registration API list/update/delete a device's own jobs without
  # a table scan, keyed on the (supportID, deviceToken) identity pair the
  # whole app already uses for anonymous addressing.
  global_secondary_index {
    name            = "gsi-identity"
    hash_key        = "supportID"
    range_key       = "deviceToken"
    projection_type = "ALL"
  }

  # Lets the cron runner Query for due jobs instead of Scan. Single-partition
  # GSI (documented scaling limit -- see store.py).
  global_secondary_index {
    name            = "gsi-due"
    hash_key        = "duePartition"
    range_key       = "nextDueAt"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Project     = "datum-notification-relay"
    Environment = var.environment
  }
}
