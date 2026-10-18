"""CLI command implementation for profile.

Portable setup profile commands.
"""

import json
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from porringer.backend.command.core.discovery import DiscoveredPlugins
from porringer.backend.command.sync import SyncCommands
from porringer.schema import (
    DownloadParameters,
    InspectionMode,
    SetupParameters,
    SetupProfile,
    SetupProfileExecution,
    SetupProfileInspection,
)
from porringer.utility.download import download_file


class ProfileCommands:
    """Resolve, inspect, and execute portable setup profiles."""

    def __init__(self, sync_commands: SyncCommands) -> None:
        """Initialize profile commands."""
        self._sync = sync_commands

    @staticmethod
    async def resolve(
        url: str,
        *,
        timeout_seconds: int = 300,
        expected_hash: str | None = None,
    ) -> SetupProfile:
        """Download, parse, and validate a setup profile."""
        validate_profile_url(url)
        tmp_dir = Path(tempfile.mkdtemp(prefix='porringer_profile_'))
        try:
            destination = tmp_dir / 'profile.json'
            result = await download_file(
                DownloadParameters(
                    url=url,
                    destination=destination,
                    timeout=timeout_seconds,
                    expected_hash=expected_hash,
                )
            )
            if not result.success:
                raise ValueError(f'Failed to download profile from {url}: {result.message}')
            try:
                profile = SetupProfile.model_validate(json.loads(destination.read_text(encoding='utf-8')))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f'Failed to parse profile from {url}') from exc
            for manifest in profile.manifests:
                validate_profile_url(manifest.url)
            return profile
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    async def _download_profile_manifests(
        profile: SetupProfile,
        *,
        timeout_seconds: int = 300,
    ) -> tuple[Path, list[Path]]:
        """Download profile manifests into a temporary directory.

        Each manifest is optionally hash-verified when ``expected_hash`` is
        provided in the profile entry.
        """
        tmp_dir = Path(tempfile.mkdtemp(prefix='porringer_profile_manifests_'))
        paths: list[Path] = []
        completed = False
        try:
            for index, manifest in enumerate(profile.manifests):
                destination = tmp_dir / f'manifest_{index}.json'
                result = await download_file(
                    DownloadParameters(
                        url=manifest.url,
                        destination=destination,
                        timeout=timeout_seconds,
                        expected_hash=manifest.expected_hash,
                    )
                )
                if not result.success:
                    raise ValueError(f'Failed to download manifest from {manifest.url}: {result.message}')
                paths.append(destination)
            completed = True
        finally:
            # Also covers cancellation, which is not an ``Exception``.
            if not completed:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return tmp_dir, paths

    async def inspect(
        self,
        url: str,
        *,
        inspection_mode: InspectionMode = InspectionMode.FAST,
        plugins: DiscoveredPlugins | None = None,
        expected_hash: str | None = None,
    ) -> SetupProfileInspection:
        """Resolve a setup profile and inspect all referenced manifests."""
        profile = await self.resolve(url, expected_hash=expected_hash)
        tmp_dir, manifest_paths = await self._download_profile_manifests(profile)
        try:
            report = await self._sync.inspect(
                SetupParameters(paths=manifest_paths, inspection_mode=inspection_mode),
                plugins=plugins,
            )
            return SetupProfileInspection(
                status=report.status,
                profile=profile,
                inspection=report,
                diagnostics=report.diagnostics,
                follow_up_actions=report.follow_up_actions,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def run(
        self,
        url: str,
        *,
        parameters: SetupParameters | None = None,
        plugins: DiscoveredPlugins | None = None,
        expected_hash: str | None = None,
    ) -> SetupProfileExecution:
        """Resolve a setup profile and execute all referenced manifests."""
        profile = await self.resolve(url, expected_hash=expected_hash)
        tmp_dir, manifest_paths = await self._download_profile_manifests(profile)
        try:
            effective = (
                parameters.model_copy(update={'paths': manifest_paths})
                if parameters
                else SetupParameters(paths=manifest_paths)
            )
            report = await self._sync.run(effective, plugins=plugins)
            return SetupProfileExecution(
                status=report.status,
                profile=profile,
                results=report.results,
                diagnostics=report.diagnostics,
                follow_up_actions=report.follow_up_actions,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def validate_profile_url(url: str) -> None:
    """Validate that a profile or profile manifest URL uses HTTPS."""
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise ValueError(f'Only HTTPS URLs are allowed for setup profiles, got: {url}')
