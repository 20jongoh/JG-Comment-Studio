"""v0.1.2 regression tests: synthetic inputs and mocked provider responses only.

No real Google API call, API key, video analysis, browser login, or comment post
is used by this file. The original 68 tests remain unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import httpx
import pytest

from app.drafting import (
    _EVIDENCE_TOKEN, _GEMINI_SCHEMA, _InvalidDraft, _SCHEMA,
    _evidence_options, _gemini_request_schema, _input_material,
    _resolve_gemini_draft, _validate_draft, make_draft,
)
from app.main import create_app
from app.youtube import CHANNELS, ServiceError

SENTINEL = 'MOCK_KEY_NEVER_DISPLAY'


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def stop(*_args, **_kwargs):
        raise AssertionError('This test must not make external HTTP requests.')
    monkeypatch.setattr(httpx, 'post', stop)
    monkeypatch.setattr(httpx, 'get', stop)


@pytest.fixture
def context():
    # All text below was written as test data, not retrieved from a video.
    return {
        'video': {
            'id': 'abcdefghijk', 'source': 'youtube',
            'channel_id': CHANNELS[0]['id'], 'channel_title': '가상 검사용 채널',
            'title': '서운함을 대화로 전하는 방법',
            'description': '서로 기대하는 대화의 속도를 돌아보세요.',
            'url': 'https://www.youtube.com/watch?v=abcdefghijk',
        },
        'transcript': '답장  속도보다 약속을 지키는 태도\n서로의 기대를 차분히 말해 보세요',
        'transcript_source': 'youtube_captions',
        'transcript_segments': [
            {'text': '답장  속도보다 약속을 지키는 태도', 'start': 12.5},
            {'text': '서로의 기대를 차분히 말해 보세요', 'start': 21.0},
        ],
        'comments': [{'text': '시청자의 의견은 영상의 원문이 아닙니다', 'like_count': 20}],
        'warnings': [],
    }


def material_and_options(context):
    text, _ = _input_material(context)
    material = json.loads(text)
    return material, _evidence_options(material)


def selected_value(option):
    return {
        'evidence_id': option['id'],
        'worry': '관계에서 서운한 마음과 서로의 기대를 어떻게 대화로 전할지 고민합니다.',
        'comment_template': '[과제 시연] ‘[[EVIDENCE]]’라는 표현을 바탕으로 대화를 생각했어요. 운명원 연애 사주를 질문의 소재로 삼는다면 무엇부터 물어볼까요?',
    }


def gemini_response(value=None, *, finish='STOP', blocked=None, thought=False):
    if blocked:
        return httpx.Response(200, json={'promptFeedback': {'blockReason': blocked}})
    parts = []
    if thought:
        parts.append({'thought': True, 'text': 'This must never enter the returned draft.'})
    if value is not None:
        parts.append({'text': json.dumps(value, ensure_ascii=False)})
    candidate = {'content': {'parts': parts}}
    if finish is not None:
        candidate['finishReason'] = finish
    return httpx.Response(200, json={'candidates': [candidate]})


def setup_response(monkeypatch, context, modify=None, source='transcript'):
    _, options = material_and_options(context)
    option = next(item for item in options if item['source'] == source)
    value = selected_value(option)
    if modify:
        modify(value)
    post = Mock(return_value=gemini_response(value))
    monkeypatch.setattr(httpx, 'post', post)
    return option, value, post


def test_catalogue_is_literal_and_has_unique_request_local_ids(context):
    material, options = material_and_options(context)
    assert options and len(options) <= 64
    assert len({option['id'] for option in options}) == len(options)
    assert {option['source'] for option in options} == {'transcript', 'title', 'description'}
    for item in options:
        raw = material['source_text'][item['source']]
        assert raw[item['start']:item['end']] == item['quote']
        assert item['quote'] in raw
        assert 4 <= len(item['quote']) <= 28
        assert item['char_count'] == len(item['quote'])
    assert all(item['quote'] not in context['comments'][0]['text'] for item in options)


def test_double_spaces_are_preserved_not_normalized(monkeypatch, context):
    option, _, post = setup_response(monkeypatch, context)
    assert '  ' in option['quote']
    result = make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert result['evidence_quote'] == option['quote']
    assert option['quote'] in result['comment']
    assert result['evidence_timestamp'] == 12.5
    assert _EVIDENCE_TOKEN not in result['comment']
    assert result['grounding_patch_version'] == '0.1.2'
    assert result['source_mode'] == 'ai'
    assert post.call_count == 1


def test_previous_mismatch_still_fails_the_exact_source_validator(context):
    quote = '답장 속도보다 약속을 지키는 태도'  # deliberately collapses a space
    draft = {
        'worry': '상대의 기대를 무엇으로 판단할지 고민합니다.',
        'evidence_quote': quote, 'evidence_source': 'transcript', 'evidence_timestamp': None,
        'comment': '[과제 시연] ‘' + quote + '’를 살펴보며 운명원 연애 사주를 대화의 질문 소재로 생각하는 예시입니다.',
    }
    with pytest.raises(_InvalidDraft) as error:
        _validate_draft(draft, context)
    assert any('정확한 부분 문자열' in message for message in error.value.reasons)


@pytest.mark.parametrize('source', ['title', 'description'])
def test_title_and_description_are_never_presented_as_caption_quotes(monkeypatch, context, source):
    context['transcript'] = ''
    context['transcript_source'] = 'none'
    context['transcript_segments'] = []
    option, _, _ = setup_response(monkeypatch, context, source=source)
    result = make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert result['evidence_source'] == source
    assert result['evidence_quote'] == option['quote']
    assert result['evidence_timestamp'] is None
    assert any('직접 인용 출처' in warning for warning in result['warnings'])


def test_manual_text_does_not_acquire_a_measured_timestamp(monkeypatch, context):
    context['transcript_source'] = 'manual'
    setup_response(monkeypatch, context)
    result = make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert result['evidence_timestamp'] is None
    assert any('사용자 제공' in item['detail'] for item in result['checks'])


@pytest.mark.parametrize('case', [
    'unknown_id', 'id_not_string', 'missing_field', 'extra_source', 'extra_quote',
    'extra_timestamp', 'no_token', 'two_tokens', 'token_in_worry', 'short_comment',
    'long_comment', 'missing_disclosure', 'brand_twice', 'url', 'hashtag',
    'control_character', 'experience_claim', 'empty_worry', 'long_worry',
])
def test_invalid_ai_output_is_never_turned_into_success(monkeypatch, context, case):
    def alter(value):
        if case == 'unknown_id': value['evidence_id'] = 'E999'
        elif case == 'id_not_string': value['evidence_id'] = 1
        elif case == 'missing_field': value.pop('worry')
        elif case == 'extra_source': value['evidence_source'] = 'transcript'
        elif case == 'extra_quote': value['evidence_quote'] = '없는 원문 인용'
        elif case == 'extra_timestamp': value['evidence_timestamp'] = 999
        elif case == 'no_token': value['comment_template'] = value['comment_template'].replace(_EVIDENCE_TOKEN, '직접 꾸며 쓴 인용')
        elif case == 'two_tokens': value['comment_template'] += _EVIDENCE_TOKEN
        elif case == 'token_in_worry': value['worry'] += _EVIDENCE_TOKEN
        elif case == 'short_comment': value['comment_template'] = '[과제 시연] 운명원 [[EVIDENCE]]'
        elif case == 'long_comment': value['comment_template'] += '가' * 130
        elif case == 'missing_disclosure': value['comment_template'] = value['comment_template'].replace('[과제 시연]', '')
        elif case == 'brand_twice': value['comment_template'] += ' 운명원'
        elif case == 'url': value['comment_template'] += ' https://x.test'
        elif case == 'hashtag': value['comment_template'] += ' #링크'
        elif case == 'control_character': value['comment_template'] += '\u200b'
        elif case == 'experience_claim': value['comment_template'] += ' 상담을 받았어요'
        elif case == 'empty_worry': value['worry'] = ''
        elif case == 'long_worry': value['worry'] = '가' * 181
    _, _, post = setup_response(monkeypatch, context, modify=alter)
    with pytest.raises(ServiceError) as error:
        make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert error.value.code == 'draft_validation_failed'
    assert post.call_count == 2
    assert SENTINEL not in str(error.value)
    assert '..' not in str(error.value)


def test_valid_repair_retains_all_checks(monkeypatch, context):
    option, good, _ = setup_response(monkeypatch, context)
    bad = {**good, 'evidence_id': 'invented'}
    post = Mock(side_effect=[gemini_response(bad), gemini_response(good)])
    monkeypatch.setattr(httpx, 'post', post)
    result = make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert post.call_count == 2
    assert result['evidence_quote'] == option['quote']
    assert all(check['ok'] for check in result['checks'])
    assert any('한 번 수정' in warning for warning in result['warnings'])
    retry = post.call_args_list[1].kwargs['json']['contents'][0]['parts'][0]['text']
    assert 'validation_errors' in retry and 'evidence_options' in retry


@pytest.mark.parametrize('field', ['start', 'end', 'source', 'quote'])
def test_server_rechecks_catalogue_offsets_and_source(context, field):
    _, options = material_and_options(context)
    changed = deepcopy(options)
    chosen = changed[0]
    if field == 'start': chosen[field] += 1
    elif field == 'end': chosen[field] -= 1
    elif field == 'source': chosen[field] = 'title'
    else: chosen[field] = '원문에 없는 새로운 문구'
    with pytest.raises(_InvalidDraft):
        _resolve_gemini_draft(selected_value(chosen), changed, context)


def test_catalogue_uses_only_clipped_material_and_excludes_comments(context):
    context['transcript'] = '가나다라마바사 아자차카타파하\n' * 2000 + '맨끝의보이지않는원문'
    context['video']['title'] = '제목문구' * 300 + '숨긴제목접미부'
    context['video']['description'] = '설명문구' * 2000 + '숨긴설명접미부'
    material, options = material_and_options(context)
    assert len(options) <= 64
    assert all(option['quote'] in material['source_text'][option['source']] for option in options)
    assert '맨끝의보이지않는원문' not in json.dumps(options, ensure_ascii=False)
    assert '숨긴제목접미부' not in json.dumps(options, ensure_ascii=False)
    assert '숨긴설명접미부' not in json.dumps(options, ensure_ascii=False)
    assert all(option['source'] != 'audience_comments' for option in options)


def test_no_source_fails_before_billable_generation(monkeypatch, context):
    context['transcript'] = ''
    context['video']['title'] = ''
    context['video']['description'] = ''
    post = Mock()
    monkeypatch.setattr(httpx, 'post', post)
    with pytest.raises(ServiceError) as error:
        make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert error.value.code == 'evidence_missing'
    assert post.call_count == 0


def test_schema_constrains_ids_and_preserves_previous_token_fix(monkeypatch, context):
    _, _, post = setup_response(monkeypatch, context)
    make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    request = post.call_args
    body = request.kwargs['json']
    config = body['generationConfig']
    material = json.loads(body['contents'][0]['parts'][0]['text'])
    enum = config['responseFormat']['text']['schema']['properties']['evidence_id']['enum']
    assert enum == [option['id'] for option in material['evidence_options']]
    assert config['maxOutputTokens'] == 8192
    assert config['thinkingConfig']['thinkingLevel'] == 'low'
    assert request.kwargs['timeout'].read == 60.0
    assert request.kwargs['headers']['x-goog-api-key'] == SENTINEL
    assert SENTINEL not in json.dumps(body)
    assert set(config['responseFormat']['text']['schema']['required']) == {'evidence_id', 'worry', 'comment_template'}
    assert _SCHEMA['required'] == ['worry', 'evidence_quote', 'evidence_source', 'evidence_timestamp', 'comment']


def test_schema_does_not_leak_between_requests():
    first = _gemini_request_schema(['E001'])
    second = _gemini_request_schema(['E010', 'E020'])
    assert first['properties']['evidence_id']['enum'] == ['E001']
    assert second['properties']['evidence_id']['enum'] == ['E010', 'E020']
    assert 'enum' not in _GEMINI_SCHEMA['properties']['evidence_id']


@pytest.mark.parametrize('reason', ['MAX_TOKENS', 'SAFETY', 'RECITATION', None, 'UNKNOWN_REASON'])
def test_unfinished_or_restricted_output_is_not_accepted(monkeypatch, context, reason):
    _, options = material_and_options(context)
    post = Mock(return_value=gemini_response(selected_value(options[0]), finish=reason))
    monkeypatch.setattr(httpx, 'post', post)
    with pytest.raises(ServiceError) as error:
        make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert error.value.code == 'ai_incomplete'
    assert post.call_count == 1


def test_thinking_text_is_not_included_as_comment(monkeypatch, context):
    _, options = material_and_options(context)
    monkeypatch.setattr(httpx, 'post', Mock(return_value=gemini_response(selected_value(options[0]), thought=True)))
    result = make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert 'never enter' not in json.dumps(result)


@pytest.mark.parametrize('status,code', [(401, 'ai_authorization'), (429, 'ai_quota'), (404, 'ai_configuration')])
def test_provider_errors_do_not_echo_secret_diagnostics(monkeypatch, context, status, code):
    post = Mock(return_value=httpx.Response(status, json={'error': {'message': SENTINEL}}))
    monkeypatch.setattr(httpx, 'post', post)
    with pytest.raises(ServiceError) as error:
        make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert error.value.code == code
    assert SENTINEL not in str(error.value)
    assert post.call_count == 1


def test_non_json_response_stays_a_failed_draft(monkeypatch, context):
    response = httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'not JSON'}]}}]})
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, 'post', post)
    with pytest.raises(ServiceError) as error:
        make_draft(context, 'gemini', SENTINEL, 'gemini-3-test')
    assert error.value.code == 'draft_validation_failed'
    assert post.call_count == 2


@pytest.mark.parametrize('fail', [False, True])
def test_live_analyze_route_uses_grounded_output_or_saves_nothing(monkeypatch, tmp_path, context, fail):
    class FakeYouTube:
        def __init__(self, _key): pass
        def resolve_video(self, _video_id): return deepcopy(context['video'])
        def context(self, video, manual):
            result = deepcopy(context)
            result['video'] = video
            return result
    monkeypatch.setattr('app.main.YouTubeService', FakeYouTube)
    _, _, post = setup_response(monkeypatch, context, modify=(lambda value: value.update(evidence_id='E999')) if fail else None)
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        client.headers['X-CSRF-Token'] = client.get('/api/bootstrap').json()['session_token']
        assert client.post('/api/settings', json={'ai_provider': 'gemini', 'ai_api_key': SENTINEL, 'youtube_api_key': 'MOCK_YOUTUBE'}).status_code == 200
        response = client.post('/api/analyze', json={'video_id': 'abcdefghijk', 'mode': 'live'})
        with app.state.store.connect() as connection:
            saved = connection.execute('SELECT COUNT(*) FROM analyses').fetchone()[0]
        assert response.status_code == (400 if fail else 200)
        assert saved == (0 if fail else 1)
        assert app.state.store.history() == []
        assert SENTINEL not in response.text
        if not fail:
            draft = response.json()
            assert draft['grounding_patch_version'] == '0.1.2'
            assert draft['evidence_quote'] in draft['comment']
            assert all(check['ok'] for check in draft['checks'])
            assert client.post('/api/validate', json={'analysis_id': draft['analysis_id'], 'comment': draft['comment']}).json()['valid']
            # The external-channel posting prohibition is unchanged.
            assert client.post('/api/publish', json={'analysis_id': draft['analysis_id'], 'comment': draft['comment'], 'confirmed': True}).status_code == 400
    assert post.call_count == (2 if fail else 1)
