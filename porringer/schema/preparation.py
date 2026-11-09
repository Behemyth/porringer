"""Versioned metadata reported by the project-owned native preparation command."""

from pathlib import Path
from typing import Literal

from pydantic import Field

from porringer.core.schema import PorringerModel
from porringer.schema.observability import Diagnostic

PREPARATION_REPORT_ENV = 'CPPYTHON_PREPARATION_REPORT'


class PreparationMetadata(PorringerModel, extra='forbid'):
    """Authoritative selection, never inferred from paths or identity names."""

    schema_version: Literal['1']
    identity: str | None = None
    backend: str
    provider: str
    dependency_outputs: tuple[Path, ...] | None = None


class PreparationStatus(PorringerModel):
    """Preparation-only status; native build and test are deliberately unverified."""

    action_id: str | None = None
    outcome: Literal['prepared', 'failed', 'skipped', 'not_requested']
    metadata: PreparationMetadata | None = None
    native_build: Literal['not_verified'] = 'not_verified'
    native_test: Literal['not_verified'] = 'not_verified'
    diagnostics: tuple[Diagnostic, ...] = Field(default_factory=tuple)


class InstallPayload(PorringerModel):
    """Version one of the install preparation contract, independent of event schema versions."""

    schema_version: Literal['1'] = '1'
    outcome: Literal['completed', 'failed', 'cancelled']
    preparation: tuple[PreparationStatus, ...] = Field(default_factory=tuple)
    native_build: Literal['not_verified'] = 'not_verified'
    native_test: Literal['not_verified'] = 'not_verified'
