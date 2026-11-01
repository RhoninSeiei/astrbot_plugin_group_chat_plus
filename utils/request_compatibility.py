"""Request-local compatibility; no global handler wrapping or shared state."""
import copy
import json

BASELINE = '_gcp_request_baseline'
STATIC_INSTRUCTIONS = '_gcp_static_instructions'


def enabled_for(event, config, key='compatibility_groups'):
    groups = (config or {}).get(key, [])
    return isinstance(groups, list) and str(getattr(event, 'unified_msg_origin', '') or '') in groups


def capture_baseline(event, req, plugin_contexts, plugin_images):
    # Run before other hooks, after the core has loaded conversation history.
    # Keep independent copies so in-place mutations cannot rewrite the baseline.
    event.set_extra(BASELINE, copy.deepcopy({
        'prompt': getattr(req, 'prompt', '') or '',
        'contexts': getattr(req, 'contexts', None) or [],
        'images': getattr(req, 'image_urls', None) or [],
        'plugin_contexts': plugin_contexts or [],
        'plugin_images': plugin_images or [],
    }))


def _fingerprint(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def merge_request(event, req, plugin_contexts, plugin_prompt, plugin_images):
    baseline = event.get_extra(BASELINE, None)
    if not isinstance(baseline, dict):
        return plugin_contexts, plugin_prompt, plugin_images
    contexts = copy.deepcopy(baseline.get('plugin_contexts', plugin_contexts) or [])
    known = {_fingerprint(v) for v in contexts}
    original = {_fingerprint(v) for v in baseline.get('contexts', [])}
    for value in getattr(req, 'contexts', None) or []:
        if not isinstance(value, dict) or value.get('role') not in {'user', 'assistant', 'system', 'tool'}:
            continue
        key = _fingerprint(value)
        if key not in original and key not in known:
            contexts.append(copy.deepcopy(value))
            known.add(key)
    prompt = plugin_prompt
    before, current = baseline.get('prompt', ''), getattr(req, 'prompt', '') or ''
    # Preserve additions around the unchanged original request only. An arbitrary
    # replacement cannot be reliably attributed to a third-party plugin.
    if before and before in current:
        prefix, suffix = current.split(before, 1)
        if prefix.strip():
            prompt = '[其他插件补充内容]\n' + prefix + '\n[/其他插件补充内容]\n' + prompt
        if suffix.strip():
            prompt += '\n[其他插件补充内容]\n' + suffix + '\n[/其他插件补充内容]'
    images = list(baseline.get('plugin_images', plugin_images) or [])
    for value in getattr(req, 'image_urls', None) or []:
        if value not in baseline.get('images', []) and value not in images:
            images.append(value)
    return contexts, prompt, images


def split_static_instructions(prompt, static):
    if static and prompt.startswith(static):
        return prompt[len(static):].lstrip(), static
    return prompt, ''


def native_memory_available(context, event, config, injector, mode, version):
    """Only regular, opted-in requests delegate recall to the native hook.

    Missing/initializing/disabled plugins retain the previous manual fallback.
    Proactive requests do not pass through this helper.
    """
    if not enabled_for(event, config, 'native_memory_groups') or not enabled_for(event, config):
        return False
    try:
        plugins = getattr(event, 'plugins_name', None)
        if plugins is not None and 'astrbot_plugin_livingmemory' not in plugins:
            return False
        resolved, version = injector.resolve_mode(context, mode, version)
        if resolved != 'livingmemory':
            return False
        plugin, ready, engine = injector._get_livingmemory_plugin_state(context, version)
        return bool(ready and engine is not None and getattr(plugin, 'event_handler', None) is not None)
    except Exception:
        return False
