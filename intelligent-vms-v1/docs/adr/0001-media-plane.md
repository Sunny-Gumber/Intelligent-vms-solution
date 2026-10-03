# ADR-0001: Distributed media plane

## Decision
Use MediaMTX-class media servers as replaceable, sharded media nodes. Do not make one node the source of truth for camera configuration.

## Reason
Media transport, WebRTC/HLS conversion and RTSP pulling are solved problems. The VMS should own orchestration, recording policy, intelligence and UX. At 100K+ channels, path configuration and failure handling must be partitioned across many nodes.

## Consequence
A regional placement/reconciliation service becomes mandatory before large-scale deployment.
