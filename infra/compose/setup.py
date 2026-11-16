"""Prepare local Compose secrets with Python 3.11+, without backend dependencies."""

import argparse
import os
import secrets
import sys
from pathlib import Path

PASSWORD_FILES = ("postgres_password", "migrator_password", "runtime_password")
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[2] / ".secrets" / "compose"


def prepare(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [directory / name for name in PASSWORD_FILES]
    if any(path.exists() or path.is_symlink() for path in paths):
        if not all(path.is_file() and not path.is_symlink() for path in paths):
            raise ValueError("Incomplete secret set")
        values = [path.read_text(encoding="utf-8").rstrip("\r\n") for path in paths]
        if len(set(values)) != len(paths) or any(
            not value or any(char in value for char in "\r\n\0") for value in values
        ):
            raise ValueError("Invalid secret set")
        return
    # Exclusive creation preserves existing credentials even after interruption
    # or concurrent invocations. An incomplete set requires explicit recovery.
    for path in paths:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(secrets.token_hex(32))
            if os.name == "posix":
                # File bind mounts preserve host permissions. A restrictive
                # umask must not remove read access for the container UID.
                os.chmod(stream.fileno(), 0o444)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    arguments = parser.parse_args()
    try:
        prepare(arguments.directory)
    except (OSError, UnicodeError, ValueError):
        print(
            "Secret preparation failed. Check the directory and complete secret set; "
            "existing files were not replaced.",
            file=sys.stderr,
        )
        return 1
    print("Compose secrets are ready; existing credentials were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
