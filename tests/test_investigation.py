import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from filabrain import investigation as research
from filabrain.proposals import Identity
from filabrain.sources import Evidence

OLD = "https://store.example/products/tpu"
CORRECT = "https://store.example/products/tpu-90a"
PDF = "https://docs.example/90a.pdf"


def test_publisher_variants_match_exact_family_and_color_with_package_choices():
    from filabrain.proposals import _investigation_variants

    base = "https://shop.example.test/products/pla"
    choices = (
        ("PLA Basic - Brand Green (10501) / Filament with spool / 1kg", base + "?id=3"),
        ("PLA Basic - Brand Green (10501) / Refill / 1kg", base + "?id=4"),
        ("PLA Basic - Bright Green (10503) / Refill / 1kg", base + "?id=5"),
        ("PLA Matte - Brand Green (10502) / Refill / 1kg", base + "?id=6"),
    )
    publisher = Evidence(base, "PLA Basic", "", variants=choices)
    retailer = Evidence(OLD, "PLA Basic", "", variants=((choices[0][0], OLD + "?id=9"),))
    identity = Identity(manufacturer="Example", designation="Brand Green", material="PLA Basic", color="00AE42")
    assert _investigation_variants(identity, [retailer, publisher], "example.test") == [
        {"name": name, "url": url, "code": "10501", "publisher_page": base} for name, url in choices[:2]
    ]

@pytest.mark.asyncio
async def test_model_chooses_second_search_after_wrong_grade(monkeypatch):
    index = "https://store.example/catalog"
    selection = CORRECT + "?variant=1"
    model = AsyncMock(side_effect=list(map(json.dumps, [
        {"action": "open", "url": OLD},
        {"action": "search", "query": "TPU 90A Gray"},
        {"action": "open", "url": index},
        {"action": "open", "url": CORRECT},
        {"action": "open", "url": selection},
        {"action": "open", "url": PDF},
        {"action": "finish", "status": "complete", "suggestions": [
            {"field": "bed_temp_range_c", "value": {"min": 0, "max": 60},
             "source": 5, "quote": "Bed temperature 0–60°C"},
        ]},
    ])))
    fetch = AsyncMock(side_effect=[
        Evidence(OLD, "TPU 95A", "TPU 95A"),
        Evidence(index, "Catalog", "Catalog", product_links=(CORRECT,)),
        Evidence(CORRECT, "TPU 90A", "Colors", product_links=(selection,)),
        Evidence(selection, "TPU 90A", "TPU 90A Gray", datasheets=(PDF,)),
        Evidence(PDF, "TPU 90A TDS", "Bed temperature 0–60°C", datasheet=True),
    ])
    monkeypatch.setattr(research, "request_completion", model)
    monkeypatch.setattr(research, "fetch_supplier", fetch)
    search = AsyncMock(return_value=[index])
    monkeypatch.setattr(research, "search_web", search)
    result = await research.investigate(Identity(material="TPU", designation="90A Gray"),
        start_urls=(OLD,), endpoint={"model_url": "http://model.test"}, model="fixture",
        search_url="http://search.test", max_bytes=1024, deadline=asyncio.get_running_loop().time() + 5,
        publisher_host="example.test")
    assert [call.args[0] for call in fetch.await_args_list] == [OLD, index, CORRECT, selection, PDF]
    assert json.loads(model.await_args_list[0].args[3][-1]["content"])["manufacturer_site_hint"] == "example.test"
    assert "TPU 95A" in model.await_args_list[1].args[3][-1]["content"]
    search.assert_awaited_once_with("http://search.test", "TPU 90A Gray", limit=8)
    assert result.status == "complete"
    assert result.evidence[4].url == PDF
    assert result.suggestions[0]["source"] == 5
    assert "Bed temperature 0–60°C" in model.await_args.args[3][-1]["content"]

@pytest.mark.asyncio
async def test_rejects_unoffered_url_and_fabricated_evidence(monkeypatch):
    monkeypatch.setattr(research, "request_completion", AsyncMock(side_effect=[
        '{"action":"open","url":"http://127.0.0.1/private"}',
        '{"action":"finish","status":"complete","suggestions":[{"source": 42,"quote":"forged"}]}',
    ]))
    fetch = AsyncMock()
    monkeypatch.setattr(research, "fetch_supplier", fetch)
    result = await research.investigate(Identity(manufacturer="Example"), start_urls=(),
        endpoint={"model_url": "http://model.test"}, model="fixture",
        search_url="http://search.test", max_bytes=1024, deadline=asyncio.get_running_loop().time() + 5)
    fetch.assert_not_awaited()
    assert result.suggestions == []

