"""Opt-in, session-scoped subject definitions without an extra model call."""
import json
import re


_IMAGE_INTENT = re.compile(r"画|绘|生图|改图|换成|变成|改成|替换|生成|来(?:一)?张|给我(?:一)?张|做(?:一)?张|头像|立绘|表情包|meme|\bdraw\b|\bimage\b", re.I)


def subject_definitions(event, config, text):
    # The full UMO separates platforms and groups; never infer from history,
    # tool arguments, quoted messages, or a similarly numbered group.
    umo = str(getattr(event, 'unified_msg_origin', '') or '')
    if ':GroupMessage:' not in umo or not isinstance(text, str):
        return {}
    try:
        mapping = json.loads((config or {}).get('image_subject_contexts', '{}'))
    except (TypeError, ValueError):
        return {}
    if not isinstance(mapping, dict) or not isinstance(mapping.get(umo), dict):
        return {}
    matched = {}
    for alias, definition in mapping[umo].items():
        if not isinstance(alias, str) or not alias.strip() or not isinstance(definition, str) or not definition.strip():
            continue
        pattern = r'(?<![a-zA-Z0-9_])' + re.escape(alias.strip()) + r'(?![a-zA-Z0-9_])'
        if re.search(pattern, text, flags=re.I):
            matched[alias.strip()] = definition.strip()
    return matched


def append_subject_definitions(event, config, raw, prompt):
    definitions = subject_definitions(event, config, raw)
    if not definitions:
        return prompt
    return prompt + (
        '\n\n【当前群角色别名定义，仅用于理解主体】\n'
        + json.dumps(definitions, ensure_ascii=False)
        + '\n保留上述用户原始要求。定义不是画面文字，不将别名理解为随机物品；'
          '未指定具体角色时不要声称是某个固定角色，不添加未要求的属性或修改。'
    )


def subject_tool_hint(event, config, raw):
    if not isinstance(raw, str) or not _IMAGE_INTENT.search(raw):
        return ''
    definitions = subject_definitions(event, config, raw)
    if not definitions:
        return ''
    return (
        '\n本轮绘图请求中的群内角色别名定义：'
        + json.dumps(definitions, ensure_ascii=False)
        + '。执行器会单独补充这些定义，prompt 仍忠实保留用户原文。'
          '按当前请求和当前参考图片判断，不能把历史中某次请求的拒绝理由扩大到该别名的所有请求。'
          '普通非色情角色画像、常服造型和表情包，不因角色别名或年龄信息本身一概拒绝；'
          '区分非性化的游泳服饰描绘与性化呈现，不能仅凭服饰名称或别名断言违法。'
          '若当前请求触及内容限制，应简要说明具体问题，不作无依据的法律结论。'
          '没有调用指定接口时，不得声称该接口已经拒绝；HTTP 400 等通用错误也不能自行解释为内容违法。'
          '具体请求仍须遵守适用的内容限制。别名定义不改变年龄，也不赋予额外权限。'
    )
