# =============================================================================
# IAM – Least-privilege execution role for the sandbox Lambda
#
# Principle-of-least-privilege design
# ------------------------------------
# 1. Trust policy  – only the Lambda service may assume the role.
# 2. Permission Boundary – a hard ceiling on what the role can ever do,
#    regardless of any future policy attachments.  Even if an operator
#    accidentally attaches AdministratorAccess, the boundary prevents it
#    from taking effect.
# 3. Inline policy – grants ONLY the three action groups the Lambda runtime
#    actually needs: write logs, manage its own VPC ENI, and write X-Ray
#    segments.  Every other AWS action is implicitly denied.
# 4. No AWS-managed policies attached (they are intentionally broader than
#    needed; we replicate only the exact actions required).
#
# MCP caller policy
# -----------------
# A standalone policy scoped to a single Lambda ARN is provided for the
# principal that runs the MCP server (attach it to that role/user).
# =============================================================================

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Permission Boundary
# Defines the MAXIMUM permissions the execution role may ever hold.
# Actions outside this boundary are always denied, even if a broader policy
# is later attached.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_exec_boundary" {
  # 1. Allow only the exact AWS actions the Lambda runtime needs
  statement {
    sid    = "AllowRuntimeActions"
    effect = "Allow"
    actions = [
      # CloudWatch Logs
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      # VPC ENI management (required for VPC-attached Lambda)
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
      # X-Ray tracing
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  # 2. Explicitly deny everything else – belt-and-braces
  statement {
    sid    = "DenyEverythingElse"
    effect = "Deny"
    not_actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "lambda_exec_boundary" {
  name        = "${local.name_prefix}-lambda-exec-boundary"
  description = "Permission boundary for the sandbox Lambda execution role"
  policy      = data.aws_iam_policy_document.lambda_exec_boundary.json
}

# ---------------------------------------------------------------------------
# Trust policy
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# Execution role  (boundary applied at creation time)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda_exec" {
  name                 = "${local.name_prefix}-lambda-exec"
  description          = "Execution role for the code-execution sandbox Lambda"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume_role.json
  permissions_boundary = aws_iam_policy.lambda_exec_boundary.arn
}

# ---------------------------------------------------------------------------
# Inline policy  – grants only what the runtime actually needs
# (mirrors the boundary Allow statement so effective permissions = boundary)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_exec_allow" {
  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    # Scope to this function's log group only
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-sandbox:*",
    ]
  }

  statement {
    sid    = "ManageVpcEni"
    effect = "Allow"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]   # EC2 ENI actions do not support resource-level conditions
  }

  statement {
    sid    = "WriteXRay"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda_exec_inline" {
  name   = "sandbox-runtime-allow"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_exec_allow.json
}

# ---------------------------------------------------------------------------
# MCP server caller policy
# Attach this to whatever IAM role/user runs the MCP server process.
# Scoped to a single Lambda ARN – no wildcard.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "mcp_invoke" {
  statement {
    sid     = "InvokeSandboxLambdaOnly"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.sandbox.arn]

    # Optional: restrict to synchronous invocations only
    condition {
      test     = "StringEquals"
      variable = "lambda:InvocationType"
      values   = ["RequestResponse"]
    }
  }
}

resource "aws_iam_policy" "mcp_invoke" {
  name        = "${local.name_prefix}-mcp-invoke"
  description = "Allows the MCP server to invoke the sandbox Lambda (RequestResponse only)"
  policy      = data.aws_iam_policy_document.mcp_invoke.json
}
