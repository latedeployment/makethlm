# SSH & Host Inventory

makethlm includes an Ansible-like host inventory for running shell commands on remote machines via SSH.

## Defining Host Groups

```
hosts web [user=deploy, port=22]:
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

- **Shell commands** (`!` lines) execute on **every host** in the group via SSH, sequentially. If any host fails, execution stops.
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

SSH connections use `BatchMode=yes` for non-interactive operation.
