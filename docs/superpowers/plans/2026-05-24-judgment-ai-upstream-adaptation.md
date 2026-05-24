# Judgment AI Upstream Adaptation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate upstream judgment-AI, error-handling, prompt-rewrite, smart-concurrency, and web-security improvements without replacing the custom RhoninSeiei fork behavior.

**Architecture:** Keep the current fork as the source of truth. Import small upstream helpers where they have clear boundaries, then adapt call sites to preserve session-level persona/provider selection, final reply gate isolation, frequency reduction settings, restart guard, and existing tests.

**Tech Stack:** Python 3, AstrBot plugin APIs, `unittest`, local source-level regression tests.

---

### Task 1: Error Formatting And Zero-Side-Effect Judgment Failures

**Files:**
- Create: `utils/ai_error_formatter.py`
- Modify: `utils/decision_ai.py`
- Modify: `utils/reply_handler.py`
- Modify: `utils/proactive_chat_manager.py`
- Test: `tests/test_ai_error_handling.py`

- [ ] Write tests for HTTP, HTML, empty-output, and network exception formatting.
- [ ] Write source-level tests proving proactive AI judge failures do not update `last_bot_reply_time`.
- [ ] Import the formatter from upstream and adapt wording only where needed.
- [ ] Use formatted errors in decision AI, final gate, reply generation, and proactive judgment calls.
- [ ] Run `python3 -m unittest tests/test_ai_error_handling.py`.

### Task 2: Judgment Persona And Reasoning Integration

**Files:**
- Modify: `_conf_schema.json`
- Modify: `main.py`
- Modify: `utils/decision_ai.py`
- Modify: `utils/proactive_chat_manager.py`
- Test: `tests/test_decision_ai_config.py`

- [ ] Add config keys for judgment persona selection and optional reasoning.
- [ ] Add helper methods for judgment persona resolution and reasoning protocol parsing.
- [ ] Keep first-stage read-air judgment low-cost by default.
- [ ] Keep second-stage final gate short and isolated from reply generation.
- [ ] Pass config from `main.py` into `DecisionAI.should_reply`.
- [ ] Pass matching config into proactive AI judge.
- [ ] Run `python3 -m unittest tests/test_decision_ai_config.py tests/test_session_preferences.py tests/test_reply_leakage_guard.py`.

### Task 3: SystemPromptRewriter

**Files:**
- Create: `utils/system_prompt_rewriter.py`
- Modify: `main.py`
- Test: `tests/test_system_prompt_rewriter.py`

- [ ] Import the upstream conservative `SystemPromptRewriter`.
- [ ] Add tests for exact persona match, wrapped persona match, LTM stripping, and conservative fallback.
- [ ] Replace the hand-written `on_llm_request` system prompt merge with the helper.
- [ ] Preserve current fork behavior that restores the plugin tool set instead of merging framework tools.
- [ ] Run `python3 -m unittest tests/test_system_prompt_rewriter.py`.

### Task 4: Smart Concurrent

**Files:**
- Create: `utils/smart_concurrent_manager.py`
- Modify: `utils/__init__.py`
- Modify: `_conf_schema.json`
- Modify: `main.py`
- Test: `tests/test_smart_concurrent_manager.py`

- [ ] Import and test the manager as an isolated coordinator.
- [ ] Add config keys for `concurrent_mode=legacy|smart` and smart batch timing.
- [ ] Wire smart mode around the existing processing-session guard without removing legacy mode.
- [ ] Add a smart batch hint that can be appended to the reply prompt without entering persisted history.
- [ ] Run `python3 -m unittest tests/test_smart_concurrent_manager.py`.

### Task 5: Web Security Settings

**Files:**
- Modify: `_conf_schema.json`
- Modify: `web/security.py`
- Modify: `web/server.py`
- Test: `tests/test_web_security.py`

- [ ] Add configurable authenticated rate limits and brute-force windows.
- [ ] Apply authenticated request rate checks after token verification.
- [ ] Keep security-sensitive config keys managed from AstrBot config rather than browser edits.
- [ ] Run `python3 -m unittest tests/test_web_security.py`.

### Final Verification

- [ ] Run `python3 -m unittest tests/test_ai_error_handling.py tests/test_decision_ai_config.py tests/test_system_prompt_rewriter.py tests/test_smart_concurrent_manager.py tests/test_web_security.py tests/test_session_preferences.py tests/test_reply_leakage_guard.py tests/test_restart_guard.py tests/test_multimodal_history_content.py`.
- [ ] Run `python3 -m py_compile main.py utils/*.py private_chat/private_chat_utils/*.py web/*.py`.
- [ ] Review `git diff --stat`.
- [ ] Commit and push to `origin main`.
