#!/usr/bin/env python3
"""Daily encrypted estate backup. No model calls, source edits, PM2 saves, or snapshot pruning."""
from pathlib import Path
import os,sys,json,subprocess,shlex,time,datetime,hashlib,fcntl,threading,signal,shutil
os.umask(0o077)
ROOT=Path('/opt/alloyscape/ops/estate-backup');CONFIG=json.loads((ROOT/'destinations.json').read_text());HOSTS=json.loads((ROOT/'hosts.json').read_text());SOURCE=(ROOT/'source.py').read_text();REPO='sftp:root@100.103.27.39:'+CONFIG['staging_repository'];PASSWORD=CONFIG['HQ_password_file'];CACHE='/var/cache/alloyscape-estate-backup';GIT=ROOT/'control-git';STATUS=ROOT/'LAST-STATUS.json'
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def atomic(path,data):
 path=Path(path);temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');os.chmod(temp,0o600);os.replace(temp,path)
def hostcmd(host,mode,*args):
 if host=='hq':return ['/usr/bin/python3',str(ROOT/'source.py'),mode,*args]
 spec=HOSTS[host];cmd=['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=20','-o','ServerAliveCountMax=3']
 if spec.get('key'):cmd+=['-i',spec['key']]
 remote=['nice','-n','15']+(['sudo','-n'] if spec.get('sudo') else [])+['python3','-c',SOURCE,mode,*args]
 return cmd+[spec['target'],shlex.join(remote)]
def storage_guard():
 if shutil.disk_usage('/').free<CONFIG['disk_floor_bytes']:raise RuntimeError('HQ disk safety floor reached')
 code="from pathlib import Path;import json,shutil,fcntl;root=Path('/var/lib/alloyscape-estate-backup');lock=(root/'publish.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);total=sum(p.stat().st_size for p in (root/'restic').rglob('*') if p.is_file());print(json.dumps({'bytes':total,'free':shutil.disk_usage(root).free}))"
 q=subprocess.run(['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','root@100.103.27.39','python3 -c '+shlex.quote(code)],capture_output=True,text=True,timeout=20)
 if q.returncode:raise RuntimeError('Backup storage unavailable or previous publisher still holds the lock')
 d=json.loads(q.stdout)
 if d['bytes']>CONFIG['staging_repository_limit_bytes'] or d['free']<40*1024**3:raise RuntimeError('Remote backup storage safety limit reached')
 return d
def base():return ['/usr/bin/restic','--repo',REPO,'--password-file',PASSWORD,'--cache-dir',CACHE,'--limit-upload','16384','--limit-download','16384','--pack-size','128']
def execute_backup(state,run,label,host,tail):
 storage_guard();state['current_job']={'host':host,'label':label,'started':now()};atomic(STATUS,state);start=time.monotonic();errpath=run/(host+'-'+label+'.stderr');outpath=run/(host+'-'+label+'.jsonl');summary=None;guard_errors=[];done=threading.Event()
 with errpath.open('wb') as err,outpath.open('wb') as out:
  child=subprocess.Popen(base()+['backup','--json','--host',host,'--tag','daily-estate','--tag',state['run_id']]+tail,stdout=out,stderr=err,start_new_session=True,env={**os.environ,'GOMAXPROCS':'2'})
  def watch():
   while not done.wait(60):
    try:storage_guard()
    except Exception as exc:
     guard_errors.append(str(exc))
     try:os.killpg(child.pid,signal.SIGTERM)
     except ProcessLookupError:pass
     break
  watcher=threading.Thread(target=watch,daemon=True);watcher.start()
  try:code=child.wait(timeout=8*3600)
  except subprocess.TimeoutExpired:
   guard_errors.append('Bounded backup acquisition time exceeded');os.killpg(child.pid,signal.SIGTERM)
   try:code=child.wait(timeout=15)
   except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);code=child.wait()
  finally:done.set();watcher.join(timeout=25)
 for line in outpath.read_text(errors='replace').splitlines():
  try:
   d=json.loads(line)
   if d.get('message_type')=='summary':summary=d
  except ValueError:pass
 error=errpath.read_text(errors='replace');warnings=[]
 for marker in ['SOURCE_LIVE_READ_NOT_ATOMIC','SQLITE_BUNDLE_PARTIAL']:
  if marker in error:warnings.append(marker)
 ok=code==0 and summary is not None and not guard_errors
 result={'host':host,'label':label,'ok':ok,'exit':code,'seconds':round(time.monotonic()-start,2),'summary':summary,'warnings':warnings,'guard_errors':guard_errors,'private_error_log':str(errpath)};state['jobs'].append(result);atomic(run/'RUN.json',state);atomic(STATUS,state);print(json.dumps({'job':label,'host':host,'ok':ok,'snapshot':summary.get('snapshot_id') if summary else None,'seconds':result['seconds']}),flush=True);return result
