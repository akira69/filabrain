import pytest
import pytest_asyncio
from app.api.v1 import filamentdb_proxy
from app.core.cache import response_cache
from app.core.security import Principal
from app.models import Filament, InstalledPlugin, Manufacturer
from fastapi import HTTPException
from sqlalchemy import select
from test_backend import BASE, installed

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio


def empty_row(key, brand_href, label, tare, net="1 kg", photo=""):
    return f"""
        <tr class="spool-row" data-spool-id="{key}">
          <td><img src="https://logo.example/favicon.png"><a href="{brand_href}">Brand</a></td>
          <td>{'<a class="spool-photo-trigger" href="' + photo + '">Photo</a>' if photo else ''}</td>
          <td>{tare} g</td><td>{net}</td><td></td>
          <td><button data-spool-id="{key}" data-spool-weight="{tare}"
            data-spool-label="{label}">Weighed it?</button></td>
        </tr>
    """


def only_card(slug, title, facts, photo, style="dark_plastic"):
    return f"""
        <div class="masonry-item" data-spool-type={style}>
          <div class=box-masonry>
            <a href=https://www.onlyspoolz.com/portfolio/{slug}/><img src={photo}></a>
            <div class=box-masonry-text>
              <h4><a href=https://www.onlyspoolz.com/portfolio/{slug}/>{title}</a></h4>
              <div class=box-masonry-description><p><ul>
                {''.join(f"<li>{fact}</li>" for fact in facts)}
              </ul></p></div>
            </div>
          </div>
        </div>
    """


EMPTY = "<table>" + "".join([
    empty_row(263, "/brand/paramount-3d", "Paramount 3D — 1 kg Plastic", 208),
    empty_row(264, "/brand/paramount-3d", "Paramount 3D — 1 kg Plastic (Clear, No Slots)", 232),
    empty_row(101, "/brand/elegoo", "Elegoo — Rapid PETG 3 kg Plastic", 322, net="3 kg"),
    empty_row(102, "/brand/elegoo", "Elegoo — Rapid PETG 1 kg Cardboard", 153),
    empty_row(103, "/brand/elegoo", "Elegoo — 1 kg Plastic (PLA Basic)", 233,
              photo="/static/spools/elegoo--1-kg-plastic-pla-basic.webp?v=820f862e97f1"),
    empty_row(300, "/brand/the-filament", "The Filament — 1 kg Plastic", 200),
    empty_row(100, "/brand/elegoo", "Elegoo — 1 kg Cardboard", 156,
              photo="/static/spools/elegoo--1-kg-cardboard.webp?v=123456"),
]) + "</table>"
ONLY = only_card(
    "spool-elegoo-petg", "Elegoo PETG 1KG",
    ["Printing temp: 230 C – 260 C", "Empty spool weight: 114 g", "Advertised filament weight: 1000 g",
     "Outside Diameter: 197 mm", "Height: 59 mm", "Hole Diameter: 73 mm"],
    "/img/portfolio/spool_elegoo_petg_1kg_1_hu_e51267f497ada0dd.webp",
) + only_card(
    "spool-professional-lab-petg-white-1kg", "Professional Lab PETG White 1KG",
    ["Empty spool weight: 145 g", "Advertised filament weight: 1000 g",
     "Outside Diameter: 197.9 mm", "Height: 63.5 mm", "Hole Diameter: 57.7 mm"],
    "https://other.example/logo.webp",
) + "".join(
    only_card(slug, title,
              [f"Empty spool weight: {tare} g", "Advertised filament weight: 1000 g",
               "Outside Diameter: 200 mm", f"Height: {width} mm", f"Hole Diameter: {hole} mm"],
              photo, style)
    for slug, title, tare, width, hole, photo, style in [
        ("spool-elegoo-rapid-pla-plus", "Elegoo Rapid PLA+", 156, 56, 53,
         "/img/portfolio/spool_elegoo_rapid_pla_plus_hu_16932afdb5408602.webp", "cardboard"),
        ("spool-elegoo-matte-pla-black", "Elegoo Matte PLA Black 1KG", 145, 64, 55,
         "/img/portfolio/spool_elegoo_matte_pla_black_1kg_hu_93e98d6eddb6fe6d.webp", "cardboard"),
        ("spool-elegoo-pla-basic", "Elegoo PLA Basic 1KG", 233, 66, 58,
         "/img/portfolio/spool_elegoo_pla_basic_1kg_hu_20ab69788c44f50a.webp", "light_plastic"),
        ("spool-elegoo-pla", "Elegoo PLA", 160, 64, 53,
         "/img/portfolio/spool_elegoo_pla_hu_8af5b99796d0f1d8.webp", "dark_plastic"),
    ]
)
PAGES = {
    ("theemptyspool.cc", "/"): EMPTY,
    ("www.onlyspoolz.com", "/portfolio/"): ONLY,
}


