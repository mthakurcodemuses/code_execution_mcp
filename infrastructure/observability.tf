# =============================================================================
# Observability – X-Ray, CloudWatch Alarms, Dashboard
# =============================================================================

# ---------------------------------------------------------------------------
# Variables local to this file
# ---------------------------------------------------------------------------
variable "alarm_email" {
  description = "Email address to receive CloudWatch alarm notifications. Leave empty to skip SNS."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# SNS topic for alarm notifications
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "alarms" {
  name = "${local.name_prefix}-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ---------------------------------------------------------------------------
# X-Ray – enable active tracing on the Lambda function
# (the function resource references this; declared here for clarity)
# The Lambda aws_xray_tracing_config is set in main.tf via tracing_config.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CloudWatch Alarms
# ---------------------------------------------------------------------------

# 1. Error rate > 10% over 5 minutes
resource "aws_cloudwatch_metric_alarm" "lambda_error_rate" {
  alarm_name          = "${local.name_prefix}-error-rate-high"
  alarm_description   = "Sandbox Lambda error rate exceeded 10% over 5 minutes"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 10

  metric_query {
    id          = "error_rate"
    expression  = "100 * errors / MAX([errors, invocations])"
    label       = "Error rate (%)"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      metric_name = "Errors"
      namespace   = "AWS/Lambda"
      period      = 300
      stat        = "Sum"
      dimensions  = { FunctionName = aws_lambda_function.sandbox.function_name }
    }
  }

  metric_query {
    id = "invocations"
    metric {
      metric_name = "Invocations"
      namespace   = "AWS/Lambda"
      period      = 300
      stat        = "Sum"
      dimensions  = { FunctionName = aws_lambda_function.sandbox.function_name }
    }
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# 2. Timeout rate > 5% over 5 minutes  (custom metric from EMF)
resource "aws_cloudwatch_metric_alarm" "timeout_rate" {
  alarm_name          = "${local.name_prefix}-timeout-rate-high"
  alarm_description   = "Code execution timeout rate exceeded 5% over 5 minutes"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 5

  metric_query {
    id          = "timeout_rate"
    expression  = "100 * timeouts / MAX([timeouts, invocations])"
    label       = "Timeout rate (%)"
    return_data = true
  }

  metric_query {
    id = "timeouts"
    metric {
      metric_name = "TimeoutCount"
      namespace   = "CodeExecution"
      period      = 300
      stat        = "Sum"
      dimensions  = { Environment = var.environment }
    }
  }

  metric_query {
    id = "invocations"
    metric {
      metric_name = "Invocations"
      namespace   = "AWS/Lambda"
      period      = 300
      stat        = "Sum"
      dimensions  = { FunctionName = aws_lambda_function.sandbox.function_name }
    }
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# 3. P99 duration > 28 s (Lambda timeout is 35 s → warn before it fires)
resource "aws_cloudwatch_metric_alarm" "duration_p99" {
  alarm_name          = "${local.name_prefix}-duration-p99-high"
  alarm_description   = "Sandbox Lambda P99 duration exceeded 28 000 ms"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  threshold           = 28000
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  statistic           = "p99"
  period              = 300
  dimensions          = { FunctionName = aws_lambda_function.sandbox.function_name }
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# 4. Any throttling
resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${local.name_prefix}-throttles"
  alarm_description   = "Sandbox Lambda is being throttled"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 60
  dimensions          = { FunctionName = aws_lambda_function.sandbox.function_name }
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# 5. Blocked submissions spike (custom metric)
resource "aws_cloudwatch_metric_alarm" "blocked_submissions" {
  alarm_name          = "${local.name_prefix}-blocked-submissions"
  alarm_description   = "High rate of blocked code submissions – possible abuse attempt"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 10   # more than 10 blocked in 5 minutes
  namespace           = "CodeExecution"
  metric_name         = "BlockedSubmissions"
  statistic           = "Sum"
  period              = 300
  dimensions          = { Environment = var.environment }
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# CloudWatch Dashboard
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_dashboard" "sandbox" {
  dashboard_name = "${local.name_prefix}-sandbox"

  dashboard_body = jsonencode({
    widgets = [
      # ── Row 1: Invocations & Errors ──────────────────────────────────────
      {
        type   = "metric"
        x = 0; y = 0; width = 8; height = 6
        properties = {
          title  = "Invocations"
          region = var.aws_region
          metrics = [[
            "AWS/Lambda", "Invocations",
            "FunctionName", aws_lambda_function.sandbox.function_name,
            { stat = "Sum", period = 60 }
          ]]
          view  = "timeSeries"
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x = 8; y = 0; width = 8; height = 6
        properties = {
          title  = "Errors & Timeouts"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.sandbox.function_name,
              { stat = "Sum", period = 60, color = "#d62728" }],
            ["CodeExecution", "TimeoutCount", "Environment", var.environment,
              { stat = "Sum", period = 60, color = "#ff7f0e" }],
            ["CodeExecution", "BlockedSubmissions", "Environment", var.environment,
              { stat = "Sum", period = 60, color = "#9467bd" }],
          ]
          view  = "timeSeries"
          yAxis = { left = { min = 0 } }
        }
      },
      # ── Row 1 continued: Throttles ───────────────────────────────────────
      {
        type   = "metric"
        x = 16; y = 0; width = 8; height = 6
        properties = {
          title  = "Throttles"
          region = var.aws_region
          metrics = [[
            "AWS/Lambda", "Throttles",
            "FunctionName", aws_lambda_function.sandbox.function_name,
            { stat = "Sum", period = 60, color = "#e377c2" }
          ]]
          view  = "timeSeries"
          yAxis = { left = { min = 0 } }
        }
      },
      # ── Row 2: Latency ───────────────────────────────────────────────────
      {
        type   = "metric"
        x = 0; y = 6; width = 12; height = 6
        properties = {
          title  = "Lambda Duration (p50 / p95 / p99)"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.sandbox.function_name,
              { stat = "p50", period = 60, label = "p50" }],
            ["...", { stat = "p95", period = 60, label = "p95", color = "#ff7f0e" }],
            ["...", { stat = "p99", period = 60, label = "p99", color = "#d62728" }],
          ]
          view  = "timeSeries"
          yAxis = { left = { min = 0 } }
        }
      },
      # Code execution time (custom metric from EMF)
      {
        type   = "metric"
        x = 12; y = 6; width = 12; height = 6
        properties = {
          title  = "Code Execution Time ms (p50 / p99)"
          region = var.aws_region
          metrics = [
            ["CodeExecution", "ExecutionTime", "Environment", var.environment,
              { stat = "p50", period = 60, label = "p50" }],
            ["...", { stat = "p99", period = 60, label = "p99", color = "#d62728" }],
          ]
          view  = "timeSeries"
          yAxis = { left = { min = 0 } }
        }
      },
      # ── Row 3: Alarm status ──────────────────────────────────────────────
      {
        type   = "alarm"
        x = 0; y = 12; width = 24; height = 3
        properties = {
          title = "Alarm Status"
          alarms = [
            aws_cloudwatch_metric_alarm.lambda_error_rate.arn,
            aws_cloudwatch_metric_alarm.timeout_rate.arn,
            aws_cloudwatch_metric_alarm.duration_p99.arn,
            aws_cloudwatch_metric_alarm.throttles.arn,
            aws_cloudwatch_metric_alarm.blocked_submissions.arn,
          ]
        }
      },
      # ── Row 4: Log Insights ──────────────────────────────────────────────
      {
        type   = "log"
        x = 0; y = 15; width = 24; height = 6
        properties = {
          title  = "Recent execution_complete events"
          region = var.aws_region
          query  = join("\n", [
            "SOURCE '${aws_cloudwatch_log_group.lambda_logs.name}'",
            "| filter event = 'execution_complete'",
            "| fields timestamp, correlation_id, exit_code, execution_time_ms, timed_out",
            "| sort @timestamp desc",
            "| limit 50",
          ])
          view = "table"
        }
      },
    ]
  })
}
