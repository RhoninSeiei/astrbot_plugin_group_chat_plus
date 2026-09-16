"""Grok Imagine adapter using the existing Provider's credential and media boundary."""
from __future__ import annotations

import asyncio
import base64
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .grok_image_size_resolver import GrokImageSizeResolver, GrokImageSizeResolutionError


DEFAULT_GROK_IMAGE_MODEL = 'grok-imagine-image-2.0'


class GrokOAuthImageUserError(Exception):
    pass


class GrokOAuthImageConfigError(Exception):
    pass


class GrokOAuthImageProviderError(Exception):
    def __init__(self, message='Grok 图片服务调用未完成。', *, reason_code='provider_call_failed', partial=False, assets=(), request_id=''):
        super().__init__(message)
        self.reason_code = reason_code
        self.diagnostic = 'category=' + reason_code
        self.retryable = False
        self.partial = bool(partial)
        self.assets = tuple(assets)
        self.request_id = str(request_id) if re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', str(request_id)) else ''


@dataclass(frozen=True)
class GrokOAuthImageResult:
    path: str
    mode: str
    backend: str = 'grok_oauth'
    media_type: str = 'image/png'
    revised_prompt: str = ''


class GrokOAuthImageService:
    MAX_PROMPT_CHARS = 9216
    MAX_REFERENCE_BYTES = 20 * 1024 * 1024
    MAX_TOTAL_REFERENCE_BYTES = 80 * 1024 * 1024

    def __init__(self, *, context: Any, config: dict):
        self.context = context
        self.config = dict(config or {})

    @staticmethod
    def normalize_size(value):
        try:
            return GrokImageSizeResolver.normalize(value)
        except GrokImageSizeResolutionError:
            raise GrokOAuthImageUserError('Grok 图片尺寸不受支持。') from None

    def _geometry(self, size: str) -> tuple[str, str]:
        value = ''.join(str(size or self.config.get('grok_image_aspect_ratio') or '1:1').lower().split())
        value = value.replace('×', 'x').replace('：', ':')
        aliases = {
            'square': '1:1', '方图': '1:1', '1536x1024': '3:2', '1024x1536': '2:3',
            '1080p': '16:9', '1920x1080': '16:9', '1080x1920': '9:16',
            'landscape': '16:9', '横图': '16:9', 'portrait': '9:16', '竖图': '9:16',
        }
        value = aliases.get(value, value)
        normalized = self.normalize_size(value)
        ratio, _, explicit_resolution = normalized.partition('@')
        resolution = explicit_resolution or ('2k' if value == '2048x2048' else str(self.config.get('grok_image_resolution') or '1k'))
        if resolution not in {'1k', '2k'}:
            raise GrokOAuthImageConfigError('Grok 图片分辨率必须为 1k 或 2k。')
        return ratio, resolution

    def _settings(self):
        model = str(self.config.get('grok_image_model') or DEFAULT_GROK_IMAGE_MODEL).strip()
        if not model.startswith('grok-imagine-image'):
            raise GrokOAuthImageConfigError('Grok 绘图必须使用 Imagine 图片模型。')
        raw = self.config.get('grok_image_timeout', 180)
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            raise GrokOAuthImageConfigError('Grok 图片超时配置无效。') from None
        if isinstance(raw, bool) or not math.isfinite(timeout) or not 1 <= timeout <= 600:
            raise GrokOAuthImageConfigError('Grok 图片超时必须在 1 至 600 秒之间。')
        return model, timeout

    def _provider(self, needs_edit):
        provider_id = str(self.config.get('image_planner_provider_id') or '').strip()
        try:
            provider = self.context.get_provider_by_id(provider_id)
            metadata = provider.meta() if provider else None
            if metadata is None or metadata.type != 'grok_oauth_chat_completion':
                raise GrokOAuthImageConfigError('请选择 Grok OAuth Provider。')
            caps = provider.capabilities
            for key in ('image_generation', 'image_editing') if needs_edit else ('image_generation',):
                cap = caps.get(key, {}) if isinstance(caps, dict) else {}
                if not isinstance(cap, dict) or cap.get('implementation') is not True or cap.get('enabled') is not True:
                    raise GrokOAuthImageConfigError('Grok 图片能力未启用。')
            if not callable(getattr(provider, 'generate_image', None)):
                raise GrokOAuthImageConfigError('Grok 图片接口不可用。')
            return provider
        except GrokOAuthImageConfigError:
            raise
        except Exception:
            raise GrokOAuthImageProviderError('Grok 图片提供商查询失败。', reason_code='provider_lookup_failed') from None

    def validate_configuration(self, *, needs_edit):
        self._settings()
        self._provider(needs_edit)

    @classmethod
    def _reference(cls, value):
        fd = -1
        try:
            path = Path(str(value))
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= cls.MAX_REFERENCE_BYTES:
                raise GrokOAuthImageUserError('参考图片格式或大小无效。')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                fd = -1
                opened = os.fstat(stream.fileno())
                data = stream.read(cls.MAX_REFERENCE_BYTES + 1)
                after = os.fstat(stream.fileno())
            current = path.lstat()
            fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
            if (fingerprint(before) != fingerprint(opened) or fingerprint(opened) != fingerprint(after)
                    or fingerprint(after) != fingerprint(current) or not stat.S_ISREG(current.st_mode)
                    or len(data) != after.st_size or len(data) > cls.MAX_REFERENCE_BYTES):
                raise GrokOAuthImageUserError('参考图片在读取期间发生变化。')
            if data.startswith(b'\x89PNG\r\n\x1a\n'):
                mime = 'image/png'
            elif data.startswith(b'\xff\xd8\xff'):
                mime = 'image/jpeg'
            elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
                mime = 'image/webp'
            else:
                raise GrokOAuthImageUserError('Grok 参考图片仅支持 PNG、JPEG、WebP。')
            return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii'), len(data)
        except GrokOAuthImageUserError:
            raise
        except (OSError, ValueError):
            raise GrokOAuthImageUserError('参考图片不可读取。') from None
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _result(asset, action):
        try:
            path = Path(str(asset.path))
            if path.is_symlink() or not path.is_file():
                raise ValueError('missing file')
            mime = str(asset.mime_type)
            if mime not in {'image/png', 'image/jpeg', 'image/webp'}:
                raise ValueError('unsupported image')
            return GrokOAuthImageResult(str(path), action, media_type=mime,
                                       revised_prompt=str(getattr(asset, 'revised_prompt', '') or ''))
        except Exception:
            raise GrokOAuthImageProviderError('Grok 图片结果不可用。', reason_code='result_read_failed') from None

    async def generate(self, *, prompt: str, size: str = ''):
        return await self._execute(prompt=prompt, size=size, paths=(), action='generate')

    async def edit(self, *, prompt: str, image_path: str | None = None,
                   image_paths: Sequence[str] | None = None, size: str = ''):
        if (image_path is None) == (image_paths is None):
            raise GrokOAuthImageUserError('Grok 图片编辑需要 1 至 5 张参考图。')
        if image_path is not None:
            image_paths = (image_path,)
        if isinstance(image_paths, (str, bytes)) or not isinstance(image_paths, (list, tuple)) or not 1 <= len(image_paths) <= 5:
            raise GrokOAuthImageUserError('Grok 图片编辑需要 1 至 5 张参考图。')
        return await self._execute(prompt=prompt, size=size, paths=tuple(image_paths), action='edit')

    async def _execute(self, *, prompt, size, paths, action):
        prompt = str(prompt or '').strip()
        raw_budget = self.config.get('grok_image_prompt_max_chars', self.MAX_PROMPT_CHARS)
        try:
            budget = int(raw_budget)
        except (TypeError, ValueError, OverflowError):
            raise GrokOAuthImageConfigError('Grok 图片提示词预算无效。') from None
        if isinstance(raw_budget, bool) or str(raw_budget).strip() != str(budget) or not 2048 <= budget <= 32000:
            raise GrokOAuthImageConfigError('Grok 图片提示词预算必须在 2048 至 32000 字符之间。')
        if not prompt or len(prompt) > budget:
            raise GrokOAuthImageUserError(f'图片提示词必须为 1 至 {budget} 个字符。')
        model, timeout = self._settings()
        provider = self._provider(bool(paths))
        ratio, resolution = self._geometry(size)
        geometry = {'aspect_ratio': ratio, 'resolution': resolution}
        references = [await asyncio.to_thread(self._reference, path) for path in paths]
        if sum(length for _, length in references) > self.MAX_TOTAL_REFERENCE_BYTES:
            raise GrokOAuthImageUserError('参考图片总大小超过 80 MiB。')
        try:
            # The Provider owns the deadline, cancellation and OutcomeUnknown semantics.
            # Never retry or fall back: an unsuccessful response may already have generated assets.
            generated = await provider.generate_image(
                prompt=prompt, model=model, n=1, action=action, timeout=timeout,
                reference_images=[uri for uri, _ in references] or None, **geometry)
        except Exception as error:
            code = getattr(error, 'code', type(error).__name__)
            reasons = {'OutcomeUnknown': 'outcome_unknown', 'PaymentRequired': 'payment_required',
                       'RateLimited': 'rate_limited', 'Busy': 'busy', 'PermissionDenied': 'permission_denied',
                       'ReauthorizationRequired': 'reauthorization_required', 'InvalidImageRequest': 'request_rejected',
                       'TimeoutError': 'provider_timeout'}
            assets = []
            for asset in getattr(error, 'assets', ()) or ():
                try:
                    assets.append(self._result(asset, action))
                except GrokOAuthImageProviderError:
                    continue
            raise GrokOAuthImageProviderError(
                reason_code=reasons.get(code, 'provider_call_failed'), partial=getattr(error, 'partial', False),
                assets=assets, request_id=getattr(error, 'request_id', '')) from None
        if not isinstance(generated, (list, tuple)):
            raise GrokOAuthImageProviderError(reason_code='invalid_result')
        if len(generated) != 1:
            assets = []
            for asset in generated:
                try:
                    assets.append(self._result(asset, action))
                except GrokOAuthImageProviderError:
                    continue
            raise GrokOAuthImageProviderError(reason_code='invalid_result', partial=bool(assets), assets=assets)
        return self._result(generated[0], action)
