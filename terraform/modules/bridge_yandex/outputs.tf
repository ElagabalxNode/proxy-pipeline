output "external_ip" {
  value = yandex_compute_instance.bridge.network_interface[0].nat_ip_address
}

output "internal_ip" {
  value = yandex_compute_instance.bridge.network_interface[0].ip_address
}

output "instance_id" {
  value = yandex_compute_instance.bridge.id
}

output "ssh_user" {
  value = "ubuntu"
}
