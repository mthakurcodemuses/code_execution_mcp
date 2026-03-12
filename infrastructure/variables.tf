variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name prefixed on every resource."
  type        = string
  default     = "code-execution"
}

variable "environment" {
  description = "Environment tag (dev / staging / prod)."
  type        = string
  default     = "dev"
}

# ---------------------------------------------------------------------------
# Lambda tuning
# ---------------------------------------------------------------------------
variable "lambda_memory_mb" {
  description = "Memory allocated to the Lambda function (MB)."
  type        = number
  default     = 512
}

variable "lambda_timeout_seconds" {
  description = "Hard Lambda timeout (seconds). Should exceed max_execution_timeout."
  type        = number
  default     = 35
}

variable "max_execution_timeout" {
  description = "Maximum code-execution timeout exposed to callers (seconds)."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------------------
# Networking – outbound isolation
# ---------------------------------------------------------------------------
variable "vpc_cidr" {
  description = "CIDR for the dedicated sandbox VPC."
  type        = string
  default     = "10.10.0.0/16"
}

variable "private_subnet_cidrs" {
  description = "CIDRs for private subnets (one per AZ)."
  type        = list(string)
  default     = ["10.10.1.0/24", "10.10.2.0/24"]
}

# ---------------------------------------------------------------------------
# MCP caller
# ---------------------------------------------------------------------------
variable "mcp_caller_arn" {
  description = <<-EOT
    IAM ARN (role or user) that runs the MCP server.
    Granted lambda:InvokeFunction on the sandbox.
    Leave empty to skip the explicit resource-based policy.
  EOT
  type    = string
  default = ""
}
