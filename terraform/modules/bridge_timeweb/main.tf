data "twc_os" "ubuntu" {
  name    = var.os_name
  version = var.os_version
}

data "twc_configurator" "c" {
  location = var.configurator_location
}

resource "twc_ssh_key" "bridge" {
  name = "${var.name_prefix}-bridge"
  body = trimspace(file(var.ssh_public_key_path))
}

resource "twc_server" "bridge" {
  name  = "${var.name_prefix}-bridge"
  os_id = data.twc_os.ubuntu.id

  # null => omit, so Timeweb assigns a zone (and accounts without AZ choice still work).
  availability_zone = var.availability_zone != "" ? var.availability_zone : null

  ssh_keys_ids = [twc_ssh_key.bridge.id]

  configuration {
    configurator_id = data.twc_configurator.c.id
    cpu             = var.cpu
    ram             = var.ram_mb
    disk            = var.disk_mb
  }
}

# Timeweb больше не выдаёт публичный IPv4 по умолчанию (сервер приходит IPv6-only),
# поэтому заказываем IPv4 явно и привязываем к серверу. IP умирает вместе с сервером
# при rotate (= «свежий IP при ротации»); для постоянного адреса см. twc_floating_ip.
resource "twc_server_ip" "bridge" {
  source_server_id = tonumber(twc_server.bridge.id)
  type             = "ipv4"
}
