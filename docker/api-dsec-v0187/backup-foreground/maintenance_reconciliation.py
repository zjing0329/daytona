"""Read-only integrity checks for explicit natural warm-pool rotation evidence.
Hashes establish file integrity, not cryptographic authentication or signatures.
"""
import datetime,hashlib,json,pathlib,uuid,re
RUN='dd-app200-20260930-attempt3-formal-71b16897'
def sha(data):return hashlib.sha256(data).hexdigest()
def identity_hash(ids):return sha(json.dumps(sorted(ids)).encode())
def stamp(v):
 text=str(v).replace('Z','+00:00')
 text=re.sub(r'\.(\d+)(?=[+-]\d{2}:\d{2}$)',lambda m:'.'+m.group(1).ljust(6,'0'),text)
 try:result=datetime.datetime.fromisoformat(text)
 except ValueError:result=datetime.datetime.fromtimestamp(float(v),datetime.timezone.utc)
 assert result.tzinfo is not None,'Timestamp missing timezone'
 return result
def uuids(values,count):
 result={str(uuid.UUID(v))for v in values};assert len(values)==count and len(result)==count
 return result
WARM_FIELDS=('id','runnerId','snapshot','pool_profile','pool_state','pool_use_count','app_id','dd_app_id','session_id','closed_app_id','skill_bundle_version','warm_baseline_sha256')
def validate_current_warm_rows(rows,expected_rows):
 ids=uuids([r['id']for r in rows],80);assert ids=={r['id']for r in expected_rows},'Current warm identities changed'
 assert all(r['state']=='started'and r['desiredState']=='started'and r['pool_profile']=='default'and r['pool_state']=='idle'and r['pool_use_count']=='0'and not any(r.get(k)for k in ['app_id','dd_app_id','session_id','closed_app_id'])for r in rows),'Current warm pool unsafe'
 key=lambda r:{k:r.get(k)for k in WARM_FIELDS}
 assert sorted((key(r)for r in rows),key=lambda r:r['id'])==sorted((key(r)for r in expected_rows),key=lambda r:r['id']),'Current warm metadata drift'
 return ids
