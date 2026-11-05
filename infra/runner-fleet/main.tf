# Ephemeral, autoscaling, scale-to-zero self-hosted GitHub Actions runners for one GitHub organisation, with a HARD CEILING
# per runner class and a plan-time cost check, from the first apply.
#
# Module: github-aws-runners/terraform-aws-github-runner, release v7.11.0, its `modules/multi-runner`. A plain
# `terraform init` does not fetch the three Lambda zips that release ships as assets: run ./download-lambdas.sh once first
# (it checks each file's sha256).

data "aws_caller_identity" "current" {}

locals {
  ssm_arn_prefix = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter"

  # ---- the plan-time cost check (see terraform_data.budget_guard below) ----
  # Worst case per class = max_count x the on-demand price of the DEAREST type in the class's list x 730 hours. It is the only
  # guaranteed bound: a spot price can rise to the on-demand price, and a refused spot launch fails over to on-demand.
  # A type missing from the price table is priced at 0 here only so this local can be computed; the guard below then fails
  # the plan naming the type, so a missing price never makes the total look smaller than it is.
  class_dearest_hourly_usd = {
    for name, c in var.classes : name => max([for t in c.instance_types : lookup(local.on_demand_hourly_usd, t, 0)]...)
  }
  class_worst_case_monthly_usd = {
    for name, c in var.classes : name => c.max_count * local.class_dearest_hourly_usd[name] * local.hours_per_month
  }
  worst_case_monthly_usd = sum(values(local.class_worst_case_monthly_usd))
  unpriced_instance_types = distinct(flatten([
    for name, c in var.classes : [for t in c.instance_types : t if !contains(keys(local.on_demand_hourly_usd), t)]
  ]))
  worst_case_breakdown = join(", ", [for name in sort(keys(var.classes)) : "${name}: ${var.classes[name].max_count} x ${local.class_dearest_hourly_usd[name]}/h x ${local.hours_per_month} h = USD ${format("%.2f", local.class_worst_case_monthly_usd[name])}"])

  # One ephemeral-runner shape for every class: one job, then the machine terminates (GitHub's own hardening advice).
  common_runner_config = {
    enable_ephemeral_runners    = true
    enable_organization_runners = true
    runner_os                   = "linux"
    runner_architecture         = "x64"
    vpc_id                      = local.vpc_id
    subnet_ids                  = local.runner_subnet_ids
    enable_ssm_on_runners       = true # Session Manager to look inside a stuck runner: no inbound SSH, no bastion
    delay_webhook_event         = 0    # that delay only lets an already-idle runner take the job first; ephemeral runners are never idle

    # THE CEILING IS EXACT. Each scale-up invocation counts the class's pending and running machines and launches at most
    # `max_count - current`. Two invocations at once could each see room, so launches are serialised: one scale-up at a time
    # per class. For a small fleet this costs nothing noticeable; murmur-platform's fleet raised it to 10 for burst speed and
    # accepted "up to one batch over the ceiling" as the price.
    scale_up_reserved_concurrent_executions = 1

    # IMDSv2 only, one hop: this is the module's own default, stated so an edit cannot loosen it without showing in a diff.
    runner_metadata_options = {
      instance_metadata_tags      = "enabled"
      http_endpoint               = "enabled"
      http_tokens                 = "required"
      http_put_response_hop_limit = 1
    }

    # Errors a launch can hit that mean "not now, retry from the queue" rather than "drop the job". This list REPLACES the
    # module's default; VcpuLimitExceeded is added so a launch that meets the account's vCPU quota waits instead of being lost.
    scale_errors = [
      "UnfulfillableCapacity",
      "MaxSpotInstanceCountExceeded",
      "TargetCapacityLimitExceededException",
      "RequestLimitExceeded",
      "ResourceLimitExceeded",
      "MaxSpotFleetRequestCountExceeded",
      "InsufficientInstanceCapacity",
      "InsufficientCapacityOnHost",
      "VcpuLimitExceeded",
    ]
  }
}

