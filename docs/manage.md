# Managing Entries

`add`, `remove` and `upgrade` change what is installed and edit a manifest in one step. They edit your global manifest unless `--manifest` names a `porringer.json` (or its folder). Manifests embedded in other files, such as `[tool.porringer]` in `pyproject.toml`, are never edited.

```shell
porringer add pdm --kind tool --ecosystem python
porringer upgrade pdm --kind tool --ecosystem python --range replace
porringer remove pdm --kind tool --ecosystem python
```

## Commands

| Command | What happens |
| --- | --- |
| `porringer add SPEC` | Installs the package, then records the entry. Nothing is recorded if the install fails. Adding an entry that is already there updates it in place, for example to change its constraint. `--no-install` only edits the manifest, which also covers skipping a version: `porringer add "ruff!=0.15.3" -k tool -e python --no-install`. |
| `porringer remove NAME` | Uninstalls the package, then drops the entry. The entry stays if the uninstall fails. `--keep-installed` stops managing the package without uninstalling it. For runtimes, see below. |
| `porringer upgrade NAME` | `--range in_range` (default) upgrades within the entry's constraint and leaves the manifest alone. `--range replace` upgrades past the constraint, then moves the constraint up to the installed version. |

An entry is identified by its manifest, kind, ecosystem, name, `installer` and `runtime`, so pass the same `--installer` and `--runtime` the entry was added with.

## Removing a Runtime

`porringer remove 3.14 --kind runtime --ecosystem python` first checks what depends on that runtime:

- entries in the manifest and in your global manifest whose `runtime` is `3.14`;
- installed tools built on its interpreter, such as pipx venvs.

While any remain, the removal is refused and they are listed. `--cascade` removes them first (dependent entries are uninstalled and dropped), then the runtime. Porringer also warns when the runtime is the default interpreter and lists the packages inside it, which go with it. Project environments (for example a PDM `.venv` built on that interpreter) belong to their tools and are not checked; recreate them with that tool.

A runtime installed by something else (for example python.org's installer when the entry uses `pim`) is skipped as `NOT_MANAGED` rather than reported as missing.

## Options

| Option | Short | Description |
| --- | --- | --- |
| `--kind` | `-k` | Manifest section: `package`, `tool` or `runtime`. Required. |
| `--ecosystem` | `-e` | Ecosystem, such as `python` or `system`. Required. |
| `--installer` | | Installer plugin for the entry. |
| `--runtime` | | Runtime tag the installer runs under. |
| `--manifest` | `-m` | `porringer.json` or its folder. Defaults to your global manifest. |

## Manifest Edits

Edits keep key order, `$schema` and indentation, are checked against the manifest schema, and replace the file atomically. Only one combined verb runs at a time per `API` instance, and it holds that lock across both the install and the write.

## API Usage

```python
from porringer import API
from porringer.core.schema import PluginKind
from porringer.schema import RangeStrategy

api = API()
report = await api.tool.add('pdm>=2.20', kind=PluginKind.TOOL, ecosystem='python')
pdm = report.results[0].entry_id
await api.tool.upgrade({pdm}, range_strategy=RangeStrategy.REPLACE)
await api.tool.remove({pdm}, keep_installed=True)
```

Results carry `entry_id`; store it to refer to the entry later. `manifest=` selects another `porringer.json`.
