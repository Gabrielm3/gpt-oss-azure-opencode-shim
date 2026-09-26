# The AI Services (Foundry) account that serves gpt-oss-120b to the shim.
# The resource group and the account's other deployments (gpt-5-mini,
# text-embedding-ada-002) and Foundry project are deliberately not managed here.
resource "azurerm_cognitive_account" "foundry" {
  name                = var.account_name
  resource_group_name = var.resource_group_name
  # Literal region, not the resource group's (westus3): a mismatch forces
  # replacement, which would rotate the endpoint and the keys.
  location = var.location

  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = var.custom_subdomain_name

  # Entra ID only: API keys are rejected.
  local_auth_enabled            = false
  public_network_access_enabled = true
  project_management_enabled    = true

  identity {
    type = "SystemAssigned"
  }

  network_acls {
    default_action = "Allow"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_cognitive_deployment" "gpt_oss" {
  name                 = "gpt-oss-120b"
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = "OpenAI-OSS"
    name    = "gpt-oss-120b"
    version = "1"
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.gpt_oss_capacity
  }

  rai_policy_name = "Microsoft.DefaultV2"
  # Model upgrades go through a PR (and the live eval), never silently.
  version_upgrade_option = "NoAutoUpgrade"

  lifecycle {
    prevent_destroy = true
  }
}