# Fails the plan, never silently, when the classes could cost more in a month than the budget, or name a type with no price.
resource "terraform_data" "budget_guard" {
  input = local.worst_case_monthly_usd

  lifecycle {
    precondition {
      condition     = length(local.unpriced_instance_types) == 0
      error_message = "These instance types are in a class but not in prices.tf, so the worst-case cost cannot be computed: ${join(", ", local.unpriced_instance_types)}. Add each type's us-east-1 on-demand price to prices.tf (README: Refresh the price table), or use a listed type."
    }
    precondition {
      condition     = local.worst_case_monthly_usd <= var.monthly_budget_usd
      error_message = "The runner classes' worst-case monthly cost is USD ${format("%.2f", local.worst_case_monthly_usd)}, over monthly_budget_usd of USD ${format("%.2f", var.monthly_budget_usd)} (every machine of every class busy for ${local.hours_per_month} hours at its dearest listed type's on-demand price, price table dated ${local.price_table_date}). Per class: ${local.worst_case_breakdown}. Lower a class's max_count, drop its dearest instance type, or raise monthly_budget_usd on purpose."
    }
  }
}

module "runners" {
  source  = "github-aws-runners/github-runner/aws//modules/multi-runner"
  version = "7.11.0"

  webhook_lambda_zip                = "${path.module}/lambdas/webhook.zip"
  runners_lambda_zip                = "${path.module}/lambdas/runners.zip"
  runner_binaries_syncer_lambda_zip = "${path.module}/lambdas/runner-binaries-syncer.zip"

  aws_region = var.aws_region
  prefix     = var.prefix
  tags = merge(var.tags, {
    GithubOrg = var.github_org
  })

  logging_retention_in_days = var.log_retention_days

  vpc_id     = local.vpc_id
  subnet_ids = local.runner_subnet_ids

  # The App id is public; the key and the webhook secret are read from SSM at run time and never enter Terraform state.
  github_app = {
    id = var.github_app.id
    key_base64_ssm = {
      arn  = "${local.ssm_arn_prefix}${var.github_app.key_base64_ssm_parameter_name}"
      name = var.github_app.key_base64_ssm_parameter_name
    }
    webhook_secret_ssm = {
      arn  = "${local.ssm_arn_prefix}${var.github_app.webhook_secret_ssm_parameter_name}"
      name = var.github_app.webhook_secret_ssm_parameter_name
    }
  }

  enable_managed_runner_security_group = true
  # Egress is HTTPS and HTTP only (GitHub, package registries); no inbound rule exists.
  runner_egress_rules = [
    {
      cidr_blocks      = ["0.0.0.0/0"]
      ipv6_cidr_blocks = []
      prefix_list_ids  = null
      from_port        = 443
      protocol         = "tcp"
      security_groups  = null
      self             = null
      to_port          = 443
      description      = "HTTPS egress: GitHub API and Actions, package registries"
    },
    {
      cidr_blocks      = ["0.0.0.0/0"]
      ipv6_cidr_blocks = []
      prefix_list_ids  = null
      from_port        = 80
      protocol         = "tcp"
      security_groups  = null
      self             = null
      to_port          = 80
      description      = "HTTP egress: package-registry redirects"
    },
  ]

  eventbridge = {
    enable        = true
    accept_events = ["workflow_job"]
  }

  # One entry per class, built from var.classes: the class's max_count is its `runners_maximum_count`, so a class cannot exist
  # without a ceiling (variables.tf requires max_count, and the budget guard above prices every class).
  multi_runner_config = {
    for name, c in var.classes : name => {
      matcherConfig = {
        labelMatchers = [concat(["self-hosted", "linux", "x64"], coalescelist(c.labels, [name]))]
        exactMatch    = true
      }
      runner_config = merge(local.common_runner_config, {
        instance_types                = c.instance_types
        instance_target_capacity_type = c.capacity
        # Spot: spread over the cheapest pools least likely to be reclaimed. On-demand accepts only lowest-price or prioritized.
        instance_allocation_strategy    = c.capacity == "spot" ? "price-capacity-optimized" : "lowest-price"
        create_service_linked_role_spot = var.create_spot_service_linked_role
        runner_extra_labels             = coalescelist(c.labels, [name])
        runners_maximum_count           = c.max_count
        runner_boot_time_in_minutes     = c.boot_time_minutes
        # Spot stays the default and on-demand is bought ONLY when spot is refused (a spot quota full, or no capacity). The
        # failover is bounded by the same max_count: the scale-up Lambda never launches past it, spot or on-demand.
        enable_on_demand_failover_for_errors = c.capacity == "spot" ? ["MaxSpotInstanceCountExceeded", "InsufficientInstanceCapacity"] : []
      })
    }
  }

  depends_on = [terraform_data.budget_guard, terraform_data.network_guard]
}
