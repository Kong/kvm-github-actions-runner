# The cloud-init provider renders the actual multipart data; libvirt is mocked.
mock_provider "libvirt" {}

run "broker_payload_without_guest_mounts" {
  command = apply

  variables {
    name        = "test-slot"
    runnergroup = "Host Runners"
    runner_payload_b64 = base64encode(jsonencode({
      schema_version = 2
      capacity_key   = "456:300:100:1"
      repository     = { full_name = "example-org/repo" }
      job            = { id = 100, name = "build" }
      registration = {
        url         = "https://github.com/example-org"
        token       = "test-registration-token"
        runner_name = "capacity-100-unique"
        labels      = ["self-hosted", "Linux", "X64", "ubuntu-latest-kong"]
        ephemeral   = true
      }
    }))
  }

  assert {
    condition = (
      !strcontains(data.cloudinit_config.main.rendered, "CONFIG_B64") &&
      strcontains(data.cloudinit_config.main.rendered, "runner-config.json") &&
      jsondecode(base64decode(regex("<<'CONFIG'\n([^\n]+)\nCONFIG", data.cloudinit_config.main.rendered)[0])).settings.runner_group == "Host Runners" &&
      jsondecode(base64decode(regex("<<'CONFIG'\n([^\n]+)\nCONFIG", data.cloudinit_config.main.rendered)[0])).settings.runner_name == "test-slot" &&
      !strcontains(data.cloudinit_config.main.rendered, "REG_TOKEN_LAMBDA_APIKEY") &&
      length(libvirt_domain.test.filesystem) == 0 &&
      jsondecode(base64decode(regex("<<'CONFIG'\n([^\n]+)\nCONFIG", data.cloudinit_config.main.rendered)[0])).settings.runner_name == var.name &&
      !strcontains(data.cloudinit_config.main.rendered, "9pnet_virtio")
    )
    error_message = "Render the base64 configuration inside cloud-init without additional mounts."
  }
}

run "legacy_pat_inputs" {
  command = apply

  variables {
    url         = "https://github.com/example-org"
    token       = "test-legacy-token"
    labels      = "ubuntu-latest-kong"
    runnergroup = "Default"
  }

  assert {
    condition = (
      length(libvirt_domain.test.filesystem) == 0 &&
      strcontains(data.cloudinit_config.main.rendered, "GHASR-92 / GHASR-95") &&
      strcontains(data.cloudinit_config.main.rendered, "GHASR-97")
    )
    error_message = "Existing direct inputs must render without additional guest mounts."
  }
}
