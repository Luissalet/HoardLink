"""Legacy extension methods keep their call shape and native chat deadlines."""
import asyncio

import pytest

from hoard_link import Link
from hoard_link.errors import BackendError
from tests.test_chat_timeouts import delayed_backend, tcp_link
from tests.hub.test_chat_timeouts import private_hub


class LegacyPost(Link):
    async def _post_chat(self, provider, url, payload, level):
        self.entered.append(payload['messages'][-1]['content'])
        return await super()._post_chat(provider, url, payload, level)

    async def _post_json(self, *args, **kwargs):
        self.http_timeouts.append(kwargs['timeout'])
        return await super()._post_json(*args, **kwargs)


class LegacyDialects(LegacyPost):
    async def _chat_openai(self, res, messages, images, max_tokens, temperature,
                           response_format, level=None, chat_template_kwargs=None):
        return await super()._chat_openai(res, messages, images, max_tokens, temperature,
                                          response_format, level, chat_template_kwargs)

    async def _chat_ollama(self, res, messages, images, max_tokens, temperature,
                           response_format, level=None):
        return await super()._chat_ollama(res, messages, images, max_tokens, temperature,
                                          response_format, level)


def legacy(backend, api='openai', cls=LegacyPost):
    link=tcp_link(backend,api)
    link.__class__=cls
    link.entered=[]
    link.http_timeouts=[]
    return link


@pytest.mark.asyncio
@pytest.mark.parametrize('cls',[LegacyPost,LegacyDialects])
@pytest.mark.parametrize('api',['openai','ollama'])
@pytest.mark.parametrize('timeout',[None,.9])
async def test_legacy_methods_work_without_new_positional_arguments(delayed_backend,monkeypatch,cls,api,timeout):
    monkeypatch.setattr('hoard_link.link._reasoning.timeout_for',lambda *_:.3)
    delayed_backend['delay']=.03 if timeout is None else .421
    link=legacy(delayed_backend,api,cls)
    try:
        result=await link.chat([{'role':'user','content':'normal'}],timeout=timeout)
        assert result.text=='ok' and result.finish_reason=='stop'
        assert link.entered==['normal']
        assert len(link.http_timeouts)==1
        if timeout is None:assert link.http_timeouts==[.3]
        else:assert .3<link.http_timeouts[0]<=timeout
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==1


@pytest.mark.asyncio
async def test_legacy_super_omitted_timeout_keeps_backend_default(delayed_backend,monkeypatch):
    monkeypatch.setattr('hoard_link.link._reasoning.timeout_for',lambda *_:.1)
    link=legacy(delayed_backend)
    try:
        with pytest.raises(BackendError,match='ReadTimeout'):
            await link.chat([{'role':'user','content':'normal'}])
        assert link.http_timeouts==[.1]
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==1


@pytest.mark.asyncio
async def test_legacy_explicit_deadline_still_cancels_slow_response(delayed_backend):
    link=legacy(delayed_backend)
    try:
        with pytest.raises((TimeoutError,BackendError)):
            await link.chat([{'role':'user','content':'normal'}],timeout=.1)
        assert 0<link.http_timeouts[0]<=.1
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==1
    assert delayed_backend['closed'].wait(1)


@pytest.mark.asyncio
async def test_legacy_negotiation_uses_remaining_whole_budget(delayed_backend):
    delayed_backend['delay']=.12
    link=legacy(delayed_backend)
    try:
        with pytest.raises((TimeoutError,BackendError)):
            await link.chat([{'role':'user','content':'negotiate'}],effort='high',timeout=.2)
        assert link.entered==['negotiate']
        assert len(link.http_timeouts)==2
        assert 0<link.http_timeouts[1]<link.http_timeouts[0]<=.2
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==2  # Only the existing 400 negotiation.


