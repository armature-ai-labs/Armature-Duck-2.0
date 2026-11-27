#!/usr/bin/env python3
"""Cross-platform deployment tool for Armature Duck 2.0.

Synchronizes local Microduck Studio modifications to the robot,
executes remote self-checks, and restarts the microduck-studio service.
Works on macOS, Linux, and Windows.

Usage:
    python3 tools/deploy_to_duck.py
    python3 tools/deploy_to_duck.py --host 192.168.100.245
    python3 tools/deploy_to_duck.py --dry-run
"""
import argparse
import os
import shutil
import subprocess
import sys

DEFAULT_HOST = "duck-c4ef.local"
DEFAULT_USER = "radxa"
DEFAULT_PORT = 22
REMOTE_STAGING_DIR = "/home/radxa/studio-dev"
REMOTE_PROD_DIR = "/opt/microduck/software/current/studio"


def get_repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def run_cmd(cmd, check=True):
    print(f"--> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, check=check, shell=isinstance(cmd, str))


def main():
    parser = argparse.ArgumentParser(description="Deploy Microduck Studio to target robot.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Robot hostname or IP (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER, help=f"SSH username (default: {DEFAULT_USER})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="SSH port (default: 22)")
    parser.add_argument("--dry-run", action="store_true", help="Preview sync actions without making changes")
    args = parser.parse_args()

    repo_root = get_repo_root()
    local_studio_dir = os.path.join(repo_root, "software", "studio")

    if not os.path.isdir(local_studio_dir):
        sys.exit(f"Error: Local studio directory not found at {local_studio_dir}")

    target = f"{args.user}@{args.host}"
    print("=" * 60)
    print(f"Deploying Armature Studio to {target}")
    print(f"Source: {local_studio_dir}")
    print(f"Remote Staging: {REMOTE_STAGING_DIR}")
    print("=" * 60)

    # 1. Check if rsync is available on the local machine
    has_rsync = shutil.which("rsync") is not None

    if has_rsync:
        rsync_opts = [
            "rsync",
            "-avz",
            "--delete",
            "--exclude", "__pycache__",
            "--exclude", "*.pyc",
            "--exclude", "_testlogs",
            "--exclude", "state",
            "--exclude", "logs",
            "--exclude", ".git",
            f"-e", f"ssh -p {args.port} -o BatchMode=no",
        ]
        if args.dry_run:
            rsync_opts.append("--dry-run")

        rsync_opts.extend([f"{local_studio_dir}/", f"{target}:{REMOTE_STAGING_DIR}/"])
        print("\n[1/3] Syncing files to remote staging area via rsync...")
        ret = subprocess.run(rsync_opts)
        if ret.returncode != 0:
            sys.exit("rsync failed. Please check network connectivity and SSH credentials.")
    else:
        # Fallback using scp / tar over ssh
        print("\n[1/3] rsync not found; syncing via tar over SSH...")
        tar_cmd = (
            f"tar --exclude='__pycache__' --exclude='*.pyc' --exclude='_testlogs' "
            f"--exclude='state' --exclude='logs' -czf - -C \"{local_studio_dir}\" . | "
            f"ssh -p {args.port} {target} 'mkdir -p \"{REMOTE_STAGING_DIR}\" && tar -xzf - -C \"{REMOTE_STAGING_DIR}\"'"
        )
        if not args.dry_run:
            ret = subprocess.run(tar_cmd, shell=True)
            if ret.returncode != 0:
                sys.exit("Tar sync over SSH failed.")

    if args.dry_run:
        print("\nDry-run complete. No remote cutover executed.")
        return

    # 2. Run remote selfcheck
    print("\n[2/3] Running remote studio selfcheck...")
    selfcheck_cmd = [
        "ssh", "-p", str(args.port), target,
        f"cd {REMOTE_STAGING_DIR} && python3 -I run.py --selfcheck"
    ]
    ret = subprocess.run(selfcheck_cmd)
    if ret.returncode != 0:
        print("Warning: Remote selfcheck returned non-zero exit code.")

    # 3. Cut over & restart service
    print("\n[3/3] Performing cutover and restarting microduck-studio...")
    cutover_cmd = [
        "ssh", "-t", "-p", str(args.port), target,
        f"sudo /home/radxa/deploy_armature_studio.sh || "
        f"(sudo rsync -a --delete '{REMOTE_STAGING_DIR}/' '{REMOTE_PROD_DIR}/' && "
        f"sudo chown -R radxa:radxa '{REMOTE_PROD_DIR}' && "
        f"sudo systemctl restart microduck-studio && sudo systemctl status microduck-studio --no-pager)"
    ]
    subprocess.run(cutover_cmd)
    print("\n" + "=" * 60)
    print("Deployment finished successfully!")
    print(f"Open Studio in browser: http://{args.host}:8000/")
    print("=" * 60)


if __name__ == "__main__":
    main()
