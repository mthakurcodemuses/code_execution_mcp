output "lambda_function_name" {
  description = "Name of the code-execution sandbox Lambda function."
  value       = aws_lambda_function.sandbox.function_name
}

output "lambda_function_arn" {
  description = "ARN of the sandbox Lambda function."
  value       = aws_lambda_function.sandbox.arn
}

output "mcp_invoke_policy_arn" {
  description = "ARN of the IAM policy to attach to the MCP server's role/user."
  value       = aws_iam_policy.mcp_invoke.arn
}

output "lambda_exec_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda_exec.arn
}

output "sandbox_vpc_id" {
  description = "ID of the isolated sandbox VPC."
  value       = aws_vpc.sandbox.id
}

output "mcp_env_vars" {
  description = "Environment variables to set when running the MCP server."
  value = {
    LAMBDA_FUNCTION_NAME = aws_lambda_function.sandbox.function_name
    AWS_REGION           = var.aws_region
  }
}
