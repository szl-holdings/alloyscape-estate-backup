#!/usr/bin/env python3
# Read-only source acquisition for encrypted backups. stdout is backup payload, never operator logging.
from pathlib import Path
import sys,os,json,subprocess,shutil,sqlite3,tempfile,time,urllib.parse
EXCLUDED=['proc','sys','dev','run','tmp','var/tmp','var/cache','swapfile','var/lib/alloyscape-estate-backup']
def run(args,**kw):return subprocess.run(args,**kw)
def mountmap():
 q=run(['findmnt','-J','-o','TARGET,SOURCE,FSTYPE,FSROOT,MAJ:MIN'],capture_output=True,text=True,check=True);rows=[]
 def visit(items):
  for x in items:
   if str(x.get('source','')).startswith('/dev/') and x.get('fstype') not in ['squashfs','overlay']:rows.append({k:v for k,v in x.items() if k!='children'})
   visit(x.get('children',[]))
 visit(json.loads(q.stdout)['filesystems']);canonical={}
 for x in rows:
  if x.get('fsroot')=='/' and x.get('maj:min') not in canonical:canonical[x.get('maj:min')]=x['target']
 aliases=[];roots=[]
 for x in rows:
  is_alias=x.get('fstype') not in ['btrfs','zfs'] and x.get('fsroot','/')!='/'
  if is_alias:
   base=canonical.get(x.get('maj:min'))
   if base:aliases.append({'target':x['target'],'canonical':str(Path(base)/x['fsroot'].lstrip('/'))})
  elif x['target'] not in roots:roots.append(x['target'])
 return ['/']+[x for x in roots if x!='/'],aliases

def mounts():return mountmap()[0]
def pginfo():
 if not shutil.which('pg_isready'):return []
 if run(['pg_isready','-q'],timeout=6).returncode:return []
 q=run(['runuser','-u','postgres','--','psql','-XAt','-d','postgres','-c',"SELECT json_build_object('name',datname,'bytes',pg_database_size(oid)) FROM pg_database WHERE datallowconn AND NOT datistemplate"],capture_output=True,text=True,timeout=15,check=True);return [json.loads(x) for x in q.stdout.splitlines() if x]
def sqlitefiles():
 found=set();seen=set();aliases=mountmap()[1]
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:fds=list((p/'fd').iterdir())
  except OSError:continue
  for f in fds:
   try:
    name=os.readlink(f)
    if not name.startswith('/') or not name.lower().endswith(('.db','.sqlite','.sqlite3')):continue
    for alias in sorted(aliases,key=lambda x:len(x['target']),reverse=True):
     if name.startswith(alias['target']+'/'):name=alias['canonical']+name[len(alias['target']):];break
    st=os.stat(name);identity=(st.st_dev,st.st_ino)
    if identity in seen:continue
    seen.add(identity)
    if any(name.startswith('/'+x+'/') for x in EXCLUDED):continue
    with open(name,'rb') as stream:head=stream.read(16)
    if head==b'SQLite format 3\x00':found.add(name)
   except OSError:pass
 return sorted(found)
def state():
 processes=[]
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   cmd=(p/'cmdline').read_bytes().decode('utf8','replace').split(chr(0));env=(p/'environ').read_bytes().decode('utf8','replace').split(chr(0));processes.append({'pid':int(p.name),'uid':p.stat().st_uid,'cmdline':cmd,'environ':env})
  except OSError:pass
 return {'captured_UTC':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'processes':processes,'uname':list(os.uname()),'mount_roots':mounts(),'bind_mount_map':mountmap()[1],'transient_and_backend_exclusions':EXCLUDED}
mode=sys.argv[1]
if mode=='inventory':print(json.dumps({'persistent_mount_roots':mounts(),'postgres_databases':pginfo(),'active_sqlite_files':sqlitefiles(),'bind_mount_map':mountmap()[1],'transient_and_backend_exclusions':EXCLUDED}))
elif mode=='state':sys.stdout.write(json.dumps(state()))
elif mode=='pg-globals':raise SystemExit(run(['runuser','-u','postgres','--','pg_dumpall','--globals-only'],stdout=sys.stdout.buffer).returncode)
elif mode=='pg-db':
 name=sys.argv[2]
 if '=' in name or '://' in name:raise SystemExit('Unexpected database name; inspect before exporting')
 raise SystemExit(run(['runuser','-u','postgres','--','pg_dump','--format=custom','--dbname',name],stdout=sys.stdout.buffer).returncode)
