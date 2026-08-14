"""
图片处理器模块
负责处理消息中的图片，包括检测、过滤和转文字

作者: Him666233
版本: v1.2.1
"""

import asyncio
from dataclasses import dataclass
from typing import List, Optional, Tuple
from astrbot.api.all import *
from astrbot.api.message_components import Face, At, Reply
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images
from .image_description_cache import ImageDescriptionCache

# 详细日志开关（与 main.py 同款方式：单独用 if 控制）
DEBUG_MODE: bool = False


@dataclass(frozen=True)
class ResolvedMessageImage:
    url: str
    source: str
    component_index: int


PLUGIN_REFERENCE_IMAGE_URLS = "_group_chat_plus_reference_image_urls"


class ImageHandler:
    """
    图片处理器

    主要功能：
    1. 检测消息中的图片
    2. 过滤纯图片消息或移除图片
    3. 调用AI将图片转为文字描述
    4. 将描述融入原消息
    """

    @staticmethod
    async def _resolve_image_component(image, source: str, component_index: int):
        try:
            value = await image.convert_to_file_path()
        except Exception as exc:
            logger.warning(
                "[QUOTED_IMAGE_RESOLVE_FAILED] source=%s component_index=%s error_type=%s",
                source,
                component_index,
                type(exc).__name__,
            )
            return None
        value = str(value or "").strip()
        if not value:
            logger.warning(
                "[QUOTED_IMAGE_RESOLVE_FAILED] source=%s component_index=%s error_type=empty_result",
                source,
                component_index,
            )
            return None
        return ResolvedMessageImage(value, source, component_index)

    @staticmethod
    async def _collect_reply_images(
        event,
        reply,
        component_index,
        max_results=None,
        seen_urls=None,
    ):
        remaining = None if max_results is None else max(0, int(max_results))
        if remaining == 0:
            return []
        seen_urls = set(seen_urls or ())
        reply_seen = set()
        embedded = [
            item
            for item in (getattr(reply, "chain", None) or [])
            if isinstance(item, Image)
        ]
        embedded_results = []
        embedded_failed = False
        for image in embedded:
            if remaining is not None and len(embedded_results) >= remaining:
                break
            resolved = await ImageHandler._resolve_image_component(
                image,
                "quoted_embedded",
                component_index,
            )
            if resolved is None:
                embedded_failed = True
            elif resolved.url not in seen_urls and resolved.url not in reply_seen:
                reply_seen.add(resolved.url)
                embedded_results.append(resolved)

        embedded_budget_spent = (
            remaining is not None and len(embedded_results) >= remaining
        )
        if embedded and (not embedded_failed or embedded_budget_spent):
            return embedded_results

        reason = "missing_embedded" if not embedded else "embedded_resolve_failed"
        logger.info("[QUOTED_IMAGE_FALLBACK] reason=%s", reason)
        try:
            fetched = await extract_quoted_message_images(event, reply)
        except Exception as exc:
            logger.warning(
                "[QUOTED_IMAGE_RESOLVE_FAILED] source=quoted_fetched "
                "component_index=%s error_type=%s",
                component_index,
                type(exc).__name__,
            )
            return embedded_results

        fetched_results = []
        fetched_seen = set()
        for value in fetched or []:
            normalized = str(value or "").strip()
            if (
                not normalized
                or normalized in seen_urls
                or normalized in fetched_seen
            ):
                continue
            fetched_seen.add(normalized)
            fetched_results.append(
                ResolvedMessageImage(
                    normalized,
                    "quoted_fetched",
                    component_index,
                )
            )
            if remaining is not None and len(fetched_results) >= remaining:
                break
        return fetched_results or embedded_results

    @staticmethod
    async def collect_message_images(
        event, message_chain=None, max_images=10
    ) -> List[ResolvedMessageImage]:
        chain_source = (
            message_chain
            if message_chain is not None
            else getattr(event.message_obj, "message", [])
        )
        chain = list(chain_source or [])
        limit = max(0, int(max_images))
        if limit == 0:
            return []

        results = []
        seen = set()

        def append_items(items):
            for item in items:
                if item.url in seen:
                    continue
                seen.add(item.url)
                results.append(item)
                if len(results) >= limit:
                    return True
            return False

        for component_index, component in enumerate(chain):
            if isinstance(component, Image):
                item = await ImageHandler._resolve_image_component(
                    component, "top_level", component_index
                )
                if item and append_items([item]):
                    break
                continue
            if not isinstance(component, Reply):
                continue

            items = await ImageHandler._collect_reply_images(
                event,
                component,
                component_index,
                limit - len(results),
                seen,
            )
            if append_items(items):
                break

        logger.info(
            "[QUOTED_IMAGE_COLLECTED] top_level=%s quoted_embedded=%s quoted_fetched=%s total=%s",
            sum(item.source == "top_level" for item in results),
            sum(item.source == "quoted_embedded" for item in results),
            sum(item.source == "quoted_fetched" for item in results),
            len(results),
        )
        return results

    @staticmethod
    async def process_message_images(
        event: AstrMessageEvent,
        context: Context,
        enable_image_processing: bool,
        image_to_text_scope: str,
        image_to_text_provider_id: str,
        image_to_text_prompt: str,
        is_at_message: bool,
        has_trigger_keyword: bool,
        timeout: int = 60,
        image_description_cache: Optional[ImageDescriptionCache] = None,
        max_images_per_message: int = 10,
    ) -> Tuple[bool, str, List[str], bool]:
        """
        处理消息中的图片

        Args:
            event: 消息事件
            context: Context对象
            enable_image_processing: 是否启用图片处理
            image_to_text_scope: 应用范围（all/mention_only/at_only/keyword_only）
            image_to_text_provider_id: 图片转文字AI提供商ID
            image_to_text_prompt: 转换提示词
            is_at_message: 是否@消息
            has_trigger_keyword: 是否包含触发关键词
            timeout: 图片转文字超时时间（秒）
            image_description_cache: 图片描述缓存实例（可选，用于省钱）
            max_images_per_message: 单条消息最大处理图片数

        Returns:
            (是否继续处理, 处理后的消息, 图片URL列表, 图片是否保留)
            - True=继续，False=丢弃
            - 图片URL列表：用于多模态AI直接处理
            - 图片是否保留：True=图片信息仍在消息中（作为URL或文字描述），False=图片已被移除/过滤
        """
        try:
            # 获取消息链
            if not hasattr(event, "message_obj") or not hasattr(
                event.message_obj, "message"
            ):
                # 没有消息链,使用原始文本
                return True, event.get_message_outline(), [], False

            message_chain = event.message_obj.message

            # 检查消息中是否有图片
            resolved_images = await ImageHandler.collect_message_images(
                event,
                message_chain,
                max_images_per_message,
            )
            has_image = bool(resolved_images)
            has_text = ImageHandler._has_text_content(message_chain)
            event.set_extra(
                PLUGIN_REFERENCE_IMAGE_URLS,
                [item.url for item in resolved_images],
            )

            # 如果没有图片，从消息链提取完整文本（含引用内容），不使用 get_message_outline()
            # 因为 get_message_outline() 通常不包含引用消息（Reply 组件）的内容
            if not has_image:
                text_content = ImageHandler._extract_text_only(message_chain)
                if not text_content:
                    # 兜底：message chain 提取为空时才使用 get_message_outline()
                    text_content = event.get_message_outline()
                return True, text_content, [], False

            if DEBUG_MODE:
                logger.info(
                    f"检测到消息包含 {len(resolved_images)} 张图片, 是否有文字: {has_text}"
                )

            # === 第一步：检查图片处理开关 ===
            # 如果不启用图片处理，所有带图片的消息都要过滤（不管是什么模式）
            if not enable_image_processing:
                if DEBUG_MODE:
                    logger.info("图片处理未启用,过滤所有图片")
                # 如果是纯图片消息,丢弃
                if not has_text:
                    if DEBUG_MODE:
                        logger.info("检测到纯图片消息,但图片处理未启用,丢弃该消息")
                    return False, "", [], False
                else:
                    # 如果是图文混合,移除图片只保留文字
                    text_only = ImageHandler._extract_text_only(message_chain)
                    if DEBUG_MODE:
                        logger.info(
                            "IMAGE_FILTERED mode=disabled has_text=%s",
                            bool(text_only),
                        )
                    return True, text_only, [], False

            # === 第二步：根据应用范围(image_to_text_scope)决定是否对当前消息启用图片转文字 ===
            scope = (image_to_text_scope or "all").strip().lower()
            should_apply_image_to_text = True

            # 🔍 调试日志：始终输出scope判断信息，便于排查问题
            logger.info(
                f"🖼️ [图片范围检查] scope={scope}, is_at_message={is_at_message}, has_trigger_keyword={has_trigger_keyword}"
            )

            if scope == "all":
                should_apply_image_to_text = True
            elif scope == "mention_only":
                # 兼容旧逻辑：@消息或包含触发关键词的消息都视为适用
                should_apply_image_to_text = is_at_message or has_trigger_keyword
            elif scope == "at_only":
                # 仅对真正的@机器人消息启用图片转文字
                should_apply_image_to_text = is_at_message
            elif scope == "keyword_only":
                # 仅对包含触发关键词的消息启用图片转文字
                should_apply_image_to_text = has_trigger_keyword
            else:
                # 未知配置值时，退回到与mention_only一致的行为
                should_apply_image_to_text = is_at_message or has_trigger_keyword

            # 🔍 调试日志：输出最终判断结果
            logger.info(
                f"🖼️ [图片范围判断] should_apply_image_to_text={should_apply_image_to_text}"
            )

            if not should_apply_image_to_text:
                if DEBUG_MODE:
                    logger.info(
                        f"图片转文字应用范围为{scope}, 当前消息不符合范围, 过滤图片"
                    )
                # 如果是纯图片消息,丢弃
                if not has_text:
                    if DEBUG_MODE:
                        logger.info("非适用范围内的纯图片消息,丢弃该消息")
                    return False, "", [], False
                else:
                    # 如果是图文混合,移除图片只保留文字
                    text_only = ImageHandler._extract_text_only(message_chain)
                    if DEBUG_MODE:
                        logger.info(
                            "IMAGE_FILTERED mode=scope has_text=%s",
                            bool(text_only),
                        )
                    return True, text_only, [], False

            # === 第三步：启用了图片处理，根据是否配置图片转文字ID决定处理方式 ===
            if DEBUG_MODE:
                logger.info("图片处理已启用")

            # 如果没有填写图片转文字的提供商ID,说明使用多模态AI,提取图片URL传递
            if not image_to_text_provider_id:
                if DEBUG_MODE:
                    logger.info("未配置图片转文字提供商ID,提取图片URL传递给多模态AI")
                # 提取图片URL
                image_urls = [item.url for item in resolved_images]
                # 提取文本内容（不包含图片）
                text_content = ImageHandler._extract_text_only(message_chain)
                if DEBUG_MODE:
                    logger.info(
                        "IMAGE_PROCESSING_COMPLETE mode=multimodal "
                        "image_count=%s has_text=%s",
                        len(image_urls),
                        bool(text_content),
                    )
                return True, text_content, image_urls, bool(image_urls)

            # === 第四步：配置了图片转文字提供商ID，尝试转换图片 ===
            if DEBUG_MODE:
                logger.info(
                    f"已配置图片转文字提供商ID,尝试转换图片(超时时间: {timeout}秒)"
                )
            processed_message = await ImageHandler._convert_images_to_text(
                message_chain,
                context,
                image_to_text_provider_id,
                image_to_text_prompt,
                resolved_images,
                timeout,
                image_description_cache,
            )

            # 如果转换失败或超时,进行降级处理（过滤图片）
            if processed_message is None:
                logger.warning("图片转文字超时或失败,进行过滤处理")
                # 纯图片（无文字、无引用）且转换失败：不丢弃，用占位符替代每张图片
                # 仅在用户填写了转换服务商ID（真实转换模式）时才会走到这里
                if not has_text:
                    fallback_parts = []
                    for comp in message_chain:
                        if isinstance(comp, Image):
                            fallback_parts.append("[图片（识别失败）]")
                        else:
                            fmt = ImageHandler._format_special_component(comp)
                            if fmt:
                                fallback_parts.append(fmt)
                    fallback_text = (
                        "".join(fallback_parts).strip() or "[图片（识别失败）]"
                    )
                    logger.warning(
                        "IMAGE_DESCRIPTION_FALLBACK mode=pure_image "
                        "placeholder_count=%s",
                        len(fallback_parts),
                    )
                    return True, fallback_text, [], False
                else:
                    # 如果是图文混合,只保留文字
                    text_only = ImageHandler._extract_text_only(message_chain)
                    if DEBUG_MODE:
                        logger.info(
                            "IMAGE_DESCRIPTION_FALLBACK mode=text_only has_text=%s",
                            bool(text_only),
                        )
                    return True, text_only, [], False  # 图片转文字失败，图片被移除

            # 转换成功，返回转换后的消息（图片已转成文字描述）
            if DEBUG_MODE:
                logger.info(
                    "IMAGE_PROCESSING_COMPLETE mode=image_to_text "
                    "reference_count=%s",
                    len(resolved_images),
                )
            return (
                True,
                processed_message,
                [],
                True,
            )  # 图片转文字成功: 图片信息保留为文字描述

        except Exception as exc:
            logger.error(
                "IMAGE_PROCESSING_FAILED error_type=%s",
                exc.__class__.__name__,
            )
            # 发生错误时,返回原消息文本
            return True, event.get_message_outline(), [], False

    @staticmethod
    def _analyze_message(
        message_chain: List[BaseMessageComponent],
        max_images: int = 10,
    ) -> Tuple[bool, bool, List[Image]]:
        """
        分析消息链，检查图片和文字

        Args:
            message_chain: 消息链
            max_images: 单条消息最大处理图片数

        Returns:
            (是否有图片, 是否有文字, 图片组件列表)
        """
        has_image = False
        has_text = False
        image_components = []

        for component in message_chain:
            if isinstance(component, Image):
                has_image = True
                image_components.append(component)
            elif isinstance(component, Plain):
                # 检查是否有非空白文字
                if component.text and component.text.strip():
                    has_text = True
            elif isinstance(component, Reply):
                # 引用消息也视为有文字内容，防止「引用+图片」的消息被当作纯图片丢弃
                has_text = True

        # 限制单条消息处理的图片数量，防止恶意刷图
        if len(image_components) > max_images:
            logger.warning(
                f"[图片处理] 单条消息包含 {len(image_components)} 张图片，"
                f"超过上限 {max_images}，仅处理前 {max_images} 张"
            )
            image_components = image_components[:max_images]

        return has_image, has_text, image_components

    @staticmethod
    def _has_text_content(message_chain: List[BaseMessageComponent]) -> bool:
        return any(
            isinstance(component, Reply)
            or (
                isinstance(component, Plain)
                and bool(str(getattr(component, "text", "") or "").strip())
            )
            for component in message_chain
        )

    @staticmethod
    def _format_special_component(component: BaseMessageComponent) -> str:
        """
        格式化特殊消息组件为文本表示

        Args:
            component: 消息组件

        Returns:
            格式化后的文本，如果不是特殊组件返回空字符串
        """
        if isinstance(component, Face):
            return f"[表情:{component.id}]"
        elif isinstance(component, At):
            return f"[At:{component.qq}]"
        elif isinstance(component, Reply):
            # 格式化引用消息，保留引用内容让AI理解上下文
            try:
                message_content = getattr(component, "message_str", None) or getattr(
                    component, "message", None
                )
                sender_nickname = getattr(
                    component, "sender_nickname", None
                ) or getattr(component, "sender_name", None)
                if not sender_nickname and hasattr(component, "sender"):
                    sender_nickname = getattr(component.sender, "nickname", None)
                sender_id = getattr(component, "sender_id", None)
                if message_content:
                    if sender_nickname and sender_id:
                        return f"[引用 {sender_nickname}(ID:{sender_id}): {message_content}]"
                    elif sender_id:
                        return f"[引用 用户(ID:{sender_id}): {message_content}]"
                    elif sender_nickname:
                        return f"[引用 {sender_nickname}: {message_content}]"
                    else:
                        return f"[引用消息: {message_content}]"
                return "[引用消息]"
            except Exception:
                return "[引用消息]"
        else:
            return ""

    @staticmethod
    def _extract_text_only(message_chain: List[BaseMessageComponent]) -> str:
        """
        从消息链提取纯文字，过滤图片

        Args:
            message_chain: 消息链

        Returns:
            纯文字内容
        """
        text_parts = []

        for component in message_chain:
            if isinstance(component, Plain):
                text_parts.append(component.text)
            elif isinstance(component, Image):
                # 跳过图片
                continue
            else:
                # 其他类型的组件,尝试转为文本表示
                formatted = ImageHandler._format_special_component(component)
                if formatted:
                    text_parts.append(formatted)

        result = "".join(text_parts).strip()
        if not result:
            logger.warning(
                "IMAGE_TEXT_EXTRACTION_EMPTY part_count=%s",
                len(text_parts),
            )
        return result

    @staticmethod
    async def _extract_image_urls(image_components: List[Image]) -> List[str]:
        """
        从图片组件列表中提取图片URL

        Args:
            image_components: 图片组件列表

        Returns:
            图片URL列表（可能包含本地路径或base64等格式）
        """
        image_urls = []
        for idx, img_component in enumerate(image_components):
            try:
                # 尝试获取图片路径或URL
                image_path = await img_component.convert_to_file_path()
                if image_path:
                    image_urls.append(image_path)
                    if DEBUG_MODE:
                        logger.info(
                            "IMAGE_URL_EXTRACTED index=%s source=top_level",
                            idx,
                        )
                else:
                    logger.warning(
                        "IMAGE_URL_EXTRACT_FAILED index=%s error_type=empty_result",
                        idx,
                    )
            except Exception as exc:
                logger.error(
                    "IMAGE_URL_EXTRACT_FAILED index=%s error_type=%s",
                    idx,
                    exc.__class__.__name__,
                )
                continue

        return image_urls

    @staticmethod
    async def _convert_images_to_text(
        message_chain: List[BaseMessageComponent],
        context: Context,
        provider_id: str,
        prompt: str,
        resolved_images: List[ResolvedMessageImage],
        timeout: int = 60,
        image_description_cache: Optional[ImageDescriptionCache] = None,
    ) -> Optional[str]:
        """
        将图片转换为文字描述

        Args:
            message_chain: 消息链
            context: Context对象
            provider_id: AI提供商ID
            prompt: 转换提示词
            resolved_images: 已解析图片记录列表
            timeout: 超时时间（秒）
            image_description_cache: 图片描述缓存实例（可选）

        Returns:
            转换后的文本，失败返回None
        """
        try:
            # 获取指定的提供商
            provider = context.get_provider_by_id(provider_id)
            if not provider:
                logger.error("IMAGE_DESCRIPTION_PROVIDER_MISSING")
                return None

            # 对每张图片进行转文字
            image_descriptions = {}
            for idx, resolved_image in enumerate(resolved_images):
                try:
                    image_path = resolved_image.url

                    if DEBUG_MODE:
                        logger.info(
                            "IMAGE_DESCRIPTION_CONVERTING index=%s source=%s",
                            idx,
                            resolved_image.source,
                        )

                    # 调用AI进行图片转文字,添加超时控制
                    async def call_vision_ai():
                        response = await provider.text_chat(
                            prompt=prompt,
                            contexts=[],
                            image_urls=[image_path],
                            func_tool=None,
                            system_prompt="",
                        )
                        return response.completion_text

                    if image_description_cache and image_description_cache.enabled:
                        was_cached = bool(image_description_cache.lookup(image_path))
                        description = await image_description_cache.get_or_create(
                            image_path,
                            lambda: asyncio.wait_for(
                                call_vision_ai(),
                                timeout=timeout,
                            ),
                        )
                        if was_cached:
                            logger.info(
                                "IMAGE_DESCRIPTION_CACHE_HIT index=%s source=%s",
                                idx,
                                resolved_image.source,
                            )
                    else:
                        description = await asyncio.wait_for(
                            call_vision_ai(), timeout=timeout
                        )

                    if description:
                        image_descriptions[idx] = description
                        if DEBUG_MODE:
                            logger.info(
                                "IMAGE_DESCRIPTION_CONVERTED index=%s source=%s",
                                idx,
                                resolved_image.source,
                            )

                except asyncio.TimeoutError:
                    logger.warning(
                        "IMAGE_DESCRIPTION_TIMEOUT index=%s source=%s timeout=%s",
                        idx,
                        resolved_image.source,
                        timeout,
                    )
                    continue
                except Exception as exc:
                    logger.error(
                        "IMAGE_DESCRIPTION_FAILED index=%s source=%s error_type=%s",
                        idx,
                        resolved_image.source,
                        exc.__class__.__name__,
                    )
                    continue

            # 如果没有成功转换任何图片,返回None
            if not image_descriptions:
                logger.warning("没有成功转换任何图片")
                return None

            rendered_by_component = {}
            for idx, resolved_image in enumerate(resolved_images):
                description = image_descriptions.get(idx)
                is_quoted = resolved_image.source.startswith("quoted_")
                if description:
                    rendered = (
                        f"[引用图片内容: {description}]"
                        if is_quoted
                        else f"[图片内容: {description}]"
                    )
                else:
                    rendered = "[引用图片]" if is_quoted else "[图片]"
                rendered_by_component.setdefault(
                    resolved_image.component_index,
                    [],
                ).append(rendered)

            # 构建新的消息文本,将图片替换为描述
            result_parts = []
            for chain_idx, component in enumerate(message_chain):
                if isinstance(component, Plain):
                    result_parts.append(component.text)
                elif isinstance(component, Image):
                    rendered = rendered_by_component.get(chain_idx)
                    if rendered:
                        result_parts.extend(rendered)
                    else:
                        result_parts.append("[图片]")
                elif isinstance(component, Reply):
                    formatted = ImageHandler._format_special_component(component)
                    if formatted:
                        result_parts.append(formatted)
                    result_parts.extend(rendered_by_component.get(chain_idx, []))
                else:
                    # 其他组件使用统一的格式化方法
                    formatted = ImageHandler._format_special_component(component)
                    if formatted:
                        result_parts.append(formatted)

            result_text = "".join(result_parts)
            if DEBUG_MODE:
                logger.info(
                    "IMAGE_DESCRIPTION_PROCESS_COMPLETE "
                    "resolved_count=%s described_count=%s",
                    len(resolved_images),
                    len(image_descriptions),
                )
            return result_text

        except Exception as exc:
            logger.error(
                "IMAGE_DESCRIPTION_PROCESS_FAILED error_type=%s",
                exc.__class__.__name__,
            )
            return None
