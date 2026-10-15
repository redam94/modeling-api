terraform {
  required_version = ">= 1.11"

  # Partial configuration: `terraform init -backend-config=backend.hcl` (bootstrap.sh writes it).
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.46"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 7.46"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}
