"""Helper functions for the sample app (TEST FIXTURE - intentionally vulnerable)."""
import subprocess


def ping(host):
    # Vulnerable on purpose: user-controlled input reaches a shell.
    return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)


def add(a, b):
    return a + b