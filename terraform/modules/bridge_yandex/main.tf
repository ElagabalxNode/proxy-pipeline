locals {
  ssh_public_key = trimspace(file(var.ssh_public_key_path))
}

data "yandex_compute_image" "ubuntu" {
  family = "ubuntu-2204-lts"
}

# Reserved (static) public IP. It outlives the VM, so rebuilding the bridge keeps the address
# every client QR points at. The original ephemeral 130.193.37.161 was made static in place on
# 2026-09-08 (same IP) and imported. Billing: same as an attached ephemeral IP while the VM runs;
# YC charges a reservation surcharge only while the address sits unattached.
# create_before_destroy: a deliberate address rotation (`rotate bridge --new-ip`) must allocate
# the new address before YC will let the old, still-attached one go.
resource "yandex_vpc_address" "bridge" {
  name      = "${var.name_prefix}-bridge"
  folder_id = var.folder_id

  external_ipv4_address {
    zone_id = var.zone
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "yandex_compute_instance" "bridge" {
  name        = "${var.name_prefix}-bridge"
  folder_id   = var.folder_id
  zone        = var.zone
  platform_id = var.platform_id

  resources {
    cores  = var.cores
    memory = var.memory_gb
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.ubuntu.id
      size     = var.disk_size_gb
      type     = "network-ssd"
    }
  }

  network_interface {
    subnet_id      = var.subnet_id
    nat            = true
    nat_ip_address = yandex_vpc_address.bridge.external_ipv4_address[0].address
  }

  metadata = {
    ssh-keys = "ubuntu:${local.ssh_public_key}"
  }

  scheduling_policy {
    preemptible = false
  }

  # The image is resolved from the family at plan time, so a newer Ubuntu image would force a
  # replacement of the bridge on ANY apply. That happened on 2026-09-08 during `rotate exit`:
  # the bridge was recreated, its ephemeral public IP changed and every client QR died.
  # The image only matters at creation — ignore drift afterwards.
  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image_id]
  }
}
