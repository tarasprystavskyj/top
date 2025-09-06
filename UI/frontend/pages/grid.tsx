
import {useEffect,useState} from 'react';
export default function Grid(){ const [cfgs,setCfgs]=useState<any[]>([]); const [cfg,setCfg]=useState(''); const [bars,setBars]=useState(5000);
 const [axes,setAxes]=useState<any[]>([{path:'entry.type',values:['market','limit_aggressive']},{path:'entry.offset_ticks',values:[1,2]}]);
 useEffect(()=>{fetch('/api/configs').then(r=>r.json()).then(setCfgs)},[]);
 async function start(){ const j=await fetch('/api/grid',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cfg_name:cfg,limit_bars:bars,grid:axes})}).then(r=>r.json()); alert('Grid started: '+j.job_id); }
 return <div><h3>Grid Search</h3><div><select value={cfg} onChange={e=>setCfg(e.target.value)}><option value=''>--pick config--</option>{cfgs.map((c:any)=><option key={c.name} value={c.name}>{c.name}</option>)}</select> <input type='number' value={bars} onChange={e=>setBars(parseInt(e.target.value||'0'))}/> <button onClick={start}>Start</button></div></div>;
}