@pytest.fixture
def catalog_requests(monkeypatch):
    from filabrain import sources

    async def unavailable_filamandb(*_args, **_kwargs):
        raise HTTPException(503)

    def read(url, _address, _deadline, *, max_bytes):
        requests.append((url.hostname, url.path))
        assert url.scheme == "https" and not url.query
        body = PAGES[url.hostname, url.path].encode()
        assert len(body) < max_bytes <= 2 * 1024**2
        return 200, "", "text/html", body

    requests = []
    monkeypatch.setattr(sources, "_public_address", lambda host, port: "1.1.1.1")
    monkeypatch.setattr(sources, "_get_pinned", read)
    monkeypatch.setattr(response_cache, "_store", {})
    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", unavailable_filamandb)

    return requests


@pytest_asyncio.fixture
async def chat(db_session, monkeypatch, catalog_requests):
    from filabrain import research
    from filabrain.settings import SettingsInput

    db_session.add(InstalledPlugin(
        plugin_key="filabrain", name="FilaBrain", version="1.0",
        plugin_type="integration", is_active=True,
    ))
    await db_session.commit()
    config = SettingsInput(model_url="http://model.test", model_id="test-model").model_dump()

    async def config_record(_db):
        return None, config

    async def check_model_catalog(*_args):
        pass

    async def complete(*_args, **_kwargs):
        return "Ordinary chat answer."

    monkeypatch.setattr(research, "config_record", config_record)
    monkeypatch.setattr(research, "check_model_catalog", check_model_catalog)
    monkeypatch.setattr(research, "request_completion", complete)

    async def ask(question, *, prior=None, scopes=None, auth_type="session"):
        messages = [] if prior is None else [
            {"role": "user", "content": prior[0]}, {"role": "assistant", "content": prior[1]},
        ]
        data = research.ChatRequest(messages=[*messages, {"role": "user", "content": question}])
        principal = Principal(auth_type=auth_type, user_id=1, is_superadmin=True, scopes=scopes)
        return await research.research_chat(data, db_session, principal)

    return ask, catalog_requests


async def test_tare_candidates_keep_paramount_designs_separate(chat):
    ask, _ = chat
    result = await ask("What is the empty spool weight for Paramount 3D?")
    assert result["action"] is None
    assert result["reply"].count("?") == 1
    assert [(item["title"], item["empty_weight_g"]) for item in result["spool_profiles"]] == [
        ("Paramount 3D — 1 kg Plastic", 208),
        ("Paramount 3D — 1 kg Plastic (Clear, No Slots)", 232),
    ]
    for item in result["spool_profiles"]:
        assert item["source"] == "The Empty Spool"
        assert item["url"] == "https://theemptyspool.cc/brand/paramount-3d"
        assert item["net_weight_g"] == 1000
        assert "photo" not in item and "outer_diameter_mm" not in item
    assert len({item["id"] for item in result["spool_profiles"]}) == 2


