# check-tree runner fleet: ephemeral, scale-to-zero, HARD-CAPPED self-hosted GitHub Actions runners on AWS.
#
# Module: github-aws-runners/terraform-aws-github-runner, release v7.11.0 (pinned in main.tf). Provenance: this is the shape
# murmur-platform's CI fleet ran on (spot with price-capacity-optimized, ephemeral runners, a private subnet behind a
# fck-nat instance, on-demand failover), made generic, and with the one thing that fleet lacked at the start: a ceiling per
# class from the first commit. That fleet peaked at 454 machines and cost about $5,700 a month before it got one.
#
# No backend is configured here, so state is local until you add one: copy backend.tf.example to backend.tf (README step 6).
# State holds only SSM parameter NAMES, never a secret value.

terraform {
  required_version = ">= 1.9" # cross-variable references in the plan-time budget check (main.tf)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.33" # the multi-runner module's own stated requirement at v7.11.0
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      Component = "check-tree-runner-fleet"
      ManagedBy = "terraform"
      Fleet     = var.prefix
    })
  }
}
