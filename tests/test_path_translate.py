"""Unit tests for hyperagent0.sandbox.path_translate.

These tests use synthetic mountinfo fixtures and MUST pass without root
or Docker — they exercise only pure parsing and prefix-matching logic.
"""

from __future__ import annotations

import pytest

from hyperagent0.sandbox import path_translate


# Sample /proc/self/mountinfo lines (trimmed to the fields we parse).
SAMPLE_MOUNTINFO = """\
26 31 0:23 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw
27 31 0:5 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc rw
35 31 0:27 / /dev/pts rw,nosuid,noexec,relatime shared:3 - devpts devpts rw,seclabel
770 660 254:1 /var/lib/agent/projects/foo /workspace/projects/foo rw,relatime - ext4 /dev/sda1 rw
771 660 254:1 /opt/knowledge /workspace/knowledge ro,relatime - ext4 /dev/sda1 ro
"""


def test_parse_mountinfo_returns_expected_entries() -> None:
    entries = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    # All five lines parse.
    assert len(entries) == 5
    # Find the project bind mount.
    project = next(e for e in entries if e.mount_point == "/workspace/projects/foo")
    assert project.root == "/var/lib/agent/projects/foo"
    assert project.fs_type == "ext4"
    assert project.source == "/dev/sda1"


def test_parse_mountinfo_skips_malformed_lines() -> None:
    text = "not a real line\n" + SAMPLE_MOUNTINFO + "\nshort line\n"
    entries = path_translate.parse_mountinfo(text)
    assert len(entries) == 5  # malformed lines silently dropped


def test_to_host_uses_longest_matching_mount() -> None:
    mounts = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    host = path_translate.to_host("/workspace/projects/foo/src/main.py", mountinfo=mounts)
    assert host == "/var/lib/agent/projects/foo/src/main.py"


def test_to_host_handles_exact_mount_point() -> None:
    mounts = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    host = path_translate.to_host("/workspace/projects/foo", mountinfo=mounts)
    assert host == "/var/lib/agent/projects/foo"


def test_to_host_identity_when_no_mount_matches() -> None:
    mounts = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    # /tmp is not in our fixture (the root mount line is absent).
    host = path_translate.to_host("/tmp/something", mountinfo=mounts)
    assert host == "/tmp/something"


def test_to_host_identity_when_no_mountinfo_available() -> None:
    # Empty mounts list => identity transform.
    host = path_translate.to_host("/anywhere/path", mountinfo=[])
    assert host == "/anywhere/path"


def test_from_host_is_inverse_of_to_host() -> None:
    mounts = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    agent_path = "/workspace/projects/foo/src/main.py"
    host = path_translate.to_host(agent_path, mountinfo=mounts)
    round_trip = path_translate.from_host(host, mountinfo=mounts)
    assert round_trip == agent_path


def test_from_host_handles_unmounted_path() -> None:
    mounts = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    assert path_translate.from_host("/elsewhere/foo", mountinfo=mounts) == "/elsewhere/foo"


def test_to_host_normalizes_path() -> None:
    mounts = path_translate.parse_mountinfo(SAMPLE_MOUNTINFO)
    host = path_translate.to_host("/workspace/projects/foo/../foo/src", mountinfo=mounts)
    assert host == "/var/lib/agent/projects/foo/src"


def test_to_host_with_root_mount() -> None:
    # Synthetic mountinfo where the entire root is a bind mount.
    text = "1 0 0:1 / / rw - ext4 /dev/root rw\n"
    mounts = path_translate.parse_mountinfo(text)
    # root mount has root == mount_point => identity.
    assert path_translate.to_host("/usr/bin/python", mountinfo=mounts) == "/usr/bin/python"


def test_to_host_with_nested_mounts_picks_longest() -> None:
    text = (
        "1 0 0:1 /host/parent /workspace rw - ext4 /dev/sda1 rw\n"
        "2 1 0:2 /host/child /workspace/sub rw - ext4 /dev/sda1 rw\n"
    )
    mounts = path_translate.parse_mountinfo(text)
    # Longest mount_point match wins.
    assert (
        path_translate.to_host("/workspace/sub/file", mountinfo=mounts)
        == "/host/child/file"
    )
    # Falls back to the parent mount for paths not under the child.
    assert (
        path_translate.to_host("/workspace/other", mountinfo=mounts)
        == "/host/parent/other"
    )
