# Phase 6 Research — AI interoperability and runtimes

The VMS supports two AI sources: camera/edge metadata and replaceable inference providers. ONVIF Profile M is the interoperability path for standardized analytics metadata/events. Server-side providers are deployment artifacts and publish bounded results back to the VMS.

Model weights are never committed. Model/provider catalog references use approved model:// or provider:// identifiers with optional SHA-256 identity. Face detection/recognition events can be transported, but a biometric identity/gallery database is outside this base phase and requires separate security/retention review.

The orchestration layer records stream role, sample FPS, enabled analytics, zones/lines and confidence threshold. Capacity is benchmarked by exact model/runtime/hardware, not model name or camera count.
