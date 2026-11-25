output "report_bucket" {
  value = aws_s3_bucket.reports.id
}

output "sns_topic_arn" {
  value = aws_sns_topic.report.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.fn.function_name
}

output "run_now" {
  description = "Command to run a scan now."
  value       = "aws lambda invoke --function-name ${aws_lambda_function.fn.function_name} --cli-read-timeout 600 out.json && cat out.json"
}
