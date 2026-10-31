from agent_conversation_search.redact import redact_text


def test_redacts_api_keys_and_bearer_tokens():
    text = "key sk-ws-abc1234567890 token=super-secret-value Authorization: Bearer abcdefghijklmnop"
    redacted = redact_text(text)
    assert "sk-ws-abc1234567890" not in redacted
    assert "super-secret-value" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "[REDACTED:" in redacted


def test_keeps_normal_conversation_text():
    text = "我们在讨论 TTS 方案和每日简报，没有密钥。"
    assert redact_text(text) == text


def test_removes_unpaired_surrogates():
    assert redact_text("hello \ud83e world") == "hello ? world"