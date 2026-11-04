"""Cold-load time and resident size of each Ollama model at num_ctx 8192: python ollama_sizes.py [MODEL ...]"""
import json, time, urllib.request, subprocess
def post(path, body, t=300):
    r=urllib.request.Request('http://127.0.0.1:11434'+path, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(r, timeout=t))
def ps(): return json.load(urllib.request.urlopen('http://127.0.0.1:11434/api/ps'))['models']
def vram(): return int(subprocess.check_output(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],text=True).split()[0])
import sys
for m in (sys.argv[1:] or ['nomic-embed-text','qwen3.5:4b','gpt-oss:20b']):
    for x in ps(): post('/api/generate',{'model':x['name'],'keep_alive':0})
    time.sleep(3); v0=vram(); t=time.time()
    if 'embed' in m: post('/api/embeddings',{'model':m,'prompt':'ping'})
    else: post('/api/chat',{'model':m,'messages':[{'role':'user','content':'ping'}],'stream':False,'options':{'num_predict':1,'num_ctx':8192},'keep_alive':'30m'})
    dt=time.time()-t; time.sleep(1)
    e=[x for x in ps() if x['name'].startswith(m)]
    print(json.dumps({'model':m,'cold_load_s':round(dt,2),'size_mb':e[0]['size']//2**20 if e else None,'size_vram_mb':e[0]['size_vram']//2**20 if e else None,'ctx':e[0].get('context_length') if e else None,'vram_delta':vram()-v0}))
for x in ps(): post('/api/generate',{'model':x['name'],'keep_alive':0})
