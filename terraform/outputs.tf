output "exit_ip" {
  description = "Exit node public IPv4 (Cherry)"
  value       = module.exit.exit_ip
}

output "exit_id" {
  description = "Cherry server id"
  value       = module.exit.exit_id
}

output "panel_url" {
  description = "3X-UI panel URL on Exit"
  value       = module.exit.panel_url
}

output "ssh_command" {
  description = "SSH to Exit as root"
  value       = module.exit.ssh_command
}

output "bridge_ip" {
  description = "Yandex bridge public IP (empty if enable_yandex_bridge=false)"
  value       = try(module.bridge[0].external_ip, "")
}

output "bridge_instance_id" {
  description = "Yandex instance id (empty if bridge disabled)"
  value       = try(module.bridge[0].instance_id, "")
}

output "bridge_ssh_user" {
  description = "SSH user for bridge VM"
  value       = try(module.bridge[0].ssh_user, "")
}

output "pipeline_contract_version" {
  value = 1
}
