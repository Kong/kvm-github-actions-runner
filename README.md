# Readme

## Prepare the base image

### Option 1: Download from other machine

TODO: use scp to copy from other existing machines for now

### Option2: Build on current machine

```shell
PACKER_VER=1.8.6
wget https://releases.hashicorp.com/packer/${PACKER_VER}/packer_${PACKER_VER}_linux_amd64.zip
unzip packer_${PACKER_VER}_linux_amd64.zip
mv packer /usr/local/bin/packer
rm packer_${PACKER_VER}_linux_amd64.zip

sudo apt install git -y
git clone https://github.com/fffonion/runner-images-kvm -b kvm /root/runner-images-kvm
cd /root/runner-images-kvm/images/linux
packer build ./ubuntu2204.pkr.hcl
mv output-custom_image/ubuntu-22.04 /root/ubuntu-22.04
# creates  output-custom_image/ubuntu-22.04
``` 

## Provision

```shell
git clone https://github.com/fffonion/kvm-github-actions-runner /root/self-hosted-kvm
cd /root/self-hosted-kvm

./ops/provision-new-host.sh

cat << EOF > /root/self-hosted-kvm.env
GITHUB_TOKEN=<token with repo scope for repo runner, or admin:org for org runner>
REPO=<owner/repo; leave empty for org>
LABELS=Hetzner,ubuntu-22.04-hetzner
RUNNER_VERSION=2.303.0
DOCKER_USER=<docker user that access to public registry, to increase pull rate limit only>
DOCKER_PASS=<the password>
EOF

# start the managing process as well the VMs
sudo systemctl start self-hosted-kvm@worker-{1,2,3,4,5,6,7,8}

# enable start at boot
sudo systemctl enable self-hosted-kvm@worker-{1,2,3,4,5,6,7,8}
```

Each VM has 2 vCPU and 4G RAM.

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
# restart all managing process (doesn't restart running VMs)
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
