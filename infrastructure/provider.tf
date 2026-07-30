terraform {
  required_version = ">= 1.12.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # Local backend stores state on disk.
  # SECURITY: State contains sensitive data (Proxmox API tokens, LXC IPs).
  # Protect via filesystem encryption (LUKS, ecryptfs) or use a remote backend.
  #
  # PRODUCTION WARNING: The local backend does NOT support state locking.
  # When multiple users or CI runners apply infrastructure concurrently,
  # state corruption can occur. For production deployments, switch to a
  # remote backend that supports locking (S3 with DynamoDB, GCS, Azure
  # Storage, or Terraform Cloud). See backend.tf.example for S3/GCS configs.
  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = var.proxmox_api_token
  insecure  = false
}
