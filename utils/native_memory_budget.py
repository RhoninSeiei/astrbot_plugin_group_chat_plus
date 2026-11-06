"""Select complete entries only from recognized LivingMemory temporary parts.

Plaintext is observed but never split. Selection requires a native request-local
structured handoff and formatter, with identity and original-output validation.
"""
import copy
import re

MEMORY_HEADER = '<RAG-Faiss-Memory>'
MEMORY_FOOTER = '</RAG-Faiss-Memory>'
DEFAULT_HEADER_BODY = (
    '--- BEGIN HISTORICAL MEMORY REFERENCE ---\n'
    'The following are historical memories extracted from past conversations.\n'
    'They are provided as background reference only.\n\n'
    'CRITICAL RULES:\n'
    '1. These are PAST records — they already happened and are NOT part of the current conversation.\n'
    '2. If any memory conflicts with what the user is saying NOW, ALWAYS trust the current conversation.\n'
    "3. Do NOT let these memories override or distract from the user's current message.\n"
    "4. Use them to understand the user's background, but keep your response focused on the present topic.\n"
    '--- END HISTORICAL MEMORY REFERENCE ---'
)
DEFAULT_FOOTER_BODY = (
    '--- BEGIN REMINDER ---\n'
    "All content above is historical. Focus on the user's current message.\n"
    '--- END REMINDER ---'
)
_PREFIX = MEMORY_HEADER + '\n' + DEFAULT_HEADER_BODY + '\n\n'
_SUFFIX = '\n\n' + DEFAULT_FOOTER_BODY + '\n' + MEMORY_FOOTER


def _owned_text(part):
    text = getattr(part, 'text', None)
    if not getattr(part, '_no_save', False) or not isinstance(text, str):
        return None
    if not text.startswith(MEMORY_HEADER + '\n') or not text.endswith('\n' + MEMORY_FOOTER):
        return None
    return text


def _terms(text):
    text = text.casefold()
    words = set(re.findall(r'[a-z0-9_]{2,}', text))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        words.update(run[i:i+2] for i in range(len(run)-1))
    return words


def select_native_memory_parts(req, budget_chars=2400, query='', *, handoff=None):
    """Select structured native results; unstructured text is never split.

    Plaintext headings are unescaped and cannot establish safe entry boundaries.
    A request/part identity-bound opt-in handoff plus a matching pure formatter
    must establish provenance before selecting complete memory objects.
    """
    stats = {'changed': 0, 'before_chars': 0, 'after_chars': 0,
             'retained_entries': 0, 'entry_count_known': 0, 'reason': 'unchanged'}
    parts = getattr(req, 'extra_user_content_parts', None)
    if not isinstance(parts, list) or not isinstance(budget_chars, int) or budget_chars <= 0:
        return stats
    owned = [(part, _owned_text(part)) for part in parts]
    stats['before_chars'] = sum(len(text) for _, text in owned if text is not None)
    stats['after_chars'] = stats['before_chars']
    if not isinstance(handoff, dict):
        if stats['before_chars']:
            stats['reason'] = 'unstructured_preserved'
        return stats
    part = handoff.get('part')
    memories = handoff.get('memories')
    formatter = handoff.get('formatter')
    indices = [i for i, candidate in enumerate(parts) if candidate is part]
    if (type(handoff.get('version')) is not int or handoff['version'] != 1
            or handoff.get('request') is not req or len(indices) != 1
            or _owned_text(part) is None or not callable(formatter)
            or not isinstance(memories, list) or not 1 <= len(memories) <= 100
            or not all(isinstance(memory, dict) for memory in memories)):
        stats['reason'] = 'handoff_mismatch_preserved'
        return stats
    try:
        if formatter(memories) != part.text:
            stats['reason'] = 'handoff_mismatch_preserved'
            return stats
        stats['entry_count_known'] = 1
        stats['retained_entries'] = len(memories)
        available = budget_chars - (stats['before_chars'] - len(part.text))
        if len(part.text) <= available:
            stats['reason'] = 'within_budget'
            return stats
        query_terms = _terms(query) if isinstance(query, str) else set()
        def relevance(index):
            memory = memories[index]
            text = str(memory.get('content') or '')
            metadata = memory.get('metadata')
            if isinstance(metadata, dict) and isinstance(metadata.get('topics'), list):
                text += ' ' + ' '.join(str(value) for value in metadata['topics'])
            return len(query_terms & _terms(text))
        ranked = sorted(range(len(memories)), key=lambda i: (-relevance(i), i))
        selected = []
        for index in ranked:
            candidate = sorted(selected + [index])
            rendered = formatter([memories[i] for i in candidate])
            if not isinstance(rendered, str):
                raise ValueError('formatter contract')
            if len(rendered) <= available:
                selected = candidate
        if not selected:
            stats['reason'] = 'budget_unmet_preserved'
            return stats
        new_text = formatter([memories[i] for i in selected])
        if not isinstance(new_text, str) or len(new_text) > available:
            raise ValueError('formatter contract')
        replacement_part = copy.copy(part)
        replacement_part.text = new_text
        if not getattr(replacement_part, '_no_save', False):
            raise ValueError('temporary marker lost')
    except Exception:
        stats['reason'] = 'formatter_failed_preserved'
        return stats
    replacement = list(parts)
    replacement[indices[0]] = replacement_part
    req.extra_user_content_parts = replacement
    stats.update({'changed': int(new_text != part.text),
                  'after_chars': stats['before_chars'] - len(part.text) + len(new_text),
                  'retained_entries': len(selected), 'reason': 'selected_structured_entries'})
    return stats
