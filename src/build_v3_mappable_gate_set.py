"""Build a high-precision v3-mappable claim set from completed pipeline audits."""
from __future__ import annotations
import argparse,json
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p): return [json.loads(x) for x in Path(p).open(encoding='utf8') if x.strip()]
def main():
 p=argparse.ArgumentParser(); p.add_argument('--claims',default=ROOT/'output/hybrid_v3_500_20260723/canonical_claims_500.jsonl'); p.add_argument('--hybrid',default=ROOT/'output/hybrid_v3_500_20260723/retry_hyde_v3/hybrid_top20_500.jsonl'); p.add_argument('--ncp',default=ROOT/'output/hybrid_v3_500_20260723/ncp_rerank_reasoning_v2/mapping_records.jsonl'); p.add_argument('--output-dir',default=ROOT/'output/hybrid_v3_500_20260723/v3_mappable_gate_v1'); a=p.parse_args()
 claims={x['claim_id']:x for x in read(a.claims)}; hybrid={x['claim_id']:x for x in read(a.hybrid)}; ncp={x['claim_id']:x for x in read(a.ncp)}; passed=[]; audit=[]
 for claim_id,claim in claims.items():
  h=hybrid.get(claim_id); n=ncp.get(claim_id); reasons=[]
  if not (claim.get('is_claim') and claim.get('indicator_raw')): reasons.append('STRUCTURE_MISSING')
  top1=(h or {}).get('selected_tables',[{}])[0] if h else {}; support=len(top1.get('path_ranks',{}))
  if support<3: reasons.append('MULTIPATH_SUPPORT_LT3')
  if not n or n.get('mapping_status')!='RERANKED': reasons.append('RERANKER_NO_CITATION')
  row={'claim_id':claim_id,'claim_text':claim.get('claim_text'),'indicator_raw':claim.get('indicator_raw'),'population_raw':claim.get('population_raw'),'top1_table_key':top1.get('table_key'),'top1_path_support':support,'reranked_candidates':(n or {}).get('reranked_candidates',[]),'gate_status':'PASS' if not reasons else 'ABSTAIN','gate_reasons':reasons}
  audit.append(row)
  if not reasons: passed.append(row)
 out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
 for name,rows in [('mappable_claims.jsonl',passed),('gate_audit.jsonl',audit)]:
  with (out/name).open('w',encoding='utf8') as f:
   for row in rows:f.write(json.dumps(row,ensure_ascii=False)+'\n')
 summary={'input_claims':len(claims),'passed':len(passed),'abstained':len(audit)-len(passed),'abstain_reasons':Counter(r for x in audit for r in x['gate_reasons'])}
 (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8'); print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
