variable "cherry_api_token" {
  description = "Cherry Servers API token"
  type        = string
  sensitive   = true
}

variable "cherry_project_id" {
  description = "Cherry Servers Project ID"
  type        = number
}

variable "exit_region" {
  description = "Cherry API region slug"
  type        = string
}

variable "exit_plan" {
  type = string
}

variable "exit_image" {
  type = string
}

variable "ssh_public_key_path" {
  type = string
}

variable "panel_port" {
  type = string
}

variable "panel_user" {
  type = string
}

variable "panel_pass" {
  type      = string
  sensitive = true
}

variable "panel_path" {
  type = string
}