@pytest.mark.asyncio
@pytest.mark.parametrize("ask_for_clue", [False, True])
async def test_rejected_product_stops_at_budget_or_clue(monkeypatch, ask_for_clue):
    rejected = json.dumps({"action": "finish", "status": "complete", "product_source": 1,
                           "suggestions": [{"field": "manufacturer_sku", "value": "WRONG", "source": 1}]})
    model_turns = (
        [rejected, "", json.dumps({"action": "finish", "status": "needs_clue"})] if ask_for_clue
        else [rejected] * 9
    )
    model = AsyncMock(side_effect=[json.dumps({"action": "open", "url": OLD}), *model_turns])
    monkeypatch.setattr(research, "request_completion", model)
    fetch = AsyncMock(return_value=Evidence(OLD, "TPU 95A", "An incompatible product"))
    monkeypatch.setattr(research, "fetch_supplier", fetch)
    result = await research.investigate(
        Identity(designation="TPU 90A"), start_urls=(OLD,),
        endpoint={"model_url": "http://model.test"}, model="fixture", search_url="http://search.test",
        max_bytes=1024, deadline=asyncio.get_running_loop().time() + 5, verify_product=lambda _: False,
    )
    assert result.status == "needs_clue"
    assert result.product_source is None
    assert result.suggestions == []
    assert len(result.evidence) == 1
    assert model.await_count == (4 if ask_for_clue else 10)
    if ask_for_clue:
        assert "JSON action" in model.await_args.args[3][-1]["content"]
    else:
        assert "Finish with the evidence" in model.await_args.args[3][0]["content"]
    fetch.assert_awaited_once_with(OLD, max_bytes=1024)


@pytest.mark.asyncio
async def test_prefetches_publisher_sheet_before_model_timeout(monkeypatch):
    issuer = "https://www.example.test/standard-tpu-90a-filament"
    incomplete = "https://www.example.test/tpu-90a"
    broken = "https://cdn.example.test/missing.pdf"
    sibling = "https://www.example.test/tpu-95a"
    retailer = "https://retail.test/tpu-90a"
    pages = {
        incomplete: Evidence(incomplete, "Example TPU 90A", "Example TPU 90A", datasheets=(broken,)),
        issuer: Evidence(issuer, "Example Standard TPU 90A", "Example TPU 90A",
                         datasheets=(PDF,), product_links=(CORRECT,)),
        PDF: Evidence(PDF, "Example TPU 90A Technical Data Sheet", "Bed temperature 0–60°C", datasheet=True),
    }
    model = AsyncMock(side_effect=[json.dumps({"action": "open", "url": issuer}), TimeoutError()])

    def fetch_page(url, **_):
        if url == broken:
            raise OSError("Document unavailable")
        return pages[url]

    fetch = AsyncMock(side_effect=fetch_page)
    search = AsyncMock(return_value=[retailer, sibling, incomplete, issuer])
    monkeypatch.setattr(research, "request_completion", model)
    monkeypatch.setattr(research, "fetch_supplier", fetch)
    monkeypatch.setattr(research, "search_web", search)

    result = await research.investigate(
        Identity(manufacturer="Example", designation="90A Grey", material="TPU"), start_urls=(),
        endpoint={"model_url": "http://model.test"}, model="fixture", search_url="http://search.test",
        max_bytes=1024, deadline=asyncio.get_running_loop().time() + 5,
        publisher_host="example.test", publisher_terms=("tpu", "90a"),
    )

    assert result.status == "partial"
    assert [page.url for page in result.evidence] == [incomplete, issuer, PDF]
    assert [call.args[0] for call in fetch.await_args_list] == [incomplete, broken, issuer, PDF]
    search.assert_awaited_once_with("http://search.test", "site:example.test tpu 90a", limit=8)
    initial = json.loads(model.await_args_list[0].args[3][-1]["content"])
    assert [source["source"] for source in initial["prefetched"]] == [1, 2, 3]
    assert json.loads(model.await_args.args[3][-1]["content"])["source"] == 2


@pytest.mark.asyncio
async def test_deadline_preserves_retrieved_sources(monkeypatch):
    async def model(*args, **kwargs):
        if not args[3][-1]["content"].startswith('{"source"'):
            return json.dumps({"action": "open", "url": OLD})
        await asyncio.Future()

    monkeypatch.setattr(research, "request_completion", model)
    monkeypatch.setattr(research, "fetch_supplier", AsyncMock(return_value=Evidence(OLD, "Page", "Evidence")))
    result = await research.investigate(Identity(), start_urls=(OLD,),
        endpoint={"model_url": "http://model.test"}, model="fixture",
        search_url="http://search.test", max_bytes=1024, deadline=asyncio.get_running_loop().time() + .05)
    assert result.status == "partial"
    assert [e.url for e in result.evidence] == [OLD]

