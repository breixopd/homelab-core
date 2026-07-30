resource "random_password" "machine_root" {
  for_each = {
    for id, machine in var.machines : id => machine
    if machine.enabled && machine.managed
  }

  length           = 24
  override_special = "_%@"
  special          = true
}

locals {
  lxc_machines = {
    for id, machine in var.machines : id => machine
    if machine.enabled && machine.managed && machine.kind == "lxc"
  }
  vm_machines = {
    for id, machine in var.machines : id => machine
    if machine.enabled && machine.managed && machine.kind == "vm"
  }
  default_lxc_template_required = anytrue([
    for machine in values(local.lxc_machines) : machine.template_file_id == ""
  ])
}

resource "proxmox_download_file" "lxc_template" {
  count = local.default_lxc_template_required ? 1 : 0

  content_type        = "vztmpl"
  datastore_id        = var.lxc_template_datastore
  node_name           = var.proxmox_node
  url                 = var.lxc_template_url
  checksum            = var.lxc_template_checksum
  checksum_algorithm  = "sha256"
  overwrite           = true
  overwrite_unmanaged = true
  upload_timeout      = 1800
}

resource "proxmox_virtual_environment_container" "machine" {
  for_each = local.lxc_machines

  node_name    = var.proxmox_node
  vm_id        = each.value.vmid
  description  = each.value.description
  unprivileged = true

  features {
    nesting = each.value.nesting
  }

  cpu {
    cores = each.value.cores
  }

  memory {
    dedicated = each.value.memory_mb
  }

  disk {
    datastore_id = each.value.root_datastore != "" ? each.value.root_datastore : var.default_datastore
    size         = each.value.root_disk_gb
  }

  dynamic "mount_point" {
    for_each = each.value.data_disks
    content {
      volume = mount_point.value.datastore != "" ? mount_point.value.datastore : var.default_datastore
      size   = "${mount_point.value.size_gb}G"
      path   = mount_point.value.path
      backup = mount_point.value.backup
    }
  }

  initialization {
    hostname = each.value.hostname

    dynamic "ip_config" {
      for_each = each.value.public_bridge == "" ? [] : [true]
      content {
        ipv4 {
          address = "dhcp"
        }
      }
    }

    ip_config {
      ipv4 {
        address = "${each.value.address}/${each.value.cidr}"
        gateway = each.value.gateway
      }
    }

    user_account {
      keys     = [var.ssh_public_key]
      password = random_password.machine_root[each.key].result
    }
  }

  dynamic "network_interface" {
    for_each = each.value.public_bridge == "" ? [] : [true]
    content {
      name   = "veth0"
      bridge = each.value.public_bridge
    }
  }

  network_interface {
    name   = each.value.public_bridge == "" ? "veth0" : "veth1"
    bridge = each.value.private_bridge
  }

  operating_system {
    template_file_id = each.value.template_file_id != "" ? each.value.template_file_id : proxmox_download_file.lxc_template[0].id
    type             = "debian"
  }

  wait_for_ip {
    ipv4 = true
  }

  startup {
    order      = each.value.startup_order
    up_delay   = 30
    down_delay = 60
  }

  lifecycle {
    prevent_destroy = var.allow_destroy ? false : true
    ignore_changes  = [features, device_passthrough]
  }
}

resource "proxmox_download_file" "vm_image" {
  for_each = local.vm_machines

  content_type       = "import"
  datastore_id       = each.value.cloud_image_datastore
  node_name          = var.proxmox_node
  url                = each.value.cloud_image_url
  file_name          = "homelab-${each.key}-cloud.${each.value.cloud_image_format}"
  checksum           = each.value.cloud_image_sha256
  checksum_algorithm = "sha256"
  overwrite          = false
}

resource "proxmox_virtual_environment_vm" "machine" {
  for_each = local.vm_machines

  name        = each.value.hostname
  node_name   = var.proxmox_node
  vm_id       = each.value.vmid
  description = each.value.description
  started     = true

  cpu {
    cores = each.value.cores
  }

  memory {
    dedicated = each.value.memory_mb
  }

  serial_device {
    device = "socket"
  }

  disk {
    datastore_id = each.value.root_datastore != "" ? each.value.root_datastore : var.default_datastore
    import_from  = proxmox_download_file.vm_image[each.key].id
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
    size         = each.value.root_disk_gb
  }

  dynamic "disk" {
    for_each = each.value.data_disks
    iterator = data_disk
    content {
      datastore_id = data_disk.value.datastore != "" ? data_disk.value.datastore : var.default_datastore
      interface    = "scsi${data_disk.key + 1}"
      iothread     = true
      discard      = "on"
      size         = data_disk.value.size_gb
    }
  }

  initialization {
    datastore_id = each.value.root_datastore != "" ? each.value.root_datastore : var.default_datastore

    dynamic "ip_config" {
      for_each = each.value.public_bridge == "" ? [] : [true]
      content {
        ipv4 {
          address = "dhcp"
        }
      }
    }

    ip_config {
      ipv4 {
        address = "${each.value.address}/${each.value.cidr}"
        gateway = each.value.gateway
      }
    }

    user_account {
      username = each.value.admin_user
      keys     = [var.ssh_public_key]
      password = random_password.machine_root[each.key].result
    }
  }

  dynamic "network_device" {
    for_each = each.value.public_bridge == "" ? [] : [true]
    content {
      bridge = each.value.public_bridge
      model  = "virtio"
    }
  }

  network_device {
    bridge = each.value.private_bridge
    model  = "virtio"
  }

  startup {
    order      = each.value.startup_order
    up_delay   = 30
    down_delay = 60
  }

  delete_unreferenced_disks_on_destroy = false
  stop_on_destroy                      = false

  lifecycle {
    prevent_destroy = var.allow_destroy ? false : true
  }
}