@pytest.mark.parametrize("follow_up", [
    "It's the clear plastic one with no slots.", "Is it the clear plastic one?",
    "Mine is made from clear plastic and has no slots.",
])
async def test_visual_follow_up_narrows_the_catalog_style(chat, follow_up):
    ask, requests = chat
    question = "What's the tare on a Paramount spool?"
    first = await ask(question)
    second = await ask(follow_up, prior=(question, first["reply"]))
    assert [item["empty_weight_g"] for item in second["spool_profiles"]] == [232]
    assert "232 g" in second["reply"]
    assert len(requests) == 2


async def test_dimensions_and_photo_belong_to_the_specific_match(chat):
    ask, _ = chat
    result = await ask("What are the spool dimensions for the Elegoo PETG 1 kg plastic spool?")
    assert "197 mm" in result["reply"] and "?" not in result["reply"]
    assert result["spool_profiles"] == [{
        "id": "onlyspoolz-spool-elegoo-petg",
        "title": "Elegoo PETG 1KG",
        "source": "OnlySpoolz",
        "url": "https://www.onlyspoolz.com/portfolio/spool-elegoo-petg/",
        "photo": "https://www.onlyspoolz.com/img/portfolio/spool_elegoo_petg_1kg_1_hu_e51267f497ada0dd.webp",
        "empty_weight_g": 114,
        "outer_diameter_mm": 197,
        "width_mm": 59,
        "hole_diameter_mm": 73,
        "net_weight_g": 1000,
    }]


async def test_filament_color_does_not_verify_the_reel_color(chat):
    ask, _ = chat
    question = 'What does my 1 kg Elegoo PLA spool weigh empty?'
    first = await ask(question)
    answer = await ask('Black cardboard', prior=(question, first['reply']))
    assert answer['reply'].endswith('?')
    assert any(card['empty_weight_g'] == 145 for card in answer['spool_profiles'])


async def test_unverified_image_host_is_omitted_but_measurements_are_kept(chat):
    ask, _ = chat
    result = await ask("What does the Professional Lab PETG White spool look like?")
    assert len(result["spool_profiles"]) == 1
    item = result["spool_profiles"][0]
    assert "photo" not in item
    assert (item["outer_diameter_mm"], item["width_mm"], item["hole_diameter_mm"]) == (197.9, 63.5, 57.7)


async def test_inventory_chat_and_permission_failures_do_not_fetch_catalogs(chat):
    ask, requests = chat
    with pytest.raises(HTTPException, match="Filament read access is required"):
        await ask("What is the tare of a Paramount spool?", scopes=["spools:read"])
    with pytest.raises(HTTPException, match="A browser session is required"):
        await ask("What is the tare of a Paramount spool?", auth_type="api_key")
    result = await ask("What empty spools do I have in my inventory?")
    assert "spool_profiles" not in result
    navigation = await ask("Open a new spool page so I can enter the missing tare")
    assert "spool_profiles" not in navigation
    assert requests == []


@pytest.mark.parametrize("weight", ["5 kg", "5,000 g", "5,0 kg"])
async def test_visual_lookup_respects_requested_package_and_caps_candidates(chat, monkeypatch, weight):
    ask, _ = chat
    additional = [
        empty_row(200 + size, "/brand/polymer", f"Polymer — {size} kg Plastic", 100 + size, net=f"{size} kg")
        for size in range(1, 8)
    ]
    monkeypatch.setitem(PAGES, ("theemptyspool.cc", "/"), "<table>" + "".join(additional) + "</table>")
    broad = await ask("Can you show spool photos for Polymer?")
    assert len(broad["spool_profiles"]) == 4
    narrow = await ask(f"What is the empty weight of the Polymer {weight} spool?")
    assert [item["net_weight_g"] for item in narrow["spool_profiles"]] == [5000]
    assert "105 g empty" in narrow["reply"]


