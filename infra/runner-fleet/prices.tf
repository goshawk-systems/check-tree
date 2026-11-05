# On-demand list prices, USD per instance-hour, Linux, shared tenancy, us-east-1. Compiled 2026-11-05 from AWS's published
# EC2 on-demand price list. The worst-case check in main.tf reads this table; a type that is not in it fails the plan
# (naming the type) rather than being priced at zero. Prices change rarely, but they do: refresh the table when you add a
# type or once a year (README, "Refresh the price table"), and change the date below with it.
#
# On-demand is the right number even for spot classes: a spot price can rise to the on-demand price, and a refused spot
# launch fails over to on-demand (main.tf), so the on-demand price of the dearest listed type is the only guaranteed bound.

locals {
  price_table_date   = "2026-11-05"
  price_table_region = "us-east-1"
  hours_per_month    = 730 # AWS's own billing month

  on_demand_hourly_usd = {
    # 2 vCPU / 8 GiB
    "m7i.large"      = 0.1008
    "m7i-flex.large" = 0.09576
    "m6i.large"      = 0.096
    "m6in.large"     = 0.11151
    "m5.large"       = 0.096
    "m5n.large"      = 0.119
    # 2 vCPU / 4 GiB (compute-optimised)
    "c7i.large" = 0.08925
    "c6i.large" = 0.085
    # 4 vCPU / 16 GiB
    "m7i.xlarge"      = 0.2016
    "m7i-flex.xlarge" = 0.19152
    "m6i.xlarge"      = 0.192
    "m6in.xlarge"     = 0.22302
    "m5.xlarge"       = 0.192
    "m5n.xlarge"      = 0.238
    # 4 vCPU / 8 GiB (compute-optimised)
    "c7i.xlarge" = 0.1785
    "c6i.xlarge" = 0.17
    # 8 vCPU / 32 GiB
    "m7i.2xlarge" = 0.4032
    "m6i.2xlarge" = 0.384
    "m5.2xlarge"  = 0.384
  }
}
