# Every identifier is a variable: the repo is public and the endpoint
# subdomain is treated as a secret (see UPSTREAM_URL in live-eval.yml).
# Locally they come from terraform.tfvars (gitignored); in CI from TF_VAR_*.

variable "subscription_id" {
  description = "Azure subscription that holds the AI Services account."
  type        = string
  sensitive   = true
}

variable "resource_group_name" {
  description = "Existing resource group (not managed here)."
  type        = string
}

variable "state_storage_account_name" {
  description = "Storage account that holds the remote state."
  type        = string
}

variable "state_passphrase" {
  description = "Passphrase for OpenTofu state encryption (PBKDF2, 16+ chars)."
  type        = string
  sensitive   = true
}

variable "account_name" {
  description = "Name of the AI Services (Cognitive Services) account."
  type        = string
}

variable "custom_subdomain_name" {
  description = "Custom subdomain of the account endpoint."
  type        = string
  sensitive   = true
}

variable "location" {
  description = "Account region. Differs from the resource group's region; changing it replaces the account."
  type        = string
  default     = "northcentralus"
}

variable "credit_budget_amount" {
  type    = number
  default = 520
}

variable "gpt_oss_capacity" {
  description = "gpt-oss-120b deployment capacity, in thousands of tokens per minute."
  type        = number
  default     = 5000
}
