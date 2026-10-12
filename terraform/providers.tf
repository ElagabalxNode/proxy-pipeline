provider "cherryservers" {
  api_token = var.cherry_api_token
}

# Credentials: YC_TOKEN, YC_CLOUD_ID, YC_FOLDER_ID (or set in provider via CI).
# provider "yandex" {
#   zone = var.yandex_zone
# }

# Token via env TWC_TOKEN. The token MUST have "delete services via API without a
# Telegram code" enabled, otherwise `terraform destroy` / `pipelinectl rotate` hang.
provider "twc" {}
