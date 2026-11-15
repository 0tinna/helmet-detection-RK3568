import http.client,json,pathlib,subprocess,sys,time
ROOT=pathlib.Path('/userdata/helmet-validation/20261115-native-app-v5-r2-lan-2')
OUT=pathlib.Path('/userdata/helmet-validation/20261115-app-lan-validation-2')
sys.path.insert(0,str(ROOT/'src'))
from app import resident
run=subprocess.run(['sh','/etc/init.d/S99zhelmet-app','start'],capture_output=True,text=True,timeout=35)
assert run.returncode==0,run.stderr
start=json.loads(run.stdout)
coord=resident.Coordinator().prepare();first=coord.read_record()
result={'kind':'installed-init-background-idle-180s','start':start,'session':first['session'],'identities':{k:first[k] for k in ('manager','service','ui')},'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'actual_reboot':False}
rows=[];began=time.monotonic()
print(json.dumps({'started':True,'session':first['session'],'visible':start['visible'],'ui':first['ui']}),flush=True)
try:
 with (OUT/'idle-180s.jsonl').open('x') as stream:
  while time.monotonic()-began<180:
   record=coord.read_record()
   conn=http.client.HTTPConnection('127.0.0.1',8091,timeout=4);conn.request('GET','/status.json');response=conn.getresponse();status=json.loads(response.read());conn.close()
   row={'elapsed':time.monotonic()-began,'http':response.status,'status':status,'session':record['session'],'identities':{k:record[k] for k in ('manager','service','ui')},'phase':record['phase'],'visible':record['application']['intent']['visible']}
   stream.write(json.dumps(row)+'\n');stream.flush();rows.append(row)
   if len(rows)%12==0:print(json.dumps({'elapsed':row['elapsed'],'phase':row['phase'],'running':status['running'],'ui':record['ui']}),flush=True)
   time.sleep(5)
 result['elapsed']=time.monotonic()-began;result['samples']=len(rows)
 result['owner_changes']=sum(r['session']!=result['session'] or r['identities']['manager']!=result['identities']['manager'] or r['identities']['service']!=result['identities']['service'] for r in rows)
 result['failed_samples']=sum(r['http']!=200 or r['phase']!='running' for r in rows)
 result['ui_or_detection_started']=sum(r['identities']['ui'] is not None or r['status']['running'] is True for r in rows)
 result['passed']=result['elapsed']>=180 and result['owner_changes']==0 and result['failed_samples']==0 and result['ui_or_detection_started']==0
except BaseException as error:result.update(passed=False,error=type(error).__name__+': '+str(error))
finally:
 coord.close()
 with (OUT/'idle-summary.json').open('x') as stream:json.dump(result,stream,indent=2)
 print(json.dumps(result),flush=True)
