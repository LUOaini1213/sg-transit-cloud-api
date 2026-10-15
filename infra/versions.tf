terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.58.0 added a wait for REST API status AVAILABLE, a field LocalStack
      # 4.14 (the last community image) does not return. Pinned below it.
      version = "6.57.1"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

# Credentials and endpoints come from the environment. For LocalStack, run
# `tflocal` (terraform-local), which writes a temporary provider override that
# points every service at http://localhost:4566. See docs/REAL_AWS.md for a
# real account.
provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Same account and region, but without default_tags. LocalStack 4.14 stores
# alarm tags and then fails to serialize DescribeAlarms (KeyError 'Tags' in
# its serializer), which hangs terraform. Alarms use this provider; on a real
# account they could use the default one.
provider "aws" {
  alias  = "untagged"
  region = var.region
}
