from router import command_arg, extract_search_directives, normalize_command


def test_normalize_command_maps_aliases():
    assert normalize_command("/daemon restart") == "/restart"
    assert normalize_command("/service restart") == "/restart"
    assert normalize_command("/task list") == "/tasks"


def test_extract_search_directives():
    assert extract_search_directives("/web 今天黄金价格") == ("今天黄金价格", True, False)
    assert extract_search_directives("/local 你现在的模型是什么") == ("你现在的模型是什么", False, True)
    assert extract_search_directives("普通消息") == ("普通消息", False, False)


def test_command_arg():
    assert command_arg("/task 123") == "123"
    assert command_arg("/task cancel 88") == "cancel 88"
    assert command_arg("/status") == ""
