"""Conversation isolation and non-confirmation regressions found during live functional QA."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lan_mesh.chat_handler import ChatHandler
from lan_mesh.service_runtime import ServiceError


@pytest.mark.parametrize('message', [
    '功能验证：请只回复 PONG，不进行任何其他操作。',
    '不要执行任何操作', '请问如何运行程序', '银行业务如何办理',
    '我只是想了解一下', '这是测试消息', '行程安排是什么',
])
def test_embedded_affirmative_characters_do_not_confirm_actions(message):
    assert not ChatHandler._is_confirmation(message)


@pytest.mark.parametrize('message', ['是', '可以', '好的', '确认开始', 'OK',
                                     '是, 以上信息应该也已经足够了'])
def test_complete_affirmative_phrases_remain_supported(message):
    assert ChatHandler._is_confirmation(message)


def handler():
    result = ChatHandler.__new__(ChatHandler)
    result._conversations = {'existing': [{'role': 'assistant',
        'content': '例如「创建项目: 一个项目」'}]}
    result._active_conv_id = 'existing'
    result._conv_index = [{'id': 'existing', 'title': 'existing'}]
    result.controller = SimpleNamespace(_default_model='', model_router=None)
    return result


def test_plain_test_message_never_inherits_a_previous_project_suggestion():
    chat = handler()
    try:
        assert chat._detect_action_with_context(
            '功能验证：请只回复 PONG，不进行任何其他操作。',
            chat._conversations['existing']) == ''
    finally:
        chat.services.close()


def test_unknown_explicit_conversation_is_rejected_before_any_history_or_llm_access():
    chat = handler()
    chat.runtime = Mock()
    with pytest.raises(ServiceError) as error:
        chat.chat('普通测试消息', conv_id='missing')
    assert error.value.status_code == 404
    assert chat._active_conv_id == 'existing'
    assert len(chat._conversations['existing']) == 1
    chat.runtime._call_llm_with_routing.assert_not_called()


def test_failed_classifier_cannot_supply_an_action_even_if_error_text_contains_json():
    chat = handler()
    chat.runtime = Mock()
    chat.runtime._call_llm_with_routing.return_value = {
        '_error': True, 'model': 'none', 'content': '{"action":"create_project"}'}
    try:
        assert chat._classify_action_llm('帮我建个项目', []) == ''
    finally:
        chat.services.close()
