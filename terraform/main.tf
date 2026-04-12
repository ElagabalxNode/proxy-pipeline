# State migration: flat resources -> exit module (safe when old addresses absent)
moved {
  from = cherryservers_ssh_key.proxy
  to   = module.exit.cherryservers_ssh_key.proxy
}

moved {
  from = cherryservers_server.exit
  to   = module.exit.cherryservers_server.exit
}

module "exit" {
  source = "./modules/exit_cherry"

  cherry_api_token    = var.cherry_api_token
  cherry_project_id   = var.cherry_project_id
  exit_region         = var.exit_region
  exit_plan           = var.exit_plan
  exit_image          = var.exit_image
  ssh_public_key_path = var.ssh_public_key_path
  panel_port          = var.panel_port
  panel_user          = var.panel_user
  panel_pass          = var.panel_pass
  panel_path          = var.panel_path
}

module "bridge" {
  count  = var.enable_yandex_bridge ? 1 : 0
  source = "./modules/bridge_yandex"

  folder_id           = var.yandex_folder_id
  zone                = var.yandex_zone
  subnet_id           = var.yandex_subnet_id
  name_prefix         = var.yandex_instance_name_prefix
  ssh_public_key_path = var.ssh_public_key_path
  cores               = var.yandex_cores
  memory_gb           = var.yandex_memory_gb
  disk_size_gb        = var.yandex_disk_gb
}

resource "terraform_data" "yandex_bridge_inputs" {
  lifecycle {
    precondition {
      condition     = !var.enable_yandex_bridge || (var.yandex_folder_id != "" && var.yandex_subnet_id != "")
      error_message = "When enable_yandex_bridge=true, set yandex_folder_id and yandex_subnet_id. Authenticate Yandex provider via YC_TOKEN / service account (see docs/PIPELINE.md)."
    }
  }
}
