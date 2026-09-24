terraform {
  required_version = "~> 1.12"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
  }

  # Remote state in Azure Blob Storage, Entra ID auth only (the storage
  # account has shared-key access disabled). Bootstrap: infra/README.md.
  backend "azurerm" {
    resource_group_name  = var.resource_group_name
    storage_account_name = var.state_storage_account_name
    container_name       = "tfstate"
    key                  = "azure-shim.tfstate"
    use_azuread_auth     = true
  }

  # The state holds the account's API keys (computed attributes), so it is
  # encrypted client-side before it ever reaches the backend.
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method   = method.aes_gcm.state
      enforced = true
    }

    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}

provider "azurerm" {
  features {}

  subscription_id = var.subscription_id

  # CI runs with a least-privilege identity that cannot register providers.
  resource_provider_registrations = "none"
}
