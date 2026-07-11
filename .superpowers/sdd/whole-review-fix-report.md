# Group Image Backends Whole-Branch Review Fix Report

Base: `f016665`

Implementation commit: `1ca1921` (`fix: address group image backend review`)

## Status

The whole-branch review items are addressed within the approved file scope. The implementation keeps the existing tool names, group-only authorization, dynamic backend progress, one-image delivery, model-facing safe summaries, natural-language final reply instructions, and persistent-history sanitization behavior.

No production synchronization or plugin reload was performed in this review-fix task.

## RED evidence

### Config migration and Codex Provider isolation

Command:

```text
python3 -m unittest tests.test_codex_oauth_image_service tests.test_step_image_tool_integration -v
```

Observed before implementation: `Ran 29 tests`, `FAILED (failures=6, errors=11)`.

Expected failures covered the missing `image_tool_backend_config_version`, missing migration helper and runtime backend selection, use of `get_provider_by_id`, shared Provider timeout mutation and locking, missing `asyncio.wait_for`, and unsafe lookup behavior.

### Async tool execution and safe logging

Command:

```text
python3 -m unittest tests.test_step_image_tool_integration -v
```

Observed before implementation: `Ran 24 tests`, `FAILED (failures=8)`.

Expected failures covered raw exception text and `exc_info=True` in image-tool logs plus duplicate image sends when the event marker was already set. The execution tests ran undecorated copies of both async tool generators with minimal events, message chains, and facade services.

### Migration documentation

Command:

```text
python3 -m unittest tests.test_group_only_boundary.GroupOnlyBoundaryTest.test_group_image_backend_documentation_contract -v
```

Observed before documentation changes: `Ran 1 test`, `FAILED (failures=1)` because the internal migration marker was undocumented.

## GREEN evidence

### Config migration and Codex Provider isolation

The 29-test RED command passed after implementation. Tests cover new installs, upgraded schema-completed configs, post-migration explicit Codex selection, save failure runtime behavior, public Provider enumeration through `get_all_providers()` and `meta().id`, unchanged Provider timeout state, concurrent calls, timeout cancellation, fixed sanitized exceptions, and result parsing failures.

### Async tool execution and safe logging

The 24-test RED command passed after implementation. A later guard and missing-edit-image execution test increased this module to 25 tests. Coverage includes progress and image ordering, exact send counts, event extras, facade arguments, success summaries, user/config/provider/unexpected failures for generate and edit, pre-send guard failures, and duplicate-send markers.

### Migration documentation

The documentation contract passed after README, configuration reference, message workflow, project structure, schema hint, and changelog updates.

## Final verification

```text
Focused image, history, policy, leakage, and tool tests: 101 passed
Full unittest discovery: 173 passed
_conf_schema.json parsing: exit 0
Python in-memory compilation: 6 files compiled
git diff --check: exit 0
```

The focused set included Codex OAuth, the group-image facade, both real async tool generators, group-only documentation boundaries, StepFun regression tests, multimodal history sanitization, ToolPolicy, tool-call leakage guards, and tool passthrough.

The schema parse and all unittest runs used WSL Python 3.12 with `PYTHONDONTWRITEBYTECODE=1`. After schema parsing, repeated new WSL instance starts returned `Wsl/Service/CreateInstance/E_ACCESSDENIED`; the six-file source compilation therefore used host Python 3.13 `compile()` without imports or bytecode output. Full WSL Python 3.12 discovery had already compiled and executed every test module and the touched runtime modules used by those tests.

## Scope checks

Only approved Group Image Backends files were committed. The existing `.tmp` contents and untracked `docs/superpowers/plans/2026-04-17-matoi-guardian-ep5-plugin.md` were left unchanged and uncommitted.

Previously approved history sanitization and Provider capability documentation remain present. The Codex adapter contains no `get_provider_by_id`, Provider timeout access, timeout lock, or Provider-manager internal mapping access.
