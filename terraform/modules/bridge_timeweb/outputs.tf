output "external_ip" {
  description = "Public IPv4 ordered for the Timeweb server (twc_server_ip)"
  value       = twc_server_ip.bridge.ip
}

output "instance_id" {
  value = twc_server.bridge.id
}

output "ssh_user" {
  value = "root" # Timeweb gives root SSH via the uploaded key
}
