"""Catalog-v3 two-vector indexing and B2/B4+dense+HyDE RRF retrieval."""
from __future__ import annotations
import argparse, hashlib, json, math, os, time, uuid
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
COLLECTION = "kosis_tables_v3"
NAMESPACE = uuid.UUID("1c9e5c35-2b4f-4677-b4ea-aef0b63e1882")

def api_key():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    value = (os.getenv('HCX_API_KEY') or os.getenv('NCP_CLOVASTUDIO_API_KEY') or '').strip()
    if not value: raise RuntimeError('HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 필요합니다')
    return value

class Cache:
    def __init__(self, path):
        self.path=path; self.data={}; self.hits=self.calls=0
        if path.exists():
            for line in path.open(encoding='utf8'):
                row=json.loads(line); self.data[row['key']]=row['vector']
    def embed(self, text):
        key=hashlib.sha256(('clova-v2|'+text).encode()).hexdigest()
        if key in self.data: self.hits+=1; return self.data[key]
        import requests
        response=requests.post(URL,headers={'Authorization':f'Bearer {api_key()}','Content-Type':'application/json'},json={'text':text},timeout=30)
        body=response.json(); vector=body.get('result',{}).get('embedding')
        if response.status_code != 200 or not vector: raise RuntimeError(f'embedding API {response.status_code}: {body}')
        self.calls+=1; self.data[key]=vector; self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open('a',encoding='utf8') as out: out.write(json.dumps({'key':key,'vector':vector})+'\n')
        return vector

def rows(path): return [json.loads(line) for line in path.open(encoding='utf-8-sig') if line.strip()]

def morph(text, core=False):
    from kiwipiepy import Kiwi
    kiwi=getattr(morph,'kiwi',None)
    if kiwi is None: kiwi=morph.kiwi=Kiwi(num_workers=-1)
    keep=('NN','NR','NP','VV','VA','VX','XR','MM','SL','SN')
    result=[]
    for token in kiwi.tokenize(str(text or '')):
        if token.tag.startswith(keep) and (not core or token.tag.startswith(('NN','VV','VA','SL'))): result.append(token.form.lower())
    return list(dict.fromkeys(result))

class BM25:
    def __init__(self, catalog):
        self.catalog=catalog; self.posts=defaultdict(list); self.length=[]
        for i,row in enumerate(catalog):
            counts=Counter(morph(row.get('doc_meta_text',''))); self.length.append(sum(counts.values()))
            for term,count in counts.items(): self.posts[term].append((i,count))
        self.avg=sum(self.length)/max(1,len(self.length)); n=len(catalog)
        self.idf={term:math.log(1+(n-len(v)+.5)/(len(v)+.5)) for term,v in self.posts.items()}
    def search(self, query, core, limit):
        score=defaultdict(float); k1=1.5; b=.75
        for term in morph(query,core):
            for i,freq in self.posts.get(term,[]):
                norm=k1*(1-b+b*self.length[i]/self.avg); score[i]+=self.idf[term]*freq*(k1+1)/(freq+norm)
        ranked=sorted(score.items(),key=lambda x:(-x[1],self.catalog[x[0]]['table_key']))[:limit]
        return [(self.catalog[i],float(value)) for i,value in ranked if value>0]

def rrf(paths, top, k):
    merged={}
    for name,hits in paths.items():
        for rank,(row,score) in enumerate(hits,1):
            key=row['table_key']; item=merged.setdefault(key,{'table_key':key,'tbl_name':row.get('tbl_name'),'path_ranks':{},'path_scores':{},'fusion_score':0})
            item['path_ranks'][name]=rank; item['path_scores'][name]=score; item['fusion_score']+=1/(k+rank)
    return sorted(merged.values(),key=lambda x:(-x['fusion_score'],x['table_key']))[:top]

def index(args):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
    catalog=rows(args.catalog); cache=Cache(args.cache); client=QdrantClient(path=str(args.db_path))
    if args.recreate and args.collection in [x.name for x in client.get_collections().collections]: client.delete_collection(args.collection)
    meta,name=cache.embed(catalog[0]['doc_meta_text']),cache.embed(catalog[0]['tbl_name'])
    if args.collection not in [x.name for x in client.get_collections().collections]: client.create_collection(args.collection,vectors_config={'doc_meta_vector':VectorParams(size=len(meta),distance=Distance.COSINE),'tbl_name_vector':VectorParams(size=len(name),distance=Distance.COSINE)})
    for start in range(0,len(catalog),args.batch_size):
        points=[]
        for row in catalog[start:start+args.batch_size]:
            points.append(PointStruct(id=str(uuid.uuid5(NAMESPACE,row['table_key'])),vector={'doc_meta_vector':cache.embed(row['doc_meta_text']),'tbl_name_vector':cache.embed(row['tbl_name'])},payload={key:row.get(key) for key in ('table_key','tbl_name','org_id','category_paths')}))
        client.upsert(args.collection,points,wait=True); print(f'indexed {min(start+args.batch_size,len(catalog))}/{len(catalog)} cache_hits={cache.hits} api_calls={cache.calls}',flush=True)
    print(json.dumps({'catalog_documents':len(catalog),'qdrant_points':client.count(args.collection,exact=True).count,'cache_hits':cache.hits,'embedding_api_calls':cache.calls},ensure_ascii=False)); client.close()

