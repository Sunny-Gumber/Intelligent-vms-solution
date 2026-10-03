#!/usr/bin/env python3
import argparse, math

def structural(cameras, enabled_fraction, sample_fps, width, height):
    """Calculate structural AI sampling and decode load.

    Args:
        cameras: Total camera count.
        enabled_fraction: Fraction with AI sampling enabled.
        sample_fps: Sampled frames per second per active camera.
        width: Sample frame width.
        height: Sample frame height.

    Returns:
        Active-camera, sampled-FPS and decoded-MPix/s load dictionary.
    """
    active=math.ceil(cameras*max(0,min(1,enabled_fraction)))
    fps=active*sample_fps
    return {"active_cameras":active,"sampled_frames_per_second":fps,"decoded_megapixels_per_second":fps*width*height/1e6}

def measured(load, measured_fps_per_worker=None, measured_mpix_per_worker=None, headroom=1.3):
    """Estimate AI worker count only from supplied measured throughput.

    Args:
        load: Structural AI load dictionary.
        measured_fps_per_worker: Measured frames/s per worker.
        measured_mpix_per_worker: Measured decoded MPix/s per worker.
        headroom: Design headroom multiplier.

    Returns:
        workers_required when measured throughput is supplied, otherwise empty dict.
    """
    workers=0
    if measured_fps_per_worker: workers=max(workers,math.ceil(load["sampled_frames_per_second"]*headroom/measured_fps_per_worker))
    if measured_mpix_per_worker: workers=max(workers,math.ceil(load["decoded_megapixels_per_second"]*headroom/measured_mpix_per_worker))
    return {"workers_required":workers} if workers else {}

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--cameras",type=int,default=100000);p.add_argument("--enabled-fraction",type=float,default=.1);p.add_argument("--sample-fps",type=float,default=2);p.add_argument("--width",type=int,default=640);p.add_argument("--height",type=int,default=360);p.add_argument("--measured-fps-per-worker",type=float);p.add_argument("--measured-mpix-per-worker",type=float);p.add_argument("--headroom",type=float,default=1.3);a=p.parse_args()
    load=structural(a.cameras,a.enabled_fraction,a.sample_fps,a.width,a.height); out={**load,**measured(load,a.measured_fps_per_worker,a.measured_mpix_per_worker,a.headroom)}
    [print(f"{k}: {v:.3f}" if isinstance(v,float) else f"{k}: {v}") for k,v in out.items()]
    if "workers_required" not in out: print("Hardware count not calculated: provide measured target throughput.")
