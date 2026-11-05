#!/usr/bin/env python3
"""Run on the backend host with its existing Docker/registry permissions."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def deploy(compose: Path, service: str, image: str):
    if not compose.is_absolute() or not compose.is_file():
        raise ValueError("--compose must name an existing absolute Compose file")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", service):
        raise ValueError("Invalid Compose service")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:@-]+", image) or ":" not in image.rsplit("/", 1)[-1] or image.rsplit(":", 1)[-1] == "latest":
        raise ValueError("Image must use an explicit version tag or digest")
    base = ["docker", "compose", "--project-directory", str(compose.parent), "-f", str(compose)]
    services = subprocess.run([*base, "config", "--services"], check=True, capture_output=True, text=True, timeout=60).stdout.splitlines()
    if service not in services:
        raise ValueError("Service does not exist in the configured Compose project")
    # Pull before changing the project's deployment override.
    subprocess.run(["docker", "pull", image], check=True, timeout=300)
    override = compose.parent / f"flowdeck-{service}.override.json"
    pending = override.with_suffix(".pending.json")
    pending.write_text(json.dumps({"services": {service: {"image": image}}}, indent=2), encoding="utf-8")
    subprocess.run([*base, "-f", str(pending), "config", "--quiet"], check=True, timeout=60)
    if override.exists():
        override.with_suffix(".previous.json").write_bytes(override.read_bytes())
    pending.replace(override)
    subprocess.run([*base, "-f", str(override), "up", "-d", "--no-deps", "--no-build", service], check=True, timeout=300)
    # The worker separately verifies health and application version before declaring success.
    return {"service": service, "image": image, "override": str(override)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    print(json.dumps(deploy(args.compose, args.service, args.image)))


if __name__ == "__main__":
    main()
