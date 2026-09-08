import json

import pytest

from tcg.document_workspace import DocumentWorkspace
from tcg.schemas import DomainError
from tcg.storage import Store


def serialized_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def add_source(store, chat, source_id, name, role, paragraphs):
    return store.add_source(
        chat['id'], name, role, '\n'.join(paragraphs),
        [{'text': text, 'location': f'paragraph {index}'} for index, text in enumerate(paragraphs, 1)],
        source_id=source_id,
    )


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Workspace')
    add_source(store, chat, 'src-a', 'Requirements', 'primary', ['alpha', 'beta'])
    add_source(store, chat, 'src-b', 'Changes', 'change', ['gamma'])
    try:
        yield store, DocumentWorkspace(store), project, chat
    finally:
        store.close()


def test_evidence_is_stable_scoped_and_cached_without_mutation_leaks(workspace, monkeypatch):
    store, documents, project, _ = workspace
    other_chat = store.create_chat(project['id'], 'Other')
    add_source(store, other_chat, 'src-other', 'Secret', 'primary', ['outside scope'])
    store.deactivate_source('src-b')

    calls = []
    original = store.evidence

    def counted(source_ids):
        calls.append(list(source_ids))
        return original(source_ids)

    monkeypatch.setattr(store, 'evidence', counted)
    first = documents.evidence(['src-b', 'src-a'])
    assert [item['id'] for item in first] == ['src-b#P1', 'src-a#P1', 'src-a#P2']
    assert all(item['source_id'] != 'src-other' for item in first)
    first[0]['text'] = 'caller mutation'
    assert documents.evidence(['src-b'])[0]['text'] == 'gamma'
    assert calls == [['src-b', 'src-a']]

    assert [item['id'] for item in documents.evidence(['src-other'])] == ['src-other#P1']
    assert calls == [['src-b', 'src-a'], ['src-other']]


def test_store_evidence_lists_each_chat_only_once_and_preserves_source_order(workspace, monkeypatch):
    store, _, _, _ = workspace
    calls = []
    original = store.list

    def counted(kind, project_id=None, chat_id=None):
        if kind == 'chunk':
            calls.append(chat_id)
        return original(kind, project_id=project_id, chat_id=chat_id)

    monkeypatch.setattr(store, 'list', counted)
    evidence = store.evidence(['src-b', 'src-a'])
    assert [item['id'] for item in evidence] == ['src-b#P1', 'src-a#P1', 'src-a#P2']
    assert calls == [store.get('source', 'src-a')['chat_id']]


