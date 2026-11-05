output "webhook_endpoint" {
  description = "Paste this into the GitHub App's Webhook URL field (infra/README.md, step 5)."
  value       = module.runners.webhook.endpoint
}

output "runner_labels" {
  description = "The self-hosted labels a workflow's runs-on: line can use, one per class."
  value       = keys(module.runners.runners_map)
}

output "ceilings" {
  description = "The hard ceiling (most machines at once) of each class."
  value       = { for name, c in var.classes : name => c.max_count }
}

output "worst_case_monthly_usd" {
  description = "Every class's machines busy all month at the dearest listed type's on-demand price. The plan fails if this is over monthly_budget_usd."
  value       = local.worst_case_monthly_usd
}

output "worst_case_monthly_usd_by_class" {
  value = local.class_worst_case_monthly_usd
}

output "price_table_date" {
  description = "The date of the on-demand price table the worst case was computed from."
  value       = local.price_table_date
}

output "runner_subnet_ids" {
  description = "The subnets runners launch into."
  value       = local.runner_subnet_ids
}
