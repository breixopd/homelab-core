variable "proxmox_api_url" {
  description = "Proxmox API endpoint"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token"
  type        = string
  sensitive   = true
}

variable "proxmox_node" {
  description = "Target Proxmox cluster node"
  type        = string
}

variable "lxc_template_url" {
  description = "Pinned source URL for the default LXC template"
  type        = string
}

variable "lxc_template_checksum" {
  description = "Expected SHA-256 checksum for the default LXC template"
  type        = string

  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.lxc_template_checksum))
    error_message = "LXC template checksum must be a lowercase SHA-256 digest."
  }
}

variable "lxc_template_datastore" {
  description = "Proxmox datastore that accepts vztmpl content"
  type        = string
}

variable "default_datastore" {
  description = "Fallback datastore for machine disks"
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key injected into managed machines"
  type        = string
}

variable "machines" {
  description = "Machine plugin instances keyed by arbitrary machine ID"
  type = map(object({
    kind           = string
    enabled        = bool
    managed        = bool
    hostname       = string
    address        = string
    vmid           = number
    description    = string
    labels         = list(string)
    cores          = number
    memory_mb      = number
    root_disk_gb   = number
    root_datastore = string
    data_disks = list(object({
      path      = string
      size_gb   = number
      datastore = string
      backup    = bool
    }))
    private_bridge        = string
    public_bridge         = string
    gateway               = string
    cidr                  = number
    startup_order         = number
    nesting               = bool
    keyctl                = bool
    fuse                  = bool
    template_file_id      = string
    admin_user            = string
    ssh_user              = string
    ssh_port              = number
    cloud_image_datastore = string
    cloud_image_format    = string
    cloud_image_url       = string
    cloud_image_sha256    = string
  }))

  validation {
    condition     = alltrue([for machine in values(var.machines) : contains(["lxc", "vm"], machine.kind)])
    error_message = "Machine kind must be lxc or vm."
  }

  validation {
    condition = alltrue([
      for machine in values(var.machines) : machine.kind != "vm" || !machine.managed || (
        machine.admin_user != "" &&
        machine.cloud_image_datastore != "" &&
        contains(["qcow2", "raw"], machine.cloud_image_format) &&
        can(regex("^https?://", machine.cloud_image_url)) &&
        can(regex("^[a-f0-9]{64}$", machine.cloud_image_sha256))
      )
    ])
    error_message = "Managed VMs require an admin user and a checksum-pinned HTTP(S) qcow2/raw image on an import-enabled datastore."
  }
}

variable "allow_destroy" {
  description = "Allow managed machine destruction"
  type        = bool
  default     = false
}
