# AGENTS.md

## Scope

- This repository contains a plugin at the repo root and a full upstream-style AstrBot source tree in `AstrBot_src/`.
- Treat `AstrBot_src/` as the authoritative reference for runtime conventions, test commands, linting, and dashboard workflows.
- Root-level plugin files include `main.py`, `config.json`, `metadata.yaml`, `_conf_schema.json`, and docs in Chinese.
- There is an existing upstream agent guide at `AstrBot_src/AGENTS.md`; keep this root guide aligned with it when editing both areas.
- No Cursor rules were found in `.cursor/rules/` or `.cursorrules`.
- No GitHub Copilot instructions were found in `.github/copilot-instructions.md`.

## Repository Layout

- Root: plugin package `astrbot_plugin_DynamicPersona` implemented in `main.py`.
- `config.json`: sample plugin config and schema-driven defaults.
- `metadata.yaml`: plugin manifest used by AstrBot.
- `AstrBot_src/`: main Python application, tests, CLI, dashboard, scripts, and Makefile.
- `AstrBot_src/dashboard/`: Vue 3 + Vite + TypeScript dashboard.
- `AstrBot_src/tests/`: pytest suite with unit, integration, and feature tests.

## Environment Expectations

- Prefer `uv` for Python environment management inside `AstrBot_src/`.
- Install dev dependencies with `uv sync --group dev`.
- `AstrBot_src/pyproject.toml` declares `requires-python = ">=3.12"`; use Python 3.12 unless you have a strong reason not to.
- Python code is linted/formatted with Ruff and upgraded with `pyupgrade` via pre-commit.
- Frontend work uses `pnpm` in `AstrBot_src/dashboard/`.
- The app commonly expects local directories such as `data/plugins`, `data/config`, `data/temp`, and `data/skills`.

## Core Commands

- Sync Python deps: `uv sync --group dev`
- Run AstrBot from source tree: `uv run main.py`
- Run AstrBot CLI: `uv run astrbot run`
- Format Python: `uv run ruff format .`
- Lint Python: `uv run ruff check .`
- Check formatting only: `uv run ruff format --check .`
- Run all tests: `uv run pytest -q`
- Run verbose full test flow: `uv run pytest --cov=. -v -o log_cli=true -o log_level=DEBUG`
- Run recommended local PR check: `make pr-test-neo`
- Run full local PR check: `make pr-test-full`
- Run faster repeated full check: `make pr-test-full-fast`

## Single-Test Commands

- Run one test file: `uv run pytest tests/test_dashboard.py -q`
- Run one test function: `uv run pytest tests/test_dashboard.py::test_neo_skills_routes -q`
- Run one unit test file: `uv run pytest tests/unit/test_astr_agent_tool_exec.py -q`
- Run one parametrized or async test by node id: `uv run pytest 'tests/unit/test_astr_agent_tool_exec.py::test_collect_handoff_image_urls_filters_supported_schemes_and_extensions' -q`
- Run tests by keyword: `uv run pytest -k handoff -q`
- Run only blocking subset: `uv run pytest --test-profile blocking -q`
- Add extra args to scripted PR env runs through `PYTEST_ARGS`, for example: `PYTEST_ARGS='tests/test_dashboard.py::test_neo_skills_routes -q' ./scripts/pr_test_env.sh --profile neo --skip-smoke`

## Dashboard Commands

- Install dashboard deps: `pnpm --dir dashboard install`
- Start dashboard dev server: `pnpm --dir dashboard dev`
- Build dashboard: `pnpm --dir dashboard build`
- Preview dashboard build: `pnpm --dir dashboard preview`
- Type-check dashboard: `pnpm --dir dashboard typecheck`
- Lint dashboard: `pnpm --dir dashboard lint`

## Validation Workflow

- For Python-only changes, usually run `uv run ruff format .`, `uv run ruff check .`, and the smallest relevant pytest target.
- For dashboard-only changes, usually run `pnpm --dir dashboard typecheck` and `pnpm --dir dashboard build`.
- For cross-cutting changes, prefer `make pr-test-neo` first, then `make pr-test-full` if the change touches core execution paths.
- `scripts/pr_test_env.sh` also performs a smoke test against `http://localhost:6185` unless skipped.
- Full profile builds the dashboard automatically unless `--no-dashboard` is passed.

## Python Style

