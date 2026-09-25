# Alerts before the credit runs out and Azure disables the subscription.
locals {
  # Azure allows 5 notifications per budget: these 4 + the forecast.
  credit_alert_thresholds = [20, 50, 80, 100]
}

resource "azurerm_consumption_budget_subscription" "credit" {
  name            = "credit"
  subscription_id = "/subscriptions/${var.subscription_id}"

  amount     = var.credit_budget_amount
  time_grain = "Annually"

  # Start must be the 1st of a month.
  time_period {
    start_date = "2026-09-01T00:00:00Z"
  }

  dynamic "notification" {
    for_each = local.credit_alert_thresholds
    content {
      enabled        = true
      threshold      = notification.value
      threshold_type = "Actual"
      operator       = "GreaterThanOrEqualTo"
      # No email in the public repo.
      contact_roles = ["Owner"]
    }
  }

  # Forecast hits the full credit.
  notification {
    enabled        = true
    threshold      = 100
    threshold_type = "Forecasted"
    operator       = "GreaterThanOrEqualTo"
    contact_roles  = ["Owner"]
  }
}
