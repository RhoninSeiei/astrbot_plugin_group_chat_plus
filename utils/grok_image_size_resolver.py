"""Resolve only geometry supported by the installed Grok Imagine interface."""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from .image_size_syntax import is_panel_grid


@dataclass(frozen=True)
class ResolvedGrokImageSize:
    requested: str
    resolved: str
    source: str
    experimental: bool = False


class GrokImageSizeResolutionError(ValueError):
    def __init__(self, reason_code: str = 'invalid_image_size') -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class GrokImageSizeResolver:
    RATIOS = frozenset({'auto', '1:1', '3:2', '2:3', '4:3', '3:4', '16:9', '9:16', '21:9', '5:2'})
    RESOLUTIONS = frozenset({'1k', '2k'})
    COMPATIBILITY_SIZES = {'1024x1024': '1:1@1k', '2048x2048': '1:1@2k'}

    @classmethod
    def normalize(cls, value: str) -> str:
        value = ''.join(str(value or 'auto').lower().replace('：', ':').replace('×', 'x').split())
        value = cls.COMPATIBILITY_SIZES.get(value, value)
        parts = value.split('@')
        if len(parts) > 2 or parts[0] not in cls.RATIOS:
            raise GrokImageSizeResolutionError()
        if len(parts) == 2 and parts[1] not in cls.RESOLUTIONS:
            raise GrokImageSizeResolutionError()
        return value

    @classmethod
    def parameters(cls, value: str) -> dict[str, str]:
        parts = cls.normalize(value).split('@')
        result = {'aspect_ratio': parts[0]}
        if len(parts) == 2:
            result['resolution'] = parts[1]
        return result

    @classmethod
    def resolve(cls, raw_request: str) -> ResolvedGrokImageSize:
        text = str(raw_request or '')
        ratios, resolutions, requested = set(), set(), []
        for match in re.finditer(r'(?<!\d)(\d+)\s*[xX×]\s*(\d+)(?!\d)', text):
            if is_panel_grid(text, match):
                continue
            pixels = 'x'.join(match.groups())
            mapped = cls.COMPATIBILITY_SIZES.get(pixels)
            if mapped is None:
                raise GrokImageSizeResolutionError()
            ratio, resolution = mapped.split('@')
            ratios.add(ratio); resolutions.add(resolution); requested.append(match.group())
        for match in re.finditer(r'(?<!\d)(\d+)\s*(?:[:：]|比)\s*(\d+)(?!\d)', text):
            try:
                fraction = Fraction(int(match[1]), int(match[2]))
            except (ValueError, ZeroDivisionError):
                raise GrokImageSizeResolutionError() from None
            ratio = next((r for r in cls.RATIOS if r != 'auto'
                          and Fraction(*map(int, r.split(':'))) == fraction), None)
            if ratio is None:
                raise GrokImageSizeResolutionError()
            ratios.add(ratio); requested.append(match.group())
        for match in re.finditer(r'(?<![A-Za-z0-9.])(\d+)\s*[kK](?![A-Za-z0-9])', text):
            resolution = match[1] + 'k'
            if resolution not in cls.RESOLUTIONS:
                raise GrokImageSizeResolutionError()
            resolutions.add(resolution); requested.append(match.group())
        if len(ratios) > 1 or len(resolutions) > 1:
            raise GrokImageSizeResolutionError('conflicting_image_size')
        size = next(iter(ratios), 'auto')
        if resolutions:
            size += '@' + next(iter(resolutions))
        return ResolvedGrokImageSize(' '.join(requested), size, ('explicit_resolution' if resolutions else 'explicit_ratio') if requested else 'auto')
