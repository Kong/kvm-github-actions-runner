locals {
  amd64_previous_image_version = "20260907.300"
  amd64_image_version          = "20261026.354"
  arm64_previous_image_version = "20260907.300"
  arm64_image_version          = "20261026.166"

  image_version          = var.arm64 ? local.arm64_image_version : local.amd64_image_version
  previous_image_version = var.arm64 ? local.arm64_previous_image_version : local.amd64_previous_image_version

  runner_version = "2.338.2"
}
