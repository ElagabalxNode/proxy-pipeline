# --- Cherry (Exit) ---
variable "cherry_api_token" {
  description = "Cherry Servers API token"
  type        = string
  sensitive   = true
}

variable "cherry_project_id" {
  description = "Cherry Servers Project ID (число из URL в панели)"
  type        = number
}

variable "exit_region" {
  description = "Cherry API region slug (GET https://api.cherryservers.com/v1/regions)"
  type        = string
  default     = "SE-Stockholm"

  validation {
    condition = contains(
      ["LT-Siauliai", "NL-Amsterdam", "US-Chicago", "SG-Singapore", "SE-Stockholm", "DE-Frankfurt", "JP-Tokyo"],
      var.exit_region
    )
    error_message = "Unknown exit_region slug. Use Cherry GET /v1/regions."
  }
}

variable "exit_plan" {
  type    = string
  default = "B1-1-1gb-20s-shared"
}

variable "exit_image" {
  type    = string
  default = "ubuntu_24_04_64bit"
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/proxy_pipeline.pub"
}

variable "panel_port" {
  type    = string
  default = "2053"
}

variable "panel_user" {
  type    = string
  default = "admin"
}

variable "panel_pass" {
  type      = string
  sensitive = true
}

variable "panel_path" {
  type    = string
  default = "/secretpanel"
}

# --- Yandex (Bridge) — optional stack slice ---
variable "enable_yandex_bridge" {
  description = "When true, provision Yandex Cloud VM for RU bridge (requires YC credentials + subnet_id)"
  type        = bool
  default     = false
}

variable "yandex_zone" {
  type    = string
  default = "ru-central1-a"
}

variable "yandex_folder_id" {
  type        = string
  default     = ""
  description = "Yandex folder ID (required if enable_yandex_bridge)"
}

variable "yandex_subnet_id" {
  type        = string
  default     = ""
  description = "Subnet with NAT / public IP path (required if enable_yandex_bridge)"
}

variable "yandex_instance_name_prefix" {
  type    = string
  default = "proxy-pipeline"
}

variable "yandex_cores" {
  type    = number
  default = 2
}

variable "yandex_memory_gb" {
  type    = number
  default = 2
}

variable "yandex_disk_gb" {
  type    = number
  default = 20
}

# --- Timeweb Cloud (Bridge) — optional stack slice ---
variable "enable_timeweb_bridge" {
  description = "When true, provision a Timeweb Cloud VM for the RU bridge (requires TWC_TOKEN)"
  type        = bool
  default     = false
}

# Region for the sizing configurator: ru-1 (СПб), ru-2 (Москва), de-1, kz-1.
variable "timeweb_configurator_location" {
  type    = string
  default = "ru-1"
}

# AZ slug (spb-1, msk-1, nsk-1, ...). Empty => let Timeweb pick (safe on accounts
# without AZ selection).
variable "timeweb_availability_zone" {
  type    = string
  default = ""
}

variable "timeweb_cpu" {
  type    = number
  default = 1
}

# Megabytes; ram must be divisible by 1024.
variable "timeweb_ram_mb" {
  type    = number
  default = 1024
}

# Megabytes (15360 = 15 GB).
variable "timeweb_disk_mb" {
  type    = number
  default = 15360
}
