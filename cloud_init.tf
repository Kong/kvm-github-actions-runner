resource "libvirt_cloudinit_disk" "commoninit" {
  name      = "${var.name}-commoninit.iso"
  user_data = data.cloudinit_config.main.rendered

  pool = "kong"
}


data "cloudinit_config" "main" {
  gzip          = false
  base64_encode = false

  part {
    filename     = "init.cfg"
    content_type = "text/cloud-config"
    content = templatefile("${path.module}/cloud-init.yml",
    { NAME = var.name })
  }

  part {
    content_type = "text/x-shellscript"
    content = templatefile(
      "${path.module}/cloud-init.sh.tmpl",
      {
        CONFIG_B64 = base64encode(jsonencode({
          payload = jsondecode(var.runner_payload_b64 != "" ? base64decode(var.runner_payload_b64) : jsonencode({
            capacity_key = ""
            repository   = { full_name = "" }
            job          = {}
            registration = {
              url         = var.url
              token       = var.token
              runner_name = var.name
              labels      = compact(split(",", var.labels))
              ephemeral   = true
            }
          }))
          settings = {
            runner_name    = var.name
            runner_group   = var.runnergroup
            docker_user    = var.docker_user
            docker_pass    = var.docker_pass
            runner_version = local.runner_version
          }
        }))
      }
    )
  }
}

