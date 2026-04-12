locals {
  panel_path_clean     = trimsuffix(trimprefix(trimspace(var.panel_path), "/"), "/")
  panel_request_prefix = local.panel_path_clean == "" ? "" : "/${local.panel_path_clean}"
  panel_ui_path        = local.panel_request_prefix == "" ? "/" : "${local.panel_request_prefix}/"
}

output "exit_ip" {
  value = local.exit_public_ipv4
}

output "exit_id" {
  value = cherryservers_server.exit.id
}

output "panel_url" {
  value = "http://${local.exit_public_ipv4}:${var.panel_port}${local.panel_ui_path}"
}

output "ssh_command" {
  value = "ssh root@${local.exit_public_ipv4}"
}

output "ssh_key_id" {
  value = cherryservers_ssh_key.proxy.id
}
