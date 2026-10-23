#!/bin/bash -e
umask 077

function echoerr() {
    printf '%s %s\n' "ERROR" "$@" 1>&2;
}

function checkdrain() {
	if [[ -e /tmp/self-hosted-kvm-draining ]]; then
		echo "Draining, not starting new VMs"
		sleep 30
		exit 0
	fi
}

dd_host="10.1.0.1"

function send_metrics() {
	metrics="github.actions.$1:${2:-1}|${3:-c}|#runner_group:${RUNNERGROUP}"
	if [[ ! -z "$4" ]]; then metrics="$metrics,$4"; fi
	echo "Send metrics $metrics to $dd_host"
	echo -n "$metrics" > /dev/udp/$dd_host/8125
}

source /root/self-hosted-kvm.env && echo "Reloaded env vars" || true

broker_enabled=false
if [[ -n $REG_TOKEN_LAMBDA_URL && -n $REG_TOKEN_LAMBDA_APIKEY ]]; then
    broker_enabled=true
    broker_url=$REG_TOKEN_LAMBDA_URL
    broker_url=${broker_url%/}
    [[ $broker_url == */pull ]] || broker_url=$broker_url/pull
    RUNS_ON=${RUNS_ON:-$(printf '%s' "$LABELS" | tr ',' '\n' | grep -Ex 'ubuntu-latest-kong|ubuntu-latest-arm64-kong')}
    case "$RUNS_ON" in
        ubuntu-latest-kong|ubuntu-latest-arm64-kong) ;;
        *) echoerr 'Set RUNS_ON to exactly one supported label'; exit 1 ;;
    esac
fi

function broker_backoff() {
    delay=$((2 + RANDOM % 7))
    echo "Retrying in $delay seconds"
    sleep "$delay"
}

function broker_action() {
    local code curl_status attempt delay
    for ((attempt=1; attempt<=5; attempt++)); do
        code=$(jq -c --arg action "$1" \
            '{action: $action, "runs-on": .["runs-on"], receipt_handle: .lease.receipt_handle,
              trace_id: (.trace_id // .delivery_id // .lease.message_id)}' "$payload_file" |
            curl -sS --connect-timeout 5 --max-time 100 -H "apikey: $REG_TOKEN_LAMBDA_APIKEY" \
                -H 'Content-Type: application/json' --data-binary @- -o /dev/null -w '%{http_code}' "$broker_url")
        curl_status=$?
        [[ $curl_status -eq 0 && $code == 204 ]] && return 0
        echoerr "Broker $1 failed: HTTP $code, curl $curl_status (attempt $attempt/5)"
        [[ $attempt -lt 5 ]] || return 1
        case "$code" in
            403|429|503) delay=$((15 + RANDOM % 6)) ;;
            *)
                [[ $curl_status -ne 0 || $code == 408 || $code == 5?? ]] || return 1
                delay=$((2 ** attempt + RANDOM % 4))
                ;;
        esac
        echo "Retrying broker $1 in $delay seconds"
        sleep "$delay"
    done
}

function broker_pull() {
    local code
    code=$(jq -nc --arg runs_on "$RUNS_ON" '{"runs-on": $runs_on}' |
        curl -sS --connect-timeout 5 --max-time 100 -H "apikey: $REG_TOKEN_LAMBDA_APIKEY" \
            -H 'Content-Type: application/json' --data-binary @- -o "$payload_file" -w '%{http_code}' "$broker_url")
    if [[ $code != 200 ]]; then
        echo "Broker pull: HTTP $code"
        return 1
    fi
    [[ $(wc -c < "$payload_file") -le 262144 ]] || { echoerr 'Payload exceeds 256 KiB'; return 1; }
    jq -e '.registration.token | type == "string" and length > 0' "$payload_file" >/dev/null || return 1
    jq -c '{repo: .repository.full_name, job: .job.name, job_id: .job.id, trace_id: .trace_id}' "$payload_file"
    # Use a file so large base64 values never enter argv or the environment.
    jq -Rs '{runner_payload_b64: (. | @base64)}' "$payload_file" > "$payload_varfile"
}

if [[ $broker_enabled == true ]]; then
    echo "Using Lambda runner broker"
elif [[ -n $GITHUB_TOKEN ]]; then
	echo "Using PAT to get reg token"
else
	echoerr '$GITHUB_TOKEN, or $REG_TOKEN_LAMBDA_URL and $REG_TOKEN_LAMBDA_APIKEY is required'
	exit 1
fi

if [[ -z $NAME ]]; then
	echoerr '$NAME is required'
	exit 1
fi

urlvar="https://github.com/$OWNER"
namevar="$(hostname)-$NAME"


mkdir -p /root/vms
workdir=/root/vms/self-hosted-kvm-tf-$NAME
statedir=/root/vms/self-hosted-kvm-tf-$NAME.state
mkdir -p $statedir
if [[ -e $workdir/terraform.tfstate ]]; then
	cp $workdir/terraform.tfstate* $statedir
fi

reload_file=/tmp/self-hosted-kvm@${namevar}.reload
tfvarfile="/root/vms/self-hosted-kvm-tf-$NAME.tfvars"

cat > $tfvarfile <<EOF
url="$urlvar"
docker_user="$DOCKER_USER"
docker_pass="$DOCKER_PASS"
name="$namevar"
labels="$LABELS"
runnergroup="$RUNNERGROUP"

EOF

tf_args="-var-file $tfvarfile"
if [[ $broker_enabled == true ]]; then
    payload_file="$statedir/payload.json"
    payload_varfile="$statedir/payload.tfvars.json"
    [[ -f $payload_varfile ]] || printf '{"runner_payload_b64":""}' > "$payload_varfile"
    tf_args="$tf_args -var-file $payload_varfile"
