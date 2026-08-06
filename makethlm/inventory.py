"""Ansible INI inventory file parser.

Parses the core subset of Ansible inventory format:
  - [group] headers
  - hostnames (one per line)
  - Per-host vars: ansible_user, ansible_port

Does NOT support: child groups (:children), host ranges [1:50],
YAML format, or dynamic inventory scripts.
"""

from __future__ import annotations

from .models import HostConnection, HostGroup


def parse_ansible_inventory(path: str) -> dict[str, HostGroup]:
    """Parse an Ansible INI-format inventory file.

    Returns a dict of group name -> HostGroup.
    """
    with open(path) as f:
        lines = f.readlines()

    groups: dict[str, HostGroup] = {}
    current_group: str | None = None

    for raw_line in lines:
        line = raw_line.strip()

        # Skip blanks and comments
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        # Group header: [groupname]
        if line.startswith("[") and line.endswith("]"):
            group_name = line[1:-1].strip()
            # Skip meta-groups like [group:children] or [group:vars]
            if ":" in group_name:
                current_group = None
                continue
            current_group = group_name
            if current_group not in groups:
                groups[current_group] = HostGroup(name=current_group)
            continue

        # Host line (only if we're inside a group)
        if current_group is None:
            continue

        parts = line.split()
        hostname = parts[0]

        # Parse per-host vars
        host_user: str | None = None
        host_port: int | None = None
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key == "ansible_user":
                host_user = value
            elif key == "ansible_port":
                try:
                    host_port = int(value)
                except ValueError:
                    pass

        group = groups[current_group]
        group.hosts.append(hostname)
        if host_user or host_port:
            group.connections[hostname] = HostConnection(
                user=host_user,
                port=host_port,
            )

    # Filter out empty groups
    return {name: grp for name, grp in groups.items() if grp.hosts}
