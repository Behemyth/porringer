---
icon: lucide/target
---

# Scope and Non-goals

Porringer is a cross-platform orchestrator for developer toolchains. It reads a declarative manifest and drives existing package managers and project tools through plugins.

The boundary is intentional: Porringer coordinates package, runtime, repository, and project setup. It does not replace the tools that actually install packages or configure operating systems.

Porringer is closest to Homebrew Bundle or WinGet Configuration: a declarative file applied through the package managers already on the machine. Graphical clients such as Spurtle build on its API.

## Principles

1. **Changes act on manifest entries.** Every install, upgrade, or uninstall comes from an entry in a project manifest, a setup profile, or the user's global manifest. Porringer has no commands that pass a single package straight through to one plugin.
2. **Porringer owns its manifests.** It reads them and may write the entries it manages. It never writes another tool's files.
3. **Reading is in scope.** Porringer may report installed runtimes, packages, and available updates through its plugins.
4. **Package managers do the work.** Porringer records *what* should exist; the package manager decides *how*.
5. **Callers own application state.** Project registries, schedules, UI preferences, and history belong to the application using Porringer.
6. **Public APIs have documented, reusable contracts.** A CLI command is optional; graphical clients and agents are first-class consumers.

## What Porringer Does and Never Does

| Porringer does | Porringer never does |
| --- | --- |
| Records an entry: name, constraint, installer, runtime | Writes or reads lockfiles of its own |
| Writes its own manifests | Edits `pyproject.toml` dependencies, `package.json`, or any lockfile |
| Asks a package manager to install, upgrade, or uninstall an entry | Resolves dependency trees or picks transitive versions |
| Reads installed state through plugins | Keeps its own database of installed packages |
| Orders work by phase: runtimes, packages, tools, projects, repositories | Orders packages by their dependencies |

## In Scope

- Declaring packages, CLI tools, language runtimes, and source repositories.
- Resolving installers per ecosystem through plugins.
- Resolving project install through project plugins and repository evidence.
- Running setup in a deterministic phase order.
- Inspecting manifests and reporting structured diagnostics before execution.
- Reporting installed packages and available updates through plugins.
- Supporting local manifests, HTTPS setup profiles, and hash-verified downloads.

## Non-goals

- **Machine or OS provisioning.** Porringer does not configure registry settings, shell themes, fonts, terminal profiles, services, or reboot choreography.
- **Platform-specific core behavior.** Core manifest semantics are platform-neutral. Platform specifics belong in plugins.
- **Replacing substrate package managers.** Porringer declares what should exist. Tools such as `apt`, `winget`, `pip`, `npm`, and `pdm` still do the installation work.
- **Provisioning WSL distributions.** Porringer does not create or configure WSL distributions.
- **Managing remote machines.** Porringer targets the current machine or session. It is not remote host orchestration.

## Project Install Policy

Project-local dependency install belongs to project-install plugins, not arbitrary manifest commands. Plugins identify relevant projects from marker files, lock files, and tool-specific configuration. Porringer then selects one project-install owner per ecosystem.

Custom imperative setup remains outside the core manifest. If a workflow needs arbitrary shell commands, keep those commands in the project tool that owns them and let Porringer invoke the tool through its plugin.
