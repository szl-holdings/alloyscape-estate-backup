# AlloyScape daily encrypted estate backup

Authorized by Rosa. Implemented by Codex; no AI member is claimed to have performed this work.

- Private GitHub control code and verified snapshot catalog: szl-holdings/alloyscape-estate-backup.
- Private HF encrypted data repository: SZLHOLDINGS/alloyscape-encrypted-estate-backup, under restic/.
- Working encrypted repository on the existing CPU server; HQ does not build full-disk archives.
- Daily schedule: 03:15 UTC. First full acquisition is a separate run; a passing canary is not a full backup.
- Source scope: HQ, Old Island (including its persistent volume), Massed, and CPU server persistent files; PostgreSQL logical exports, transaction-consistent copies of detected open SQLite databases, and live process/mount metadata.
- Mounted views of existing directories are excluded as duplicate data, with reconstruction maps preserved. Transient kernel/runtime/cache directories and the backup repository itself are excluded.
- Live files are not a globally atomic application snapshot. Other database engines are included as live files; their engine-specific recovery consistency is not certified.
- No model calls, no new paid storage plan, no automatic snapshot pruning, no deletion of source data.
- The controller stops at low disk space, a 500 GiB repository cap, failed exports, public repository visibility, or failed publication. Partial runs remain marked PARTIAL/FAILED; previous recovery points remain intact.

## Recovery

1. Obtain the separate offline RECOVERY.txt held by Rosa. It is not stored here or as plaintext on HF.
2. Choose a COMPLETE catalog entry (latest-complete.json when available); an initial canary catalog is not a full estate backup. Each entry pins an immutable HF commit.
3. Authenticated to the private HF dataset, download restic/ at that exact revision. Verify repository integrity with restic check using the recovery password.
4. Restore the selected filesystem snapshot into an empty staging directory. Remote filesystem snapshots are uncompressed tar streams inside restic and must be restored or streamed through tar with ACL/xattr support. Never automatically unpack over a live estate.
5. Restore PostgreSQL custom-format database archives and globals into an isolated compatible PostgreSQL installation first. Consistent SQLite tar bundles include SQLITE-MANIFEST.json.
6. Reconstruct documented mounts and services only after reviewing the restored configuration and verifying source versions.

## Evidence and status

The first encrypted canary was downloaded from HF commit d27c73157a76489a44683c4585c1cf78f98177ff and restored byte-for-byte (SHA-256 matched). This proves that small recovery path only.
Current HQ status: /opt/alloyscape/ops/estate-backup/LAST-STATUS.json.
Run receipts: /opt/alloyscape/ops/estate-backup/runs/.
Recovery key: /root/.config/alloyscape-backup/RECOVERY.txt (root-private).
