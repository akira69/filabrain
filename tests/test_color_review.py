"""Draft color reviews resolve publisher previews without changing the library."""
import pytest
from app.api.deps import require_auth
from app.core.cache import TTLCache
from app.core.security import Principal
from app.main import app
from app.models import Color, Filament, FilamentColor, InstalledPlugin, Manufacturer
from sqlalchemy import event, select, text
from test_backend import BASE, installed

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio
PATH = f"{BASE}/color-previews"


@pytest.fixture
def publisher(monkeypatch):
    from filabrain import color_standards

    async def read(url):
        assert url == 'https://www.ral-farben.de/en/all-ral-colours'
        return b'<a href="/en/colour/ral-classic/ral-3020/9143" style="background-color:#AB1519"><span>RAL</span><span class="number">3020</span><div class="subtext">Verkehrsrot<br/>Traffic red</div></a>'

    monkeypatch.setattr(color_standards, 'read_asset', read)
    monkeypatch.setattr(color_standards, '_previews', TTLCache())
    monkeypatch.setattr(color_standards, '_ral_page', TTLCache())


async def test_draft_review_matches_hex_and_preserves_current_selection_without_writes(installed, db_session, publisher):
    selected = Color(name='Manufacturer red', hex_code='#ab1519')
    matching = Color(name='Different label', hex_code='#Ab1519')
    unrelated = Color(name='Traffic red', hex_code='#FFFFFF')
    manufacturer = Manufacturer(name='Preview fixture')
    db_session.add_all([selected, matching, unrelated, manufacturer])
    await db_session.flush()
    filament = Filament(manufacturer_id=manufacturer.id, designation='Existing', material_type='PLA', diameter_mm=1.75)
    db_session.add(filament)
    await db_session.flush()
    assignment = FilamentColor(filament_id=filament.id, color_id=selected.id, position=1)
    db_session.add(assignment)
    await db_session.execute(text("UPDATE colors SET hex_code = '#ab1519' WHERE id = :id"), {'id': selected.id})
    await db_session.execute(text("UPDATE colors SET hex_code = '#Ab1519' WHERE id = :id"), {'id': matching.id})
    await db_session.commit()
    # Initialize the plugin schema before measuring the preview's write behavior.
    await installed.get(f'{BASE}/status')
    writes = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(' ', 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(statement)

    event.listen(db_session.bind.sync_engine, 'before_cursor_execute', record)
    try:
        response = await installed.post(PATH, json={
            'references': [{'code': 'RAL 3020', 'manufacturer_source': 'https://manufacturer.example/products/red', 'from_product_url': True}],
            'color_id': selected.id,
        })
    finally:
        event.remove(db_session.bind.sync_engine, 'before_cursor_execute', record)
    assert response.status_code == 200, response.text
    review = response.json()
    assert review['current'] == [{'id': selected.id, 'name': 'Manufacturer red', 'hex_code': '#ab1519'}]
    assert (review['can_save'], review['can_assign'], review['can_create']) == (True, True, True)
    assert review['references'] == [{
        'code': 'RAL 3020', 'name': 'Traffic red', 'hex_code': '#AB1519',
        'source': 'https://www.ral-farben.de/en/colour/ral-classic/ral-3020/9143',
        'manufacturer_source': 'https://manufacturer.example/products/red', 'from_product_url': True,
        'saved': False, 'token': '',
        'matches': [{'id': matching.id, 'name': 'Different label', 'hex_code': '#Ab1519'}],
    }]
    assert writes == []
    await db_session.refresh(assignment)
    assert assignment.color_id == selected.id
    assert len(list(await db_session.scalars(select(Color)))) == 3


@pytest.mark.parametrize('payload', [
    {'references': []},
    {'references': [{'code': 'RAL 3020'}] * 5},
    {'references': [{'code': 'RAL 3020 trailing text'}]},
    {'references': [{'code': 'RAL ٣٠٢٠'}]},
    {'references': [{'code': 'Pantone ７５４６ C'}]},
    {'references': [{'code': 'Pantone 7546 C/../../secret'}]},
    {'references': [{'code': 'https://attacker.example/ral-3020'}]},
    {'references': [{'code': 'RAL ' + '1' * 100}]},
    {'references': [{'code': 'RAL 3020', 'manufacturer_source': 'x' * 2049}]},
    {'references': [{'code': 'RAL 3020'}], 'color_id': True},
    {'references': [{'code': 'RAL 3020'}], 'color_id': 0},
    {'references': [{'code': 'RAL 3020'}], 'action': 'create'},
])
async def test_draft_review_rejects_unbounded_or_non_reference_input(installed, monkeypatch, payload):
    from filabrain import color_standards

    async def forbidden(_url):
        pytest.fail('Invalid input must not request a publisher')

    monkeypatch.setattr(color_standards, 'read_asset', forbidden)
    assert (await installed.post(PATH, json=payload)).status_code == 422


@pytest.mark.parametrize('source', [
    'javascript:alert(1)', 'https://username:password@manufacturer.example/red',
    'http://127.0.0.1/red', 'http://[::1]/red', 'http://localhost/red',
    'http://192.168.1.5/red', 'http://127.1/red', 'http://127.000.000.001/red',
    'https://manufacturer.example\\@localhost/red',
])
async def test_draft_review_omits_unsafe_manufacturer_links(installed, publisher, source):
    response = await installed.post(PATH, json={'references': [{'code': 'RAL 3020', 'manufacturer_source': source}]})
    assert response.status_code == 200, response.text
    assert response.json()['references'][0]['manufacturer_source'] is None


async def test_unknown_reference_has_no_invented_preview(installed, publisher):
    response = await installed.post(PATH, json={'references': [{'code': 'RAL 9999'}]})
    assert response.status_code == 200, response.text
    assert response.json()['current'] == []
    assert response.json()['references'] == [{
        'code': 'RAL 9999', 'name': 'RAL 9999', 'hex_code': None, 'source': None,
        'manufacturer_source': None, 'from_product_url': False, 'saved': False, 'matches': [], 'token': '',
    }]


async def test_deleted_current_color_requires_refresh(installed, publisher):
    response = await installed.post(PATH, json={'references': [{'code': 'RAL 3020'}], 'color_id': 2147483647})
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(('scopes', 'expected_status', 'can_create'), [
    (['filaments:read'], 403, None),
    (['colors:create'], 403, None),
    (['filaments:create'], 200, False),
    (['filaments:update'], 200, False),
    (['filaments:update', 'colors:create'], 200, True),
])
async def test_draft_review_enforces_effective_edit_and_color_permissions(installed, publisher, monkeypatch, scopes, expected_status, can_create):
    async def principal():
        return Principal(auth_type='token', is_superadmin=True, scopes=scopes)

    monkeypatch.setitem(app.dependency_overrides, require_auth, principal)
    response = await installed.post(PATH, json={'references': [{'code': 'RAL 3020'}]})
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert response.json()['can_create'] is can_create


async def test_disabled_plugin_cannot_resolve_draft_colors(installed, db_session, monkeypatch):
    from filabrain import color_standards

    async def forbidden(_url):
        pytest.fail('Disabled plugin must not request a publisher')

    monkeypatch.setattr(color_standards, 'read_asset', forbidden)
    plugin = await db_session.scalar(select(InstalledPlugin).where(InstalledPlugin.plugin_key == 'filabrain'))
    plugin.is_active = False
    await db_session.commit()
    assert (await installed.post(PATH, json={'references': [{'code': 'RAL 3020'}]})).status_code == 404


async def test_draft_pantone_review_uses_the_exact_publisher_chip(installed, monkeypatch):
    import io
    from PIL import Image
    from filabrain import color_standards

    chip = io.BytesIO()
    Image.new('RGB', (16, 16), (36, 55, 70)).save(chip, 'WEBP', lossless=True)
    asset = 'https://pantone-web-assets-dev.s3.us-east-2.amazonaws.com/magento/media/color-finder/img/chips/pantone-color-chip-7546-c.webp'

    async def read(url):
        if url == 'https://www.pantone.com/na/en-us/color-finder/7546-c':
            return f'<img src="{asset}" alt="PANTONE 7546 C Chip">'.encode()
        assert url == asset
        return chip.getvalue()

    monkeypatch.setattr(color_standards, 'read_asset', read)
    monkeypatch.setattr(color_standards, '_previews', TTLCache())
    response = await installed.post(PATH, json={'references': [{'code': 'pantone 7546 c'}]})
    assert response.status_code == 200, response.text
    preview = response.json()['references'][0]
    assert (preview['code'], preview['hex_code'], preview['source']) == (
        'Pantone 7546 C', '#243746', 'https://www.pantone.com/na/en-us/color-finder/7546-c',
    )


async def test_disabled_during_publisher_request_cannot_return_a_draft_review(installed, db_session, monkeypatch):
    from filabrain import color_standards

    async def read(_url):
        plugin = await db_session.scalar(select(InstalledPlugin).where(InstalledPlugin.plugin_key == 'filabrain'))
        plugin.is_active = False
        await db_session.commit()
        return b''

    monkeypatch.setattr(color_standards, 'read_asset', read)
    monkeypatch.setattr(color_standards, '_previews', TTLCache())
    monkeypatch.setattr(color_standards, '_ral_page', TTLCache())
    response = await installed.post(PATH, json={'references': [{'code': 'RAL 3020'}]})
    assert response.status_code == 404


async def test_created_preview_swatch_and_spool_dimensions_save_with_the_new_filament(installed, db_session, publisher):
    manufacturer = Manufacturer(name='Complete draft fixture')
    db_session.add(manufacturer)
    await db_session.commit()
    manufacturer_id = manufacturer.id
    preview = await installed.post(PATH, json={'references': [{'code': 'RAL 3020'}]})
    assert preview.is_success, preview.text
    reference = preview.json()['references'][0]
    color = await installed.post('/api/v1/colors', json={
        'name': 'Traffic red (RAL 3020)', 'hex_code': reference['hex_code'],
        'custom_fields': {'filabrain_color_references': [{'code': reference['code'], 'source': reference['source']}]},
    })
    assert color.status_code == 201, color.text
    color_id = color.json()['id']
    response = await installed.post(f'{BASE}/saved-filaments/color-and-spool-draft', json={
        'manufacturer_id': manufacturer_id, 'designation': 'PLA Enzo Red', 'material_type': 'PLA',
        'diameter_mm': 1.75, 'manufacturer_color_name': 'Enzo Red', 'colors': [{'color_id': color_id}],
        'default_spool_weight_g': 233, 'spool_outer_diameter_mm': 200, 'spool_width_mm': 66,
    })
    assert response.is_success, response.text
    saved = await installed.get('/api/v1/filaments/' + str(response.json()['id']))
    assert saved.is_success, saved.text
    filament = saved.json()
    assert filament['manufacturer_color_name'] == 'Enzo Red'
    assert filament['colors'][0]['color_id'] == color_id
    assert filament['colors'][0]['color']['hex_code'].upper() == '#AB1519'
    assert filament['default_spool_weight_g'] == 233
    assert filament['spool_outer_diameter_mm'] == 200
    assert filament['spool_width_mm'] == 66
