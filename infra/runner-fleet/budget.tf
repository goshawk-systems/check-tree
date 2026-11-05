# An AWS Budget on the fleet's monthly spend: an email at 80% and at 100% of monthly_budget_usd (actual spend). The plan-time
# check in main.tf bounds what the CEILINGS can cost; this alarm tells a person when the real bill is getting there anyway
# (NAT instance, storage, logs and Lambda are outside the worst-case figure, so the bill can pass it by a little).

resource "aws_budgets_budget" "fleet" {
  name         = "${var.prefix}-monthly"
  budget_type  = "COST"
  limit_amount = format("%.2f", var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "cost_filter" {
    for_each = var.budget_cost_filter_tag == null ? [] : [var.budget_cost_filter_tag]
    content {
      name   = "TagKeyValue"
      values = ["user:${cost_filter.value}"]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
