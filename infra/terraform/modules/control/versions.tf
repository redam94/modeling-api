terraform {
  # Write-only arguments (password_wo, secret_data_wo) and ephemeral resources need 1.11+:
  # database passwords and generated secrets never enter the state or a plan file.
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0, < 8.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 7.0, < 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.7, < 4.0"
    }
  }
}
