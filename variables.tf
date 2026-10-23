variable "name" {
  type    = string
  default = "runner"
}

variable "labels" {
  type    = string
  default = ""
}

variable "runnergroup" {
  type    = string
  default = ""
}

variable "url" {
  type        = string
  description = "url to register the runner with, like https://github.com/owner/repo or https://github.com/org"
  default     = ""
}

variable "token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "runner_payload_b64" {
  description = "Base64-encoded complete broker response, passed through a protected tfvars file."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.runner_payload_b64 == "" || can(jsondecode(base64decode(var.runner_payload_b64)).registration.token)
    error_message = "runner_payload_b64 must encode JSON containing a registration token."
  }
}

variable "runner_version" {
  type    = string
  default = "2.301.1"
}

variable "docker_user" {
  type    = string
  default = ""
}

variable "docker_pass" {
  type      = string
  default   = ""
  sensitive = true
}

variable "arm64" {
  description = "apply ARM64 specific configs"
  type        = bool
  default     = false
}

variable "cpu" {
  type    = number
  default = 2
}

variable "memory" {
  type    = number
  default = 7168
}
