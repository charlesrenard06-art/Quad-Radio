#!/usr/bin/env python3
"""Secure Quad Radio release downloader and installer."""

import argparse
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

OWNER = "charlesrenard06-art"
REPO = "Quad-Radio"
RELEASE_API = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
USER_AGENT = "QuadRadioUpdater/1.0"
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024

ROOT_FILES = {
    "app.py", "config.json", "requirements.txt",
    "VERSION", "updater.py"
}
ALLOWED_ROOTS = ROOT_FILES | {"static", "templates"}


def latest_release():
    response = requests.get(
        RELEASE_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
        timeout=20,
    )
    response.raise_for_status()
    release = response.json()

    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError("The latest release is not a stable release.")

    tag = release.get("tag_name", "")
    version = tag[1:] if tag.startswith("v") else tag

    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("Invalid release version tag.")

    assets = {item["name"]: item for item in release.get("assets", [])}
    zip_name = f"quad-radio-{version}.zip"
    checksum_name = f"quad-radio-{version}.sha256"

    if zip_name not in assets or checksum_name not in assets:
        raise RuntimeError("Release is missing its ZIP or checksum file.")

    urls = []
    for name in (zip_name, checksum_name):
        url = assets[name].get("browser_download_url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise RuntimeError("Release download URL failed security validation.")
        urls.append(url)

    return version, urls[0], urls[1]


def download(url, destination, limit):
    """Download a release asset with retries for temporary network failures."""
    destination = Path(destination)
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    last_error = None

    for attempt in range(4):
        total = 0

        try:
            if destination.exists():
                destination.unlink()

            with requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=(20, 90),
            ) as response:
                response.raise_for_status()

                with open(destination, "wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if not chunk:
                            continue

                        total += len(chunk)
                        if total > limit:
                            raise RuntimeError(
                                "Download exceeded the size limit."
                            )

                        output.write(chunk)

            if total == 0:
                raise RuntimeError("Downloaded release asset is empty.")

            return

        except requests.exceptions.RequestException as error:
            last_error = error

            if destination.exists():
                destination.unlink()

            status = getattr(
                getattr(error, "response", None),
                "status_code",
                None,
            )

            if status is not None and status not in retryable_statuses:
                raise

            if attempt == 3:
                break

            delay = 2 ** (attempt + 1)
            print(
                f"Download attempt {attempt + 1}/4 failed "
                f"({error}); retrying in {delay}s...",
                flush=True,
            )
            time.sleep(delay)

        except Exception:
            if destination.exists():
                destination.unlink()
            raise

    raise RuntimeError(
        f"Release download failed after 4 attempts: {last_error}"
    )

def verified_archive(zip_path, checksum_path):
    expected = checksum_path.read_text().strip().split()[0].lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RuntimeError("Invalid SHA-256 checksum file.")

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError("SHA-256 verification failed.")


def safe_extract(zip_path, destination):
    destination = Path(destination).resolve()
    expanded = 0

    with zipfile.ZipFile(zip_path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000:
            raise RuntimeError("Archive contains too many entries.")

        seen_paths = set()

        for item in entries:
            name = item.filename

            if not name or "\\" in name or "\x00" in name:
                raise RuntimeError("Unsafe archive path.")
            if item.flag_bits & 0x1:
                raise RuntimeError("Encrypted archive entries are not allowed.")

            path = PurePosixPath(name)

            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("Archive contains a path traversal attempt.")

            normalized = "/".join(path.parts)
            if normalized in seen_paths:
                raise RuntimeError("Archive contains duplicate paths.")
            seen_paths.add(normalized)

            mode = item.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                raise RuntimeError("Archive contains a special filesystem entry.")
            if item.is_dir() and file_type == stat.S_IFREG:
                raise RuntimeError("Archive directory metadata is inconsistent.")
            if not item.is_dir() and file_type == stat.S_IFDIR:
                raise RuntimeError("Archive file metadata is inconsistent.")

            parts = path.parts
            if not parts or parts[0] != "quad-radio":
                raise RuntimeError("Unexpected archive folder.")

            relative = parts[1:]
            if not relative:
                continue

            if relative[0] not in ALLOWED_ROOTS:
                raise RuntimeError(f"Unexpected archive content: {name}")

            if len(relative) == 1 and relative[0] not in ROOT_FILES:
                if not (item.is_dir() and relative[0] in {"static", "templates"}):
                    raise RuntimeError(f"Unexpected root file: {name}")

            if len(relative) > 1 and relative[0] not in {"static", "templates"}:
                raise RuntimeError(f"Unexpected nested path: {name}")

            expanded += item.file_size
            if expanded > MAX_EXPANDED:
                raise RuntimeError("Archive exceeds the extraction size limit.")

        archive.extractall(destination)

    extracted = destination / "quad-radio"
    if not (extracted / "app.py").is_file() or not (extracted / "VERSION").is_file():
        raise RuntimeError("Release archive is missing required application files.")

    return extracted


def install(app_dir, data_dir, parent_pid):
    app_dir = Path(app_dir).resolve()
    data_dir = Path(data_dir).resolve()

    if not app_dir.is_dir() or not data_dir.is_dir():
        raise RuntimeError("Application or data directory does not exist.")

    if (
        app_dir == data_dir
        or app_dir in data_dir.parents
        or data_dir in app_dir.parents
    ):
        raise RuntimeError("Application and user data directories overlap.")

    venv_python = app_dir / ".venv" / "bin" / "python"
    launcher = data_dir.parent / "launch.sh"

    # Validate the existing Chromebook runtime before stopping the server.
    if not venv_python.is_file() or not os.access(venv_python, os.X_OK):
        raise RuntimeError("Python virtual environment is missing or not executable.")

    if not launcher.is_file():
        raise RuntimeError("Quad Radio launcher was not found.")

    subprocess.run(
        [str(venv_python), "-m", "pip", "--version"],
        check=True,
        timeout=20,
        stdout=subprocess.DEVNULL,
    )

    version, zip_url, checksum_url = latest_release()
    current = (
        (app_dir / "VERSION").read_text().strip()
        if (app_dir / "VERSION").exists()
        else "0.0.0"
    )

    def version_tuple(value):
        parts = tuple(int(part) for part in value.split("."))
        return parts + (0,) * (3 - len(parts))

    if version_tuple(version) <= version_tuple(current):
        print(f"Already running version {current}; refusing downgrade to {version}.")
        return

    print(f"Preparing Quad Radio {version}...")

    with tempfile.TemporaryDirectory(prefix="quad-radio-update-") as temporary:
        temporary = Path(temporary)
        zip_path = temporary / "release.zip"
        checksum_path = temporary / "release.sha256"

        download(zip_url, zip_path, MAX_DOWNLOAD)
        download(checksum_url, checksum_path, 4096)
        verified_archive(zip_path, checksum_path)
        extracted = safe_extract(zip_path, temporary)

        if (extracted / "VERSION").read_text().strip() != version:
            raise RuntimeError("Downloaded archive version does not match release tag.")

        backup = data_dir / "update-backup"
        if backup.exists():
            shutil.rmtree(backup)

        shutil.copytree(
            app_dir,
            backup,
            ignore=shutil.ignore_patterns(".venv", "__pycache__")
        )

        # Stop the running Flask process only after validating the release.
        try:
            os.kill(parent_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        for _ in range(30):
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(1)
        else:
            raise RuntimeError("The radio server did not stop; update cancelled.")

        try:
            # Replace application files while preserving the virtual environment.
            for child in list(app_dir.iterdir()):
                if child.name == ".venv":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

            for child in extracted.iterdir():
                target = app_dir / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)

            subprocess.run(
                [str(venv_python), "-m", "pip", "install",
                 "-r", str(app_dir / "requirements.txt")],
                check=True,
                timeout=300,
            )

            subprocess.Popen(
                ["/bin/bash", str(launcher)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            # Do not report success until Flask responds.
            healthy = False
            for _ in range(30):
                try:
                    response = requests.get(
                        "http://127.0.0.1:5055/",
                        timeout=2,
                    )
                    if response.status_code == 200:
                        healthy = True
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)

            if not healthy:
                raise RuntimeError(
                    "Updated application did not become healthy. "
                    "Restoring the previous application."
                )

            print(f"Updated successfully to version {version}.")

        except Exception:
            # Stop a partially started updated server, if one exists.
            pid_file = data_dir / "server.pid"
            if pid_file.exists():
                try:
                    new_pid = int(pid_file.read_text().strip())
                    if new_pid > 1 and new_pid != parent_pid:
                        os.kill(new_pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass

            # Restore application files without touching .venv or user data.
            app_dir.mkdir(parents=True, exist_ok=True)
            for child in list(app_dir.iterdir()):
                if child.name == ".venv":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

            for child in backup.iterdir():
                target = app_dir / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)

            subprocess.Popen(
                ["/bin/bash", str(launcher)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            raise

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--app")
    parser.add_argument("--data")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()

    if args.check:
        version, _, _ = latest_release()
        print(version)
        return

    if args.install:
        if not args.app or not args.data or not args.pid:
            parser.error("--install requires --app, --data and --pid")
        install(args.app, args.data, args.pid)
        return

    parser.error("Choose --check or --install")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Updater error: {error}", file=sys.stderr)
        sys.exit(1)
