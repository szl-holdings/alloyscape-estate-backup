#!/usr/bin/env python3
from pathlib import Path
import os,json,sys,time,hashlib,fcntl
os.umask(0o077)
os.environ.update({'HF_HUB_DISABLE_PROGRESS_BARS':'1','RAYON_NUM_THREADS':'2','HF_XET_NUM_CONCURRENT_RANGE_GETS':'4'})
from huggingface_hub import HfApi,snapshot_download
ROOT=Path('/var/lib/alloyscape-estate-backup');REPO=ROOT/'restic';ID=json.loads((ROOT/'destination.json').read_text())['hf_dataset'];TOKEN=(ROOT/'secrets/hf-token').read_text().strip();api=HfApi(token=TOKEN)
def publish(verify_small=False):
 lock=(ROOT/'publish.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 info=api.repo_info(ID,repo_type='dataset');assert info.private,'Refusing to upload estate backup to a public repository'
 expected=[];total=0
 for base in ['data','index','keys','snapshots']:
  for p in (REPO/base).rglob('*'):
   if p.is_file():expected.append(p.relative_to(REPO).as_posix());total+=p.stat().st_size
 expected.append('config');total+=(REPO/'config').stat().st_size
 assert total<=500*1024**3,'Encrypted repository exceeded 500GiB safety cap; no new storage spending authorized'
 # First commit all encrypted packs, keys and indexes. Publish snapshot references only after dependencies exist.
 data=api.upload_folder(repo_id=ID,repo_type='dataset',folder_path=REPO,path_in_repo='restic',allow_patterns=['config','data/**','index/**','keys/**'],commit_message='Upload encrypted restic data before snapshot publication')
 snapshots=api.upload_folder(repo_id=ID,repo_type='dataset',folder_path=REPO,path_in_repo='restic',allow_patterns=['snapshots/**'],commit_message='Publish completed encrypted snapshot references')
 revision=api.repo_info(ID,repo_type='dataset').sha
 files=set(api.list_repo_files(ID,repo_type='dataset',revision=revision));missing=[p for p in expected if 'restic/'+p not in files];assert not missing, 'Missing encrypted repository objects: '+str(len(missing))
 record={'repo_id':ID,'private':True,'revision':revision,'encrypted_files_verified_present':len(expected),'encrypted_bytes':total,'published_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'snapshot_refs_committed_last':True,'data_commit':str(data),'snapshot_commit':str(snapshots)}
 if verify_small:
  assert total<100*1024**2,'Small restore verification is limited to 100MiB; use selected recovery for full snapshots'
  dest=ROOT/'hf-restore-verification'/revision;dest.mkdir(parents=True,exist_ok=True)
  snapshot_download(ID,repo_type='dataset',revision=revision,token=TOKEN,local_dir=dest,allow_patterns=['restic/**'],max_workers=2)
  record['downloaded_HF_repository_for_restore']=str(dest/'restic')
 (ROOT/'LAST-PUBLICATION.json').write_text(json.dumps(record,indent=2));return record
if __name__=='__main__':print(json.dumps(publish('--verify-small' in sys.argv)))
