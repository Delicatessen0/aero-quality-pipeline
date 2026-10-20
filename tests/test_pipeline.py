import pandas as pd
import pytest
from fastapi.testclient import TestClient

from aeroq import analytics, anomaly, etl, generate, governance, quality
from aeroq.models import create_schema, get_engine


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("aeroq")
    generate.generate(d / "raw", n_work_orders=600, seed=7)
    engine = get_engine(f"sqlite:///{d / 'wh.db'}")
    create_schema(engine, drop=True)
    tables = etl.transform(etl.extract(d / "raw"))
    results = quality.run_checks(tables)
    quality.persist(engine, "test", results)
    etl.load(engine, tables)
    found = anomaly.detect(engine, "test")
    return engine, tables, results, found


def test_duplicates_removed_and_casing_normalized(built):
    _, t, _, _ = built
    wo = t["fact_work_order"]
    assert wo["work_order_id"].is_unique
    assert wo["work_center_key"].notna().all()  # lowercase codes were conformed


def test_orphans_quarantined_not_loaded(built):
    _, t, _, _ = built
    assert len(t["quarantine_inspection"]) == 12
    assert t["fact_inspection"]["work_order_id"].isin(t["fact_work_order"]["work_order_id"]).all()


def test_sensor_glitches_filtered(built):
    _, t, _, _ = built
    temps = t["fact_sensor_reading"].query("sensor == 'autoclave_temp_c'")["value"]
    assert temps.max() < 400


def test_pii_is_masked(built):
    _, t, _, _ = built
    insp = t["fact_inspection"]
    assert "inspector" not in insp.columns
    assert insp["inspector_hash"].str.len().eq(16).all()


def test_dq_gate_passes_on_clean_data(built):
    _, _, results, _ = built
    quality.gate(results)  # no exception


def test_dq_gate_blocks_bad_data():
    df = pd.DataFrame({"id": [1, 1, None]})
    res = quality.Suite("x", [quality.unique("id"), quality.not_null("id")]).run(df)
    with pytest.raises(quality.DataQualityError):
        quality.gate(res)


def test_spc_flags_injected_autoclave_drift(built):
    _, _, _, found = built
    flagged = found["spc"].query("out_of_control")["work_center"].value_counts()
    assert flagged.index[0] == "Autoclave Cure"


def test_analytics_library(built):
    engine, *_ = built
    fpy = analytics.first_pass_yield(engine, "program")
    assert fpy["fpy"].between(0, 1).all()
    p = analytics.defect_pareto(engine)
    assert p["cum_pct"].iloc[-1] == pytest.approx(1.0)


def test_api_role_filtering(built, monkeypatch):
    engine, *_ = built
    from aeroq import api

    monkeypatch.setattr(api, "_engine", engine)
    c = TestClient(api.app)
    assert c.get("/health").json() == {"status": "ok"}
    eng = c.get("/nonconformances", headers={"X-Role": "engineer"}).json()
    ext = c.get("/nonconformances", headers={"X-Role": "external"}).json()
    assert "serial_number" in eng[0] and "serial_number" not in ext[0]
    assert c.get("/nonconformances", headers={"X-Role": "hacker"}).status_code == 403


def test_governance_filter():
    rows = [{"serial_number": "SN1", "defect_code": "POR"}]
    assert governance.filter_columns(rows, "analyst") == [{"defect_code": "POR"}]
