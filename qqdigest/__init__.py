"""qqdigest —— QQ 消息定时整合系统（全本地、零第三方依赖）。

数据流：
    NapCat(OneBot11 HTTP 上报) -> store(SQLite 暂存) -> llm(本地 Qwen3) -> sender(发回 QQ)

设计要点见 README.md 与 ../QQ消息定时整合-技术方案.md。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
