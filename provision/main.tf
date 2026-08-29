terraform {
  required_providers {
    libvirt = {
      source = "dmacvicar/libvirt"
      version = "=0.7.6"
    }
  }
}

provider "libvirt" {
  uri = "qemu:///system"
}

resource "libvirt_volume" "ubuntu_2404_base_volumes" {
  for_each = toset([local.image_version])
  name     = "runner-ubuntu-24.04-${each.key}.qcow2"
  source   = "/root/ubuntu-24.04-${each.key}"
  format   = "qcow2"
  pool     = libvirt_pool.kong.name
}

resource "libvirt_pool" "kong" {
  name = "kong"
  type = "dir"
  path = "/var/lib/libvirt/images"
}

resource "libvirt_network" "kong" {
  name   = "kong"
  mode   = "nat"
  domain = "ci.konghq.com.internal"

  dhcp {
    enabled = true
  }

  dns {
    enabled = true
    local_only = false

    hosts {
      hostname = "archive.ubuntu.com"
      ip       = "37.27.33.247"
    }
    hosts {
      hostname = "security.ubuntu.com"
      ip       = "37.27.33.247"
    }
    hosts {
      hostname = "ports.ubuntu.com"
      ip       = "37.27.33.247"
    }

    # GHASR-92: point the ghcr pull-through cache hostname at the host gateway
    # (10.1.0.1). The host runs a registry:2 proxy bound to 10.1.0.1:5556, so
    # once the fleet-wide vars.GHCR_REGISTRY is set to registry-ghcr.internal:5556,
    # runner VMs pull ghcr images over the internal NAT network instead of
    # saturating public egress. Setting that variable to ghcr.io bypasses the cache.
    hosts {
      hostname = "registry-ghcr.internal"
      ip       = "10.1.0.1"
    }

    # GHASR-95: same idea for Docker Hub, second registry:2 proxy on 10.1.0.1:5555.
    # This one is wired via the VM's daemon.json registry-mirrors (cloud-init.sh.tmpl)
    # rather than by rewriting image names, so it needs no CI-side switch — which
    # also means removing this entry alone does not disable it. To bypass the cache,
    # drop registry-mirrors from the VM daemon.json.
    hosts {
      hostname = "registry-dockerhub.internal"
      ip       = "10.1.0.1"
    }
  }

  addresses = ["10.1.0.0/24"]

  autostart = true

  xml {
    # patch to use disallow networking between guests
    xslt = file("patch-network-isolated.xsl")
  }
}

