# Code X-Ray

A small, read-only Python CLI for getting oriented in unfamiliar code and RPM packages. No AI, API key, Python dependencies, server, or installation required. MIT licensed.

## Start here

Requires Python **3.11+**. Use inside your Fedora toolbox or Distrobox as you would any other Python script.

```bash
python3 code_xray.py scan /path/to/facet-data-agent
python3 code_xray.py scan /path/to/facet-data-agent --section deps
python3 code_xray.py scan /path/to/facet-data-agent --section signals
python3 code_xray.py scan /path/to/facet-data-agent --json -o facet-report.json
```

Run from the extracted `code-xray` directory. Targets may be directories or individual files; paths with spaces need quotes. Existing report files are never overwritten. JSON contains the complete bounded report; `--section` and `--limit` affect the terminal tables only.

## GitHub / GitLab

With `git` installed, use the repository's HTTPS clone URL, not a file, tree, or branch page URL:

```bash
python3 code_xray.py scan https://github.com/celery/vine.git
python3 code_xray.py scan https://gitlab.com/group/project.git --json
```

The tool downloads a shallow, default-branch clone into a temporary directory, records its commit, materializes ordinary files using `git archive`, inspects them, then removes its temporary copy. There is no checkout, dependency installation, build, or execution of repository code. Git must have network access. Private repositories can use Git's existing credential helper; prompts are disabled and tokens embedded in URLs are rejected. Self-hosted HTTPS GitLab works the same way. For another branch, submodules, LFS content, or offline use, obtain your own local checkout and scan its path.

Remote limits bound inspected materialized content, not Git's network download or object database. An unusually large repository can still have a large clone. Links, oversized files, lockfiles, generated directories, and submodules are omitted from the remote inventory; warnings distinguish that from a full inventory.

## RPM packages

Requires the system `rpm` command. Query an RPM you downloaded, or query an installed package inside the current container/host:

```bash
python3 code_xray.py rpm ./some-package.rpm
python3 code_xray.py rpm python3 --installed
python3 code_xray.py rpm ./some-package.rpm --json -o package-report.json
```

Reports name/version/architecture/summary, requirements, provided capabilities, payload paths, and scriptlet **text**. It never installs the RPM or executes its scriptlets. It does not extract RPM payloads, download source RPMs, or determine whether a package is trustworthy. Inside a toolbox, `--installed` queries that toolbox's RPM database, not the Silverblue host's database. To inspect source contents, obtain/extract the source separately and run `scan` on that directory.

## Reading the output

| Section | Meaning | Evidence strength |
| --- | --- | --- |
| `files` | Inventory, language by extension, likely role | Role is a filename/path heuristic |
| `entries` | Python main guards and declared package entry points | Python AST or manifest declaration |
| `commands` | npm scripts | Declared text, never executed |
| `deps` | npm, PEP 621, Poetry, requirements, Cargo and Go dependencies | Manifest declarations, not installed versions |
| `imports` | Python imports and JS/TS import/require clues | AST for Python; text matches for JS/TS |
| `symbols` | Python functions, async functions, classes | Python AST |
| `calls` | Python call expressions | Syntactic targets; no claim they resolve or execute |
| `signals` | OpenAI, Ollama and selected HTTP request clues | Text matches with source locations; can match comments/docs |

Example: an `OpenAI reference` at `app/lib/agents.ts:75` means inspect that location. It does **not** establish that OpenAI powers every feature. A directory named `services` suggests processing; reading its functions establishes what it actually does.

Use the evidence to inspect a short range:

```bash
sed -n '60,100p' app/lib/agents.ts
```

Manifest line numbers are the first text match for a key and can be approximate when keys repeat. Python AST line numbers are exact for the scanned source. Relative file references resolve from the scanned root; a single-file scan uses its basename.

## Keep output focused

```bash
python3 code_xray.py scan . --section entries
python3 code_xray.py scan . --section imports --limit 40
python3 code_xray.py scan . --exclude data --exclude notebooks
python3 code_xray.py scan . --max-files 10000 --max-bytes 524288
```

Default limits: 5,000 inventoried files, 256 KiB per file, and 32 MiB of scanned content. Larger and binary files can appear in local inventories without content inspection. Read warnings and `summary.skipped_content`. Common generated folders, lockfiles, environment files, several common credential filenames and symlinks are excluded. `.gitignore` is not interpreted. **This is not a secret scrubber**: scripts, manifests, unusual credential files, dependencies and RPM scriptlets can contain private information. Review reports before sharing them. No report is uploaded by this tool.

No full execution tracing, dependency vulnerability lookup, dependency resolution, AST analysis for non-Python languages, complete route inference, or cross-file call graph is claimed. The scanner does not import scanned modules. It offers a compact, reproducible orientation map rather than an AI-written explanation.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary fixtures and mock RPM queries; the script also works as an importable module (`scan(path)` returns a dictionary). JSON `schema_version` is `1`.
