"""Load burn cycles recorded by the StoveLink service (HDF5) for model fitting."""
import h5py, glob, numpy as np, os
ROOT = os.environ.get("BURNIE_DATA", "/Volumes/Burnie").rstrip("/") + "/"
ev=np.zeros((24,32),bool); ev[1::2,::2]=True; ev[::2,1::2]=True

def load(f):
    h=h5py.File(f,'r'); n=h['sequence_ids'].shape[0]
    fr=h['mlx90640_frames'][:]
    t=(h['timestamps_ms'][:].astype(float)-h['timestamps_ms'][0])/1000
    u=1-h['vent_positions'][:]/100.0
    good=np.abs(fr[:,ev].mean(1)-fr[:,~ev].mean(1))<10
    mx=fr.max(axis=(1,2)).astype(float)
    mx[~good]=np.interp(t[~good],t[good],mx[good])
    return t,u,mx

def linear_burns():
    """Burns where the vent closed monotonically with no manual reopening (linear closer only)."""
    out=[]
    for f in sorted(glob.glob(ROOT+'20*/*/*.h5')):
        if 'part' in f: continue
        try:
            with h5py.File(f,'r') as h: n=h['sequence_ids'].shape[0]
        except OSError:
            continue
        if not (500<n<2600): continue
        t,u,mx=load(f)
        if u[0]>0.05: continue
        du=np.diff(u)
        if (du< -0.03).any(): continue        # any reopening
        close_i=np.argmax(u>0.95)
        if close_i==0 or t[close_i]>90*60: continue
        # truncate at end: drop the trailing reopen to 100% (next reload)
        end=n-1
        while end>0 and u[end]<0.9: end-=1
        out.append((os.path.basename(f),t[:end],u[:end],mx[:end]))
    return out


def reopen_burns(max_reopen_open=0.7):
    """Burns with manual partial re-openings (vent never back above max_reopen_open open mid-burn)."""
    out=[]
    for f in sorted(glob.glob(ROOT+'20*/*/*.h5')):
        if 'part' in f: continue
        try:
            with h5py.File(f,'r') as h: n=h['sequence_ids'].shape[0]
        except OSError:
            continue
        if not (500<n<2600): continue
        t,u,mx=load(f)
        if u[0]>0.05: continue
        end=n-1
        while end>0 and u[end]<0.9: end-=1
        t,u,mx=t[:end],u[:end],mx[:end]
        first_closed=np.argmax(u>0.6)
        if first_closed==0: continue
        rest=u[first_closed:]
        reopen=(np.diff(u)< -0.08).any()
        if reopen and rest.min()>1-max_reopen_open and t[-1]>60*60:
            out.append((os.path.basename(f),t,u,mx))
    return out
