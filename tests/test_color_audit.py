"""Selected-library color audits preserve product names and shared palette state."""
import pytest
import pytest_asyncio
from app.models import Color, Filament, FilamentColor, Manufacturer
from sqlalchemy import select
from test_backend import BASE, installed
from test_color_review import publisher

pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def audit(installed, db_session, monkeypatch, publisher):
    from filabrain import batch_api
    from filabrain.db import FilamentResearchBatchItem
    monkeypatch.setattr(batch_api, 'start_worker', lambda _app: None)
    maker = Manufacturer(name='Audit fixture')
    color = Color(name='RAL 3020', hex_code='#FF0000', custom_fields={'unrelated': 'keep'})
    db_session.add_all([maker, color])
    await db_session.flush()
    filaments = [Filament(manufacturer_id=maker.id, designation=f'PLA {i}', material_type='PLA',
                          diameter_mm=1.75, manufacturer_color_name='Enzo Red', color_mode='single') for i in range(2)]
    db_session.add_all(filaments)
    await db_session.flush()
    links = [FilamentColor(filament_id=f.id, color_id=color.id, position=1) for f in filaments]
    db_session.add_all(links)
    await db_session.commit()
    result = await installed.post(f'{BASE}/batches', json={'filament_ids': [filaments[0].id], 'mode': 'colors'})
    assert result.status_code == 200, result.text
    run = result.json()
    item = await db_session.get(FilamentResearchBatchItem, run['items'][0]['id'])
    item.status = 'no_change'
    await db_session.commit()
    path = f"{BASE}/batches/{run['id']}/items/{item.id}/swatches"
    return path, color, filaments, links, item


async def test_shared_rename_changes_only_name(installed, db_session, audit):
    path, color, filaments, links, _ = audit
    review = (await installed.get(path)).json()
    assert review['can_update'] is True
    reference = review['references'][0]
    change, = reference['name_changes']
    assert change['usage_count'] == 2
    assert change['proposed_name'] == 'Traffic red (RAL 3020)'
    response = await installed.post(path, json={'code': reference['code'], 'action': 'rename',
        'token': change['token'], 'color_id': color.id, 'color_name': change['current_name']})
    assert response.status_code == 200, response.text
    await db_session.refresh(color)
    assert (color.name, color.hex_code) == ('Traffic red (RAL 3020)', '#FF0000')
    assert color.custom_fields['unrelated'] == 'keep'
    for filament, link in zip(filaments, links):
        await db_session.refresh(filament)
        await db_session.refresh(link)
        assert filament.manufacturer_color_name == 'Enzo Red'
        assert link.color_id == color.id


@pytest.mark.parametrize('mutation', ['usage', 'metadata', 'name', 'preview', 'forged_color', 'wrong_token'])
async def test_shared_rename_rejects_stale_or_forged_state(installed, db_session, audit, monkeypatch, mutation):
    from filabrain import batch_api
    path, color, filaments, links, _ = audit
    reference = (await installed.get(path)).json()['references'][0]
    change, = reference['name_changes']
    request = {'code': reference['code'], 'action': 'rename', 'token': change['token'],
               'color_id': color.id, 'color_name': change['current_name']}
    if mutation == 'usage':
        await db_session.delete(links[1])
    elif mutation == 'metadata':
        color.custom_fields = {'unrelated': 'edited'}
    elif mutation == 'name':
        color.name = 'Edited'
    elif mutation == 'preview':
        async def changed(_code):
            return {'name': 'Changed publisher', 'hex_code': '#AB1519', 'source': 'https://www.ral-farben.de/en/colour/ral-3020'}
        monkeypatch.setattr(batch_api, 'resolve_preview', changed)
    elif mutation == 'forged_color':
        request['color_id'] += 1
    else:
        request['token'] = reference['token']
    await db_session.commit()
    result = await installed.post(path, json=request)
    assert result.status_code == 409, result.text