def validate_reconciliation(path,*,expected_run_id,max_age_seconds=900):
 path=pathlib.Path(path).resolve();raw=path.read_bytes();proof=json.loads(raw)
 assert expected_run_id==RUN and proof['runId']==RUN and proof['schemaVersion']==1
 assert proof['kind']=='natural-live-warm-pool-rotation'and proof['original80Unchanged']is False and proof['ttlSeconds']==21600
 now=datetime.datetime.now(datetime.timezone.utc);assert 0<=(now-stamp(proof['at'])).total_seconds()<=max_age_seconds,'Reconciliation stale'
 docs={};hashes={}
 for name in ['manifest','baseline','original_final','current_pool','delete_audit','live_evidence']:
  entry=proof['evidence'][name];rel=pathlib.Path(entry['path']);assert not rel.is_absolute()and'..'not in rel.parts
  source=(path.parent/rel).resolve();assert source.parent==path.parent,'Evidence path escapes private bundle'
  data=source.read_bytes();assert sha(data)==entry['sha256'],'Evidence hash mismatch: '+name
  docs[name]=json.loads(data);hashes[name]=entry['sha256']
 m=docs['manifest'];a=docs['original_final'];base=docs['baseline'];pool=docs['current_pool'];deleted=docs['delete_audit'];live=docs['live_evidence']
 assert m['run_id']==RUN and m['status']=='pass'and m.get('finished_at')and len(m['apps'])==200 and m.get('identity_cleanup')is True and all((x.get('cleanup')or{}).get('passed')is True and(x.get('restore')or{}).get('passed')is True for x in m['apps'])
 assert a['runId']==RUN and a['phase']=='final'and stamp(a['at'])>=stamp(m['finished_at'])and a['testAppCount']==200
 assert a['protectedWarm80Passed']is False and a['cleanupPassed']is False,'Original failed identity proof must remain unchanged'
 assert a['manifestCoveragePassed']and a['testTerminalStatesPassed']and a['claimUniquenessPassed']and not a['activeJobs']and not a['duplicateExecutions']and not a['missingRequiredLifecycleJobs']and not a['missingLifecycleExecutionCompletionEvidence']
 test_ids=uuids(a['auditedSandboxIds'],400);app_ids=uuids([x['app_id']for x in m['apps']],200)
 assert a['runtimeCountPerAppDistribution']=={'2':200}
 for kind in ['CREATE_SANDBOX','DESTROY_SANDBOX']:
  jobs=[j for j in a['jobs']if j['type']==kind];assert len(jobs)==400 and {j['resourceId']for j in jobs}==test_ids and all(j['status']=='COMPLETED'for j in jobs)
 failures={j['id']:j for j in a['failedJobs']};classified={x['id']:x for x in pool['failedBackupCategories']};assert set(failures)==set(classified)
 destroys={j['resourceId']:j for j in a['jobs']if j['type']=='DESTROY_SANDBOX'}
 for key,j in failures.items():
  c=classified[key];assert j['type']=='CREATE_BACKUP'and c['category']in ['context_canceled','container_not_found']and c['resourceId']==j['resourceId']
  assert stamp(j['completedAt'])>=stamp(destroys[j['resourceId']]['createdAt']),'Unexplained backup failure predates destruction'
 old_ids=uuids([x['id']for x in base['samples']['a1']['rows']],80)
 assert old_ids=={x['id']for x in a['protectedWarm']}=={x['id']for x in pool['original']}
 assert all(x['state']=='destroyed'and x['desiredState']=='destroyed'for x in pool['original'])
 current=pool['current'];current_ids=validate_current_warm_rows(current,current)
 assert not(current_ids&old_ids or current_ids&test_ids),'Replacement IDs overlap protected/test scopes'
 for field in ['snapshot','pool_profile','skill_bundle_version','warm_baseline_sha256']:
  assert {x[field]for x in current}=={x[field]for x in pool['original']},'Warm baseline fingerprint changed: '+field
 assert 0<=(now-stamp(pool['at'])).total_seconds()<=max_age_seconds,'Replacement metadata stale'
 assert proof['currentWarmIds']==sorted(current_ids)and proof['currentWarmIdentitySha256']==identity_hash(current_ids)
 assert proof['originalWarmIdentitySha256']==identity_hash(old_ids)
 meta={x['id']:x for x in deleted['poolMetadata']};assert set(meta)==old_ids
 audits=deleted['destroyAudits'];assert len(audits)==80 and {x['targetId']for x in audits}==old_ids
 assert len({x['actorFingerprint']for x in audits})==1
 ages=[]
 for row in audits:
  old=meta[row['targetId']];assert old['pool_profile']=='default'and old['pool_use_count']=='0'and row['action']=='delete'and row['statusCode']==200 and row['ipAddress']=='192.168.0.225'
  age=(stamp(row['createdAt'])-stamp(old['pool_created_at']or old['createdAt'])).total_seconds();assert age>=21600,'Deletion younger than configured max age';ages.append(age)
 assert live['ttlSeconds']==21600 and live['liveRuntimeMatchesSource']and live['sourceRetiresMaxAge']
 assert uuids(live['replacementCreatedIds'],80)==current_ids,'Replacement creation provenance incomplete'
 assert live['sourceEvidence']and all(x['path']and len(x['sha256'])==64 for x in live['sourceEvidence'])
 assert live['retirementLogEvidence']['exact_old_ids_with_direct_reason']==0,'Unexpected retirement-log evidence contract'
 return dict(current_warm_ids=current_ids,current_warm_rows=current,test_cleanup_passed=True,original80_unchanged=False,warm_rotation_reconciled=True,reconciliation_path=str(path),reconciliation_sha256=sha(raw),manifest_sha256=hashes['manifest'],original_final_sha256=hashes['original_final'],baseline_sha256=hashes['baseline'],current_warm_identity_sha256=identity_hash(current_ids),minimum_retirement_age_seconds=min(ages),maximum_retirement_age_seconds=max(ages),backup_cancellations_during_destroy=len(failures))
