"""插件根模块，兼容旧版插件载入路径。"""

try:
    from .main import ChatPlus
except Exception:  # pragma: no cover - 本地轻量测试环境可能缺少运行依赖
    ChatPlus = None


if ChatPlus is not None:
    class Main(ChatPlus):
        pass
else:  # pragma: no cover - 仅用于缺少运行依赖的本地测试环境
    Main = None

__all__ = ["ChatPlus", "Main"]
