"""Bounded Reply.chain traversal shared by image and history paths.

Component classes are supplied by callers so this utility remains independent
of AstrBot import aliases. No platform fetch or file conversion occurs here.
"""

MAX_REPLY_NESTING_DEPTH = 3


def get_reply_chain(reply):
    chain = getattr(reply, "chain", None)
    return chain if isinstance(chain, (list, tuple)) else ()


def iter_reply_chain(chain, reply_type, max_depth=MAX_REPLY_NESTING_DEPTH):
    """Yield components in display order, bounded by depth and ancestor cycles."""
    def walk(parts, depth, ancestors):
        if not isinstance(parts, (list, tuple)):
            return
        for component in parts:
            yield component
            if (isinstance(component, reply_type) and depth < max_depth
                    and id(component) not in ancestors):
                yield from walk(get_reply_chain(component), depth + 1,
                                ancestors | {id(component)})

    yield from walk(chain, 0, set())


def analyze_chain(chain, *, image_type, plain_type, reply_type,
                  max_depth=MAX_REPLY_NESTING_DEPTH):
    images = []
    has_text = False
    for component in iter_reply_chain(chain, reply_type, max_depth):
        if isinstance(component, image_type):
            images.append(component)
        elif isinstance(component, plain_type):
            has_text = has_text or bool(str(getattr(component, "text", "") or "").strip())
        elif isinstance(component, reply_type):
            # A reference is meaningful content, including quote + mention.
            has_text = True
    return bool(images), has_text, images


def _legacy_text(reply):
    for name in ("message_str", "message"):
        value = getattr(reply, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_reply_content(reply, *, image_type, plain_type, reply_type,
                          at_type=None, max_depth=MAX_REPLY_NESTING_DEPTH,
                          image_placeholder="[图片]"):
    """Render structured quote content; retain legacy text when chain is absent."""
    def render(parts, depth, ancestors):
        buffer = []
        for component in parts:
            if isinstance(component, plain_type):
                buffer.append(str(getattr(component, "text", "") or ""))
            elif isinstance(component, image_type):
                buffer.append(image_placeholder)
            elif at_type is not None and isinstance(component, at_type):
                buffer.append(f"[At:{getattr(component, 'qq', '')}]")
            elif isinstance(component, reply_type):
                nested = ""
                if depth < max_depth and id(component) not in ancestors:
                    nested = render(get_reply_chain(component), depth + 1,
                                    ancestors | {id(component)})
                nested = nested or _legacy_text(component)
                buffer.append(f"[引用消息: {nested}]" if nested else "[引用消息]")
            else:
                # Preserve known non-image component placeholders without
                # serializing arbitrary objects or exposing their file paths.
                kind = type(component).__name__
                if kind == "Face":
                    buffer.append(f"[表情:{getattr(component, 'id', '')}]")
                elif kind in {"Video", "Record", "File", "AtAll"}:
                    buffer.append({"Video": "[视频]", "Record": "[语音]",
                                   "File": "[文件]", "AtAll": "[At:all]"}[kind])
        return "".join(buffer).strip()

    chain = get_reply_chain(reply)
    if chain:
        content = render(chain, 1, {id(reply)})
        if content:
            return content
    return _legacy_text(reply)
