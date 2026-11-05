"""Prepare verified local copies of static site releases."""

import hashlib
import shutil
import stat
import tempfile
from pathlib import Path


def _inventory(root: Path) -> dict[Path, tuple[str, str]]:
    """Record directories and file contents without following symbolic links."""
    entries = {}

    def visit(path: Path) -> None:
        mode = path.lstat().st_mode
        relative = path.relative_to(root)
        if stat.S_ISDIR(mode):
            entries[relative] = ("directory", "")
            for child in path.iterdir():
                visit(child)
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries[relative] = ("file", digest.hexdigest())
        else:
            raise ValueError(f"Unsupported site entry: {path}")

    visit(root)
    if entries[Path(".")][0] != "directory":
        raise ValueError(f"Site root is not a directory: {root}")
    return entries


def _set_permissions(root: Path, inventory: dict[Path, tuple[str, str]]) -> None:
    for path, (kind, _) in inventory.items():
        (root / path).chmod(0o755 if kind == "directory" else 0o644)


def prepare_site_copy(source: Path, destination: Path) -> None:
    """Verify or replace a local release copy, recovering interrupted replacement."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.previous")
    if backup.is_symlink() or destination.is_symlink():
        raise ValueError("Symbolic links are forbidden for site copies")
    if backup.exists() and not backup.is_dir():
        raise ValueError(f"Invalid site backup: {backup}")
    if backup.exists() and not destination.exists():
        backup.rename(destination)
    source_entries = _inventory(source)
    if destination.exists():
        existing = _inventory(destination)
        if existing == source_entries:
            _set_permissions(destination, existing)
            if backup.exists():
                shutil.rmtree(backup)
            return
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.staging-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "site"
        shutil.copytree(source, staged, symlinks=True)
        staged_entries = _inventory(staged)
        if staged_entries != source_entries or _inventory(source) != source_entries:
            raise ValueError("Site copy does not match source")
        _set_permissions(staged, staged_entries)
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except BaseException:
            if backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
