"""Exercise host transport, complete payload handoff, and rendered guest bootstrap."""

import base64
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
TF = shutil.which("terraform") or str(Path.home() / ".cache/kvm-runner-tools/terraform")
ISO_TOOLS = Path.home() / ".cache/kvm-runner-tools/genisoimage/usr/bin"


def payload():
    return {
        "schema_version": 2, "capacity_key": "456:300:100:1", "organization": "example-org", "installation_id": 456,
        "runs-on": "ubuntu-latest-kong", "repository": {"full_name": "example-org/repo"},
        "job": {"id": 100, "run_id": 200, "name": "build", "workflow_name": "CI"},
        "registration": {"scope": "organization", "url": "https://github.com/example-org",
                         "token": "secret-registration-token", "expires_at": "2099-01-01T00:00:00Z",
                         "runner_name": "capacity-100-unique",
                         "labels": ["self-hosted", "Linux", "X64", "ubuntu-latest-kong"], "ephemeral": True},
        "lease": {"receipt_handle": "secret-receipt", "visibility_timeout_seconds": 900},
    }


def bash(script, env=None, cwd=None, check=True):
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env={**os.environ, **(env or {})}, cwd=cwd, check=check)


def render(tmp_path, value, runner_group=""):
    if not Path(TF).is_file():
        pytest.skip("Terraform is required to render the guest template")
    values = {"CONFIG_B64": base64.b64encode(json.dumps({
        "payload": value, "settings": {"runner_version": "2.337.0", "runner_group": runner_group,
                                     "runner_name": "test-host-worker-1"}
    }).encode()).decode()}
    values_file = tmp_path / "template-values.json"
    values_file.write_text(json.dumps(values))
    expression = f'base64encode(templatefile({json.dumps(str(ROOT / "cloud-init.sh.tmpl"))}, jsondecode(file({json.dumps(str(values_file))}))))'
    result = subprocess.run([TF, "console"], input=expression, cwd=tmp_path,
                            text=True, capture_output=True, check=True)
    return base64.b64decode(json.loads(result.stdout)).decode()


