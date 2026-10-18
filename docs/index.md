---
icon: lucide/home
---

# Porringer

Porringer is a CLI and Python API for synchronizing developer environments from a declarative manifest. A manifest says what should exist, such as Python packages, Node packages, runtimes, tools, or Git repositories. Porringer chooses the installed plugin that knows how to make that happen.

Use Porringer when you want one repeatable entry point for setup while still relying on the package managers your projects already use.

## Features

- Manifest-driven setup from `porringer.json` or `[tool.porringer]` in `pyproject.toml`.
- Plugin-based installers for Python, Node, system packages, project install, runtimes, and source repositories.
- Backend resolution: declare an ecosystem such as `python`, and Porringer selects an available installer such as `uv` or `pip`.
- Read-only previews with stable action IDs, native command previews, diagnostics, and JSON output.
- Sync strategies for minimal installs or latest allowed versions.
- Download and profile flows with HTTPS and hash verification.
- Python APIs for inspection, execution, profiles, tools, and progress events.

## Commands

| Command | Description |
| --- | --- |
| `porringer preview` | Inspect manifest actions and diagnostics without installing anything. |
| `porringer install` | Execute a manifest, setup profile, or install link after confirmation. |
| `porringer open` | Open a `porringer://` link as a read-only preview. |
| `porringer check` | Check available package updates through installed plugins. |
| `porringer add` | Install a package and record it in a manifest (your global one by default). |
| `porringer upgrade` | Upgrade a manifest entry, optionally moving its constraint up. |
| `porringer remove` | Uninstall a package and drop its manifest entry. |
| `porringer plugin list` | List available installer, project, runtime, and SCM plugins. |
| `porringer env info` | Print local environment and plugin diagnostics. |
| `porringer schema` | Export the manifest JSON Schema. |
| `porringer self check` | Check PyPI for a newer Porringer release. |

## Documentation

- [Setup](setup.md): install Porringer and run the first preview.
- [Install links](links.md): understand `porringer://` links and their safety boundary.
- [Install command](install.md): run environment sync from manifests, profiles, and links.
- [Preview command](preview.md): inspect planned actions, diagnostics, and JSON reports.
- [Check command](check.md): query plugins for package updates.
- [Managing entries](manage.md): add, upgrade and remove packages together with their manifest entries.
- [Download API](download.md): download files with hash and size verification from the Python API.
- [API surface](api.md): use the public Python imports, schemas, and plugin contracts.
- [Scope and non-goals](scope.md): understand what Porringer does and deliberately leaves to other tools.
- [Development](development.md): run tests, diagnostics, traces, and performance probes.

Example manifests live in the repository's `examples/` directory.

## Related work

[Microsoft Windows Developer Config](https://github.com/microsoft/WindowsDeveloperConfig)
shares Porringer's declarative setup model. Both tools can install developer packages, including packages reached through `winget` on Windows.

They work at different layers. Windows Developer Config is Windows-only and can configure OS state through DSC v3, including registry settings, themes, fonts, terminal profiles, and WSL provisioning. Porringer stays out of OS provisioning. It focuses on cross-platform package, runtime, repository, and project orchestration through plugins.

That makes them complementary. Windows Developer Config can prepare a Windows machine. Porringer can then synchronize the developer toolchains and projects that live on that machine.
