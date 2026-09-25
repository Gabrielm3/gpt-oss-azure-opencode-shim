# infra

OpenTofu code for the Azure resources behind the shim:

| Resource | Managed here |
|---|---|
| AI Services account (`azurerm_cognitive_account.foundry`) | yes |
| `gpt-oss-120b` deployment (`azurerm_cognitive_deployment.gpt_oss`) | yes |
| Credit budget (`azurerm_consumption_budget_subscription.credit`) | yes |
| Resource group, other deployments, Foundry project | no |
| API keys | no. They are read-only attributes of the account. Rotate them with `az cognitiveservices account keys regenerate`. |

Both resources existed before this code, so they were adopted with `import {}`
blocks (`imports.tf`), not recreated. Recreating the account would change the
endpoint and the keys. Both resources have `prevent_destroy`.

The deployment uses `version_upgrade_option = "NoAutoUpgrade"`. Model upgrades
go through a PR, and the live eval (`docs/EVALS.md`) confirms that behavior
did not regress.

## Credit budget

`budget.tf` emails the subscription Owner at 20%, 50%, 80% and 100% of actual
spend, and at 100% forecast (Azure caps budgets at 5 notifications). Out of
credit means the subscription is disabled. Cost data lags 8-24h, so act on 80%.

## State

- **Backend:** Azure Blob Storage, container `tfstate`. The storage account
  has shared-key access disabled, so it accepts Entra ID logins only. Blob
  versioning is on, and deleted blobs are kept for 30 days.
- **Encryption:** OpenTofu client-side state and plan encryption (PBKDF2 +
  AES-GCM, `enforced = true`). The state holds the account's API keys, so it
  is encrypted before it leaves the machine. Because of this, the `terraform`
  CLI cannot read this state. Use `tofu` only.
- The passphrase lives outside the repo (locally in
  `~/.config/azure-shim/tofu-state-passphrase`, in CI as a secret). If you
  lose it, you lose the state. Keep a copy in a password manager.

## Bootstrap (one time, already done)

These resources hold the state or support CI, so they live outside this
configuration. They were created with the Azure CLI:

```bash
az provider register -n Microsoft.Storage --wait

# State storage: Entra ID only, versioned, soft delete
az storage account create -g "$RG" -n "$SA" -l northcentralus --sku Standard_LRS \
  --min-tls-version TLS1_2 --allow-blob-public-access false \
  --allow-shared-key-access false --https-only true
az storage account blob-service-properties update -g "$RG" -n "$SA" \
  --enable-versioning true --enable-delete-retention true --delete-retention-days 30
az storage container create --account-name "$SA" -n tfstate --auth-mode login
az storage container create --account-name "$SA" -n drift-reports --auth-mode login
# plus a lifecycle rule that deletes drift-reports/ blobs after 90 days

# CI identity, trusted only for the infra-drift environment
az identity create -g "$RG" -n id-azure-shim-drift -l northcentralus
az identity federated-credential create -g "$RG" --identity-name id-azure-shim-drift \
  -n github-infra-drift --issuer https://token.actions.githubusercontent.com \
  --subject "repo:Gabrielm3@48194646/gpt-oss-azure-opencode-shim@1380006725:environment:infra-drift" \
  --audiences api://AzureADTokenExchange
```

This repo's OIDC tokens use GitHub's immutable subject format
(`owner@<owner id>/repo@<repo id>`). A subject in the old `owner/repo` form
fails with `AADSTS700213`.

The role assignments, the master-only `infra-drift` environment and its
secrets were then set up by a one-time script. The steps are described below.

## Local use

The repo is public, so no identifiers are committed. Create
`infra/terraform.tfvars`, which is gitignored:

```hcl
subscription_id            = "..."
resource_group_name        = "..."
state_storage_account_name = "..."
account_name               = "..."
custom_subdomain_name      = "..."
```

Then run:

```bash
az login
export TF_VAR_state_passphrase="$(cat ~/.config/azure-shim/tofu-state-passphrase)"
cd infra
tofu init
tofu plan
```

Your user needs `Storage Blob Data Contributor` on the state storage account.
Being Owner is not enough, because blob data access is a separate permission.

## Drift check (CI)

`.github/workflows/infra-drift.yml` runs `tofu plan -detailed-exitcode`
nightly, on pushes to `master` that touch `infra/`, and on demand. On drift it
fails and opens (or comments on) an `infra-drift` issue.

- **Auth:** the user-assigned managed identity `id-azure-shim-drift` logs in
  through GitHub OIDC (a federated credential for the `infra-drift`
  environment). There is no client secret, and no Entra app registration is
  needed.
- **Least privilege:** the custom role `Azure Shim Drift Reader` on the
  account, `Storage Blob Data Reader` on `tfstate` (the plan runs with
  `-lock=false`), `Storage Blob Data Contributor` on `drift-reports` only,
  and `Cost Management Reader` on the `credit` budget only.
  The identity cannot change Azure.
- **Accepted tradeoff: CI can read the API keys.** Plain `Reader` is not
  enough. The azurerm provider calls `listKeys` on every refresh of an account
  with local auth enabled, and the plan fails if that call is denied. So the
  custom role is Reader plus `listKeys`, scoped to this one account. The
  identity works only from `master` through the `infra-drift` environment, and
  the keys appear as `(sensitive value)` in the plan. The way to remove this
  tradeoff is to switch the shim to Entra ID auth (`local_auth_enabled =
  false`).
- **No public logs:** Actions logs on a public repo are public, and a plan
  prints the subscription ID and the endpoint. The full plan goes to the
  private `drift-reports/<run id>/` container, which deletes reports after 90
  days. The log and the job summary show only the one-line result.

Secrets for the `infra-drift` environment:

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | client ID of `id-azure-shim-drift` |
| `AZURE_TENANT_ID` | tenant ID |
| `AZURE_SUBSCRIPTION_ID` | subscription ID |
| `TF_RESOURCE_GROUP_NAME` | same as `resource_group_name` |
| `TF_STATE_STORAGE_ACCOUNT` | same as `state_storage_account_name` |
| `TF_ACCOUNT_NAME` | same as `account_name` |
| `TF_CUSTOM_SUBDOMAIN_NAME` | same as `custom_subdomain_name` |
| `TF_STATE_PASSPHRASE` | the state passphrase |
