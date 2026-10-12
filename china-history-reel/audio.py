import numpy as np, wave, sys
sr=48000; DUR=15.0; N=int(sr*DUR)
L=np.zeros(N); R=np.zeros(N)
rs=np.random.default_rng(3)
def T(s): return np.arange(int(sr*s))/sr
def add(sig,t,gain=1.0,pan=0.0):
    i=int(t*sr); 
    if i>=N: return
    s=sig[:N-i]*gain; l=np.cos((pan+1)*np.pi/4); r=np.sin((pan+1)*np.pi/4)
    L[i:i+len(s)]+=s*l*1.414; R[i:i+len(s)]+=s*r*1.414
def lp(x,a):  # one-pole lowpass, a in (0,1)
    y=np.empty_like(x); acc=0.0
    for i in range(len(x)): acc+=a*(x[i]-acc); y[i]=acc
    return y
def lpv(x,cut):  # fast FFT brickwall-ish lowpass
    X=np.fft.rfft(x); f=np.fft.rfftfreq(len(x),1/sr); X*=1/(1+(f/cut)**4); return np.fft.irfft(X,len(x))
def bpv(x,lo,hi):
    X=np.fft.rfft(x); f=np.fft.rfftfreq(len(x),1/sr); X*=(1/(1+(f/hi)**4))*(1/(1+(lo/np.maximum(f,1))**4)); return np.fft.irfft(X,len(x))
def taiko(big=1.0):
    t=T(1.4); f=48+90*np.exp(-t*28); ph=2*np.pi*np.cumsum(f)/sr
    body=np.sin(ph)*np.exp(-t*(4.5 if big>0.7 else 8))
    click=lpv(rs.standard_normal(len(t)),1800)*np.exp(-t*60)*0.6
    return np.tanh((body+click)*1.6*big)
def tom():
    t=T(0.5); f=110+120*np.exp(-t*35); return np.sin(2*np.pi*np.cumsum(f)/sr)*np.exp(-t*12)*0.5
def pluck(freq,dur=1.6,bright=0.6):
    n=int(sr*dur); P=int(sr/freq); y=np.zeros(n)
    exc=rs.uniform(-1,1,P); exc=lpv(np.concatenate([exc,np.zeros(P)]),2000+6000*bright)[:P]
    y[:P]=exc; d=0.997
    for s in range(P,n,P):
        e=min(n,s+P); prev=y[s-P:e-P]; prev2=np.concatenate([[y[s-P-1] if s-P-1>=0 else 0],prev[:-1]])
        y[s:e]=d*0.5*(prev+prev2)
    t=T(dur)[:n]; y*=np.minimum(1,t*400)
    # guzheng-ish metallic partial
    y+=0.15*np.sin(2*np.pi*freq*2*t)*np.exp(-t*6)
    return y/ (np.max(np.abs(y))+1e-9)
def whoosh(dur=0.35,lo=300,hi=4000,rev=False):
    t=T(dur); n=rs.standard_normal(len(t)); env=np.sin(np.pi*t/dur)**2
    if rev: env=(t/dur)**3
    # sweep: blend two bands
    a=bpv(n,lo,hi*0.4); b=bpv(n,lo*3,hi); m=t/dur
    return (a*(1-m)+b*m)*env*0.5
def gong(f0=98,dur=4.0):
    t=T(dur); s=np.zeros(len(t))
    for k,(r,a,dk) in enumerate([(1,1,.9),(1.47,.6,1.2),(1.98,.5,1.5),(2.44,.35,2),(2.9,.3,2.6),(3.56,.2,3.2),(4.2,.15,4)]):
        s+=a*np.sin(2*np.pi*f0*r*t*(1-0.004*np.exp(-t*2))+k)*np.exp(-t*dk)
    s*=np.minimum(1,t*200); return s/np.max(np.abs(s))
# --- notes: D major pentatonic
def hz(m): return 440*2**((m-69)/12)
D4=62; scale=[0,2,4,7,9]
def deg(d,base=D4): o,k=divmod(d,5); return hz(base+12*o+scale[k])

# drone
t=T(DUR); env=np.clip((t-0.3)/1.5,0,1)*np.clip((12.1-t)/0.3,0,1)
drone=(np.sin(2*np.pi*hz(38)*t)*0.5+np.sin(2*np.pi*hz(45)*t)*0.25+np.sin(2*np.pi*hz(50)*t+np.sin(t*0.7))*0.15)*(0.6+0.4*np.sin(t*1.3))
drone=drone*env*(0.5+0.5*np.clip((t-1.5)/10,0,1))
add(drone,0,0.10)