async def test_a_source_outage_keeps_the_other_sites_profiles(chat, monkeypatch):
    from filabrain import sources

    ask, _ = chat
    original = sources._get_pinned

    def outage(url, *args, **kwargs):
        if url.hostname == "www.onlyspoolz.com":
            raise TimeoutError()
        return original(url, *args, **kwargs)

    monkeypatch.setattr(sources, "_get_pinned", outage)
    result = await ask("How much does an empty Paramount 3D spool weigh?")
    assert [item["empty_weight_g"] for item in result["spool_profiles"]] == [208, 232]


async def test_create_proposal_uses_the_draft_identity_and_ignores_other_products_in_guidance(installed, catalog_requests):
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "spool_lookup": True,
        "identity": {"manufacturer": "Elegoo", "designation": "PETG", "weight_g": 1000},
        "guidance": "The other spool was Professional Lab PETG White 1 kg.",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["target"]["identity"]["manufacturer"] == "Elegoo"
    assert result["suggestions"] == [] and result["issues"] == []
    assert {item["empty_weight_g"] for item in result["spool_profiles"]} == {114, 153}
    assert result["spool_reply"]
    assert set(catalog_requests) == set(PAGES)


@pytest.mark.parametrize("mode", ["chat", "create", "edit"])
async def test_unlinked_spool_search_includes_valid_filamandb_candidates(
    installed, db_session, catalog_requests, monkeypatch, mode,
):
    calls = []
    profile = {
        "id": 735, "name": "Paramount 3D PLA Spool", "image_file": "735.png",
        "empty_weight_g": 170, "net_weight_g": 1000, "outer_diameter_mm": 200,
    }

    async def lookup(path, params):
        if path == "/manufacturers":
            raise HTTPException(503)
        calls.append((path, params["search"]))
        return {"items": [
            profile,
            dict(profile, id=736, is_active=False),
            dict(profile, id=True),
            dict(profile, id=738, image_file="../738.png", empty_weight_g=1, outer_diameter_mm=601),
            dict(profile, id=739, name="Unrelated PLA"),
        ]}

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", lookup)
    if mode == "chat":
        assert (await installed.put(f"{BASE}/settings", json={
            "model_url": "http://model.test", "model_id": "example",
        })).status_code == 200
        response = await installed.post(f"{BASE}/chat", json={
            "messages": [{"role": "user", "content": "What does the Paramount 3D PLA 1 kg spool look like?"}],
        })
    else:
        data = {"mode": mode, "spool_lookup": True}
        if mode == "create":
            data["identity"] = {"manufacturer": "Paramount 3D", "designation": "PLA", "weight_g": 1000}
        else:
            filament = Filament(manufacturer=Manufacturer(name="Paramount 3D"), designation="PLA",
                                material_type="PLA", diameter_mm=1.75, raw_material_weight_g=1000)
            db_session.add(filament)
            await db_session.commit()
            data["filament_id"] = filament.id
        response = await installed.post(f"{BASE}/proposals", json=data)

    assert response.status_code == 200, response.text
    result = response.json()
    assert len(calls) == 1 and calls[0][0] == "/spool-profiles"
    assert "paramount 3d" in calls[0][1].lower()
    assert result["spool_profiles"] == [{
        "id": "filamandb-735", "title": "Paramount 3D PLA Spool", "source": "FilaManDB",
        "url": "https://db.filaman.app/api/v1/spool-profiles/735",
        "photo": "/api/v1/filamentdb/spool-profile-image?image_file=735.png",
        "empty_weight_g": 170, "net_weight_g": 1000, "outer_diameter_mm": 200,
    }]
    if mode == "chat":
        assert result["action"] is None and "your spool" in result["reply"]
    else:
        assert result["suggestions"] == [] and "your spool" in result["spool_reply"]


