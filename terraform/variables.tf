variable "name" {
  type    = string
  default = "cost-scanner"
}

variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "report_email" {
  description = "E-mail for the weekly report. Leave empty to subscribe later."
  type        = string
  default     = ""
}

variable "scan_regions" {
  description = "Regions to scan: a list, or [\"all\"] for every enabled region. Empty means the deploy region only."
  type        = list(string)
  default     = []
}

variable "schedule" {
  description = "EventBridge Scheduler cron expression."
  type        = string
  default     = "cron(0 8 ? * MON *)"
}

variable "schedule_timezone" {
  type    = string
  default = "Europe/London"
}

variable "snapshot_age_days" {
  type    = number
  default = 90
}

variable "stopped_age_days" {
  type    = number
  default = 14
}

variable "include_costs" {
  description = "Call Cost Explorer. Cost Explorer must be enabled once in the Billing console."
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Create an AWS Budget with alerts at 80% actual and 100% forecast. 0 skips it."
  type        = number
  default     = 0
}

variable "anomaly_threshold_usd" {
  description = "Create Cost Anomaly Detection for services and alert on anomalies above this amount. 0 skips it."
  type        = number
  default     = 0
}

variable "log_retention_days" {
  type    = number
  default = 30
}