@pytest.mark.parametrize('name', ['Enzo Red', 'RAL 3020'])
async def test_colors_mode_audits_complete_records_and_limits_fields(monkeypatch, name):
    from filabrain import proposals
    from filabrain.batch_api import CreateBatch
    assert CreateBatch(filament_ids=[1], mode='colors').mode == 'colors'
    assert proposals.MODE_FIELDS['colors'] == ('manufacturer_color_name',)
    snapshot = {field: 'complete' for field in proposals.MODE_FIELDS['fill']}
    snapshot.update(manufacturer_name='Maker', designation='PLA', material_type='PLA', diameter_mm=1.75,
                    raw_material_weight_g=1000, manufacturer_color_name=name, manufacturer_sku='SKU',
                    manufacturer_url='https://maker.example', material_subgroup=None, finish_type=None, color_mode='single')
    called = []
    async def research(_db, identity, **kwargs):
        called.append(identity)
        return {'suggestions': [
            {'field': 'manufacturer_color_name', 'value': 'Enzo Red', 'sources': [{
                'url': 'https://maker.example/products/red', 'title': 'Enzo Red',
                'evidence': 'Enzo Red, RAL 3020'}]},
            {'field': 'density_g_cm3', 'value': 1.24, 'sources': []}], 'issues': [{
                'code': 'manufacturer_color_reference', 'name': 'RAL 3020',
                'url': 'https://maker.example/products/red'}]}
    monkeypatch.setattr(proposals, 'research_products', research)
    result = await proposals.batch_proposal(None, snapshot, 'colors', skip_complete=True)
    assert len(called) == 1
    assert called[0].color == ('' if name == 'RAL 3020' else name)
    assert [unit['id'] for unit in result['proposals']] == ([] if name == 'Enzo Red' else ['manufacturer_color_name'])
    if name == 'RAL 3020':
        assert any(issue.get('name') == name for issue in result['issues'])


@pytest.mark.parametrize(('value', 'expected'), [
    ('RAL Classic 3020', True), ('RAL Design 120 50 40', True), ('Pantone 7449 CP', True),
    ('Purple (RAL 4007)', False), ('RAL 3020 Traffic red', False), ('Enzo Red', False),
])
async def test_code_only_keeps_full_standard_variants(value, expected):
    from filabrain.color_audit import code_only
    assert code_only(value) is expected


@pytest.mark.parametrize(('code', 'name', 'expected'), [
    ('RAL 4007', 'Purple violet', 'Purple violet (RAL 4007)'),
    ('Pantone 7449 C', 'Pantone 7449 C', 'Pantone 7449 C'),
    ('Pantone 7449 CP', 'Purple', 'Purple (Pantone 7449 CP)'),
    ('Pantone 19-4052 TCX', 'Classic Blue', 'Classic Blue (Pantone 19-4052 TCX)'),
    ('RAL 4007', 'Purple violet (RAL 4007)', 'Purple violet (RAL 4007)'),
])
async def test_canonical_names_retain_reference_suffix(code, name, expected):
    from filabrain.color_standards import canonical_name
    assert canonical_name(code, {'name': name}) == expected


@pytest.mark.parametrize('origin', ['color_name', 'color_metadata', 'filament_metadata', 'issue'])
async def test_discovers_only_explicit_saved_or_researched_codes(installed, db_session, audit, origin):
    path, color, filaments, _, item = audit
    color.name = 'Enzo Red'
    if origin == 'color_name':
        color.name = 'Traffic red (RAL 3020)'
    elif origin == 'color_metadata':
        color.custom_fields = {'filabrain_color_references': [{'code': 'RAL 3020'}]}
    elif origin == 'filament_metadata':
        filaments[0].custom_fields = {'filabrain_color_references': [{'code': 'RAL 3020'}]}
    else:
        item.issues = [{'code': 'manufacturer_color_reference', 'name': 'RAL 3020', 'url': 'https://maker.example/red'}]
    await db_session.commit()
    response = await installed.get(path)
    assert response.status_code == 200, response.text
    assert response.json()['references'][0]['code'] == 'RAL 3020'
    if origin != 'issue':
        assert response.json()['references'][0]['saved_reference'] is True
    color.name = 'Enzo Red'
    color.custom_fields = {'hex': '#AB1519'}
    filaments[0].custom_fields = None
    item.issues = []
    await db_session.commit()
    assert (await installed.get(path)).json()['references'] == []


