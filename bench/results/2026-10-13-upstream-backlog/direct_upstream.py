import concurrent.futures, importlib.util, threading, json, time, sys
spec=importlib.util.spec_from_file_location('load','/evidence/trace_load.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
if len(sys.argv)>1:m.Upstream.request_queue_size=int(sys.argv[1])
server=m.Upstream(('127.0.0.1',5201),10);threading.Thread(target=server.serve_forever,daemon=True).start()
try:
 for trial in range(4):
  for direction in ['down','up']:
   errors=[];start=time.monotonic()
   with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
    futures=[ex.submit(m.bulk_flow,('127.0.0.1',5201),10,direction,i,1<<20) for i in range(64)]
    for f in futures:
     try:f.result()
     except Exception as e:errors.append(repr(e))
   print(json.dumps({'trial':trial,'direction':direction,'errors':errors,'server_errors':server.error_snapshot(),'seconds':time.monotonic()-start,'backlog':server.request_queue_size}),flush=True)
finally:server.shutdown();server.server_close()
