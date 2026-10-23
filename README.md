# Readme

## Lambda runner capacity broker

The [lambda/](lambda/README.md) directory contains the GitHub App webhook, runner
pull API, health endpoint, and a standalone Terraform stack for their AWS
resources in `eu-central-1`. SSM SecureString values are managed separately.
Run Terraform from that directory; the root Terraform configuration manages KVM.

## Use the broker from a KVM host

Configure `/root/self-hosted-kvm.env` with the existing broker URL and pull key:

```shell
REG_TOKEN_LAMBDA_URL='https://your-function.lambda-url.eu-central-1.on.aws'
REG_TOKEN_LAMBDA_APIKEY='<existing-pull-api-key>'
RUNS_ON='ubuntu-latest-kong'
RUNNERGROUP='<existing-runner-group>'
DOCKER_USER='<docker-user>'
DOCKER_PASS='<docker-password>'
CPU=2
MEMORY=7168
```

Use `ubuntu-latest-arm64-kong` on ARM64 hosts. `RUNS_ON` selects exactly one
queue; when omitted, one supported target can be inferred from the existing
comma-separated `LABELS`. The URL may be the Function URL base or end in `/pull`.
Keep this file accessible only to root. Start the existing
`self-hosted-kvm@worker-*` services as described below.

`loop.sh` calls the broker directly from the existing Terraform loop when the
broker credentials are configured. The PAT path remains available otherwise.
It pulls only when the VM needs provisioning. An empty poll (`204`), a job-state
conflict (`409`), or any error is logged and followed by a random 2-8 second
retry delay. Conflict responses are not automatically acknowledged.

The default `/pull` wait budget is 60 seconds. Lambda repeats SQS polls of at
most 20 seconds and returns as soon as a message arrives; `204` means the total
wait expired without a message. Lambda has a 90-second timeout and the host uses
a 100-second HTTP timeout. To change the budget, set `pull_wait_seconds` in the
Lambda Terraform stack, and set host `BROKER_HTTP_TIMEOUT` above the resulting
Lambda timeout. Each blocked request occupies Lambda concurrency; size
`reserved_concurrency` above the total number of simultaneously polling slots,
with additional capacity for webhooks and health checks.
See [SQS long polling](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html)
and [Lambda timeouts](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html).

The full response is stored in a root-only JSON tfvars file and passed to
`runner_payload_b64`; it is never interpolated into shell code or put into an argv
or environment variable. Cloud-init receives base64 data and reads it with jq.
The host enforces a 256 KiB response limit. A 180 KiB payload is covered by the
integration tests. Base64 adds approximately one third to the size. This uses a
libvirt NoCloud ISO, so EC2's user-data limit does not apply. The full payload and
registration token are present in Terraform state and the cloud-init ISO;
protect those files as credentials.
See the [NoCloud data source](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).

The VM registers with `registration.url`, `registration.token`, the supplied
runner labels, and `--ephemeral`. Broker runner names use the existing
`<hostname>-<slot>` prefix followed by the broker's job ID and `trace_id`,
for example `self-hosted-runner-2-worker-1-120283397859-72d3162e-cc78-11e3-81ab-4c9367dc0958`.
The trace ID is the GitHub webhook's `X-GitHub-Delivery` GUID. The webhook
response, queued/pulled payload, Lambda logs, host pull log, and guest logs
carry the same value. Search this ID in CloudWatch Logs Insights to follow
the capacity request; SQS redelivery keeps the same trace and runner suffix.
The PAT path keeps its original static name. The runner group comes from the host's
`RUNNERGROUP` through Terraform's `runnergroup` variable; registration, metrics,
and logs all use that host setting. Lambda does not configure a runner group.
An empty `RUNNERGROUP` omits `--runnergroup`. Broker labels use
`--no-default-labels` because the payload already includes the complete label
set. The token is an ordinary registration token; it is not a JIT configuration
or a removal token. The old retry that registered without `--ephemeral` and
patched `.runner` afterward has been removed.

After Terraform successfully creates and starts the VM, the host calls `ack`.
Final Terraform failure calls `release`. Ack confirms VM startup handoff rather
than GitHub registration or job completion. Cloud-init and the existing
watchdogs handle the guest lifecycle. Each receive has a fixed 15-minute SQS
visibility window; there is no renewal process. The host calls `ack` on success
or `release` on failure, with at most five total attempts for transient failures.
HTTP 403, 429, and 503 wait 15-20 seconds between attempts. Network failures,
HTTP 408, and other 5xx responses use exponential delays of 2, 4, 8, and 16
seconds plus 0-3 seconds of jitter. Other HTTP errors fail immediately. After
exhausting attempts, the host logs the failure and leaves SQS to make the message
available again when the window expires. Empty/conflicting pulls retain the
existing 2-8 second retry delay in the outer loop.
An ack failure does not roll back or recreate a running VM. This assumes VM
provisioning normally finishes within 15 minutes and accepts possible extra
capacity if provisioning takes longer or acknowledgment fails. The pull API key
stays on the host. Configuration parsing and monitoring remain inside cloud-init.

