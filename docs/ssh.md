# SSH & Host Inventory

makethlm includes an Ansible-like host inventory for running shell commands on remote machines via SSH.

## Defining Host Groups

```
hosts web [user=deploy, port=22, identity-file=~/.ssh/deploy, strict-host-key-checking=accept-new]:
    web1.prod.internal
    web2.prod.internal
    web3.prod.internal

hosts db [user=postgres, port=5433]:
    db-primary.prod.internal
    db-replica.prod.internal
```

## Targeting a Host Group

Use the `on` task option to target a host group:

```
task deploy [on=web]:
    !systemctl restart my-web-app
    verify the app is responding on port 8080
```

When a task has `[on=<group>]`:

- **Shell commands** (`!` lines) execute on **every host** in the group via SSH, sequentially by default. If any host fails, execution stops.
- **Prompt steps** (natural language) still execute **locally** via the LLM.

## Interleaving Remote and Local

This lets you interleave remote operations with local LLM reasoning:

```
task deploy [on=web]:
    !systemctl restart myapp          # runs on each web host via SSH
    verify the restart was successful  # runs locally via LLM
    !curl -sf http://localhost/health  # runs on each web host via SSH
```

## Host Group Options

| Option | Default | Description |
|--------|---------|-------------|
| `user` | (SSH default) | SSH username |
| `port` | (SSH default, 22) | SSH port |
| `identity-file` | (SSH default) | SSH identity file |
| `strict-host-key-checking` | (SSH default) | SSH host key policy: `yes`, `no`, or `accept-new` |

SSH connections use `BatchMode=yes` for non-interactive operation.

## Ansible Inventory Files

Imported INI inventories preserve `ansible_user` and `ansible_port` separately
for every host:

```ini
[web]
web1.example.test ansible_user=alice ansible_port=2201
web2.example.test ansible_user=bob ansible_port=2202
```

```make
inventory "./hosts.ini"
```

One host's values are never reused as defaults for another host.

## Task-Level SSH Options

Task options can override host group SSH settings:

```
task deploy [on=web, ssh-key=~/.ssh/deploy, ssh-strict-host-key-checking=yes, ssh-parallel, timeout=45s]:
    !systemctl restart myapp
```

Use `ssh-parallel` to run each shell step across all hosts concurrently. The
next step starts only after every host finishes the current step.
