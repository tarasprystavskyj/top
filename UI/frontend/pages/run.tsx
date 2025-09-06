
import {useEffect,useState} from 'react';
export default function Run(){ const [cfgs,setCfgs]=useState<any[]>([]); const [cfg,setCfg]=useState(''); const [bars,setBars]=useState(5000); const [job,setJob]=useState<any>(null); const [res,setRes]=useState<any>(null);
 useEffect(()=>{fetch('/api/configs').then(r=>r.json()).then(setCfgs)},[]);
 async function start(){ const j=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cfg_name:cfg,limit_bars:bars})}).then(r=>r.json()); setJob(j); }
 useEffect(()=>{ if(!job) return; const id=setInterval(async()=>{ const st=await fetch('/api/jobs/'+job.job_id+'/status').then(r=>r.json()); if(st.status==='done'||st.status==='error'){ const rs=await fetch('/api/jobs/'+job.job_id+'/result').then(r=>r.json()); setRes(rs); clearInterval(id);} },1000); return ()=>clearInterval(id); },[job]);
 return <div><h3>Run Backtest</h3><div><select value={cfg} onChange={e=>setCfg(e.target.value)}><option value=''>--pick config--</option>{cfgs.map((c:any)=><option key={c.name} value={c.name}>{c.name}</option>)}</select> <input type='number' value={bars} onChange={e=>setBars(parseInt(e.target.value||'0'))}/> <button onClick={start}>Start</button></div>
 {job && <p>Job: {job.job_id}</p>}{res && <pre>{JSON.stringify(res.summary,null,2)}</pre>}</div>;
}