def test_catalog_is_metadata_only_and_hard_bounded(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Large catalog')
    source_ids = []
    for index in range(140):
        source_id = f'src-{index:03d}'
        source_ids.append(source_id)
        add_source(store, chat, source_id, 'N' * 90 + str(index), 'primary', [f'private body {index}'])
    try:
        result = DocumentWorkspace(store).catalog(source_ids)
        assert result['total_sources'] == 140
        assert result['total_chunks'] == 140
        assert result['omitted_count'] == 140 - len(result['sources']) > 0
        assert result['truncated'] is True
        assert serialized_size(result) < 6000
        assert all(set(source) == {'id', 'name', 'role', 'chunk_count', 'first_ref', 'last_ref'} for source in result['sources'])
        assert 'private body' not in json.dumps(result, ensure_ascii=False)
    finally:
        store.close()


def test_repeated_catalog_uses_cached_body_free_source_metadata(workspace, monkeypatch):
    store, documents, _, _ = workspace
    original = store.get
    source_gets = []

    def counted(kind, object_id):
        if kind == 'source':
            source_gets.append(object_id)
        return original(kind, object_id)

    monkeypatch.setattr(store, 'get', counted)
    first = documents.catalog(['src-a', 'src-b'])
    initial_gets = len(source_gets)
    assert initial_gets > 0
    assert documents.catalog(['src-a', 'src-b']) == first
    assert len(source_gets) == initial_gets
    assert all('_text' not in metadata for metadata in documents._metadata_by_source.values())


def test_search_ranks_english_and_chinese_finds_tail_and_has_no_fallback(workspace):
    store, documents, _, chat = workspace
    tail = 'x' * 700 + ' tailneedle authorization rule'
    add_source(store, chat, 'src-search', 'Searchable', 'supplement', [
        'The account profile can be viewed.',
        '支付失败时必须保留订单并显示重试按钮。',
        tail,
        'Authorization authorization appears twice.',
    ])

    english = documents.search(['src-search'], 'authorization')
    assert [match['id'] for match in english['matches']] == ['src-search#P4', 'src-search#P3']
    assert english['total_matches'] == 2
    assert all(len(match['text']) <= 400 for match in english['matches'])

    chinese = documents.search(['src-search'], '支付失败')
    assert [match['id'] for match in chinese['matches']] == ['src-search#P2']
    tail_result = documents.search(['src-search'], 'tailneedle')
    assert 'tailneedle' in tail_result['matches'][0]['text']
    assert documents.search(['src-search'], 'not-present-anywhere')['matches'] == []
    assert documents.search(['src-search'], 'not-present-anywhere')['total_matches'] == 0


def test_search_is_bounded_and_rejects_invalid_queries(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Many matches')
    add_source(store, chat, 'src-many', 'Many', 'primary', ['needle ' + ('x' * 600) for _ in range(100)])
    try:
        documents = DocumentWorkspace(store)
        result = documents.search(['src-many'], 'needle')
        assert result['total_matches'] == 100
        assert result['truncated'] is True
        assert serialized_size(result) < 6000
        with pytest.raises(DomainError, match='查询'):
            documents.search(['src-many'], '   ')
        with pytest.raises(DomainError, match='500'):
            documents.search(['src-many'], 'q' * 501)
    finally:
        store.close()


def test_read_is_all_or_nothing_scoped_and_preserves_exact_ids(workspace):
    _, documents, _, _ = workspace
    with pytest.raises(DomainError, match='不在当前来源范围'):
        documents.read(['src-a'], ['src-a#P1', 'src-b#P1'])

    result = documents.read(['src-a'], ['src-a#P2', 'src-a#P1'])
    assert [item['id'] for item in result['evidence']] == ['src-a#P2', 'src-a#P1']
    assert result == {
        'evidence': [documents.evidence(['src-a'])[1], documents.evidence(['src-a'])[0]],
        'requested_count': 2,
        'returned_count': 2,
        'omitted_refs': [],
        'omitted_ref_count': 0,
        'truncated': False,
    }


def test_read_excerpts_oversized_paragraph_and_bounds_huge_ref_lists(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Large reads')
    giant = 'BEGIN-' + ('界' * 9000) + '-END'
    paragraphs = [giant] + [f'paragraph-{index}' for index in range(1, 600)]
    add_source(store, chat, 'src-large', 'Large', 'primary', paragraphs)
    try:
        documents = DocumentWorkspace(store)
        excerpt = documents.read(['src-large'], ['src-large#P1'], budget=900)
        assert excerpt['requested_count'] == excerpt['returned_count'] == 1
        assert excerpt['truncated'] is True
        item = excerpt['evidence'][0]
        assert item['id'] == 'src-large#P1'
        assert item['truncated'] is True
        assert item['excerpt'] == {'start': 0, 'end': len(item['text']), 'total': len(giant)}
        assert item['text'] == giant[:item['excerpt']['end']]
        assert serialized_size(excerpt) <= 900

        refs = [f'src-large#P{index}' for index in range(1, 601)]
        bounded = documents.read(['src-large'], refs, budget=1200)
        assert bounded['requested_count'] == 600
        assert bounded['returned_count'] < 600
        assert bounded['omitted_ref_count'] == 600 - bounded['returned_count']
        assert bounded['truncated'] is True
        assert serialized_size(bounded) <= 1200
        assert all(ref in refs for ref in bounded['omitted_refs'])
        with pytest.raises(DomainError, match='10000'):
            documents.read(['src-large'], ['src-large#P1'] * 10_001)
    finally:
        store.close()
