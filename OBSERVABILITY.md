# SDK observability

The private runtime supplies structured logging, metrics, tracing context, server/client spans, and transport exporters. Bounded task admission exposes in-flight and dropped-task counters; recorder shutdown drains admitted work. The public client raises typed errors so consumers can log status and retry context without exposing credentials.

Consumers should chart span admission/drop, sink failure, trace queue age, dispatcher backlog, HTTP retry rate, client latency, and pool saturation. Alert thresholds and sampling policy belong to consuming services. Do not treat a successful local enqueue as proof of sink delivery.

Local tests cover task caps and shutdown behavior. No production trace sink, dashboard, alert, or end-to-end delivery sample was checked.
