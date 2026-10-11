variable "ssh_public_key_path" {
  description = "Path to the public key uploaded to Timeweb; the VM gets root SSH with it"
  type        = string
}

variable "name_prefix" {
  type    = string
  default = "proxy-pipeline"
}

# Region used to resolve the configurator (sizing catalogue). NOT the same as the
# availability zone: ru-1 = СПб, ru-2 = Москва, de-1, kz-1. `location` is read-only
# on twc_server, so it is set here, on the configurator data source.
variable "configurator_location" {
  type    = string
  default = "ru-1"
}

# Availability zone slug (spb-1, msk-1, nsk-1, ...). Empty string => not set, let
# Timeweb pick. Some accounts cannot select an AZ at all — leave empty if plan rejects it.
variable "availability_zone" {
  type    = string
  default = ""
}

variable "cpu" {
  type    = number
  default = 1
}

# Megabytes. Timeweb requires ram divisible by 1024 (1024 = 1 GB).
variable "ram_mb" {
  type    = number
  default = 1024
}

# Megabytes (15360 = 15 GB).
variable "disk_mb" {
  type    = number
  default = 15360
}

variable "os_name" {
  type    = string
  default = "ubuntu"
}

variable "os_version" {
  type    = string
  default = "22.04"
}
