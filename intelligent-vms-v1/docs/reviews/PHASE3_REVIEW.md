# Phase 3 reviewer checklist

Status: pre-CI implementation review.

## Positives
- main recording stream separated from live substream;
- continuous recorder path is not source-on-demand;
- source-copy fMP4, no mandatory transcode;
- 1-second default part/RPO and configurable segment duration;
- recording policy stays transactional while segment metadata goes to ClickHouse;
- playback path is derived from authorized camera/policy;
- internal playback server is not handed to the browser;
- per-path hot retention avoids broad filesystem deletion.

## Items that block production certification, not development merge
- real camera codec/interoperability recording tests;
- disk-full and crash tests;
- object-storage archive worker;
- legal hold;
- regional recording-node placement;
- playback node routing for multiple regions;
- Prometheus recording metrics;
- production OIDC UI.

## Merge gate
Automated CI plus migration and image builds must pass. Any test showing main/sub confusion, arbitrary playback path injection, unsafe retention or recording reconciliation failure is a blocker.
