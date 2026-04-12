terraform {
  required_version = ">= 1.5"
  required_providers {
    cherryservers = {
      source  = "cherryservers/cherryservers"
      version = "~> 1.5"
    }
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "~> 0.130"
    }
  }
}
