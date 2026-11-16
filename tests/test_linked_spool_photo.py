import pytest
from app.api.v1 import filamentdb_proxy
from app.core.config import settings
from app.models import Filament, Manufacturer
from fastapi import HTTPException
from sqlalchemy import select
from test_backend import BASE, installed
from test_spool_profiles import catalog_requests

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio


@pytest.fixture
def linked_profile(monkeypatch):
    profile = {
        "id": 735,
        "name": "Paramount 170 g Spool",
        "is_active": True,
        "image_file": "735.png",
        "empty_weight_g": 170,
        "outer_diameter_mm": 200,
        "width_mm": 68,
    }

    async def get_profile(path, params=None):
        if path in {"/spool-profiles", "/manufacturers"}:
            raise HTTPException(503)
        assert path == "/spool-profiles/735"
        return profile

    monkeypatch.setattr(settings, "filamentdb_url", "https://db.filaman.app")
    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", get_profile)
    return profile


@pytest.fixture
def filament():
    return Filament(
        manufacturer=Manufacturer(name="Paramount 3D"), designation="PLA",
        material_type="PLA", diameter_mm=1.75, raw_material_weight_g=1000,
        manufacturer_sku="PM-PLA", filamentdb_spool_profile_id=735,
    )


@pytest.mark.parametrize("image_file", ["735.png", "../735.png"])
async def test_edit_uses_only_the_exact_linked_profile(
    installed, db_session, catalog_requests, linked_profile, filament, image_file,
):
    linked_profile["image_file"] = image_file
    db_session.add(filament)
    await db_session.commit()
    filament_id, token = filament.id, filament.record_token
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "edit", "filament_id": filament_id, "record_token": token, "spool_lookup": True,
    })

    assert response.status_code == 200, response.text
    card, = response.json()["spool_profiles"]
    assert card == {
        "id": "filamandb-735",
        "title": "Paramount 170 g Spool",
        "source": "FilaManDB",
        "url": "https://db.filaman.app/api/v1/spool-profiles/735",
        "empty_weight_g": 170,
        "outer_diameter_mm": 200,
        "width_mm": 68,
        "package_weights_g": [],
        **({"photo": "/api/v1/filamentdb/spool-profile-image?image_file=735.png"} if image_file == "735.png" else {}),
    }
    saved = await db_session.scalar(select(Filament).where(Filament.id == filament_id))
    assert saved.record_token == token and saved.default_spool_weight_g is None
    assert catalog_requests == []


@pytest.mark.parametrize(("guidance", "alternative"), [
    ("My spool is clear plastic with no slots.", 232),
    ("Cardboard", None),
    ("Please verify the printable temperature range.", None),
])
async def test_edit_keeps_the_linked_profile_and_compares_described_designs(
    installed, db_session, catalog_requests, linked_profile, filament, guidance, alternative,
):
    db_session.add(filament)
    await db_session.commit()
    filament_id, token = filament.id, filament.record_token
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "edit", "filament_id": filament_id, "record_token": token,
        "spool_lookup": True, "guidance": guidance,
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["spool_profiles"][0]["id"] == "filamandb-735"
    assert [card["empty_weight_g"] for card in result["spool_profiles"][1:]] == ([alternative] if alternative else [])
    assert result["suggestions"] == []
    if alternative:
        assert "compare" in result["spool_reply"].lower()
    saved = await db_session.scalar(select(Filament).where(Filament.id == filament_id))
    assert saved.filamentdb_spool_profile_id == 735 and saved.record_token == token
    assert saved.default_spool_weight_g is None


async def test_wrong_profile_id_falls_back_to_public_catalogs(
    installed, db_session, catalog_requests, linked_profile, filament,
):
    linked_profile["id"] = 736
    db_session.add(filament)
    await db_session.commit()
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "edit", "filament_id": filament.id, "spool_lookup": True,
    })
    assert response.status_code == 200, response.text
    assert {card["empty_weight_g"] for card in response.json()["spool_profiles"]} == {208, 232}
    assert len(catalog_requests) == 2


