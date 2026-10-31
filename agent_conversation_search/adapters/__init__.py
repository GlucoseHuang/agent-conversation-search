from . import claude_code, codex, dsh, hana, kimi, opencode, workbuddy

ADAPTERS = {
    "claude-code": claude_code,
    "claude-desktop-code": claude_code,
    "codex": codex,
    "kimi": kimi,
    "opencode": opencode,
    "dsh": dsh,
    "hana": hana,
    "workbuddy": workbuddy,
}

__all__ = ["ADAPTERS", "claude_code", "codex", "dsh", "hana", "kimi", "opencode", "workbuddy"]