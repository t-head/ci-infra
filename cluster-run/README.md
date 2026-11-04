# Cluster Run

A GitHub composite action that submits and manages framework-agnostic distributed jobs
(from a single card up to multi-node, multi-card) on a Kubernetes PPU cluster.

The action runs on a CPU-only orchestration runner and consumes no accelerator itself.
All PPU resources are requested by the worker pods it creates.

## Features

- Declarative inputs: node count, cards per node, image and command.
- Gang scheduling through `PodGroup` with a dedicated `ppu-scheduler` and
  multi-node pod anti-affinity.
- Standard distributed environment contract injected into every worker pod.
- Automatic source staging: `$GITHUB_WORKSPACE` (plus optional artifacts) is packed and
  extracted inside the worker pod.
- Default CPU/memory limits derived from board type and card count.
- Scheduling progress and worker output appear in distinct steps: placement must finish before the log step begins, which shows startup state and follows the full worker-0 log. One deadline covers both stages; resources are still removed after errors or cancellation.
- One-line `PPU usage` notice on the run page plus a collapsed usage summary in the job log, covering queue time and occupied/execution PPU card-hours; the GitHub Job Summary is left untouched.
- One `PPU queue` annotation emitted after the job ends, listing each pod's queue time (creation until scheduling). It is a warning with the latest scheduling failure reason when any pod queued for five minutes or more or never scheduled, and a notice otherwise. Apart from these two and errors, the action creates no run annotations.
- Optional kernel log (`dmesg`) collection from worker nodes.

## Usage

```yaml
jobs:
  train:
    runs-on: <cpu-orchestration-runner-label>
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - uses: t-head/ci-infra/cluster-run@main
        with:
          image: <registry>/<image>:<tag>
          nnodes: 2
          nproc_per_node: 8
          node_selector: board-type=ZW-M890P
          timeout_minutes: 120
          command: |
            torchrun \
              --nnodes=$NNODES --nproc_per_node=$NPROC_PER_NODE \
              --node_rank=$NODE_RANK \
              --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT \
              train.py
```

### Execution modes

| `nnodes` | Mode | Behavior |
|----------|------|----------|
| `0` (default) | Single pod | One pod, no `PodGroup` or headless `Service` |
| `1..N` | Gang | `N` worker pods, a `PodGroup` (`minMember=N`) and a headless `Service` for rendezvous |

`launch_mode` controls execution independently of allocation. `each-node` is the
unchanged default: every pod runs setup and then command. In multi-node `master`
mode, all pods install SSH, establish trusted passwordless connections, and run
setup; only worker-0 executes the foreground command. Worker SSH services remain
supervised until allocation cleanup. MPI is one possible master-mode launcher,
not a separate launch mode.

### Automatic SSH installation

Multi-node master mode installs `openssh-server`, the client package, `jq` and
required package dependencies inside each root container. Compatible installed
tools are reused. Otherwise, matching authenticated packages are read from
`/mnt/wl_nas/devops/cache/openssh`, or downloaded from Aliyun repositories and
cached. Global APT/YUM repository configuration is not replaced.

| Image OS | Installation path |
| --- | --- |
| Ubuntu 20.04/22.04/24.04; Debian 11/12/13 | APT and authenticated distro metadata |
| CentOS Linux 7 (x86_64) | YUM and signed RPM packages |
| CentOS Linux 8; CentOS Stream 8/9 (x86_64/aarch64) | DNF and signed RPM packages |
| Alibaba Cloud Linux 2/3 (x86_64/aarch64) | YUM/DNF and signed RPM packages |

CentOS releases that are end-of-life use the corresponding Aliyun archive;
installation support does not provide ongoing security maintenance. CentOS 7
and Alibaba Cloud Linux 2 use signed EPEL 7 packages for `jq`. RPM signing keys must match the fixed
vendor identities; an arbitrary NAS checksum or repository key is not trusted.

The container must have a writable root filesystem, Bash, its package manager,
GNU timeout/checksum tools, and a usable TLS trust store. RPM installation also
needs RPM signature tooling and key-download tooling. Frameworks such as MPI,
PCCL and the accelerator SDK are still supplied by the image or `setup_command`.
No privileged mode, source compilation, or multi-source fallback is enabled by
SSH installation. Master mode additionally requires namespace-scoped Secret
permissions and a runner-owned NAS runtime root prepared by cluster operations.

### What reaches the worker

Staging omits Git config files from repositories, submodules and worktrees so checkout
authentication is not carried into the archive. Objects, refs and `HEAD` are preserved
for commands such as `git describe --always`; the runner's workspace config is left
intact. Configure Git on the worker if the workload needs custom settings. The checkout
example disables credential persistence with `persist-credentials: false`.

Extra artifacts are copied by basename only when the workspace does not already contain
that name. Existing destinations are kept as-is, while missing paths are warning-only skips.

## Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `image` | yes | | Container image with job code and dependencies |
| `command` | yes | | Foreground shell command, on each node or only worker-0 according to launch mode |
| `launch_mode` | no | `each-node` | `each-node` or `master` |
| `setup_command` | no | | Per-node initialization; multi-node master establishes SSH first and retains exported variables |
| `ssh_port` | no | `2222` | Preferred SSH port; occupied ports use discovered per-node alternatives |
| `nnodes` | no | `0` | Single-pod mode at `0`; otherwise the number of gang-scheduled pods |
| `nproc_per_node` | no | `1` | PPU cards requested per pod (`alibabacloud.com/ppu`) |
| `namespace` | no | `default` | Kubernetes namespace |
| `timeout_minutes` | no | `60` | Combined scheduling and execution limit; expiry records `timeout` and removes job resources |
| `host_volumes` | no | | `/host/a:/ctr/a,/host/b:/ctr/b`; NAS mounts are added by default |
| `extra_env` | no | | `KEY1=VAL1,KEY2=VAL2` |
| `master_port` | no | `29500` | Rendezvous port |
| `cleanup_policy` | no | `always` | Accepted but not applied; resource removal is unconditional |
| `source_dir` | no | `/workspace/source` | Path where staged source is extracted in the pod |
| `node_selector` | no | | `KEY1=VAL1,...`, merged into `spec.nodeSelector` |
| `container_options` | no | | YAML snippet, see below |
| `artifact_paths` | no | | Extra paths (one per line) staged with the source |
| `job_suffix` | no | | Readable segment of resource names; a unique hash is always appended |
| `dmesg_policy` | no | `never` | No logs with `never`; either `on_failure` or `always` enables collection, printing and NAS storage |

Changing `cleanup_policy` cannot keep pods or other job resources, which are removed
for all terminal states. The current implementation treats `on_failure` exactly like
`always`: kernel logs also remain on NAS after successful execution, not just after
an error, deadline or cancellation.

### `container_options`

| Key | Maps to |
|-----|---------|
| `privileged` | container `securityContext.privileged` |
| `host_ipc` | pod `spec.hostIPC` |
| `host_network` | pod `spec.hostNetwork` (sets `dnsPolicy: ClusterFirstWithHostNet`) |
| `dns_policy` | pod `spec.dnsPolicy` |
| `shm_size` | `/dev/shm` emptyDir `sizeLimit` (default `64Gi`) |
| `cap_add` | `securityContext.capabilities.add`, comma separated |
| `cpus` / `cpu` | `resources.limits.cpu` |
| `memory` | `resources.limits.memory`; docker units `b/k/m/g/t` are converted |

When `cpus` or `memory` is not set, limits are computed per card:
`ZW-M890P` uses 31 cores and 256Gi, while `OAM-810E` (including `ZW-810E`) uses 11 cores and 112Gi.
Without a `board-type` selector, `OAM-810E` values are used.
Board equivalence affects resource defaults only; node selection retains the supplied label value.

## Environment contract

Each worker pod receives:

| Variable | Value |
|----------|-------|
| `NNODES` / `WORLD_SIZE` | Number of worker pods |
| `NPROC_PER_NODE` | Cards per pod |
| `NODE_RANK` / `RANK` | Pod index |
| `LOCAL_RANK` | `0` |
| `MASTER_ADDR` | Standalone pod: `127.0.0.1`; gang workers: worker-0 headless `Service` DNS name |
| `MASTER_PORT` | `master_port` input |
| `NODE_NAME` | Kubernetes node name |
| `SOURCE_DIR` | Source extraction directory |
| `CLUSTER_HOSTS_FILE` | Multi-node master: local ordered FQDN list |
| `CLUSTER_NODES_FILE` | Multi-node master: local node metadata with actual SSH ports |

Master mode provides `cluster-ssh` and `cluster-exec` to select each destination's
actual port and enforce host-key verification. Remote commands restore the
destination node's exported environment; node-level RANK/WORLD_SIZE are not MPI
process ranks. The orchestrator must wait for all remote tasks and propagate
their failures before exiting. Command completion is recorded independently of
container shutdown; up to ten seconds after the deadline are reserved only for
reporting that result and stopping SSH services, not for extending the command budget.

Variables declared in the workflow or job `env` context are forwarded automatically.

## Outputs

| Name | Description |
|------|-------------|
| `job_name` | Resource name of the job |
| `job_status` | `succeeded`, `failed`, `timeout`, `cancelled` |
| `duration_seconds` | Job duration |
| `queue_duration_seconds` | Time until all PPU pods are scheduled |
| `occupied_ppu_card_hours` | PPU card-hours from pod scheduling until completion, including image pulling and startup |
| `execution_ppu_card_hours` | PPU card-hours while worker containers are running |
| `task_pods` | Comma-separated worker pod names |

The queue duration is measured from the first pod creation until every pod is scheduled. For a pod
that never schedules, it is measured until the job ends. Both card-hours metrics sum each
pod independently, so multi-node jobs are accounted for correctly.

## Action layout

```
action.yml          Composite action definition
scripts/            Naming, submission, cleanup and dmesg scripts
tests/              Workload lifecycle and source-transfer checks
```