@pytest.mark.parametrize('reason', ['multi', 'two_assignments', 'conflict', 'publisher_unavailable', 'canceled', 'duplicate'])
async def test_unsafe_shared_renames_are_unavailable_or_conflict(installed, db_session, audit, monkeypatch, reason):
    from filabrain import batch_api
    from filabrain.db import FilamentResearchBatch
    path, color, filaments, _, item = audit
    reference = (await installed.get(path)).json()['references'][0]
    change, = reference['name_changes']
    if reason == 'multi':
        filaments[0].color_mode = 'multi'
        item.identity = {**item.identity, 'color_mode': 'multi'}
    elif reason == 'two_assignments':
        other = Color(name='Other', hex_code='#FFFFFF')
        db_session.add(other)
        await db_session.flush()
        db_session.add(FilamentColor(filament_id=filaments[0].id, color_id=other.id, position=2))
    elif reason == 'conflict':
        color.custom_fields = {'filabrain_color_references': [{'code': 'RAL 5000'}]}
    elif reason == 'publisher_unavailable':
        async def unavailable(code):
            return {'name': code, 'hex_code': None, 'source': None}
        monkeypatch.setattr(batch_api, 'resolve_preview', unavailable)
    elif reason == 'canceled':
        batch = await db_session.get(FilamentResearchBatch, item.batch_id)
        batch.canceled_at = batch_api.now()
    else:
        db_session.add(Color(name=change['proposed_name'], hex_code=color.hex_code))
    await db_session.commit()
    if reason != 'duplicate':
        review = await installed.get(path)
        assert review.status_code == 200, review.text
        assert review.json()['references'][0]['name_changes'] == []
    result = await installed.post(path, json={'code': reference['code'], 'action': 'rename',
        'token': change['token'], 'color_id': color.id, 'color_name': change['current_name']})
    assert result.status_code == 409, result.text
    await db_session.refresh(color)
    assert color.name == 'RAL 3020'


async def test_shared_rename_rechecks_color_permission(installed, db_session, audit, monkeypatch):
    from filabrain import batch_api
    path, color, _, _, _ = audit
    reference = (await installed.get(path)).json()['references'][0]
    change, = reference['name_changes']
    async def denied(*_args):
        return False
    monkeypatch.setattr(batch_api, 'may_update_color', denied)
    review = (await installed.get(path)).json()
    assert review['can_update'] is False
    assert review['references'][0]['name_changes'] == []
    response = await installed.post(path, json={'code': reference['code'], 'action': 'rename',
        'token': change['token'], 'color_id': color.id, 'color_name': change['current_name']})
    assert response.status_code == 403


@pytest.mark.parametrize('reason', ['wrong_batch', 'wrong_owner', 'expired', 'deleted', 'revoked_editor'])
async def test_shared_rename_honors_batch_access(installed, db_session, audit, monkeypatch, reason):
    from datetime import timedelta
    from filabrain import batch_api
    from filabrain.db import FilamentResearchBatch
    path, color, _, _, item = audit
    reference = (await installed.get(path)).json()['references'][0]
    change, = reference['name_changes']
    batch = await db_session.get(FilamentResearchBatch, item.batch_id)
    if reason == 'wrong_batch':
        path = path.replace(batch.id, '0' * 32)
    elif reason == 'wrong_owner':
        from app.models import User
        other = User(email='other@example.test')
        db_session.add(other)
        await db_session.flush()
        batch.owner_id = other.id
    elif reason == 'expired':
        batch.finished_at = batch_api.now() - timedelta(days=31)
    elif reason == 'deleted':
        item.live_filament_id = None
    else:
        async def denied(*_args):
            return False
        monkeypatch.setattr(batch_api, 'owner_can_update', denied)
    await db_session.commit()
    response = await installed.post(path, json={'code': reference['code'], 'action': 'rename',
        'token': change['token'], 'color_id': color.id, 'color_name': change['current_name']})
    assert response.status_code == (403 if reason == 'revoked_editor' else 404), response.text