@pytest.mark.parametrize("runs_on", ["ubuntu-latest-kong", "ubuntu-latest-arm64-kong"])
@pytest.mark.parametrize("jq_version", ["system", "1.6"])
def test_large_payload_survives_tfvars_and_template_without_shell_evaluation(tmp_path, runs_on, jq_version):
    env = {}
    if jq_version == "1.6":
        jq = Path.home() / ".cache/kvm-runner-tools/jq-1.6"
        if not jq.is_file():
            pytest.skip("Cached jq 1.6 is required for the minimum-version compatibility check")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin/jq").symlink_to(jq)
        env["PATH"] = str(tmp_path / "bin") + ":" + os.environ["PATH"]
    value = payload()
    value["runs-on"] = runs_on
    value["job"]["name"] = "build ' $(touch SHOULD_NOT_EXIST) ${HOME}\nUnicode: \u6d4b\u8bd5"
    value["extension"] = "x" * 180_000
    source = tmp_path / "payload.json"
    source.write_text(json.dumps(value))
    tfvars = tmp_path / "runner.tfvars.json"
    function = (ROOT / "loop.sh").read_text().split("function broker_pull() {", 1)[1].split("\n}\n", 1)[0]
    function = "function broker_pull() {" + function + "\n}"
    result = bash(f"""
        echoerr() {{ echo "$*" >&2; }}
        payload_file='{source}'
        payload_varfile='{tfvars}'
        RUNS_ON={runs_on}
        broker_url=https://example.invalid/pull
        REG_TOKEN_LAMBDA_APIKEY=test-key
        curl() {{
            request=$(cat)
            [[ $(printf '%s' "$request" | jq -r '.["runs-on"]') == "$RUNS_ON" ]] || return 1
            printf 200
        }}
        {function}
        broker_pull
    """, cwd=tmp_path, env=env)
    assert result.stderr == ""
    transferred = base64.b64decode(json.loads(tfvars.read_text())["runner_payload_b64"])
    assert json.loads(transferred) == value
    rendered = render(tmp_path, json.loads(transferred))
    guest = tmp_path / "guest.sh"
    guest.write_text(rendered)
    subprocess.run(["bash", "-n", guest], check=True)
    embedded = re.search(r"<<'CONFIG'\n(.*?)\nCONFIG", rendered, re.S).group(1)
    assert json.loads(base64.b64decode(embedded))["payload"] == value
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("payload_bytes", [None, 262144])
def test_actual_terraform_multipart_and_nocloud_iso_round_trip(tmp_path, payload_bytes):
    mkisofs = shutil.which("mkisofs") or str(ISO_TOOLS / "genisoimage")
    isoinfo = shutil.which("isoinfo") or str(ISO_TOOLS / "isoinfo")
    if not all(Path(tool).is_file() for tool in (TF, mkisofs, isoinfo)):
        pytest.skip("Terraform, mkisofs, and isoinfo are required for the ISO test")
    value = payload()
    if payload_bytes:
        value["extension"] = ""
        value["extension"] = "x" * (payload_bytes - len(json.dumps(value).encode()))
        assert len(json.dumps(value).encode()) == payload_bytes
    # Render the real data-source configuration with its real provider; only
    # libvirt upload and VM startup are outside this local media round trip.
    for name in ("variables.tf", "local.tf", "cloud-init.sh.tmpl", "cloud-init.yml"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    data_source = (ROOT / "cloud_init.tf").read_text().split('data "cloudinit_config"', 1)[1]
    (tmp_path / "cloud_init.tf").write_text('data "cloudinit_config"' + data_source)
    (tmp_path / "main.tf").write_text('''
terraform {
  required_providers {
    cloudinit = { source = "hashicorp/cloudinit", version = "2.4.1" }
  }
}
output "media" {
  value = base64encode(data.cloudinit_config.main.rendered)
  sensitive = true
}
''')
    (tmp_path / "test.auto.tfvars.json").write_text(json.dumps({
        "name": "iso-test", "runnergroup": "Host Runners",
        "runner_payload_b64": base64.b64encode(json.dumps(value).encode()).decode(),
    }))
    subprocess.run([TF, "init", "-backend=false", "-input=false",
                    "-plugin-dir=" + str(ROOT / ".terraform/providers")],
                   cwd=tmp_path, capture_output=True, check=True)
    subprocess.run([TF, "apply", "-input=false", "-auto-approve"],
                   cwd=tmp_path, capture_output=True, check=True)
    output = subprocess.run([TF, "output", "-json", "media"], cwd=tmp_path,
                            capture_output=True, check=True)
    original = base64.b64decode(json.loads(output.stdout))
    (tmp_path / "user-data").write_bytes(original)
    (tmp_path / "meta-data").touch()
    (tmp_path / "network-config").touch()
    iso = tmp_path / "seed.iso"
    # Use exactly the flags used by libvirt provider 0.7.6.
    subprocess.run([mkisofs, "-output", str(iso), "-volid", "cidata", "-joliet", "-rock",
                    "user-data", "meta-data", "network-config"], cwd=tmp_path,
                   capture_output=True, check=True)
    readback = subprocess.run([isoinfo, "-i", str(iso), "-R", "-x", "/user-data"],
                              capture_output=True, check=True).stdout
    assert readback == original
    descriptor = subprocess.run([isoinfo, "-i", str(iso), "-d"], capture_output=True, check=True).stdout
    assert b"Volume id: cidata" in descriptor
    mime = BytesParser(policy=policy.default).parsebytes(readback)
    script = next(part.get_payload(decode=True) for part in mime.iter_parts()
                  if part.get_content_type() == "text/x-shellscript")
    subprocess.run(["bash", "-n"], input=script, capture_output=True, check=True)
    embedded = re.search(rb"<<'CONFIG'\n(.*?)\nCONFIG", script, re.S).group(1)
    assert json.loads(base64.b64decode(embedded))["payload"] == value
    assert json.loads(base64.b64decode(embedded))["settings"]["runner_group"] == "Host Runners"
    assert json.loads(base64.b64decode(embedded))["settings"]["runner_name"] == "iso-test"


@pytest.mark.parametrize("runner_group", ["Host Runners", ""])
@pytest.mark.parametrize("broker_payload", [True, False])
def test_guest_registration_uses_host_name_and_group(tmp_path, runner_group, broker_payload):
    value = payload()
    if not broker_payload:
        value.pop("schema_version")
    # A stale broker response must never override the host setting.
    value["registration"]["runner_group"] = "Old Broker Group"
    rendered = render(tmp_path, value, runner_group=runner_group)
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "config.json").write_text(json.dumps({
        "payload": value, "settings": {"runner_group": runner_group, "runner_name": "test-host-worker-1"},
    }))
    runtime = rendered.split("set +x\n", 1)[1].split('\necho "Repository:', 1)[0]
    runtime = runtime.replace("/home/ubuntu/runner-config.json", str(bootstrap / "config.json"))
    config = tmp_path / "config.sh"
    config.write_text("#!/usr/bin/env python3\nimport json, pathlib, sys\n"
                      "pathlib.Path('args.json').write_text(json.dumps(sys.argv[1:]))\n"
                      "pathlib.Path('.runner').touch()\n")
    config.chmod(0o700)
    block = rendered.split('# Keep registration ephemeral', 1)[1].split('if [[ ! -f /home/ubuntu/actions-runner/.runner', 1)[0]
    block = '# Keep registration ephemeral' + block
    bash(runtime + "\nsend_logs() { :; }\nsend_metrics() { :; }\n" + block,
         cwd=tmp_path)
    args = json.loads((tmp_path / "args.json").read_text())
    for flag in ("--unattended", "--ephemeral"):
        assert flag in args
    assert ("--no-default-labels" in args) == broker_payload
    for flag, expected in (("--url", "https://github.com/example-org"),
                           ("--token", "secret-registration-token"),
                           ("--name", "test-host-worker-1-100-unique" if broker_payload else "test-host-worker-1")):
        assert args[args.index(flag) + 1] == expected
    if runner_group:
        assert args[args.index("--runnergroup") + 1] == runner_group
    else:
        assert "--runnergroup" not in args
    assert args[args.index("--labels") + 1] == ",".join(payload()["registration"]["labels"])
    assert "--replace" in args


