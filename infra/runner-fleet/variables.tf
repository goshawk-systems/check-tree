variable "github_org" {
  description = "The GitHub organisation the runners serve. The GitHub App (README step 1) is created in and installed on this organisation."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,38}$", var.github_org))
    error_message = "github_org must be a GitHub organisation (or user) name: letters, digits and hyphens."
  }
}

variable "github_app" {
  description = <<-EOT
    The GitHub App that registers runners and receives job webhooks. Nothing secret is ever a value here: the App's private
    key (base64 of the .pem) and its webhook secret live in SSM Parameter Store as SecureString parameters you create by hand
    (README step 2), and this variable holds only their NAMES. The App id is not a secret.
  EOT
  type = object({
    id                                = string
    key_base64_ssm_parameter_name     = string
    webhook_secret_ssm_parameter_name = string
  })

  validation {
    condition     = can(regex("^[0-9]+$", var.github_app.id))
    error_message = "github_app.id must be the App's numeric id (the 'App ID' on the App's settings page)."
  }
  validation {
    condition     = startswith(var.github_app.key_base64_ssm_parameter_name, "/") && startswith(var.github_app.webhook_secret_ssm_parameter_name, "/")
    error_message = "The two SSM parameter names must start with a slash, for example /check-tree/github-app/key_base64. Pass the NAME, never the secret value."
  }
}

variable "aws_region" {
  description = "AWS region for the whole fleet. The committed price table is us-east-1; another region is allowed but its prices are not in the table, so check them (README)."
  type        = string
  default     = "us-east-1"
}

variable "prefix" {
  description = "Prefix for every resource the fleet creates. Lower case, at most 16 characters (the module builds IAM role and Lambda names from it plus the class name)."
  type        = string
  default     = "check-tree-ci"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.prefix))
    error_message = "prefix must be 1 to 16 characters: a lower-case letter, then lower-case letters, digits or hyphens."
  }
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "An existing VPC to launch runners into. Leave null and the fleet creates its own small VPC (a public subnet with one fck-nat instance, and private subnets for the runners). When set, also set private_subnet_ids: the fleet creates no network of its own then, and those subnets need a route to the internet (a NAT) so a runner can reach GitHub."
  type        = string
  default     = null
}

variable "private_subnet_ids" {
  description = "Required when vpc_id is set: the existing private subnets runners launch into, at most one per availability zone (two subnets in one zone make EC2 refuse every launch with 'duplicate instance pools'). Ignored when the fleet creates its own VPC."
  type        = list(string)
  default     = []
}

variable "vpc_cidr" {
  description = "CIDR of the VPC the fleet creates when vpc_id is null."
  type        = string
  default     = "10.77.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR."
  }
}

variable "classes" {
  description = <<-EOT
    The runner classes, keyed by class name. Each class is one label a workflow asks for in `runs-on:` and one HARD CEILING.
      instance_types  Equivalent types (same vCPU and memory), more than one so spot has several pools to draw from.
                      Every type must be in prices.tf, because the worst-case cost check prices the dearest one.
      max_count       The most machines the class may hold at once. Required. 0 launches nothing (the kill switch for the class).
      capacity        "spot" (default: spot first, on-demand only when spot is refused, still under max_count) or "on-demand".
      labels          Extra runner labels; defaults to the class name. A workflow uses runs-on: [self-hosted, linux, x64, <label>].
      boot_time_minutes  How long a new runner may take to register before it is counted as failed.
    A job past the ceiling waits for a machine; it does not fail. The plan fails if the classes' worst-case monthly cost is
    over monthly_budget_usd (main.tf).
  EOT
  type = map(object({
    instance_types    = list(string)
    max_count         = number
    capacity          = optional(string, "spot")
    labels            = optional(list(string), [])
    boot_time_minutes = optional(number, 6)
  }))

  default = {
    # 2 vCPU / 8 GiB. 4 machines at most.
    "ci-small" = {
      instance_types = ["m7i.large", "m7i-flex.large", "m6i.large", "m5.large"]
      max_count      = 4
      capacity       = "spot"
    }
    # 4 vCPU / 16 GiB. 2 machines at most.
    "ci-medium" = {
      instance_types = ["m7i.xlarge", "m7i-flex.xlarge", "m6i.xlarge", "m5.xlarge"]
      max_count      = 2
      capacity       = "spot"
    }
  }

  validation {
    condition     = length(var.classes) > 0
    error_message = "classes must name at least one runner class."
  }
  validation {
    condition     = alltrue([for name, c in var.classes : can(regex("^[a-z][a-z0-9-]{0,23}$", name))])
    error_message = "A class name is 1 to 24 characters: a lower-case letter, then lower-case letters, digits or hyphens."
  }
  validation {
    condition     = alltrue([for c in values(var.classes) : c.max_count >= 0 && c.max_count == floor(c.max_count)])
    error_message = "Every class's max_count must be a whole number of machines, zero or more."
  }
  validation {
    condition     = alltrue([for c in values(var.classes) : length(c.instance_types) > 0])
    error_message = "Every class needs at least one instance type."
  }
  validation {
    condition     = alltrue([for c in values(var.classes) : contains(["spot", "on-demand"], c.capacity)])
    error_message = "A class's capacity must be \"spot\" or \"on-demand\"."
  }
}

variable "monthly_budget_usd" {
  description = "The most the fleet's machines may cost in a month. Two things use it: the plan FAILS if the classes' worst case (every machine of every class busy all month at the dearest listed type's on-demand price) is over it, and an AWS Budget alarms at 80% and 100% of it."
  type        = number
  default     = 700

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be more than zero."
  }
}

variable "budget_alert_email" {
  description = "The address AWS Budgets emails at 80% and 100% of monthly_budget_usd. AWS sends a confirmation message the first time; the alarm is not live until someone confirms it."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be an email address."
  }
}

variable "budget_cost_filter_tag" {
  description = "Optional. A user-defined cost-allocation tag, as \"Key$Value\" (for example \"Fleet$check-tree-ci\"), to restrict the budget to the fleet. The tag must be activated in Billing > Cost allocation tags first (it takes up to a day). Null (default) budgets the whole AWS account, which is right for an account the fleet has to itself."
  type        = string
  default     = null
}

variable "create_spot_service_linked_role" {
  description = "Create the AWSServiceRoleForEC2Spot service-linked role. True suits an account that has never run spot; set false if the account already has it (the apply fails with 'role already exists' otherwise)."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "How long the fleet's CloudWatch log groups keep logs."
  type        = number
  default     = 30
}
