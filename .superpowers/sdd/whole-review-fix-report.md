# Group Image Backends Whole-Branch Review Fix Report

Base: `f016665`

Implementation commit: `1ca1921` (`fix: address group image backend review`)

Final follow-up base: `27d62ec`

Final follow-up commit: this commit (`fix: harden codex image provider integration`)

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

Previously approved history sanitization and Provider capability documentation remain present. The Codex adapter contains no `get_provider_by_id`, Provider `.timeout` state access, timeout lock, or Provider-manager internal mapping access.

## Final follow-up review fixes

### Provider enumeration isolation

`get_all_providers()` access and invocation retain the fixed safe `Codex OAuth 图片 Provider 查询失败。` mapping. Provider enumeration now isolates each member's `meta()` call: exceptions, `None`, and metadata without `id` are skipped while later members remain eligible. If every member is invalid or no ID matches, the adapter returns the fixed configuration error `Codex OAuth 图片 Provider 不存在。` without exposing the configured Provider ID through logs, exception text, cause, context, or traceback.

RED command:

```text
python3 -m unittest tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_provider_lookup_skips_broken_member_before_target tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_all_invalid_provider_members_return_fixed_safe_config_error -v
```

Observed before implementation: `Ran 2 tests`, `FAILED (errors=2)`. Both cases stopped at the first metadata exception and returned the Provider query error.

### Optional Provider timeout compatibility

The adapter validates `codex_oauth_image_timeout`, reads `generate_image()` with `inspect.signature`, and passes the validated value when the method declares a keyword-capable `timeout` parameter or `**kwargs`. Legacy signatures and unreadable signatures keep the original argument set. Signature detection happens before the single Provider call, so no failed request is retried. `asyncio.wait_for` remains the outer maximum wait with the same timeout value.

RED command:

```text
python3 -m unittest tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_explicit_timeout_parameter_receives_same_outer_timeout_once tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_kwargs_provider_receives_configured_timeout_once tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_legacy_provider_omits_timeout_and_is_called_once tests.test_codex_oauth_image_service.CodexOAuthImageServiceTest.test_signature_read_failure_uses_legacy_call_once -v
```

Observed before implementation: `Ran 4 tests`, `FAILED (failures=3, errors=1)`. Explicit and variadic signatures received no timeout, and neither legacy case performed signature inspection.

The production Codex OAuth Provider was checked at `0e3a237`; its `generate_image()` declaration includes `timeout: float | None = None`.

### Documentation RED evidence

The documentation contract initially failed for README, configuration reference, project structure, message workflow, and changelog. The updated documentation states the single-call timeout behavior, outer wait protection, legacy compatibility, possible Provider-owned HTTP timeout, and production Provider support.

### Final follow-up verification

```text
Codex OAuth adapter module: 18 passed
Focused Codex, GroupImage, tool integration, history, policy, leakage, and passthrough set: 108 passed
Full unittest discovery: 180 passed
_conf_schema.json parsing: exit 0
Python in-memory compilation: 2 changed Python files compiled
git diff --check: exit 0
```

WSL Python 3.12 ran all unittest commands with `PYTHONDONTWRITEBYTECODE=1`. Host Python compiled `utils/codex_oauth_image_service.py` and `tests/test_codex_oauth_image_service.py` in memory without imports or bytecode output.

### Final follow-up scope checks

Only the approved implementation, test, documentation, changelog, and report files were modified. The existing untracked `.tmp/` directory and `docs/superpowers/plans/2026-04-17-matoi-guardian-ep5-plugin.md` remain untouched and uncommitted. No production synchronization, plugin reload, or remote host action was performed.
