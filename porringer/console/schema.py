"""Data schemas for console commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import LiteralString

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich.console import Console

from porringer.backend.schema import GlobalConfiguration
from porringer.console.output import PORRINGER_THEME, Output, build_console
from porringer.schema import LocalConfiguration

MAX_VERBOSITY_LEVEL = 3
VERBOSITY_DEBUG_THRESHOLD = 2


@dataclass(slots=True)
class LogLevel:
    """Log level metadata."""

    name: LiteralString
    colour: str


LOG_LEVELS: list[LogLevel] = [
    LogLevel(name='ERROR', colour='red'),
    LogLevel(name='WARNING', colour='yellow'),
    LogLevel(name='INFO', colour='white'),
    LogLevel(name='DEBUG', colour='bright_white'),
]


class ConsoleConfiguration(BaseModel):
    """Configuration object for the CLI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    console: Console = Field(default_factory=build_console)
    error_console: Console = Field(default_factory=lambda: build_console(stderr=True))
    local_configuration: LocalConfiguration = LocalConfiguration()
    global_configuration: GlobalConfiguration = GlobalConfiguration()
    debug: bool = False
    verbosity: int = 0
    quiet: bool = False

    @model_validator(mode='after')
    def _apply_theme(self) -> ConsoleConfiguration:
        """Ensure injected consoles understand the semantic theme styles."""
        self.console.push_theme(PORRINGER_THEME)
        self.error_console.push_theme(PORRINGER_THEME)
        return self

    @property
    def output(self) -> Output:
        """Semantic output facade honouring the configured quiet mode."""
        return Output(self.console, self.error_console, quiet=self.quiet)
