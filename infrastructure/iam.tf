# =============================================================================
# IAM – Lambda execution role
# =============================================================================

# ---------------------------------------------------------------------------
# Trust policy: only Lambda service may assume this role
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

resource "aws_iam_role" "lambda_exec" {
  name               = "${local.name_prefix}-lambda-exec"
  description        = "Execution role for the code-execution sandbox Lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# ---------------------------------------------------------------------------
# Attach AWS-managed policies
# ---------------------------------------------------------------------------

# Allows writing logs to CloudWatch
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Allows the Lambda to be placed inside a VPC (create ENIs)
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# ---------------------------------------------------------------------------
# Explicit deny: prevent the Lambda from calling any AWS API
# (defence-in-depth on top of the VPC network isolation)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "deny_aws_apis" {
  statement {
    sid       = "DenyAllAWSAPIs"
    effect    = "Deny"
    actions   = ["*"]
    resources = ["*"]

    # Carve out the two actions that the Lambda runtime itself needs
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }
}

resource "aws_iam_policy" "deny_aws_apis" {
  name        = "${local.name_prefix}-deny-aws-apis"
  description = "Prevent the sandbox Lambda from making AWS API calls"
  policy      = data.aws_iam_policy_document.deny_aws_apis.json
}

resource "aws_iam_role_policy_attachment" "deny_aws_apis" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.deny_aws_apis.arn
}

# =============================================================================
# IAM – MCP server caller policy (attach to the role/user running the server)
# =============================================================================
data "aws_iam_policy_document" "mcp_invoke" {
  statement {
    sid       = "InvokeSandboxLambda"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.sandbox.arn]
  }
}

resource "aws_iam_policy" "mcp_invoke" {
  name        = "${local.name_prefix}-mcp-invoke"
  description = "Allows the MCP server to invoke the code-execution sandbox Lambda"
  policy      = data.aws_iam_policy_document.mcp_invoke.json
}
