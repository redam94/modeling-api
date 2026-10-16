# Mock values for `terraform test` (mock_provider "google" { source = ... }).
# Computed attributes get these shapes instead of random strings, because the provider
# validates some references (service account names, network ids) even under mocks.

mock_data "google_project" {
  defaults = {
    number = "111111111111"
  }
}

mock_data "google_kms_crypto_key_version" {
  defaults = {
    public_key = [{ pem = "-----BEGIN PUBLIC KEY-----\nMOCK\n-----END PUBLIC KEY-----\n", algorithm = "EC_SIGN_P256_SHA256" }]
  }
}

mock_data "google_storage_project_service_account" {
  defaults = {
    email_address = "service-111111111111@gs-project-accounts.iam.gserviceaccount.com"
  }
}

mock_resource "google_service_account" {
  defaults = {
    email = "mock-sa@mock-project.iam.gserviceaccount.com"
    name  = "projects/mock-project/serviceAccounts/mock-sa@mock-project.iam.gserviceaccount.com"
  }
}

mock_resource "google_compute_network" {
  defaults = {
    id        = "projects/mock-project/global/networks/mock-network"
    self_link = "https://www.googleapis.com/compute/v1/projects/mock-project/global/networks/mock-network"
  }
}

mock_resource "google_compute_subnetwork" {
  defaults = {
    id = "projects/mock-project/regions/europe-west4/subnetworks/mock-subnet"
  }
}

mock_resource "google_logging_project_sink" {
  defaults = {
    writer_identity = "serviceAccount:service-111111111111@gcp-sa-logging.iam.gserviceaccount.com"
  }
}

mock_resource "google_compute_security_policy" {
  defaults = {
    id        = "projects/mock-project/global/securityPolicies/mock-edge"
    self_link = "https://www.googleapis.com/compute/v1/projects/mock-project/global/securityPolicies/mock-edge"
  }
}