@pytest.mark.parametrize("mode", ["chat", "create"])
async def test_filamandb_uses_the_spool_linked_to_a_matching_filament(
    installed, catalog_requests, monkeypatch, mode,
):
    profile = {
        "id": 735, "name": "170g Spool", "image_file": "735.png", "spool_material": "Cardboard",
        "empty_weight_g": 170, "outer_diameter_mm": 200, "width_mm": 68, "is_active": True,
    }
    row = {
        "designation": "Cold White", "manufacturer": {"name": "eSUN"}, "material": {"name": "PLA"},
        "nominal_weight_g": 1000, "spool_profile_id": 735, "spool_profile": profile,
    }

    async def lookup(path, params):
        if path == "/manufacturers":
            assert params["search"].casefold() == "esun"
            return {"items": [{"id": 276, "name": "eSUN"}]}
        if path == "/filaments":
            assert params["manufacturer_id"] == 276
            assert params["search"].casefold() == "cold white"
            return {"items": [
                row,
                dict(row, designation="Ice Blue", spool_profile_id=736, spool_profile=dict(profile, id=736)),
                dict(row, nominal_weight_g=250, spool_profile_id=737, spool_profile=dict(profile, id=737)),
                dict(row, spool_profile_id=738),
                dict(row, manufacturer={"name": "Other"}, spool_profile_id=739, spool_profile=dict(profile, id=739)),
                dict(row, material={"name": "ABS"}, spool_profile_id=740, spool_profile=dict(profile, id=740)),
            ]}
        assert path == "/spool-profiles"
        raise HTTPException(503)

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", lookup)
    if mode == "chat":
        assert (await installed.put(f"{BASE}/settings", json={
            "model_url": "http://model.test", "model_id": "example",
        })).status_code == 200
        response = await installed.post(f"{BASE}/chat", json={
            "messages": [{"role": "user", "content": "What does the eSUN Cold White PLA 1 kg spool look like?"}],
        })
    else:
        response = await installed.post(f"{BASE}/proposals", json={
            "mode": "create", "spool_lookup": True, "identity": {
                "manufacturer": "eSUN", "designation": "Cold White", "material": "PLA", "weight_g": 1000,
            },
        })
    assert response.status_code == 200, response.text
    card, = response.json()["spool_profiles"]
    assert card["id"] == "filamandb-735"
    assert card["net_weight_g"] == 1000
    assert card["photo"] == "/api/v1/filamentdb/spool-profile-image?image_file=735.png"


@pytest.mark.parametrize(("weights", "package_evidence", "matching_weight"), [
    ((1000, 1000), {"net_weight_g": 1000}, 1000),
    ((1000, 500), {"package_weights_g": [500, 1000]}, 500),
    ((None, 0), {"package_weights_g": []}, 500),
])
async def test_filamandb_preserves_the_package_weights_associated_with_a_reel(
    installed, catalog_requests, monkeypatch, weights, package_evidence, matching_weight,
):
    profile = {"id": 735, "name": "eSUN PLA Spool", "empty_weight_g": 170}

    async def lookup(path, params):
        if path == "/manufacturers":
            return {"items": [{"id": 276, "name": "eSUN"}]}
        if path == "/filaments":
            return {"items": [{
                "designation": "Cold White", "manufacturer": {"name": "eSUN"}, "material": {"name": "PLA"},
                "nominal_weight_g": weight, "spool_profile_id": 735, "spool_profile": profile,
            } for weight in weights]}
        assert path == "/spool-profiles"
        return {"items": [profile]}

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", lookup)
    request = {"mode": "create", "spool_lookup": True, "identity": {
        "manufacturer": "eSUN", "designation": "Cold White", "material": "PLA",
    }}
    for selected_weight in (None, matching_weight):
        response = await installed.post(f"{BASE}/proposals", json=request | {"spool_package_weight_g": selected_weight})
        assert response.status_code == 200, response.text
        card, = response.json()["spool_profiles"]
        assert card["id"] == "filamandb-735"
        assert {key: card[key] for key in ("net_weight_g", "package_weights_g") if key in card} == package_evidence

    response = await installed.post(f"{BASE}/proposals", json=request | {"spool_package_weight_g": 250})
    assert response.status_code == 200, response.text
    assert [card["id"] for card in response.json()["spool_profiles"]] == (
        ["filamandb-735"] if package_evidence == {"package_weights_g": []} else []
    )


