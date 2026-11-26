# ---------------------------------------------------------------- storage

resource "aws_s3_bucket" "reports" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is out of scope.
  #checkov:skip=CKV2_AWS_62:Event notifications are not needed.
  #checkov:skip=CKV_AWS_18:Access logging is out of scope for a report bucket.
  #checkov:skip=CKV_AWS_145:SSE-S3 is enough for cost reports; no KMS cost.
  bucket        = "${var.name}-reports-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_sns_topic" "report" {
  #checkov:skip=CKV_AWS_26:Report summaries hold no secrets; default encryption avoids KMS cost.
  name = "${var.name}-report"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.report_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.report.arn
  protocol  = "email"
  endpoint  = var.report_email
}

# ----------------------------------------------------------------- lambda

data "archive_file" "fn" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/scanner.zip"
  excludes    = ["**/__pycache__/**"]
}

resource "aws_cloudwatch_log_group" "fn" {
  #checkov:skip=CKV_AWS_158:Default encryption is used.
  #checkov:skip=CKV_AWS_338:Retention is configurable.
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_sqs_queue" "dlq" {
  name                    = "${var.name}-dlq"
  sqs_managed_sse_enabled = true
}

resource "aws_lambda_function" "fn" {
  #checkov:skip=CKV_AWS_117:Calls only public AWS APIs; a VPC adds NAT cost.
  #checkov:skip=CKV_AWS_272:Code signing is out of scope.
  #checkov:skip=CKV_AWS_173:Environment variables hold no secrets.
  #checkov:skip=CKV_AWS_116:Failed scheduled runs go to the Scheduler DLQ instead.
  function_name                  = var.name
  description                    = "Weekly cost summary and unused resource scan (read-only)"
  role                           = aws_iam_role.fn.arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "scanner.handler.lambda_handler"
  filename                       = data.archive_file.fn.output_path
  source_code_hash               = data.archive_file.fn.output_base64sha256
  timeout                        = 600
  memory_size                    = 256
  reserved_concurrent_executions = 1

  environment {
    variables = {
      REPORT_BUCKET     = aws_s3_bucket.reports.id
      SNS_TOPIC_ARN     = aws_sns_topic.report.arn
      REGIONS           = join(",", var.scan_regions)
      SNAPSHOT_AGE_DAYS = tostring(var.snapshot_age_days)
      STOPPED_AGE_DAYS  = tostring(var.stopped_age_days)
      INCLUDE_COSTS     = tostring(var.include_costs)
    }
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "fn" {
  #checkov:skip=CKV_AWS_356:Describe and Cost Explorer read actions do not support resource-level permissions.
  #checkov:skip=CKV_AWS_111:No write actions use Resource "*".
  statement {
    sid = "ReadOnlyInventory"
    actions = [
      "ec2:DescribeRegions",
      "ec2:DescribeVolumes",
      "ec2:DescribeAddresses",
      "ec2:DescribeSnapshots",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "ec2:DescribeNatGateways",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth",
      "cloudwatch:GetMetricStatistics",
      "logs:DescribeLogGroups",
      "ce:GetCostAndUsage",
      "ce:GetCostForecast",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn.arn}:*"]
  }

  statement {
    sid       = "WriteReports"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.reports.arn}/reports/*"]
  }

  statement {
    sid       = "SendSummary"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.report.arn]
  }
}

resource "aws_iam_role_policy" "fn" {
  name   = "scanner"
  role   = aws_iam_role.fn.id
  policy = data.aws_iam_policy_document.fn.json
}

# --------------------------------------------------------------- schedule

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-scanner"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.fn.arn },
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.dlq.arn },
    ]
  })
}

resource "aws_scheduler_schedule" "weekly" {
  #checkov:skip=CKV_AWS_297:The schedule holds no sensitive data; the AWS-owned key is used.
  name                         = var.name
  description                  = "Run the cost scanner"
  schedule_expression          = var.schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.fn.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }

    dead_letter_config {
      arn = aws_sqs_queue.dlq.arn
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.name}-errors"
  alarm_description   = "The cost scanner failed. No report was sent."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fn.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.report.arn]
}
