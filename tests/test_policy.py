import main
from state import ModelHealth


def test_should_search_requires_external_topic():
    assert main.should_search("今天苹果股价多少", True)
    assert not main.should_search("你现在的模型是什么", True)
    assert not main.should_search("帮我看看这个 bridge 的当前状态", True)


def test_timeout_policy_prefers_code_then_image_then_search():
    assert main.get_task_timeout("写一个脚本", False, False, main.TIMEOUT_NORMAL, main.TIMEOUT_SEARCH, main.TIMEOUT_CODE, main.TIMEOUT_IMAGE) == main.TIMEOUT_CODE
    assert main.get_task_timeout("请描述图片", False, True, main.TIMEOUT_NORMAL, main.TIMEOUT_SEARCH, main.TIMEOUT_CODE, main.TIMEOUT_IMAGE) == main.TIMEOUT_IMAGE
    assert main.get_task_timeout("今天黄金价格", True, False, main.TIMEOUT_NORMAL, main.TIMEOUT_SEARCH, main.TIMEOUT_CODE, main.TIMEOUT_IMAGE) == main.TIMEOUT_SEARCH
    assert main.get_task_timeout("你好", False, False, main.TIMEOUT_NORMAL, main.TIMEOUT_SEARCH, main.TIMEOUT_CODE, main.TIMEOUT_IMAGE) == main.TIMEOUT_NORMAL


def test_dangerous_request_detection():
    assert main.is_dangerous_request("rm -rf /tmp/demo")
    assert main.is_dangerous_request("请删除这个目录")
    assert not main.is_dangerous_request("帮我读取这个目录")


def test_auth_required_disables_model_for_cooldown(tmp_path):
    health = ModelHealth(str(tmp_path / "health.json"), main.logger)

    health.record_failure("claude", "auth required")

    assert health.is_available("claude") is False
    assert "需重新登录" in health.status_line("claude")
