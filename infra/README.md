# Capped CI runner fleet (Terraform)

A self-hosted GitHub Actions runner fleet for one GitHub organisation, on AWS, that starts with a **hard ceiling on machines
and a monthly budget from the first apply**. The code is in [`runner-fleet/`](runner-fleet/). It is ephemeral (one job per
machine, then the machine is terminated), scale-to-zero (an idle fleet costs a few dollars a month for its NAT instance),
and spot-first with on-demand failover, with the failover bounded by the same ceiling.

Why the ceiling is there from the start: a fleet with no ceiling grows to whatever the queue asks for. One real fleet of this
shape peaked at 454 machines at once and cost about $5,700 a month before it was given a ceiling of 40.

## What you get by default

| Class | Machine | Ceiling (`max_count`) | Capacity |
|---|---|---|---|
| `ci-small` | 2 vCPU / 8 GiB (five equivalent Intel types) | 4 | spot, on-demand failover |
| `ci-medium` | 4 vCPU / 16 GiB (five equivalent Intel types) | 2 | spot, on-demand failover |

At most **6 machines** at once. A job past the ceiling **waits** for a machine; it does not fail. Worst case for the month
(every machine busy all month at the dearest listed type's on-demand price): **USD 651.22**, under the default
`monthly_budget_usd` of 700. The plan refuses to run if it is not (see "The two cost guards").

A workflow uses a class with `runs-on: [self-hosted, linux, x64, ci-small]`.

## What you need first

- Terraform 1.9 or newer, and `bash`, `curl`, `shasum` (for step 3).
- An AWS account, ideally one the fleet has to itself (then the budget alarm covers exactly the fleet), and credentials that
  can create EC2, VPC, IAM, Lambda, SQS, API Gateway, EventBridge, CloudWatch, SSM and Budgets resources.
- Owner rights on the GitHub organisation (to create and install a GitHub App).

## Owner steps

### 1. Create the GitHub App

GitHub organisation, Settings, Developer settings, GitHub Apps, New GitHub App.

- **Webhook**: Active. For the URL put any placeholder (`https://example.com`) for now; step 5 replaces it. For the secret,
  generate one (`openssl rand -hex 32`) and keep it for step 2.
- **Repository permissions**: Actions: Read-only. Checks: Read-only. Metadata: Read-only.
- **Organization permissions**: Self-hosted runners: Read and write.
- **Subscribe to events**: Workflow job.
- Create it. On the App's page note the **App ID**, then **Generate a private key** and save the `.pem`.
- Install the App on the organisation (choose the repositories that may use the fleet).

### 2. Put the App's two secrets in SSM

The secrets live in SSM Parameter Store as SecureStrings. Terraform only ever sees their **names**. Use a profile for the
fleet's account:

```
aws ssm put-parameter --region us-east-1 --name /check-tree/github-app/key_base64 --type SecureString \
  --value "$(base64 < your-app.private-key.pem | tr -d '\n')"
read -rs WEBHOOK_SECRET   # paste the secret from step 1, press return
aws ssm put-parameter --region us-east-1 --name /check-tree/github-app/webhook_secret --type SecureString --value "$WEBHOOK_SECRET"
unset WEBHOOK_SECRET
```

Then delete the `.pem` from your disk. Use other names if you like; the same names go in `terraform.tfvars`.

### 3. Download the module's three Lambda zips

A plain `terraform init` does not fetch them. From `infra/runner-fleet/`:

```
./download-lambdas.sh
```

It downloads the v7.11.0 release assets and stops if a sha256 does not match the one pinned in the script.

### 4. Configure, `init`, `plan`, `apply`

```
cd infra/runner-fleet
cp terraform.tfvars.example terraform.tfvars      # fill in github_org, github_app.id, the two SSM names, budget_alert_email
terraform init
terraform plan -out=fleet.tfplan
```

**Reading the plan.** The first plan creates only new things and destroys nothing. Expect these resource types:

- Network (when `vpc_id` is left null): `aws_vpc`, `aws_internet_gateway`, `aws_subnet` (1 public, 2 private),
  `aws_route_table` and associations, `aws_vpc_endpoint` (S3), and the fck-nat module's launch template, auto-scaling group,
  network interface, security group, IAM role/policy/instance profile.
- The runner module: `aws_lambda_function` (webhook, scale-up, scale-down, binaries syncer, and a few more),
  `aws_sqs_queue` (per class), `aws_apigatewayv2_*` (the webhook endpoint), `aws_cloudwatch_event_*` (EventBridge),
  `aws_cloudwatch_log_group`, `aws_iam_role`/`aws_iam_policy`/`aws_iam_role_policy`, `aws_launch_template` (per class),
  `aws_s3_bucket` and its configuration resources (runner binaries), `aws_kms_key`, `aws_security_group`,
  `aws_scheduler_schedule`, `aws_iam_service_linked_role` (spot, if `create_spot_service_linked_role`), `aws_ssm_parameter`
  entries for the module's own settings, and a few `random_*` and `null_resource` helpers.
- `aws_budgets_budget` (one, with two notifications) and two `terraform_data` resources (the cost guard and the network guard).

Stop and ask if you see any `destroy`, any resource type that is not in that list, or a `max_count` you did not set. (No runner
machine is in the plan: the Lambdas launch them later, one at a time, up to the ceiling.) Check the `ceilings` and
`worst_case_monthly_usd` outputs in the plan's output section match what you meant. Then:

```
terraform apply fleet.tfplan
```

### 5. Point the App's webhook at the fleet

```
terraform output -raw webhook_endpoint
```

Paste it into the GitHub App's **Webhook URL** and save. Confirm the budget alarm: AWS emails `budget_alert_email` a
subscription confirmation the first time; **the alarm is not live until someone confirms it**.

### 6. (Recommended) Remote state

Local state is fine for one person. For more, copy `backend.tf.example` to `backend.tf`, name a bucket you have created
(versioning and encryption on), and run `terraform init -migrate-state`. State contains SSM parameter names, never a secret.

Check it works by pushing a workflow with `runs-on: [self-hosted, linux, x64, ci-small]`. The first job takes a minute or so
while a machine boots; afterwards the machine is gone.

## The two cost guards

1. **The plan-time guard** (`terraform_data.budget_guard` in `main.tf`). For each class: `max_count` times the on-demand hourly
   price of the dearest instance type in its list times 730 hours (prices in `prices.tf`, us-east-1, dated 2026-11-05). If
   the sum is over `monthly_budget_usd`, **the plan fails**, and the message names the total and each class's share. A type
   with no price in the table fails the plan too (it is never priced at zero). On-demand is used even for spot classes
   because it is the only guaranteed bound: a spot price can rise to it, and a refused spot launch falls back to it.
   The worst case covers the machines only. The NAT instance (about $4 a month), storage, logs and Lambda are outside it.
2. **The budget alarm** (`aws_budgets_budget`): email at 80% and at 100% of `monthly_budget_usd` of actual spend, account-wide
   unless you set `budget_cost_filter_tag`. It is the guard for what the ceilings cannot bound.

## Raise or lower a ceiling

Edit the class's `max_count` in `terraform.tfvars` (copy the `classes` block from `terraform.tfvars.example`, uncomment it,
change the number), then `terraform plan` and `terraform apply`. Expect only the scale-up Lambda's settings of that class to
change. If the new worst case is over `monthly_budget_usd`, the plan fails and tells you the total: either lower something or
raise `monthly_budget_usd` as a deliberate act. To add a class, add a key with its instance types and a `max_count`; the new
class gets a ceiling because it cannot be defined without one.

Launches are serialised per class, so a ceiling is exact: a class never holds more than its `max_count` machines.

## Kill switch

Set **every** class's `max_count` to 0 in `terraform.tfvars` and apply:

```
terraform apply
```

Nothing launches. A machine already running finishes its job and terminates (runners are ephemeral); queued jobs wait. To stop
running machines at once, terminate the fleet's instances in the EC2 console (the runner module tags them
`ghr:environment=<prefix>`, `check-tree-ci` by default). Set `max_count` back to turn the fleet on again. To remove the fleet
completely, `terraform destroy`.

## Refresh the price table

`runner-fleet/prices.tf` holds the on-demand hourly price of each instance type the classes may use, in `us-east-1`, with its
date. To add a type or refresh a price, take the price from the AWS Pricing API or the EC2 pricing page (Linux, shared
tenancy, on-demand), edit the table, and change `price_table_date`. Another region has other prices: re-price the table for it
before trusting the guard.

## Tests

`tests/test_pkg_infra.py` (no AWS needed) checks the module's text, and, when `terraform` is installed, runs `terraform fmt
-check`, `terraform validate` and `terraform test` (the budget guard, against a mocked provider, in a scratch copy) in a
scratch copy. It never runs `plan` or `apply` against an account.