The GHASR-92/95 registry cache configuration and the original GHASR-97 liveness,
stall, and provisioning watchdogs remain in cloud-init. Their diagnostic-log
checks and timeout conditions are unchanged. Monitoring reads repository/job/
workflow metadata from the payload; runner output is no longer filtered for
metadata. The existing completion hook reports job duration. `jobs.completed`
retains its original condition of a successful runner exit, and `runner_exit`
records the runner's own exit status. Datadog records are marked
`metadata_source: capacity_request`: GitHub can assign another matching queued
job to this runner, so the payload identifies the source capacity request and
does not bind the runner to an exact job.

Run the broker application and host/guest integration tests on Linux with jq,
Terraform, and the development Python dependencies:
The Terraform mock tests require Terraform 1.9 or later.

```shell
python -m pytest lambda/tests tests -q
terraform -chdir=lambda test
```

The integration tests render the actual Terraform template and exercise the
host with fake HTTP, Terraform, and libvirt commands. Real KVM boot and GitHub
registration require validation on a runner host.

## Prepare the base image

### Option 1: Download from other machine

Use rsync.

### Option2: Build on current machine

```shell
PACKER_VER=1.8.6
wget https://releases.hashicorp.com/packer/${PACKER_VER}/packer_${PACKER_VER}_linux_amd64.zip
unzip packer_${PACKER_VER}_linux_amd64.zip
mv packer /usr/local/bin/packer
rm packer_${PACKER_VER}_linux_amd64.zip

sudo apt install git -y
git clone https://github.com/fffonion/runner-images-kvm
git checkout main
# A docker user with any public repository pull access is preferred to overcome ratelimit.
DOCKERHUB_LOGIN=user DOCKERHUB_PASSWORD=pass ./build-new-image.sh    # build ubuntu 22.04
DOCKERHUB_LOGIN=user DOCKERHUB_PASSWORD=pass ./build-new-image.sh 24 # build ubuntu 24.04
# The output is saved under "output-custom_image/ubuntu-*.04"
``` 

## Provision

```shell
git clone https://github.com/fffonion/kvm-github-actions-runner /root/self-hosted-kvm
cd /root/self-hosted-kvm

./provision/setup-new-host.sh

cat << EOF > /root/self-hosted-kvm.env
GITHUB_TOKEN=<token with repo scope for repo runner, or admin:org for org runner>
OWNER=<org; owner/repo; enterprises/xx-inc >
LABELS=ubuntu-24.04-kong,ubuntu-latest-kong
RUNNERGROUP='awesome group'
RUNNER_VERSION=2.303.0
DOCKER_USER=<docker user that access to public registry, to increase pull rate limit only>
DOCKER_PASS=<the password>

CPU=2 # vCPU count, default to 2
MEMORY=7168 # memory in MB, default to 7168
EOF

# start the managing process as well the VMs
sudo systemctl start self-hosted-kvm@worker-{1,2,3,4,5,6,7,8}

# enable start at boot
sudo systemctl enable self-hosted-kvm@worker-{1,2,3,4,5,6,7,8}
```

Each VM has 2 vCPU and 7G RAM.

## Drain a node

```shell
touch /tmp/self-hosted-kvm-draining
```

After maintainance is finished:

```shell
rm /tmp/self-hosted-kvm-draining
```

## Useful debugging commands

systemd:

```shell
# restart all managing process (also restart running VMs)
sudo systemctl restart self-hosted-kvm@worker-*

# update terraform files (doesn't restart running VMs, affective on next boot)
# use conjunction of reload then restart to actually update VMs
sudo systemctl reload self-hosted-kvm@worker-*

# stop all managing process as well VMs
sudo systemctl stop self-hosted-kvm@worker-*
```

virsh:

```shell
# show running VMs
virsh -c qemu:///system list

# show all VMs
virsh -c qemu:///system list --all

# destroy (stop the qemu process)
virsh -c qemu:///system destroy $(hostname)-worker-1-runner

# undefine (remove from libvirt)
virsh -c qemu:///system undefine $(hostname)-worker-1-runner

# use the serial console; username and password both `ubuntu`
# might need to type Enter to show new shell prompt
virsh -c qemu:///system console $(hostname)-worker-1-runner

# display dhcp leases
virsh -c qemu:///system net-dhcp-leases kong
```
