# Phase 5 Research — ONVIF Events and Diagnostics

## Research Agent findings

### PullPoint is the baseline real-time event path

The ONVIF Core specification requires the real-time Pull-Point notification interface. The core flow is:

1. obtain the Event service XAddr;
2. CreatePullPointSubscription;
3. preserve the returned SubscriptionReference, including ReferenceParameters;
4. optionally SetSynchronizationPoint;
5. continuously PullMessages;
6. renew/recreate as required;
7. Unsubscribe during controlled shutdown when possible.

PullMessages is a long poll. The HTTP client timeout therefore has to be longer than the ONVIF PullMessages Timeout. The VMS uses a 30-second pull with 35 seconds of HTTP headroom.

Official references:
- https://www.onvif.org/specs/core/ONVIF-Core-Specification-v260.pdf
- https://www.onvif.org/wp-content/uploads/2022/07/ONVIF_Event_Handling_Device_Test_Specification_21.12.pdf

### Event normalization

Profile T includes motion/tampering events and metadata alongside advanced video streaming.

Profile M standardizes analytics metadata/events including object counters, faces and license-plate-related applications. Profile M can carry events in metadata stream, ONVIF Event service or MQTT where supported.

References:
- https://www.onvif.org/profiles/profile-t/
- https://www.onvif.org/profiles/profile-m/

The VMS therefore stores the original ONVIF topic and SimpleItem payload in attributes, while mapping common topics to vendor-neutral event types such as motion, tamper, digital_input, tripwire, intrusion, anpr and face.

### Synchronization events

SetSynchronizationPoint can produce Initialized property events that represent current state rather than a newly occurring alarm. These remain searchable but the alarm engine explicitly does not open alarms from PropertyOperation=Initialized.

### Diagnostics

MediaMTX exposes Prometheus metrics for path bytes and RTSP session RTP packet count, packet loss, errors and jitter.

Reference:
- https://mediamtx.org/docs/features/metrics

Current API provides a current diagnostic sample. Production historical dashboards should scrape MediaMTX with Prometheus and calculate rates there rather than rely on one control-api process's in-memory byte delta.
