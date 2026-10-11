resource "cherryservers_ssh_key" "proxy" {
  name       = "proxy-pipeline"
  public_key = file(var.ssh_public_key_path)
}

locals {
  user_data = <<-SCRIPT
    #!/bin/bash
    set -e
    export DEBIAN_FRONTEND=noninteractive

    cat > /etc/sysctl.d/99-bbr.conf <<'SYSCTL'
    net.core.default_qdisc=fq
    net.ipv4.tcp_congestion_control=bbr
    net.ipv4.tcp_fastopen=3
    net.ipv4.tcp_slow_start_after_idle=0
    net.core.rmem_max=16777216
    net.core.wmem_max=16777216
    net.ipv4.tcp_rmem=4096 87380 16777216
    net.ipv4.tcp_wmem=4096 65536 16777216
    net.ipv4.ip_forward=1
    SYSCTL
    sysctl -p /etc/sysctl.d/99-bbr.conf

    apt-get update -qq
    apt-get install -y -qq curl wget jq tar unzip fail2ban ca-certificates

    cat > /etc/fail2ban/jail.local <<'F2B'
    [sshd]
    enabled = true
    port = ssh
    maxretry = 3
    bantime = 3600
    F2B
    systemctl enable --now fail2ban

    ARCH=$(uname -m)
    case "$${ARCH}" in
      x86_64 | x64 | amd64) XUI_ARCH="amd64" ;;
      i*86 | x86) XUI_ARCH="386" ;;
      armv8* | armv8 | arm64 | aarch64) XUI_ARCH="arm64" ;;
      armv7* | armv7) XUI_ARCH="armv7" ;;
      armv6* | armv6) XUI_ARCH="armv6" ;;
      armv5* | armv5) XUI_ARCH="armv5" ;;
      s390x) XUI_ARCH="s390x" ;;
      *) XUI_ARCH="amd64" ;;
    esac

    if [[ -f /etc/os-release ]]; then
      . /etc/os-release
      RELEASE_ID="$${ID}"
    elif [[ -f /usr/lib/os-release ]]; then
      . /usr/lib/os-release
      RELEASE_ID="$${ID}"
    else
      echo "Unable to detect OS release" >&2
      exit 1
    fi

    TMP_DIR=$(mktemp -d)
    trap 'rm -rf "$${TMP_DIR}"' EXIT
    wget -q -O "$${TMP_DIR}/x-ui.tar.gz" "https://github.com/MHSanaei/3x-ui/releases/latest/download/x-ui-linux-$${XUI_ARCH}.tar.gz"
    tar -xzf "$${TMP_DIR}/x-ui.tar.gz" -C "$${TMP_DIR}"
    chmod +x "$${TMP_DIR}/x-ui/x-ui" "$${TMP_DIR}"/x-ui/bin/xray-linux-* "$${TMP_DIR}/x-ui/x-ui.sh"
    install -m 0755 "$${TMP_DIR}/x-ui/x-ui.sh" /usr/bin/x-ui

    if [[ -f "$${TMP_DIR}/x-ui/x-ui.service" ]]; then
      install -m 0644 "$${TMP_DIR}/x-ui/x-ui.service" /etc/systemd/system/x-ui.service
    elif [[ "$${RELEASE_ID}" == "ubuntu" || "$${RELEASE_ID}" == "debian" || "$${RELEASE_ID}" == "armbian" ]]; then
      install -m 0644 "$${TMP_DIR}/x-ui/x-ui.service.debian" /etc/systemd/system/x-ui.service
    else
      install -m 0644 "$${TMP_DIR}/x-ui/x-ui.service.rhel" /etc/systemd/system/x-ui.service
    fi

    systemctl stop x-ui >/dev/null 2>&1 || true
    rm -rf /usr/local/x-ui
    mv "$${TMP_DIR}/x-ui" /usr/local/
    systemctl daemon-reload
    systemctl enable x-ui
    systemctl restart x-ui

    mkdir -p /opt/proxy-pipeline
    echo "ready $(date '+%Y-%m-%d %H:%M:%S')" > /opt/proxy-pipeline/.status
  SCRIPT
}

resource "cherryservers_server" "exit" {
  project_id = var.cherry_project_id
  plan       = var.exit_plan
  region     = var.exit_region
  image      = var.exit_image
  hostname   = lower("exit-${var.exit_region}")

  ssh_key_ids = [cherryservers_ssh_key.proxy.id]
  user_data   = base64encode(local.user_data)

  tags = {
    role    = "exit-proxy"
    managed = "proxy-pipeline"
  }

  lifecycle {
    ignore_changes = [hostname]
    # Create the replacement first, destroy the old Exit only once it exists: an out-of-stock
    # region (Cherry API 400 "Server is out of stock", 2026-09-08) must not leave the chain
    # without an Exit.
    create_before_destroy = true
  }
}

locals {
  exit_public_ipv4 = one([
    for ip in cherryservers_server.exit.ip_addresses : ip.address
    if ip.address_family == 4 && startswith(lower(coalesce(ip.type, "")), "primary")
  ])
}
