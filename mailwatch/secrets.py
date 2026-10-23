"""Resolve secret references.

Values never live in config files or plists. Supported:
  keychain://<service>/<account>   macOS login keychain (generic password)
  env://<VAR>                      environment variable (tests, CI)
"""
from __future__ import annotations

import os
import subprocess
from urllib.parse import urlparse


class SecretError(RuntimeError):
    pass


def resolve(ref: str) -> str:
    if not ref:
        raise SecretError("empty secret reference")
    u = urlparse(ref)
    if u.scheme == "env":
        name = u.netloc or u.path.lstrip("/")
        val = os.environ.get(name)
        if not val:
            raise SecretError(f"env var {name} not set")
        return val
    if u.scheme == "keychain":
        service, account = u.netloc, u.path.strip("/")
        if not service or not account:
            raise SecretError(f"bad keychain ref {ref}")
        try:
            out = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError as e:  # not macOS
            raise SecretError("macOS `security` not available") from e
        if out.returncode != 0:
            raise SecretError(f"keychain item {service}/{account} not found ({out.stderr.strip()[:120]})")
        return out.stdout.strip()
    raise SecretError(f"unsupported secret scheme in {ref}")
