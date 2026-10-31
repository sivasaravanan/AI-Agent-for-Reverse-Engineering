# Known issues

- Not verified on Windows (developed and tested on Linux with Python 3.12; Tk under Xvfb).
- Container isolation (Phase 4) is tested with a fake runtime. Run `python -m plugins.build_execution check` once, and confirm `--network none`.
- AI providers are tested with a fake transport; no real provider call was made. Model output quality is not evaluated.
- External analyzers (ruff, pyright, eslint, tsc, javac, dotnet, sqlfluff) run only if installed; dotnet build is never run on the host.
- Static findings from heuristics can be false positives; import/dependency checks are suffix-based and name-based.
- Secret scanning (reports, AI payloads, packages) is pattern-based and can miss unusual secrets.
- The notebook has many tabs; labels shrink on small screens.
- History stores results, not the ZIP; reopening a past result requires re-selecting the ZIP to propose fixes.
- Delivery package is not signed and is not an installer/EXE.
- Code map: only Python has a real call graph; other languages use regex name extraction (no calls). "Unreferenced" is a weak hint.
- HTML/CSS checks are lightweight heuristics, not a validator. PHP/Go syntax checks need `php` / `gofmt` installed. Go reports only the first syntax error per file.
- Folder/tar inputs: .7z and .rar are not supported; convert to ZIP. Generated documentation is a draft from static facts; review the TODO items.
- Education: scores cover only what static analysis can see; correctness that needs running code must be scored manually. Similarity is a lead for human review, never proof of misconduct; small or template-based assignments produce false positives.
- Licences: detected from licence texts and package metadata only; dependency licences are NOASSERTION unless a vendored package ships metadata. Not legal advice. The SBOM covers declared dependencies (transitive only if a lockfile lists them).
- Audit log: the hash chain detects edits, deletion and reordering of earlier entries, but not deletion of the whole file or tail truncation; store the head hash elsewhere for stronger assurance.
- Team mode is a shared-folder model: no server, no SSO, no network access control. Anyone who can write to the shared folder can bypass it. SQLite on network drives can be unreliable under heavy concurrent use.
- Signatures are HMAC (symmetric): anyone with the workspace signing key can sign. Not a replacement for public-key signing or a PKI.
- Not tested on Windows or against real LMS exports; UI tests run under Xvfb only.
- UI icons are symbol glyphs from the system fonts; on a system without a symbol font a few may show as empty boxes (labels still read correctly). Look verified under Xvfb on Linux only.
- Auto-start: needs the runtime to be installed already. Linux Docker start uses systemd (system unit needs root or passwordless sudo; otherwise you are told the exact command). Docker Desktop / a Podman machine can take 30-120 s. Not tested against real Docker Desktop / Podman here (fake runtime + unit tests only).
- Live Run shows the container's console, CPU/memory and process list. It is not a screen/web-page preview: sandbox containers have no network and publish no ports, so a web app started inside one cannot be opened in the embedded view.
- Live Run output is shown raw (secrets a program prints would be visible on screen); only the final stored result/report is redacted. The live stream stops at 4 MB per stream.

- Two test modules are obsolete and excluded by `tests/unit/conftest.py`: `test_execution_integration.py` and `test_language_analysis_integration.py` target an earlier pipeline API and abort
  the whole pytest run at import. Port or delete them, then delete that conftest.
- `tests/institution/test_portfolio.py::test_batch_survives_bad_archive_and_scores_projects` passes only when Ruff/Pyright are NOT installed: its "clean" fixture repeats function
  definitions that Ruff reports, so the risk score becomes 100. The product is correct; the fixture depends on the environment.
- Run targets are read from files only: a program that needs input, a display or packages missing from the image will not run to completion in the sandbox (the Risks column says so).
- Prerequisites: winget/Docker/Podman installs, UAC, `podman machine` and authenticating proxies are untested on Windows (see `docs/PREREQUISITES.md`).

- GUI preview: needs the local image `ase-preview-gui:latest` (button / script / `--build-gui-image`; building needs the internet). Verified end to end here against a real Docker daemon using a locally assembled image with the same contents (Xvfb + Tk + ImageMagick): the Tk app's window appeared as screen frames in the panel. Podman, nerdctl, Finch, Docker Desktop, Windows and macOS were not tested. No window manager inside the screen.
- Local AI auto-setup and the cloud catalogue were tested with fake HTTP transports only (this sandbox cannot reach the provider sites). Anthropic's and Gemini's model-list endpoints and their OpenAI-compatible chat endpoints are used as documented but not exercised live; if a provider changes its list format, that provider shows an error and keeps its saved list.
- Free/Paid per model is known only where the provider publishes prices in its list (OpenRouter).
- Storage & clean-up was verified against a real Docker daemon (listing, removing exited containers, keeping running ones, scratch folder with a space in its path). `builder prune` and `system df` exist in Docker and nerdctl; Podman reports an error for `builder prune`, which the window shows as "could not prune the build cache". Not tested with Podman, Finch or Windows paths.
