variable "folder_id" {
  description = "Yandex Cloud folder ID"
  type        = string
}

variable "zone" {
  description = "Yandex zone, e.g. ru-central1-a"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID with Internet access / NAT for the VM (use an existing subnet from your folder)"
  type        = string
}

variable "name_prefix" {
  type    = string
  default = "proxy-pipeline"
}

variable "ssh_public_key_path" {
  description = "Path to public key; metadata user is ubuntu (Ubuntu image)"
  type        = string
}

variable "platform_id" {
  type    = string
  default = "standard-v3"
}

variable "cores" {
  type    = number
  default = 2
}

variable "memory_gb" {
  type    = number
  default = 2
}

variable "disk_size_gb" {
  type    = number
  default = 20
}