@pytest.mark.parametrize("source", ["empty", "only"])
@pytest.mark.parametrize(("advertised", "expected"), [
    ("3000 g", []), ("3,000 g", []), ("3 000 g", []), ("3 kg", []), ("1,000 g", [1000]),
])
async def test_catalog_weight_formats_respect_the_filament_package(
    installed, catalog_requests, monkeypatch, source, advertised, expected,
):
    empty = empty_row(133, "/brand/fixture", "Fixture Special PLA", 322, net=advertised)
    only = only_card(
        "spool-fixture-pla", "Fixture Special PLA",
        ["Empty spool weight: 322 g", f"Advertised filament weight: {advertised}"],
        "/img/portfolio/spool_fixture_plastic.webp",
    )
    monkeypatch.setitem(PAGES, ("theemptyspool.cc", "/"), empty if source == "empty" else "")
    monkeypatch.setitem(PAGES, ("www.onlyspoolz.com", "/portfolio/"), only if source == "only" else "")
    result = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "spool_lookup": True,
        "identity": {"manufacturer": "Fixture", "designation": "Special PLA", "material": "PLA", "weight_g": 1000},
    })
    assert result.status_code == 200, result.text
    assert [card.get("net_weight_g") for card in result.json()["spool_profiles"]] == expected


async def test_explicit_sku_drives_catalog_search(monkeypatch):
    from filabrain import catalog

    seen = []
    profile = {"id": 1, "name": "eSUN Cold White PLA Spool", "empty_weight_g": 322}
    matching = dict(manufacturer={"name": "eSUN"}, designation="Cold White", material={"name": "PLA"},
                    nominal_weight_g=1000, sku="FIX-SPOOL", spool_profile_id=1, spool_profile=profile)
    alternate = dict(matching, sku="FIX-REFILL")

    async def find_filaments(search, **kwargs):
        seen.append(search)
        if search == "FIX-SPOOL":
            return {"items": [matching], "total": 1}
        return {"items": [alternate] * 20, "total": 21}

    async def find_profiles(*args, **kwargs):
        return {"items": []}

    monkeypatch.setattr(catalog, "lookup_filaments", find_filaments)
    monkeypatch.setattr(catalog.upstream, "search_spool_profiles", find_profiles)
    matches = await catalog.search_spool_cards("esun cold white", "eSUN", "Cold White", "PLA", {1000}, sku="FIX-SPOOL")
    assert [card["id"] for card, _, _ in matches] == ["filamandb-1"], seen


@pytest.mark.parametrize("mode", ["create-hint", "edit", "chat"])
@pytest.mark.parametrize(("catalog_sku", "expected"), [
    (" fix-spool ", ["filamandb-735"]), ("FIX-REFILL", []), (None, ["filamandb-735"]),
])
async def test_spool_lookup_filters_known_conflicting_skus(
    installed, db_session, catalog_requests, monkeypatch, mode, catalog_sku, expected,
):
    profile = {"id": 735, "name": "eSUN Cold White PLA Spool", "image_file": "735.png", "empty_weight_g": 322}

    async def catalog(path, params=None):
        if path == "/manufacturers":
            return {"items": [{"id": 276, "name": "eSUN"}], "total": 1}
        if path == "/filaments":
            return {"items": [{
                "manufacturer": {"name": "eSUN"}, "designation": "Cold White", "material": {"name": "PLA"},
                "nominal_weight_g": 1000, "sku": catalog_sku, "spool_profile_id": 735, "spool_profile": profile,
            }], "total": 1}
        assert path == "/spool-profiles"
        return {"items": [profile], "total": 1}

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", catalog)
    if mode == "create-hint":
        result = await installed.post(f"{BASE}/proposals", json={
            "mode": "create", "spool_lookup": True, "sku_hint": "FIX-SPOOL",
            "identity": {"manufacturer": "eSUN", "designation": "Cold White", "material": "PLA", "weight_g": 1000},
        })
    else:
        filament = Filament(manufacturer=Manufacturer(name="eSUN"), designation="Cold White",
                            material_type="PLA", diameter_mm=1.75, raw_material_weight_g=1000,
                            manufacturer_sku="FIX-SPOOL")
        db_session.add(filament)
        await db_session.commit()
        filament_id = filament.id
        if mode == "edit":
            result = await installed.post(f"{BASE}/proposals", json={
                "mode": "edit", "filament_id": filament_id, "spool_lookup": True,
            })
        else:
            assert (await installed.put(f"{BASE}/settings", json={
                "model_url": "http://model.test", "model_id": "example",
            })).is_success
            result = await installed.post(f"{BASE}/chat", json={
                "filament_id": filament_id,
                "messages": [{"role": "user", "content": "What does my spool look like?"}],
            })
    assert result.status_code == 200, result.text
    assert [card["id"] for card in result.json()["spool_profiles"]] == expected