def test_monitoring_metadata_comes_from_payload_and_is_json_safe(tmp_path):
    value = payload()
    value["job"]["name"] = 'build "quoted"\nnext line'
    rendered = render(tmp_path, value, runner_group="Host Runners")
    (tmp_path / "config.json").write_text(json.dumps({
        "payload": value, "settings": {"runner_group": "Host Runners", "runner_name": "test-host-worker-1"},
    }))
    runtime = rendered.split("set +x\n", 1)[1].split('\necho "Repository:', 1)[0]
    runtime = runtime.replace("/home/ubuntu/runner-config.json", str(tmp_path / "config.json"))
    runtime = runtime.replace('echo -n "$entry" > /dev/udp/$dd_host/10518', ":")
    result = bash(runtime + "\nsend_logs job_started\n")
    entry = json.loads(result.stdout.removeprefix("Send logs ").removesuffix(" to 10.1.0.1\n"))
    assert entry["repo"] == value["repository"]["full_name"]
    assert entry["job"] == value["job"]["name"]
    assert entry["job_id"] == "100"
    assert entry["runner_group"] == "Host Runners"
    assert entry["runner_name"] == "test-host-worker-1-100-unique"
    assert entry["metadata_source"] == "capacity_request"
    assert value["registration"]["token"] not in result.stdout
    assert value["lease"]["receipt_handle"] not in result.stdout


def test_completion_hook_reads_payload_metadata_and_original_duration(tmp_path):
    value = payload()
    value["job"]["name"] = 'build "quoted"\nnext|line,part'
    rendered = render(tmp_path, value, runner_group="Host Runners")
    (tmp_path / "config.json").write_text(json.dumps({
        "payload": value, "settings": {"runner_group": "Host Runners"},
    }))
    (tmp_path / "started").write_text("1000")
    hook = rendered.split("cat << 'EOF' > stop.sh\n", 1)[1].split("\nEOF\n", 1)[0]
    hook = hook.replace("/home/ubuntu/runner-config.json", str(tmp_path / "config.json"))
    hook = hook.replace("/tmp/job_start.ts", str(tmp_path / "started"))
    hook = hook.replace("/dev/udp/10.1.0.1/8125", str(tmp_path / "metric"))
    hook = hook.replace('echo -n "$metrics" >', 'printf "%s\\n" "$metrics" >>')
    bash("date() { echo 2000; }\n" + hook, env={
        "GITHUB_REPOSITORY": "wrong/repo", "GITHUB_JOB": "wrong-job", "GITHUB_WORKFLOW": "wrong-workflow",
    })
    packets = (tmp_path / "metric").read_text().splitlines()
    assert len(packets) == 2
    assert packets[0].startswith("github.actions.jobs.repo:1|c|#")
    assert packets[1].startswith("github.actions.jobs.duration:1000|ms|@1|#")
    for packet in packets:
        assert "repo:example-org/repo" in packet
        assert 'job:build_"quoted"_next_line_part' in packet
        assert "workflow:CI" in packet and "job_id:100" in packet
        assert "runner_group:Host_Runners" in packet
        assert "arch:" in packet
        assert "wrong" not in packet


