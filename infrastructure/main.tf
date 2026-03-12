terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------
locals {
  name_prefix = "${var.project_name}-${var.environment}"
}

# ---------------------------------------------------------------------------
# Package the Lambda source into a zip
# ---------------------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda/handler.py"
  output_path = "${path.module}/../lambda/handler.zip"
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group (created before the function so Terraform owns it)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${local.name_prefix}-sandbox"
  retention_in_days = 14
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "sandbox" {
  function_name = "${local.name_prefix}-sandbox"
  description   = "Sandboxed Python code execution"

  # Zip deployment – Python 3.12 managed runtime
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"

  role        = aws_iam_role.lambda_exec.arn
  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds

  environment {
    variables = {
      MAX_TIMEOUT_SECONDS = tostring(var.max_execution_timeout)
    }
  }

  # Attach to the private (no-internet) subnets
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_logs,
    aws_iam_role_policy_attachment.lambda_logs,
    aws_iam_role_policy_attachment.lambda_vpc,
  ]
}

# ---------------------------------------------------------------------------
# Optional: allow a specific IAM principal to invoke the Lambda
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "allow_mcp_caller" {
  count = var.mcp_caller_arn != "" ? 1 : 0

  statement_id  = "AllowMCPCallerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sandbox.function_name
  principal     = var.mcp_caller_arn
}
