# Phase 3 QA plan and evidence

## Automated gates

- recording source uses camera main path, never the live substream;
- record stream key deterministic;
- policy bounds reject unsafe retention/segment/RPO values;
- MediaMTX recording path is always-on and fMP4;
- hook duration parsing;
- segment start extraction;
- desired-state reconciliation restores missing recording path;
- Alembic migration adds recording_policies;
- compile and Compose validation.

## Integration gates

Before production:
1. continuous record from at least two real camera vendors;
2. H.264 and H.265 main-stream recording;
3. abrupt MediaMTX kill: measured loss <= configured part RPO plus filesystem effects;
4. media-node restart: live and recording paths recover;
5. playback while recording;
6. playback across segment boundary;
7. retention expiry removes only the intended path files;
8. disk-full / read-only filesystem behavior;
9. 24h soak with no recording gaps;
10. secret scan of hook/config/log output.

## Scale gates

Synthetic metadata:
- 100K logical policies;
- completed-segment event rate matching selected segment duration;
- Kafka lag;
- ClickHouse insert/query rate.

Media:
- increase aggregate source-copy Gbps per node until one resource reaches safe limit;
- mixed playback during write load;
- reconnect storm after recorder restart.

No "channels per recorder" production claim is accepted until these measurements exist.