@pytest.mark.asyncio
@pytest.mark.parametrize("model_uses_retailer,times_out", [(False, False), (True, False), (False, True)])
async def test_fill_uses_exact_product_and_linked_sheet(monkeypatch, model_uses_retailer, times_out):
    from filabrain import investigation, proposals

    issuer = "https://example.test/standard-tpu-90a"
    selected = "https://store.example/products/tpu-90a?variant=123"
    title = "Example TPU 90A - 1.75mm±0.03mm (1kg) - US / Gray"
    retailer = "https://retailer.example/products/tpu-90a"
    final_url = retailer if model_uses_retailer else PDF
    pages = {
        OLD: Evidence(OLD, "Example TPU 95A Gray", "Example TPU 95A Gray B0107017",
                      product={"designation": "TPU 95A Gray", "sku": "B0107017"}),
        selected: Evidence(selected, title, f"{title} SKU: B0107060",
                           product={"designation": title, "sku": "B0107060", "manufacturer": "Example"}),
        retailer: Evidence(retailer, title, f"{title} SKU: X0029G7BAN",
                           product={"designation": title, "sku": "X0029G7BAN", "manufacturer": "Example"}),
        issuer: Evidence(issuer, "Example Standard TPU 90A", "Example TPU 90A", datasheets=(PDF,),
                         product_links=(CORRECT,)),
        PDF: Evidence(PDF, "Technical sheet", """Example TPU 90A

Part I: Suggests Printing Parameters
Bed temperature                  0–60°C
Drying conditions                65°C, 8h

Part II: Mechanical Properties of Printed Samples
Bed temperature                  55°C
Drying conditions                50°C, 4h
""", datasheet=True),
    }
    model = AsyncMock(side_effect=list(map(json.dumps, [
        {"action": "open", "url": OLD},
        {"action": "finish", "status": "complete", "product_source": 1, "suggestions": [
            {"field": "manufacturer_sku", "value": "B0107017", "source": 1, "quote": "B0107017"},
        ]},
        {"action": "search", "query": "Example TPU 90A Gray"},
        {"action": "open", "url": selected},
        {"action": "open", "url": issuer},
        {"action": "open", "url": final_url},
        {"action": "finish", "status": "complete", "product_source": 4 if model_uses_retailer else 2,
         "suggestions": [
            {"field": "manufacturer_sku", "value": "X0029G7BAN", "source": 4, "quote": "X0029G7BAN"},
            {"field": [], "value": 7, "source": 4, "quote": "Bed temperature 0–60°C"},
            {"field": "bed_temp_range_c", "value": {"min": 0, "max": 60}, "source": 4,
             "quote": "Bed temperature 0–60°C"},
            {"field": "bed_temp_range_c", "value": {"min": 55, "max": 55}, "source": 4,
             "quote": "Bed temperature 55°C"},
        ]},
    ])))
    if times_out:
        responses = list(model.side_effect)
        model.side_effect = [*responses[:-1], TimeoutError()]
    monkeypatch.setattr(investigation, "request_completion", model)
    monkeypatch.setattr(investigation, "search_web", AsyncMock(return_value=[selected, issuer, retailer]))
    fetch = AsyncMock(side_effect=lambda url, **_: pages[url])
    monkeypatch.setattr(investigation, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "config_record", AsyncMock(return_value=(None, {
        "model_url": "http://model.test", "model_id": "fixture", "search_url": "http://search.test",
        "supplier_download_mib": 1, "supplier_sites": [],
    })))
    for name, result in (
        ("manufacturer_website", None), ("search_web", []), ("catalog_candidates", ([], "", True)),
        ("ofd_candidates", ([], "")), ("fetch_supplier", pages[OLD]),
    ):
        monkeypatch.setattr(proposals, name, AsyncMock(return_value=result))
    identity = proposals.Identity(manufacturer="Example", designation="90A - Grey", material="TPU", color="Grey", diameter_mm=1.75)
    result = await proposals.research_products(None, identity, snapshot={"shop_url": OLD}, investigate_missing=True)
    found = {item["field"]: item["value"] for item in result["suggestions"]}
    assert found["manufacturer_sku"] == "B0107060"
    sku = next(item for item in result["suggestions"] if item["field"] == "manufacturer_sku")
    assert [source["url"] for source in sku["sources"]] == [selected]
    assert found["raw_material_weight_g"] == 1000
    assert found.get("bed_temp_range_c") == (None if model_uses_retailer else {"min": 0, "max": 60})
    if times_out:
        assert found["drying_temp_c"] == 65
        assert found["drying_time_hours"] == 8
        sheet_field = next(item for item in result["suggestions"] if item["field"] == "bed_temp_range_c")
        assert {source["url"] for source in sheet_field["sources"]} == {PDF}
    assert any(i.get("product_url") == selected for i in result["issues"])
    assert any(i["code"] == "source_product_mismatch" for i in result["issues"])
    assert "shop_url" not in found
    feedback = json.loads(model.await_args_list[2].args[3][-1]["content"])
    assert feedback["rejected_product_source"] == 1
    assert [call.args[0] for call in fetch.await_args_list] == [OLD, selected, issuer, final_url]

