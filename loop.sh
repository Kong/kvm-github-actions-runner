#!/bin/bash -e

function echoerr() {
    printf '%s %s\n' "ERROR" "$@" 1>&2;
}

if [[ -n $REG_TOKEN_LAMBDA_URL && -n $REG_TOKEN_LAMBDA_APIKEY ]]; then
	echoerr "Using lambda to get reg token"
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

if [[ -z $RUNNER_VERSION ]]; then
	echoerr '$RUNNER_VERSION is required'
	exit 1
fi

repovar=""
if [[ ! -z $REPO ]]; then
	repovar=https://github.com/$REPO
fi
namevar="$(hostname)-$NAME"

mkdir -p /root/vms
workdir=/root/vms/self-hosted-kvm-tf-$NAME
statedir=/root/vms/self-hosted-kvm-tf-$NAME.state
mkdir -p $statedir
if [[ -e $workdir/terraform.tfstate ]]; then
	cp $workdir/terraform.tfstate* $statedir
fi
rm -rf $workdir/*
mkdir -p $workdir
cp -r $(dirname $(readlink -f $0))/* $workdir/
pushd $workdir

rm terraform.tfstate* -f
if [[ -e $statedir/terraform.tfstate ]]; then
	cp $statedir/terraform.tfstate* $workdir
fi

terraform init -upgrade

tf_args="-var repo=$repovar -var runner_version=$RUNNER_VERSION -var docker_user=$DOCKER_USER -var docker_pass=$DOCKER_PASS -var name=$namevar -var labels=$LABELS"

if [[ "$1" == "stop" ]]; then
	echo "Stopping the VM..."
	terraform destroy -auto-approve $tf_args -var token=$reg_token
	exit 0
elif [[ "$1" == "reload" ]]; then
	exit 0
fi

if [[ -e /tmp/self-hosted-kvm-draining ]]; then
	echo "Draining, not starting new VMs"
	sleep 30
	exit 0
fi

if [[ ! -z $ORG ]]; then
	url=https://api.github.com/orgs/${ORG}/actions/runners/registration-token
elif [[ ! -z $REPO ]]; then
	url=https://api.github.com/repos/${REPO}/actions/runners/registration-token
else
	echoerr 'Neither $ORG nor $REPO is defined'
	exit 1
fi

# remove the -e flag, in case we hit a bug, we don't want to just kill the vm
set +e

while true; do
	token_start=$(date +%s)
	token_expire=$((token_start + 1700))
	token_method=""
	if [[ -n "$REG_TOKEN_LAMBDA_URL" && -n "$REG_TOKEN_LAMBDA_APIKEY" ]]; then
		reg_token_ret=$(curl \
		  -s \
		  $REG_TOKEN_LAMBDA_URL \
		  -H "apikey: $REG_TOKEN_LAMBDA_APIKEY"
		)

		reg_token=$(echo "$reg_token_ret" | jq -r .join_token)
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
		exit 1
	fi

	echo "Reg token is obtained using $token_method: $reg_token"

	while [[ $(date +%s) -lt $token_expire ]]; do
		while [[ ! -z $(terraform state list) ]]; do
			plan=$(timeout 10 terraform plan $tf_args -var token=$reg_token -detailed-exitcode)
			# we only re-apply when instance exists/job finishes
			# also ignore timeouts
			if [[ $? -ne 0 && ! $(echo "$plan"|grep running|grep -q false) ]]; then
				break
			fi
			sleep 5
		done

		echo "Reprovisioning the VM..."
		terraform taint libvirt_volume.master || true
		terraform apply -auto-approve $tf_args -var token=$reg_token
		old_token=$reg_token

		sleep 5

                # don't cache the token if it's returned by lambda: it's already cached
                if [[ $token_method == "lambda" ]]; then
                   break
                fi
	done
done

echoerr "Should not reach here"