@pytest.mark.parametrize("state,shutdown", [
    ("no_log", False), ("idle", False), ("active", False),
    ("recent_progress", False), ("stalled_before_job_hook", True),
])
def test_ghasr97_stall_watchdog_preserves_original_diagnostic_signals(tmp_path, state, shutdown):
    rendered = render(tmp_path, payload())
    body = rendered.split("function runner_watchdog() {", 1)[1].split("\nrunner_watchdog &", 1)[0]
    script = "function runner_watchdog() {" + body
    script = script.replace("/home/ubuntu/actions-runner/_diag", str(tmp_path / "diag"))
    diag = tmp_path / "diag"
    diag.mkdir()
    if state != "no_log":
        log = diag / "Runner_test.log"
        log.write_text("Listening for Jobs\n" +
                       ("Acknowledging runner request\n" if state != "idle" else ""))
        modified = 2000 if state == "recent_progress" else 1000
        os.utime(log, (modified, modified))
    result = bash('''
        tick=2000
        date() { echo "$tick"; }
        sleep() { tick=$((tick + $1)); (( tick <= 2180 )) || exit 77; }
        send_metrics() { printf '%s\\n' "$*"; }
        send_logs() { printf '%s\\n' "$*"; }
        sudo() { printf 'shutdown:%s\\n' "$*"; }
        RUNNER_NAME=test-runner
    ''' + f"\npgrep() {{ {'return 0' if state == 'active' else 'return 1'}; }}\n" + script + "\nrunner_watchdog\n",
                  check=False)
    assert ("shutdown:poweroff" in result.stdout) == shutdown
    assert ("listener_stalled" in result.stdout) == shutdown
    assert result.returncode == (0 if shutdown else 77)


@pytest.mark.parametrize("diagnostic,shutdown", [
    (None, True),
    ("Runner.Listener process started\n", True),
    ("Listening for Jobs\n", False),
])
def test_ghasr97_provision_watchdog_preserves_listening_diagnostic(tmp_path, diagnostic, shutdown):
    rendered = render(tmp_path, payload())
    body = rendered.split("function provision_watchdog() {", 1)[1].split("\nprovision_watchdog &", 1)[0]
    script = "function provision_watchdog() {" + body
    script = script.replace("/home/ubuntu/actions-runner/_diag", str(tmp_path / "diag"))
    (tmp_path / "diag").mkdir()
    if diagnostic is not None:
        (tmp_path / "diag" / "Runner_test.log").write_text(diagnostic)
    result = bash('''
        sleep() { :; }
        send_metrics() { printf '%s\\n' "$*"; }
        send_logs() { printf '%s\\n' "$*"; }
        sudo() { printf 'shutdown:%s\\n' "$*"; }
        pgrep() { return 0; }
        RUNNER_NAME=test-runner
    ''' + script + "\nprovision_watchdog\n")
    assert ("shutdown:poweroff" in result.stdout) == shutdown
    assert ("never_listening" in result.stdout) == shutdown


@pytest.mark.parametrize("exit_status", [0, 17])
def test_foreground_runner_exit_status_is_preserved(tmp_path, exit_status):
    rendered = render(tmp_path, payload())
    (tmp_path / "run.sh").write_text(f"#!/bin/bash\nexit {exit_status}\n")
    (tmp_path / "run.sh").chmod(0o700)
    lifecycle = "export ACTIONS_RUNNER_HOOK_JOB_STARTED=" + rendered.split(
        "export ACTIONS_RUNNER_HOOK_JOB_STARTED=", 1)[1]
    result = bash('''
        send_metrics() { printf 'metric:%s\\n' "$*"; }
        send_logs() { printf 'log:%s\\n' "$*"; }
        sudo() { printf 'shutdown:%s\\n' "$*"; }
    ''' + lifecycle, cwd=tmp_path)
    assert f"log:runner_exit {exit_status}\n" in result.stdout
    assert ("metric:jobs.completed" in result.stdout) == (exit_status == 0)
    assert "shutdown:poweroff" in result.stdout