async def test_marketing_name_correction_keeps_saved_reference(installed, db_session, audit):
    from filabrain.color_audit import COLOR_REFERENCES
    path, _, filaments, _, item = audit
    filament = filaments[0]
    filament.manufacturer_color_name = 'Pantone 7449 CP'
    filament.custom_fields = {'unrelated': 'preserve'}
    item.identity = {**item.identity, 'manufacturer_color_name': filament.manufacturer_color_name}
    item.status = 'ready'
    item.proposals = [{'id': 'manufacturer_color_name', 'fields': {'manufacturer_color_name': {
        'old': 'Pantone 7449 CP', 'value': 'Decepticon Purple'}}}]
    await db_session.commit()
    batch_path = path.split('/items/')[0]
    selection = {'item_id': item.id, 'revision': item.revision, 'decision_revision': item.decision_revision,
                 'approved_ids': ['manufacturer_color_name']}
    approved = await installed.post(batch_path + '/decisions', json={'items': [selection | {'decision': 'approved'}]})
    assert approved.status_code == 200, approved.text
    selection['decision_revision'] = approved.json()['items'][0]['decision_revision']
    applied = await installed.post(batch_path + '/apply', json={'items': [selection]})
    assert applied.status_code == 200, applied.text
    assert applied.json()['results'][0]['status'] == 'applied'
    await db_session.refresh(filament)
    assert filament.manufacturer_color_name == 'Decepticon Purple'
    assert filament.custom_fields[COLOR_REFERENCES] == [{'code': 'Pantone 7449 CP'}]
    assert filament.custom_fields['unrelated'] == 'preserve'


async def test_saved_code_fast_path_does_not_research_marketing_name(monkeypatch):
    from filabrain import proposals
    snapshot = {'manufacturer_name': 'Maker', 'designation': 'PLA', 'material_type': 'PLA',
        'manufacturer_color_name': 'Enzo Red', 'diameter_mm': 1.75, 'raw_material_weight_g': 1000,
        'manufacturer_sku': 'SKU', 'color_references': ['RAL 3020']}
    async def forbidden(*_args, **_kwargs):
        pytest.fail('Saved reference audit must not call product/model research')
    monkeypatch.setattr(proposals, 'research_products', forbidden)
    result = await proposals.batch_proposal(None, snapshot, 'colors', skip_complete=True)
    assert result['proposals'] == []
    assert result['issues'] == [{'code': 'manufacturer_color_reference', 'name': 'RAL 3020',
                                'url': None, 'saved_reference': True}]


async def test_shared_rename_binds_unselected_product_identity(installed, db_session, audit):
    path, color, filaments, _, _ = audit
    reference = (await installed.get(path)).json()['references'][0]
    change, = reference['name_changes']
    filaments[1].manufacturer_sku = 'A different variant'
    await db_session.commit()
    response = await installed.post(path, json={'code': reference['code'], 'action': 'rename',
        'token': change['token'], 'color_id': color.id, 'color_name': change['current_name']})
    assert response.status_code == 409, response.text


