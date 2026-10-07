from src.design.fixed_verification import run_fixed_cases


def test_fixed_scenarios_report_all_expectations_without_model_calls(monkeypatch):
    monkeypatch.setenv('RAG_ENABLED', '0')
    report = run_fixed_cases()
    assert report['total'] == 10
    assert report['passed'] == report['total'], [(r['id'], r['actual']) for r in report['cases'] if not r['passed']]
    assert len({r['id'] for r in report['cases']}) == report['total']
