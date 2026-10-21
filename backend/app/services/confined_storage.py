"""Descriptor-relative access to a server-selected private storage namespace."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path


@contextmanager
def confined_storage_parent(root: Path, relative: str, *, create=False):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError("Unsafe storage path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = check = None
    try:
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(root, flags)
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        anchored = os.fstat(directory)
        yield directory, parts[-1]
        check = os.open(root, flags)
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=check)
            os.close(check)
            check = child
        current = os.fstat(check)
        if (current.st_dev, current.st_ino) != (anchored.st_dev, anchored.st_ino):
            raise OSError(errno.ESTALE, "Storage directory changed during access")
    finally:
        if check is not None:
            os.close(check)
        if directory is not None:
            os.close(directory)
