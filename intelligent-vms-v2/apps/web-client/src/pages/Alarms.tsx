/**
 * Alarm management.
 *
 * The important behaviour here is optimistic concurrency: every mutation
 * sends the `version` the operator actually saw. If someone else acted first
 * the server returns 409 `stale_version` and we refresh rather than
 * overwriting their decision.
 */
import { useEffect, useState } from "react";
import { Alarm, api } from "../lib/api";

export function AlarmsPage() {
  const [alarms, setAlarms] = useState<Alarm[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    setAlarms((await api.listAlarms({ state: ["new", "active", "acknowledged"] })).items);
  }

  useEffect(() => {
    load();
    const stream = api.openEventStream();
    // Any inbound event may change alarm state; refresh rather than guess.
    stream.onmessage = () => load();
    return () => stream.close();
  }, []);

  async function acknowledge(alarm: Alarm) {
    try {
      await api.acknowledgeAlarm(alarm.id, alarm.version);
      setNotice(null);
    } catch (e: any) {
      if (e?.code === "stale_version") {
        setNotice(
          `${alarm.title} was already handled by another operator (now ${e.current_state}).`,
        );
      } else {
        setNotice(e?.detail ?? "Could not acknowledge the alarm");
      }
    } finally {
      load();
    }
  }

  return (
    <section className="page">
      <h1>Alarms</h1>
      {notice && <p role="status" className="notice">{notice}</p>}
      <ul className="alarm-list">
        {alarms.map((alarm) => (
          <li key={alarm.id} className={`alarm alarm--${alarm.severity}`}>
            <div>
              <strong>{alarm.title}</strong>
              <small>
                {new Date(alarm.first_event_at).toLocaleString()} · {alarm.state}
                {alarm.event_count > 1 && ` · ${alarm.event_count} occurrences`}
              </small>
            </div>
            <div className="alarm__actions">
              {alarm.state !== "acknowledged" && (
                <button onClick={() => acknowledge(alarm)}>Acknowledge</button>
              )}
              {alarm.camera_id && (
                <a href={`/playback/${alarm.camera_id}?at=${alarm.first_event_at}`}>
                  Open footage
                </a>
              )}
            </div>
          </li>
        ))}
        {alarms.length === 0 && <li className="muted">No open alarms.</li>}
      </ul>
    </section>
  );
}
