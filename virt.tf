resource "libvirt_volume" "master" {
  name = "${var.name}-master.qcow2"
  #base_volume_id = libvirt_volume.base_volume.id
  base_volume_name = "runner-ubuntu-24.04-${local.image_version}.qcow2"
  pool             = "kong"
}


# Define KVM domain to create
resource "libvirt_domain" "test" {
  name   = "${var.name}-runner"
  memory = var.memory
  vcpu   = var.cpu

  cpu {
    mode = "host-passthrough"
  }

  cloudinit = libvirt_cloudinit_disk.commoninit.id

  disk {
    volume_id = libvirt_volume.master.id
  }

  xml {
    # patch to use sata controller to compat in arm64
    # https://github.com/dmacvicar/terraform-provider-libvirt/issues/885
    xslt = <<-EOT
      <?xml version="1.0" ?>
      <xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">

        <xsl:template match="node()|@*">
          <xsl:copy>
            <xsl:apply-templates select="node()|@*"/>
          </xsl:copy>
        </xsl:template>

        <xsl:template match="/domain/features/acpi"/>
        <xsl:template match="/domain/features/apic"/>

        <xsl:template match="/domain/devices/controller[@type='ide']">
          <controller type='scsi' model='virtio-scsi' index='0'/>
        </xsl:template>

        <xsl:template match="/domain/devices/disk[@device='cdrom']/target">
          <target dev='sda' bus='scsi'/>
        </xsl:template>

        <xsl:template match="/domain/devices/disk[@device='cdrom']/address"/>

      </xsl:stylesheet>
    EOT
  }

  machine = var.arm64 ? "virt" : "s390-ccw-virtio"
  nvram {
    file     = var.arm64 ? "/usr/share/AAVMF/AAVMF_CODE.fd" : ""
    template = var.arm64 ? "flash1.img" : ""
  }

  # IMPORTANT: this is a known bug on cloud images, since they expect a console
  # we need to pass it
  # https://bugs.launchpad.net/cloud-images/+bug/1573095
  console {
    type        = "pty"
    target_port = "0"
    target_type = "serial"
  }

  console {
    type        = "pty"
    target_type = "virtio"
    target_port = "1"
  }

  graphics {
    type        = "spice"
    listen_type = "address"
    autoport    = true
  }


  network_interface {
    network_name = "kong" # List networks with virsh net-list
    hostname     = "${var.name}-runner"
  }
}
