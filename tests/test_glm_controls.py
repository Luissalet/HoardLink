"""Exact wire contracts: served GLM identity + native HTTP capability, never GPU inference."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from hoard_link import Link, LinkConfig, reasoning
from hoard_link.types import Resolution

MODEL = 'nvidia/GLM-5.3-Flash-NVFP4'
SCHEMA = {'paths': {'/v1/chat/completions': {'post': {'requestBody': {'content': {
    'application/json': {'schema': {'$ref': '#/components/schemas/ChatCompletionRequest'}}}}}}},
    'components': {'schemas': {'ChatCompletionRequest': {'properties': {
        'thinking_token_budget': {'type':'integer'}, 'chat_template_kwargs': {'type':'object'}}}}}}
MESSAGES = [{'role':'user','content':'fixture'}]


@pytest.mark.parametrize('field,invalid', [
    ('thinking_token_budget', {'type':'string'}),
    ('chat_template_kwargs', {'type':'string'}),
    (None, ['thinking_token_budget','chat_template_kwargs']),
])
def test_incompatible_native_schema_is_unverified(field, invalid):
    schema = copy.deepcopy(SCHEMA)
    request = schema['components']['schemas']['ChatCompletionRequest']
    if field is None:
        request['properties'] = invalid
    else:
        request['properties'][field] = invalid
    assert reasoning.native_glm_controls(MODEL, {'data':[{'id':MODEL}]}, schema) is None


def test_native_nullable_schema_types_are_supported():
    schema = copy.deepcopy(SCHEMA)
    fields = schema['components']['schemas']['ChatCompletionRequest']['properties']
    fields['thinking_token_budget'] = {'anyOf':[{'type':'integer'},{'type':'null'}]}
    fields['chat_template_kwargs'] = {'anyOf':[{'type':'object'},{'type':'null'}]}
    assert reasoning.native_glm_controls(MODEL, {'data':[{'id':MODEL}]}, schema)['budget_field'] == 'thinking_token_budget'


def fixture_link(*, model=MODEL, served=None, schema=None, refuse=False):
    requests = []
    posts = []
    def handler(request):
        requests.append(request.url.path)
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data':[{'id':served or model}]})
        if request.url.path == '/openapi.json':
            return httpx.Response(200, json=SCHEMA if schema is None else schema)
        assert request.url.path == '/v1/chat/completions' and request.method == 'POST'
        payload = json.loads(request.read())
        posts.append(payload)
        if refuse and len(posts) == 1:
            return httpx.Response(400, text='unknown thinking_token_budget')
        choice = {'message':{'content':'ok'},'finish_reason':'stop'}
        if payload['stream']:
            return httpx.Response(200, content=('data: '+json.dumps({'choices':[
                {'delta':{'content':'ok'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode())
        return httpx.Response(200, json={'choices':[choice]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    link = Link(LinkConfig.load(None, env={}, app='controls-test'), client=client)
    async def resolve(*_):
        return Resolution('llm','unknown','http://fixture.invalid/v1/chat/completions',model,'openai','resolved','fixture')
    @asynccontextmanager
    async def lease(*_):
        yield
    link.resolve, link._load_lease = resolve, lease
    return link, client, requests, posts


@pytest.mark.parametrize('mode', ['blocking','streaming'])
@pytest.mark.parametrize('effort,effective,budget', [
    (None,None,None), ('off','low',None), ('low','low',1024),
    ('medium','low',4096), ('high','high',8192), ('max','max',16384)])
@pytest.mark.asyncio
async def test_glm_effort_wire_and_metadata(mode, effort, effective, budget):
    link, client, requests, posts = fixture_link()
    try:
        kwargs = dict(effort=effort, max_tokens=100, chat_template_kwargs={'clear_thinking':False})
        if mode == 'blocking':
            result = await link.chat(MESSAGES, **kwargs)
            meta = result.reasoning_controls
        else:
            events = [e async for e in link.stream_chat(MESSAGES, **kwargs)]
            meta = events[-1]['reasoning_controls']
        assert len(posts) == 1 and requests.count('/openapi.json') == 1
        body = posts[0]
        assert body.get('reasoning_effort') == effective
        assert body.get('thinking_token_budget') == budget
        assert 'thinking_budget_tokens' not in body and 'reasoning_budget' not in body
        assert body['max_tokens'] == 100 + (budget or 0)
        assert body['chat_template_kwargs'] == {'clear_thinking':False,'enable_thinking':True}
        assert meta['requested_effort'] == effort
        assert meta['effective_effort'] == (effective or 'server_default')
        assert meta['thinking_off_behavior'] == 'low_effort' and meta['thinking_can_disable'] is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_default_clear_thinking_and_cached_singleflight():
    link, client, requests, posts = fixture_link()
    try:
        await asyncio.gather(*(link.chat(MESSAGES, effort='high') for _ in range(6)))
        assert requests.count('/openapi.json') == requests.count('/v1/models') == 1
        assert all(p['chat_template_kwargs'] == {'enable_thinking':True,'clear_thinking':True} for p in posts)
        link._now = lambda: 10**12
        await link.chat(MESSAGES)
        assert requests.count('/openapi.json') == 2
        assert 'thinking_token_budget' not in posts[-1]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('schema,served', [({},MODEL), (SCHEMA,'other-model')])
async def test_unverified_capability_keeps_legacy_payload(schema, served):
    link, client, requests, posts = fixture_link(schema=schema, served=served)
    try:
        result = await link.chat(MESSAGES, effort='max')
        assert result.reasoning_controls is None
        assert posts[0]['reasoning_effort'] == 'high'
        assert posts[0]['thinking_budget_tokens'] == posts[0]['reasoning_budget'] == 16384
        assert 'thinking_token_budget' not in posts[0]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['Qwen/Qwen3', 'zai-org/GLM-5.3', 'foo/not-GLM-5.3-Flash'])
async def test_other_models_do_not_probe_or_change(model):
    link, client, requests, posts = fixture_link(model=model)
    try:
        result = await link.chat(MESSAGES, effort='off')
        assert requests == ['/v1/chat/completions']
        assert posts[0]['chat_template_kwargs'] == {'enable_thinking':False}
        assert result.reasoning_controls is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('effort', [None,'medium'])
async def test_400_fallback_strips_native_fields_and_reports_server_default(effort):
    link, client, _, posts = fixture_link(refuse=True)
    try:
        result = await link.chat(MESSAGES, effort=effort, chat_template_kwargs={'custom':1})
        assert len(posts) == 2
        assert posts[1]['chat_template_kwargs'] == {'custom':1}
        assert not any(key in posts[1] for key in reasoning.REASONING_KEYS)
        assert result.reasoning_controls['applied'] is False
        assert result.reasoning_controls['effective_effort'] == 'server_default'
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_streaming_400_is_not_replayed():
    link, client, _, posts = fixture_link(refuse=True)
    from hoard_link.errors import BackendError
    try:
        with pytest.raises(BackendError):
            async for _ in link.stream_chat(MESSAGES, effort='max'):
                pass
        assert len(posts) == 1
    finally:
        await client.aclose()


def test_native_budget_presence_and_unique_effort_strip():
    controls = reasoning.native_glm_controls(MODEL, {'data':[{'id':MODEL}]}, SCHEMA)
    for budget in (0,-1,128):
        payload = {'thinking_token_budget':budget,'reasoning_budget':999,
                   'chat_template_kwargs':{'reasoning_effort':'high','clear_thinking':False,'enable_thinking':False}}
        reasoning.apply_openai(payload, 'high', controls)
        assert payload['thinking_token_budget'] == budget
        assert payload['reasoning_effort'] == 'low'
        assert payload['chat_template_kwargs'] == {'clear_thinking':False,'enable_thinking':True}
        assert reasoning.strip(payload) and not payload


def test_unrelated_schema_field_or_malformed_metadata_does_not_verify():
    unrelated = copy.deepcopy(SCHEMA)
    unrelated['paths']['/v1/chat/completions']['post']['requestBody']['content']['application/json']['schema'] = {'properties':{}}
    assert reasoning.native_glm_controls(MODEL, {'data':[{'id':MODEL}]}, unrelated) is None
    assert reasoning.native_glm_controls(MODEL, {'data':None}, SCHEMA) is None