# intro: drop tick + impact
tt=T(0.12); add(np.sin(2*np.pi*(1500-6000*tt)*tt)*np.exp(-tt*40),0.27,0.25)
add(taiko(1.0),0.28,0.9); add(whoosh(0.6,200,2500),0.28,0.25)
for i,d in enumerate([0,2,4,5,7,9]): add(pluck(deg(d),1.4,0.5),0.5+i*0.11,0.22,pan=-0.5+i*0.2)
add(whoosh(0.4,300,6000,rev=True),1.1,0.5)

# era groove: 120 bpm
mel=[[0,4,7,4],[2,5,9,7],[4,7,9,11],[5,9,12,9],[7,9,12,14],[4,7,11,9],[9,12,14,12],[7,11,14,16],[9,12,16,14],[12,14,17,19]]
for k in range(10):
    t0=1.5+k
    add(taiko(1.0 if k%2==0 else 0.8),t0,0.85)
    add(tom(),t0+0.5,0.45,pan=0.3)
    if k>=4: add(tom(),t0+0.75,0.3,pan=-0.3)
    for j,d in enumerate(mel[k]): add(pluck(deg(d),1.2,0.4+0.05*k),t0+j*0.25,0.24,pan=(-0.4 if j%2 else 0.4))
    for j in range(8):  # shaker 16ths-ish
        n=bpv(rs.standard_normal(int(sr*0.05)),5000,14000)*np.exp(-T(0.05)*80)
        add(n,t0+j*0.125,0.06+0.04*(j%2==0)+0.01*k,pan=0.6)
    if k>0: add(whoosh(0.32,250,5000),t0-0.08,0.35,pan=-0.2+0.04*k)
# doors (S9 at 9.5): slam at 9.665
th=T(0.8); slam=np.sin(2*np.pi*(60+40*np.exp(-th*20))*th)*np.exp(-th*7)+lpv(rs.standard_normal(len(th)),900)*np.exp(-th*18)*0.8
add(np.tanh(slam*2),9.665,0.8); add(whoosh(0.5,120,1500),9.75,0.4)
# glitch (S10 at 10.5)
for i in range(6):
    g=np.sign(np.sin(2*np.pi*(200+i*170)*T(0.04)))*0.5+rs.standard_normal(int(sr*0.04))*0.3
    add(g*np.exp(-T(0.04)*30),10.5+i*0.05,0.18,pan=(-1)**i*0.6)
# riser + recap ticks
rt=T(1.4); riser=bpv(rs.standard_normal(len(rt)),800,9000)*(rt/1.4)**2.5; riser+=np.sin(2*np.pi*np.cumsum(200+800*(rt/1.4)**2)/sr)*(rt/1.4)**3*0.3
add(riser,10.82,0.45)
for i in range(10): add(pluck(deg(5+i),0.4,0.9),11.5+i*0.05,0.22,pan=-0.6+i*0.13)
# seal impact
add(taiko(1.3),12.22,1.0); add(gong(73.4,3.0),12.22,0.55)
sub=np.sin(2*np.pi*(55-15*T(1.5)/1.5)*T(1.5))*np.exp(-T(1.5)*2.5); add(sub,12.22,0.6)
add(lpv(rs.standard_normal(int(sr*0.6)),3000)*np.exp(-T(0.6)*9),12.22,0.35)
for i,d in enumerate([0,4,7,9,12,14]): add(pluck(deg(d),2.6,0.35),12.62+i*0.09,0.2,pan=-0.5+i*0.2)
add(pluck(deg(10),3.0,0.3),13.6,0.18)

# reverb (FFT convolution with decaying noise IR)
ir_t=T(2.2); irL=rs.standard_normal(len(ir_t))*np.exp(-ir_t*3.2); irR=rs.standard_normal(len(ir_t))*np.exp(-ir_t*3.2)
irL=lpv(irL,6000); irR=lpv(irR,6000)
def conv(x,ir):
    n=len(x)+len(ir); F=1<<(n-1).bit_length(); return np.fft.irfft(np.fft.rfft(x,F)*np.fft.rfft(ir,F),F)[:len(x)]
wL=conv(L,irL); wR=conv(R,irR); s=np.max(np.abs(wL))/max(np.max(np.abs(L)),1e-9)
L=L+wL/s*0.28; R=R+wR/s*0.28
fade=np.clip((DUR-np.arange(N)/sr)/0.6,0,1); L*=fade; R*=fade
m=max(np.max(np.abs(L)),np.max(np.abs(R))); L=np.tanh(L/m*1.2)/np.tanh(1.2)*0.89; R=np.tanh(R/m*1.2)/np.tanh(1.2)*0.89
out=(np.stack([L,R],1)*32767).astype(np.int16)
with wave.open(sys.argv[1],'wb') as w: w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr); w.writeframes(out.tobytes())
print('ok')