fi

if [[ $(arch) == "aarch64" ]]; then
	tf_args="$tf_args -var arm64=true"
fi

if [[ ! -z $CPU ]]; then
	tf_args="$tf_args -var cpu=$CPU"
fi

if [[ ! -z $MEMORY ]]; then
	tf_args="$tf_args -var memory=$MEMORY"
fi

function do_cleanup() {
       set -x
       if [[ $(arch) == "aarch64" ]]; then
           undefine_args="--nvram"
       fi

       virsh -c qemu:///system destroy ${namevar}-runner
       virsh -c qemu:///system undefine $undefine_args ${namevar}-runner

       virsh vol-delete ${namevar}-commoninit.iso kong
       virsh vol-delete ${namevar}-master.iso kong

       terraform destroy -lock-timeout=30s -auto-approve $tf_args -var token=x
       set +x
}

if [[ "$1" == "stop" ]]; then
	pushd $workdir
	echo "Stopping the VM..."
        set +e
	do_cleanup
	exit 0
fi

rm $workdir/*.* || true
mkdir -p $workdir
cp -r $(dirname $(readlink -f $0))/* $workdir/
pushd $workdir

rm terraform.tfstate* -f
if [[ -e $statedir/terraform.tfstate ]]; then
	cp $statedir/terraform.tfstate* $workdir/
fi

terraform init -upgrade

if [[ "$1" == "reload" ]]; then
        touch $reload_file
	exit 0
fi

checkdrain

if [[ $OWNER == "enterprises/"* ]]; then
	url=https://api.github.com/${OWNER}/actions/runners/registration-token
elif [[ $OWNER == *"/"* ]]; then
	url=https://api.github.com/repos/${OWNER}/actions/runners/registration-token
else
	url=https://api.github.com/orgs/${OWNER}/actions/runners/registration-token
fi

# remove the -e flag, in case we hit a bug, we don't want to just kill the vm
set +e

while true; do
	token_start=$(date +%s)
	token_expire=$((token_start + 1700))
	token_method=""
	if [[ $broker_enabled == true ]]; then
		reg_token=x
		token_method="lambda"

	elif [[ -n "$GITHUB_TOKEN" ]]; then
		reg_token_ret=$(curl \
		  -s \
		  -X POST \
		  -H "Accept: application/vnd.github+json" \
		  -H "Authorization: Bearer $GITHUB_TOKEN"\
		  -H "X-GitHub-Api-Version: 2022-11-28" \
		  $url)

		reg_token=$(echo "$reg_token_ret" | jq -r .token)
		token_method="PAT"

	else
		echoerr "Unable to use either lambda or PAT to get token?"
		exit 1
	fi


	if [[ -z $reg_token || $reg_token == "null" ]]; then
		echoerr "Unable to get registration token using $token_method, error was $reg_token_ret"
                send_metrics runners.anomaly "1" "c" "#runner_name:${namevar},#type:get_token_failed" 
		exit 1
	fi

	echo "Reg token is obtained using $token_method"

        need_respawn=0
	while [[ $(date +%s) -lt $token_expire ]]; do
                # check reload flag
                if [[ -e $reload_file ]]; then
                    rm -f $reload_file
                    # respawn
                    echo "Reloading loop.sh"
                    exec $0
                fi


                # check terraform state
		if [[ ! -z $(terraform state list) ]]; then
			plan=$(timeout 10 terraform plan $tf_args -var token=$reg_token -detailed-exitcode)
			# we only re-apply when instance exists/job finishes
			# also ignore timeouts
			# mark as respawn if 1) we change from stopped to running, or 2) first time
			if [[ $? -ne 0 && (
					! -z $(echo "$plan" | grep "running" | grep "false") ||
					! -z $(echo "$plan" | grep "libvirt_domain.test" |grep "will be created") ||
					! -z $(echo "$plan" | grep "Error: error while retrieving remote ISO")
				) ]]; then

				need_respawn=1
			fi
                else # not created
                    need_respawn=1
                fi

                # Health checking now lives inside the VM, in cloud-init.sh.tmpl.
                # The IRQ heuristic that used to sit here was x86_64-only and
                # never fired once (GHASR-97, INC-849: 36h of journal on both
                # kuma hosts contained zero anomaly metrics), because it could
                # not distinguish a wedged VM from an idle one. The in-VM
                # watchdog powers the VM off instead, which surfaces here as
                # running=false and is rebuilt by the branch below — the same
                # path a normally finished job takes.

                if [[ $need_respawn -eq 1 ]]; then
		    # check drain flag
		    checkdrain

                    if [[ $broker_enabled == true ]] && ! broker_pull; then
                        broker_backoff
                        continue
                    fi

                    echo "Reprovisioning the VM..."
                    terraform taint libvirt_volume.master || true
                    
                    # prepare new nvram overlay for uefi used by aarch64
                    if [[ -e /usr/share/AAVMF/AAVMF_CODE.fd ]]; then
                            sudo cp /usr/share/AAVMF/AAVMF_CODE.fd flash1.img
                    fi

                    set -x
                    terraform apply -auto-approve $tf_args -var token=$reg_token || (
                        send_metrics runners.anomaly "1" "c" "#runner_name:${namevar},#type:vm_tfstate_broken";
                        do_cleanup;
                        terraform apply -auto-approve $tf_args -var token=$reg_token
                    )
                    apply_status=$?
                    set +x
                    if [[ $broker_enabled == true ]]; then
                        if [[ $apply_status -eq 0 ]]; then
                            broker_action ack || broker_backoff
                        else
                            broker_action release || true
                            broker_backoff
                            continue
                        fi
                    fi
                    need_respawn=0

                    sleep 5
                fi

                sleep 5
	done

	sleep 30
done

echoerr "Should not reach here"
