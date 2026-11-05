# Plan-time tests of the worst-case cost check. Run with `terraform test` after `terraform init`. The AWS provider is mocked:
# no credentials are read and nothing is created.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
}

variables {
  github_org = "example-org"
  github_app = {
    id                                = "123456"
    key_base64_ssm_parameter_name     = "/check-tree/github-app/key_base64"
    webhook_secret_ssm_parameter_name = "/check-tree/github-app/webhook_secret"
  }
  budget_alert_email = "owner@example.com"
}

run "defaults_are_under_the_default_budget" {
  command = plan

  assert {
    condition     = output.worst_case_monthly_usd <= 700 && output.worst_case_monthly_usd > 600
    error_message = "the two default classes should price at about USD 651 a month in the worst case"
  }
  assert {
    condition     = sum(values(output.ceilings)) <= 6
    error_message = "the default classes must hold at most 6 machines in total"
  }
}

run "a_class_over_the_budget_fails_the_plan" {
  command = plan

  variables {
    monthly_budget_usd = 500
  }

  expect_failures = [terraform_data.budget_guard]
}

run "raising_a_ceiling_past_the_budget_fails_the_plan" {
  command = plan

  variables {
    classes = {
      "ci-small" = {
        instance_types = ["m7i.large", "m6in.large"]
        max_count      = 20
      }
    }
  }

  expect_failures = [terraform_data.budget_guard]
}

run "a_type_with_no_price_fails_the_plan" {
  command = plan

  variables {
    classes = {
      "ci-odd" = {
        instance_types = ["m99.mega"]
        max_count      = 1
      }
    }
  }

  expect_failures = [terraform_data.budget_guard]
}

run "all_ceilings_zero_is_the_kill_switch_and_costs_nothing" {
  command = plan

  variables {
    classes = {
      "ci-small" = {
        instance_types = ["m7i.large", "m6in.large"]
        max_count      = 0
      }
      "ci-medium" = {
        instance_types = ["m7i.xlarge", "m6in.xlarge"]
        max_count      = 0
      }
    }
  }

  assert {
    condition     = output.worst_case_monthly_usd == 0
    error_message = "with every ceiling at 0 the worst case is 0"
  }
}
