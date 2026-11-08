#!/bin/bash
set -Eeuo pipefail

OWNER="charlesrenard06-art"
REPO="Quad-Radio"
REMOTE="origin"
BRANCH="main"

if [[ $# -ne 1 ]]; then
    echo "Usage: ./publish_release.sh VERSION"
    echo "Example: ./publish_release.sh 1.0.0"
    exit 2
fi

VERSION="$1"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: Version must use MAJOR.MINOR.PATCH format."
    exit 2
fi

TAG="v$VERSION"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "===== PRE-PUBLISH VALIDATION ====="

command -v git >/dev/null || { echo "Missing git."; exit 1; }
command -v gh >/dev/null || { echo "Missing GitHub CLI."; exit 1; }
command -v zip >/dev/null || { echo "Missing zip."; exit 1; }

gh auth status
git remote get-url "$REMOTE" >/dev/null

if [[ -n "$(git diff --cached --name-only)" ]]; then
    echo "ERROR: Git staging area is not empty."
    echo "Commit or unstage existing staged changes first."
    exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: Git tag $TAG already exists."
    exit 1
fi

if gh release view "$TAG" --repo "$OWNER/$REPO" >/dev/null 2>&1; then
    echo "ERROR: GitHub release $TAG already exists."
    exit 1
fi

for file in app.py config.json requirements.txt VERSION updater.py \
            templates/index.html; do
    [[ -f "$file" ]] || { echo "Missing required file: $file"; exit 1; }
done

echo "$VERSION" > VERSION

echo ""
echo "===== STAGE APPROVED SOURCE FILES ONLY ====="

git add \
    app.py \
    config.json \
    requirements.txt \
    VERSION \
    updater.py \
    publish_release.sh \
    templates \
    static

git diff --cached --check

echo ""
echo "Files staged for publication:"
git diff --cached --name-only

echo ""
echo "===== COMMIT AND PUSH ====="

git commit -m "Release Quad Radio $VERSION"

echo ""
echo "===== BUILD RELEASE ARCHIVE ====="

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

ZIP_NAME="quad-radio-$VERSION.zip"
ZIP_PATH="$TEMP_DIR/$ZIP_NAME"
SUM_PATH="$TEMP_DIR/quad-radio-$VERSION.sha256"

git archive \
    --format=zip \
    --prefix=quad-radio/ \
    --output="$ZIP_PATH" \
    HEAD \
    app.py config.json requirements.txt VERSION updater.py static templates

shasum -a 256 "$ZIP_PATH" > "$SUM_PATH"

echo ""
echo "===== VERIFY ARCHIVE CONTENTS ====="

.venv/bin/python - "$ZIP_PATH" "$SUM_PATH" "$VERSION" <<'PY'
import sys
import tempfile
import zipfile
from pathlib import Path

from updater import safe_extract, verified_archive

archive = Path(sys.argv[1])
checksum = Path(sys.argv[2])
version = sys.argv[3]

verified_archive(archive, checksum)

with zipfile.ZipFile(archive) as z:
    names = z.namelist()

    forbidden = [
        name for name in names
        if any(token in name.lower() for token in (
            ".sqlite", ".db", ".backup", ".log",
            ".env", ".venv", "__pycache__", "installer"
        ))
    ]
    if forbidden:
        raise SystemExit(f"Forbidden files in release: {forbidden}")

with tempfile.TemporaryDirectory() as temp:
    extracted = safe_extract(archive, temp)
    if (extracted / "VERSION").read_text().strip() != version:
        raise SystemExit("Archive version does not match release version.")
    for required in ("app.py", "config.json", "requirements.txt", "updater.py"):
        if not (extracted / required).is_file():
            raise SystemExit(f"Missing required file: {required}")

print("PASS: SHA-256 checksum verified.")
print("PASS: Archive extracted with updater security checks.")
print("PASS: Required application files present.")
print("PASS: No database, backup, log, environment, or installer files.")
PY

echo ""
echo "===== PUSH VERIFIED SOURCE ====="
git push "$REMOTE" "$BRANCH"

echo "===== PUBLISH GITHUB RELEASE ====="

gh release create "$TAG" \
    "$ZIP_PATH" \
    "$SUM_PATH" \
    --repo "$OWNER/$REPO" \
    --title "Quad Radio $VERSION" \
    --notes "Quad Radio $VERSION. Includes the in-app update checker and verified updater."

echo ""
echo "===== RELEASE PUBLISHED ====="
gh release view "$TAG" --repo "$OWNER/$REPO" \
    --json tagName,url,assets
