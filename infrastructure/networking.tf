# =============================================================================
# Networking – isolated VPC for the sandbox Lambda
#
# Design goals
# ------------
#  * Lambda runs in private subnets with NO outbound internet access.
#  * No NAT Gateway, no Internet Gateway route to subnets → executed code
#    cannot reach the public internet.
#  * Security group: deny all inbound; deny all outbound (except to AWS
#    internal endpoints used by the Lambda runtime itself).
# =============================================================================

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------
resource "aws_vpc" "sandbox" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "${local.name_prefix}-vpc" }
}

# ---------------------------------------------------------------------------
# Private subnets (one per AZ, no route to internet)
# ---------------------------------------------------------------------------
resource "aws_subnet" "private" {
  count = length(var.private_subnet_cidrs)

  vpc_id                  = aws_vpc.sandbox.id
  cidr_block              = var.private_subnet_cidrs[count.index]
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${local.name_prefix}-private-${count.index + 1}" }
}

# ---------------------------------------------------------------------------
# Route tables – empty (no IGW, no NAT GW attached)
# ---------------------------------------------------------------------------
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.sandbox.id
  tags   = { Name = "${local.name_prefix}-private-rt" }
  # Deliberately no routes added → no egress path
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# Security group for the Lambda function
# ---------------------------------------------------------------------------
resource "aws_security_group" "lambda_sg" {
  name        = "${local.name_prefix}-lambda-sg"
  description = "Sandbox Lambda – deny all inbound and outbound internet"
  vpc_id      = aws_vpc.sandbox.id

  # No ingress rules → Lambda cannot receive unsolicited connections

  # Allow outbound HTTPS only to VPC endpoints (so the Lambda runtime can
  # communicate with AWS services like CloudWatch Logs).
  egress {
    description = "HTTPS to VPC endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = { Name = "${local.name_prefix}-lambda-sg" }
}

# ---------------------------------------------------------------------------
# VPC Endpoints – allow Lambda runtime to reach AWS APIs without internet
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.sandbox.id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.lambda_sg.id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-logs-endpoint" }
}

resource "aws_vpc_endpoint" "lambda" {
  vpc_id              = aws_vpc.sandbox.id
  service_name        = "com.amazonaws.${var.aws_region}.lambda"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.lambda_sg.id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-lambda-endpoint" }
}
