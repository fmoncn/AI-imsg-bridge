from engine import build_command, build_imessage_prompt, canned_reply, is_execution_task, select_runtime_model


class DummyMemory:
    def has_session(self, model: str) -> bool:
        return False

    def get_context(self, model: str, max_turns: int | None = None) -> str:
        return ""


def test_build_command_uses_explicit_gemini_model():
    cmd = build_command(
        "gemini",
        "自我介绍一下",
        {"gemini": "/tmp/gemini"},
        DummyMemory(),
        3,
        "low",
        "gemini-3-flash-preview",
        True,
        6,
        220,
    )

    assert cmd[:4] == ["/tmp/gemini", "-y", "-m", "gemini-3-flash-preview"]
    assert "[iMessage回复要求]" in cmd[-1]
    assert cmd[-1].endswith("自我介绍一下")


def test_build_imessage_prompt_skips_brief_mode_for_detail_request():
    prompt = build_imessage_prompt("请详细说明原因", True, 6, 220)

    assert prompt == "请详细说明原因"


def test_build_imessage_prompt_wraps_normal_request():
    prompt = build_imessage_prompt("总结今天进展", True, 6, 220)

    assert prompt.startswith("[iMessage回复要求]")
    assert prompt.endswith("总结今天进展")


def test_identity_prompt_keeps_selected_model():
    model = select_runtime_model("自我介绍一下", "gemini", False, False, False, True)

    assert model == "gemini"


def test_identity_prompt_is_not_canned_reply():
    assert canned_reply("自我介绍一下") is None


def test_bridge_summary_routes_to_claude():
    model = select_runtime_model("总结今天 bridge 的进展", "gemini", False, False, False, True)

    assert model == "gemini"


def test_general_explanation_routes_from_codex_to_claude():
    model = select_runtime_model("为什么这个 bridge 有时会超时", "codex", False, False, False, True)

    assert model == "gemini"


def test_explicit_execution_stays_on_codex():
    model = select_runtime_model("检查 bridge 日志并修复超时问题", "codex", False, False, False, True)

    assert model == "codex"


def test_search_summary_routes_from_codex_to_gemini():
    model = select_runtime_model("总结今天 AI 新闻热点", "codex", False, True, False, True)

    assert model == "gemini"


def test_local_explanation_with_disable_search_routes_to_gemini():
    model = select_runtime_model("解释一下这个项目现在的状态", "codex", False, False, True, True)

    assert model == "gemini"


def test_claude_only_used_when_explicitly_selected():
    model = select_runtime_model("为什么这个 bridge 有时会超时", "claude", False, False, False, True)

    assert model == "claude"


def test_is_execution_task_requires_action_intent():
    assert is_execution_task("检查 bridge 日志并修复超时问题") is True
    assert is_execution_task("解释一下这个项目现在的状态") is False
    assert is_execution_task("总结一下这段代码") is False
