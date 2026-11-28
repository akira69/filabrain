import pytest
from app.api.deps import require_auth
from app.core.security import Principal
from app.main import app
from app.models import Filament, LabelPreset, Manufacturer, Spool, SpoolStatus, SystemExtraField, User
from app.models.label_preset import label_preset_name_key
from fastapi import HTTPException
from sqlalchemy import select
from test_backend import BASE, installed

__all__ = ['installed']
pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio

CLEANUP = f'{BASE}/extra-field-cleanup'


async def seed(db, native=None):
    manufacturer = Manufacturer(name='Cleanup fixture')
    definition = SystemExtraField(target_type='filament', key='legacy_density', label='Density', field_type='number')
    db.add_all([manufacturer, definition])
    await db.flush()
    filament = Filament(manufacturer_id=manufacturer.id, designation='Cleanup PLA', material_type='PLA',
                        diameter_mm=1.75, density_g_cm3=native, custom_fields={'legacy_density': 1.24, 'keep': 'yes'})
    db.add(filament)
    await db.flush()
    ids = definition.id, filament.id
    await db.commit()
    return ids


async def test_cleanup_previews_native_copy_and_retires_definition_atomically(installed, db_session):
    field_id, filament_id = await seed(db_session)
    listed = await installed.get(CLEANUP)
    assert listed.status_code == 200, listed.text
    assert [entry['id'] for entry in listed.json()['items']] == [field_id]
    response = await installed.get(f'{CLEANUP}/{field_id}')
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['can_apply']
    assert preview['rows'] == [{'id': filament_id, 'custom': 1.24, 'native': None, 'action': 'copy'}]
    assert (await db_session.get(Filament, filament_id)).density_g_cm3 is None
    applied = await installed.post(f'{CLEANUP}/{field_id}', json={'fingerprint': preview['fingerprint']})
    assert applied.status_code == 200, applied.text
    archive_id = applied.json()['archive_id']
    archived = await installed.get(f'{CLEANUP}/archives/{archive_id}')
    assert archived.status_code == 200, archived.text
    assert archived.json()['definition']['key'] == 'legacy_density'
    assert archived.json()['rows'] == preview['rows']
    db_session.expire_all()
    assert await db_session.get(SystemExtraField, field_id) is None
    filament = await db_session.get(Filament, filament_id)
    assert filament.density_g_cm3 == 1.24
    assert filament.custom_fields == {'keep': 'yes'}


async def test_cleanup_rejects_stale_preview_and_conflicting_density(installed, db_session):
    field_id, filament_id = await seed(db_session)
    preview = (await installed.get(f'{CLEANUP}/{field_id}')).json()
    filament = await db_session.get(Filament, filament_id)
    filament.density_g_cm3 = 1.06
    await db_session.commit()
    response = await installed.post(f'{CLEANUP}/{field_id}', json={'fingerprint': preview['fingerprint']})
    assert response.status_code == 409, response.text
    changed = (await installed.get(f'{CLEANUP}/{field_id}')).json()
    assert not changed['can_apply']
    assert changed['rows'] == [{'id': filament_id, 'custom': 1.24, 'native': 1.06, 'action': 'conflict'}]
    response = await installed.post(f'{CLEANUP}/{field_id}', json={'fingerprint': changed['fingerprint']})
    assert response.status_code == 409
    assert await db_session.get(SystemExtraField, field_id) is not None


async def test_existing_label_reference_blocks_cleanup(installed, db_session):
    field_id, _ = await seed(db_session, 1.24)
    owner = await db_session.scalar(select(User.id).limit(1))
    db_session.add(LabelPreset(user_id=owner, preset_type='filament', name='Density label',
                               name_key=label_preset_name_key('Density label'), data={'text': '{extra.filament.legacy_density}'}))
    await db_session.commit()
    preview = (await installed.get(f'{CLEANUP}/{field_id}')).json()
    assert not preview['can_apply']
    assert 'referenced' in preview['blockers']


async def test_spool_override_cannot_be_promoted_to_shared_filament(installed, db_session):
    _, filament_id = await seed(db_session)
    field = SystemExtraField(target_type='spool', key='legacy_bed', label='Bed Temperature', field_type='range')
    status_id = await db_session.scalar(select(SpoolStatus.id).where(SpoolStatus.key == 'new'))
    spool = Spool(filament_id=filament_id, status_id=status_id, custom_fields={'legacy_bed': {'min': 50, 'max': 55}})
    db_session.add_all([field, spool])
    await db_session.flush()
    field_id = field.id
    await db_session.commit()
    preview = (await installed.get(f'{CLEANUP}/{field_id}')).json()
    assert not preview['can_apply']
    assert preview['rows'][0]['action'] == 'conflict'
    assert (await db_session.get(Filament, filament_id)).bed_temp_range_c is None


async def test_api_key_cannot_use_admin_cleanup(installed, monkeypatch):
    async def api_principal():
        return Principal(auth_type='token', is_superadmin=True, scopes=['admin:system', 'filaments:read'])

    monkeypatch.setitem(app.dependency_overrides, require_auth, api_principal)
    assert (await installed.get(CLEANUP)).status_code == 403
    assert (await installed.post(f'{CLEANUP}/1', json={'fingerprint': '1' * 64})).status_code == 403


async def test_host_delete_failure_keeps_original_inventory_and_no_archive(installed, db_session, monkeypatch):
    from filabrain import extra_field_cleanup

    async def unavailable(_field_id, _db):
        raise HTTPException(503, 'Host definition removal failed')

    field_id, filament_id = await seed(db_session)
    preview = (await installed.get(f'{CLEANUP}/{field_id}')).json()
    monkeypatch.setattr(extra_field_cleanup, 'delete_system_extra_field', unavailable)
    response = await installed.post(f'{CLEANUP}/{field_id}', json={'fingerprint': preview['fingerprint']})
    assert response.status_code == 503, response.text
    assert (await installed.get(CLEANUP)).json()['archives'] == []
    db_session.expire_all()
    assert await db_session.get(SystemExtraField, field_id) is not None
    filament = await db_session.get(Filament, filament_id)
    assert filament.density_g_cm3 is None
    assert filament.custom_fields['legacy_density'] == 1.24
