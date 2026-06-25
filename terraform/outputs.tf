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

# Active bridge = Timeweb if enabled, else Yandex, else none. During a zero-downtime
# cutover both modules may be up at once; Timeweb wins so clients move to it first.
output "bridge_ip" {
  description = "Active bridge public IP (Timeweb > Yandex; empty if no bridge)"
  value       = try(module.bridge_timeweb[0].external_ip, try(module.bridge[0].external_ip, ""))
}

output "bridge_instance_id" {
  description = "Active bridge instance id (empty if no bridge)"
  value       = try(module.bridge_timeweb[0].instance_id, try(module.bridge[0].instance_id, ""))
}

output "bridge_ssh_user" {
  description = "SSH user for the active bridge VM (root on Timeweb, ubuntu on Yandex)"
  value       = try(module.bridge_timeweb[0].ssh_user, try(module.bridge[0].ssh_user, ""))
}

output "pipeline_contract_version" {
  value = 1
}
