"""Helpers for test package relation.

Tests for PackageRelation, pipx injection metadata, and PackageCache.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from packaging.version import Version

from porringer.backend.command.core.resolution import PackageCache, is_package_installed
from porringer.core.plugin_schema.environment import Environment
from porringer.core.plugin_schema.runtime import RuntimeContext
from porringer.core.schema import (
    Distribution,
    Package,
    PackageRef,
    PackageRelation,
    PackageRelationKind,
    PluginParameters,
)
from porringer.plugin.pipx.plugin import PIPXEnvironment
from tests.fixtures.factories import make_environment

_MOCK_PARAMS = PluginParameters(distribution=Distribution(version=Version('0.0.0')))

_EXPECTED_PIPX_PACKAGES = 3
"""pdm + cppython (injected) + ruff."""

_EXPECTED_TWO_CALLS = 2
"""Sentinel for assertions that expect exactly two invocations."""


# ---------------------------------------------------------------------------
# PackageRelation schema round-trip
# ---------------------------------------------------------------------------


class TestPackageRelationSchema:
    """Verify PackageRelation and Package serialisation."""

    @staticmethod
    def test_package_defaults_to_no_relation() -> None:
        """A bare Package has relation=None."""
        pkg = Package(name='ruff', version='0.8.0')
        assert pkg.relation is None

    @staticmethod
    def test_package_with_injected_relation() -> None:
        """Package can carry an INJECTED relation."""
        pkg = Package(
            name='cppython',
            version='0.5.0',
            relation=PackageRelation(host='pdm', kind=PackageRelationKind.INJECTED),
        )
        assert pkg.relation is not None
        assert pkg.relation.host == 'pdm'
        assert pkg.relation.kind == PackageRelationKind.INJECTED

    @staticmethod
    def test_pydantic_round_trip() -> None:
        """model_dump then model_validate preserves relation."""
        relation = PackageRelation(host='pdm', kind=PackageRelationKind.INJECTED)
        pkg = Package(name='cppython', version='0.5.0', relation=relation)
        data = pkg.model_dump()
        restored = Package.model_validate(data)
        assert restored.relation is not None
        assert restored.relation.host == 'pdm'
        assert restored.relation.kind == PackageRelationKind.INJECTED


# ---------------------------------------------------------------------------
# pipx plugin — injected package metadata
# ---------------------------------------------------------------------------


class TestPipxInjectedPackages:
    """Verify that the pipx plugin's packages() annotates injected packages."""

    @staticmethod
    def _make_pipx_venvs(tmp: Path) -> Path:
        """Create a fake pipx venvs directory with metadata."""
        venvs = tmp / 'venvs'
        venvs.mkdir()

        # A tool with one injected package
        pdm_dir = venvs / 'pdm'
        pdm_dir.mkdir()
        metadata = {
            'main_package': {'package': 'pdm', 'package_version': '2.20.0'},
            'injected_packages': {
                'cppython': {'package': 'cppython', 'package_version': '0.5.0'},
            },
        }
        (pdm_dir / 'pipx_metadata.json').write_text(json.dumps(metadata))

        # A standalone tool (no injected packages)
        ruff_dir = venvs / 'ruff'
        ruff_dir.mkdir()
        ruff_meta = {
            'main_package': {'package': 'ruff', 'package_version': '0.8.0'},
            'injected_packages': {},
        }
        (ruff_dir / 'pipx_metadata.json').write_text(json.dumps(ruff_meta))

        return venvs

    @staticmethod
    async def test_injected_packages_carry_relation() -> None:
        """Injected packages should have relation.kind == INJECTED."""
        with tempfile.TemporaryDirectory() as tmp:
            venvs = TestPipxInjectedPackages._make_pipx_venvs(Path(tmp))
            env = PIPXEnvironment(_MOCK_PARAMS)

            with patch('porringer.plugin.pipx.plugin._get_pipx_venvs_dir', return_value=venvs):
                packages = await env.packages()

            assert len(packages) == _EXPECTED_PIPX_PACKAGES

            by_name = {p.name: p for p in packages}

            # pdm is a main package — no relation
            assert by_name['pdm'].relation is None
            assert by_name['pdm'].version == '2.20.0'

            # cppython is injected into pdm
            assert by_name['cppython'].relation is not None
            assert by_name['cppython'].relation.host == 'pdm'
            assert by_name['cppython'].relation.kind == PackageRelationKind.INJECTED
            assert by_name['cppython'].version == '0.5.0'

            # ruff is a standalone tool — no relation
            assert by_name['ruff'].relation is None

    @staticmethod
    async def test_empty_venvs_dir() -> None:
        """Empty pipx venvs directory returns empty list."""
        with tempfile.TemporaryDirectory() as tmp:
            venvs = Path(tmp) / 'venvs'
            venvs.mkdir()
            env = PIPXEnvironment(_MOCK_PARAMS)

            with patch('porringer.plugin.pipx.plugin._get_pipx_venvs_dir', return_value=venvs):
                packages = await env.packages()

            assert packages == []

    @staticmethod
    async def test_missing_venvs_dir() -> None:
        """Non-existent pipx venvs directory returns empty list."""
        with tempfile.TemporaryDirectory() as tmp:
            venvs = Path(tmp) / 'nonexistent'
            env = PIPXEnvironment(_MOCK_PARAMS)

            with patch('porringer.plugin.pipx.plugin._get_pipx_venvs_dir', return_value=venvs):
                packages = await env.packages()

            assert packages == []

    @staticmethod
    async def test_corrupt_metadata_skipped() -> None:
        """Venvs with corrupt metadata are silently skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            venvs = Path(tmp) / 'venvs'
            venvs.mkdir()
            bad_dir = venvs / 'broken'
            bad_dir.mkdir()
            (bad_dir / 'pipx_metadata.json').write_text('not valid json{{{')

            env = PIPXEnvironment(_MOCK_PARAMS)

            with patch('porringer.plugin.pipx.plugin._get_pipx_venvs_dir', return_value=venvs):
                packages = await env.packages()

            assert packages == []


# ---------------------------------------------------------------------------
# PackageCache
# ---------------------------------------------------------------------------


class TestPackageCache:
    """Verify the resolution-layer PackageCache."""

    @staticmethod
    async def test_single_call_per_installer() -> None:
        """packages() is called once even with multiple cache.get_packages() calls."""
        env = make_environment(installed=[Package(name='ruff', version='0.8.0')])

        cache = PackageCache()

        result1 = await cache.get_packages('pip', env)
        result2 = await cache.get_packages('pip', env)
        result3 = await cache.get_packages('pip', env)

        # All three calls return the same list
        assert result1 == result2 == result3
        assert len(result1) == 1
        assert result1[0].name == 'ruff'

        # But env.packages was only called once
        env.packages.assert_awaited_once()

        stats = cache.stats()
        assert stats.package_misses == 1
        assert stats.package_hits == _EXPECTED_TWO_CALLS

    @staticmethod
    async def test_different_installers_cached_separately() -> None:
        """Different installers get separate cache entries."""
        env_pip = make_environment(installed=[Package(name='ruff', version='0.8.0')])
        env_uv = make_environment(installed=[Package(name='black', version='24.0')])

        cache = PackageCache()

        result_pip = await cache.get_packages('pip', env_pip)
        result_uv = await cache.get_packages('uv', env_uv)

        assert result_pip[0].name == 'ruff'
        assert result_uv[0].name == 'black'
        env_pip.packages.assert_awaited_once()
        env_uv.packages.assert_awaited_once()

    @staticmethod
    async def test_invalidation_triggers_requery() -> None:
        """After invalidation, the next get_packages re-queries."""
        env = MagicMock(spec=Environment)
        call_count = 0

        async def _packages(*, project_path=None, runtime_context=None):
            nonlocal call_count
            call_count += 1
            return [Package(name='ruff', version=f'0.{call_count}.0')]

        env.packages = _packages

        cache = PackageCache()

        result1 = await cache.get_packages('pip', env)
        assert result1[0].version == '0.1.0'

        cache.invalidate_packages('pip')

        result2 = await cache.get_packages('pip', env)
        assert result2[0].version == '0.2.0'
        assert call_count == _EXPECTED_TWO_CALLS

        stats = cache.stats()
        assert stats.package_misses == _EXPECTED_TWO_CALLS
        assert stats.package_invalidations == 1

    @staticmethod
    async def test_invalidate_all_clears_everything() -> None:
        """invalidate_all clears all cached data."""
        env = make_environment(installed=[Package(name='ruff', version='0.8.0')])

        cache = PackageCache()

        await cache.get_packages('pip', env)
        cache.invalidate_all()
        await cache.get_packages('pip', env)

        assert env.packages.await_count == _EXPECTED_TWO_CALLS

    @staticmethod
    async def test_runtimes_cached_separately_and_invalidated_together() -> None:
        """Different interpreters get separate entries; invalidation drops all of them."""
        env = make_environment(installed=[Package(name='ruff', version='0.8.0')])
        py312 = RuntimeContext(executables={'python': Path('py312')})
        py313 = RuntimeContext(executables={'python': Path('py313')})
        cache = PackageCache()

        await cache.get_packages('pip', env, runtime_context=py312)
        await cache.get_packages('pip', env, runtime_context=py313)
        assert env.packages.await_count == _EXPECTED_TWO_CALLS

        cache.invalidate_packages('pip')
        await cache.get_packages('pip', env, runtime_context=py312)

        assert cache.stats().package_invalidations == _EXPECTED_TWO_CALLS
        assert env.packages.await_count == _EXPECTED_TWO_CALLS + 1

    @staticmethod
    async def test_invalidation_does_not_match_installer_prefix() -> None:
        """Invalidating 'pip' leaves 'pipx' cached."""
        env = make_environment()
        cache = PackageCache()

        await cache.get_packages('pipx', env)
        cache.invalidate_packages('pip')

        assert cache.stats().package_invalidations == 0

    @staticmethod
    async def test_concurrent_access_serializes() -> None:
        """Concurrent get_packages calls for the same key don't race."""
        call_count = 0

        async def _slow_packages(*, project_path=None, runtime_context=None):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # Simulate slow I/O
            return [Package(name='ruff', version='0.8.0')]

        env = MagicMock(spec=Environment)
        env.packages = _slow_packages

        cache = PackageCache()

        # Launch 5 concurrent requests for the same installer
        results = await asyncio.gather(
            cache.get_packages('pip', env),
            cache.get_packages('pip', env),
            cache.get_packages('pip', env),
            cache.get_packages('pip', env),
            cache.get_packages('pip', env),
        )

        # All should return the same data
        for r in results:
            assert len(r) == 1
            assert r[0].name == 'ruff'

        # But the underlying function was only called once
        assert call_count == 1

        stats = cache.stats()
        assert stats.package_misses == 1
        assert stats.package_hits == len(results) - 1


# ---------------------------------------------------------------------------
# is_package_installed ignores relation
# ---------------------------------------------------------------------------


class TestPresenceIgnoresRelation:
    """Verify that is_package_installed works regardless of relation."""

    @staticmethod
    def test_installed_with_relation() -> None:
        """A package with a relation is still found by name."""
        pkg = PackageRef.model_validate('cppython')
        installed = Package(
            name='cppython',
            version='0.5.0',
            relation=PackageRelation(host='pdm', kind=PackageRelationKind.INJECTED),
        )
        ok, _detail, matched = is_package_installed(pkg, [installed], 'pep440')
        assert ok is True
        assert matched is installed

    @staticmethod
    def test_installed_without_relation() -> None:
        """A package without a relation is found normally."""
        pkg = PackageRef.model_validate('ruff')
        installed = Package(name='ruff', version='0.8.0')
        ok, _detail, matched = is_package_installed(pkg, [installed], 'pep440')
        assert ok is True
        assert matched is installed