@pytest.mark.parametrize("action", ["ack", "release"])
@pytest.mark.parametrize("responses,attempts,succeeds", [
    ([(204, 0)], 1, True),
    ([(500, 0), (502, 0), (204, 0)], 3, True),
    ([(403, 0)] * 5 + [(204, 0)], 5, False),
    ([(429, 0)] * 5 + [(204, 0)], 5, False),
    ([(503, 0), (204, 0)], 2, True),
    ([(0, 28), (0, 7), (204, 0)], 3, True),
    ([(500, 0)] * 5 + [(204, 0)], 5, False),
    ([(408, 0), (204, 0)], 2, True),
    ([(400, 0), (204, 0)], 1, False),
    ([(401, 0), (204, 0)], 1, False),
    ([(204, 28), (204, 0)], 2, True),
])
def test_broker_action_retries_are_bounded_and_keep_the_same_receipt(
        tmp_path, action, responses, attempts, succeeds):
    (tmp_path / "payload.json").write_text(json.dumps(payload()))
    (tmp_path / "responses").write_text("".join(f"{code:03d}:{status}\n" for code, status in responses))
    function = (ROOT / "loop.sh").read_text().split("function broker_action() {", 1)[1].split("\n}\n", 1)[0]
    function = "function broker_action() {" + function + "\n}"
    result = bash(r'''
        echoerr() { echo "$*" >&2; }
        payload_file=payload.json
        broker_url=https://example.invalid/pull
        REG_TOKEN_LAMBDA_APIKEY=host-only-api-key
        curl() {
            cat >> requests
            response=$(sed -n "$(wc -l < requests)p" responses)
            printf '%s' "${response%:*}"
            return "${response#*:}"
        }
        sleep() { printf '%s\n' "$1" >> delays; }
    ''' + function + f"\nbroker_action {action}\n", cwd=tmp_path, check=False)
    assert result.returncode == (0 if succeeds else 1)
    requests = [json.loads(line) for line in (tmp_path / "requests").read_text().splitlines()]
    assert requests == [{"action": action, "runs-on": "ubuntu-latest-kong",
                         "receipt_handle": "secret-receipt"}] * attempts
    delay_file = tmp_path / "delays"
    delays = [int(line) for line in delay_file.read_text().splitlines()] if delay_file.exists() else []
    assert len(delays) == attempts - 1
    for attempt, (delay, (code, _)) in enumerate(zip(delays, responses), start=1):
        low, high = (15, 20) if code in (403, 429, 503) else (2 ** attempt, 2 ** attempt + 3)
        assert low <= delay <= high
    output = result.stdout + result.stderr
    for secret in ("secret-receipt", "secret-registration-token", "host-only-api-key"):
        assert secret not in output


@pytest.mark.parametrize("apply_success", [True, False])
@pytest.mark.parametrize("action_failure", [False, True])
def test_host_handoff_without_renewal_or_recovery_states(tmp_path, apply_success, action_failure):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    value_file = tmp_path / "response.json"
    value_file.write_text(json.dumps(payload()))
    events = tmp_path / "events.jsonl"
    state = tmp_path / "state"
    state.mkdir()
    empty_env = tmp_path / "empty.env"
    empty_env.touch()
    # Redirect the original fixed host paths only inside this test copy.
    source = tmp_path / "source"
    source.mkdir()
    loop = source / "loop.sh"
    loop.write_text((ROOT / "loop.sh").read_text().replace(
        "/root/self-hosted-kvm.env", str(empty_env)).replace("/root/vms", str(state)))
    fake = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
tool = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
def record(value):
    with (root / 'events.jsonl').open('a') as f: f.write(json.dumps(value) + '\\n')