async def test_worker_collects_saved_color_codes_without_model_research(installed, db_session, audit, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from filabrain import batch_worker, proposals
    path, _, _, _, item = audit
    item.status = 'queued'
    await db_session.commit()
    async def forbidden(*_args, **_kwargs):
        pytest.fail('Known saved code with marketing name requires no product/model research')
    monkeypatch.setattr(proposals, 'research_products', forbidden)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    assert await batch_worker.run_next_batch_item(sessions)
    await db_session.refresh(item)
    assert item.status == 'no_change'
    assert item.issues == [{'code': 'manufacturer_color_reference', 'name': 'RAL 3020',
                            'url': None, 'saved_reference': True}]
    assert (await installed.get(path)).json()['references'][0]['name_changes'][0]['usage_count'] == 2


@pytest.mark.parametrize('saved_origin', ['manufacturer_name', 'palette', 'none'])
@pytest.mark.parametrize('evidence_case', [
    'catalog_only', 'no_reference', 'wrong_reference', 'sibling_wrong_reference', 'same_reference',
])
async def test_code_name_correction_requires_same_product_reference_in_real_research(monkeypatch, evidence_case, saved_origin):
    from filabrain import proposals
    from filabrain.sources import Evidence

    class DetachedSnapshotDB:
        def in_transaction(self):
            return False

        async def rollback(self):
            pass

    site = 'https://audit.example'
    url = site + '/products/red'
    named = evidence_case != 'catalog_only'
    designation = 'PLA (Enzo Red)' if named else 'PLA'
    title = 'PLA (Enzo Red) 1.75mm 1kg Filament'
    code = 'RAL 3020' if evidence_case == 'same_reference' else 'RAL 5000'
    scopes = (title, code) if evidence_case not in ('no_reference', 'catalog_only') else (title,)
    if evidence_case == 'sibling_wrong_reference':
        title = 'PLA (Enzo Red) 2.85mm 1kg Filament'
        scopes = (title, code)
    page = Evidence(url, title, '\n'.join(scopes), {'designation': title}, scopes=scopes)
    if evidence_case == 'catalog_only':
        page = Evidence(url, 'No product', 'No product')

    async def config(_db):
        return None, {'model_url': '', 'model_id': '', 'search_url': '',
                      'supplier_download_mib': 2, 'supplier_sites': []}

    async def catalog(_identity):
        return [{'id': 1, 'name': designation, 'manufacturer': {'name': 'Audit Brand'},
                 'material': {'key': 'PLA'}, 'manufacturer_color_name': 'Ocean Blue',
                 'diameter_mm': 1.75, 'nominal_weight_g': 1000, 'color_mode': 'single'}], 'https://catalog.example/filaments', True

    async def empty_ofd(_manufacturer):
        return [], ''

    async def fetch(requested_url, **_kwargs):
        assert requested_url == url
        return page

    async def empty_sitemap(*_args, **_kwargs):
        return []

    monkeypatch.setattr(proposals, 'config_record', config)
    monkeypatch.setattr(proposals, 'catalog_candidates', catalog)
    monkeypatch.setattr(proposals, 'ofd_candidates', empty_ofd)
    monkeypatch.setattr(proposals, 'fetch_supplier', fetch)
    monkeypatch.setattr(proposals, '_manufacturer_sitemap_candidates', empty_sitemap)
    monkeypatch.setattr(proposals, 'bundled_catalog_candidates', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(proposals, 'lookup_published_specs', lambda *_args, **_kwargs: [])
    snapshot = {'manufacturer_name': 'Audit Brand', 'designation': designation, 'material_type': 'PLA',
                'diameter_mm': 1.75, 'raw_material_weight_g': 1000, 'manufacturer_color_name': 'RAL3020',
                'manufacturer_sku': None, 'manufacturer_url': site, 'shop_url': url,
                'material_subgroup': None, 'finish_type': None, 'color_mode': 'single'}
    if saved_origin != 'manufacturer_name':
        snapshot['manufacturer_color_name'] = None
        if saved_origin == 'palette':
            snapshot['color_references'] = ['RAL 3020']
    result = await proposals.batch_proposal(DetachedSnapshotDB(), snapshot, 'colors')
    if evidence_case == 'same_reference' or (saved_origin == 'none' and evidence_case == 'wrong_reference'):
        proposal, = result['proposals']
        assert proposal['fields']['manufacturer_color_name'] == {'old': snapshot['manufacturer_color_name'], 'value': 'Enzo Red'}
        assert proposal['sources'][0]['url'] == url
    else:
        assert result['proposals'] == [], result
    if saved_origin != 'none' or evidence_case == 'same_reference':
        assert any(issue.get('name') == 'RAL 3020' for issue in result['issues'])


@pytest.mark.parametrize('reference_origin', ['manufacturer_name', 'metadata', 'override'])
async def test_preexisting_conflicting_linked_references_block_shared_rename(installed, db_session, audit, reference_origin):
    path, color, filaments, links, item = audit
    color.name = 'Generic red'
    filaments[0].manufacturer_color_name = 'RAL3020'
    item.identity = {**item.identity, 'manufacturer_color_name': 'RAL3020'}
    if reference_origin == 'manufacturer_name':
        filaments[1].manufacturer_color_name = 'RAL5000'
    elif reference_origin == 'metadata':
        filaments[1].custom_fields = {'filabrain_color_references': [{'code': 'RAL 5000'}]}
    else:
        links[1].display_name_override = 'Blue (RAL 5000)'
    await db_session.commit()
    response = await installed.get(path)
    assert response.status_code == 200, response.text
    reference, = response.json()['references']
    assert reference['name_changes'] == []
    assert reference['rename_conflicts'] == ['RAL 5000']
    # A hand-built rename request remains invalid even though swatch assignment
    # choices continue to be available for the selected filament.
    assert response.json()['can_assign'] is True
    renamed = await installed.post(path, json={'action': 'rename', 'code': reference['code'],
        'token': reference['token'], 'color_id': color.id, 'color_name': color.name})
    assert renamed.status_code == 409, renamed.text
    await db_session.refresh(color)
    assert color.name == 'Generic red'
    await db_session.refresh(links[1])
    if reference_origin == 'override':
        assert links[1].display_name_override == 'Blue (RAL 5000)'


async def test_linked_reference_in_another_standard_is_not_a_conflict(installed, db_session, audit):
    path, color, filaments, links, _ = audit
    links[1].display_name_override = 'Pantone 7449 C'
    filaments[1].custom_fields = {'filabrain_color_references': [{'code': 'Pantone 7449 C'}]}
    await db_session.commit()
    reference, = (await installed.get(path)).json()['references']
    change, = reference['name_changes']
    assert reference.get('rename_conflicts', []) == []
    renamed = await installed.post(path, json={'action': 'rename', 'code': reference['code'],
        'token': change['token'], 'color_id': color.id, 'color_name': color.name})
    assert renamed.status_code == 200, renamed.text
    await db_session.refresh(links[1])
    assert links[1].display_name_override == 'Pantone 7449 C'


async def test_assignment_override_codes_are_audited_without_changing_overrides(installed, db_session, audit):
    path, color, _, links, _ = audit
    color.name = 'Generic red'
    links[0].display_name_override = 'My red (RAL 3020)'
    await db_session.commit()
    reference, = (await installed.get(path)).json()['references']
    assert reference['code'] == 'RAL 3020'
    change, = reference['name_changes']
    renamed = await installed.post(path, json={'action': 'rename', 'code': reference['code'],
        'token': change['token'], 'color_id': color.id, 'color_name': color.name})
    assert renamed.status_code == 200, renamed.text
    await db_session.refresh(links[0])
    assert links[0].display_name_override == 'My red (RAL 3020)'


@pytest.mark.parametrize('primary_name', ['RAL 3020', 'Generic red'])
async def test_palette_alternate_standard_metadata_preserves_explicit_name_primary(installed, db_session, audit, monkeypatch, primary_name):
    from filabrain import batch_api
    path, color, _, _, _ = audit
    color.name = primary_name
    color.custom_fields = {'filabrain_color_references': [{'code': 'RAL 3020'}, {'code': 'Pantone 7449 C'}]}
    await db_session.commit()
    resolve = batch_api.resolve_preview
    async def previews(code):
        if code == 'Pantone 7449 C':
            return {'name': code, 'hex_code': '#112233', 'source': 'https://www.pantone.com/na/en-us/color-finder/7449-c'}
        return await resolve(code)
    monkeypatch.setattr(batch_api, 'resolve_preview', previews)
    response = await installed.get(path)
    assert response.status_code == 200, response.text
    refs = {ref['code']: ref for ref in response.json()['references']}
    change, = refs['RAL 3020']['name_changes']
    assert change['proposed_name'] == 'Traffic red (RAL 3020)'
    assert bool(refs['Pantone 7449 C']['name_changes']) is (primary_name == 'Generic red')
    result = await installed.post(path, json={'action': 'rename', 'code': 'RAL 3020',
        'token': change['token'], 'color_id': color.id, 'color_name': color.name})
    assert result.status_code == 200, result.text
    await db_session.refresh(color)
    assert color.custom_fields['filabrain_color_references'] == [{'code': 'RAL 3020'}, {'code': 'Pantone 7449 C'}]


async def test_palette_name_with_several_codes_is_not_rewritten_losing_a_reference(installed, db_session, audit, monkeypatch):
    from filabrain import batch_api
    path, color, _, _, _ = audit
    color.name = 'Red (RAL3020 / Pantone7449 C)'
    await db_session.commit()
    resolve = batch_api.resolve_preview
    async def previews(code):
        if code == 'Pantone 7449 C':
            return {'name': code, 'hex_code': '#112233', 'source': 'https://www.pantone.com/na/en-us/color-finder/7449-c'}
        return await resolve(code)
    monkeypatch.setattr(batch_api, 'resolve_preview', previews)
    response = await installed.get(path)
    assert response.status_code == 200, response.text
    assert len(response.json()['references']) == 2
    assert all(ref['name_changes'] == [] for ref in response.json()['references'])
    await db_session.refresh(color)
    assert color.name == 'Red (RAL3020 / Pantone7449 C)'


@pytest.mark.parametrize(('mode', 'marketing_name', 'uses_model'), [
    ('colors', 'Enzo Red', False), ('colors', 'RAL3020', True),
    ('colors', None, True), ('fill', 'Enzo Red', True),
])
async def test_real_color_research_skips_model_only_for_an_existing_marketing_name(monkeypatch, mode, marketing_name, uses_model):
    from filabrain import proposals
    from filabrain.sources import Evidence

    class DetachedSnapshotDB:
        def in_transaction(self):
            return False

        async def rollback(self):
            pass

    url = 'https://audit.example/products/red'
    title = 'PLA (Enzo Red) 1.75mm 1kg Filament'
    page = Evidence(url, title, title + '\nRAL 3020', {'designation': title}, scopes=(title, 'RAL 3020'))
    calls = []

    async def config(_db):
        return None, {'model_url': 'https://configured-model.example', 'model_id': 'fixture-model',
                      'search_url': '', 'supplier_download_mib': 2, 'supplier_sites': []}

    async def catalog(_identity):
        return [], '', True

    async def empty_ofd(_manufacturer):
        return [], ''

    async def fetch(requested_url, **_kwargs):
        assert requested_url == url
        return page

    async def empty_sitemap(*_args, **_kwargs):
        return []

    async def model_catalog(*_args, **_kwargs):
        if not uses_model:
            pytest.fail('Existing marketing name requires no model discovery or extraction')

    async def extraction(*_args, **_kwargs):
        calls.append('extraction')
        return [], []

    monkeypatch.setattr(proposals, 'config_record', config)
    monkeypatch.setattr(proposals, 'catalog_candidates', catalog)
    monkeypatch.setattr(proposals, 'ofd_candidates', empty_ofd)
    monkeypatch.setattr(proposals, 'fetch_supplier', fetch)
    monkeypatch.setattr(proposals, '_manufacturer_sitemap_candidates', empty_sitemap)
    monkeypatch.setattr(proposals, 'bundled_catalog_candidates', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(proposals, 'lookup_published_specs', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(proposals, 'check_model_catalog', model_catalog)
    monkeypatch.setattr(proposals, 'request_extraction', extraction)
    snapshot = {'manufacturer_name': 'Audit Brand', 'designation': 'PLA (Enzo Red)', 'material_type': 'PLA',
                'diameter_mm': 1.75, 'raw_material_weight_g': 1000, 'manufacturer_color_name': marketing_name,
                'manufacturer_sku': None, 'manufacturer_url': 'https://audit.example', 'shop_url': url,
                'material_subgroup': None, 'finish_type': None, 'color_mode': 'single'}
    result = await proposals.batch_proposal(DetachedSnapshotDB(), snapshot, mode)
    assert any(issue.get('name') == 'RAL 3020' and not issue.get('saved_reference') for issue in result['issues'])
    assert calls == (['extraction'] if uses_model else [])
    if not uses_model:
        assert result['proposals'] == []
        assert not any(issue.get('source') == 'Configured model' for issue in result['issues'])


@pytest.mark.parametrize('mode', ['colors', 'fill'])
@pytest.mark.parametrize('warning', ['unavailable_source', 'supplier_download_limit'])
async def test_worker_keeps_color_reference_reviewable_despite_ancillary_failure(installed, db_session, audit, monkeypatch, mode, warning):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from filabrain import batch_worker
    from filabrain.db import FilamentResearchBatch
    path, _, _, _, item = audit
    batch = await db_session.get(FilamentResearchBatch, item.batch_id)
    batch.mode = mode
    item.status = 'queued'
    await db_session.commit()
    diagnostics = [{'code': 'manufacturer_color_reference', 'name': 'RAL 3020',
                    'url': 'https://manufacturer.example/red'}, {'code': warning, 'source': 'Ancillary source'}]
    async def partial_result(*_args, **_kwargs):
        return {'proposals': [], 'issues': diagnostics}
    monkeypatch.setattr(batch_worker, 'batch_proposal', partial_result)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    assert await batch_worker.run_next_batch_item(sessions)
    await db_session.refresh(item)
    await db_session.refresh(batch)
    assert item.status == ('no_change' if mode == 'colors' else 'failed')
    assert item.issues == diagnostics
    assert (batch.finished_at is not None) is (mode == 'colors')
    if mode == 'colors':
        review = await installed.get(path)
        assert review.status_code == 200, review.text
        assert review.json()['references'][0]['name_changes'][0]['proposed_name'] == 'Traffic red (RAL 3020)'


async def test_fifth_researched_reference_blocks_rename_without_exceeding_preview_limit(installed, db_session, audit, monkeypatch):
    from filabrain import batch_api
    path, color, _, _, item = audit
    color.name = 'Red'
    codes = ['RAL 3020', 'Pantone 7449 C', 'Pantone 7546 C', 'Pantone 7549 C', 'RAL 9005']
    item.issues = [{'code': 'manufacturer_color_reference', 'name': code, 'url': 'https://maker.example/red'}
                   for code in codes]
    await db_session.commit()
    resolved = []
    async def preview(code):
        resolved.append(code)
        return {'name': 'Official name', 'hex_code': '#AB1519', 'source': 'https://publisher.example/' + code}
    monkeypatch.setattr(batch_api, 'resolve_preview', preview)
    response = await installed.get(path)
    assert response.status_code == 200, response.text
    assert resolved == codes[:4]
    refs = response.json()['references']
    assert [reference['code'] for reference in refs] == codes[:4]
    assert refs[0]['name_changes'] == []
    assert response.json()['can_assign'] is True
    kept = await installed.post(path, json={'code': refs[0]['code'], 'token': refs[0]['token'], 'action': 'keep'})
    assert kept.status_code == 200, kept.text
    await db_session.refresh(color)
    assert color.name == 'Red'
