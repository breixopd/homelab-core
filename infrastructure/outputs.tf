output "machine_ips" {
  description = "Enabled machine addresses keyed by machine ID"
  sensitive   = true
  value = {
    for id, machine in var.machines : id => machine.address
    if machine.enabled
  }
}

output "machine_ids" {
  description = "Managed Proxmox VMIDs keyed by machine ID"
  value = merge(
    { for id, machine in proxmox_virtual_environment_container.machine : id => machine.vm_id },
    { for id, machine in proxmox_virtual_environment_vm.machine : id => machine.vm_id },
  )
}

output "machine_root_passwords" {
  description = "Generated root or cloud-init passwords keyed by machine ID"
  sensitive   = true
  value       = { for id, password in random_password.machine_root : id => password.result }
}

output "template_id" {
  description = "Provider-managed default LXC template ID"
  value       = one(proxmox_download_file.lxc_template[*].id)
}
