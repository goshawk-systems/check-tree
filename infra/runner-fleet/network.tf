# The network runners launch into. Two cases:
#   var.vpc_id = null  -> this file builds a small VPC: one public subnet holding one fck-nat instance (a t4g-class NAT
#                         instance in an auto-scaling group of one, about $4 a month, not a $33+ NAT Gateway), and two private
#                         subnets (one per availability zone) the runners launch into, routed through that NAT.
#   var.vpc_id = "vpc-..." -> nothing here is created; var.private_subnet_ids are used as given (network_guard below checks
#                         they were given).
# Runners get no public IP and no inbound rule (main.tf's managed security group allows HTTPS and HTTP egress only).
# The two private subnets route through the one NAT, so a runner in the second zone pays cross-zone data charges for its
# egress: a small repo's CI traffic is a few dollars a month of that. It is the price of spot having two zones to draw from.

locals {
  create_vpc = var.vpc_id == null
  az_names   = slice(data.aws_availability_zones.available.names, 0, 2)

  vpc_id            = local.create_vpc ? aws_vpc.fleet[0].id : var.vpc_id
  runner_subnet_ids = local.create_vpc ? aws_subnet.runners_private[*].id : var.private_subnet_ids
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "terraform_data" "network_guard" {
  lifecycle {
    precondition {
      condition     = local.create_vpc || length(var.private_subnet_ids) > 0
      error_message = "vpc_id is set but private_subnet_ids is empty. Name the existing private subnets runners should launch into (one per availability zone), or leave vpc_id null and the fleet builds its own network."
    }
  }
}

resource "aws_vpc" "fleet" {
  count = local.create_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.prefix}-vpc"
  }
}

resource "aws_internet_gateway" "fleet" {
  count = local.create_vpc ? 1 : 0

  vpc_id = aws_vpc.fleet[0].id

  tags = {
    Name = "${var.prefix}-igw"
  }
}

resource "aws_subnet" "nat_public" {
  count = local.create_vpc ? 1 : 0

  vpc_id                  = aws_vpc.fleet[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone       = local.az_names[0]
  map_public_ip_on_launch = true # the NAT instance needs a public address

  tags = {
    Name = "${var.prefix}-nat-public"
  }
}

resource "aws_route_table" "nat_public" {
  count = local.create_vpc ? 1 : 0

  vpc_id = aws_vpc.fleet[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.fleet[0].id
  }

  tags = {
    Name = "${var.prefix}-nat-public"
  }
}

resource "aws_route_table_association" "nat_public" {
  count = local.create_vpc ? 1 : 0

  subnet_id      = aws_subnet.nat_public[0].id
  route_table_id = aws_route_table.nat_public[0].id
}

resource "aws_subnet" "runners_private" {
  count = local.create_vpc ? length(local.az_names) : 0

  vpc_id                  = aws_vpc.fleet[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  availability_zone       = local.az_names[count.index] # one subnet per zone, never two in one zone: EC2 refuses duplicate fleet pools
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.prefix}-runners-private-${local.az_names[count.index]}"
  }
}

resource "aws_route_table" "runners_private" {
  count = local.create_vpc ? 1 : 0

  vpc_id = aws_vpc.fleet[0].id

  tags = {
    Name = "${var.prefix}-runners-private"
  }
}

resource "aws_route_table_association" "runners_private" {
  count = local.create_vpc ? length(local.az_names) : 0

  subnet_id      = aws_subnet.runners_private[count.index].id
  route_table_id = aws_route_table.runners_private[0].id
}

# The runner binaries and the job caches come from S3; a gateway endpoint is free and keeps that traffic off the NAT.
resource "aws_vpc_endpoint" "s3" {
  count = local.create_vpc ? 1 : 0

  vpc_id            = aws_vpc.fleet[0].id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.runners_private[0].id]

  tags = {
    Name = "${var.prefix}-s3"
  }
}

module "fck_nat" {
  count = local.create_vpc ? 1 : 0

  source  = "RaJiska/fck-nat/aws"
  version = "1.6.1"

  name          = "${var.prefix}-nat"
  vpc_id        = aws_vpc.fleet[0].id
  subnet_id     = aws_subnet.nat_public[0].id
  instance_type = "t4g.nano"
  ha_mode       = true # an auto-scaling group of one: a replaced instance re-attaches the same network interface

  update_route_tables = true
  route_tables_ids = {
    "${var.prefix}-runners-private" = aws_route_table.runners_private[0].id
  }
}