elif mode=='sqlite':
 p=Path(sys.argv[2]);free=shutil.disk_usage('/var/tmp').free;reserve=max(2*1024**3,min(20*1024**3,free//2));need=p.stat().st_size*2+reserve
 if shutil.disk_usage('/var/tmp').free<need:raise SystemExit('Not enough temporary space for a consistent SQLite copy')
 with tempfile.TemporaryDirectory(prefix='alloyscape-sqlite-backup-',dir='/var/tmp') as d:
  src=sqlite3.connect('file:'+urllib.parse.quote(str(p))+'?mode=ro',uri=True,timeout=20);dst=sqlite3.connect(str(Path(d)/'copy.sqlite'));deadline=time.monotonic()+90
  def progress(status,remaining,total):
   if time.monotonic()>deadline:raise RuntimeError('SQLite copy exceeded bounded acquisition time')
  try:src.backup(dst,pages=512,progress=progress,sleep=0.01);assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
  finally:dst.close();src.close()
  with (Path(d)/'copy.sqlite').open('rb') as f:shutil.copyfileobj(f,sys.stdout.buffer,1024*1024)
elif mode=='sqlite-bundle':
 import tarfile,io,hashlib
 records=[];failures=[]
 with tempfile.TemporaryDirectory(prefix='alloyscape-sqlite-bundle-',dir='/var/tmp') as tmp,tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
  for name in sqlitefiles():
   try:
    original=Path(name);free=shutil.disk_usage('/var/tmp').free;reserve=max(2*1024**3,min(20*1024**3,free//2))
    if free<original.stat().st_size*2+reserve:raise RuntimeError('temporary space guard')
    copy=Path(tmp)/'copy.sqlite';src=sqlite3.connect('file:'+urllib.parse.quote(name)+'?mode=ro',uri=True,timeout=20);dst=sqlite3.connect(copy);deadline=time.monotonic()+90
    def progress(status,remaining,total):
     if time.monotonic()>deadline:raise RuntimeError('SQLite acquisition timeout')
    try:src.backup(dst,pages=512,progress=progress,sleep=0.01);assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    finally:dst.close();src.close()
    st=original.stat();entry=tarfile.TarInfo('databases/'+name.lstrip('/'));entry.size=copy.stat().st_size;entry.mode=st.st_mode&0o777;entry.uid=st.st_uid;entry.gid=st.st_gid;entry.mtime=int(time.time())
    with copy.open('rb') as f:archive.addfile(entry,f)
    records.append({'original_path':name,'archive_path':entry.name,'bytes':entry.size,'quick_check':'ok','captured_UTC':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())})
    copy.unlink()
   except Exception as e:failures.append({'original_path':name,'error':type(e).__name__+': '+str(e)[:250]})
  data=json.dumps({'databases':records,'failures':failures,'consistency':'individual SQLite transaction snapshots;not cross-database atomic'},indent=2).encode();entry=tarfile.TarInfo('SQLITE-MANIFEST.json');entry.size=len(data);entry.mode=0o600;archive.addfile(entry,io.BytesIO(data))
 if failures:sys.stderr.write('SQLITE_BUNDLE_PARTIAL failures='+str(len(failures))+chr(10))
elif mode=='tar':
 args=['tar','--one-file-system','--acls','--xattrs','--numeric-owner','--sparse','--sort=name','-C','/']+['--exclude=./'+x for x in EXCLUDED]+['--exclude=./'+x['target'].lstrip('/') for x in mountmap()[1]]+['-cpf','-','.']+[x.lstrip('/') for x in mounts() if x!='/']
 q=subprocess.Popen(args,stdout=sys.stdout.buffer,stderr=subprocess.PIPE);_,err=q.communicate();sys.stderr.buffer.write(err)
 if q.returncode==1 and b'file changed as we read it' in err and b'Permission denied' not in err and b'Cannot open' not in err:
  sys.stderr.write('SOURCE_LIVE_READ_NOT_ATOMIC: one or more files changed during read; database exports are separate.\n');raise SystemExit(0)
 raise SystemExit(q.returncode)
else:raise SystemExit('Unknown source mode')
