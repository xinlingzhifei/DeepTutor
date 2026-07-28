from deeptutor.core import i18n


def test_backend_messages_default_to_chinese_and_preserve_english(monkeypatch) -> None:
    assert i18n._parse_language(None) == "zh"
    assert i18n._parse_language("unsupported") == "zh"
    assert i18n._parse_language("zh-CN") == "zh"
    assert i18n._parse_language("en-US") == "en"

    monkeypatch.setattr(i18n, "current_language", lambda: "zh")
    chinese = i18n.t("api.content_required", language="zh")
    assert i18n.t("api.content_required", language=None) == chinese
    assert i18n.t("api.content_required", language="unsupported") == chinese
    assert i18n.t("api.content_required", language="en") != chinese
