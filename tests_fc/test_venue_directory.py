"""Independent public venue metadata; no companion reads or private state."""
import pytest
from funding_collector.domain import Reject
from funding_collector.live_markets import MarketDirectory, HL, ENT, LIGHTER
from tests_fc.test_live_markets import adapters

@pytest.mark.parametrize('deployment', [HL, ENT, *LIGHTER])
def test_directory_reads_only_selected_venue(tmp_path, deployment):
    calls = []
    def isolated(root, dep):
        calls.append(dep)
        if dep != deployment:
            raise OSError('unselected venue down')
        adapter = adapters(root, HL if dep == ENT else dep)
        if dep == ENT:
            original = adapter.catalog
            adapter.catalog = lambda: dict(original(), markets=[dict(original()['markets'][0], coin='io:NVDA')])
        return adapter
    result = MarketDirectory(tmp_path, factory=isolated).venue(deployment)
    assert calls == [deployment]
    assert result['deployment'] == deployment
    assert len(result['markets']) == 1
    assert result['markets'][0]['supported']
    assert 'pairs' not in result

@pytest.mark.parametrize('deployment', ['evil', 'hyperliquid-mainnet-io', 'variational-mainnet', 'evedex-mainnet'])
def test_directory_rejects_unknown_scope_before_transport(tmp_path, deployment):
    def forbidden(*args):
        pytest.fail('unexpected public read')
    with pytest.raises(Reject):
        MarketDirectory(tmp_path, factory=forbidden).venue(deployment)