@pytest.mark.parametrize(("question", "linked"), [
    ("What does my spool look like?", True),
    ("How much does my spool weigh empty?", True),
    ("What is the hole diameter of my spool?", True),
    ("What does the Paramount 3D PLA spool look like?", True),
    ("What is the tare of the Paramount 3D PLA 1,000 g spool?", True),
    ("What is the tare of the Elegoo PETG 1 kg spool?", False),
    ("What does my spool look like compared with the Elegoo PETG 1 kg spool?", False),
])
async def test_chat_uses_the_linked_profile_for_questions_about_the_selected_filament(
    installed, db_session, catalog_requests, linked_profile, filament, question, linked,
):
    db_session.add(filament)
    await db_session.commit()
    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200

    response = await installed.post(f"{BASE}/chat", json={
        "filament_id": filament.id,
        "messages": [{"role": "user", "content": question}],
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert any(card["id"] == "filamandb-735" for card in result["spool_profiles"]) is linked
    if linked:
        assert "170 g empty" in result["reply"]
        assert catalog_requests == []
    else:
        assert result["spool_profiles"]
        assert len(catalog_requests) == 2


@pytest.mark.parametrize(("follow_up", "current_filament", "linked"), [
    ("How wide is it?", False, True),
    ("How wide is it?", True, True),
    ("What is the tare of the Paramount 3D PLA spool?", True, False),
])
async def test_selected_spool_replay_uses_its_lookup_filament(
    installed, db_session, catalog_requests, linked_profile, filament, follow_up, current_filament, linked,
):
    other = Filament(
        manufacturer=Manufacturer(name="Elegoo"), designation="PETG", material_type="PETG",
        diameter_mm=1.75, raw_material_weight_g=1000,
    )
    db_session.add_all([filament, other])
    await db_session.commit()
    filament_id, other_id = filament.id, other.id
    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200
    question = {"role": "user", "content": "What does my spool look like?"}
    first = await installed.post(f"{BASE}/chat", json={"filament_id": filament_id, "messages": [question]})
    assert first.status_code == 200
    card, = first.json()["spool_profiles"]
    assert (card["id"], card["width_mm"]) == ("filamandb-735", 68)

    linked_profile["width_mm"] = 70
    db_session.expunge_all()
    response = await installed.post(f"{BASE}/chat", json={
        "filament_id": other_id if current_filament else None,
        "spool_lookup_filament_id": filament_id,
        "spool_lookup_message_index": 1,
        "selected_spool_profile_id": card["id"],
        "messages": [question, {"role": "assistant", "content": first.json()["reply"]},
                     {"role": "user", "content": follow_up}],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    if linked:
        assert result.get("selected_spool_profile_id") == card["id"]
        verified, = result["spool_profiles"]
        assert verified["width_mm"] == 70
        assert catalog_requests == []
    else:
        assert "selected_spool_profile_id" not in result
        assert result["spool_profiles"]
        assert all(profile["id"] != card["id"] for profile in result["spool_profiles"])
        assert len(catalog_requests) == 2


@pytest.mark.parametrize(("selected", "saved_weight"), [(False, None), (True, None), (True, 1.2)])
async def test_chat_clarifies_ambiguous_spool_package_weight(
    installed, db_session, catalog_requests, linked_profile, filament, selected, saved_weight,
):
    filament.raw_material_weight_g = saved_weight
    db_session.add(filament)
    await db_session.commit()
    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200

    response = await installed.post(f"{BASE}/chat", json={
        "filament_id": filament.id if selected else None,
        "messages": [{"role": "user", "content": "What is the tare of the Paramount 3D PLA 1.200 g spool?"}],
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert "without a thousands separator" in result["reply"]
    assert result["spool_profiles"] == []
    assert catalog_requests == []


async def test_create_spool_lookup_still_flags_duplicate_filament(
    installed, db_session, catalog_requests, filament,
):
    db_session.add(filament)
    await db_session.commit()
    filament_id = filament.id
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "spool_lookup": True, "sku_hint": "PM-PLA",
        "identity": {"designation": "Paramount PLA", "manufacturer": "Paramount 3D"},
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["issues"] == [{"code": "duplicate", "filament_id": filament_id}]
    assert result["spool_profiles"]


async def test_chat_uses_saved_identity_for_unlinked_filament(
    installed, db_session, catalog_requests, filament,
):
    filament.filamentdb_spool_profile_id = None
    db_session.add(filament)
    await db_session.commit()
    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200

    response = await installed.post(f"{BASE}/chat", json={
        "filament_id": filament.id,
        "messages": [{"role": "user", "content": "What does my spool look like?"}],
    })

    assert response.status_code == 200, response.text
    assert {card["empty_weight_g"] for card in response.json()["spool_profiles"]} == {208, 232}
    assert len(catalog_requests) == 2


async def test_chat_accepts_style_after_weight_clarification(installed, catalog_requests, monkeypatch):
    from filabrain import research

    async def check_model(*_args, **_kwargs):
        pass

    async def complete(*_args, **_kwargs):
        return "Ordinary chat answer."

    monkeypatch.setattr(research, "check_model_catalog", check_model)
    monkeypatch.setattr(research, "request_completion", complete)
    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200
    first_question = "What is the tare of the Paramount 3D 2 kg spool?"
    first = await installed.post(f"{BASE}/chat", json={
        "messages": [{"role": "user", "content": first_question}],
    })
    assert first.status_code == 200, first.text

    messages = [
        {"role": "user", "content": first_question},
        {"role": "assistant", "content": first.json()["reply"]},
        {"role": "user", "content": "1 kg"},
    ]
    second = await installed.post(f"{BASE}/chat", json={"messages": messages})
    assert second.status_code == 200, second.text
    assert second.json()["reply"].endswith("cutouts?")

    messages.extend([
        {"role": "assistant", "content": second.json()["reply"]},
        {"role": "user", "content": "Clear plastic with no slots."},
    ])
    third = await installed.post(f"{BASE}/chat", json={"messages": messages})
    assert third.status_code == 200, third.text
    assert [card["empty_weight_g"] for card in third.json().get("spool_profiles", [])] == [232]
    assert "232 g empty" in third.json()["reply"]


async def test_edit_compares_distinct_filamandb_alternatives(
    installed, db_session, filament, linked_profile, catalog_requests, monkeypatch,
):
    db_session.add(filament)
    await db_session.commit()
    profiles = [{**linked_profile, "spool_material": "Cardboard"}, {
        "id": 999, "name": "Cardboard Spool", "spool_material": "Cardboard",
        "image_file": "999.png", "empty_weight_g": 232,
    }]

    async def catalog(path, params=None):
        if path == "/spool-profiles/735":
            return profiles[0]
        if path == "/manufacturers":
            return {"items": [{"id": 618, "name": "Paramount 3D"}], "total": 1}
        if path == "/filaments":
            return {"items": [{
                "manufacturer": {"name": "Paramount 3D"}, "designation": "PLA",
                "material": {"name": "PLA"}, "nominal_weight_g": 1000,
                "spool_profile_id": profile["id"], "spool_profile": profile,
            } for profile in profiles], "total": 2}
        return {"items": [], "total": 0}

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", catalog)
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "edit", "filament_id": filament.id, "spool_lookup": True, "guidance": "It is cardboard.",
    })
    assert response.status_code == 200, response.text
    assert [card["id"] for card in response.json()["spool_profiles"]] == ["filamandb-735", "filamandb-999"]


@pytest.mark.parametrize("stored_weight", [1000, None])
async def test_edit_excludes_known_incompatible_package(
    installed, db_session, filament, linked_profile, monkeypatch, stored_weight,
):
    from filabrain import spool_profiles

    filament.filamentdb_spool_profile_id = None
    filament.designation = "Special PLA"
    filament.raw_material_weight_g = stored_weight
    db_session.add(filament)
    await db_session.commit()

    async def catalog(url):
        if url != spool_profiles.CATALOGS[0]:
            return []
        common = {"source": "The Empty Spool", "url": "https://theemptyspool.cc/brand/paramount-3d"}
        return [
            ({**common, "id": "3-kg", "title": "Paramount 3D Special PLA",
              "net_weight_g": 3000, "empty_weight_g": 322}, "", "plastic"),
            ({**common, "id": "1-kg", "title": "Paramount 3D PLA",
              "net_weight_g": 1000, "empty_weight_g": 150}, "", "plastic"),
        ]

    monkeypatch.setattr(spool_profiles, "_catalog", catalog)
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "edit", "filament_id": filament.id, "spool_lookup": True,
        "spool_package_weight_g": 1000,
    })
    assert response.status_code == 200, response.text
    assert [card["id"] for card in response.json()["spool_profiles"]] == ["1-kg"]
