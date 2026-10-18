"""File fingerprints that tell whether a sync plan is still current."""

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path

from porringer.schema import SetupResults


def file_digest(path: Path) -> str:
    """Return the SHA-256 of *path*, or an empty string when it is missing or unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ''


def plan_fingerprints(inputs: Iterable[Path], previews: Iterable[SetupResults]) -> dict[Path, str]:
    """Fingerprint every file a plan was built from.

    That is each input file, each loaded manifest, and the
    ``pyproject.toml`` beside each manifest's root, which can host or
    reference the manifest and feeds project inference.
    """
    paths: set[Path] = {path.resolve() for path in inputs if path.is_file()}
    for preview in previews:
        if preview.manifest_path is not None:
            paths.add(preview.manifest_path.resolve())
        if preview.root_directory is not None:
            host = preview.root_directory / 'pyproject.toml'
            if host.is_file():
                paths.add(host.resolve())
    return {path: file_digest(path) for path in sorted(paths)}


def changed_paths(fingerprints: Mapping[Path, str]) -> tuple[Path, ...]:
    """Return the fingerprinted files whose contents changed, appeared, or vanished."""
    return tuple(path for path, digest in fingerprints.items() if file_digest(path) != digest)
