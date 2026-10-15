terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0, < 8.0"
    }
    # Response-policy bypass rules (behavior = "bypassResponsePolicy") are beta-only.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 7.0, < 8.0"
    }
  }
}
