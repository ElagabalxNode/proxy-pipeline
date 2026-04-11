# ============================================
# Terraform — Exit Node (Cherry Servers)
# Provider: cherryservers/cherryservers v1.5+
# API key: portal.cherryservers.com → Settings → API Keys
# ============================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    cherryservers = {
      source  = "cherryservers/cherryservers"
      version = "~> 1.5"
    }
  }
}

# -----------------------------------------------
# Variables
# -----------------------------------------------

variable "cherry_api_token" {
  description = "Cherry Servers API token"
  type        = string
  sensitive   = true
}

variable "cherry_project_id" {
  description = "Cherry Servers Project ID (число из URL в панели)"
  type        = number
}

variable "exit_region" {
  description = "EU-Nord-1 (Stockholm), NL-Ams-1 (Amsterdam), EU-East-1 (Lithuania), DE-Fra-1 (Frankfurt)"
  type        = string
  default     = "EU-Nord-1"
}

# Планы VPS Cherry Servers (актуальные на 2026):
#   cloud_vps_1   — 1 vCPU, 1GB, 20GB SSD   (~$6/мес)
#   cloud_vps_2   — 1 vCPU, 2GB, 40GB SSD   (~$12/мес)
#   cloud_vps_3   — 2 vCPU, 4GB, 80GB SSD   (~$22/мес)
# Для прокси cloud_vps_1 более чем достаточно.
variable "exit_plan" {
  type    = string
  default = "cloud_vps_1"
}

variable "exit_image" {
  type    = string
  default = "ubuntu_24_04_64bit"
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/proxy_pipeline.pub"
}

variable "panel_port" {
  type    = string
  default = "2053"
}

variable "panel_user" {
  type    = string
  default = "admin"
}

variable "panel_pass" {
  type      = string
  sensitive = true
}

variable "panel_path" {
  type    = string
  default = "/secretpanel"
}

# -----------------------------------------------
# Provider
# -----------------------------------------------

provider "cherryservers" {
  api_token = var.cherry_api_token
}

# -----------------------------------------------
# SSH Key
# -----------------------------------------------

resource "cherryservers_ssh_key" "proxy" {
  name       = "proxy-pipeline"
  public_key = file(var.ssh_public_key_path)
}

# -----------------------------------------------
# User-data (bash bootstrap)
# -----------------------------------------------

locals {
  user_data = <<-SCRIPT
    #!/bin/bash
    set -e
    export DEBIAN_FRONTEND=noninteractive

    # --- BBR + TCP tuning ---
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

    # --- Packages ---
    apt-get update -qq
    apt-get install -y -qq curl wget jq unzip fail2ban ca-certificates

    # --- Fail2ban ---
    cat > /etc/fail2ban/jail.local <<'F2B'
    [sshd]
    enabled = true
    port = ssh
    maxretry = 3
    bantime = 3600
    F2B
    systemctl enable --now fail2ban

    # --- 3X-UI ---
    echo "y" | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
    sleep 10
    x-ui setting -port ${var.panel_port}
    x-ui setting -username ${var.panel_user} -password ${var.panel_pass}
    x-ui setting -webBasePath ${var.panel_path}
    x-ui restart

    # --- Marker ---
    mkdir -p /opt/proxy-pipeline
    echo "ready $(date '+%Y-%m-%d %H:%M:%S')" > /opt/proxy-pipeline/.status
  SCRIPT
}

# -----------------------------------------------
# Exit VPS
# -----------------------------------------------

resource "cherryservers_server" "exit" {
  project_id = var.cherry_project_id
  plan       = var.exit_plan
  region     = var.exit_region
  image      = var.exit_image
  hostname   = "exit-${var.exit_region}"

  ssh_key_ids = [cherryservers_ssh_key.proxy.id]
  user_data   = base64encode(local.user_data)

  tags = {
    role    = "exit-proxy"
    managed = "proxy-pipeline"
  }

  lifecycle {
    ignore_changes = [hostname]
  }
}

# -----------------------------------------------
# Outputs
# -----------------------------------------------

output "exit_ip" {
  value = cherryservers_server.exit.primary_ip
}

output "exit_id" {
  value = cherryservers_server.exit.id
}

output "panel_url" {
  value = "http://${cherryservers_server.exit.primary_ip}:${var.panel_port}${var.panel_path}"
}

output "ssh_command" {
  value = "ssh root@${cherryservers_server.exit.primary_ip}"
}