def test_investigation_keeps_recommendations_after_properties_and_specimen_sections():
    from filabrain.proposals import _investigated_specs

    product = Evidence(CORRECT, "Example TPU 90A", "Example TPU 90A")
    sheet = Evidence(PDF, "Technical sheet", """Example TPU 90A

Part I: Physical Properties
Density                  1.21 g/cm³

Part II: Specimen Printing Conditions
Printing Parameters
Density                         1.33 g/cm³
Bed temperature                  55°C
Chamber temperature             45°C

Part III: Recommended Printing Settings
Bed temperature                  0–60°C
Chamber temperature             35°C

Part IV: Mechanical Properties (ISO 527)
Bed temperature                  49°C
""", datasheet=True)
    result = research.InvestigationResult(evidence=[product, sheet], suggestions=[
        {"field": "bed_temp_range_c", "value": {"min": 55, "max": 55}, "source": 2,
         "quote": "Bed temperature 55°C"},
        {"field": "bed_temp_range_c", "value": {"min": 49, "max": 49}, "source": 2,
         "quote": "Bed temperature 49°C"},
        {"field": "chamber_temp_c", "value": 45, "source": 2, "quote": "Chamber temperature 45°C"},
        {"field": "chamber_temp_c", "value": 35, "source": 2, "quote": "Chamber temperature 35°C"},
    ])

    findings, conflicts = _investigated_specs(result, product, {PDF}, {"tpu", "90a"})

    assert not conflicts
    assert {finding["field"]: finding["value"] for finding in findings} == {
        "density_g_cm3": 1.21,
        "bed_temp_range_c": {"min": 0, "max": 60},
        "chamber_temp_c": 35,
    }
    assert all(source["url"] == PDF for finding in findings for source in finding["sources"])


@pytest.mark.parametrize("title", [
    "Example High Speed TPU 90A Gray 1.75 mm 1 kg",
    "Example TPU 95A Gray 1.75 mm 1 kg",
    "Example TPU 90A Gray 1.75 mm 0.5 kg",
    "Example TPU 90A Gray 1.75 mm 1 kg refill",
])
def test_wrong_product_line_or_package_cannot_fill_fields(title):
    from filabrain import proposals

    page = Evidence(CORRECT, title, title, product={"designation": title, "sku": "OTHER"})
    result = research.InvestigationResult(evidence=[page], product_source=1, status="complete")
    identity = Identity(manufacturer="Example", designation="90A Gray", material="TPU", color="Gray",
                        diameter_mm=1.75, weight_g=1000)
    assert proposals._investigated_product(identity, result, "store.example") is None


@pytest.mark.parametrize("codes,expected", [
    (["MANUFACTURER"], "MANUFACTURER"),
    (["MANUFACTURER", "MANUFACTURER"], "MANUFACTURER"),
    (["", "MANUFACTURER"], "MANUFACTURER"),
    (["MANUFACTURER", "DIFFERENT"], None),
    ([], None),
])
def test_sku_requires_agreement_on_authorized_variants(codes, expected):
    from filabrain import investigation, proposals

    title = "Example TPU 90A Gray 1.75 mm 1 kg"
    retailer = Evidence("https://retailer.example/products/tpu-90a", title, title,
                        product={"designation": title, "sku": "RETAILER"})
    pages = [
        Evidence(f"{CORRECT}?variant={i}", title, title, product={"designation": title, "sku": code})
        for i, code in enumerate(codes, 1)
    ]
    result = investigation.InvestigationResult(evidence=[retailer, *pages], product_source=1, status="complete")
    identity = proposals.Identity(manufacturer="Example", designation="90A Gray", material="TPU",
                                  color="Gray", diameter_mm=1.75)
    match = proposals._investigated_product(identity, result, "store.example")
    assert (match[1].sku if match else None) == expected