@pytest.mark.asyncio
async def test_context_resets_after_nested_error_and_next_top_level_call(delayed_backend,monkeypatch):
    monkeypatch.setattr('hoard_link.link._reasoning.timeout_for',lambda *_:.3)
    class Nested(LegacyPost):
        async def _post_chat(self,provider,url,payload,level):
            if payload['messages'][-1]['content']=='outer':
                with pytest.raises((TimeoutError,BackendError)):
                    await self.chat([{'role':'user','content':'inner'}],timeout=.05)
            return await super()._post_chat(provider,url,payload,level)
    delayed_backend['delay']=.11
    link=legacy(delayed_backend,cls=Nested)
    try:
        assert (await link.chat([{'role':'user','content':'outer'}],timeout=.9)).text=='ok'
        assert 0<link.http_timeouts[0]<=.05
        assert .3<link.http_timeouts[1]<.9
        assert (await link.chat([{'role':'user','content':'top-level'}])).text=='ok'
        assert link.http_timeouts[2]==.3
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==3


@pytest.mark.asyncio
async def test_same_link_concurrent_calls_do_not_share_budget(delayed_backend):
    delayed_backend['delay']=.22
    link=legacy(delayed_backend)
    try:
        a,b=await asyncio.gather(link.chat([{'role':'user','content':'short'}],timeout=.08),
                                 link.chat([{'role':'user','content':'long'}],timeout=.9),return_exceptions=True)
        assert isinstance(a,(TimeoutError,BackendError))
        assert b.text=='ok'
        assert len(link.http_timeouts)==2
        assert any(0<x<=.08 for x in link.http_timeouts)
        assert any(.3<x<=.9 for x in link.http_timeouts)
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==2


@pytest.mark.asyncio
async def test_cancel_does_not_leak_deadline_into_next_chat(delayed_backend,monkeypatch):
    monkeypatch.setattr('hoard_link.link._reasoning.timeout_for',lambda *_:.3)
    delayed_backend['delay']=.22
    link=legacy(delayed_backend)
    try:
        task=asyncio.create_task(link.chat([{'role':'user','content':'cancel'}],timeout=.9))
        while not delayed_backend['requests'] and not task.done():await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert (await link.chat([{'role':'user','content':'after'}])).text=='ok'
        assert link.http_timeouts[-1]==.3
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==2


@pytest.mark.asyncio
async def test_whole_deadline_applies_to_override_work_before_super(delayed_backend):
    class SlowOverride(LegacyPost):
        async def _post_chat(self,provider,url,payload,level):
            await asyncio.sleep(.2)
            return await super()._post_chat(provider,url,payload,level)
    link=legacy(delayed_backend,cls=SlowOverride)
    try:
        with pytest.raises(TimeoutError):await link.chat([{'role':'user','content':'normal'}],timeout=.05)
        assert link.http_timeouts==[]
    finally:await link.aclose()
    assert delayed_backend['requests']==[]


@pytest.mark.asyncio
async def test_typeerror_inside_override_is_never_caught_or_replayed(delayed_backend):
    class Reject(LegacyPost):
        async def _post_chat(self,provider,url,payload,level):
            self.entered.append('once')
            raise TypeError('intentional error inside extension')
    link=legacy(delayed_backend,cls=Reject)
    try:
        with pytest.raises(TypeError,match='intentional error inside extension'):
            await link.chat([{'role':'user','content':'normal'}],timeout=.9)
        assert link.entered==['once']
    finally:await link.aclose()
    assert delayed_backend['requests']==[]


@pytest.mark.asyncio
async def test_external_timeout_in_same_task_resets_chat_context(delayed_backend,monkeypatch):
    monkeypatch.setattr('hoard_link.link._reasoning.timeout_for',lambda *_:.3)
    delayed_backend['delay']=.11
    link=legacy(delayed_backend)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.05):
                await link.chat([{'role':'user','content':'cancel'}],timeout=.9)
        assert (await link.chat([{'role':'user','content':'after'}])).text=='ok'
        assert link.http_timeouts[-1]==.3
    finally:await link.aclose()
    assert len(delayed_backend['requests'])==2


def test_native_family_route_reaches_legacy_override_with_remaining_budget(private_hub,delayed_backend):
    from hoard_link import family
    hub,_=private_hub
    hub.link._factory=lambda _:legacy(delayed_backend)
    result=family.chat([{'role':'user','content':'normal'}],timeout=1)
    assert result['ok'] and result['text']=='ok' and result['finish_reason']=='stop'
    assert len(delayed_backend['requests'])==1
    assert len(hub.link._links)==1
    link=next(iter(hub.link._links.values()))
    assert link.entered==['normal']
    assert .3<link.http_timeouts[0]<=1