def stream(state,run,label,host,mode,*args):
 name='/hosts/'+host+'/'+label+('.dump' if mode=='pg-db' else '.tar' if mode in ['tar','sqlite-bundle'] else '.json' if mode=='state' else '.sql')
 return execute_backup(state,run,label,host,['--stdin-filename',name,'--stdin-from-command','--',*hostcmd(host,mode,*args)])
def github_catalog(state,publication):
 q=subprocess.run(['/usr/bin/gh','api','repos/'+CONFIG['github']],capture_output=True,text=True,timeout=20,check=True);assert json.loads(q.stdout)['private'],'Backup control repository became public'
 catalog={'run_id':state['run_id'],'status':state['status'],'started':state['started'],'updated':now(),'hf_dataset':publication['repo_id'],'verified_HF_revision':publication['revision'],'encrypted_bytes':publication['encrypted_bytes'],'encrypted_files_verified_present':publication['encrypted_files_verified_present'],'jobs':[{k:j.get(k) for k in ['host','label','ok','exit','seconds','summary','warnings','guard_errors']} for j in state['jobs']],'coverage_limits':state['coverage_limits']};(GIT/'catalog').mkdir(exist_ok=True);atomic(GIT/'catalog'/('run-'+state['run_id']+'.json'),catalog);atomic(GIT/'latest.json',catalog)
 paths=['catalog','latest.json']
 if state['status']=='COMPLETE':atomic(GIT/'latest-complete.json',catalog);paths.append('latest-complete.json')
 subprocess.run(['/usr/bin/git','-C',str(GIT),'add','--',*paths],check=True,capture_output=True)
 q=subprocess.run(['/usr/bin/git','-C',str(GIT),'diff','--cached','--quiet']);
 if q.returncode:subprocess.run(['/usr/bin/git','-C',str(GIT),'commit','-m','Record verified encrypted backup '+state['run_id']+' '+state['status']],check=True,capture_output=True)
 subprocess.run(['/usr/bin/git','-C',str(GIT),'-c','credential.helper=!gh auth git-credential','push','origin','main'],check=True,capture_output=True,timeout=60)
 return subprocess.run(['/usr/bin/git','-C',str(GIT),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
def publish(state,run):
 state['current_job']={'host':'cpu','label':'publish-encrypted-HF','started':now()};atomic(STATUS,state)
 q=subprocess.run(['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=20','-o','ServerAliveCountMax=3','root@100.103.27.39','nice -n 15 /var/lib/alloyscape-estate-backup/venv/bin/python /var/lib/alloyscape-estate-backup/publish-hf.py'],capture_output=True,text=True,timeout=8*3600);(run/'HF-PUBLISH.stderr').write_text(q.stderr)
 if q.returncode:raise RuntimeError('Encrypted HF publication failed; previous verified revisions remain intact')
 publication=json.loads(q.stdout);atomic(run/'HF-PUBLICATION.json',publication);state['last_verified_HF_publication']=publication;state['git_catalog_commit']=github_catalog(state,publication);atomic(STATUS,state);atomic(run/'RUN.json',state);print(json.dumps({'HF_revision':publication['revision'],'encrypted_bytes':publication['encrypted_bytes'],'catalog':state['git_catalog_commit']}),flush=True)
def exit_note():
 if STATUS.exists():
  d=json.loads(STATUS.read_text())
  if d.get('status')=='RUNNING':d.update({'status':'INTERRUPTED','updated':now(),'systemd_result':os.environ.get('SERVICE_RESULT','unknown')});atomic(STATUS,d)
def main():
 if '--exit-note' in sys.argv:exit_note();return
 lock=(ROOT/'controller.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);storage_guard()
 if '--preflight' in sys.argv:
  out={}
  for host in ['hq',*HOSTS]:
   q=subprocess.run(hostcmd(host,'inventory'),capture_output=True,text=True,timeout=45,check=True);d=json.loads(q.stdout);out[host]={'mounts':len(d['persistent_mount_roots']),'bind_views':len(d['bind_mount_map']),'postgres_databases':len(d['postgres_databases']),'active_SQLite':len(d['active_sqlite_files'])}
  print(json.dumps({'ok':True,'hosts':out,'repository':storage_guard()}));return
 stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ');run=ROOT/'runs'/stamp;run.mkdir(parents=True,mode=0o700);state={'run_id':stamp,'started':now(),'status':'RUNNING','jobs':[],'coverage_limits':['Persistent files and mount reconstruction metadata; transient kernel/runtime files and backup backend excluded','Live filesystem acquisition is not a globally atomic application snapshot','PostgreSQL exports are per-database transactions; SQLite exports are per-database transaction copies','Other database/storage engines are captured as live files; engine-specific consistency is not certified','External SaaS account state is not a filesystem snapshot'],'completed_hosts':[]};atomic(STATUS,state)
 try:
  for host in ['hq',*HOSTS]:
   q=subprocess.run(hostcmd(host,'inventory'),capture_output=True,text=True,timeout=45)
   if q.returncode:state['jobs'].append({'host':host,'label':'inventory','ok':False,'exit':q.returncode});atomic(STATUS,state);continue
   inv=json.loads(q.stdout);atomic(run/(host+'-INVENTORY.json'),inv);stream(state,run,'live-process-and-mount-state',host,'state')
   if inv['postgres_databases']:
    stream(state,run,'postgres-globals',host,'pg-globals')
    for db in inv['postgres_databases']:
     label='postgres-'+hashlib.sha256(db['name'].encode()).hexdigest()[:12];stream(state,run,label,host,'pg-db',db['name'])
   if inv['active_sqlite_files']:stream(state,run,'sqlite-consistent',host,'sqlite-bundle')
   # Publish consistent state/database backups before the potentially long full filesystem read.
   publish(state,run)
   if host=='hq':
    exclusions=['--exclude=/'+x for x in ['proc','sys','dev','run','tmp','var/tmp','var/cache','swapfile','var/lib/alloyscape-estate-backup']]+['--exclude='+x['target'] for x in inv['bind_mount_map']]
    execute_backup(state,run,'persistent-filesystem','hq',['--one-file-system',*exclusions,*inv['persistent_mount_roots']])
   else:stream(state,run,'persistent-filesystem',host,'tar')
   state['completed_hosts'].append(host);publish(state,run)
  q=subprocess.run(base()+['check'],capture_output=True,text=True,timeout=30*60);(run/'RESTIC-CHECK.stdout').write_text(q.stdout);(run/'RESTIC-CHECK.stderr').write_text(q.stderr);state['repository_metadata_check_passed']=q.returncode==0
  partial=any(not j.get('ok') or 'SQLITE_BUNDLE_PARTIAL' in j.get('warnings',[]) for j in state['jobs']) or not state['repository_metadata_check_passed'];state['status']='PARTIAL' if partial else 'COMPLETE';state['finished']=now();publish(state,run);atomic(STATUS,state);print(json.dumps({'run_id':stamp,'status':state['status'],'finished':state['finished']}),flush=True)
  if partial:raise SystemExit(3)
 except BaseException as exc:
  if state.get('status')=='RUNNING':state.update({'status':'FAILED','error_class':type(exc).__name__,'error_summary':str(exc)[:300],'finished':now()});atomic(STATUS,state);atomic(run/'RUN.json',state)
  raise
if __name__=='__main__':main()