- Follow Ruff defaults configured in `AstrBot_src/pyproject.toml`.
- Line length target is 88.
- Import order matters; Ruff uses `I` rules, so keep imports sorted and grouped.
- Prefer module-level imports at the top of the file unless a local import is needed to avoid heavy startup cost or circular imports.
- Use double quotes in Python where formatting tools prefer them.
- Keep functions and methods small and direct; the codebase favors pragmatic, readable control flow over abstraction-heavy patterns.
- Prefer explicit early returns for guard conditions.
- Keep comments sparse; add them only when behavior is non-obvious.
- Use English for all new comments, even if nearby code or docs are in Chinese.

## Python Types

- Use modern built-in generics such as `list[str]`, `dict[str, Any]`, and `str | None`.
- Match the existing codebase style: type annotate public functions, important locals, fixtures, and return values when it improves clarity.
- Pyright runs in `basic` mode, so favor useful annotations without overengineering types.
- Use `Path` objects for filesystem work where practical.
- Follow the existing project guidance: prefer `pathlib.Path` over stringly-typed paths for new code.
- In AstrBot code, prefer existing path helpers from `astrbot.core.utils` when choosing data, temp, config, or plugin directories.

## Python Naming

- Classes use `PascalCase`.
- Functions, methods, variables, fixtures, and modules generally use `snake_case`.
- Constants use `UPPER_SNAKE_CASE`.
- Private helpers use a leading underscore.
- Test names should describe behavior, typically `test_<action>_<expected_result>`.
- Keep naming domain-specific and literal; avoid vague names like `data2`, `handler_new`, or `tmpThing`.

## Error Handling And Logging

- Prefer narrow exception handling where failure is expected and recoverable.
- Log operational failures with `logger.warning(...)` when execution can continue.
- Use `logger.error(...)` for failures that break the current action.
- Avoid silent `except` blocks.
- Raise `ValueError` for invalid caller input when that matches surrounding patterns.
- When surfacing error text that may contain secrets, prefer existing redaction helpers such as `astrbot.core.utils.error_redaction.safe_error`.
- Preserve current user-facing language conventions; many runtime messages are Chinese, but new code comments should still be English.

## Async And Concurrency

- Much of the backend is async; preserve async boundaries instead of wrapping async code in sync helpers.
- In tests, use `@pytest.mark.asyncio` for async test functions.
- Await I/O and service calls directly; do not hide awaits in opaque helper chains unless reuse clearly helps.
- Be careful with startup, shutdown, and smoke-test behavior around the dashboard and core lifecycle.

## Testing Conventions

- Tests are written with `pytest` and `pytest-asyncio`.
- Shared fixtures live in `AstrBot_src/tests/conftest.py` and `AstrBot_src/tests/fixtures/`.
- The suite auto-classifies tests into unit vs integration and supports `--test-profile blocking`.
- Prefer adding or updating the smallest targeted test that covers the changed behavior.
- Keep fixtures explicit and readable; this codebase uses `MagicMock`, `AsyncMock`, and lightweight dummy classes heavily.
- When editing plugin behavior at the repo root, add tests in `AstrBot_src/tests/` only if the plugin is wired into that source tree; otherwise document manual verification clearly.

## Frontend Style

- The dashboard uses Vue 3, Vite, Pinia, Axios, and TypeScript, with some legacy `.js` files still present.
- Follow existing semicolon-heavy TS style in the dashboard.
- Prefer explicit types for exported interfaces, composables, store state, and function params.
- Keep API calls centralized and consistent with current Axios usage.
- Reuse shared components/composables instead of duplicating view logic.
- Do not introduce a brand new styling system when extending existing dashboard views.

## Frontend Naming And Structure

- Vue view and component files use `PascalCase.vue`.
- Stores use descriptive names such as `useAuthStore` and `useCustomizerStore`.
- Composables use `useXxx` naming.
- Shared exports may be collected in `index.ts` barrels when the surrounding folder already follows that pattern.
- Prefer small, feature-focused components over monolithic pages.

## Plugin-Specific Notes

- Root `main.py` is the dynamic persona plugin entrypoint registered via `@register(...)`.
- The plugin currently relies on AstrBot APIs such as `AstrMessageEvent`, `ProviderRequest`, `Context`, and `Star`.
- Preserve the existing hook separation: provider selection in `on_waiting_llm_request`, prompt injection in `on_llm_request`.
- Keep config-driven behavior aligned with `config.json` and `metadata.yaml`.
- If you add config fields, update schema/docs together rather than only one file.

## Change Hygiene

- Do not add summary/report files like `*_SUMMARY.md` unless explicitly requested.
- Keep commits and PR titles in conventional-commit style when asked to prepare them.
- Use English for PR titles and descriptions.
- Before finishing substantial Python changes, at minimum run formatter, linter, and the most relevant pytest target.
- Before finishing substantial dashboard changes, at minimum run type-check and build.