def hyde(text, model):
    import requests
    # HCX-007 accepts Structured Output in the claim experiment, but the
    # HyDE endpoint rejects responseFormat/thinking. Request plain JSON and
    # validate it locally instead.
    body={'messages':[{'role':'system','content':'KOSIS 통계표 검색용 예상 표명을 한 줄로만 출력하세요. 해설·추론·JSON은 출력하지 마세요.'},{'role':'user','content':text}],'temperature':.1,'topP':.8,'topK':0,'repetitionPenalty':1.1,'maxCompletionTokens':120,'thinking':{'effort':'none'}}
    response=requests.post(f'https://clovastudio.stream.ntruss.com/v3/chat-completions/{model}',headers={'Authorization':f'Bearer {api_key()}','Content-Type':'application/json','X-NCP-CLOVASTUDIO-REQUEST-ID':str(uuid.uuid4())},json=body,timeout=120)
    payload=response.json()
    if response.status_code >= 400: raise RuntimeError(f'HyDE API {response.status_code}: {payload}')
    result=payload.get('result') or payload
    message=result.get('message') or {}
    content=message.get('content') or result.get('content') or ''
    if isinstance(content,list): content=''.join(str(x.get('text') or '') for x in content if isinstance(x,dict))
    cleaned=str(content).replace('```json','').replace('```','').strip(); start,end=cleaned.find('{'),cleaned.rfind('}')
    if start >= 0 and end > start: return json.loads(cleaned[start:end+1]).get('predicted_tbl_nm') or cleaned
    if cleaned: return cleaned
    raise RuntimeError(f'HyDE text missing: {payload}')

def run(args):
    from qdrant_client import QdrantClient
    catalog=rows(args.catalog); bm=BM25(catalog); claims=[x for x in rows(args.claims) if x.get('is_claim') and x.get('indicator_raw')]
    if args.limit: claims=claims[:args.limit]
    client=QdrantClient(path=str(args.db_path)); cache=Cache(args.output_dir/'query_embedding_cache.jsonl'); args.output_dir.mkdir(parents=True,exist_ok=True)
    final=args.output_dir/'hybrid_top20_500.jsonl'; debug=args.output_dir/'path_debug_500.jsonl'; hyde_out=args.output_dir/'hyde_predictions_500.jsonl'; done={x['claim_id'] for x in rows(final)} if final.exists() else set(); start=time.perf_counter()
    for pos,claim in enumerate(claims,1):
        if claim['claim_id'] in done: continue
        text=claim['claim_text']; query=' '.join(x for x in (claim.get('indicator_raw'),claim.get('population_raw'),text) if x); paths={'b2_doc_meta_bm25':bm.search(text,False,args.per_path_n),'b4_doc_meta_bm25':bm.search(text,True,args.per_path_n)}; errors={}; predicted=''
        try: paths['claim_dense']=[(p.payload,float(p.score)) for p in client.query_points(args.collection,query=cache.embed(query),using='doc_meta_vector',limit=args.per_path_n,with_payload=True).points]
        except Exception as e: paths['claim_dense']=[]; errors['claim_dense']=str(e)
        try:
            predicted=hyde(text,args.hcx_model); paths['hyde_dense']=[(p.payload,float(p.score)) for p in client.query_points(args.collection,query=cache.embed(predicted),using='tbl_name_vector',limit=args.per_path_n,with_payload=True).points]
        except Exception as e: paths['hyde_dense']=[]; errors['hyde_dense']=str(e)
        fused=rrf(paths,args.top_n,args.rrf_k); row={'claim_id':claim['claim_id'],'claim_text':text,'indicator_raw':claim.get('indicator_raw'),'population_raw':claim.get('population_raw'),'predicted_tbl_nm':predicted,'selected_tables':[dict(x,final_rank=i+1) for i,x in enumerate(fused)],'errors':errors}
        with final.open('a',encoding='utf8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        with debug.open('a',encoding='utf8') as f:f.write(json.dumps({**row,'path_results':{k:[{'table_key':r['table_key'],'score':s} for r,s in v] for k,v in paths.items()}},ensure_ascii=False)+'\n')
        with hyde_out.open('a',encoding='utf8') as f:f.write(json.dumps({'claim_id':claim['claim_id'],'predicted_tbl_nm':predicted},ensure_ascii=False)+'\n')
        if pos%10==0 or pos==len(claims): print(f'hybrid={pos}/{len(claims)} embed_calls={cache.calls}',flush=True)
    client.close(); print(json.dumps({'eligible_claims':len(claims),'elapsed_sec':round(time.perf_counter()-start,3),'output':str(final)},ensure_ascii=False))

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd',required=True)
    for name in ('index','run'):
        x=sub.add_parser(name); x.add_argument('--catalog',type=Path,default=ROOT/'data/kosis_catalog_v3.jsonl'); x.add_argument('--db-path',type=Path,default=ROOT/'output/kosis_qdrant_v3'); x.add_argument('--collection',default=COLLECTION)
        if name=='index': x.add_argument('--cache',type=Path,default=ROOT/'output/hybrid_v3_500_20260723/catalog_embed_cache.jsonl'); x.add_argument('--batch-size',type=int,default=32); x.add_argument('--recreate',action='store_true')
        else: x.add_argument('--claims',type=Path,default=ROOT/'output/hybrid_v3_500_20260723/canonical_claims_500.jsonl'); x.add_argument('--output-dir',type=Path,default=ROOT/'output/hybrid_v3_500_20260723'); x.add_argument('--per-path-n',type=int,default=20); x.add_argument('--top-n',type=int,default=20); x.add_argument('--rrf-k',type=int,default=60); x.add_argument('--hcx-model',default='HCX-007'); x.add_argument('--limit',type=int)
    a=p.parse_args(); index(a) if a.cmd=='index' else run(a)
if __name__=='__main__': main()
