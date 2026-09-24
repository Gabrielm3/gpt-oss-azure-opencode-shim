locals {
  # Import IDs cannot be sensitive; the subscription ID stays out of git anyway.
  account_id = "/subscriptions/${nonsensitive(var.subscription_id)}/resourceGroups/${var.resource_group_name}/providers/Microsoft.CognitiveServices/accounts/${var.account_name}"
}

# These resources existed before this code. Import them instead of
# recreating them: recreation would change the endpoint and the keys.
import {
  to = azurerm_cognitive_account.foundry
  id = local.account_id
}

import {
  to = azurerm_cognitive_deployment.gpt_oss
  id = "${local.account_id}/deployments/gpt-oss-120b"
}