async def test_edit_proposal_checks_the_record_before_reading_public_catalogs(installed, db_session, catalog_requests):
    filament = Filament(manufacturer=Manufacturer(name="Paramount 3D"), designation="PLA", material_type="PLA",
                        diameter_mm=1.75, raw_material_weight_g=1000, manufacturer_sku="PM-PLA")
    db_session.add(filament)
    await db_session.commit()
    filament_id, token = filament.id, filament.record_token
    target = {"mode": "edit", "filament_id": filament_id, "spool_lookup": True}

    stale = await installed.post(f"{BASE}/proposals", json=target | {"record_token": "0" * 32})
    assert stale.status_code == 409
    different = await installed.post(f"{BASE}/proposals", json=target | {"sku_hint": "OTHER-PLA"})
    assert different.status_code == 409
    assert catalog_requests == []

    response = await installed.post(f"{BASE}/proposals", json=target | {
        "record_token": token, "guidance": "Clear plastic, no slots.",
    })
    assert response.status_code == 200, response.text
    assert [item["empty_weight_g"] for item in response.json()["spool_profiles"]] == [232]
    saved = await db_session.scalar(select(Filament).where(Filament.id == filament_id))
    assert saved.record_token == token and saved.default_spool_weight_g is None


async def test_missing_brand_asks_for_identification_instead_of_matching_generic_words(chat):
    ask, _ = chat
    result = await ask("What is the tare of the filament spool?")
    assert result["spool_profiles"] == []
    assert "brand and product" in result["reply"]


async def test_unspecified_elegoo_pla_design_surfaces_distinct_catalog_options(chat):
    ask, _ = chat
    result = await ask("What does my 1 kg Elegoo PLA spool weigh empty?")
    assert {profile["empty_weight_g"] for profile in result["spool_profiles"]} == {145, 156, 160, 233}
    assert result["reply"].count("?") == 1


async def test_unlisted_pla_cf_qualifier_is_reported_as_unverified(chat):
    ask, _ = chat
    result = await ask("What is the tare of the Elegoo PLA-CF spool?")
    assert "couldn’t verify" in result["reply"]
    assert result["reply"].count("?") == 1
    assert result["spool_profiles"] and all("PLA-CF" not in item["title"] for item in result["spool_profiles"])


async def test_explicit_material_style_narrows_generic_elegoo_petg(chat):
    ask, _ = chat
    without_style = await ask("What are the spool dimensions for Elegoo PETG 1 kg?")
    assert {item["empty_weight_g"] for item in without_style["spool_profiles"]} == {114, 153}
    cardboard = await ask("What are the spool dimensions for the Elegoo PETG 1 kg cardboard spool?")
    assert [item["empty_weight_g"] for item in cardboard["spool_profiles"]] == [153]
    assert "dimensions are unavailable" in cardboard["reply"]


