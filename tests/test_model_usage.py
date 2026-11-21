import pytest
from httpx import AsyncClient, MockTransport, Response
from filabrain import model


@pytest.mark.asyncio
@pytest.mark.parametrize('fingerprint,window', [('fp_ollama', 4096), ('cloud-provider', None)])
async def test_completion_reports_usage_and_only_ollama_active_limit(monkeypatch, fingerprint, window):
    paths = []
    def upstream(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/chat/completions'):
            return Response(200, json={'model': 'qwen3:8b', 'system_fingerprint': fingerprint,
                'choices': [{'message': {'content': 'Answer'}}],
                'usage': {'prompt_tokens': 120, 'completion_tokens': 40, 'total_tokens': 160}})
        assert request.url.path == '/api/ps'
        return Response(200, json={'models': [
            {'name': 'other-model', 'context_length': 100000},
            {'name': 'qwen3:8b', 'context_length': 4096},
        ]})
    monkeypatch.setattr(model.httpx, 'AsyncClient', lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
    usage = {}
    assert await model.request_completion('http://ollama/v1', 'qwen3:8b', '', [], usage=usage) == 'Answer'
    assert usage == {'prompt_tokens': 120, 'completion_tokens': 40, **({'context_window': window} if window else {})}
    assert len(paths) == (2 if window else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('reported', [None, [], {'prompt_tokens': True}, {'prompt_tokens': -1}, {'prompt_tokens': '100'}])
async def test_missing_or_invalid_usage_does_not_break_the_answer(monkeypatch, reported):
    def upstream(_request):
        return Response(200, json={'choices': [{'message': {'content': 'Answer'}}], 'usage': reported})
    monkeypatch.setattr(model.httpx, 'AsyncClient', lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
    usage = {}
    assert await model.request_completion('http://model/v1', 'test', '', [], usage=usage) == 'Answer'
    assert usage == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [Response(404), Response(200, content='not-json'), Response(200, json={'models': None})])
async def test_unavailable_ollama_metadata_keeps_token_counts(monkeypatch, metadata):
    def upstream(request):
        if request.url.path == '/api/ps': return metadata
        return Response(200, json={'system_fingerprint': 'fp_ollama', 'model': 'test',
            'choices': [{'message': {'content': 'Answer'}}], 'usage': {'prompt_tokens': 100}})
    monkeypatch.setattr(model.httpx, 'AsyncClient', lambda **kwargs: AsyncClient(transport=MockTransport(upstream), **kwargs))
    usage = {}
    assert await model.request_completion('http://model/v1', 'test', '', [], usage=usage) == 'Answer'
    assert usage == {'prompt_tokens': 100}
