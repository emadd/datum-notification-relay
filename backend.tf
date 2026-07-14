terraform {
  backend "s3" {
    bucket       = "datum-tfstate-947047971987"
    key          = "datum-notification-relay/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # native S3 conditional-write locking (Terraform 1.10+) -- no DynamoDB table needed
  }
}
