# Holds the APNs Auth Key's .p8 private key material -- the ONE secret this
# whole repo needs and the one thing Terraform must never be given (see
# NOTIFICATION-SERVER-INFRA.md §8; the .p8 is held privately by the app
# owner, outside every repo, downloaded once from Apple).
#
# Terraform only creates the empty secret container. The value itself is
# populated out-of-band, after `terraform apply`, e.g.:
#
#   aws secretsmanager put-secret-value \
#     --profile datum \
#     --secret-id datum-relay-apns-key-dev \
#     --secret-string '{"privateKeyPem":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"}'
#
# `lifecycle.ignore_changes` on the (nonexistent, here) secret_string means a
# later `terraform apply` can never accidentally revert or wipe a value set
# this way.

resource "aws_secretsmanager_secret" "apns_key" {
  name        = "datum-relay-apns-key-${var.environment}"
  description = "APNs Auth Key (.p8) private key material, JSON: {\"privateKeyPem\": \"...\"}. Populated out-of-band -- see secrets.tf."

  tags = {
    Project     = "datum-notification-relay"
    Environment = var.environment
  }
}
