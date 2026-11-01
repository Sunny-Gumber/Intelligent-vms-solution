import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { VideoTile } from '../components/VideoTile';
import { api } from '../lib/api';
import type { Camera } from '../lib/types';

const LAYOUTS = [
  { label: '1x1', rows: 1, cols: 1 },
  { label: '2x2', rows: 2, cols: 2 },
  { label: '3x3', rows: 3, cols: 3 },
  { label: '4x4', rows: 4, cols: 4 },
  { label: '5x5', rows: 5, cols: 5 },
];

export default function Live() {
  const [layoutIndex, setLayoutIndex] = useState(1);
  const [assignments, setAssignments] = useState<Record<number, string>>({});

  const cameras = useQuery({
    queryKey: ['cameras'],
    queryFn: () => api.get<{ items: Camera[] }>('/cameras?limit=500'),
  });

  const layout = LAYOUTS[layoutIndex]!;
  const tileCount = layout.rows * layout.cols;

  // Tile size drives the server's stream choice: a 4x4 wall asks for sub
  // streams, a single-camera view asks for main. See app/media/stream_policy.py.
  const tileSize = useMemo(() => ({
    width: Math.round(1600 / layout.cols),
    height: Math.round(900 / layout.rows),
  }), [layout]);

  function assign(index: number, cameraId: string) {
    setAssignments((previous) => ({ ...previous, [index]: cameraId }));
  }

  return (
    <>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 14 }}>
        <strong>Live monitoring</strong>
        <select value={layoutIndex} onChange={(e) => setLayoutIndex(Number(e.target.value))}>
          {LAYOUTS.map((item, index) => (
            <option key={item.label} value={index}>{item.label}</option>
          ))}
        </select>
        <span className="muted">
          Tiles are independent: one camera failing never stops the others.
        </span>
      </div>

      <div className="tiles"
           style={{ gridTemplateColumns: `repeat(${layout.cols}, 1fr)` }}>
        {Array.from({ length: tileCount }, (_, index) => {
          const cameraId = assignments[index];
          const camera = cameras.data?.items.find((c) => c.id === cameraId);
          return (
            <div key={index} className="tile"
                 onDragOver={(e) => e.preventDefault()}
                 onDrop={(e) => assign(index, e.dataTransfer.getData('text/camera-id'))}>
              {cameraId && camera ? (
                <>
                <VideoTile cameraId={camera.id} cameraName={camera.name}
                           width={tileSize.width} height={tileSize.height} />
                <button aria-label={`Close ${camera.name}`} onClick={() => setAssignments((p) => {
                             const next = { ...p };
                             delete next[index];
                             return next;
                           })}>Close</button>
                </>
              ) : (
                <div className="error" style={{ color: 'var(--muted)' }}>
                  Drag a camera here
                </div>
              )}
            </div>
          );
        })}
      </div>

      <h3 style={{ marginTop: 20 }}>Cameras</h3>
      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))' }}>
        {cameras.data?.items.map((camera) => (
          <div key={camera.id} className="card" draggable
               onDragStart={(e) => e.dataTransfer.setData('text/camera-id', camera.id)}
               style={{ cursor: 'grab', padding: 10 }}>
            <div>{camera.name}</div>
            <div className="muted">{camera.host} · {camera.state}</div>
          </div>
        ))}
      </div>
    </>
  );
}
