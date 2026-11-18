import sys, json, asyncio
sys.path.insert(0, '.')

from pathlib import Path
from analysis.providers.dexscreener_parser import parse_pairs_response
from analysis.providers.rugcheck_parser import parse_report
from analysis.core_engine import CoreEngine
from analysis.security_engine import SecurityEngine

class FakeMarketProvider:
    def __init__(self, pairs): self._pairs = pairs
    async def get_pairs(self, address): return self._pairs

class FakeSecurityProvider:
    def __init__(self, report): self._report = report
    async def scan(self, address, chain): return self._report

async def main():
    FIXTURE_ROOT = Path('tests/fixtures')

    ds_f = {f.stem: parse_pairs_response(json.loads(f.read_text()))
            for f in (FIXTURE_ROOT/'dexscreener').glob('*.json')}
    rc_f = {f.stem: parse_report(json.loads(f.read_text()), True)
            for f in (FIXTURE_ROOT/'rugcheck').glob('*.json')}

    print('VulnerabilityPenalty via SecurityEngine:')
    for ds_name, pairs in ds_f.items():
        if not pairs: continue
        core = CoreEngine(FakeMarketProvider(pairs))
        primary = pairs[0]
        for rc_name, rc_rpt in rc_f.items():
            sec = SecurityEngine(FakeSecurityProvider(rc_rpt))
            cr = await core.analyze(primary.base_token_address)
            sr = await sec.analyze(cr)
            from scoring.formulas import vulnerability_penalty
            print(f'  {ds_name}+{rc_name}: trust={sr.trust_score:.1f}, vuln_penalty={vulnerability_penalty(sr)}, degraded={sr.degraded}')

if __name__ == '__main__':
    asyncio.run(main())