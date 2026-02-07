"""Tests for the Ansible inventory parser."""

import os
import tempfile

import pytest

from makethlm.inventory import parse_ansible_inventory


class TestParseAnsibleInventory:
    def _write_inventory(self, content: str) -> str:
        """Write content to a temp file and return its path."""
        fd, path = tempfile.mkstemp(suffix=".ini")
        os.write(fd, content.encode())
        os.close(fd)
        return path

    def test_single_group(self):
        path = self._write_inventory("""\
[webservers]
web1.example.com
web2.example.com
""")
        try:
            groups = parse_ansible_inventory(path)
            assert "webservers" in groups
            assert groups["webservers"].hosts == ["web1.example.com", "web2.example.com"]
        finally:
            os.unlink(path)

    def test_multiple_groups(self):
        path = self._write_inventory("""\
[webservers]
web1.example.com
web2.example.com

[databases]
db1.example.com
db2.example.com
""")
        try:
            groups = parse_ansible_inventory(path)
            assert len(groups) == 2
            assert "webservers" in groups
            assert "databases" in groups
            assert groups["databases"].hosts == ["db1.example.com", "db2.example.com"]
        finally:
            os.unlink(path)

    def test_ansible_user_var(self):
        path = self._write_inventory("""\
[webservers]
web1.example.com ansible_user=deploy
web2.example.com
""")
        try:
            groups = parse_ansible_inventory(path)
            assert groups["webservers"].user == "deploy"
        finally:
            os.unlink(path)

    def test_ansible_port_var(self):
        path = self._write_inventory("""\
[webservers]
web1.example.com ansible_port=2222
""")
        try:
            groups = parse_ansible_inventory(path)
            assert groups["webservers"].port == 2222
        finally:
            os.unlink(path)

    def test_combined_vars(self):
        path = self._write_inventory("""\
[databases]
db1.example.com ansible_user=postgres ansible_port=5432
""")
        try:
            groups = parse_ansible_inventory(path)
            grp = groups["databases"]
            assert grp.hosts == ["db1.example.com"]
            assert grp.user == "postgres"
            assert grp.port == 5432
        finally:
            os.unlink(path)

    def test_comments_ignored(self):
        path = self._write_inventory("""\
# This is a comment
[webservers]
; Another comment
web1.example.com
""")
        try:
            groups = parse_ansible_inventory(path)
            assert groups["webservers"].hosts == ["web1.example.com"]
        finally:
            os.unlink(path)

    def test_blank_lines_ignored(self):
        path = self._write_inventory("""\
[webservers]

web1.example.com

web2.example.com

""")
        try:
            groups = parse_ansible_inventory(path)
            assert groups["webservers"].hosts == ["web1.example.com", "web2.example.com"]
        finally:
            os.unlink(path)

    def test_empty_group_filtered_out(self):
        path = self._write_inventory("""\
[empty]

[notempty]
host1
""")
        try:
            groups = parse_ansible_inventory(path)
            assert "empty" not in groups
            assert "notempty" in groups
        finally:
            os.unlink(path)

    def test_child_groups_skipped(self):
        path = self._write_inventory("""\
[webservers]
web1.example.com

[webservers:children]
subgroup1

[databases]
db1.example.com
""")
        try:
            groups = parse_ansible_inventory(path)
            assert "webservers" in groups
            assert "databases" in groups
            # :children group should be skipped
            assert "webservers:children" not in groups
        finally:
            os.unlink(path)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            parse_ansible_inventory("/nonexistent/path/inventory.ini")

    def test_ip_addresses(self):
        path = self._write_inventory("""\
[cluster]
192.168.1.10
192.168.1.11
10.0.0.5
""")
        try:
            groups = parse_ansible_inventory(path)
            assert len(groups["cluster"].hosts) == 3
        finally:
            os.unlink(path)

    def test_hosts_before_any_group_ignored(self):
        path = self._write_inventory("""\
ungrouped_host

[webservers]
web1.example.com
""")
        try:
            groups = parse_ansible_inventory(path)
            # ungrouped_host should be ignored (no current group)
            assert "webservers" in groups
            assert groups["webservers"].hosts == ["web1.example.com"]
        finally:
            os.unlink(path)