async def test_named_pla_family_cannot_match_an_explicit_petg_profile(chat, monkeypatch):
    ask, _ = chat
    monkeypatch.setitem(PAGES, ("www.onlyspoolz.com", "/portfolio/"), only_card(
        "spool-sunlu-hs-matte-petg", "Sunlu HS Matte PETG 1KG",
        ["Empty spool weight: 215 g", "Advertised filament weight: 1000 g",
         "Outside Diameter: 210 mm", "Height: 66 mm", "Hole Diameter: 54 mm"],
        "/img/portfolio/sunlu_hs_matte_petg.webp", "cardboard",
    ))
    result = await ask("What is the empty spool weight for Sunlu Basic ePLA Matte?")
    assert result["spool_profiles"] == []
    assert "couldn’t find a matching listing" in result["reply"]


@pytest.mark.parametrize("lookup", ["What's the tare on a Paramount spool?", "What's the tare on this spool?"])
@pytest.mark.parametrize("follow_up", [
    "What paints stick to clear plastic?", "Tell me about drying PETG", "Tell me about clear plastic",
    "Nozzle temperature for PETG", "Change the subject to nozzle temperatures.",
])
async def test_spool_question_does_not_capture_an_unrelated_follow_up(chat, lookup, follow_up):
    ask, _ = chat
    first = await ask(lookup)
    result = await ask(follow_up, prior=(lookup, first["reply"]))
    assert "spool_profiles" not in result


@pytest.mark.parametrize("label", ["The label says Paramount 3D", "paramount 3d", "It's Paramount 3D"])
async def test_identification_question_accepts_a_manufacturer_label(chat, label):
    ask, _ = chat
    question = "What's the tare on this spool?"
    first = await ask(question)
    result = await ask(label, prior=(question, first["reply"]))
    assert {card["empty_weight_g"] for card in result["spool_profiles"]} == {208, 232}


@pytest.mark.parametrize("hint,is_duplicate", [("FIX-REFILL", False), ("FIX-SPOOL", True)])
async def test_sku_hint_agrees_between_research_spool_lookup_and_save(
    installed, db_session, catalog_requests, monkeypatch, hint, is_duplicate,
):
    from filabrain import proposals

    async def catalog(_identity):
        return [{
            "name": "Cold White", "manufacturer": {"name": "eSUN"}, "material": {"key": "PLA"},
            "sku": hint, "diameter_mm": 1.75, "nominal_weight_g": 1000, "density_g_cm3": 1.24,
        }], "https://catalog.example/pla", True

    async def ofd(_manufacturer):
        return [], ""

    async def no_website(_manufacturer):
        return None

    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "manufacturer_website", no_website)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="eSUN")
    db_session.add(Filament(
        manufacturer=maker, designation="Cold White", material_type="PLA", diameter_mm=1.75,
        manufacturer_sku="FIX-SPOOL", raw_material_weight_g=1000,
    ))
    await db_session.commit()
    manufacturer_id = maker.id
    request = {"mode": "create", "sku_hint": hint, "identity": {
        "manufacturer": "eSUN", "designation": "Cold White", "material": "PLA", "diameter_mm": 1.75,
    }}
    spool = await installed.post(f"{BASE}/proposals", json=request | {"spool_lookup": True})
    assert spool.status_code == 200, spool.text
    assert any(issue["code"] == "duplicate" for issue in spool.json()["issues"]) is is_duplicate
    await db_session.rollback()

    specs = await installed.post(f"{BASE}/proposals", json=request)
    assert specs.status_code == 200, specs.text
    await db_session.rollback()

    saved = await installed.post(f"{BASE}/saved-filaments/review-hint", json={
        "manufacturer_id": manufacturer_id, "designation": "Cold White", "material_type": "PLA",
        "diameter_mm": 1.75, "raw_material_weight_g": 1000, "manufacturer_sku": hint,
    })
    assert saved.status_code == (409 if is_duplicate else 200), saved.text
    assert any(issue["code"] == "duplicate" for issue in specs.json()["issues"]) is is_duplicate, specs.text