if tool == 'curl':
    source = args[args.index('--data-binary') + 1]
    body = json.loads(sys.stdin.read() if source == '@-' else pathlib.Path(source[1:]).read_text())
    response = pathlib.Path(args[args.index('-o') + 1])
    action = body.get('action', 'pull')
    record({'tool': tool, 'action': action, 'runs-on': body.get('runs-on')})
    if action != 'pull':
        assert body['receipt_handle'] == 'secret-receipt'
        if action == 'ack':
            assert (root / 'applied').exists()
            assert 'secret-registration-token' not in json.dumps(body)
        print('500' if os.environ['TEST_ACTION_FAILURE'] == 'true' else '204', end='')
        response.write_text(''); sys.exit()
    counter = root / 'counter'
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    statuses = [204, 409, 500, 200]
    status = statuses[count] if count < len(statuses) else 204
    response.write_text((root / 'response.json').read_text() if status == 200 else '{}')
    print(status, end='')
elif tool == 'terraform':
    record({'tool': tool, 'action': args[0]})
    if args[0] == 'state' and (root / 'applied').exists():
        print('libvirt_domain.test')
    if args[0] == 'plan':
        # A successful apply leaves a running VM, even when ack fails.
        print('running = true')
        sys.exit(0)
    if args[0] == 'apply':
        if os.environ['TEST_APPLY_SUCCESS'] != 'true': sys.exit(1)
        host_vars = next(x for x in args if x.endswith('.tfvars'))
        assert 'runnergroup="Host Runners"' in pathlib.Path(host_vars).read_text()
        varfile = next(x for x in args if x.endswith('payload.tfvars.json'))
        v = json.loads(pathlib.Path(varfile).read_text())
        assert 'runner_payload_b64' in v
        pathlib.Path('terraform.tfstate').write_text('{}')
        (root / 'applied').touch()
elif tool == 'virsh':
    if 'domstate' in args:
        sys.exit(1)
elif tool == 'sleep':
    seconds = int(args[0])
    record({'tool': tool, 'seconds': seconds})
    time.sleep(0.01)
'''
    for command in ("curl", "terraform", "virsh", "sleep"):
        path = bin_dir / command
        path.write_text(fake)
        path.chmod(0o700)
    env = {**os.environ, "PATH": str(bin_dir) + ":" + os.environ["PATH"],
           "TEST_ROOT": str(tmp_path), "NAME": "test-slot", "RUNNERGROUP": "Host Runners",
           "TEST_APPLY_SUCCESS": str(apply_success).lower(),
           "TEST_ACTION_FAILURE": str(action_failure).lower(),
           "RUNS_ON": "ubuntu-latest-kong", "REG_TOKEN_LAMBDA_URL": "https://example.invalid",
           "REG_TOKEN_LAMBDA_APIKEY": "host-only-api-key"}
    output = tmp_path / "output.log"
    with output.open("w") as log:
        process = subprocess.Popen(["bash", str(loop)], env=env,
                                   stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                expected = 'ack' if apply_success else 'release'
                if events.exists():
                    recorded = [json.loads(line) for line in events.read_text().splitlines()]
                    handoff = next((i for i, item in enumerate(recorded) if item.get('action') == expected), None)
                    if handoff is not None and any(item == {'tool': 'terraform', 'action': 'state'}
                                                  for item in recorded[handoff + 1:]):
                        break
                assert process.poll() is None, output.read_text()
                time.sleep(0.05)
            else:
                pytest.fail(output.read_text()[-6000:])
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    actions = [line["action"] for line in lines if line["tool"] == "curl"]
    assert actions[:5] == ["pull", "pull", "pull", "pull", "ack" if apply_success else "release"]
    assert "extend" not in actions
    assert actions.count("ack" if apply_success else "release") == (5 if action_failure else 1)
    if apply_success:
        assert actions == ["pull", "pull", "pull", "pull"] + ["ack"] * (5 if action_failure else 1)
    delays = [line["seconds"] for line in lines if line["tool"] == "sleep"]
    assert len(delays) >= 3 and all(2 <= delay <= 19 for delay in delays)
    assert len([line for line in lines if line == {"tool": "terraform", "action": "apply"}]) == (1 if apply_success else 2)
    logs = output.read_text()
    assert "204" in logs and "409" in logs and "500" in logs
    if action_failure:
        assert f"Broker {'ack' if apply_success else 'release'} failed: HTTP 500" in logs
    assert "secret-registration-token" not in logs
    assert "secret-receipt" not in logs
    assert "host-only-api-key" not in logs
