"""NCP Reranker → RAG Reasoning mapping stage; never compares numeric values."""
from __future__ import annotations
import argparse, json, os, time, uuid
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
BASE='https://clovastudio.stream.ntruss.com/v1/api-tools'

def key():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env'); value=(os.getenv('HCX_API_KEY') or os.getenv('NCP_CLOVASTUDIO_API_KEY') or '').strip()
    if not value: raise RuntimeError('HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 필요합니다')
    return value
def read(path):
    path=Path(path)
    return [json.loads(x) for x in path.open(encoding='utf-8-sig') if x.strip()]
def post(endpoint, body):
    started=time.perf_counter(); response=requests.post(f'{BASE}/{endpoint}',headers={'Authorization':f'Bearer {key()}','X-NCP-CLOVASTUDIO-REQUEST-ID':str(uuid.uuid4()),'Content-Type':'application/json'},json=body,timeout=120)
    data=response.json()
    if response.status_code>=400: raise RuntimeError(f'{endpoint} {response.status_code}: {data}')
    return data,round((time.perf_counter()-started)*1000,1)
def table_doc(row):
    return str(row.get('doc_meta_text') or row.get('tbl_name') or '')
def rerank(query, candidates, top_n):
    docs=[{'id':x['table_key'],'doc':table_doc(x)} for x in candidates]
    data,latency=post('reranker',{'query':query,'documents':docs,'maxTokens':1024})
    result=data.get('result') or data
    values=result.get('citedDocuments') or result.get('documents') or result.get('results') or result.get('scores') or []
    by_id={x['table_key']:x for x in candidates}; selected=[]
    for item in values:
        ident=item.get('id') or item.get('documentId')
        if not ident and isinstance(item.get('document'),dict): ident=item['document'].get('id')
        ident=str(ident or '')
        if ident in by_id: selected.append({**by_id[ident],'reranker_score':item.get('score')})
    return selected[:top_n],data,latency
def reason(query, candidates, max_tokens, temperature):
    tool={'type':'function','function':{'name':'kosis_candidate_lookup','description':'Return only supplied KOSIS table candidates. Do not invent table keys or values.','parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}}
    first,lat1=post('rag-reasoning',{'messages':[{'role':'system','content':'Select the best KOSIS table only from tool results. Return table_key, item/dimension/period hints, confidence and evidence. Never calculate or assert numeric values.'},{'role':'user','content':query}],'tools':[tool],'toolChoice':'auto','maxTokens':max_tokens,'temperature':temperature,'topP':0.8,'topK':0})
    message=(first.get('result') or {}).get('message') or {}; calls=message.get('toolCalls') or []
    if not calls: return {'first_response':first,'final_response':None,'content':message.get('content',''),'latency_ms':lat1}
    call=calls[0]; tool_result=json.dumps([{'search_result':[{'id':x['table_key'],'doc':table_doc(x)} for x in candidates]}],ensure_ascii=False)
    messages=[{'role':'user','content':query},{'role':'assistant','content':message.get('content',''),'toolCalls':calls},{'role':'tool','toolCallId':call['id'],'content':tool_result}]
    final,lat2=post('rag-reasoning',{'messages':messages,'tools':[tool],'toolChoice':'none','maxTokens':max_tokens,'temperature':temperature,'topP':0.8,'topK':0})
    content=((final.get('result') or {}).get('message') or {}).get('content','')
    return {'first_response':first,'final_response':final,'content':content,'latency_ms':lat1+lat2}
def main():
    p=argparse.ArgumentParser(); p.add_argument('--config',type=Path,default=ROOT/'configs/ncp_rerank_reasoning_v1.json'); p.add_argument('--limit',type=int); args=p.parse_args(); cfg=json.loads(args.config.read_text(encoding='utf8'))
    catalog={x['table_key']:x for x in read(ROOT/cfg['catalog'])}; rows=read(ROOT/cfg['input']); rows=rows[:args.limit] if args.limit else rows; out=ROOT/cfg['output_dir']; out.mkdir(parents=True,exist_ok=True); path=out/'mapping_records.jsonl'; done={x['claim_id'] for x in read(path)} if path.exists() else set()
    for i,row in enumerate(rows,1):
        if row['claim_id'] in done: continue
        candidates=[catalog[x['table_key']] for x in row['selected_tables'] if x['table_key'] in catalog]; query='\n'.join(x for x in [row['claim_text'],row.get('indicator_raw',''),row.get('population_raw','')] if x)
        try:
            top5,raw,rlat=rerank(query,candidates,int(cfg['rerank_top_n']))
            if not top5:
                record={'claim_id':row['claim_id'],'claim_text':row['claim_text'],'reranked_candidates':[],'reranker_raw':raw,'reranker_latency_ms':rlat,'mapping_status':'RERANKER_ABSTAIN','numeric_verdict':None,'numeric_verdict_rule':'reserved_for_deterministic_code'}
            else:
                rationale=reason(query,top5,int(cfg['reasoning_max_tokens']),float(cfg['reasoning_temperature'])); record={'claim_id':row['claim_id'],'claim_text':row['claim_text'],'reranked_candidates':top5,'reranker_raw':raw,'reranker_latency_ms':rlat,'rag_reasoning':rationale,'mapping_status':'RERANKED','numeric_verdict':None,'numeric_verdict_rule':'reserved_for_deterministic_code'}
        except Exception as exc: record={'claim_id':row['claim_id'],'claim_text':row['claim_text'],'error':f'{type(exc).__name__}: {exc}','numeric_verdict':None}
        with path.open('a',encoding='utf8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
        if i%10==0 or i==len(rows): print(f'ncp-chain={i}/{len(rows)}',flush=True)
if __name__=='__main__': main()
